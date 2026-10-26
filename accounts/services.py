from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


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
    html_content = render_to_string("emails/otp_email.html", {"otp_code": otp_code})
    message = EmailMultiAlternatives(
        subject="Your FOMO Studio confirmation code",
        body=f"Your confirmation code is {otp_code}. It is valid for 5 minutes.",
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[email],
    )
    message.attach_alternative(html_content, "text/html")
    return message.send(fail_silently=False)
