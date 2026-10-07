"""
What production relies on and local development never exercises: the
forwarded-address middleware, and that the production settings pass
Django's deployment checks with a plausible environment.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.test import RequestFactory, override_settings

from core.middleware import ForwardedForMiddleware

ROOT = Path(__file__).resolve().parents[2]


def run_middleware(xff, trust):
    seen = {}

    def view(request):
        seen["ip"] = request.META.get("REMOTE_ADDR")
        return None

    request = RequestFactory().get("/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR=xff)
    with override_settings(TRUST_X_FORWARDED_FOR=trust):
        ForwardedForMiddleware(view)(request)
    return seen["ip"]


def test_behind_caddy_the_audit_log_gets_the_client_address_not_the_proxy_s():
    assert run_middleware("203.0.113.9", trust=True) == "203.0.113.9"


def test_only_the_rightmost_address_is_believed():
    """Whatever a client put in front, the last hop appended the real one."""
    assert run_middleware("10.0.0.1, 198.51.100.7, 203.0.113.9", trust=True) == "203.0.113.9"


def test_the_header_is_ignored_unless_production_says_to_trust_it():
    assert run_middleware("203.0.113.9", trust=False) == "127.0.0.1"
    assert run_middleware("", trust=True) == "127.0.0.1"


def test_production_settings_pass_the_deployment_checks():
    """check --deploy with the env file's shape; no database needed."""
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.prod",
        "DJANGO_SECRET_KEY": "".join(chr(33 + i % 90) for i in range(64)),
        "DJANGO_ALLOWED_HOSTS": "cme.mri3.ca",
        "DATABASE_NAME": "cme_check",
        "UPLOAD_ROOT": str(ROOT / "uploads"),
        "EMAIL_BACKEND": "console",
    }
    for key in ("DATABASE_USER", "DATABASE_PASSWORD", "DATABASE_HOST", "DATABASE_PORT"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize("name", ["deploy.sh", "server-setup.sh", "backup.sh", "restore-test.sh", "manage.sh"])
def test_deploy_scripts_are_committed_executable_and_take_the_org(name):
    text = (ROOT / "deploy" / name).read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    assert 'ORG="${1:?' in text  # the org is the argument, even with one tenant
    mode = subprocess.run(["git", "ls-files", "-s", f"deploy/{name}"], cwd=ROOT, capture_output=True, text=True).stdout
    assert mode.startswith("100755"), mode
