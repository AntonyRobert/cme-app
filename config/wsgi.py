"""WSGI entry point for gunicorn: `gunicorn config.wsgi:application`."""
import os

from django.core.wsgi import get_wsgi_application

# Defaults to prod on purpose. If the environment is missing, gunicorn fails
# on a missing secret rather than quietly starting with dev settings.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

application = get_wsgi_application()
