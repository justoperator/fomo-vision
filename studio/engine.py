"""
Server side of FOMO: grid post-processing, tracker, metrics and project state.
Inference itself runs in an isolated worker (studio/worker.py) managed by studio/sandbox.py
(RAM/CPU limits, timeout). The main Django process does NOT import tflite/TensorFlow.
IMPORTANT: state lives in process memory, so run ONE process (daphne without multiple workers).
"""
import collections
import threading
import time

import numpy as np
import psutil

from .sandbox import ModelWorker, WorkerError, clamp_limits, default_limits

# ----------------------------------------------------------------------------- post-processing
def extract_objects(grid, threshold, unique):
    """FOMO grid -> list of objects. unique=True merges neighbouring cells of the same class into one object."""
    gh, gw, _ = grid.shape
    cls = grid.argmax(-1)
    conf = grid.max(-1)
    ys, xs = np.nonzero((cls > 0) & (conf >= threshold))
    cells = {(int(y), int(x)): (int(cls[y, x]), float(conf[y, x])) for y, x in zip(ys, xs)}
    objs = []
    if not unique:
        for (y, x), (c, p) in cells.items():
            objs.append({"class_id": c, "x": (x + .5) / gw, "y": (y + .5) / gh, "w": 1 / gw, "h": 1 / gh,
                         "confidence": p, "cells": 1})
        return objs
    seen = set()
    for start in cells:
        if start in seen:
            continue
        c0 = cells[start][0]
        stack, comp = [start], []
        seen.add(start)
        while stack:
            cy, cx = stack.pop()
            comp.append((cy, cx))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    n = (cy + dy, cx + dx)
                    if n in cells and n not in seen and cells[n][0] == c0:
                        seen.add(n)
                        stack.append(n)
        wts = np.array([cells[p][1] for p in comp])
        py = np.array([p[0] for p in comp]) + .5
        px = np.array([p[1] for p in comp]) + .5
        objs.append({"class_id": c0, "x": float((px * wts).sum() / wts.sum() / gw),
                     "y": float((py * wts).sum() / wts.sum() / gh),
                     "w": float((px.max() - px.min() + 1) / gw), "h": float((py.max() - py.min() + 1) / gh),
                     "confidence": float(wts.max()), "cells": len(comp)})
    return objs


class Tracker:
    """Simple centroid tracker: stable object numbering across frames."""

    def __init__(self, max_dist=0.12, max_missed=8):
        self.tracks = {}  # id -> dict(x, y, class_id, missed)
        self.next_id = 1
        self.max_dist, self.max_missed = max_dist, max_missed
        self.total = collections.Counter()  # unique objects per class for the session

    def update(self, objs):
        pairs = []
        for i, o in enumerate(objs):
            for tid, t in self.tracks.items():
                if t["class_id"] == o["class_id"]:
                    d = ((t["x"] - o["x"]) ** 2 + (t["y"] - o["y"]) ** 2) ** .5
                    if d <= self.max_dist:
                        pairs.append((d, i, tid))
        used_o, used_t = set(), set()
        for d, i, tid in sorted(pairs):
            if i in used_o or tid in used_t:
                continue
            used_o.add(i)
            used_t.add(tid)
            o = objs[i]
            self.tracks[tid].update(x=o["x"], y=o["y"], missed=0)
            o["track_id"] = tid
        for i, o in enumerate(objs):
            if i not in used_o:
                tid = self.next_id
                self.next_id += 1
                self.tracks[tid] = {"x": o["x"], "y": o["y"], "class_id": o["class_id"], "missed": 0}
                self.total[o["class_id"]] += 1
                o["track_id"] = tid
        for tid in list(self.tracks):
            if tid not in used_t and tid not in {o.get("track_id") for o in objs}:
                self.tracks[tid]["missed"] += 1
                if self.tracks[tid]["missed"] > self.max_missed:
                    del self.tracks[tid]
        return objs


# ----------------------------------------------------------------------------- system metrics
_SYS = {"cpu": 0.0, "proc_cpu": 0.0, "ram_used_mb": 0.0, "ram_total_mb": 0.0, "ram_percent": 0.0}
_sampler_started = False
_runtimes = {}
_rt_lock = threading.Lock()


def _sampler():
    proc = psutil.Process()
    proc.cpu_percent(None)
    psutil.cpu_percent(None)
    while True:
        time.sleep(0.5)
        vm = psutil.virtual_memory()
        _SYS.update(cpu=psutil.cpu_percent(None), proc_cpu=proc.cpu_percent(None),
                    ram_used_mb=proc.memory_info().rss / 1048576, ram_total_mb=vm.total / 1048576,
                    ram_percent=vm.percent)


def get_runtime(pid):
    global _sampler_started
    with _rt_lock:
        if not _sampler_started:
            threading.Thread(target=_sampler, daemon=True, name="metrics").start()
            _sampler_started = True
        if pid not in _runtimes:
            _runtimes[pid] = ProjectRuntime(pid)
        return _runtimes[pid]


class Active:
    def __init__(self, mid, name, labels, info):
        self.id, self.name, self.labels, self.info = mid, name, labels, info


class ProjectRuntime:
    def __init__(self, pid):
        self.pid = pid
        self.initialized = False
        self.settings = {"model_id": None, "threshold": 0.7, "unique": True, "numbering": False,
                         "running": True, "brightness": 100, "limits": default_limits()}
        self.active = None
        self.worker = None
        self._meta = None
        self._last_err = None
        self.logs = collections.deque(maxlen=400)
        self.log_seq = 0
        self.trackers = {}
        self.frame_id = 0
        self.times = collections.deque(maxlen=30)
        self.latency = collections.deque(maxlen=30)
        self.strokes = []
        self.presence = {}
        self._last_sig = None
        self._lock = threading.Lock()

    # --- logs
    def log(self, level, msg):
        with self._lock:
            self.log_seq += 1
            self.logs.append((self.log_seq, {"ts": time.time(), "level": level, "msg": msg}))

    def logs_since(self, seq):
        with self._lock:
            return [e for s, e in self.logs if s > seq], self.log_seq

    # --- model / worker
    def _stop_worker(self):
        if self.worker:
            self.worker.stop()
            self.worker = None

    def activate(self, meta):
        self._stop_worker()
        self.active, self._meta, self._last_err = None, meta, None
        lim = clamp_limits(self.settings.get("limits"))
        self.settings["limits"] = lim
        w = ModelWorker(meta["path"], lim, self.log)
        try:
            info = w.start()
        except WorkerError as e:
            w.stop()
            self._last_err = str(e)
            self.log("ERROR", str(e))
            raise
        self.settings["running"] = True
        self.worker = w
        self.active = Active(meta["id"], meta["name"], meta["labels"], info)
        self.trackers.clear()
        self.log("INFO", f"Model “{meta['name']}” ready in {info['load_ms']} ms · engine {info['engine']} · "
                         f"input {info['input_width']}x{info['input_height']}x{info['input_channels']} "
                         f"{info['input_dtype']} · limits: RAM {lim['ram_mb']} MB, CPU {lim['cpu_percent']}% × "
                         f"{lim['cpu_cores']} core(s), timeout {lim['timeout_s']} s")
        self.log("INFO", "Enforcement: " + "; ".join(w.enforcement))

    def restart_worker(self):
        if self._meta and self.active and self.settings.get("running", True):
            self.activate(self._meta)

    def prepare_stopped(self, meta):
        """Model selected but the worker is not started (the “stopped” state after a server restart)."""
        self._meta = meta
        self.active = Active(meta["id"], meta["name"], meta["labels"], None)

    def stop_model(self):
        """Stops the worker (frees RAM) but keeps the model selection so it can be started again."""
        self._stop_worker()
        self.settings["running"] = False
        self.trackers.clear()
        self.log("INFO", "Model stopped, worker shut down — memory freed")

    def start_model(self):
        if not self._meta:
            raise WorkerError("Choose a model first")
        self.activate(self._meta)

    def deactivate(self):
        self._stop_worker()
        self.active, self._meta = None, None
        self.settings["model_id"] = None
        self.trackers.clear()
        self.log("INFO", "Model unloaded, worker stopped")

    def update_labels(self, mid, labels):
        if self.active and self.active.id == mid:
            self.active.labels = labels

    def drop_model(self, mid):
        if self.active and self.active.id == mid:
            self.deactivate()
            self.settings["model_id"] = None

    # --- frame
    def process(self, jpeg, source):
        brightness = max(20, min(250, int(self.settings.get("brightness", 100))))
        self.frame_id += 1
        fid, t0 = self.frame_id, time.perf_counter()
        base = {"frame_id": fid, "inference_ms": 0, "predictions": [], "counts": {}, "unique_total": {},
                "brightness": brightness}
        am, w = self.active, self.worker
        if am is None:
            return {**base, "error": self._last_err or "No model selected"}
        if not self.settings.get("running", True):
            return {**base, "stopped": True}  # video keeps flowing, detection is off
        if w is None:
            return {**base, "error": self._last_err or "Worker is not running"}
        s = self.settings
        try:
            grid, inf_ms = w.infer(jpeg, brightness)
        except WorkerError as e:
            msg = str(e)
            if msg != self._last_err:
                self._last_err = msg
                self.log("ERROR", msg)
            return {**base, "error": msg}
        self._last_err = None
        objs = extract_objects(grid, s["threshold"], s["unique"])
        tracker = self.trackers.setdefault(source, Tracker())
        if s["numbering"]:
            tracker.update(objs)
        counts = collections.Counter()
        for o in objs:
            lab = am.labels[o["class_id"]] if o["class_id"] < len(am.labels) else {}
            o["label"] = lab.get("name", f"Class {o['class_id']}")
            o["color"] = lab.get("color", "#ff5a1f")
            o["description"] = lab.get("description", "")
            counts[o["label"]] += 1
        self.times.append(time.time())
        self.latency.append((time.perf_counter() - t0) * 1000)
        sig = tuple(sorted(counts.items()))
        if sig != self._last_sig:
            self._last_sig = sig
            self.log("INFO", "Detected: " + (", ".join(f"{k}×{v}" for k, v in sig) or "nothing"))
        if fid % 100 == 0:
            self.log("DEBUG", f"{fid} frames, inference {inf_ms:.1f} ms, grid {grid.shape[1]}x{grid.shape[0]}")
        return {"frame_id": fid, "inference_ms": round(inf_ms, 2), "predictions": objs, "brightness": brightness,
                "counts": dict(counts), "grid": [grid.shape[1], grid.shape[0]],
                "unique_total": {(am.labels[c]["name"] if c < len(am.labels) else str(c)): n
                                 for c, n in tracker.total.items()} if s["numbering"] else {}}

    def metrics(self):
        fps = 0.0
        if len(self.times) > 1 and time.time() - self.times[-1] < 2:
            fps = (len(self.times) - 1) / max(self.times[-1] - self.times[0], 1e-6)
        lat = sum(self.latency) / len(self.latency) if self.latency and fps else 0.0
        return {**{k: round(v, 1) for k, v in _SYS.items()}, "fps": round(fps, 1), "latency_ms": round(lat, 1),
                "worker": self.worker.stats() if self.worker else None}
