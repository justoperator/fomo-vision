"""
Isolated inference worker. Started as a separate process (python worker.py) by the resource manager
(studio/sandbox.py), which applies OS limits. Does NOT import Django and avoids importing TensorFlow
when possible: the lighter the engine (ai-edge-litert / tflite-runtime), the less RAM.

Protocol over stdin/stdout: [1-byte type][4-byte length][data]
  manager -> worker:  C (JSON config) | F ([2-byte brightness %][JPEG]) | Q (quit)
  worker -> manager:  I (JSON model info) | R ([4-byte JSON length][JSON][float32 grid]) | E (error text)
All logs go to stderr.
"""
import importlib
import io
import json
import os
import struct
import sys
import time

_IN = sys.stdin.buffer
_OUT = sys.stdout.buffer
sys.stdout = sys.stderr  # a stray print() cannot corrupt the protocol


def _read(n):
    buf = b""
    while len(buf) < n:
        chunk = _IN.read(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


def recv():
    head = _read(5)
    n = struct.unpack(">I", head[1:])[0]
    return head[:1], (_read(n) if n else b"")


def send(kind, payload=b""):
    _OUT.write(kind + struct.pack(">I", len(payload)) + payload)
    _OUT.flush()


def log(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def import_runtime():
    for mod in ("ai_edge_litert.interpreter", "tflite_runtime.interpreter"):
        try:
            return importlib.import_module(mod).Interpreter, mod.split(".")[0]
        except ImportError:
            continue
    from tensorflow.lite.python.interpreter import Interpreter  # heavy fallback
    log("WARN: using TensorFlow (needs 300+ MB RAM). Install: pip install ai-edge-litert")
    return Interpreter, "tensorflow"


class Model:
    def __init__(self, path, threads):
        import numpy as np
        from PIL import Image
        self.np, self.Image = np, Image
        cls, self.engine = import_runtime()
        try:
            self.it = cls(model_path=path, num_threads=max(1, threads))
        except TypeError:
            self.it = cls(model_path=path)
        self.it.allocate_tensors()
        self.inp = self.it.get_input_details()[0]
        self.out = self.it.get_output_details()[0]
        shape = self.inp["shape"]
        self.h, self.w = int(shape[1]), int(shape[2])
        self.channels = int(shape[3]) if len(shape) > 3 else 3
        self.in_dtype = self.inp["dtype"]
        self.in_scale, self.in_zp = self._q(self.inp)
        self.out_scale, self.out_zp = self._q(self.out)

    @staticmethod
    def _q(d):
        q = d.get("quantization_parameters", {}) or {}
        scales, zps = q.get("scales", []), q.get("zero_points", [])
        scale = float(scales[0]) if len(scales) and float(scales[0]) != 0 else None
        return scale, (float(zps[0]) if len(zps) else 0.0)

    def info(self):
        o = [int(x) for x in self.out["shape"]]
        return {"engine": self.engine, "input_width": self.w, "input_height": self.h,
                "input_channels": self.channels, "input_dtype": self.np.dtype(self.in_dtype).name,
                "grid_h": o[1] if len(o) == 4 else 0, "grid_w": o[2] if len(o) == 4 else 0,
                "num_classes": o[-1], "is_fomo": len(o) == 4}

    def prepare(self, jpeg, brightness):
        np = self.np
        img = self.Image.open(io.BytesIO(jpeg)).convert("L" if self.channels == 1 else "RGB")
        img = img.resize((self.w, self.h), self.Image.BILINEAR)
        data = np.asarray(img, dtype=np.float32)
        if data.ndim == 2:
            data = data[..., None]
        if brightness != 100:  # the brightness chosen by the user is seen by the model too
            data = np.clip(data * (brightness / 100.0), 0, 255)
        if self.in_dtype == np.float32:
            data = data / 255.0
        else:
            if self.in_scale:
                data = (data / 255.0) / self.in_scale + self.in_zp
            elif self.in_dtype == np.int8:
                data = data - 128
            ii = np.iinfo(self.in_dtype)
            data = np.clip(np.round(data), ii.min, ii.max).astype(self.in_dtype)
        return data[None]

    def infer(self, jpeg, brightness):
        np = self.np
        data = self.prepare(jpeg, brightness)
        self.it.set_tensor(self.inp["index"], data)
        t = time.perf_counter()
        self.it.invoke()
        ms = (time.perf_counter() - t) * 1000
        raw = self.it.get_tensor(self.out["index"])[0].astype(np.float32)
        if self.out_scale:
            grid = (raw - self.out_zp) * self.out_scale
        elif self.out["dtype"] != np.float32:
            grid = raw / 255.0
        else:
            grid = raw
        return np.ascontiguousarray(grid, dtype="<f4"), ms


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--inspect":
        m = Model(sys.argv[2], 1)
        _OUT.write((json.dumps(m.info()) + "\n").encode())
        _OUT.flush()
        return
    kind, payload = recv()
    cfg = json.loads(payload)
    model = Model(cfg["model_path"], cfg.get("cores", 1))
    send(b"I", json.dumps(model.info()).encode())
    pct, cores = cfg.get("cpu_percent", 100), max(1, cfg.get("cores", 1))
    while True:
        kind, p = recv()
        if kind == b"Q":
            return
        if kind != b"F":
            continue
        w0, c0 = time.perf_counter(), time.process_time()
        try:
            grid, ms = model.infer(p[2:], struct.unpack(">H", p[:2])[0])
        except MemoryError:
            raise
        except Exception as e:  # noqa
            send(b"E", f"{type(e).__name__}: {e}".encode())
            continue
        head = json.dumps({"ms": ms, "shape": list(grid.shape)}).encode()
        send(b"R", struct.pack(">I", len(head)) + head + grid.tobytes())
        if pct < 100:  # soft CPU cap on top of the hard one (Job Object / cpu.max)
            sleep = (time.process_time() - c0) / (pct / 100.0 * cores) - (time.perf_counter() - w0)
            if sleep > 0:
                time.sleep(min(sleep, 2.0))


if __name__ == "__main__":
    try:
        main()
    except EOFError:
        pass  # the manager has exited
    except MemoryError:
        log("MemoryError: not enough memory within the limit")
        sys.exit(3)
