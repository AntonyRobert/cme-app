# Session notes: step 1

Written 2026-10-06, updated the same day after your review (sections A to H). Covers
everything built in the first session: environment setup, the pre-build review, step 1 of
the build order (models, migrations, admin, tests), and the per-session credit redesign.
Nothing has been pushed; the repo has no remote. Round four (the design review) is the
newest section.

## Round four: the design review, groups 1 to 5, the sheet, and QR

Written 2026-10-07. Everything in the review order is built: exact certificates,
Institution and Program, the three sources reconciled, the upload preview, sign-off, the
paper sign-in sheet entry screen, and QR sign-in; plus the two changes from your second
note (sign-off as an explicit program-admin permission; what blocks a certificate,
listed). Nothing pushed.

### What was built

- **Exact certificates** (`cbcabde`). The total is the sum of the lines to the cent; no
  rounding at issue. `certificate_total()` is the one function.
- **Institution and Program** (`8a2e362`). `programs` app: `Institution`, `Program`
  (series name, rates per hour for attendance and teaching, default accredited credits,
  accreditation year end, COI question version, `attendance_disagreement_minutes` default
  5, `retention_years` default 7, `is_active`), `ProgramRole` (coordinator, program admin,
  read only) per user per program. `RoundsEvent.program` and `Certificate.program` are
  required FKs. Staff see only their programs' rows in every admin (`ProgramScopedAdminMixin`
  on top of `core/authz.py`); the authorization walk covers program scope. A user with
  different roles in two programs is tested to get each role's permissions in each.
  `CREDIT_RATES_PER_HOUR` is gone; `SERIES_NAME`, `COI_CURRENT_VERSION` and
  `ACCREDITATION_YEAR_END` stay only as defaults for a new program.
- **Magic-link sign-in** (`2e5c85b`). `signin` app: email in, 15-minute single-use token
  (hash stored), 5 per email and 20 per IP an hour, 90-day session, sign out everywhere
  via `Person.sessions_revoked_at`. `/me/` shows credits per program at that program's
  rates. Console email backend in dev. Staff admin logins are not attendee sessions.
- **Three sources reconciled** (this commit). `AttendanceRecord.source` gains
  `signin_sheet` and `qr_signin`. `attended_minutes()` groups rows into claims, proposes
  the highest claim per session capped at the session, and flags `disagree` (more than the
  program's threshold apart) and `tick_only` (nothing recorded, only a tick or a scan).
  QR is one scan per person per session (partial unique index); merging two people whose
  scans collide is refused like any other collision.
- **Upload preview** (this commit). Adding a Teams export no longer stores anything: the
  bytes wait in `UPLOAD_ROOT/pending/<sha>` with no database row and no audit entry, and
  the reviewer sees the matched event, the date reading and why, every participant with
  minutes per session, the rule stated on screen, and the rows worth a look (matches
  nobody, checksum, disagrees with a stored claim by more than the threshold). Confirm
  stores and imports; cancel discards; pending files older than a day are purged by
  `attendance.preview.purge()` (no scheduler yet; call it from the backup script or a
  management command when deployment comes).
- **Sign-off** (this commit). `SessionAttendanceDecision`: append-only, one person one
  session, confirmed and proposed minutes, basis, the claims it was based on, a comment
  that the database requires when the figure differs from the proposal, `supersedes`.
  `attendance/signoff.py`: `review(event)`, `confirm_event()` (confirm with exceptions, per
  session not per person), `confirm_person()`. The event admin has a "Review and sign off
  attendance" page: one button for the event, a per-person form with a checkbox and a
  minutes box per session, held reasons shown per row, a banner with the unmatched count
  linking to the match queue. The changelist shows signed-off / total per event.
- **Credit and certificates on confirmed minutes.** `credit_breakdown` reports proposed
  and confirmed attendance credit and which sessions await sign-off. `certificate_figures`
  raises `NotSignedOff` while any session in the period is unconfirmed, so a certificate
  cannot be issued from proposed minutes. `/me/` shows "pending sign-off" until the event
  is confirmed, then "not yet certified" until a certificate is issued.
- **The sign-in sheet is typed in, not uploaded** (your note mid-build). `signin_sheet`
  rows carry no upload: `created_by` and one audit entry per sitting are the provenance.
  The constraint and docs are updated (`0005_signin_sheet_is_typed_in`).
- **Sign-off is an explicit, program-scoped permission** (`b5b84ae`).
  `attendance.sign_off_attendance` sits in the Program admin group only, and
  `confirm_event` / `confirm_person` also require the program-admin role in the event's
  program (`can_sign_off`, one function). The admin page shows coordinators the whole
  review, held rows and reasons included, with no buttons and a line saying who signs.
  Tested: a coordinator here who is a program admin elsewhere cannot sign here.
- **What blocks a certificate is listed.** `certificate_blockers(person, program,
  period)` names every event in the period with evaluated-but-unsigned minutes, by
  date, each with its sign-off URL; `NotSignedOff` carries the list. The person's admin
  page has a "Blocking a certificate" block per program for the current accreditation
  year. There is no issue screen yet (certificates are view-only until the issue step),
  so this is where the list lives for now; the issue screen will reuse the function.
- **QR sign-in.** `attendance/qr.py` holds the rules: thirty-second windows, HMAC token
  over session and window, valid for the current and previous window, accepted fifteen
  minutes either side of the session, one row per person per session with
  `match_method = self` and no `created_by` (the attendee is the actor; audit-logged as
  such). `/scan/<session>/<window>/<token>/` is the public route (marked, with the reason,
  so the URL walk passes). The room page is on the event admin: full screen, inline SVG,
  meta refresh every thirty seconds, picks the running session. Fifteen tests.
- **"Import paper sign-in sheet"** on the event page. Lists the program's known people
  (anyone who attended, evaluated or presented at one of its events) with a checkbox per
  session, five blank lines for names not on the list, and a Record button. A tick is a
  `signin_sheet` row claiming the whole session; a name not on the list is an unmatched
  row in the match queue. Recorded ticks come back locked and re-submitting adds nothing.
  Same permission as sign-off (change on the event). Ten tests in
  `attendance/tests/test_sheet.py`.

### Decisions this round that your review did not settle

- **Manual and room-roster rows are not a fourth sensor.** They are staff corrections to
  the device record, so they combine with the Teams rows into one "recorded" claim. Four
  independent claims would have made every manual correction "disagree" with the Teams row
  it was correcting. Recorded in data-model.md and decisions.md.
- **Tick-only rows are proposed at the whole session but held back from bulk sign-off**,
  as you asked. A human confirms them one by one; the basis is `highest_claim`.
- **Confirming at the proposal when the row was held** (a disagreement the reviewer
  accepts as is) gets basis `highest_claim`, not `sources_agree`, so the audit trail shows
  someone looked.
- **Who signs off:** anyone with change permission on the event (coordinators and program
  admins). Read-only staff get 403. I did not add a separate sign-off permission: the
  roles already encode it, and a fourth role is more to explain.
- **A superseding decision needs no new comment when it returns to the proposal**; it
  needs one when it differs, like the first. The database check enforces this on every
  row, so the admin cannot be bypassed by a shell session.
- **An admin page for an object outside the user's programs redirects with a message**
  (Django's own behaviour for a missing object) rather than 404; the sign-off and preview
  pages, which are custom views, return 404. Both leak nothing. Tests assert each.
- **Migrated events went to the default program** "Emergency Medicine" (`em`), created by
  the migration with the previous settings values as its rates. Rename or reassign in the
  admin if that is wrong.
- **The COI version for a declaration entered by staff** comes from the program of the
  event being edited, falling back to the first program the staff member has a role in.
- **`Program.retention_years`** is stored and editable, default 7. No deletion or
  anonymization logic, per your note; still open in decisions.md.
- **A scan made while signed out is remembered for twenty minutes and written after
  sign-in.** The code lives a minute; the magic-link round trip takes longer. The token
  is verified at scan time; the completion only has to come soon after. The remembered
  scan lives in the Django session, survives the key rotation at sign-in, and is used
  once. I added a safe local `next` to the sign-in flow for this (anything off-site is
  dropped).
- **In a break, the room page shows the upcoming talk**, not the one that just ended:
  people scan on the way in. Running beats both.
- **Read-only staff may show the room page.** Showing the code lets people in the room
  sign in, which is what the room is for; it writes nothing itself.
- **`created_by` is now nullable, for QR rows only** (check constraint). The convention
  says staff actions point at User and attendee facts at Person; a scan is an attendee
  fact, so a staff FK would be a lie. The audit entry names the person.
- **Blockers are evaluated-but-unsigned sessions.** Unevaluated minutes earn nothing
  and block nothing, which keeps the old "no credit, no line" behaviour; a late
  evaluation after issue is the reissue case it always was.
- **Dependency added, with your OK: `segno==1.6.6`** (pure Python, zero dependencies).
- **Pending uploads are files, not rows.** A half-finished preview leaves a file in
  `pending/` and nothing else, so nothing in the database can be "stored but not
  confirmed". The cost is that a crash between confirm and import leaves the pending file
  for the purge to remove, which is the right side to fail on.

### Deviations

- Group 5's "sign-in sheet upload" is replaced by typed-in entry, as you said, and the
  entry screen is built even though the original order said to stop before it; your
  mid-build note read as "do this instead", so I did. QR is not started. The
  `UPLOADED_SOURCES` set is now Teams only. If spreadsheet upload comes back later it can
  reuse the pending-and-preview flow unchanged.
- The preview shows minutes per session from the raw intervals, before supersession and
  matching run; the sign-off page is the authoritative figure. The two agree for a clean
  file, and the preview says it is a preview.

### Unsure

- `confirm_event` re-runs `review()` for the whole event; at a few dozen people and three
  sessions that is instant, at a few hundred it is still fine, but it is O(people) queries
  and would want prefetching before a program with large attendance.
- The sign-off page is one long page of per-person forms. For forty people that is a
  scroll; for two hundred it wants a filter (held only / all). Not built; the "held" list
  at the top is the workaround.
- The pending purge has no trigger. A `purge_pending` management command run from cron on
  the server is the obvious answer; I left it for the deployment step rather than invent a
  scheduler now.
- `signin_sheet` rows have no reason text, by design, but also no batch id tying the rows
  of one sitting together beyond the audit entry. If a sheet has to be withdrawn as a
  whole, the audit entry's row list is what you would supersede from.
- The sheet's people list grows with the program: after a year it is everyone who ever
  came. That is what a paper sheet pre-filled from the mailing list would show too, but
  if it gets long the page wants a "recent attendees first" split or a search box. Not
  built.
- A walk-in has to be matched in the queue before the event can be signed off, like any
  unmatched row. Five blank lines is a guess at how many a sheet has; the page can be
  submitted twice.
- The room page needs the server's clock and the phones' clocks within about thirty
  seconds of each other; both are NTP-synced in practice, but a laptop with a drifted
  clock would show codes the server calls expired. The page says "scan the code showing
  now", which is the right instruction either way.
- The scan page is the first attendee-facing page that does something at scan time on a
  phone, and `base.html` has only the design tokens. It works; it is not styled beyond
  that.
- `remember_next` runs on every GET of the sign-in page, so a person who scans, then
  wanders to `/signin/` by hand, loses the `next` but keeps the pending scan; visiting
  `/scan/done/` after sign-in still completes it. Fine, but a little surprising.

## Round three: the real export, and two kinds of credit

Added after the Teams fixture arrived. 485 tests. Commits `d44e70b` (parser), `22fadc7`
(credit split) and the docs commit after them. Also in this session, before the fixture:
exact credit (minutes / 60), rounding only at issue, auto-filled session times and
positions, the COI questionnaire, affiliation/employer, and the position-swap fix.

### What was built

- **`attendance/teams.py`**: a parser for the export as it actually is (see below), pure:
  bytes in, a `ParsedExport` out, nothing written. `PARSER_VERSION = "teams-2026.1"` is
  stamped on every row it produces.
- **`attendance/services.py`**: `match_event` (title + date), `check_export_against_event`
  (date, title, hours), `import_export` (one `AttendanceRecord` per Section 3 row, matched
  by exact lowercased email, the rest to the queue), `upload_teams_export` (all of it in
  one transaction: nothing is stored unless the file passes).
- **Upload screen**: the file is parsed and checked as form validation, so a wrong file is
  a red message, not a stored row. Leave the event blank and it is found by title and
  date. Warnings from the parser are shown after import and kept on the upload.
- **`RoundsEvent.teams_meeting_id` is now `teams_meeting_title`** (the thing that actually
  exists). The seed sets it to "Health Informatics Rounds".
- **`AttendanceUpload.parse_warnings`**, and `raw_participant_role` relabelled "Teams
  meeting role" with help text saying it carries no meaning for credit.
- **Credit split** into attendance and teaching: `credits/rules.py` rewritten around
  `SessionCredit.presented`, `CreditAdjustment.kind`, `CREDIT_RATES_PER_HOUR`,
  `Certificate.attendance_credits`/`teaching_credits` with `total_credits` constrained to
  their sum, `CertificateLine` carrying both kinds, and `certificates/figures.py` computing
  exactly what a certificate would print (issuing itself is still not built).
- Event and person admin tables show both kinds; the evaluation reminder list skips a
  session the person presented.

### What the real export disagreed with

Everything the docs and I assumed about the file, against what the fixture shows.

1. **No meeting ID.** `data-model.md` said `teams_meeting_id` "lets an upload match an
   event automatically". The summary has title, participant count, start, end, duration
   and average attendance, nothing else. Matching is now title plus date.
2. **Not a CSV.** `.csv` extension, but UTF-16 LE with a BOM, CRLF, tab-separated. My
   `.gitignore` ignores `*.csv` except under `tests/fixtures/`, which is why the fixture
   could be committed at all; the upload screen's "`.csv` or `.xlsx`" check is now only
   an extension check and the parser decides.
3. **Four sections, not "a header block above the table".** The docs described a header
   and stacked sections; the file is four titled sections with 2, 15, 6 and 3 columns and
   81 engagement rows we don't store.
4. **The date is unparseable on its own.** `9/10/26` with nothing to say which number is
   the month. The docs said "parse to UTC on the way in", as if the format were known. It
   isn't; the only safe reading is against a date we already know, and the parser now
   refuses rather than guesses.
5. **Section 2's total is a derived number, not an observation.** "In-Meeting Duration"
   equals the sum of that person's Section 3 rows with gaps excluded, for all ten people,
   including Noémie's three rejoins (36m 6s + 1h 12m 45s + 51m 20s = 2h 40m 11s). That
   is what makes it usable as a checksum and useless as a record.
6. **Teams truncates each row to the second.** Our union of her three intervals is
   9,612 s; Teams says 9,611. A checksum with zero tolerance would warn on every rejoin,
   so the tolerance is one second per row. (I had not expected this; it fell out of the
   checksum test.)
7. **Seconds in Section 2 and whole-second truncation mean Teams' figure and ours will
   never agree exactly for rejoiners.** Our figure, from the timestamps, is the one stored
   and the one credited. That is a judgment: the timestamps are the observation; the
   duration string is Teams' arithmetic on them.
8. **Names carry commas and suffixes.** "Camille Thibault, Dr", "Thomas Dubois (CUSM)
   (External)". The docs' "raw_display_name exactly as Teams wrote it" holds, but any
   comma-splitting or title-casing would have corrupted them.
9. **Email and UPN are two columns that happen to be equal**, and capitalization differs
   between rows ("Thomas.Dubois@..." in both sections, lowercase for everyone else).
   Stored as written; matched lowercased, which the docs already said.
10. **The Role column is a permission.** Nine of ten people are "Presenter"; the tenth is
    "Organizer". The docs listed it as "Organizer, Presenter, Attendee" as if it described
    the agenda. It describes who may share a screen.
11. **Durations are not one format.** `3h 1m 41s`, `3h 26s` (no minutes part), `36m 6s`.
    A fixed-format parse would have failed on the second one.
12. **Engagement columns are mostly empty.** In this file every Section 2 row still ends
    with a value, so the "ragged trailing tabs" case (a row shorter than 15 columns) does
    not actually occur in the fixture. The parser pads short rows anyway; see the mutation
    note below.
13. **The meeting ran 8:57 to 12:02 for a 9:00 to 12:00 event.** Early joins and a late
    end are normal; the five minutes of grace at each end cover exactly this file. Anyone
    who stayed to 12:02 gets two minutes past the last talk, within the grace.
14. **Everyone joined before their talk and most stayed to the end.** Nobody in the file
    is a clean "attended only the middle session" case; that case is tested with
    synthetic data in `test_aggregation.py`, not with the fixture.

### How I checked the parser can fail

Same method as for `attended_minutes()`: break the code one way at a time, run the parser,
credit-kind and certificate tests, and see whether anything notices. 21 mutations
(`scripts` are not committed; the list is in this note):

| Break | Caught by |
| --- | --- |
| split on commas instead of tabs | 25 tests |
| always read the date month-first | the test that reads 9/10/26 as 9 October against an October event |
| accept a date that matches neither reading | 3 tests, including "nothing is stored" |
| drop the Section 2 checksum | 2 |
| checksum with no per-row tolerance | the one-second test |
| ignore the hours part of a duration | 10 |
| forget PM | 10 (join times land in the morning) |
| treat Teams times as UTC | 8 (every row shifts four hours) |
| import Section 2 (per person) instead of Section 3 (per join) | 7, including the three-row rejoin |
| match emails case-sensitively | 2 (Thomas.Dubois) |
| skip the title check / the hours check | 1 each |
| store the file before checking it | the "nothing is stored" test |
| teaching from Teams minutes, not session length | 8 |
| own talk also counted as attendance | 1 |
| teaching gated by evaluation | 12 |
| presenters asked to evaluate their own talk | 1 |
| accreditation cap on the blended total | 1 |
| Teams "Organizer" treated as a presenter | 3 (incl. a grep over the credit path) |
| certificate rounds the blended total | 1 |
| **stop padding short rows** | **0** |

The last one is an equivalent mutant, not a gap: the parser only ever reads columns 0 to
6 of Section 2, so padding the empty engagement columns changes nothing observable. The
padding stays as a guard for a future column read; the test for it can only check that a
short row is accepted, which it does.

Several entries were caught by only one test. That is deliberate for the date, title and
hours checks (each has exactly one test), and acceptable; it does mean each of those tests
is load-bearing.

### What a second export would likely break

I have parsed one file from one meeting on one tenant. Where I would expect trouble:

1. **Locale.** The organizer's locale decides the date order, the AM/PM marker, and
   possibly the decimal and the section titles. A French-locale export could write
   `10/09/26 08:57:52`, `3 h 1 min 41 s`, and `1. Résumé`. The date order is handled by
   design; the rest would be refused with a clear message, which is the right failure but
   still a failure. **I want to see an export from a French-locale organizer before
   trusting this on another machine.**
2. **Column changes.** Teams has added engagement columns before. A 16-column Section 2
   is refused outright. Better to refuse than to read the wrong column as the email, but
   it means the first format change stops uploads until the parser is updated.
3. **Section 4 absent or renamed.** Treated as optional. Sections 1 to 3 are required by
   title; a renamed section title ("Activities" instead of "In-Meeting Activities") is a
   refusal.
4. **A meeting that spans midnight, or the November time change.** Times resolve through
   `America/Montreal`; a join at 1:30 AM on the fall-back night is ambiguous and
   `zoneinfo` will pick one. Rounds is at noon, so this is theoretical.
5. **A date where day equals month** (e.g. 10/10/26). Both readings match; the parser
   takes the first, which is fine because they are the same date.
6. **An event whose Teams title has changed mid-year**, or a title with a typo in the
   admin. Matching is exact after whitespace and case folding; a changed title is a
   refusal with the two titles shown. That is the intended behaviour, but it will happen.
7. **Two events on one date with the same title** (a morning and an afternoon rounds).
   `match_event` refuses and asks for the event; choosing it on the form works, since the
   hours check then separates them.
8. **Someone in Section 3 under an email not in Section 2, or vice versa.** Reported as a
   warning, not seen in this file.
9. **Participants with no email at all** (dial-in by phone shows a number as the name and
   nothing in the email column). Handled as unmatched rows with the name as written; not
   present in this file, so untested against real data.
10. **A very large file.** The whole file is read into memory twice (once to hash, once to
    parse). At 16 KB for ten people that is nothing; at a thousand participants it is
    still nothing, but I have not measured.
11. **Re-uploading after a parser fix.** Still undesigned; `import_export` refuses an
    upload that was already parsed.

### Decisions this round that your message did not settle

1. **Checksum tolerance is one second per row**, for the truncation reason above.
2. **Section 2 and Section 3 are keyed on lowercased email, falling back to the cleaned
   display name**, for the checksum only. Importing matches on email alone.
3. **The hours check uses overlap, not containment**: the meeting must overlap the event's
   scheduled window. A meeting that started at 8:57 for a 9:00 event passes; one at 5 PM
   does not.
4. **`match_event` tries both date readings** and refuses if they point at two different
   events. Choosing the event on the form resolves it.
5. **Section 4 (engagement) is parsed but not stored.** Its row count is kept so a
   malformed section is noticed. Storing reactions is not in any doc.
6. **Teaching minutes for a co-presented session are the whole session for each
   co-presenter.** Nothing said to split it; a talk given by two people was given by both.
7. **The accreditation cap applies to attendance only.** `accredited_credits` is what the
   event is accredited for as an attended activity; capping teaching with it would make a
   presenter who also attended lose credit for presenting.
8. **A presenter's own talk raises no review flags**, even if they also filled in an
   evaluation for it with a larger number. Their minutes there are not used.
9. **`presented_session_titles` and `session_titles` are separate lists on the line**,
   so the certificate can print "attended: A, C; presented: B".
10. **Each kind is rounded on its own at issue, and the total is their sum.** The
    alternative (round the blended total) makes the three printed figures not add up.
11. **Migration converted existing certificate totals to attendance credit** and set
    teaching to zero. There are no real certificates yet.

### Deviations

- The upload screen's old "`.csv` or `.xlsx`" filter is kept only as a cheap first check;
  `.xlsx` will be refused by the parser as not a Teams export. The docs said Teams exports
  arrive as either; this one is a tab-separated text file with a `.csv` name, and I have
  no `.xlsx` example to parse.
- `scripts/` is committed with the anonymizer, as you added it. It is not run by anything.

### Unsure

- Whether an export from the same tenant but a different organizer has the same shape.
- Whether to store engagement rows; `is_complete` for evaluations might one day be
  computed from "unmuted" events, but nobody has asked.
- The relabelled Role column still reads "Presenter" in the review queue next to real
  presenters. The label and help text are there; a column that said "screen-share" might
  be clearer still.

## How to run it

```powershell
.\.venv\Scripts\Activate.ps1
python manage.py runserver
```

Then open http://127.0.0.1:8000/admin/ and sign in with `DJANGO_SUPERUSER_USERNAME` and
`DJANGO_SUPERUSER_PASSWORD` from `.env`. The dev database was rebuilt after the
redesign and holds the new seed data.

```powershell
python -m pytest          # 485 tests, under two minutes
python manage.py seed_demo  # only fills an empty database; this one is already seeded
```

To start again from nothing: drop and recreate `cme_dev`, run `python manage.py migrate`,
create a superuser, run `seed_demo`.

## The redesign after your review (A to H)

Everything in your review is built. In order:

- **A. Session times.** `Session.start_at` / `end_at`, validated inside the event and
  non-overlapping, end defaulting to an hour after the start. Attendance is clamped to the
  union of the sessions; breaks count for nothing; five minutes of grace at the ends of
  the whole event. `sessions_attended()` is half the session or more. The event's
  `actual_start_at` / `actual_end_at` are **removed**: with session times they had no job,
  and keeping two ways to say when a talk ended would have been worse than one.
- **B. Credit per session, gated per session.** `credits/rules.py` is rewritten.
  Qualifying minutes per session count only when that session has a complete evaluation;
  the event total is their sum, capped at `accredited_credits`, which stays a field.
- **C. Credit is hours attended.** Your "five minutes" meant that five minutes of a
  talk counts, for five minutes, once the form is filled in; and then you dropped the
  quarter rounding altogether. Credit is now minutes / 60 to the hundredth: five of sixty
  is 0.08, 59 of 60 is 0.98, 65 minutes across two talks is 1.08. The quarter-step rule
  stays only on `accredited_credits`; adjustments can be any hundredth.
- **D. Evaluation windows.** `EvaluationWindow` and `credits/windows.py`: a week from the
  event date, self-service reopenings of a week each, auto-granted and audit-logged, three
  per person per session, none past the accreditation year; program-admin override,
  logged. `sessions_needing_evaluation()` returns what the credits page will list, with
  the state for each session (open, reopened, can request, limit reached, past year end).
  No page yet: that is step 2.
- **E. Moving target.** The person's admin page has an "earned versus certified" table
  per event (`credits/reports.py: person_standing`). Reissue stays manual.
- **F.** Flag unchanged; field renamed `self_reported_session_minutes`; the cap and the
  coordinator rule kept.
- **G.** The admin warns on a room-roster row whose device row was superseded.
  `Certificate.pdf_path` added. A closed event cannot change status.
- **H.** `is_complete` stays stored; the rest left as they were.

Tests you asked for, by name: `test_a_break_between_sessions_does_not_count`,
`test_attending_only_the_middle_session`,
`test_under_half_a_session_is_not_counted_as_attended`,
`test_credit_appears_after_a_late_evaluation`, `test_submission_inside_the_default_week`,
`test_submission_outside_the_default_week`, `test_acceptance_with_an_active_reopening`,
`test_the_window_closes_when_a_complete_evaluation_is_submitted`,
`test_no_self_service_reopening_past_the_accreditation_year`.

### Decisions in the redesign that your review did not settle

1. **The hundredths are cut once, on the event total.** Minutes are gathered per
   session and divided by sixty once. Cut downward, so a figure never overstates. Any
   coarser rounding happens only when the year-end certificate is generated:
   `certificates.rules.certificate_total` rounds the year's sum to the nearest whole
   credit, halves up. The lines stay exact, so they will not always add up to the
   printed total.
2. **Hours-only manual rows now name a session** (`AttendanceRecord.session`, with a
   check constraint). Minutes without times had to belong somewhere to be credited per
   session. Timed rows leave it blank and are matched by their times. The migration
   assigned any existing hours-only row to its event's first session.
3. **Grace at the ends only.** Five minutes before the first session and after the last
   are real connected time and count; a break between talks does not. With credit as
   hours attended there is no cliff any more: a minute late is a hundredth of a credit.
4. **Partial attendance still earns partial credit if evaluated.** The 50% rule decides
   who is *asked* to evaluate and what the certificate lists, not who may. Someone who
   caught the last 20 minutes of a talk and evaluates it anyway earns 20 minutes.
5. **The default window opens when the session starts and closes at midnight at the end
   of the seventh day after the event date.** "One week from the event date" needed an
   hour; midnight local time is the one nobody will argue with.
6. **A draft (incomplete) submission does not close a reopened window.** Only a complete
   one does, so a person who saves half a form keeps their week.
7. **The model refuses a submission with no window open**, staff included. Staff
   entering a paper form late grant a window first (Evaluation windows, add), which is
   the logged override. The seed and tests create past submissions directly.
8. **A self-service grant is logged with the attendee as actor**; an override with the
   staff user. `granted_by` null means self-service.
9. **Closed is final in every direction**, not only back to draft. Held, published and
   draft stay editable.
10. **Certified credit counts only valid certificates** (not revoked, not superseded)
    in the earned-versus-certified table.
11. **Certificate lines will list the sessions attended (half or more)**, which is what
    `sessions_attended()` gives. A session evaluated for partial credit below that line
    earns its minutes but is not listed by title. Issuance is not built, so this is
    recorded, not coded.
12. **The seed events are now noon to three with three one-hour sessions**, since that
    is the typical event. Every expected figure in the seed tests was recomputed by hand
    for the awkward cases (Côté's 54 minutes; Nguyen's lobby time; the room roster copy)
    and by the code for the rest.

### Things I would still raise

1. **Teams rows cannot be created yet.** Step 1 has no parser. Entering a real event
   before step 2 means manual rows. You agreed to build the parser before the next rounds.
2. **Staff entering a late paper evaluation have to grant a window first.** Two steps
   instead of one. It keeps every late submission visibly authorised; tell me if it is
   too much friction.
3. **Three one-hour sessions means `accredited_credits` is usually 3.00**, and the seed
   says so. The admin does not derive it from the sessions, on purpose.

## What was built

| App | Contents |
| --- | --- |
| `config` | Settings split into base / dev / prod. Secrets only from the environment, no defaults. |
| `core` | UUID base model, append-only and frozen-field mixins, `authz.py`, shared admin helpers, `seed_demo`. |
| `accounts` | Custom `User` (staff only), the three staff roles. |
| `people` | `Person`, `PersonEmail`, `AllowedDomain`, `SignInRequest`; normalization, `resolve_root`, the merge service. |
| `rounds` | `RoundsEvent`, `Session`, `SessionPresenter`, `LearningObjective`, `COIDeclaration`. |
| `attendance` | `AttendanceUpload`, `AttendanceRecord`, `AttendanceSupersession`; `attended_minutes()`; match, supersede and upload services. |
| `credits` | `EvaluationSubmission`, `EvaluationResponse`, `CreditAdjustment`; the credit rules. |
| `certificates` | `Certificate`, `CertificateLine`; verification-code and certificate-type rules. Models only. |
| `audit` | `AuditLog` and `audit.log.record()`. |

Not built, on purpose: `MagicLinkToken` (arrives with sign-in), certificate issuing and PDFs,
the Teams parser, any public page, `deploy.sh`, anything multi-tenant.

### The two things you said mattered most

**Attendance aggregation** is `attendance/aggregation.py: attended_minutes(person, event)`.
It takes active rows, clamps each interval to the event's credit window, takes the union
of the intervals, adds hours-only rows, and caps at the event's length. 54 tests cover
rejoins, gaps, overlapping connections, the grace period, `actual_end_at`, superseded
rows, hours-only manual rows, the cap and room-roster rows.

I checked the tests can fail by breaking the function six ways and running them each
time: counting earliest join to latest leave (3 tests fail), summing rows instead of
taking the union (8), not clamping to the window (5), counting superseded rows (5),
ignoring hours-only rows (9), and removing the cap (2). That exercise found two of my own
overlap tests passing by accident: once the total was capped at the event length, a naive
sum of a full-length laptop row and a phone row was capped back to the right answer. Both
now use cases shorter than the event, where a sum gives a different number.

One correction to the comment you asked for on
`test_laptop_and_phone_both_running_past_actual_end`. Clamping before merging and merging
before clamping always give the same answer, so there is no ordering to pin. What the
test pins is that both happen at all, rather than the total simply being capped
afterwards. I changed it to a late join (20 to 90 and 30 to 85, ending at 70, answer 50)
because with a join at 0 every shortcut also gives 70. The docstring says this.

**Object-level authorization** is `core/authz.py`. Every person-owned model has
`for_person()`. A view that takes an id is wrapped in `@owned_object(Model)`, which does
the lookup through `for_person()` and hands the view the object, or 404. A deliberately
public route uses `@public_object("reason")`. `unmarked_routes()` walks the URL
configuration, and a test fails if any route with a URL parameter is neither. I confirmed
it fails by adding an unprotected `certificate/<uuid:id>/` route, then removed it.
`request.person` is the seam the sign-in step will fill; until then every such view 404s.

### Staff roles

Three Django groups: Coordinator, Program admin, Read only. `accounts/roles.py` is the
source of truth. A data migration creates the groups; their permissions are re-applied
from that file after every `migrate`, because permissions do not exist yet while
migrations run and so that a model added later is covered. **Editing a group's
permissions by hand in the admin will not stick.**

## Decisions I made that the docs did not cover

### Credit and attendance

- **Grace minutes count as attended time, up to the session's length.** Joining at 11:57
  and leaving at 12:50 is 53 minutes. Joining at 11:40 earns five early minutes, not
  twenty.
- **A late session start shortens the session.** If a talk's `start_at` is 12:15, it
  lasted 45 minutes and nobody can show more than 45 for it.
- **Recorded attendance wins even when it adds up to zero.** Someone who only sat in the
  lobby has rows, so their self-report is not used; they are flagged instead.
- **Credit resting on a self-report alone is always flagged for review**, and is capped
  at the session's length.
- **The event page says why someone is flagged**: claims more than was recorded,
  self-reported only, or rows adding up to more than a session lasted.
- **Attended minutes round down to whole minutes** before the credit calculation.
- **Credit adjustments cannot be zero** and can be any hundredth of a credit.
- **A mistaken row is voided by superseding it with a zero-minute manual row.** Nothing is
  ever deleted, including manual rows.
- **The replacement in a supersession must be a live row and in the same event.** That is
  also what makes loops impossible.
- **Rows can be superseded only one way round.** A room-roster row stays active if the
  device row it points at is later superseded. I think that is right (the person was
  still in the room) but it is a judgment.

### Matching

- **Confirming a match clears the person's other unmatched rows with the same address,
  in every event**, not just the one on screen. One decision deals with all the rejoin
  rows.
- **An address already owned by someone else is never reassigned** by a match.
- **Matching to a merged-away record lands on the survivor.**
- `matched_by` is null for automatic matches; `matched_at`/`matched_by` describe the
  latest decision, and the history is in the audit log.

### People and merging

- **Licence normalization is exactly what the doc says**: whitespace, leading zeros,
  uppercase. Hyphens and dots are not stripped, so `12-345` and `12345` do not match. An
  all-zero number normalizes to `0`.
- **A licence number requires a jurisdiction** (check constraint).
- **Merged tombstones are exempt from the licence uniqueness constraint**, and a merge
  does not copy the licence number. The admin tells you when the merged record held one.
- **A survivor with no primary email inherits the duplicate's.** Otherwise moved
  addresses become non-primary.
- **A linked staff account moves with the merge**; two staff-linked records are refused.
- **Earlier tombstones are re-pointed at the new survivor**, so chains stay one hop.
- **Audit entries are not re-pointed.** They keep naming whoever acted.
- **The merge finds tables by introspection** rather than a hand-kept list. A test fails
  when a new table points at `Person`, so the decision is made deliberately.

### Sessions and conflict of interest

- **A declaration expires on the next 30 June** by default.
- **A presenter with no declaration set picks up their most recent valid one on save**,
  judged against the event date. A later declaration never replaces it. There is also an
  admin action to attach declarations made after the presenter was added.
- **Declarations are add-only for staff too.** One entered in the admin is logged with
  `entered_by_staff`.

### Roles

- **Merging people is Program admin only.** You listed adjustments and certificates; I
  added merging because it changes whose credit is whose.
- **Entering or deleting evaluations, allowed domains and sign-in decisions are Program
  admin only.** Evaluations are half of the credit gate.
- **Coordinators can add manual attendance rows.** You gave them the match queue and room
  rosters, which needs it, but manual rows are the fraud surface the security baseline
  names. Every one is audit-logged with its reason. Say so if you want this tightened.
- **Staff accounts and groups are superuser only.**

### Other

- **Deletes are blocked almost everywhere** (`PROTECT`). The only cascades are a
  session's presenters and objectives, and an evaluation's responses.
- **Admin sign-ins are logged; failed sign-ins are not**, because an attacker could
  otherwise fill the audit table.
- **Uploads accept `.csv` and `.xlsx` up to 10 MB**, stored as
  `UPLOAD_ROOT/teams/<sha256>.<ext>` and made read-only.
- **Verification codes** are 12 characters in three groups from a 30-character alphabet
  (no 0/O, 1/I/L, U), about 59 bits.
- **`seed_demo` refuses production settings and a non-empty database.** It creates an
  inactive `seed-script` user as the author of its rows.

## Deviations from the docs or from what you asked

- **`AuditLog` actor FKs are `PROTECT`, not `SET NULL`** as I first wrote into the schema.
  Nulling them is an UPDATE on audit rows, which the production app role must not be able
  to do. Staff accounts with history are deactivated, not deleted. `actor_label` still
  covers renames. `data-model.md` is updated.
- **One admin template exists**: the merge confirmation page
  (`people/templates/admin/people/person/merge.html`). You said no templates in step 1; a
  merge needs a screen that shows both records and asks which to keep.
- **The self-report fallback lives in `credits.rules.creditable_minutes`**, not inside
  `attended_minutes`. Evaluations belong to the credits app, and attendance cannot import
  from it. `data-model.md` describes both.
- **Staff-role permissions are applied after `migrate`, not inside the data migration.**
  The migration creates the three groups, as asked.
- **`/certificates/` was removed from `.gitignore`.** It was there for generated PDFs and
  was silently hiding the `certificates` app from git. PDFs are still covered by `*.pdf`
  and `/uploads/`. You reviewed that file, so you should know it changed.
- **Text columns have length limits** (names 200, titles 300) where the doc says `text`.
- **`rounds/admin.py` and `people/admin.py` import from apps above them** (credits,
  audit) to show the credit table and log actions. Only admin modules do this; the models
  and rules still depend strictly downward.
- **The deploy sketch and runtime layout in `deployment.md` are now per instance**
  (`/srv/cme/<org>/`, `cme@<org>`), following the multi-tenant decision. Nothing is built.
- **Global git config**: `init.defaultBranch` and `core.autocrlf` were set as asked. Your
  global `user.email` was left alone; this repo uses the GitHub noreply address, and the
  first five commits were rewritten to it before anything was pushed.

## Things I am unsure about

- **`is_complete` is stored**, per H. Compute it from the responses when the form exists.
- **Editing an existing evaluation is not window-checked.** Only new submissions are.
  The admin can edit at any time (logged); the attendee form will decide its own rule.
- **A closed event still does not stop anything by itself** beyond its own status; the
  evaluation windows are what stop late submissions.
- **Approving a `SignInRequest` only records the decision.** It does not create a person
  or send anything; that belongs with sign-in.
- **The audit log's IP will be `127.0.0.1` behind Caddy** until the forwarded header is
  read. That is a deployment-step change, not done here.
- **Session length.** Django's default two weeks applies to the admin. The 90-day figure
  in the security baseline is for attendees and belongs with sign-in.
- **The event page's credit table runs a few queries per person.** Fine for 40 people;
  it would want rewriting for 400.
- **Re-parsing an upload is still undesigned**, as noted in `decisions.md`.
  `parser_version` is recorded on uploads and rows.
- **Law 25 and `actor_label`** remain open, linked to the retention decision.
- **The trainee certificate type and the series name** are both waiting on McGill's CPD
  office. `SERIES_NAME` is still the placeholder "Health Informatics Rounds".

## What the seed data shows

Sign in and try these:

- **People → filter "Possible duplicates"**: two Marie Tremblays. Select both, choose
  "Merge two selected people". Before the merge her second-event credit rests on a
  self-report; after it, on 70 recorded minutes.
- **Attendance records → filter "Unmatched"**: six rows. Three are "Lea B." rejoins from
  an unknown address. Pick Léa Bouchard on one and save: the other two follow.
- **Rounds events → open either event**: the per-person credit table at the bottom, now
  with sessions attended and evaluated out of three. Rejoins (Côté: 54 minutes of the
  first talk, 0.75), laptop and phone at once (Haddad, two
  talks evaluated, 2.00), lobby time clamped (Nguyen), a room roster (Lavoie, Roy),
  superseded rows (Sharma), an adjustment (Okafor), a self-report-only case (Morin), and
  on the second event a talk that ran over (Gagnon, 65 minutes).
- **Sessions → filter "A presenter has no declaration"**: Haddad.
- **Evaluation windows**: Bouchard asked for another week on the first talk.
- **People → Haddad → "Credit, earned versus certified"**: 2.00 earned, nothing certified.
- **Audit log**: everything the seed did by hand, plus your own sign-in.

All names and addresses in the seed data are fictional.

## Commits

One commit per step, in order: repo hygiene, the schema revision, settings, peer
authentication, design notes, people and authorization, events and sessions, attendance,
credits, certificates, audit, services, the review-flag change, admin, staff roles, seed
data and admin tests, multi-tenant docs, the attendance cap and its tests, these notes,
run.bat, then the per-session redesign in three commits (code and migrations, seed and
admin tests, docs and notes). `git log --oneline` shows them.
