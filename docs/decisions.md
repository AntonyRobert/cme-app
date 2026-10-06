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

**Credit is rounded down to the nearest quarter, per event.**
Down, because overstating credit is the error that can't be recovered from. Per event,
because a certificate prints lines and a total, and a total that doesn't equal the sum of
its lines makes an accreditor distrust the whole document. Accepted cost: about an eighth
of a credit per event, roughly three credits a year for someone who attends everything.

**Only time inside the event window counts.**
Waiting-room time isn't educational activity. Each join interval is clamped to the event
window, with five minutes' grace at the start. `actual_start_at` / `actual_end_at` on the
event override the schedule, so a session that runs over is fixed once, not per person.

**Attended time is a union of intervals, not a sum of rows.**
Someone on a laptop who also dials in by phone produces overlapping rows. Overlaps are
merged before summing. `duration_seconds` stays as recorded but is only summed for
hours-only manual rows.

**Observations are immutable; interpretations are not.**
What Teams recorded never changes. Who a row belongs to is a judgment and can be revised,
with an audit entry. Corrections are new rows linked through `AttendanceSupersession`
(many old rows to one new row, each old row superseded at most once). Certificates use the
same direction: the reissue carries `supersedes`.

**Room-roster rows copy the join and leave times of the row they sat in.**
A copied window is an observation about a device that really was in the meeting. The times
can be edited for someone who walked in late, with the reason saying so.

**Store what was typed, match on a normalized copy.**
A certificate that prints a licence number different from the one on the licence is worse
than useless. `licence_number` is as entered; `licence_number_normalized` is for matching.
Names are never title-cased.

**Presenters are many-to-many, through `SessionPresenter`.**
Not because panels are common, but because conflict of interest is per person. The COI
snapshot lives on the through row.

**A merge re-points the duplicate's rows and moves its emails to the survivor.**
The duplicate becomes a tombstone. If both records hold an evaluation (or presenter slot)
for the same session, the merge is refused and the admin chooses. No automatic winner,
because that would silently discard someone's responses. The audit entry lists everything
moved so a wrong merge can be reversed.

**Staff are `User`, attendees are `Person`.**
Upload, match and correction fields point at the staff `User`. `AuditLog` records an
`actor_type` and a text `actor_label` alongside nullable FKs to both, so an entry still
names its actor after an account is renamed or removed.

**Emails are lowercased on save, not `citext`.**
Django 5.1 removed its `citext` field types, and the suggested replacement (a
case-insensitive collation) breaks `LIKE`, which breaks admin search.

**No soft-delete fields on `Person` yet.**
Adding nullable columns later is a trivial migration. The hard part of retention is policy.

**Two database roles in production.**
An owner role runs migrations; the app role has no DDL rights and cannot update or delete
`AuditLog`. One role locally.

**Non-physicians get an attendance certificate, not a CME certificate.**
Rounds pull in nurses, pharmacists, fellows, grad students. Same pipeline, different
template, licence number optional. Stops people inventing a number to clear a required
field. The certificate type is snapshotted on the certificate at issue.

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
These pull against each other. Check what CMQ actually requires before committing to a
number, then write it into the privacy notice.

Linked to this: `AuditLog.actor_label` snapshots a username or email into an append-only
table, so erasing a person leaves their address there. The usual answer is to pseudonymize
the label on erasure rather than delete the row, which keeps the chain intact. That follows
from the retention policy, so it is not built ahead of it.

**Do trainees get a CME certificate?**
Default is an attendance certificate. Royal College Section 1 credits sit inside the MOC
program, which residents aren't enrolled in, but that reading is not authoritative.
Confirm with McGill's CPD office before the first December issue. Understating is
recoverable; overstating isn't.

**What a re-parse does to existing rows.**
When a fixed parser re-reads a stored export, the old rows may already carry matches,
room-roster links and supersessions. Not designed yet. `parser_version` is recorded on
uploads and rows so there is something to work with.

**Series name.**
`RoundsEvent.title` defaults to a `SERIES_NAME` setting. The value in settings is a
placeholder until the real name is confirmed.
