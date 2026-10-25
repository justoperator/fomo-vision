import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Settings are read from the `.env` file next to manage.py (see .env.example).
# Variables that are already set in the real environment take priority over the file.
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")
# For ngrok/Cloudflare/your own domain: CSRF_ORIGINS="https://xxx.ngrok-free.app"
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("CSRF_ORIGINS", "").split(",") if o]

INSTALLED_APPS = [
    "daphne",  # must be FIRST so runserver uses ASGI (WebSocket)
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "accounts",
    "studio",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "studio.context.assets",
    ]},
}]

# ---- Database. The engine and connection come from .env:
#   DB_ENGINE=django.db.backends.mysql      (default: SQLite, a single db.sqlite3 file)
#   DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT
DB_ENGINE = os.environ.get("DB_ENGINE", "django.db.backends.sqlite3")

if DB_ENGINE == "django.db.backends.sqlite3":
    DATABASES = {"default": {"ENGINE": DB_ENGINE, "NAME": BASE_DIR / "db.sqlite3"}}
else:
    if not os.environ.get("DB_NAME"):
        raise ImproperlyConfigured("DB_ENGINE is set, but DB_NAME is missing (check your .env file).")
    DATABASES = {"default": {
        "ENGINE": DB_ENGINE,
        "NAME": os.environ["DB_NAME"],
        "USER": os.environ.get("DB_USER", ""),
        "PASSWORD": os.environ.get("DB_PASSWORD", ""),
        "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("DB_PORT", ""),
    }}
    if "mysql" in DB_ENGINE:
        DATABASES["default"]["OPTIONS"] = {
            "charset": "utf8mb4",                                  # full Unicode (Cyrillic, emoji)
            "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",  # reject bad data instead of cutting it
        }

REDIS_URL = os.environ.get("REDIS_URL")
if REDIS_URL:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels_redis.core.RedisChannelLayer",
                                  "CONFIG": {"hosts": [REDIS_URL]}}}
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 6}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en"
TIME_ZONE = "Europe/Kyiv"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"  # uploaded .tflite models are stored here
DATA_UPLOAD_MAX_MEMORY_SIZE = 200 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024

LOGIN_URL = "/accounts/"
LOGIN_REDIRECT_URL = "/projects/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---- Model (worker) resource limits
FOMO_MAX_RAM_MB = int(os.environ.get("FOMO_MAX_RAM_MB", "100"))      # ceiling that can be set in the UI
FOMO_HARD_MEMORY = os.environ.get("FOMO_HARD_MEMORY", "1") == "1"    # 0 = watchdog only (no Job/cgroup RAM limit)
