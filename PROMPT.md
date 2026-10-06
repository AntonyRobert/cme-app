# Kickoff prompt

Paste this into Claude Code with this folder open.

---

Read `CLAUDE.md` and everything in `docs/` before doing anything. `docs/data-model.md` is
the source of truth for the schema.

## My environment

- Windows, VS Code, Claude Code
- Python 3.13, PostgreSQL 17, git 2.52 — all installed already
- Target server is Ubuntu on AWS Lightsail, but that is much later. Nothing today touches
  AWS.
- I understand programming but I am new to Django. Explain choices briefly as you go.

## Step 0: environment setup

Do these first, then show me what you did before moving on.

1. **git config.** Set `user.name`, `user.email`, `init.defaultBranch main`, and
   `core.autocrlf true` globally. The last one matters because I'm on Windows and the
   server is Linux — `deploy.sh` must end up with LF endings in the repo.
2. **`git init`** in this folder, then write a `.gitignore` before anything is staged. It
   must cover `.env`, `__pycache__/`, `*.pyc`, `.venv/`, `/uploads/`, `/staticfiles/`,
   `*.pdf`, and anything else Django projects routinely leak. Show me the file and wait
   for my OK before the first commit — this is the list standing between me and
   publishing a secret.
3. **Virtual environment.** Create `.venv` and tell me the exact command to activate it in
   PowerShell. Tell me which interpreter to select in VS Code.
4. **Local database.** Give me the `psql` commands to create a `cme_dev` database and a
   role for it. I'll run those myself and tell you the credentials went into `.env`.
5. **`.env.example`** with placeholder values, committed. The real `.env` never is.

## Then: review before you build

Before writing any application code, do these three things and stop so I can react:

1. Tell me anything in the docs that looks wrong, contradictory, or like it will cause
   pain later. You have more Django context than I do. I would rather hear it now than
   after the migration.
2. List the models you plan to create, the app layout you propose, and anything in
   `data-model.md` you would change before writing it.
3. Tell me which decisions you need from me that the docs don't answer.

Then wait. Don't scaffold or write migrations until I've replied.

## Build order, once I've confirmed

Step 1 of the build order only: models, migrations, and the Django admin. No custom views,
no templates, no auth yet. The admin is the entire interface at this stage, and that is
deliberate — I'm going to run a real rounds event on it before building any screens.

Pause after each for review:

- `requirements.txt`, settings split (base / dev / prod), Postgres not SQLite even in dev.
  Django 5.2 LTS, not 6.x.
- The models, in dependency order: Person and identity first, then sessions, then
  attendance, then evaluation and credits, then certificates, then audit.
- Admin registrations with list displays, search fields and filters that make the
  spreadsheet-review and record-merge workflows actually usable.
- Tests for the rules in `CLAUDE.md` that can be tested: the credit function, the
  attendance aggregation across rejoin rows, the merge resolver, licence number
  normalization.

## Two things I care about more than speed

The attendance aggregation is the piece most likely to be subtly wrong. Teams writes one
row per join, not per person, and rows can be superseded by manual corrections. Write it
once as a method with tests covering rejoins, superseded rows, manual rows, and room
roster rows.

Object-level authorization will matter as soon as there are views. Set up the pattern now,
even though the admin handles it at this stage, so it isn't bolted on later.
