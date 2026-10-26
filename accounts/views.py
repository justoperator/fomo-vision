import secrets

import pyotp
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .services import (
    OTP_INTERVAL,
    delete_pending_signup,
    get_pending_signup,
    send_signup_otp,
    store_pending_signup,
)


def _safe_next(request, value):
    if value and url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()}):
        return value
    return "/projects/"


def auth_view(request):
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if request.user.is_authenticated:
        return redirect(_safe_next(request, next_url))
    pending_token = request.session.get("pending_signup_token")
    pending = get_pending_signup(pending_token) if pending_token else None
    initial_tab = "verify" if pending else request.GET.get("tab", "login")
    ctx = {
        "tab": initial_tab,
        "next": next_url,
        "errors": [],
        "form": {},
        "pending_email": pending["data"]["email"] if pending else "",
    }

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "verify":
            if not pending:
                request.session.pop("pending_signup_token", None)
                ctx["tab"] = "signup"
                ctx["errors"].append("This confirmation request has expired. Please sign up again.")
                return render(request, "accounts/auth.html", ctx)
            code = request.POST.get("code", "").strip()
            ctx["tab"] = "verify"
            if not code.isdigit() or len(code) != 6:
                ctx["errors"].append("Enter the 6-digit code from your email.")
            elif not pyotp.TOTP(pending["secret"], interval=OTP_INTERVAL).verify(code):
                ctx["errors"].append("The code is incorrect or has expired.")
            else:
                data = pending["data"]
                try:
                    with transaction.atomic():
                        user = User.objects.create_user(
                            username=data["username"],
                            email=data["email"],
                        )
                        user.password = data["password_hash"]
                        user.save(update_fields=["password"])
                except IntegrityError:
                    ctx["errors"].append(
                        "This username or email was registered while you were confirming."
                    )
                else:
                    delete_pending_signup(pending_token)
                    request.session.pop("pending_signup_token", None)
                    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                    return redirect(_safe_next(request, data.get("next", "")))
            return render(request, "accounts/auth.html", ctx)

        username = request.POST.get("username", "").strip()
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")
        ctx["tab"] = "signup" if action == "signup" else "login"
        ctx["form"] = {"username": username, "email": email}

        if action == "login":
            if "@" in username:
                u = User.objects.filter(email__iexact=username).first()
                username = u.username if u else username
            user = authenticate(request, username=username, password=password)
            if user:
                login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                return redirect(_safe_next(request, next_url))
            ctx["errors"].append("Invalid username or password.")
        else:
            errors = []
            if len(username) < 3:
                errors.append("Username must be at least 3 characters.")
            elif User.objects.filter(username__iexact=username).exists():
                errors.append("This username is already taken.")
            try:
                validate_email(email)
                if User.objects.filter(email__iexact=email).exists():
                    errors.append("This email is already registered.")
            except ValidationError:
                errors.append("Enter a valid email address.")
            try:
                validate_password(password)
            except ValidationError as exc:
                errors += list(exc.messages)
            if not errors:
                token = secrets.token_urlsafe(32)
                secret = pyotp.random_base32()
                store_pending_signup(
                    token,
                    {
                        "username": username,
                        "email": email,
                        "password_hash": make_password(password),
                        "next": next_url,
                    },
                    secret,
                )
                try:
                    send_signup_otp(email, pyotp.TOTP(secret, interval=OTP_INTERVAL).now())
                except Exception as exc:
                    delete_pending_signup(token)
                    errors.append(f"Could not send the confirmation email: {exc}")
                else:
                    request.session["pending_signup_token"] = token
                    ctx["tab"] = "verify"
                    ctx["pending_email"] = email
                    return render(request, "accounts/auth.html", ctx)
            ctx["errors"] = errors
    return render(request, "accounts/auth.html", ctx)


def verify_signup_view(request):
    return redirect("auth")


@require_POST
def logout_view(request):
    logout(request)
    return redirect("auth")
