import asyncio
import json
import time
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from . import sandbox
from .engine import get_runtime
from .models import Invite, Membership, MLModel, Project


@database_sync_to_async
def _member_perms(user, pid):
    if not user.is_authenticated:
        return None
    m = Membership.objects.filter(project_id=pid, user=user).first()
    return m.perms() if m else None


@database_sync_to_async
def _device_ok(pid, code):
    inv = Invite.objects.filter(project_id=pid, code=code, kind=Invite.DEVICE).first()
    return bool(inv and inv.is_valid())


@database_sync_to_async
def _load_project_settings(pid):
    p = Project.objects.filter(pk=pid).first()
    return dict(p.settings) if p else {}


@database_sync_to_async
def _save_project_settings(pid, settings):
    Project.objects.filter(pk=pid).update(settings=settings)


@database_sync_to_async
def _model_meta(pid, mid):
    m = MLModel.objects.filter(pk=mid, project_id=pid).first()
    if not m:
        return None
    return {"id": m.id, "name": m.name, "path": m.file.path, "labels": m.labels}


def _jpeg(data):
    """Accepts a raw JPEG, or the older format with a 2-byte brightness prefix (a phone may still run a cached script)."""
    if data[:2] == b"\xff\xd8":
        return data
    if len(data) > 4 and data[2:4] == b"\xff\xd8":
        return data[2:]
    return data


class StreamConsumer(AsyncWebsocketConsumer):
    """/ws/stream/<pid>/ — binary JPEG frames from a source; everyone in the project receives
    a packet [4-byte JSON length][result JSON][JPEG]."""

    async def connect(self):
        self.pid = self.scope["url_route"]["kwargs"]["pid"]
        token = parse_qs(self.scope["query_string"].decode()).get("t", [""])[0]
        perms = await _member_perms(self.scope["user"], self.pid)
        self.token, self.kind, self._chk = token, "user", time.time()
        if perms:
            self.can_stream, self.name = perms["stream"], self.scope["user"].username
        elif token and await _device_ok(self.pid, token):
            self.can_stream, self.name, self.kind = True, "Phone", "device"
        else:
            await self.close(code=4403)
            return
        self.rt = get_runtime(self.pid)
        self.group = f"stream_{self.pid}"
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        await self.send(text_data=json.dumps({"type": "hello", "can_stream": self.can_stream}))

    async def disconnect(self, code):
        if hasattr(self, "group"):
            await self.channel_layer.group_discard(self.group, self.channel_name)
            self.rt.trackers.pop(self.channel_name, None)

    async def receive(self, text_data=None, bytes_data=None):
        if not bytes_data or not self.can_stream:
            return
        if time.time() - self._chk > 5:  # permissions/invites may have changed during the stream
            self._chk = time.time()
            if self.kind == "user":
                perms = await _member_perms(self.scope["user"], self.pid)
                if not perms:
                    return await self.close(code=4403)
                self.can_stream = perms["stream"]
            elif not await _device_ok(self.pid, self.token):
                return await self.close(code=4403)
            if not self.can_stream:
                return
        jpeg = _jpeg(bytes_data)  # clean video frame (no canvas drawings); brightness is added by ProjectRuntime
        result = await asyncio.to_thread(self.rt.process, jpeg, self.channel_name)
        result["source"] = self.name
        head = json.dumps(result, ensure_ascii=False).encode()
        packet = len(head).to_bytes(4, "big") + head + jpeg
        await self.channel_layer.group_send(self.group, {"type": "packet", "data": packet})
        await self.send(text_data='{"type":"ack"}')

    async def packet(self, event):
        await self.send(bytes_data=event["data"])

    async def kick(self, event):
        if self.scope["user"].id == event["user_id"]:
            await self.close(code=4403)


class TelemetryConsumer(AsyncWebsocketConsumer):
    """/ws/telemetry/<pid>/ — metrics, logs, shared settings, drawing, presence."""

    async def connect(self):
        self.pid = self.scope["url_route"]["kwargs"]["pid"]
        user = self.scope["user"]
        perms = await _member_perms(user, self.pid)
        if not perms:
            await self.close(code=4403)
            return
        self.rt = get_runtime(self.pid)
        self.group = f"ctl_{self.pid}"
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        if not self.rt.initialized:
            await self._init_runtime()
        self.rt.presence[self.channel_name] = user.username
        self.task = asyncio.create_task(self._pump())
        await self.send_json({"type": "hello", "settings": self.rt.settings, "strokes": self.rt.strokes,
                              "perms": perms,
                              "caps": sandbox.caps()})
        await self._broadcast_presence()

    async def _init_runtime(self):
        saved = await _load_project_settings(self.pid)
        self.rt.settings.update({k: v for k, v in saved.items() if k in self.rt.settings})
        self.rt.settings["limits"] = sandbox.clamp_limits(self.rt.settings.get("limits"))
        self.rt.initialized = True
        self.rt.settings["brightness"] = max(20, min(250, int(self.rt.settings.get("brightness", 100))))
        mid = self.rt.settings.get("model_id")
        if mid:
            meta = await _model_meta(self.pid, mid)
            if meta and self.rt.settings.get("running", True):
                try:
                    await asyncio.to_thread(self.rt.activate, meta)
                except Exception as e:  # noqa
                    self.rt.log("ERROR", f"Could not load the model: {e}")
            elif meta:
                self.rt.prepare_stopped(meta)

    async def disconnect(self, code):
        if not hasattr(self, "group"):
            return
        if hasattr(self, "task"):
            self.task.cancel()
        self.rt.presence.pop(self.channel_name, None)
        await self.channel_layer.group_discard(self.group, self.channel_name)
        await self._broadcast_presence()

    async def send_json(self, obj):
        await self.send(text_data=json.dumps(obj, ensure_ascii=False))

    async def _broadcast_presence(self):
        await self.channel_layer.group_send(self.group, {
            "type": "broadcast", "payload": {"type": "presence", "online": sorted(set(self.rt.presence.values()))}})

    async def _pump(self):
        _, last = self.rt.logs_since(0)
        last = max(0, last - 40)
        try:
            while True:
                logs, last = self.rt.logs_since(last)
                await self.send_json({"type": "telemetry", "timestamp": time.time(),
                                      "system": self.rt.metrics(), "logs": logs})
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
