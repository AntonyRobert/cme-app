# Decisions

Settled calls and why. Reopen one only deliberately.

## Settled

**Build a custom app, not Microsoft 365.**
Power Automate handles the easy parts and fights the hard ones. Credit aggregation, flyer
generation from structured data, and Teams-export name reconciliation are cheap in code
and expensive in a flow engine. The Premium licence requirement for Word templating was a
further mark against it. The hinge was that manual upload of the Teams attendance export
removes the only thing M365 did better: tenant-native access to meeting attendance.

**Django, not Flask.**
Two reasons. The Django admin is the entire back office for free — spreadsheet upload
review, record merging, correcting a presenter's typo, adjusting a credit total in
December — and hand-building those screens in Flask is most of the build. And Django ships
CSRF, ORM parameterization, clickjacking protection and session hardening on by default,
which matters for a system holding accreditation records.

**Postgres, not SQL Server Express.**
Express is free up to 10 GB but wants ~2 GB RAM to start, which eats the whole VM. It is a
second-class Django backend via `mssql-django`. Postgres is first-class, `pg_dump` into
restic is a one-line cron job.

**AWS ca-central-1 (Montreal), not Azure.**
SES is the cheapest transactional sender and lives in the same account. Bedrock is in-region
for later. Lightsail bills flat. Azure's only real advantage was institutional billing,
which doesn't apply here.

Note on Bedrock: for many models ca-central-1 is served through geo or global routing
rather than in-region inference, so requests can leave the region. Irrelevant for flyer
copy; check routing mode before anything sensitive.

**Magic links, not Entra SSO.**
Avoids an app registration conversation with McGill IT. Gate on an institutional email
allowlist; unmatched addresses go to a review queue rather than being blocked.

**Identity keys on an internal ID, with a licence number as a matching signal.**
Licence number is collected (it prints on the certificate) and carries a unique constraint
with its jurisdiction, but it is not the identity key.

**Merge offers go to the existing account, not the new sign-in.**
When a licence number collides, say nothing specific to the newcomer and email the merge
offer to the address already on file. CMQ licence numbers are publicly searchable, so
telling the newcomer "an account exists under X@Y" leaks a private address to anyone who
looked up a colleague's number.

**Credit is per event, not per session.**
Hours come from the Teams export for the whole three-session block, capped at the event's
accredited credits.

**Non-physicians get an attendance certificate, not a CME certificate.**
Rounds pull in nurses, pharmacists, fellows, grad students. Same pipeline, different
template, licence number optional. Stops people inventing a number to clear a required
field.

**Attendance is uploaded manually after each meeting.**
26 uploads a year. Avoids needing Graph API permissions for tenant-wide meeting artifacts,
which would require McGill IT admin consent.

**AI features are deferred, with one accommodation.**
Draft and published are separate fields on anything an agent might one day write. That
split is the entire retrofit. No provider abstraction, no job queue, no scaffolding.

## Open

**The evaluation gate rule.**
Credit is per event but evaluation is per session. Two candidates:

- A complete evaluation for every session their attendance overlapped. More defensible to
  an accreditor.
- Any one complete evaluation for the event. Much less work for someone who sat through all
  three.

Write it as one function either way. Decide after one real cycle.

**Retention period.**
Law 25 gives a right to erasure; accreditation bodies require retention for several years.
These pull against each other. The decision also determines whether `Person` needs
soft-delete fields from the start, which is unpleasant to retrofit. Check what CMQ actually
requires before committing to a number, then write it into the privacy notice.
