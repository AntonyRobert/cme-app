# Security baseline

Target: health-app discipline, minus the compliance machinery that is disproportionate for
one VM serving a few hundred people. There is no PHI here, but there is personal
information under Quebec Law 25 (names, emails, licence numbers, attendance), and
certificates have real-world value.

## Required

### Platform

- TLS via Caddy, automatic certificates, HSTS on.
- gunicorn behind Caddy, running as a non-root user. Never the Django dev server.
- Firewall: 22, 80, 443 only. SSH keys only, password auth off, fail2ban on.
- `unattended-upgrades` for OS patches. An unpatched box is the likeliest real compromise.
- Dependabot on. It catches the dependency CVEs you would otherwise never hear about.

### Django

- `DEBUG = False` in production, `ALLOWED_HOSTS` set.
- `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`,
  `SECURE_HSTS_SECONDS`, `SESSION_COOKIE_HTTPONLY`, `SESSION_COOKIE_SAMESITE = 'Lax'`.
- CSRF protection on every POST. Don't exempt views to make something work.
- ORM or parameterized queries. No f-strings in SQL.
- Least-privileged database role. The app does not need DROP or schema rights. Two roles in
  production: an owner that runs `migrate`, and an app role with no DDL and no
  UPDATE/DELETE on `AuditLog`.

### Secrets

The rule is **never in version control, never in code, never in a log**. On disk, in a file
only the right user can read, is correct and normal. The credential has to exist somewhere;
what matters is what guards it.

- `.env` in `.gitignore` **before the first commit**. Once a secret is pushed, removing the
  line doesn't help — it is in the history and, on GitHub, has been crawled. The only fix
  is rotating the credential.
- `.env.example` **is** committed, with placeholder values like `change-me`. It documents
  which variables exist without revealing any.
- Production secrets live in `/etc/cme/<org>.env`, mode 600, owned by root, loaded by systemd as
  an `EnvironmentFile`. Not in the app directory, not readable by the web user.
- Never `print()` or log the settings object. Django's debug page redacts what it
  recognises as secret-shaped, which is not everything.

### Eliminate the database password in production

Postgres runs on the same box as the app, so there is no need for a password at all.
Connect over the Unix socket with **peer authentication**: Postgres trusts the OS user
identity instead of a credential.

- App runs as the `cme` system user; Postgres has a `cme` role with the same name.
- `pg_hba.conf`: `local all cme peer`.
- Django `DATABASES`: no `HOST`, no `PASSWORD`, `NAME` and `USER` only. An empty `HOST`
  makes psycopg use the socket.

Removing a secret beats protecting one. Keep `local all all peer` as the default and do not
open `host` lines for `127.0.0.1` unless something genuinely needs TCP.

Locally on Windows, peer authentication isn't available, so dev keeps a password in `.env`.
That is the only place a database password should appear.

`SECRET_KEY` cannot be eliminated this way and stays in the env file.

### Secrets managers

AWS Secrets Manager and Parameter Store are the right answer at scale and remain an option
later. For one VM they add a dependency, a cost and a failure mode, to protect a credential
peer authentication has already removed. Not now.

### Magic link auth

This is the entire authentication system, so it carries the weight a password hash would.

- Token from `secrets.token_urlsafe(32)`.
- Store the hash only, never the token.
- Single use, 15 minute expiry, bound to the email it was issued for.
- Rate limit hard: roughly 5 per email per hour, plus a global per-IP cap. Without this it
  is an email bomber that sends from your domain and destroys your SES reputation.
- Sessions: 90 days, rotate session ID on login, provide "sign out everywhere" that
  invalidates all sessions for a person.

### Authorization

**The one that actually bites.** Object-level authorization on every route that takes an
ID. `/certificate/1842` must check that certificate belongs to the signed-in person. Same
for evaluations, attendance records, the credits page.

This is the flaw that leaks real data in the wild, far more often than crypto failures, and
it is precisely where generated code is weakest — clean parameterized queries, and then a
route that trusts the ID in the URL. Review it by hand on every route.

### Certificates

Every certificate carries a verification code resolving to a public page with name,
sessions, credit total, issue date. Nothing else. This makes a forged PDF fail the moment
anyone checks, which is worth more here than most of the hardening above.

Random codes, not sequential. Unambiguous alphabet. Rate limit the verify endpoint.

### Audit

Append-only `AuditLog`. Actor, action, object, IP, timestamp. Log what would be disputed,
not page views. Manual attendance rows, attendance sign-off and credit adjustments are the
fraud surface, so they are the rows that matter most.

### Backups

Encrypted, including the raw attendance uploads alongside the database. Test a restore
once, now. An untested backup is decoration.

## Optional, reasonable

- TOTP on admin accounts only. About an hour. The admin login is the one that can mint
  certificates.
- Column-level encryption of licence numbers. About two hours. Good practice to have done
  once; skip it to ship.

## Deliberately skipped

Real in a hospital app, disproportionate here. Weeks of work, little gain for one box.

- VPC with private subnets and a bastion
- KMS envelope encryption with key rotation
- WAF
- Centralized log aggregation
- Formal RBAC matrices and break-glass access
- Multi-AZ failover with RPO targets
- Hash-chained tamper-evident audit logs

## Policy, not code

Law 25 erasure rights and accreditation retention requirements conflict. Decide the
retention position early, write it into the privacy notice, and check what CMQ actually
requires. Deciding in the moment someone asks for deletion is worse than deciding now.
