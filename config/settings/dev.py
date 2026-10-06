"""Local development and tests. Never used on the server."""
from pathlib import Path

from dotenv import load_dotenv

# Must run before base is imported, because base reads the environment at
# import time. Real environment variables win over the file.
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from .base import *  # noqa: E402,F401,F403
from .base import DATABASES, env  # noqa: E402

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# Emails are printed to the terminal instead of being sent.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Peer authentication doesn't exist on Windows, so dev connects over TCP with
# a password. This is the only place a database password appears.
DATABASES["default"].update(
    {
        "USER": env("DATABASE_USER"),
        "PASSWORD": env("DATABASE_PASSWORD"),
        "HOST": env("DATABASE_HOST", "localhost"),
        "PORT": env("DATABASE_PORT", "5432"),
    }
)
