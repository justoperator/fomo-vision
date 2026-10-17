from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST


def _safe_next(request, value):
    if value and url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()}):
        return value
    return "/projects/"


def auth_view(request):
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if request.user.is_authenticated:
        return redirect(_safe_next(request, next_url))
    ctx = {"tab": request.GET.get("tab", "login"), "next": next_url, "errors": [], "form": {}}

    if request.method == "POST":
        action = request.POST.get("action")
        username = request.POST.get("username", "").strip()
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")
        ctx["tab"] = "signup" if action == "signup" else "login"
        ctx["form"] = {"username": username, "email": email}

        if action == "login":
            if "@" in username:  # allow signing in with email too
                u = User.objects.filter(email__iexact=username).first()
                username = u.username if u else username
            user = authenticate(request, username=username, password=password)
            if user:
                login(request, user)
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
            except ValidationError as e:
                errors += list(e.messages)
            if not errors:
                user = User.objects.create_user(username=username, email=email, password=password)
                login(request, user)
                return redirect(_safe_next(request, next_url))
            ctx["errors"] = errors
    return render(request, "accounts/auth.html", ctx)


@require_POST
def logout_view(request):
    logout(request)
    return redirect("auth")
