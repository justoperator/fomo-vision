from django.contrib import admin
from .models import Invite, Membership, MLModel, Project

admin.site.register(Project)
admin.site.register(Membership)
admin.site.register(Invite)
admin.site.register(MLModel)
