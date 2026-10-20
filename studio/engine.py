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
