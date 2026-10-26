from django.contrib import admin
from django.urls import include, path
from django.shortcuts import redirect

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("social/", include("social_django.urls", namespace="social")),
    path("", lambda r: redirect("projects")),
    path("", include("studio.urls")),
]
