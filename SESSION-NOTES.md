# Session notes: step 1

Written 2026-10-06, updated the same day after your review (sections A to H). Covers
everything built in the first session: environment setup, the pre-build review, step 1 of
the build order (models, migrations, admin, tests), and the per-session credit redesign.
Step 2 has not been started. Nothing has been pushed; the repo has no remote.

## How to run it

```powershell
.\.venv\Scripts\Activate.ps1
python manage.py runserver
```

Then open http://127.0.0.1:8000/admin/ and sign in with `DJANGO_SUPERUSER_USERNAME` and
`DJANGO_SUPERUSER_PASSWORD` from `.env`. The dev database was rebuilt after the
redesign and holds the new seed data.

```powershell
python -m pytest          # 381 tests, about 90 seconds
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
- **C. Minutes as recorded.** Your "five minutes" meant that five minutes of a talk
  counts, for five minutes, once the form is filled in. There is no tolerance rounding 59
  up to 60; I had built one and removed it. Tests: five of sixty is five minutes; 60 + 5
  across two talks is 65, rounded once to 1.00; 59 of 60 is 59, which is 0.75.
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

1. **Rounding happens once, on the event total, not per session.** You wrote "per
   session ... round down to the quarter as before". Rounding each session separately
   would turn three 20-minute talks attended in full into 3 x 0.25 = 0.75 rather than
   1.00, and would break "total equals the sum of the lines" the moment sessions are not
   multiples of 15 minutes. Minutes are gathered per session, then rounded once per
   event. Your two examples (59 of 60 is 1.00, 52 is 0.75) hold either way; a test pins
   the three-short-sessions case. Say so if you want per-session rounding regardless.
2. **Hours-only manual rows now name a session** (`AttendanceRecord.session`, with a
   check constraint). Minutes without times had to belong somewhere to be credited per
   session. Timed rows leave it blank and are matched by their times. The migration
   assigned any existing hours-only row to its event's first session.
3. **Grace at the ends only.** Five minutes before the first session and after the last
   are real connected time and count; a break between talks does not. With no tolerance,
   someone who joins the first talk a minute late and leaves on time has 59 minutes,
   which rounds down to 0.75. That is the cliff from my earlier notes, back as a
   consequence of "minutes as recorded"; it is yours to accept or not.
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
- **Credit adjustments go in steps of 0.25 and cannot be zero**, so a certificate total
  stays a multiple of a quarter.
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
