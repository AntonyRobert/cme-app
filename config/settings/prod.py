"""Production. Everything here is required by docs/security-baseline.md."""
from pathlib import Path

from .base import *  # noqa: F401,F403
from .base import DATABASES, env

DEBUG = False
ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [f"https://{host}" for host in ALLOWED_HOSTS]

# Peer authentication over the Unix socket: no HOST, no PASSWORD. Postgres
# trusts the OS user, so there is no database secret to protect.
# USER is left empty unless set, which makes psycopg connect as the OS user
# running the process. gunicorn runs as the app user and gets the app role;
# migrate runs as the owner user and gets the owner role.
DATABASES["default"]["USER"] = env("DATABASE_USER", "")

# Caddy terminates TLS and forwards plain HTTP to gunicorn with this header.
# Without it Django can't tell the request was HTTPS and the redirect loops.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Caddy also forwards the client's address. The audit log records it in
# place of the socket's nothing (core.middleware.ForwardedForMiddleware).
TRUST_X_FORWARDED_FOR = True

# One year. Subdomains and preload are left off: they commit the whole
# domain, not just this app.
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
# Both decided, not forgotten (docs/deployment.md, "Cookies"), so the checks
# that would nag about them are silenced; deploy.sh runs check --deploy
# and fails on any warning left.
SILENCED_SYSTEM_CHECKS = ["security.W005", "security.W021"]

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Static files are collected here and served by Caddy directly (file_server
# on /static/). Django never serves them in production.
STATIC_ROOT = Path(env("STATIC_ROOT", str(BASE_DIR / "staticfiles")))  # noqa: F405

# Email is a switch. "console" until SES is verified: links are printed to
# the journal (journalctl -u cme@<org>) and nothing leaves the box. Flip
# EMAIL_BACKEND=smtp in /etc/cme/<org>.env when the domain is verified;
# SES over SMTP keeps the provider swappable.
_EMAIL_BACKENDS = {
    "console": "django.core.mail.backends.console.EmailBackend",
    "smtp": "django.core.mail.backends.smtp.EmailBackend",
}
EMAIL_BACKEND = _EMAIL_BACKENDS[env("EMAIL_BACKEND", "console")]
if EMAIL_BACKEND.endswith("smtp.EmailBackend"):
    EMAIL_HOST = env("EMAIL_HOST")
    EMAIL_PORT = int(env("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = env("EMAIL_HOST_USER")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD")
    EMAIL_USE_TLS = True
    EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "rounds@cme.mri3.ca")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# Everything to stderr; systemd's journal keeps it (journalctl -u cme@<org>).
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "{levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
