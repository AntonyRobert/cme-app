# CME App

Administration system for a fortnightly health informatics rounds series at McGill.
Presenters submit session details, attendees complete evaluations, attendance comes from
Teams exports, and attendees download an accredited CME certificate at year end.

Single maintainer. Built to be run for years with a few hours of attention a month.

## Read these first

- `docs/data-model.md` — the schema. Source of truth. Don't invent tables or fields that
  contradict it; propose a change to the file instead.
- `docs/decisions.md` — settled decisions and the reasoning. Includes what is still open.
- `docs/security-baseline.md` — non-negotiable security requirements.
- `docs/deployment.md` — target environment.

## Stack

- Python 3.13, Django 5.2 LTS (not 6.x — LTS support runs to April 2028)
- PostgreSQL 17, same major version locally and on the server
- Caddy in front, gunicorn behind, systemd units, single Linux VM
- Amazon SES over **SMTP** (not boto3 — keeps the provider swappable)
- restic or rclone for backups (not the S3 SDK, same reason)
- No serverless, no managed queue, no Redis unless something genuinely needs it
- Server-rendered Django templates. No SPA, no build step unless unavoidable.

Prefer boring and debuggable over clever. This has to be fixable at 7am before rounds.

## Non-negotiables

These are the rules that get quietly violated by otherwise-clean code. Check them on every
change.

1. **Authorization on every route that takes an ID.** `/certificate/<id>` must verify the
   object belongs to the requesting person. Object-level checks, not just
   `@login_required`. This is the flaw that leaks real data.
2. **No `credits` column anywhere except an issued `Certificate`.** Credit is computed by
   one function. If you find yourself caching a total, stop and ask.
3. **Raw uploads are never edited.** Fix the parser and re-parse. Never mutate a stored
   export.
4. **Attendance corrections add rows.** Use `superseded_by`. Never UPDATE a Teams-sourced
   row.
5. **Certificates snapshot every value they print.** Never render a certificate from live
   joins.
6. **Nothing user-supplied is a primary key.** Internal `id` everywhere. Email, licence
   number and name are attributes.
7. **No raw SQL string interpolation.** ORM or parameterized queries only.
8. **Secrets come from the environment.** Never committed, never defaulted to a real value
   in code.
9. **Audit-log anything disputable.** Manual attendance rows, credit adjustments,
   certificate issues, merges.

## Conventions

- Timestamps are `timestamptz` and stored UTC. Parse Teams exports to UTC at ingest.
- Money-like and credit values are `Decimal`, never float.
- Migrations are committed with the model change in the same commit.
- Tests: pytest. Every non-negotiable above that can be tested, is.
- One function per rule that might change (credit calculation, rounding, the evaluation
  gate). Don't inline these.

## How to work with me

- Ask before adding a dependency. The dependency list is a maintenance liability.
- Ask before changing the schema. Update `docs/data-model.md` in the same change.
- When a requirement is ambiguous, say so and propose two options rather than picking one
  silently.
- Push back if something here looks wrong. These rules were reasoned through but not
  tested against reality yet.
- Small commits with real messages.

## Not now

Explicitly deferred. Don't build these, don't scaffold for them:

- Any AI or LLM feature. The draft/published field split is the only accommodation.
- Column-level encryption.
- Self-service record merging.
- Analytics dashboards.
- A REST API.
- Docker, Kubernetes, CI/CD pipelines beyond a deploy script.
