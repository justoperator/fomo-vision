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

    # --- start
    def start(self):
        lim = self.limits
        self.generation += 1
        gen = self.generation
        self.q = queue.Queue()
        self.kill_reason = None
        self._stopping = False
        self.enforcement = []
        self.uss = self.peak = self.cpu = 0.0
        self._pobj = {}
        prefix, grp = [], None
        if not IS_WIN:
            grp, notes = _cg_create(lim, f"w{os.getpid()}_{gen}")
            self.enforcement += notes
            if grp:
                self._cg = grp
            elif _systemd_ok():
                props = [f"CPUQuota={int(lim['cpu_percent'] * lim['cpu_cores'])}%"]
                if settings.FOMO_HARD_MEMORY:
                    props += [f"MemoryMax={lim['ram_mb']}M", "MemorySwapMax=0"]
                prefix = ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
                for p in props:
                    prefix += ["-p", p]
                self.enforcement.append("systemd --user scope: " + ", ".join(props))
        self.proc = subprocess.Popen(prefix + [sys.executable, WORKER], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_env(),
                                     creationflags=_flags())
        self.ps = psutil.Process(self.proc.pid)
        if grp:
            try:
                with open(grp + "/cgroup.procs", "w") as f:
                    f.write(str(self.proc.pid))
            except OSError as e:
                self.enforcement.append(f"cgroup.procs: {e}")
        if IS_WIN:
            try:  # the Windows venv launcher spawns a child python — the Job must cover both
                venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
                end, pids = time.time() + 1.5, set()
                while True:
                    pids |= {self.proc.pid} | {c.pid for c in self.ps.children(recursive=True)}
                    if len(pids) > 1 or not venv or time.time() > end:
                        break
                    time.sleep(0.05)
                self._job, notes = _win_job(lim, pids)
                self.enforcement += notes
            except Exception as e:  # noqa
                self.enforcement.append(f"Job Object unavailable: {e}")
        try:
            cpus = sorted(self.ps.cpu_affinity())[:lim["cpu_cores"]]
            self.ps.cpu_affinity(cpus)
            self.enforcement.append(f"affinity {len(cpus)} core(s)")
        except Exception:  # noqa
            pass
        self.enforcement.append(f"watchdog RAM ≤ {lim['ram_mb']} MB")
        for fn in (self._read_out, self._read_err, self._watch):
            threading.Thread(target=fn, args=(gen,), daemon=True).start()
        self._send(b"C", json.dumps({"model_path": self.path, "cores": lim["cpu_cores"],
                                     "cpu_percent": lim["cpu_percent"]}).encode())
        t0, deadline = time.time(), time.time() + 120
        while True:
            try:
                kind, payload = self.q.get(timeout=0.5)
            except queue.Empty:
                if time.time() > deadline:
                    self.kill("startup")
                    raise self._died("Worker start-up timed out")
                continue
            if kind == b"I":
                self.info = json.loads(payload)
                self.info["load_ms"] = int((time.time() - t0) * 1000)
                self._starts.append(time.time())
                return self.info
            if kind == b"X":
                raise self._died()

    def _send(self, kind, payload=b""):
        try:
            self.proc.stdin.write(kind + struct.pack(">I", len(payload)) + payload)
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise self._died()

    def _read_out(self, gen):
        out = self.proc.stdout
        try:
            while True:
                head = out.read(5)
                if len(head) < 5:
                    break
                n = struct.unpack(">I", head[1:])[0]
                self.q.put((head[:1], out.read(n) if n else b""))
        except (OSError, ValueError):
            pass
        if gen == self.generation:
            self.q.put((b"X", b""))

    def _read_err(self, gen):
        try:
            for raw in iter(self.proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                self.err_tail.append(line)
                low = line.lower()
                lvl = "ERROR" if any(k in low for k in ("error", "traceback", "memory", "exception")) else \
                      "WARN" if low.startswith("warn") else "INFO"
                if gen == self.generation:
                    self.log(lvl, "[worker] " + line[:300])
        except (OSError, ValueError):
            pass

    def _watch(self, gen):
        lim = self.limits
        cap = lim["ram_mb"] * 1048576
        while gen == self.generation and not self._stopping and self.proc.poll() is None:
            try:
                mem, cpu = 0, 0.0
                for p in [self.ps] + self.ps.children(recursive=True):
                    p = self._pobj.setdefault(p.pid, p)
                    try:
                        try:
                            mem += p.memory_full_info().uss
                        except (psutil.AccessDenied, AttributeError, NotImplementedError):
                            mem += p.memory_info().rss
                        cpu += p.cpu_percent(None)
                    except psutil.NoSuchProcess:
                        pass
                self.uss, self.cpu = mem / 1048576, cpu / lim["cpu_cores"]
                self.peak = max(self.peak, self.uss)
                if mem > cap * 1.02:
                    self.kill("ram")
                    return
            except psutil.NoSuchProcess:
                return
            time.sleep(0.25)

    # --- frame
    def alive(self):
        return bool(self.proc) and self.proc.poll() is None

    def infer(self, jpeg, brightness=100):
        with self.lock:
            if self.fatal:
                raise WorkerError(self.fatal)
            if not self.alive() and self.kill_reason == "ram":
                raise self._died()  # the watchdog killed the worker for exceeding RAM — fatal until the limit changes
            if not self.alive():
                recent = [t for t in self._starts if time.time() - t < 60]
                if len(recent) >= 3:
                    self.fatal = "The worker keeps crashing — stopped. Check the logs and press “Apply”."
                    raise WorkerError(self.fatal)
                self.log("WARN", "Restarting the worker…")
                self.start()
            self._send(b"F", struct.pack(">H", max(20, min(250, int(brightness)))) + jpeg)
            try:
                kind, p = self.q.get(timeout=self.limits["timeout_s"])
            except queue.Empty:
                self.kill("timeout")
                raise WorkerError(f"Frame timeout ({self.limits['timeout_s']} s) — worker restarted")
            if kind == b"R":
                n = struct.unpack(">I", p[:4])[0]
                head = json.loads(p[4:4 + n])
                grid = np.frombuffer(p[4 + n:], dtype="<f4").reshape(head["shape"])
                return grid, head["ms"]
            if kind == b"E":
                raise WorkerError(p.decode("utf-8", "replace"))
            raise self._died()

    # --- shutdown
    def kill(self, reason):
        self.kill_reason = reason
        try:
            for p in self.ps.children(recursive=True) + [self.ps]:
                try:
                    p.kill()
                except psutil.NoSuchProcess:
                    pass
        except psutil.NoSuchProcess:
            pass
        try:
            self.proc.wait(timeout=3)  # wait for exit so alive() is immediately accurate
        except Exception:  # noqa
            pass

    def _died(self, prefix=None):
        try:
            code = self.proc.wait(timeout=2)
        except Exception:  # noqa
            code = self.proc.poll()
        time.sleep(0.15)
        tail = " | ".join(list(self.err_tail)[-3:])
        lim = self.limits
        blob = tail.lower()
        oom = self.kill_reason == "ram" or "memoryerror" in blob or "bad_alloc" in blob or "cannot allocate" in blob \
            or code in (-9, 137, 3, 0xC0000017, 3221225495)
        if self.kill_reason == "timeout":
            msg = f"Frame timeout ({lim['timeout_s']} s) — worker restarted"
        elif oom:
            msg = (f"Not enough RAM within the {lim['ram_mb']} MB limit (peak ≈ {self.peak:.0f} MB). "
                   f"Raise the limit or use a lighter engine (ai-edge-litert).")
            self.fatal = msg
        else:
            msg = (prefix or f"Worker exited (code {code})") + (f": {tail}" if tail else "")
            self.fatal = msg
        return WorkerError(msg)

    def stop(self):
        self._stopping = True
        self.generation += 1
        try:
            if self.alive():
                try:
                    self._send(b"Q")
                except WorkerError:
                    pass
                try:
                    self.proc.wait(timeout=1)
                except Exception:  # noqa
                    self.kill("stop")
        except Exception:  # noqa
            pass
        if self._job and IS_WIN:
            _k32.CloseHandle(self._job)
            self._job = None
        if self._cg:
            for _ in range(10):
                try:
                    os.rmdir(self._cg)
                    break
                except OSError:
                    time.sleep(0.05)
            self._cg = None

    def stats(self):
        return {"alive": self.alive(), "rss_mb": round(self.uss, 1), "peak_mb": round(self.peak, 1),
                "cpu": round(min(self.cpu, 999), 1), "enforcement": self.enforcement,
                "engine": (self.info or {}).get("engine"), "fatal": self.fatal}
