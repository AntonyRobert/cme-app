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

**Sessions have their own times, and attendance is measured against them.**
Presenters enter a start and end time when they submit; the end defaults to an hour after
the start. A typical event is three one-hour sessions. Attended time is clamped to the
sessions, not the event: time in the Teams room during a break is not educational
activity. A talk that runs over is fixed once, on its end time. The event's own
`actual_start_at` / `actual_end_at` fields were removed; they had no job left.

**Credit is earned per session and gated per session.**
Qualifying minutes per session, counted only when that session has a complete
evaluation, summed per event. The earlier event-level gate ("any one complete
evaluation") would have let someone evaluate one talk and claim credit for three. The
stricter candidate from the earlier open question is now buildable, because sessions have
times, and this is it. `accredited_credits` stays a field so an accrediting body can
approve fewer credits than the clock says.

**Credit is rounded down to the nearest quarter, once per event.**
Down, because overstating credit is the error that can't be recovered from. Once per
event, because a certificate prints lines and a total, and a total that doesn't equal the
sum of its lines makes an accreditor distrust the whole document; and because rounding
each session separately would turn three full 20-minute talks into three quarters of a
credit.

**Within five minutes of a whole session counts as the whole session.**
Round-down alone has a cliff: joining sixty seconds late cost a quarter credit, which is
not defensible. The asymmetry was the bug (grace before the start, none at the end). The
tolerance is symmetric with the grace, applied per session, before rounding. The cap at
the session's real length stays. 59 minutes of 60 is 1.00; 52 is 0.75.

**Grace is five minutes at each end of the event, not around each session.**
Joining early for the first talk and lingering after the last are ordinary; wandering in
late from a break is not.

**A session counts as attended at half its length.**
Below that, nobody is chased for an evaluation of a talk they caught the end of. They can
still evaluate it and earn the minutes they were there for.

**The evaluation form is open for a week, then reopenable on request.**
One week from the event date by default. After that an attendee can ask for another week
for a session, granted automatically: they already attended and the hours are recorded;
the form is work they have to do. Each grant is audit-logged, limited to three per person
per session, and never past the end of the accreditation year containing the event
(a setting). Program admins can override, logged. The window closes on submission.

**Credits are a moving target; certificates are correct at issue.**
Someone reopening in November for a January event earns credit after a certificate may
have been issued. That is not an error. The person's page shows earned against certified
credit; the fix is a reissue through `supersedes`, on request, never automatic.

**Closed events stay closed.**
Once totals are frozen, reopening silently moves them. The status can only move forward.

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
can be edited for someone who walked in late, with the reason saying so. A roster row stays
active if the device row is later superseded, but the admin warns on it: if the device's
times were wrong, so are the copies.

**Hours-only manual rows name a session.**
A row with minutes but no times has to say which talk the minutes belong to, or they could
not be credited to one. Timed rows are matched to sessions by their times.

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

**Multi-tenant means one instance per institution, not a tenant column.**
If a second institution uses this, it gets the same codebase with its own database,
process, env file and hostname. Two reasons. First, in a shared schema every query needs a
tenant filter, and a single missed one leaks accreditation records between institutions.
Separate databases make that leak impossible rather than unlikely. Second, institutions
differ in accrediting body, credit rules, COI wording, certificate template and retention
period. That is configuration per instance, not a column. The cost is that each tenant is
a system to patch, back up and test restores for, so this holds up to about four or five
tenants and should be revisited beyond that. Nothing is built for it yet; see
`deployment.md`.

**Staff roles are Django groups, separate from `Person.role`.**
Coordinator, Program admin and Read only. Credit adjustments, certificate issue and
revocation, and record merging are Program admin only, because those are the fraud
surface: a hired coordinator must not be able to mint a certificate. `Person.role` is
unrelated; it only decides which certificate template an attendee gets.

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

**Is `is_complete` stored or computed?**
Stored for now, set by whoever enters the evaluation. Compute it from the responses once
the attendee form exists.
