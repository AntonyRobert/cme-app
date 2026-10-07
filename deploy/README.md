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
| `cme-backup@.service`, `.timer` | Nightly `backup.sh`. |
| `backup.sh`, `restore-test.sh` | Local backups and the proof they restore. |
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
   sudo bash /tmp/cme-app/deploy/server-setup.sh mcgill cme.mri3.ca git@github.com:AntonyRobert/cme-app.git you@example.org
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
9. `curl -I https://cme.mri3.ca/` should return `200` (or `302` to the admin login) with
   `strict-transport-security` in the headers.

Routine deploys are step 7 alone.

`server-setup.sh` is safe to rerun at any time; every step checks before it acts.

## Email

`EMAIL_BACKEND=console` in `/etc/cme/<org>.env` until SES verifies the domain: every
email, magic links included, is printed to the journal (`journalctl -u cme@mcgill -f`)
and nothing is sent. When SES is ready: set `EMAIL_BACKEND=smtp`, fill the four
`EMAIL_*` lines, `systemctl restart cme@mcgill`. One line to flip, four to fill.

## Backups and the restore drill

`cme-backup@mcgill.timer` runs `backup.sh` nightly: a `pg_dump` and a tarball of
uploads into `/var/backups/cme/mcgill/`, 14 days kept. Off-box shipping (restic to S3)
is a commented block in `backup.sh` until the bucket exists; until then **a disk failure
loses everything**, so do not leave it that way for long.

`restore-test.sh mcgill` restores the latest dump into a scratch database, compares row
counts with live, and drops the scratch. Run it after the first deploy and after any
Postgres upgrade.

The full drill, done by hand so each step is seen (expect a minute of downtime):

```bash
sudo /srv/cme/mcgill/deploy/backup.sh mcgill
sudo systemctl stop cme@mcgill
sudo -u postgres psql -c 'DROP DATABASE cme_mcgill'
sudo -u postgres createdb --owner=cme_mcgill_owner --template=template0 --encoding=UTF8 --locale=C.UTF-8 cme_mcgill
sudo -u cme_mcgill_owner pg_restore --no-owner --no-privileges --exit-on-error -d cme_mcgill \
    "$(ls -1t /var/backups/cme/mcgill/cme_mcgill-*.dump | head -1)"
sudo -u cme_mcgill_owner psql -v app=cme_mcgill -d cme_mcgill -f /srv/cme/mcgill/deploy/grants.sql
sudo systemctl start cme@mcgill
sudo /srv/cme/mcgill/deploy/manage.sh mcgill shell -c "from people.models import Person; print(Person.objects.count())"
```

The grants line matters: a restore with `--no-privileges` recreates tables owned by the
owner role with no grants to the app role, and gunicorn would get "permission denied"
until it runs.

## Where things are

```
/srv/cme/<org>/            checkout, owned cme_<org>_owner:cme_<org>
/srv/cme/<org>/uploads/    Teams exports and PDFs, owned cme_<org>
/srv/cme/<org>/staticfiles collected static, served by Caddy
/etc/cme/<org>.env         settings and the secret key, root 0600
/run/cme/<org>.sock        gunicorn's socket, 0660, caddy in the group
/etc/caddy/sites/<org>.caddy
/var/backups/cme/<org>/    nightly dumps
journalctl -u cme@<org>    application log
```
