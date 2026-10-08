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


def test_grants_sql_revokes_update_and_delete_on_the_audit_log_from_the_app_role():
    """The append-only audit log is enforced by the database for the serving process."""
    import re

    sql = (ROOT / "deploy" / "grants.sql").read_text(encoding="utf-8")
    statements = [line for line in sql.splitlines() if line and not line.startswith("--")]
    grant = next(i for i, l in enumerate(statements) if re.match(r'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES', l))
    revoke = next(i for i, l in enumerate(statements) if re.match(r'REVOKE UPDATE, DELETE, TRUNCATE ON TABLE audit_auditlog FROM :"app"', l))
    assert revoke > grant  # the exception is applied after the blanket grant, not before it
    assert "GRANT ALL" not in sql and "CREATE" not in sql  # no DDL rights anywhere for the app role


def test_the_caddy_site_template_has_no_bare_lines_caddy_would_read_as_directives():
    """
    Every line is a comment, blank, a brace line, or a known directive inside
    the site block; a bare path or a stray word would be an 'unrecognized
    directive' and a Caddy that will not reload.
    """
    template = (ROOT / "deploy" / "site.caddy.template").read_text(encoding="utf-8")
    rendered = template.replace("__ORG__", "mcgill").replace("__HOSTNAME__", "cme.mri3.ca")
    directives = {"encode", "handle_path", "root", "header", "file_server", "handle", "reverse_proxy",
                  "header_up", "log", "output"}
    code = "\n".join(l for l in rendered.splitlines() if not l.strip().startswith("#"))
    assert "output file" not in code and "/var/log" not in code  # the journal, never a file Caddy cannot write
    first_code = next(l for l in rendered.splitlines() if l.strip() and not l.lstrip().startswith("#"))
    assert first_code == "cme.mri3.ca {"  # the site block opens with the hostname, nothing before it
    for line in rendered.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped in ("}", "cme.mri3.ca {"):
            continue
        assert stripped.split()[0] in directives, f"not a directive: {line!r}"
    assert "__" not in rendered  # every placeholder substituted
    assert rendered.count("{") == rendered.count("}")


def test_backup_goes_to_s3_with_the_agreed_retention_and_fails_loudly():
    backup = (ROOT / "deploy" / "backup.sh").read_text(encoding="utf-8")
    assert "restic backup" in backup and '"$UPLOADS"' in backup and '"$DUMP"' in backup
    assert "restic forget" in backup and "--keep-daily 7 --keep-weekly 5 --keep-monthly 12 --prune" in backup
    assert "set -euo pipefail" in backup  # any failed step fails the unit
    for var in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        assert f"${{{var}:?" in backup  # refuses to run half-configured
    assert "echo $RESTIC" not in backup and "echo $AWS" not in backup
    unit = (ROOT / "deploy" / "cme-backup@.service").read_text(encoding="utf-8")
    assert "OnFailure=cme-backup-failed@%i.service" in unit
    assert "/etc/cme/%i.backup.env" in unit and "backup.sh %i" in unit  # the org is the instance, never hardcoded
    failed = (ROOT / "deploy" / "cme-backup-failed@.service").read_text(encoding="utf-8")
    assert "-p user.err" in failed
    setup = (ROOT / "deploy" / "server-setup.sh").read_text(encoding="utf-8")
    assert "restic" in setup.split("apt-get install")[1] or " restic" in setup
    assert "restic cat config" in setup and "restic init" in setup  # idempotent init
    for f in ("backup.sh", "restore-test.sh", "cme-backup@.service", "cme-backup-failed@.service"):
        assert "mcgill" not in (ROOT / "deploy" / f).read_text(encoding="utf-8")


def test_the_restore_test_restores_from_s3_not_the_local_dump():
    restore = (ROOT / "deploy" / "restore-test.sh").read_text(encoding="utf-8")
    assert "restic restore" in restore and "restic snapshots" in restore
    assert "/var/backups" not in restore  # the same disk proves nothing
    assert "pg_restore" in restore and "_restoretest" in restore and "DROP DATABASE" in restore
    assert "sha256sum" in restore  # uploads come back byte for byte
