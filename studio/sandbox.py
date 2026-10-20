"""
Resource manager. Starts worker.py in a SEPARATE process and applies limits:

  Windows : Job Object (process committed-memory limit + hard CPU cap + KILL_ON_JOB_CLOSE) — via ctypes
  Linux   : cgroups v2 (memory.max, cpu.max, cpuset.cpus) directly (root) or via `systemd-run --user --scope`
  Everywhere: watchdog (USS/RSS every 0.25 s -> kill when exceeded), CPU affinity (psutil),
            soft CPU cap inside the worker, per-frame timeout with forced termination.
"""
import collections
import ctypes
import json
import os
import platform
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time

import numpy as np
import psutil
from django.conf import settings

IS_WIN = os.name == "nt"
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")
CG_ROOT = "/sys/fs/cgroup"


class WorkerError(Exception):
    pass


# ----------------------------------------------------------------------------- limits
def caps():
    return {"max_ram_mb": int(settings.FOMO_MAX_RAM_MB), "cpu_count": os.cpu_count() or 1, "os": platform.system()}


def default_limits():
    c = caps()
    return {"ram_mb": c["max_ram_mb"], "cpu_percent": 100, "cpu_cores": min(2, c["cpu_count"]), "timeout_s": 5}


def clamp_limits(d=None):
    c, base = caps(), default_limits()
    out = {}
    for key, lo, hi in (("ram_mb", 2, c["max_ram_mb"]), ("cpu_percent", 5, 100),
                        ("cpu_cores", 1, c["cpu_count"]), ("timeout_s", 1, 60)):
        try:
            v = int(float((d or {}).get(key, base[key])))
        except (TypeError, ValueError):
            v = base[key]
        out[key] = max(lo, min(hi, v))
    return out


def _env():
    return {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "TF_CPP_MIN_LOG_LEVEL": "1"}


def _flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0


def inspect_model(path):
    """Reads .tflite parameters in a short-lived subprocess (the main server never imports TF)."""
    r = subprocess.run([sys.executable, WORKER, "--inspect", path], capture_output=True, timeout=180,
                       env=_env(), creationflags=_flags())
    lines = r.stdout.decode("utf-8", "replace").strip().splitlines()
    if r.returncode != 0 or not lines:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()[-400:] or "the worker did not respond")
    return json.loads(lines[-1])


# ----------------------------------------------------------------------------- Windows: Job Objects
if IS_WIN:
    from ctypes import wintypes

    class _IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _EXT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _CPURATE(ctypes.Structure):
        _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


def _win_job(limits, pids):
    """Creates a Job Object with limits and adds processes to it. Returns (handle, [notes])."""
    job = _k32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(f"CreateJobObject: {ctypes.get_last_error()}")
    notes = []
    ext = _EXT()
    ext.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
    if settings.FOMO_HARD_MEMORY:
        ext.BasicLimitInformation.LimitFlags |= 0x100  # PROCESS_MEMORY (committed memory)
        ext.ProcessMemoryLimit = limits["ram_mb"] << 20
        notes.append(f"Job Object: RAM {limits['ram_mb']} MB")
    if not _k32.SetInformationJobObject(job, 9, ctypes.byref(ext), ctypes.sizeof(ext)):
        raise OSError(f"SetInformationJobObject(limits): {ctypes.get_last_error()}")
    total = os.cpu_count() or 1
    rate = int(max(1, min(10000, limits["cpu_percent"] / 100 * limits["cpu_cores"] / total * 10000)))
    cr = _CPURATE(0x1 | 0x4, rate)  # ENABLE | HARD_CAP
    if _k32.SetInformationJobObject(job, 15, ctypes.byref(cr), ctypes.sizeof(cr)):
        notes.append(f"Job Object: CPU hard cap {rate / 100:.1f}% of the system")
    for pid in pids:
        h = _k32.OpenProcess(0x1F0FFF, False, pid)
        if h:
            if not _k32.AssignProcessToJobObject(job, h):
                notes.append(f"! pid {pid} was not added to the Job (code {ctypes.get_last_error()})")
            _k32.CloseHandle(h)
    return job, notes


# ----------------------------------------------------------------------------- Linux: cgroups v2 / systemd
_systemd = None


def _systemd_ok():
    global _systemd
    if _systemd is None:
        _systemd = False
        if shutil.which("systemd-run") and os.environ.get("XDG_RUNTIME_DIR"):
            try:
                r = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--collect",
                                    "-p", "MemoryMax=64M", "true"], capture_output=True, timeout=6)
                _systemd = r.returncode == 0
            except Exception:  # noqa
                pass
    return _systemd


def _cg_create(limits, tag):
    if IS_WIN or not os.path.exists(CG_ROOT + "/cgroup.controllers") or not os.access(CG_ROOT, os.W_OK):
        return None, []
    try:
        base = CG_ROOT + "/fomo_studio"
        os.makedirs(base, exist_ok=True)
        for path in (CG_ROOT, base):
            for c in ("memory", "cpu", "cpuset"):
                try:
                    with open(path + "/cgroup.subtree_control", "w") as f:
                        f.write("+" + c)
                except OSError:
                    pass
        grp = f"{base}/{tag}"
        os.makedirs(grp, exist_ok=True)

        def put(name, val):
            try:
                with open(f"{grp}/{name}", "w") as f:
                    f.write(str(val))
                return True
            except OSError:
                return False

        notes = []
        if settings.FOMO_HARD_MEMORY and put("memory.max", limits["ram_mb"] << 20):
            put("memory.swap.max", 0)
            notes.append(f"cgroup v2: memory.max={limits['ram_mb']}M")
        quota = int(limits["cpu_percent"] / 100 * limits["cpu_cores"] * 100000)
        if put("cpu.max", f"{max(quota, 1000)} 100000"):
            notes.append(f"cgroup v2: cpu.max={quota}/100000")
        cpus = sorted(psutil.Process().cpu_affinity())[:limits["cpu_cores"]]
        if put("cpuset.cpus", ",".join(map(str, cpus))):
            notes.append(f"cpuset {','.join(map(str, cpus))}")
        return grp, notes
    except OSError:
        return None, []


# ----------------------------------------------------------------------------- worker
class ModelWorker:
    def __init__(self, model_path, limits, log):
        self.path, self.limits, self.log = model_path, clamp_limits(limits), log
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.proc = self.ps = None
        self.info = None
        self.enforcement = []
        self.err_tail = collections.deque(maxlen=12)
        self.uss = self.peak = self.cpu = 0.0
        self.kill_reason = self.fatal = None
        self._job = self._cg = None
        self._stopping = False
        self._pobj = {}
        self._starts = collections.deque(maxlen=4)
        self.generation = 0
