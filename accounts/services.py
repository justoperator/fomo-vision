import resend
from django.conf import settings
from django.core.cache import cache
from django.template.loader import render_to_string
from django.utils.html import strip_tags


OTP_INTERVAL = 300
PENDING_SIGNUP_TIMEOUT = 600


def store_pending_signup(token, data, secret):
    cache.set(
        f"pending_signup:{token}",
        {"data": data, "secret": secret},
        timeout=PENDING_SIGNUP_TIMEOUT,
    )


def get_pending_signup(token):
    return cache.get(f"pending_signup:{token}")


def delete_pending_signup(token):
    cache.delete(f"pending_signup:{token}")


def send_signup_otp(email, otp_code):
    api_key = getattr(settings, "RESEND_API_KEY", "")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not configured.")

    resend.api_key = api_key
    html_content = render_to_string("emails/otp_email.html", {"otp_code": otp_code})
    params = {
        "from": settings.RESEND_FROM_EMAIL,
        "to": [email],
        "subject": "Your FOMO Studio confirmation code",
        "html": html_content,
        "text": strip_tags(html_content),
    }
    return resend.Emails.send(params)
