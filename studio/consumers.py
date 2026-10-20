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
