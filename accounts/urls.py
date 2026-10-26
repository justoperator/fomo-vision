from django.urls import path
from . import views

urlpatterns = [
    path("", views.auth_view, name="auth"),
    path("verify-signup/", views.verify_signup_view, name="verify_signup"),
    path("logout/", views.logout_view, name="logout"),
]
