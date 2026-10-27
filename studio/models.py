import secrets
import uuid

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

PALETTE = ["#ff5a1f", "#0f9d8a", "#3b6cf6", "#e6a700", "#c2379b", "#6d5df6", "#2f9e44", "#d9480f"]


class Project(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="owned_projects")
    name = models.CharField("Name", max_length=120)
    description = models.TextField("Description", blank=True)
    is_public = models.BooleanField(default=False)
    # {model_id, threshold, unique, numbering, ...} — persisted between runs
    settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name


class Membership(models.Model):
    OWNER, MEMBER = "owner", "member"
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=10, choices=[(OWNER, "Owner"), (MEMBER, "Member")], default=MEMBER)
    can_control = models.BooleanField("Control model/settings", default=False)
    can_draw = models.BooleanField("Draw on video", default=True)
    can_stream = models.BooleanField("Send video", default=False)
    can_upload = models.BooleanField("Upload models", default=False)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("project", "user")

    @property
    def is_owner(self):
        return self.role == self.OWNER

    def perms(self):
        o = self.is_owner
        return {
            "owner": o,
            "control": o or self.can_control,
            "draw": o or self.can_draw,
            "stream": o or self.can_stream,
            "upload": o or self.can_upload,
        }


def make_code():
    return secrets.token_urlsafe(8)


class Invite(models.Model):
    MEMBER, DEVICE = "member", "device"
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="invites")
    code = models.CharField(max_length=24, unique=True, default=make_code)
    kind = models.CharField(max_length=10, choices=[(MEMBER, "Member"), (DEVICE, "Device (camera)")], default=MEMBER)
    can_control = models.BooleanField(default=False)
    can_draw = models.BooleanField(default=True)
    can_stream = models.BooleanField(default=False)
    can_upload = models.BooleanField(default=False)
    expires_at = models.DateTimeField(null=True, blank=True)
    max_uses = models.PositiveIntegerField(default=0)  # 0 = unlimited
    uses = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_valid(self):
        if not self.active:
            return False
        if self.expires_at and self.expires_at < timezone.now():
            return False
        if self.kind == self.MEMBER and self.max_uses and self.uses >= self.max_uses:
            return False
        return True


def model_path(instance, filename):
    return f"models/project_{instance.project_id}/{uuid.uuid4().hex[:10]}_{filename}"


class MLModel(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="ml_models")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    file = models.FileField(upload_to=model_path)
    # [{name, description, color}] — index = class_id; index 0 = background
    labels = models.JSONField(default=list)
    input_width = models.PositiveIntegerField(default=0)
    input_height = models.PositiveIntegerField(default=0)
    input_channels = models.PositiveIntegerField(default=3)
    input_dtype = models.CharField(max_length=16, blank=True)
    grid_h = models.PositiveIntegerField(default=0)
    grid_w = models.PositiveIntegerField(default=0)
    num_classes = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name
