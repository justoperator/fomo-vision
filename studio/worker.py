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
