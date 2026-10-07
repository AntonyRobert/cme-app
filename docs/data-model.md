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

## Institution and Program

Two levels above everything else. An **institution** is a deployment: McGill is one
instance, Concordia another, and their records never share a database (see
`deployment.md`). A **program** is a row inside an instance: Emergency Medicine, Internal
Medicine, General Surgery. Every event belongs to a program, every certificate is issued
by one, and staff are scoped to one or more of them.

### Institution

One row per instance in practice; a table rather than a setting so the name and identity
are data, and so the public directory (`decisions.md`) has something to push.

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| name | text | "McGill University" |
| short_name | text, unique | "mcgill". The `<org>` of the hostname and the deploy script |
| website | text, optional | |

### Program

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| institution | FK Institution | |
| name | text | "Emergency Medicine" |
| slug | text, unique within institution | For URLs |
| series_name | text | Default title of a new event. Was the `SERIES_NAME` setting |
| attendance_rate_per_hour | decimal | Credits per hour attended. Default 1.00 |
| teaching_rate_per_hour | decimal | Credits per hour presented. Default 1.00 |
| default_accredited_credits | decimal | Pre-filled on a new event. Multiple of 0.25 |
| accreditation_year_end_month, accreditation_year_end_day | smallint | Was the `ACCREDITATION_YEAR_END` setting |
| coi_question_version | text | Which `COI_QUESTIONS` version new declarations use. Was `COI_CURRENT_VERSION` |
| retention_years | smallint | How long records are kept after the accreditation year. Policy still open; the field is where the answer goes |
| is_active | bool | A retired program keeps its history and takes no new events |

Everything that used to be a per-deployment setting and differs between programs lives
here. The settings keep their values only as defaults for a newly created program. A
program's admin can edit its own row; nobody else can.

**Rates are read at issue and snapshotted onto the certificate line** (below). Changing
a rate in March changes nothing already issued and does not revalue last year's sessions
on next year's certificate. The credits page shows the rate per event so a mid-year change
does not look like a bug.

### Staff scope: ProgramRole

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| user | FK User | |
| program | FK Program | |
| role | enum | coordinator, program_admin, read_only |
| granted_by, granted_at | FK User, timestamptz | |

Unique on `(user, program)`. A user may hold different roles in different programs: a
coordinator in Emergency Medicine and read-only in Internal Medicine.

The three Django groups stay as the **permission templates** (what a coordinator may do,
model by model) and are synced to a user from the roles they hold anywhere, so Django's
own model-level checks keep working. **Which rows** they may do it to is the program
scope, enforced by `core/authz.py`:

- `request.programs` is the set of programs the signed-in staff member has a role in.
  Superusers have every program.
- Every program-owned model (event, session, upload, attendance row, evaluation, window,
  adjustment, certificate, decision) has `for_programs(programs)` on its manager, the way
  person-owned models have `for_person()`. The lookup path to the program is declared on
  the queryset (`event__program`, `session__event__program`, ...).
- Every admin changelist, change form, autocomplete and action filters through it, so an
  Emergency Medicine coordinator never sees Internal Medicine's match queue, and a URL
  with another program's id is a 404, not a 403.
- The URL-walking test (`core/tests/test_authz.py`) is extended: a staff route that takes
  an id must declare how it is scoped to a program, as an attendee route must declare how
  it is scoped to a person. The admin is covered by a separate test that walks every
  registered model and asserts program-owned ones are filtered.

Persons, emails, allowed domains and audit entries are instance-wide, not per program: a
physician who attends two programs is one person.

## Staff accounts

### User (`accounts.User`)

A custom user model subclassing Django's `AbstractUser`, with no extra fields yet. It
exists because Django cannot switch to a custom user model after the first migration.

Only the handful of staff who log into the admin have one. TOTP will sit here later
without touching attendees. What a staff member may see is decided by `ProgramRole`.

## People and identity

`Person` is the spine. Emails hang off it, licence hangs off it, everything else points at
it.

### Person

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | Never exposed in a URL you don't authorize |
| given_name, family_name | text | As typed. Never title-cased or "cleaned" |
| credential | text | MD, RN, PharmD, PhD. Prints on the certificate |
| affiliation | text, optional | e.g. a university department. Appears on the flyer and certificates, exactly as typed |
| employer | text, optional | e.g. a hospital. Appears on the flyer and certificates, exactly as typed |
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

`affiliation` and `employer` are free text on the person, not a lookup table of
institutions and not part of the session blurb: a presenter returns across events, and the
blurb is per session. Someone with several writes them however they like, and they are
rendered as typed.

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
| program | FK Program | The program this rounds belongs to. Decides the rates, the series name and whose certificate it ends up on |
| title | text | Defaults to the program's `series_name`. Prints on certificate lines |
| date | date | |
| start_at, end_at | timestamptz | The outer bounds. Every session falls inside them. A blank end is three hours after the start; a blank `date` is the start's day |
| teams_join_url | text | The link pasted in the invite |
| teams_meeting_title | text | The meeting title exactly as Teams shows it. An export is matched to an event on this plus the date; exports carry no meeting ID |
| status | enum | draft, published, held, closed |
| accredited_credits | decimal | The accreditor-set ceiling on attendance credit for the event. Pre-filled from the program's default. Must be a multiple of 0.25: it is a ceiling someone approved, not a computed figure |

Everything that will one day be pushed to the public directory (`decisions.md`) is
already on this table and its sessions: title, date, times, objectives, presenter names
and affiliations, join link, program, institution. Keep it that way: no event field should
need a join through a person's private data to publish.

The flyer page shows published events, the evaluation form opens on held, and closed stops
further submissions so December totals stop moving. **Closed is final.** A closed event
cannot go back to any other status: its totals are frozen, and reopening would silently
move them.

Attended time is measured against the sessions' own times (below), not the event's. When
a talk runs over, change that session's `end_at` once and everyone's credit follows.

### Session

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| event | FK RoundsEvent | |
| position | smallint | 1, 2, 3. Order on the flyer |
| title | text | |
| start_at, end_at | timestamptz | Entered by the presenter. A blank start follows the previous session (or the event's start); a blank end is an hour later |
| draft_blurb, published_blurb | text | The draft/published split |
| submitted_at | timestamptz, nullable | Null means the presenters haven't filled it yet |

Unique on `(event, position)`, deferred to commit (as are the presenter and objective
position constraints) so two rows can swap numbers in one save. A session must fall inside its event and must not overlap
another session of the same event; sessions are ordered by `start_at`. A typical event is
three one-hour sessions.

The session's times are what attendance is clamped to and what credit is earned against.
A break between two talks is not educational activity.

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

A structured questionnaire, not a single checkbox: one `COIResponse` per question.

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| person | FK Person | |
| declared_at | timestamptz | Valid for a year from this, rolling (`COI_VALIDITY_DAYS`) |
| disclosure_text_version | text | Which questionnaire was answered. Must exist in `COI_QUESTIONS` |

### COIResponse

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| declaration | FK COIDeclaration | |
| question_key | text | Stable across versions, e.g. `consulting` |
| has_conflict | bool | |
| details | text | Required when `has_conflict` is true (check constraint) |

Unique on `(declaration, question_key)`.

**The questions live in settings**, in `COI_QUESTIONS`, keyed by version, each item a
stable `question_key` and its text. The program's `coi_question_version` names the one
its new declarations use. A declaration always renders with the wording of **its own** version, so rewording a
question never changes what an old declaration says. To change the questions, add a new
version; never edit an existing one. The starting set, `2026-10`, is provisional pending
McGill CPD: research funding or grants; consulting or advisory roles; speaker fees or
honoraria; equity or ownership; employment; intellectual property or royalties; other
relevant interests.

**Complete means every question of its version has a response.** "No conflicts in any
category" writes an explicit no to each question (`rounds.coi.declare_no_conflicts`),
never leaves them blank, so an unanswered declaration (no responses) and an attested no
(seven noes) are always distinguishable. `rounds.coi.declare` refuses a missing answer,
an unknown question or a yes without details, and writes nothing in that case.

**Validity is one year from `declared_at`**, rolling, replacing the earlier fixed 30 June.
A declaration is in force from the day it was made to the day before its anniversary.
A presenter picks up their most recent **complete** declaration in force on the event
date; an expired or incomplete one is never picked up, so they must fill a new one.

**Immutable once created**, declaration and responses alike. A new declaration is a new
row, and the admin shows existing ones read-only. That's what makes the FK on
`SessionPresenter` a snapshot: a year later you need to show what was disclosed at that
session, not what the presenter has declared since.

Existing single-checkbox declarations were converted by migration to version `2026-10`:
"no conflict" became a no to every question; "conflict" became a yes under "other" with
the details given, and no to the rest.

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
| parse_warnings | jsonb | What the parser noticed but did not stop for, e.g. Teams' own totals disagreeing with its rows |
| row_count | int | Sanity check |

Never edit this file. If a parse was wrong, fix the parser and re-parse. What happens to
the old rows, and the matches pointing at them, on a re-parse is **not yet designed**.
`parser_version` is recorded now so that design has something to work with.

`UPLOAD_ROOT` is never served by Caddy or Django.

### AttendanceRecord

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| source | enum | teams_upload, signin_sheet, qr_signin, manual, room_roster |
| upload | FK AttendanceUpload, nullable | The Teams export or sign-in sheet the row came from. Null for QR and hand-entered rows |
| parser_version | text, nullable | Null for anything entered by hand |
| event | FK RoundsEvent | Denormalized for query speed |
| session | FK Session, nullable | Which session a row without times belongs to: hours-only manual rows, sign-in sheet ticks and QR scans. Check constraint: a row without times must have one, and vice versa |
| raw_display_name | text, nullable | Exactly as Teams wrote it |
| raw_email | text, nullable | Exactly as Teams wrote it. Sometimes a UPN, sometimes nothing |
| raw_participant_role | text, nullable | **A Teams meeting permission**, as written. Labelled "Teams meeting role" in the admin. Everyone is given Presenter so they can share a screen; it says nothing about who presented and nothing in the credit path reads it |
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

### Three sources, reconciled

A Teams export, an uploaded sign-in sheet and a QR scan are three **independent claims**
about the same person in the same session. Each under-reports differently: Teams misses
the person physically in the room, a QR scan misses someone who left early, a paper sheet
proves presence but not duration. They are **never merged arithmetically**. Someone who
appears in all three shows three rows and one proposed figure, not the sum.

| Source | What one row claims | Minutes it claims for a session |
| --- | --- | --- |
| `teams_upload` | One join, with times | The part of its interval inside the session |
| `signin_sheet` | A tick against a session on paper | The whole session |
| `qr_signin` | One scan, in the room, during the session | The whole session |
| `manual` | What staff entered, with times or minutes | As entered |
| `room_roster` | Sat behind a device for the device's window | The part of the copied interval inside the session |

**Proposed minutes per session = the highest claim among the sources, capped at the
session's length.** Highest, because every source under-reports; capped, because no source
can exceed the talk. Within one source, rows still combine the way they always did (the
union of a person's Teams intervals). Sources disagreeing by more than a few minutes are
flagged for a human; sources agreeing are signed off in bulk (see sign-off below).

All sources are `AttendanceRecord` rows with distinct `source` values, so supersession,
matching and the review queue work unchanged.

### Parser notes (Teams)

What the file actually is, from a real export (the anonymized copy is
`attendance/tests/fixtures/teams-export-2026-09-10.csv`; `attendance/teams.py` reads it):

- **UTF-16 LE with a BOM, CRLF line endings, tab-separated**, whatever the `.csv`
  extension says. Comma-splitting breaks on the first name: "Camille Thibault, Dr".
- **Four sections** headed `1. Summary`, `2. Participants`, `3. In-Meeting Activities`,
  `4. Meeting Engagement`, separated by blank lines, with 2, 15, 6 and 3 columns. A
  different column count is refused, not guessed around.
- **No meeting ID anywhere.** The summary has title, participant count, start, end,
  duration and average attendance. An upload is matched to an event on **meeting title
  plus date**, and must overlap the event's scheduled hours.
- **The date is ambiguous.** `9/10/26` is 10 September or 9 October and nothing in the
  file says which. The parser never infers a locale: it reads the date whichever way
  matches the event's known date, and fails loudly when neither does. Times are in the
  organizer's local zone and are converted to UTC on the way in.
- **Section 3 is the observation**: one row per join, not per person. Section 2's
  "In-Meeting Duration" is Teams' own sum of that person's Section 3 rows with gaps
  excluded (verified across all ten participants of the fixture, including a three-rejoin
  case). Section 2 is used only as a **checksum**: a disagreement of more than one second
  per row is recorded in `parse_warnings` and shown, never corrected. Teams does not
  deduplicate someone connected on two devices at once, so interval merging still applies.
- Display names carry suffixes like `(External)` and `(CUSM)`; stored verbatim. Durations
  are human strings with whichever parts are non-zero (`3h 1m 41s`, `3h 26s`, `36m 6s`).
  Email capitalization varies between rows; matching lowercases. Section 4 has quoted
  fields with doubled quotes inside. Engagement columns are often empty.
- **The Role column is a meeting permission**, not a statement about who presented, and
  cannot be changed on the Teams side. It is stored as observed and never read. The
  organizer and every participant are attendees; presenter hours come only from
  `SessionPresenter`.

### The sign-in sheet

For people attending in person who did not scan. Exported per event as a spreadsheet:
one row per known person of the program (name, credential, affiliation), one column per
session to tick, and blank rows at the bottom for walk-ins. Whoever transcribes the paper
ticks boxes rather than typing names, which keeps spelling variants out of the match
queue.

A hidden column carries each row's person id, and a hidden cell carries the event id with
a signature over it (`HMAC(SECRET_KEY, event id)`), so the upload is matched on ids, never
on names, and a sheet exported for one event is refused for another. A tick becomes an
`AttendanceRecord` with `source = signin_sheet`, the session, and a claim of the whole
session. Walk-in rows (name, no id) land in the review queue like any unmatched row.

### QR sign-in

For people attending in person. The room shows a QR code that **rotates every thirty
seconds**: the URL carries the session id and a time-windowed token
(`HMAC(SECRET_KEY, session id, window)`), valid for the current window and the one before.
A photographed code texted to someone at home stops working within a minute. The page it
opens requires sign-in, so the scan is tied to a `Person`, never to a typed name.

**One scan per session, credited as the whole session.** Not scan-in/scan-out: people
forget to scan out, and the orphan check-ins are a worse reconciliation problem than the
one being solved. A scan becomes an `AttendanceRecord` with `source = qr_signin` and the
session; a second scan of the same session by the same person is ignored (unique on
`(person, session)` where `source = qr_signin`). The sign-off step catches bad claims.

### Upload preview: nothing is stored until confirmed

Every import, Teams export, sign-in sheet or QR batch, shows what the system understood
before it commits:

- which event matched, and for a Teams export which date reading was chosen and why;
- every participant, matched or not, their proposed minutes per session, and which
  sessions they cross;
- the rule being applied, stated on the screen: the organizer and all participants are
  attendees; presenter hours come only from `SessionPresenter`;
- the rows worth looking at, flagged: sources disagreeing by more than a few minutes,
  names matching nobody, a Section 2 checksum that will not reconcile.

Until the reviewer confirms, the file sits in `UPLOAD_ROOT/pending/` under its hash with
no database row; pending files older than a day are removed. Confirming stores the file,
creates the `AttendanceUpload` and its rows, and audit-logs the import. Cancelling removes
the pending file. A re-upload of a file already stored is refused as before.

Matching runs email first against `PersonEmail`, lowercasing `raw_email` at compare time.
What falls through lands in the queue with a fuzzy name suggestion. Confirming writes a new
`PersonEmail` row, so the same person matches automatically from then on.

### attended_minutes(person, event)

The one place attendance is aggregated. Never compute it inline. It returns, **per
session**, what each source claims and the proposed figure.

1. Take the event's **active** rows whose `person` resolves to this person, grouped by
   `source`.
2. **Timed rows** (Teams, timed manual, room roster): take the **union** of their
   intervals within the source. `duration_seconds` is deliberately ignored for these rows,
   because summing it double-counts overlaps and counts waiting-room time.
3. For each session, measure how much of each source's union falls inside the session's
   own times. The first session opens **five minutes early** and the last closes **five
   minutes late** (symmetric grace at the ends of the whole event, not around each
   session). Time between sessions counts for nothing.
4. **Rows without times** claim their session: an hours-only manual row claims its
   `duration_seconds`, a sign-in sheet tick or a QR scan claims the whole session.
5. Cap each source's claim at the session's length, then take the **highest claim across
   sources** as the **proposed minutes**, in whole minutes rounded down. Nobody attends a
   talk for longer than it ran. When hours-only rows are what pushed a claim over, the
   function logs a warning and the person is flagged for review: it usually means a
   duplicate manual row.
6. Report, per session, each source's claim, the proposed figure, and whether the sources
   **disagree** by more than `ATTENDANCE_DISAGREEMENT_MINUTES` (default 5).

`sessions_attended(person, event)` is the sessions where the proposed figure reaches at
least half the session's length. It decides which sessions a person is asked to evaluate,
and which titles a certificate line lists.

**Proposed is not confirmed.** Credit on a certificate counts only minutes a staff member
has signed off (`SessionAttendanceDecision`, below). The credits page shows proposed
minutes at once, marked pending, so one busy fortnight does not stall everybody.

`attended_minutes` lives in the attendance app and only knows about attendance rows. The
self-report fallback sits one layer up, in `credits.rules.creditable_time(person, event)`,
because evaluations belong to the credits app. When the person has no active rows at all,
each session they evaluated falls back to that submission's
`self_reported_session_minutes`, capped at the session's length, with source
`self_reported`. Credit resting on a self-report alone is always flagged for review. When
both exist and the person claims more than was recorded for that session, by more than 15
minutes, they are flagged for review rather than one figure being silently picked.
Claiming less is not flagged. Recorded minutes still win, even when they add up to zero.

The docstring repeats step 2 in plain words. Someone will try to "optimize" this back into
a `SUM(duration_seconds)`, and that would be wrong.

### SessionAttendanceDecision: sign-off

Credit counts only confirmed minutes. A decision is one staff member's sign-off of one
person's minutes for one session.

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| person | FK Person | |
| session | FK Session | |
| confirmed_minutes | int | What counts. Zero is a valid decision |
| basis | enum | sources_agree, highest_claim, manual |
| based_on | jsonb | The attendance row ids and each source's claim at the moment of sign-off |
| proposed_minutes | int | What the system proposed, for the record |
| comment | text | Required when `confirmed_minutes` differs from `proposed_minutes` |
| confirmed_by, confirmed_at | FK User, timestamptz | |
| supersedes | one-to-one SessionAttendanceDecision, nullable | A correction points back at the decision it replaces |

Append-only. The **current** decision for a person and session is the one nothing
supersedes. A correction is a new decision with new hours and a reason, superseding the
old one, and it needs its own sign-off; nothing is edited.

**Confirm with exceptions.** One "confirm this event" action writes a decision for every
person-and-session where the sources agree (or there is one source), with
`basis = sources_agree`. Rows where sources disagree by more than the threshold are held
back and presented one by one, with each source's claim, for a human to pick. Several
thousand clicks a year is the work this project exists to remove, and an admin clicking
through without reading is a weaker control than none.

Sign-off is per person, with one action writing all of that person's sessions, and a
checkbox per session so a single session can be adjusted before the person is signed off.

**Unmatched rows cannot be signed off.** The event's sign-off screen shows how many rows
are still in the match queue and will not confirm a session that has unmatched rows for
it until the queue is cleared or the rows are superseded.

**Sign-off blocks certificates, not the credits page.** `credit_breakdown` reports both
proposed and confirmed credit; a certificate is issued from confirmed minutes only, and
the issue action refuses while any event in the period has unconfirmed rows for the
person.

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
| self_reported_session_minutes | smallint | How long they say they attended this lecture |
| attestation | bool | They confirm the minutes are accurate. Required |
| is_complete | bool | All required objective questions answered |

Unique on `(person, session)`. A submission is accepted only while a window is open for
that person and session (below). Saving a complete submission closes any reopened window.

### EvaluationWindow

The form is open for **one week from the event date** by default (`EVALUATION_WINDOW_DAYS`).
After that, an attendee may ask for another week for a session; the request is granted
automatically, because they did the attending and the form is work they still owe. The
window closes as soon as they submit a complete evaluation, or when the week passes.

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| person | FK Person | |
| session | FK Session | |
| opened_at | timestamptz | |
| expires_at | timestamptz | A week after `opened_at` |
| reason | text | What the attendee (or staff) said |
| granted_by | FK User, nullable | Null when the attendee asked for it themselves |
| closed_at | timestamptz, nullable | Set when a complete evaluation is submitted |

Frozen after insert except `closed_at`. Every grant is audit-logged.

Limits on self-service: at most `EVALUATION_REOPENINGS_MAX` (three) per person per session,
and never past the end of the accreditation year containing the event
(the program's accreditation year end, a month and day). A program admin can override both, which is
logged against them.

The credits page lists sessions attended but not yet evaluated, each with the open form,
a "request another week" action, or the reason neither is available.

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

There are **two kinds of credit, tracked and reported separately** even while both pay
one credit per hour. The rates are per program (`Program.attendance_rate_per_hour`,
`teaching_rate_per_hour`), read from the event's program. A blended figure cannot be
split retroactively.

- **Teaching**: for each session the person is in `SessionPresenter` for, the session's
  full length. A presenter is by definition present for their own talk, so this is not
  Teams minutes. Granted on presenting alone, with no evaluation gate (**provisional**,
  pending McGill CPD). Presenter identity comes only from `SessionPresenter`.
- **Attendance**: for every other session, the minutes attended, counted only once that
  session has a complete evaluation. Time inside a session the person presented is
  teaching, never attendance: no double-counting.

Presenting one session and attending the other two of a three-hour event is 1 teaching +
2 attendance. The organizer earns attendance like anyone else and still has to evaluate.
Everyone in a Teams export is an attendee for credit purposes.

Each rule lives in its own function:

```
evaluation_gate(person, session)       -> bool     # a complete evaluation of THAT session
creditable_time(person, event)         -> per session: proposed and confirmed minutes,
                                          sources, attended, evaluated, presented,
                                          review reasons
credits_for_minutes(minutes, rate)     -> Decimal  # minutes / 60 * rate, to the hundredth
attendance  = min(credits_for_minutes(sum of minutes in non-presented sessions that pass
                  the gate, attendance rate), event.accredited_credits)
              + attendance adjustments, >= 0
teaching    = credits_for_minutes(sum of presented session lengths, teaching rate)
              + teaching adjustments, >= 0
```

Both figures exist twice: **proposed**, from `attended_minutes`, shown on the credits
page as pending; and **confirmed**, from the current `SessionAttendanceDecision` per
session, which is what a certificate prints.

The gate is per session so that evaluating one talk cannot claim credit for three.
`accredited_credits` caps attendance only: it is the accreditor-set ceiling for the event
as an attended activity. It keeps its quarter-step constraint because it is a ceiling
someone approved, not a computed figure; a three-hour event capping at 3.00 while someone
shows 2.97 is correct.

Minutes count exactly as recorded, however few: five minutes of a talk is five minutes,
once that talk's form is filled in. There is no minimum and no rounding up; 59 minutes of
a 60-minute session is 59/60 of a credit, 0.98. The only rounding anywhere is to two
decimal places, downward, applied once per kind to the event's total minutes. **There is
no rounding at issue.** Credit is hours attended, so the certificate says the hours: the
exact sum of its lines, to two decimals.

Credit is a moving target: a reopened evaluation can earn credit after a certificate was
issued. That is not an error. The person's admin page shows earned against certified
credit per program, event and kind, and the answer is a reissue, on request.

### CreditAdjustment

| Field | Type | Notes |
| --- | --- | --- |
| id | UUID pk | |
| person | FK Person | |
| event | FK RoundsEvent | |
| kind | enum | attendance, teaching. Which kind this changes; never blended |
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
| program | FK Program | **One certificate per program.** A person attending two programs collects two. Issued by that program's admin |
| program_name, institution_name | text | Snapshotted. Programs get renamed |
| certificate_type | enum | cme, attendance. Snapshot of what `role` implied at issue |
| period_start, period_end | date | The program's accreditation year |
| attendance_credits | decimal | Frozen at issue. The **exact** sum of the lines' attendance credits, two decimals |
| teaching_credits | decimal | Frozen at issue. The exact sum of the lines' teaching credits |
| total_credits | decimal | `attendance_credits + teaching_credits` (check constraint). Both lines print, and the total; never one blended figure, never rounded |
| recipient_name | text | Snapshotted as typed. Names change |
| recipient_credential | text | Snapshotted |
| licence_number, licence_jurisdiction | text | Snapshotted, **as entered**, never the normalized form |
| verification_code | text, unique, indexed | Prints on the PDF |
| template_version | text | Which layout and wording was used |
| pdf_path | text | Where the PDF is, relative to `UPLOAD_ROOT`. A hash with no recorded path is half a control |
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
| session_titles | JSON array of text | The sessions attended (at least half of each, not presented), snapshotted. Print-only |
| attended_minutes | int | Snapshotted. **Confirmed** minutes in sessions the person did not present |
| minutes_source | enum | teams, signin_sheet, qr, manual, mixed, self_reported |
| attendance_rate_per_hour | decimal | The program's rate **at issue**, snapshotted. A later rate change never touches this line |
| decisions | JSON array of ids | The `SessionAttendanceDecision` rows this line was issued from |
| attendance_computed | decimal | From the credit function, at that rate |
| attendance_adjustment | decimal | Sum of attendance `CreditAdjustment` deltas |
| attendance_credits | decimal | `max(computed + adjustment, 0)` |
| presented_session_titles | JSON array of text | The sessions presented, snapshotted |
| teaching_minutes | int | Sum of presented session lengths |
| teaching_rate_per_hour | decimal | The program's rate at issue, snapshotted |
| teaching_computed, teaching_adjustment, teaching_credits | decimal | As for attendance |

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

Issuing and revoking each have their own permission (`issue_certificate`,
`revoke_certificate`), separate from general admin access, and are program-scoped: a
program admin issues only their program's certificates.

Issuing refuses while any of the person's events in the period has attendance that is
not signed off. Everything a certificate needs has to be confirmed first.

### Verification

The public page at `/verify/<code>` shows recipient name, credential, institution and
program, events and sessions, attendance and teaching credit and their total, issue date. Nothing else: no email address, no licence number, no link to any
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
| actor_user | FK User, nullable, on delete PROTECT | |
| actor_person | FK Person, nullable, on delete PROTECT | |
| actor_label | text, required | Snapshot of username or email at the time, or `system:<job>` |
| action | text | `certificate.issued`, `attendance.matched`, `credit.adjusted` |
| object_type, object_id | text, text | Text because staff `User` ids are integers |
| metadata | jsonb | Before and after, where it matters |
| ip | inet, nullable | |
| created_at | timestamptz | |

A check constraint ties the type to the FKs: `staff` requires `actor_user`, `attendee`
requires `actor_person`, and `system` requires both to be null. A null actor always says
why.

`actor_label` is what keeps an entry meaningful in three years, after the user is renamed.
The FKs are PROTECT rather than SET NULL: nulling them would be an UPDATE on audit rows,
which the app's database role is not allowed to do. A staff account with audit history is
deactivated, not deleted. The label is personal information, so erasure interacts with it (see the retention
question in `decisions.md`).

Append-only. No update or delete path in the app, and in production the app's database role
has no UPDATE or DELETE grant on this table.

Log what would be disputed: certificate issued or revoked, credit adjusted, attendance
matched or re-matched, manual attendance row created, attendance superseded, person records
merged, COI declared, admin signed in. Not page views.
