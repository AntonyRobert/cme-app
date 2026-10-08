# Deploying

The target is one Lightsail instance per `docs/deployment.md`. Everything here is
parameterized by `<org>` (today: `mcgill`), even with one tenant.

| File | What |
| --- | --- |
| `server-setup.sh` | One-time: packages (incl. fail2ban), users, Postgres roles, directories, deploy key and clone, units, Caddy. Read it first. Runs twice by design. |
| `deploy.sh` | Every deploy: pull, pip, checks, migrate (as owner), grants, collectstatic, restart, smoke test. |
| `manage.sh` | `manage.py` with the production environment, as the owner user. |
| `grants.sql` | What the app role may do. Applied after every migrate. |
| `cme@.service` | gunicorn, one instance per tenant, Unix socket, hardened. |
| `cme-backup@.service`, `.timer`, `cme-backup-failed@.service` | Nightly `backup.sh`; the failure unit makes a failed night visible. |
| `backup.sh`, `backup-now.sh` | Backup to the tenant's restic repository in S3; run one now and see the log. |
| `restore-test.sh`, `restore-drill.sh` | Prove the S3 backup restores (scratch database); replace the live data from it (the drill). |
| `split-backup-env.sh` | Move the backup credentials out of the web process's env file, verifying each step. |
| `env.backup.template` | The backup credentials file, `/etc/cme/<org>.backup.env`. |
| `env.template`, `site.caddy.template` | Filled in by `server-setup.sh`. |

## Order of operations, first time

1. **Lightsail console** (by hand): Ubuntu 24.04 LTS, 2 GB, `ca-central-1`; attach a
   static IP; firewall SSH (22), HTTP (80), HTTPS (443) only.
2. **GitHub**: the repo is private; push it.
3. **Bootstrap clone.** The setup script installs the unit files and templates that sit
   beside it, so it must run from a checkout. SSH in as `ubuntu` and, with any key that
   can read the repo (a temporary deploy key for `ubuntu` is fine), clone to a scratch
   location:
   ```
   git clone git@github.com:AntonyRobert/cme-app.git /tmp/cme-app
   sudo bash /tmp/cme-app/deploy/server-setup.sh mcgill mcgill.cme.mri3.ca git@github.com:AntonyRobert/cme-app.git admin@mri3.ca
   ```
   The last argument is the ACME email: where Let's Encrypt sends certificate expiry
   warnings. Use an address someone reads.
4. **The first run is expected to fail at the fetch.** It generates a deploy key for the
   owner user `cme_mcgill_owner`, prints the public half, and stops because that key is
   not on GitHub yet. Add it: repo → Settings → Deploy keys → Add, **read-only**. Rerun
   the same command; the repository is set up in place in `/srv/cme/mcgill` (the
   directory already holds `uploads/` and `.ssh/`, so it is initialised and fetched
   rather than cloned) and the script finishes. Every later run just pulls.
5. **Afterwards, remove the temporary key** you used for the bootstrap clone from GitHub
   (Settings → Deploy keys) and `rm -rf /tmp/cme-app`. The server pulls with the owner
   user's key and nothing else from now on; one key per purpose.
6. **DNS at GoDaddy**: `A` record, host `cme`, value = the static IP, TTL 600. Add it
   now, before the first deploy: Caddy requests the certificate as soon as the name
   resolves here, and nothing works over HTTPS until then.
7. `sudo /srv/cme/mcgill/deploy/deploy.sh mcgill`
8. `sudo /srv/cme/mcgill/deploy/manage.sh mcgill createsuperuser`
9. `curl -I https://mcgill.cme.mri3.ca/` should return `200` (or `302` to the admin login) with
   `strict-transport-security` in the headers.

Routine deploys are step 7 alone.

`server-setup.sh` is safe to rerun at any time; every step checks before it acts, and
after the first run it remembers its arguments, so a rerun is `sudo /srv/cme/mcgill/deploy/server-setup.sh mcgill`
and cannot regenerate the site under a different hostname by accident.

## Email

`EMAIL_BACKEND=console` in `/etc/cme/<org>.env` until SES verifies the domain: every
email, magic links included, is printed to the journal (`journalctl -u cme@mcgill -f`)
and nothing is sent. When SES is ready: set `EMAIL_BACKEND=smtp`, fill the four
`EMAIL_*` lines, `systemctl restart cme@mcgill`. One line to flip, four to fill.

## Backups

`cme-backup@mcgill.timer` runs `backup.sh` nightly at 03:15: a `pg_dump` of the database
and the whole `/srv/cme/mcgill/uploads/` directory go into the tenant's restic
repository in S3 (`s3:s3.ca-central-1.amazonaws.com/mri3-cme-backups/mcgill`), then
`restic forget --prune` keeps 7 daily, 5 weekly and 12 monthly snapshots, then
`restic check` (a 5% data read on Sundays). The uploads matter as much as the database:
the raw Teams exports are the evidence behind credit claims and exist nowhere else. A
local copy of the dump is kept 14 days in `/var/backups/cme/mcgill/`; it protects
against a bad migration or a fat-fingered DELETE and nothing else. **S3 is the copy that
matters.**

**Credentials** live in `/etc/cme/mcgill.backup.env` (root, 0600; template in
`env.backup.template`), read by the backup unit only, so gunicorn never carries them.
If they were put in `/etc/cme/mcgill.env` first, move them with the script, which
extracts all four, verifies they landed, and only then removes them from the main file
(it refuses and changes nothing if the count is wrong):

```
sudo /srv/cme/mcgill/deploy/split-backup-env.sh mcgill
```

**RESTIC_PASSWORD must exist somewhere other than this server.** The repository is
encrypted with it; without it the backups are unrecoverable even with full AWS access.

**When a backup fails**, the unit fails (any non-zero step) and `cme-backup-failed@mcgill`
logs at error priority: `journalctl -p err -t cme-backup` shows it, and so does
`systemctl --failed`. A notification (email through the app once SES is live, or a
healthcheck ping) still needs wiring there; until then, check `systemctl list-timers` and
the failed list when you look at the box. A backup that silently stops is worse than none.

**Run a backup now** and see how it went:

```
sudo /srv/cme/mcgill/deploy/backup-now.sh mcgill
```

## The restore test, from S3

`restore-test.sh` pulls the latest snapshot from S3 into a scratch directory, restores
the dump into `cme_mcgill_restoretest`, compares row counts with the live database,
checks every restored upload against the live file byte for byte, and drops the scratch
database. The live database and uploads are never touched. It loads the backup
environment itself. Run it right after a backup so the counts match:

```
sudo /srv/cme/mcgill/deploy/restore-test.sh mcgill
```

Run it after the first deploy, after any Postgres upgrade, and once a quarter.

## The full drill: replace the live database from S3

`restore-drill.sh` does what December would need: stops gunicorn, restores the latest
snapshot from S3, drops and recreates the database from the dump, re-applies
`grants.sql` (a `--no-privileges` restore leaves the app role with nothing), puts the
restored uploads in place, starts gunicorn and checks it answers. About a minute of
downtime. Without the flag it only says what it would do:

```
sudo /srv/cme/mcgill/deploy/restore-drill.sh mcgill --replace-live-data
```

Done once by hand with the database nearly empty; repeat yearly.

## Where things are

```
/srv/cme/<org>/            checkout, owned cme_<org>_owner:cme_<org>
/srv/cme/<org>/uploads/    Teams exports and PDFs, owned cme_<org>
/srv/cme/<org>/staticfiles collected static, served by Caddy
/etc/cme/<org>.env         settings and the secret key, root 0600
/run/cme/<org>/gunicorn.sock  gunicorn's socket; systemd makes the directory for the tenant user on each start
/etc/caddy/sites/<org>.caddy
/var/backups/cme/<org>/    the local copy of the dump, 14 days; S3 is the backup
/etc/cme/<org>.backup.env  restic repository, password and AWS keys, root 0600, backup unit only
journalctl -u cme@<org>    application log
```
