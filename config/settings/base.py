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
    "programs",
    "people",
    "rounds",
    "attendance",
    "credits",
    "certificates",
    "audit",
    "signin",
]

# Staff accounts. Must be set before the first migration and never changed.
AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    # First: fixes REMOTE_ADDR from the proxy's header before anything logs it.
    # A no-op unless TRUST_X_FORWARDED_FOR is set (prod.py only).
    "core.middleware.ForwardedForMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Sets request.person for attendee pages (magic-link sessions).
    "signin.middleware.PersonMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
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
# Only prod.py turns this on: see core.middleware.
TRUST_X_FORWARDED_FOR = False

UPLOAD_ROOT = Path(env("UPLOAD_ROOT", str(BASE_DIR / "uploads"))).resolve()

# Defaults for a NEW program. Each program carries its own copy of these
# (programs.Program), editable by that program's admins; nothing reads these
# settings at run time except as the default when a program is created.
SERIES_NAME = "Health Informatics Rounds"

# Conflict-of-interest questionnaire, by version. A declaration records the
# version it was made under and always renders with that version's wording, so
# rewording a question never changes what an old declaration says. To change
# the questions, add a new version and point COI_CURRENT_VERSION at it; never
# edit an existing one. question_key is stable across versions.
# The 2026-10 set is provisional, pending confirmation with McGill CPD.
COI_QUESTIONS = {
    "2026-10": [
        ("research_funding", "Research funding or grants"),
        ("consulting", "Consulting or advisory roles"),
        ("speaker_fees", "Speaker fees or honoraria"),
        ("equity", "Equity or ownership"),
        ("employment", "Employment"),
        ("intellectual_property", "Intellectual property or royalties"),
        ("other", "Other relevant interests"),
    ],
}
COI_CURRENT_VERSION = "2026-10"
# A declaration is valid for this long after it was made, rolling.
COI_VALIDITY_DAYS = 365


# The evaluation form is open this many days after the event date, and a
# reopening lasts this long again.
EVALUATION_WINDOW_DAYS = 7
# Self-service reopenings per person per session. Program admins can override.
EVALUATION_REOPENINGS_MAX = 3
# (month, day) a new program's accreditation year ends. Per program after that.
ACCREDITATION_YEAR_END = (12, 31)
