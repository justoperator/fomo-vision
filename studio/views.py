import io
import json
import os
from datetime import timedelta

import qrcode
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from . import engine, sandbox
from .models import PALETTE, Invite, Membership, MLModel, Project


# ----------------------------------------------------------------------------- helpers
def _member(request, pid, need=None):
    m = Membership.objects.select_related("project").filter(project_id=pid, user=request.user).first()
    if not m:
        raise Http404
    if need and not m.perms()[need]:
        raise PermissionError
    return m


def _json(request):
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return {}


def _notify(pid, what):
    async_to_sync(get_channel_layer().group_send)(
        f"ctl_{pid}", {"type": "broadcast", "payload": {"type": "refresh", "what": what}})


def _kick(pid, uid):
    """Closes the open WebSockets of a user who was removed from the project."""
    layer = get_channel_layer()
    for g in (f"ctl_{pid}", f"stream_{pid}"):
        async_to_sync(layer.group_send)(g, {"type": "kick", "user_id": uid})
    _notify(pid, "members")


def guarded(view):
    """Turns PermissionError into a 403 JSON response."""
    def wrapper(request, *a, **kw):
        try:
            return view(request, *a, **kw)
        except PermissionError:
            return JsonResponse({"ok": False, "error": "Not enough permissions"}, status=403)
    wrapper.__name__ = view.__name__
    return wrapper


def default_labels(n):
    out = []
    for i in range(n):
        out.append({"name": "background" if i == 0 else f"class_{i}", "description": "",
                    "color": "#8a949c" if i == 0 else PALETTE[(i - 1) % len(PALETTE)]})
    return out


def clean_labels(raw, n=None):
    labels = []
    for i, l in enumerate(raw or []):
        name = str(l.get("name", "")).strip()[:60] or f"class_{i}"
        color = str(l.get("color", ""))
        if not (color.startswith("#") and len(color) in (4, 7)):
            color = "#8a949c" if i == 0 else PALETTE[(i - 1) % len(PALETTE)]
        labels.append({"name": name, "description": str(l.get("description", ""))[:400], "color": color})
    return labels


def ser_model(m):
    return {"id": m.id, "name": m.name, "description": m.description, "labels": m.labels,
            "input": f"{m.input_width}x{m.input_height}x{m.input_channels}", "dtype": m.input_dtype,
            "grid": f"{m.grid_w}x{m.grid_h}", "num_classes": m.num_classes,
            "size_kb": round(m.file.size / 1024) if m.file and os.path.exists(m.file.path) else 0,
            "uploaded_by": m.uploaded_by.username if m.uploaded_by else ""}


def invite_urls(request, inv):
    path = f"/connect/{inv.code}/" if inv.kind == Invite.DEVICE else f"/join/{inv.code}/"
    return {"url": request.build_absolute_uri(path), "qr": f"/qr/{inv.code}.png"}


# ----------------------------------------------------------------------------- projects
@login_required
def projects_list(request):
    q = request.GET.get("q", "").strip()
    qs = Project.objects.filter(memberships__user=request.user).annotate(
        n_members=Count("memberships", distinct=True), n_models=Count("ml_models", distinct=True))
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q))
    roles = dict(Membership.objects.filter(user=request.user).values_list("project_id", "role"))
    items = [{"p": p, "role": roles.get(p.id)} for p in qs]
    return render(request, "studio/projects.html", {"items": items, "q": q})


@login_required
@require_POST
def project_create(request):
    name = request.POST.get("name", "").strip()
    if not name:
        messages.error(request, "Enter a project name.")
        return redirect("projects")
    p = Project.objects.create(owner=request.user, name=name[:120], description=request.POST.get("description", "")[:500])
    Membership.objects.create(project=p, user=request.user, role=Membership.OWNER,
                              can_control=True, can_draw=True, can_stream=True, can_upload=True)
    return redirect("workspace", pid=p.id)
