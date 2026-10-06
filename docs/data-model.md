# Data model

Django + Postgres schema for the rounds administration app. This file is the source of
truth for the schema. If code and this file disagree, this file is right until it is
deliberately updated. The repo copy is the maintained one.

## Design principles

Seven rules decide most of the schema.

**Nothing user-supplied is a primary key.** Every table keys on an internal `id`. Emails
change, licence numbers get typo'd, names carry accents and marriages. They are attributes
hanging off a person, never the person's identity.

**Observations are immutable; interpretations are not.** What Teams recorded is an
observation and never changes. Who that was is a judgment and may be revised, with an audit
entry. A correction is a new observation: it is added as a new row that supersedes the old
one, never written over it.

**Raw uploads are append-only.** The Teams spreadsheet is stored untouched and never
edited. Parsed rows point back at the file they came from. If an audit questions a credit,
the original export is the evidence.

**Store what people typed; match on a normalized copy.** Licence numbers, names and
anything that prints on a certificate are kept exactly as entered. Where matching needs a
canonical form, it lives in a separate `*_normalized` field.

**Draft and published are separate fields.** Any text that might one day be AI-drafted has
a draft field, a published field, and a human step between them.

**Credits are computed, not typed.** A person's credit is derived from attendance plus
evaluation. The only place a number is frozen is on an issued certificate.

**Every person-fact points at `Person`; every staff action points at `User`.** No table
stores an email string as its link to a human. Attendees are `Person`; the few staff who
log into the admin are `User`.

## Conventions that apply to every table

- Primary key is `id`, a UUID, except `AuditLog` (bigserial) and Django's own tables.
- Timestamps are `timestamptz`, stored UTC.
- Credit values are `Decimal(5, 2)`. Never float.
- Email addresses and domains are stored **lowercased and stripped**, enforced by a check
  constraint (`email = lower(email)`) plus a plain unique index. This replaces `citext`,
  whose Django field types were removed in Django 5.1. The exception is
  `AttendanceRecord.raw_email`, which is an observation and is stored exactly as Teams
  wrote it; matching lowercases at compare time.
- `created_by`, `uploaded_by`, `matched_by`, `decided_by` are FKs to the staff `User`.

## Staff accounts

### User (`accounts.User`)

A custom user model subclassing Django's `AbstractUser`, with no extra fields yet. It
exists because Django cannot switch to a custom user model after the first migration.

Only the handful of staff who log into the admin have one. TOTP will sit here later
without touching attendees.

## People and identity

`Person` is the spine. Emails hang off it, licence hangs off it, everything else points at
it.

### Person

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | Never exposed in a URL you don't authorize |
| given_name, family_name | text | As typed. Never title-cased or "cleaned" |
| credential | text | MD, RN, PharmD, PhD. Prints on the certificate |
| role | enum | physician, nurse, pharmacist, trainee, student, other |
| licence_number | text, nullable | **As entered.** This is what prints |
| licence_number_normalized | text, nullable | Derived on save: strip spaces, strip leading zeros, uppercase. Used only for matching |
| licence_jurisdiction | enum, nullable | CMQ, CPSO, OIIQ, OPQ, other |
| merged_into | FK Person, nullable | Soft merge. Never delete a duplicate, point it |
| staff_user | one-to-one User, nullable | A staff member who also attends |
| created_at, updated_at | timestamptz | |

Unique constraint on `(licence_jurisdiction, licence_number_normalized)` where both are
non-null **and `merged_into` is null**. A CMQ and a CPSO number can collide numerically,
which is why jurisdiction is part of the key. The constraint ignores tombstones, so a
merged-away duplicate never blocks the survivor from holding the number. A collision is
what triggers the merge offer.

Two check constraints: a licence number needs its jurisdiction, and a person cannot be
merged into themselves.

`role` decides which certificate template renders. Physicians get a CME credit
certificate, everyone else gets an attendance certificate, **trainees included** (pending
confirmation with McGill's CPD office, see `decisions.md`). Same pipeline, different
template, so nobody has to invent a licence number to get past a required field.

#### Merging

A merge points the duplicate at the survivor and turns the duplicate into a tombstone. It
runs in one transaction:

1. **Check every uniqueness constraint that includes `person` first.** Today that is
   `EvaluationSubmission (person, session)` and `SessionPresenter (session, person)`. If
   both records hold a row for the same session, **refuse the merge** and show the admin
   both rows so they can choose. Never pick a winner automatically: that silently discards
   someone's evaluation responses.
2. Move the duplicate's `PersonEmail` rows to the survivor. Moved addresses become
   non-primary.
3. Re-point `person` on the duplicate's `AttendanceRecord`, `EvaluationSubmission`,
   `SessionPresenter`, `COIDeclaration`, `CreditAdjustment` and `Certificate` rows.
   `person` is an interpretation, so revising it is allowed.
4. Set `merged_into` on the duplicate to the survivor's **root**. Never create a chain on
   purpose, and reject cycles.
5. Write one `person.merged` audit entry listing the moved addresses and the ids of every
   re-pointed row, so a wrong merge can be reversed by hand.

Licence numbers are not copied automatically. If the survivor needs the duplicate's
number, the admin enters it, and the tombstone exemption above lets them.

`resolve_root(person)` follows `merged_into` to the root. It is written once, used
everywhere, and capped at 10 hops so a bad merge raises instead of looping. Because merges
re-point rows, it is a safety net rather than the hot path.

### PersonEmail

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| person | FK Person | |
| email | text, unique, lowercased | See conventions |
| verified_at | timestamptz, nullable | Set when a magic link on that address is used |
| is_primary | bool | Where merge offers and certificates go. At most one per person |
| added_at | timestamptz | |

One person, many emails. Someone joins Teams on their hospital account and signs the form
with their university one. This table is the only thing that stops their credits coming up
short.

### AllowedDomain

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| domain | text, unique, lowercased | mcgill.ca, muhc.mcgill.ca, chum.qc.ca, CIUSSS domains |
| auto_admit | bool | False means sign-in lands in the review queue instead |
| note | text | Why you added it |

Anything unmatched falls to a queue reviewed afterward, so a new attendee is never blocked
mid-session.

### SignInRequest

Addresses outside `AllowedDomain` land here rather than being blocked.

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| email | text, lowercased | |
| given_name, family_name | text | As typed |
| requested_at | timestamptz | |
| status | enum | pending, approved, rejected |
| decided_by | FK User, nullable | |
| decided_at | timestamptz, nullable | |
| note | text | |

## Sessions and presenters

Two levels: the fortnightly `RoundsEvent`, and the three `Session` records inside it. The
flyer renders an event, the evaluation form targets a session.

### RoundsEvent

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| title | text | Defaults to the `SERIES_NAME` setting. Prints on certificate lines |
| date | date | |
| start_at, end_at | timestamptz | Scheduled |
| actual_start_at, actual_end_at | timestamptz, nullable | Null means "as scheduled" |
| teams_join_url | text | The link pasted in the invite |
| teams_meeting_id | text, nullable | Lets an upload match an event automatically |
| status | enum | draft, published, held, closed |
| accredited_credits | decimal | Credits available for the whole event. Must be a multiple of 0.25 |

The flyer page shows published events, the evaluation form opens on held, and closed stops
further submissions so December totals stop moving.

**The credit window** is `effective_start - 5 minutes` to `effective_end`, where
`effective_*` is `actual_*` if set, otherwise the scheduled time. When rounds runs twenty
minutes over, set `actual_end_at` once and everyone's credit recomputes. The actual times
are nullable rather than copied from the schedule, so rescheduling a draft event can't
leave stale actual times behind.

### Session

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| event | FK RoundsEvent | |
| position | smallint | 1, 2, 3. Order on the flyer |
| title | text | |
| duration_minutes | smallint | |
| draft_blurb, published_blurb | text | The draft/published split |
| submitted_at | timestamptz, nullable | Null means the presenters haven't filled it yet |

Unique on `(event, position)`.

`submitted_at` drives the reminder job. Everything unfilled three days out is the chase
list.

### SessionPresenter

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| session | FK Session | |
| person | FK Person | A presenter is sometimes also an attendee |
| position | smallint | Order the names print |
| coi_declaration | FK COIDeclaration, nullable | The declaration in force for this person at this session. Null until they declare |

Unique on `(session, person)` and `(session, position)`.

A many-to-many through table even though most sessions have one presenter. Conflict of
interest is per person, so a co-presenter would have broken a single FK on `Session`.

### LearningObjective

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
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
| details | text | Required when has_conflict is true (check constraint) |
| declared_at | timestamptz | |
| valid_until | date | Default: end of the academic year |
| disclosure_text_version | text | Which version of the statement was agreed to |

**Immutable once created.** A new declaration is a new row, and the admin shows existing
rows as read-only. That's what makes the FK on `SessionPresenter` a snapshot: a year later
you need to show what was disclosed at that session, not what the presenter has declared
since.

A presenter fills it once and the next session picks up their current declaration
automatically. If `valid_until` has passed, the form asks again.

## Attendance

One table, several sources. The Teams file lands untouched, rows get parsed out of it,
unmatched rows wait in a queue, and rows are added by hand for people Teams never saw.

### AttendanceUpload

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| event | FK RoundsEvent | |
| original_filename | text | As it came off Teams |
| stored_path | text | The untouched file, under `UPLOAD_ROOT`. Back this up with the database |
| sha256 | text, unique | Stops double-uploading the same export |
| uploaded_by | FK User | |
| uploaded_at | timestamptz | |
| parsed_at | timestamptz, nullable | |
| parser_version | text, nullable | Which parser produced the current rows |
| row_count | int | Sanity check |

Never edit this file. If a parse was wrong, fix the parser and re-parse. What happens to
the old rows, and the matches pointing at them, on a re-parse is **not yet designed**.
`parser_version` is recorded now so that design has something to work with.

`UPLOAD_ROOT` is never served by Caddy or Django.

### AttendanceRecord

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| source | enum | teams_upload, manual, room_roster |
| upload | FK AttendanceUpload, nullable | Null for anything entered by hand |
| parser_version | text, nullable | Null for anything entered by hand |
| event | FK RoundsEvent | Denormalized for query speed |
| raw_display_name | text, nullable | Exactly as Teams wrote it |
| raw_email | text, nullable | Exactly as Teams wrote it. Sometimes a UPN, sometimes nothing |
| raw_participant_role | text, nullable | Organizer, Presenter, Attendee |
| join_at, leave_at | timestamptz, nullable | Both set or both null (check constraint). Null on a manual hours-only row |
| duration_seconds | int | The one field every row has. **Not what the credit sum uses for timed rows** |
| attributed_to | FK AttendanceRecord, nullable | The room's Teams row this person sat in |
| person | FK Person, nullable | Null means unmatched. An interpretation: may be revised |
| match_method | enum | email_exact, email_alias, manual, unmatched |
| reason | text, nullable | Required when source is not teams_upload (check constraint) |
| created_by, created_at | FK User, timestamptz | |
| matched_at, matched_by | timestamptz, FK User | |

**Observation fields** never change after insert: `source`, `upload`, `parser_version`,
`event`, all `raw_*`, `join_at`, `leave_at`, `duration_seconds`, `attributed_to`,
`reason`, `created_*`.

**Interpretation fields** may be revised, and every revision is audit-logged: `person`,
`match_method`, `matched_at`, `matched_by`.

The unmatched queue is just `AttendanceRecord` where `person` is null. No separate table.

### AttendanceSupersession

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| old | FK AttendanceRecord, **unique** | The row being replaced |
| new | FK AttendanceRecord | The correcting row |
| created_by, created_at | FK User, timestamptz | |

Insert-only. A row is **active** when no supersession row has it as `old`.

The pointer lives on the link table rather than on either record, for two reasons. The old
row is never touched, because a correction is a new observation, not a revision of the old
one. And one correction can replace many rows: four 15-minute rejoin rows replaced by one
70-minute row is the common case.

`unique(old)` matters. Without it, two corrections could each supersede the same original.
The active-row query would then drop the original and count **both** corrections,
inflating the credit. Also checked: `old != new`, and both rows share an `event`.

### Parser notes

Teams writes **one row per join, not one row per person**. People drop and rejoin, laptops
sleep, they dial back in from a phone. A 60-minute attendee can appear as four rows of 15
minutes, or as two overlapping rows (laptop and phone at once).

Two more quirks:

- The export has a header block above the actual table, and more than one section stacked
  in the same sheet. You cannot hand the file to `read_excel` and expect a frame.
- Timestamps come out in the organizer's locale. Parse to UTC on the way in, or you get a
  quiet one-hour error at the November time change.

Matching runs email first against `PersonEmail`, lowercasing `raw_email` at compare time.
What falls through lands in the queue with a fuzzy name suggestion. Confirming writes a new
`PersonEmail` row, so the same person matches automatically from then on.

### attended_minutes(person, event)

The one place attendance is aggregated. Never compute it inline.

1. Take the event's **active** rows whose `person` resolves to this person.
2. **Timed rows** (`join_at`/`leave_at` set, which includes room-roster rows): clamp each
   interval to the credit window, drop the empty ones, **merge overlapping intervals**, and
   sum the merged lengths. `duration_seconds` is deliberately ignored for these rows,
   because summing it double-counts overlaps and counts waiting-room time.
3. **Hours-only rows** (no times): add `duration_seconds` on top.
4. Cap the total at the length of the credit window, then convert to whole minutes,
   rounding down.
5. Report the source: `teams` (only Teams rows), `manual` (only manual or room-roster
   rows), `mixed` (both), or `self_reported` (see below).

When the person has no active rows at all, fall back to the sum of `self_reported_minutes`
across their submissions for the event's sessions, capped at the window length, with source
`self_reported`. When both exist and differ by more than 15 minutes, flag the person for
review rather than silently picking one. Recorded minutes still win.

The docstring repeats step 2 in plain words. Someone will try to "optimize" this back into
a `SUM(duration_seconds)`, and that would be wrong.

### Manual corrections and room attendance

Three cases, all handled by adding rows rather than editing them.

**Someone sat in a room on a colleague's laptop.** Teams shows one participant, or a
meeting-room device, for four people. Add a row per extra person with
`source = room_roster`, `attributed_to` pointing at the Teams row they sat in, and
**`join_at`/`leave_at` copied from that row**. A copied window is an observation about a
device that really was in the meeting, so it clamps and merges through the same code path
as everything else. If the person walked in late, edit the times before saving and say so
in `reason`.

**The hours are wrong.** Add a correcting row with `source = manual`, then add
`AttendanceSupersession` rows linking each row it replaces to it. The originals stay, and
the credit calculation skips superseded rows.

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
| id | UUID pk | |
| submission | FK EvaluationSubmission | |
| objective | FK LearningObjective, nullable | Null for general questions |
| question_key | text | Stable key so years are comparable |
| rating | smallint, nullable | Likert |
| free_text | text, nullable | |

Keep `question_key` stable and the year-end report compares like with like even after a
question is reworded.

### Credit, computed

No stored credit total anywhere except an issued certificate.

Credit is **per event**. Each rule lives in its own function:

```
evaluation_gate(person, event)      -> bool       # rule still open, see decisions.md
round_credits(hours)                -> Decimal    # round DOWN to the nearest 0.25
computed_credits(person, event)     = 0 if not gate, else
                                      min(round_credits(attended_minutes / 60),
                                          event.accredited_credits)
event_credits(person, event)        = max(computed_credits + sum(adjustment deltas), 0)
```

Credit is rounded down because overstating it is the error you can't recover from. It is
rounded **per event**, so a certificate's total is exactly the sum of its printed lines. The
cost is about an eighth of a credit per event on average.

Self-reported minutes are only a fallback (see `attended_minutes`). Evaluation stays per
session, because the objective questions belong to a lecture.

### CreditAdjustment

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| person | FK Person | |
| event | FK RoundsEvent | |
| delta_credits | decimal | Can be negative. A ledger entry, not a total |
| reason | text | Required |
| created_by, created_at | FK User, timestamptz | |

An append-only ledger rather than an edited number.

## Certificates

A certificate is a snapshot, not a view. It freezes what was true the day it was issued,
because someone will file it with a college.

### Certificate

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| person | FK Person | |
| certificate_type | enum | cme, attendance. Snapshot of what `role` implied at issue |
| period_start, period_end | date | The accreditation year |
| total_credits | decimal | Frozen at issue. Equals the sum of the lines' `credits` |
| recipient_name | text | Snapshotted as typed. Names change |
| recipient_credential | text | Snapshotted |
| licence_number, licence_jurisdiction | text | Snapshotted, **as entered**, never the normalized form |
| verification_code | text, unique, indexed | Prints on the PDF |
| template_version | text | Which layout and wording was used |
| pdf_sha256 | text | Proves the file wasn't altered after issue |
| issued_at | timestamptz | |
| issued_by | FK User | |
| supersedes | one-to-one Certificate, nullable | Reissue chain. The new certificate points back |
| revoked_at, revoked_reason | timestamptz, text | |

### CertificateLine

One line per **event**. Per-session credit does not exist.

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| certificate | FK Certificate | |
| event | FK RoundsEvent | For traceability only. Nothing printed is read through it |
| event_title | text | Snapshotted |
| event_date | date | Snapshotted |
| session_titles | JSON array of text | Snapshotted. Print-only, never queried |
| attended_minutes | int | Snapshotted. The number someone disputes |
| minutes_source | enum | teams, manual, mixed, self_reported |
| computed_credits | decimal | From the credit function |
| adjustment_credits | decimal | Sum of `CreditAdjustment` deltas for this person and event |
| credits | decimal | `max(computed + adjustment, 0)`. What the line prints |

Unique on `(certificate, event)`.

Every value the PDF prints is snapshotted here. Fixing a session title typo next year must
not change an already-issued certificate.

Corrections work by reissue, never by edit. Issue a new certificate whose `supersedes`
points at the old one, and keep both. The old certificate's row is never touched. The old
verification code resolves to a page saying it was superseded, with a link forward through
the reverse relation. Because `supersedes` is one-to-one, a certificate can be superseded
only once.

Revocation sets `revoked_at` and `revoked_reason`. That is a status change, not an edit of
the printed content, and it is audit-logged.

### Verification

The public page at `/verify/<code>` shows recipient name, credential, events and sessions,
credit total, issue date. Nothing else: no email address, no licence number, no link to any
other record.

`verification_code` is random, not sequential, so certificates can't be enumerated. Use an
unambiguous alphabet that drops characters people misread off paper. Rate limit the
endpoint.

Store the generated PDF rather than rendering on demand. It is the artefact of record.

## Audit and operational tables

### MagicLinkToken

Built with the auth step, not before.

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| email | text, lowercased | The address it was issued for. Bind it, don't trust the click |
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
| actor_type | enum | staff, attendee, system |
| actor_user | FK User, nullable, on delete SET NULL | |
| actor_person | FK Person, nullable, on delete SET NULL | |
| actor_label | text, required | Snapshot of username or email at the time, or `system:<job>` |
| action | text | `certificate.issued`, `attendance.matched`, `credit.adjusted` |
| object_type, object_id | text, text | Text because staff `User` ids are integers |
| metadata | jsonb | Before and after, where it matters |
| ip | inet, nullable | |
| created_at | timestamptz | |

A check constraint ties the type to the FKs: `staff` requires `actor_user`, `attendee`
requires `actor_person`, and `system` requires both to be null. A null actor always says
why.

`actor_label` is what keeps an entry meaningful in three years, after the user is renamed
or deleted. It is personal information, so erasure interacts with it (see the retention
question in `decisions.md`).

Append-only. No update or delete path in the app, and in production the app's database role
has no UPDATE or DELETE grant on this table.

Log what would be disputed: certificate issued or revoked, credit adjusted, attendance
matched or re-matched, manual attendance row created, attendance superseded, person records
merged, COI declared, admin signed in. Not page views.
