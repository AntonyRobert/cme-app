# Data model

Django + Postgres schema for the rounds administration app. This file is the source of
truth for the schema. If code and this file disagree, this file is right until it is
deliberately updated.

## Design principles

Five rules decide most of the schema.

**Nothing user-supplied is a primary key.** Every table keys on an internal `id`. Emails
change, licence numbers get typo'd, names carry accents and marriages. They are attributes
hanging off a person, never the person's identity.

**Raw uploads are append-only.** The Teams spreadsheet is stored untouched and never
edited. Parsed rows point back at the file they came from. If an audit questions a credit,
the original export is the evidence.

**Draft and published are separate fields.** Any text that might one day be AI-drafted has
a draft field, a published field, and a human step between them.

**Credits are computed, not typed.** A person's credit total is derived from attendance
plus evaluation. The only place a number is frozen is on an issued certificate.

**Every person-fact points at `Person`.** No table stores an email string as its link to a
human.

## People and identity

`Person` is the spine. Emails hang off it, licence hangs off it, everything else points at
it.

### Person

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | Never exposed in a URL you don't authorize |
| given_name, family_name | text | Separate fields; certificates need them formatted |
| credential | text | MD, RN, PharmD, PhD. Prints on the certificate |
| role | enum | physician, nurse, pharmacist, trainee, student, other |
| licence_number | text, nullable | Normalized on save: strip spaces, strip leading zeros, uppercase |
| licence_jurisdiction | enum, nullable | CMQ, CPSO, OIIQ, OPQ, other |
| merged_into | FK Person, nullable | Soft merge. Never delete a duplicate, point it |
| created_at, updated_at | timestamptz | |

Unique constraint on `(licence_jurisdiction, licence_number)` where both are non-null. A
CMQ and a CPSO number can collide numerically, which is why jurisdiction is part of the
key. That constraint is what triggers the merge offer.

`role` decides which certificate template renders. Physicians get a CME credit
certificate, everyone else gets an attendance certificate. Same pipeline, different
template, so nobody has to invent a licence number to get past a required field.

`merged_into` means queries must follow the chain to a root person. Write that resolver
once, use it everywhere, and cap the depth so a bad merge can't loop.

### PersonEmail

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| person | FK Person | |
| email | citext, unique | Case-insensitive, or you get two records for one address |
| verified_at | timestamptz, nullable | Set when a magic link on that address is used |
| is_primary | bool | Where merge offers and certificates go |
| added_at | timestamptz | |

One person, many emails. Someone joins Teams on their hospital account and signs the form
with their university one. This table is the only thing that stops their credits coming up
short.

### AllowedDomain

| Field | Type | Notes |
| --- | --- | --- |
| domain | citext, unique | mcgill.ca, muhc.mcgill.ca, chum.qc.ca, CIUSSS domains |
| auto_admit | bool | False means sign-in lands in the review queue instead |
| note | text | Why you added it |

Anything unmatched falls to a queue reviewed afterward, so a new attendee is never blocked
mid-session.

## Sessions and presenters

Two levels: the fortnightly `RoundsEvent`, and the three `Session` records inside it. The
flyer renders an event, the evaluation form targets a session.

### RoundsEvent

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| date | date | |
| start_at, end_at | timestamptz | Scheduled, not actual. Actual comes from Teams |
| teams_join_url | text | The link pasted in the invite |
| teams_meeting_id | text, nullable | Lets an upload match an event automatically |
| status | enum | draft, published, held, closed |
| accredited_credits | decimal | Credits available for the whole event |

The flyer page shows published events, the evaluation form opens on held, and closed stops
further submissions so December totals stop moving.

### Session

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| event | FK RoundsEvent | |
| position | smallint | 1, 2, 3. Order on the flyer |
| title | text | |
| presenter | FK Person | A presenter is sometimes also an attendee |
| duration_minutes | smallint | |
| coi_declaration | FK COIDeclaration | Snapshot of what was declared at the time |
| draft_blurb, published_blurb | text | The draft/published split |
| submitted_at | timestamptz, nullable | Null means the presenter hasn't filled it yet |

Unique on `(event, position)`.

`submitted_at` drives the reminder job. Everything unfilled three days out is the chase
list.

### LearningObjective

| Field | Type | Notes |
| --- | --- | --- |
| session | FK Session | |
| position | smallint | |
| text | text | |

A separate table rather than two columns on `Session`. The evaluation form generates one
question per objective from these rows.

### COIDeclaration

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| person | FK Person | |
| has_conflict | bool | The checkbox |
| details | text | Required when has_conflict is true |
| declared_at | timestamptz | |
| valid_until | date | Default: end of the academic year |
| disclosure_text_version | text | Which version of the statement was agreed to |

A presenter fills it once and the next session picks up their current declaration
automatically. If `valid_until` has passed, the form asks again.

Snapshotting the declaration onto `Session` rather than reading it live matters for
accreditation: a year later you need to show what was disclosed at that session, not what
the presenter has declared since.

## Attendance

One table, several sources. The Teams file lands untouched, rows get parsed out of it,
unmatched rows wait in a queue, and rows are added by hand for people Teams never saw.

### AttendanceUpload

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| event | FK RoundsEvent | |
| original_filename | text | As it came off Teams |
| stored_path | text | The untouched file. Back this up with the database |
| sha256 | text, unique | Stops double-uploading the same export |
| uploaded_by | FK Person | |
| uploaded_at | timestamptz | |
| parsed_at | timestamptz, nullable | |
| row_count | int | Sanity check |

Never edit this file. If a parse was wrong, fix the parser and re-parse.

### AttendanceRecord

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| source | enum | teams_upload, manual, room_roster |
| upload | FK AttendanceUpload, nullable | Null for anything entered by hand |
| event | FK RoundsEvent | Denormalized for query speed |
| raw_display_name | text, nullable | Exactly as Teams wrote it |
| raw_email | citext, nullable | Teams sometimes gives a UPN, sometimes nothing |
| raw_participant_role | text, nullable | Organizer, Presenter, Attendee |
| join_at, leave_at | timestamptz, nullable | Null on a manual hours-only row |
| duration_seconds | int | The one field every row has |
| attributed_to | FK AttendanceRecord, nullable | The room's Teams row this person sat in |
| person | FK Person, nullable | Null means unmatched |
| match_method | enum | email_exact, email_alias, manual, unmatched |
| reason | text, nullable | Required when source is not teams_upload |
| superseded_by | FK AttendanceRecord, nullable | Corrections add a row, never edit one |
| created_by, created_at | FK Person, timestamptz | |
| matched_at, matched_by | timestamptz, FK Person | |

The unmatched queue is just `AttendanceRecord` where `person` is null. No separate table.

### Parser notes

Teams writes **one row per join, not one row per person**. People drop and rejoin, laptops
sleep, they dial back in from a phone. A 60-minute attendee can appear as four rows of 15
minutes. Total attendance is a sum over rows grouped by person and event, not a column.
Write that aggregation once as a method and never compute it inline.

Two more quirks:

- The export has a header block above the actual table, and more than one section stacked
  in the same sheet. You cannot hand the file to `read_excel` and expect a frame.
- Timestamps come out in the organizer's locale. Parse to UTC on the way in, or you get a
  quiet one-hour error at the November time change.

Matching runs email first against `PersonEmail`. What falls through lands in the queue with
a fuzzy name suggestion. Confirming writes a new `PersonEmail` row, so the same person
matches automatically from then on.

### Manual corrections and room attendance

Three cases, all handled by adding rows rather than editing them.

**Someone sat in a room on a colleague's laptop.** Teams shows one participant, or a
meeting-room device, for four people. Add a row per extra person with
`source = room_roster` and `attributed_to` pointing at the Teams row they sat in. That link
is what explains why four people share one join time.

**The hours are wrong.** Add a correcting row with `source = manual` and set
`superseded_by` on the row it replaces. The original stays, and the credit sum skips
superseded rows.

**They were never in the export at all.** A plain manual row with `duration_seconds` and a
reason. No join or leave times, because inventing timestamps makes a reconstructed record
look like a captured one.

`reason` is mandatory on every non-Teams row, and it should be free text, not a dropdown.

Adding attendance does not bypass the evaluation. The gate still applies.

### Attendance correction or credit adjustment

Not interchangeable. Correct attendance when the hours record is wrong, which is almost
always. Use `CreditAdjustment` only when the hours are right and the credit is not: an
accreditation rule change, or a goodwill grant you want visibly separated from the
attendance record.

Attendance is a claim about what happened; credit is derived from it. Fixing the derived
number while leaving the record untouched is what makes an audit go badly.

## Evaluation and credits

Credit needs two things: the person was there, and they completed the evaluation.

### EvaluationSubmission

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| person | FK Person | From the signed-in session, never typed |
| session | FK Session | One submission per lecture attended |
| submitted_at | timestamptz | |
| self_reported_minutes | smallint | What they claim |
| attestation | bool | They confirm the hours are accurate. Required |
| is_complete | bool | All required objective questions answered |

Unique on `(person, session)`. Editable by the submitter until the event closes, then
locked.

### EvaluationResponse

| Field | Type | Notes |
| --- | --- | --- |
| submission | FK EvaluationSubmission | |
| objective | FK LearningObjective, nullable | Null for general questions |
| question_key | text | Stable key so years are comparable |
| rating | smallint, nullable | Likert |
| free_text | text, nullable | |

Keep `question_key` stable and the year-end report compares like with like even after a
question is reworded.

### Credit, computed

No `credits` column on anything except an issued certificate.

```
credits = min(attended_minutes / 60, event.accredited_credits)
```

**Credit is per event.** Hours come from the Teams export for the whole three-session
block, capped at the event's accredited credits. Attended minutes are the sum across the
whole event, not per lecture. Round to the nearest quarter credit, in one place.

When Teams minutes exist, they win. Self-reported minutes are the fallback for someone
whose connection died or who joined by phone under an unmatched name. Flag for review when
the two diverge by more than 15 minutes rather than silently picking one.

Evaluation stays per session, because the objective questions belong to a lecture. The gate
rule is still open — see `decisions.md`.

### CreditAdjustment

| Field | Type | Notes |
| --- | --- | --- |
| person | FK Person | |
| event | FK RoundsEvent | |
| delta_credits | decimal | Can be negative |
| reason | text | Required |
| created_by, created_at | FK Person, timestamptz | |

An append-only ledger rather than an edited number.

## Certificates

A certificate is a snapshot, not a view. It freezes what was true the day it was issued,
because someone will file it with a college.

### Certificate

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| person | FK Person | |
| period_start, period_end | date | The accreditation year |
| total_credits | decimal | Frozen at issue |
| recipient_name | text | Snapshotted. Names change |
| recipient_credential | text | Snapshotted |
| licence_number, licence_jurisdiction | text | Snapshotted |
| verification_code | text, unique, indexed | Prints on the PDF |
| template_version | text | Which layout and wording was used |
| pdf_sha256 | text | Proves the file wasn't altered after issue |
| issued_at | timestamptz | |
| superseded_by | FK Certificate, nullable | Reissue chain |
| revoked_at, revoked_reason | timestamptz, text | |

### CertificateLine

| Field | Type | Notes |
| --- | --- | --- |
| certificate | FK Certificate | |
| session | FK Session | |
| session_title | text | Snapshotted at issue |
| event_date | date | |
| credits | decimal | |

Every value the PDF prints is snapshotted here. Fixing a session title typo next year must
not change an already-issued certificate.

Corrections work by reissue, never by edit. Issue a new certificate, point the old one's
`superseded_by` at it, keep both. The old verification code resolves to a page saying it
was superseded, with a link forward.

### Verification

The public page at `/verify/<code>` shows recipient name, credential, sessions, credit
total, issue date. Nothing else — no email address, no licence number, no link to any other
record.

`verification_code` is random, not sequential, so certificates can't be enumerated. Use an
unambiguous alphabet that drops characters people misread off paper. Rate limit the
endpoint.

Store the generated PDF rather than rendering on demand. It is the artefact of record.

## Audit and operational tables

### MagicLinkToken

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| email | citext | The address it was issued for. Bind it, don't trust the click |
| token_hash | text, indexed | Store the hash only. Never the token |
| created_at | timestamptz | |
| expires_at | timestamptz | 15 minutes |
| consumed_at | timestamptz, nullable | Single use |
| requested_ip, requested_ua | text | For the rate limiter and the audit trail |

Rate limit on both email and IP. Without it this is a mailer that sends from your domain to
anyone, and SES will suspend the account for it.

### AuditLog

| Field | Type | Notes |
| --- | --- | --- |
| id | bigserial pk | |
| actor | FK Person, nullable | Null for anonymous or system actions |
| action | text | `certificate.issued`, `attendance.matched`, `credit.adjusted` |
| object_type, object_id | text, UUID | |
| metadata | jsonb | Before and after, where it matters |
| ip | inet | |
| created_at | timestamptz | |

Append-only. No update or delete path in the app, and the database role shouldn't have the
grants anyway.

Log what would be disputed: certificate issued, credit adjusted, attendance matched or
re-matched, manual attendance row created, person records merged, COI declared, admin
signed in. Not page views.

### SignInRequest

Addresses outside `AllowedDomain` land here rather than being blocked. Email, requested
name, requested at, approve/reject decision.

### Django's own tables

`auth_user` and `Person` are not the same thing. Keep `auth_user` for the handful of staff
accounts that log into the admin; `Person` is everyone who attends or presents. A staff
member who also attends gets both, linked by a nullable one-to-one.

That separation is what lets TOTP sit on admin accounts without imposing it on 200
attendees who just want a certificate.
