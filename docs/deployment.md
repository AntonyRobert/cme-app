# Deployment

## Target

Single Linux VM. AWS Lightsail, 2 GB, in `ca-central-1` (Montreal).

| Piece | Choice | Rough cost |
| --- | --- | --- |
| Server | Lightsail 2 GB instance | ~12 USD/mo |
| Database | Postgres on the same box | included |
| Backups | restic to S3, nightly | cents |
| Email | Amazon SES over SMTP | cents |
| TLS | Caddy, automatic | free |
| Domain | already owned | — |

Roughly 15 to 20 USD a month. Verify current pricing at signup.

## Before writing code

1. **Set a billing budget alarm** at ~50 USD. The real AWS risk for an individual is a
   misconfiguration quietly billing for weeks.
2. **Request SES sandbox removal.** Until that is approved you cannot mail arbitrary
   addresses, which means magic links don't work. Usually approved within a day. Do this
   in week one, not the night before the first session.
3. **Set up SPF and DKIM** on the domain. Hospital spam filters eat bare-server mail
   silently, and you will not find out until someone mentions it weeks later.

## Workflow

Write locally in VS Code, commit, push. The server runs a checkout of the repo and pulls.
Git is the source of truth; the server is a consumer of it.

**Repo is private.** This holds personal information and accreditation logic. Put a
read-only deploy key on the server rather than personal credentials.

**Never in git:**

- `.env` / anything in `/etc/cme/env`
- uploaded Teams exports (`/srv/cme/uploads/`)
- the Postgres data directory
- generated certificate PDFs

These exist on the server only, which means **backups are their only copy**. A `git pull`
cannot restore them.

**Don't develop on the server.** Occasional debugging on the box is fine. The moment you
edit application files there, git stops being the truth and the next deploy silently
overwrites a fix you forgot about.

**Keep parity.** Postgres locally, not SQLite. Same Python version as the server. This is
the rule people break first and it bites during a migration.

Pin the Postgres major version: **17** on both sides. Install it on the server from the
PGDG apt repo rather than taking whatever Ubuntu's default repo ships, or the versions
drift. A `pg_dump` from a newer server will not restore into an older local instance,
which is exactly the situation you hit when testing a restore.

**Migrations run explicitly in the deploy script**, not automatically at startup. You want
to see them happen.

### deploy.sh (lives in the repo)

```bash
#!/usr/bin/env bash
set -euo pipefail

cd /srv/cme
git pull --ff-only
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
sudo systemctl restart cme-web
sudo systemctl status cme-web --no-pager
```

Deployment is a command, not a remembered sequence. The sequence is where mistakes live.

Two things this sketch does not handle yet. Fix both when the script is actually written:

- **Database roles.** Production has two: an owner role that runs `migrate`, and the app
  role gunicorn uses, which has no DDL rights and no UPDATE or DELETE on the audit table.
  The `migrate` line has to run with the owner role's credentials.
- **Environment.** `manage.py` run from a shell does not see the systemd
  `EnvironmentFile`. The script has to load `/etc/cme/env` itself, including
  `DJANGO_SETTINGS_MODULE=config.settings.prod`, or `manage.py` falls back to dev settings
  and fails.

Requirements are split: `requirements.txt` is what the server installs,
`requirements-dev.txt` adds the test and local-only tools.

## Do not

- No Lambda, API Gateway, Aurora Serverless, or anything scale-to-zero. Cold starts and
  billing surprises on an app that serves 40 people once a fortnight.
- No RDS. It costs more than the server for this workload.
- No Docker unless there is a concrete reason. One box you can SSH into is easier to debug
  before rounds.

## Portability

The AWS choice should stay reversible. Moving to Azure or anywhere else should be a day,
not a rewrite. Three things keep it that way:

- **Email over SMTP, not boto3.** Switching providers becomes a host and credential change
  in the env file.
- **Backups via restic or rclone, not the S3 SDK.** Both speak S3 and Azure Blob, so the
  target is config. Encryption and dedup come free.
- **Any future AI call behind one module.** A single `draft_blurb(session)`-shaped
  interface. Don't scatter model calls through views.

What actually costs time in a move is email deliverability — re-verifying the domain,
redoing DKIM, warming the new sending IP. Not hard, but it breaks quietly.

## Runtime layout

```
/srv/cme/            app checkout
/srv/cme/uploads/    raw Teams exports, never modified
/etc/cme/env         secrets, mode 600, owned by root
systemd: cme-web.service (gunicorn), cme-cron.timer (reminders)
Caddy: reverse proxy :443 -> gunicorn, static files served directly
```
