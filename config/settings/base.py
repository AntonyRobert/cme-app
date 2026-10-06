"""
Settings shared by every environment.

Never used directly: dev.py and prod.py import everything from here and
add what differs. Anything secret or machine-specific comes from the
environment through env() and has no default.
"""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent.parent

_REQUIRED = object()


def env(name, default=_REQUIRED):
    """Read an environment variable, failing loudly if a required one is missing."""
    value = os.environ.get(name)
    if value is None or value == "":
        if default is _REQUIRED:
            raise ImproperlyConfigured(f"Environment variable {name} is not set.")
        return default
    return value


SECRET_KEY = env("DJANGO_SECRET_KEY")

# Each environment sets these explicitly.
DEBUG = False
ALLOWED_HOSTS = []

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "accounts",
    "people",
    "rounds",
    "attendance",
    "credits",
]

# Staff accounts. Must be set before the first migration and never changed.
AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Postgres everywhere, including dev and tests. No SQLite fallback.
# How to connect differs: dev adds host and password, prod uses the Unix
# socket with peer authentication and has neither.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DATABASE_NAME"),
    }
}

# Internal ids are UUIDs declared on each model; this only covers Django's
# own tables and AuditLog.
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# USE_TZ stores every timestamp in UTC. TIME_ZONE is only how the admin
# displays and accepts them.
USE_TZ = True
TIME_ZONE = "America/Montreal"
LANGUAGE_CODE = "en-ca"
USE_I18N = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"

DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "rounds@localhost")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# --- App settings ------------------------------------------------------------

# Raw Teams exports and issued PDFs. Never served by Django or Caddy.
UPLOAD_ROOT = Path(env("UPLOAD_ROOT", str(BASE_DIR / "uploads"))).resolve()

# Default title for a RoundsEvent. Prints on certificate lines.
SERIES_NAME = "Health Informatics Rounds"
