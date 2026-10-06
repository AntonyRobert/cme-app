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

- `.env` / anything in `/etc/cme/`
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

ORG="${1:?usage: deploy.sh <org>}"

cd "/srv/cme/$ORG"
git pull --ff-only
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
sudo systemctl restart "cme@$ORG"
sudo systemctl status "cme@$ORG" --no-pager
```

The script takes the organisation as its argument from the start, even while there is only
one. See "Multi-tenant deployment" below for why.

Deployment is a command, not a remembered sequence. The sequence is where mistakes live.

Two things this sketch does not handle yet. Fix both when the script is actually written:

- **Database roles.** Production has two: an owner role that runs `migrate`, and the app
  role gunicorn uses, which has no DDL rights and no UPDATE or DELETE on the audit table.
  With peer authentication a role is selected by the OS user, not a credential, so the
  `migrate` line has to run as the owner's OS user (`sudo -u <owner> ...`) and gunicorn as
  the app user. `prod.py` leaves `USER` empty so each process connects as whoever runs it.
  New tables created by a migration also need their grants given to the app role.
- **Environment.** `manage.py` run from a shell does not see the systemd
  `EnvironmentFile`. The script has to load `/etc/cme/<org>.env` itself, including
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

One instance per organisation. `<org>` is a short name such as `mcgill`; today there is one.

```
/srv/cme/<org>/           app checkout for that instance
/srv/cme/<org>/uploads/   raw Teams exports, never modified
/etc/cme/<org>.env        secrets and settings, mode 600, owned by root
/run/cme/<org>.sock       gunicorn's Unix socket
systemd: cme@<org>.service (gunicorn), cme-cron@<org>.timer (reminders)
Caddy: reverse proxy <hostname> -> that socket, static files served directly
Postgres: database and peer-auth roles for that instance only
```

## Multi-tenant deployment

**Decision: one instance per institution, not a tenant column in the schema.** Same
codebase; separate database, process, env file and uploads directory; routed by hostname in
Caddy. The reasoning is in `decisions.md`.

Nothing here is built yet. There is one tenant and no second institution. It is written
down now because it decides the shape of `deploy.sh` and the systemd units, and those are
cheaper to get right the first time than to rename later.

### Hostnames

- Default pattern: `<org>.cme.mri3.com`.
- An institution may later point its own hostname at the server with a CNAME. Caddy gets a
  certificate for it like any other name.
- **No code may assume the shape of the hostname.** Nothing parses a hostname to work out
  the tenant. The process serving a request already is the tenant; `ALLOWED_HOSTS` in its
  env file is the full list of names it answers to, and adding a CNAME is adding a name to
  that list and to the Caddy site block.

### Processes

- One gunicorn per tenant, listening on a **Unix socket** (`/run/cme/<org>.sock`), not a
  TCP port. A socket has file permissions; a port is reachable by every local process.
- If a port is ever used instead, bind `127.0.0.1` only. Never `0.0.0.0`.
- One parameterized systemd template, `cme@.service`. The instance name (`%i`) selects the
  env file (`/etc/cme/%i.env`), the socket and the checkout path (`/srv/cme/%i`). One unit
  file, not one per tenant. Starting a tenant is `systemctl enable --now cme@<org>`.
- Each tenant runs as its own OS user, which is also what selects its database role.

### Data

- A separate Postgres database per tenant, with its own peer-authenticated roles (owner and
  app, as described under `deploy.sh`). One tenant's process cannot connect to another's
  database, because Postgres maps the OS user to the role.
- A separate uploads directory and a separate backup repository per tenant, so a restore
  never touches another institution's records.

### Cookies

`SESSION_COOKIE_DOMAIN` and `CSRF_COOKIE_DOMAIN` **stay unset**. That makes cookies
host-only: the browser sends a session cookie back to the exact hostname that set it and no
other. Setting either to `.cme.mri3.com` would send one tenant's session to every other
tenant's host. Each tenant also has its own `SECRET_KEY`, so a session signed by one
instance is not valid on another.

For the same reason HSTS stays without `includeSubDomains`, which is how `prod.py` is set.

### Per-tenant configuration

Institutions differ in more than their name: accrediting body, credit rules, COI wording,
certificate template, retention period, series name, allowed email domains. Those live in
each instance's env file, settings and database, not in a shared table.

### Ceiling

This stops making sense at around four or five tenants. Each one is a system to patch,
migrate, back up and test restores for, and that work is done by one person. Past that
point, revisit: either a shared schema with a tenant key, done deliberately and with
row-level security, or a different hosting model.
