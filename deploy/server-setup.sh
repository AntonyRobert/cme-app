#!/usr/bin/env bash
# One-time server setup for one CME tenant. Read it before running it.
#
# Run it from a checkout of the repo, because it installs the unit files and
# templates that sit beside it in deploy/:
#
#   git clone git@github.com:AntonyRobert/cme-app.git /tmp/cme-app     # with any key that can read the repo
#   sudo bash /tmp/cme-app/deploy/server-setup.sh <org> <hostname> <git-ssh-url> <acme-email>
#   e.g.  sudo bash /tmp/cme-app/deploy/server-setup.sh mcgill cme.mri3.ca \
#             git@github.com:AntonyRobert/cme-app.git you@example.org
#
# Ubuntu 24.04 LTS on Lightsail. Idempotent where it reasonably can be: running
# it twice is safe, and it is EXPECTED to run twice: the first run generates the
# owner user's deploy key and stops at the clone, because that key is not on
# GitHub yet. Add it (read-only), rerun, and the clone goes through. It writes
# /etc/cme/<org>.env with a fresh SECRET_KEY; everything else in that file is
# settings, not secrets.
#
# What it sets up, in order:
#   1. packages: Python 3.12 (Ubuntu's own), git, fail2ban, Postgres 17 (PGDG repo), Caddy (official repo)
#   2. OS users: cme_<org> (runs gunicorn, owns uploads) and cme_<org>_owner (runs migrate, owns the checkout)
#   3. Postgres: two peer-authenticated roles matching those users, one database owned by the owner role
#   4. directories per docs/deployment.md: /srv/cme/<org>, /etc/cme/<org>.env, /run/cme; the deploy key; the clone
#   5. systemd: cme@.service template, cme-backup@.service and .timer, /run/cme via tmpfiles
#   6. Caddy: a site block for the hostname proxying to the tenant's socket, static files served directly
set -euo pipefail

USAGE="usage: server-setup.sh <org> <hostname> <git-ssh-url> <acme-email>"
ORG="${1:?$USAGE}"
HOSTNAME_FQDN="${2:?$USAGE}"
GIT_URL="${3:?$USAGE}"
# Where Let's Encrypt sends certificate expiry warnings. Use an address someone reads.
ACME_EMAIL="${4:?$USAGE}"

if [[ ! "$ORG" =~ ^[a-z][a-z0-9]{1,15}$ ]]; then
  echo "org must be a short lowercase name (it becomes a unix user and a database name)" >&2
  exit 1
fi
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

APP_USER="cme_${ORG}"
OWNER_USER="cme_${ORG}_owner"
DB="cme_${ORG}"
CHECKOUT="/srv/cme/${ORG}"
ENV_FILE="/etc/cme/${ORG}.env"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\n==> %s\n' "$*"; }

# The unit files and templates live beside this script in the repo's deploy/.
for f in env.template cme@.service cme-backup@.service cme-backup@.timer site.caddy.template grants.sql; do
  [[ -f "$HERE/$f" ]] || { echo "missing $HERE/$f: run this script from a checkout of the repo (see the header)" >&2; exit 1; }
done

# --- 1. Packages ---------------------------------------------------------------------
say "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg lsb-release git build-essential libpq-dev \
  unattended-upgrades fail2ban

# Python: Ubuntu 24.04's own 3.12. Django 5.2 supports it; a third-party PPA on
# a box holding accreditation records is not worth a version bump that changes
# nothing. Local development may run 3.13; the two are compatible for this app.
apt-get install -y -q python3 python3-venv python3-dev

# fail2ban: the sshd jail, on. Ubuntu's package ships it disabled by default
# (no jail.d/defaults-debian.conf), so say so explicitly. Lightsail images
# already have password authentication off for sshd.
cat > /etc/fail2ban/jail.d/cme.conf <<'JAIL'
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
backend = systemd
JAIL
systemctl enable --now fail2ban
systemctl restart fail2ban

# Postgres 17 from PGDG, pinned to the major we run locally.
if [[ ! -f /etc/apt/sources.list.d/pgdg.list ]]; then
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list
  apt-get update -q
fi
apt-get install -y -q postgresql-17 postgresql-client-17

# Caddy from its official repo (the Ubuntu one lags).
if [[ ! -f /etc/apt/sources.list.d/caddy-stable.list ]]; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
fi
apt-get install -y -q caddy

# Security updates apply themselves. Reboots for kernels are left to you.
dpkg-reconfigure -f noninteractive unattended-upgrades

# --- 2. OS users ---------------------------------------------------------------------
say "Users"
# The owner user runs migrate and owns the checkout; the app user runs gunicorn
# and owns uploads. Peer auth maps each to the Postgres role of the same name,
# so the process that serves requests can never run DDL.
getent group "$APP_USER" >/dev/null || groupadd --system "$APP_USER"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --gid "$APP_USER" --home-dir "$CHECKOUT" \
  --shell /usr/sbin/nologin "$APP_USER"
id -u "$OWNER_USER" >/dev/null 2>&1 || useradd --system --gid "$APP_USER" --home-dir "$CHECKOUT" \
  --shell /bin/bash "$OWNER_USER"
# Caddy reads the socket, which the app user creates group-readable.
usermod -aG "$APP_USER" caddy

# --- 3. Postgres ----------------------------------------------------------------------
say "Postgres roles and database"
# Debian's default pg_hba.conf already has:  local all all peer
# which is exactly the rule we want: a local socket connection is accepted as
# the Postgres role named after the OS user, and nothing else, no password.
# We only make sure no TCP listener is exposed and no password-based local rule sneaks in.
PG_CONF="/etc/postgresql/17/main/postgresql.conf"
PG_HBA="/etc/postgresql/17/main/pg_hba.conf"
sed -i "s/^#\?listen_addresses.*/listen_addresses = ''/" "$PG_CONF"
grep -qE '^local\s+all\s+all\s+peer' "$PG_HBA" || {
  echo "pg_hba.conf has no 'local all all peer' line; refusing to guess. Fix it and rerun." >&2
  exit 1
}
systemctl enable --now postgresql
systemctl restart postgresql

sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${OWNER_USER}') THEN
    CREATE ROLE "${OWNER_USER}" LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${APP_USER}') THEN
    CREATE ROLE "${APP_USER}" LOGIN;
  END IF;
END
\$\$;
SQL
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname = '${DB}'" | grep -q 1 \
  || sudo -u postgres createdb --owner="$OWNER_USER" --encoding=UTF8 --locale=C.UTF-8 --template=template0 "$DB"
# The app role may connect and use the schema; table grants are applied by
# deploy.sh after every migrate (deploy/grants.sql), because that is when
# new tables appear.
sudo -u postgres psql -v ON_ERROR_STOP=1 -d "$DB" <<SQL
REVOKE ALL ON DATABASE "${DB}" FROM PUBLIC;
GRANT CONNECT ON DATABASE "${DB}" TO "${APP_USER}";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO "${APP_USER}";
ALTER SCHEMA public OWNER TO "${OWNER_USER}";
SQL

# --- 4. Directories -------------------------------------------------------------------
say "Directories"
# The parent belongs to root and is traversable by everyone, so a second
# tenant's users can reach their own directory; only the tenant directory is scoped.
install -d -o root -g root -m 0755 /srv/cme
install -d -o "$OWNER_USER" -g "$APP_USER" -m 0750 "$CHECKOUT"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$CHECKOUT/uploads"
install -d -o root -g root -m 0755 /etc/cme
install -d -o root -g root -m 0750 "/var/backups/cme/${ORG}"
install -d -m 0755 /run/cme

if [[ ! -f "$ENV_FILE" ]]; then
  SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')"
  sed -e "s|__ORG__|${ORG}|g" -e "s|__HOSTNAME__|${HOSTNAME_FQDN}|g" -e "s|__SECRET__|${SECRET}|g" \
    "$HERE/env.template" > "$ENV_FILE"
  chown root:root "$ENV_FILE"
  chmod 0600 "$ENV_FILE"
  say "Wrote $ENV_FILE with a fresh SECRET_KEY. Nothing in it is a placeholder except what the comments say."
fi

# Deploy key: the owner user pulls the private repo read-only.
OWNER_SSH="$CHECKOUT/.ssh"
if [[ ! -f "$OWNER_SSH/id_ed25519" ]]; then
  install -d -o "$OWNER_USER" -g "$APP_USER" -m 0700 "$OWNER_SSH"
  sudo -u "$OWNER_USER" ssh-keygen -q -t ed25519 -N "" -C "cme-${ORG}-deploy" -f "$OWNER_SSH/id_ed25519"
  sudo -u "$OWNER_USER" ssh-keyscan -t ed25519 github.com >> "$OWNER_SSH/known_hosts" 2>/dev/null
  say "Deploy key generated for ${OWNER_USER}. Add this PUBLIC key to the GitHub repo as a READ-ONLY deploy key:"
  cat "$OWNER_SSH/id_ed25519.pub"
  echo
  echo "Then rerun this same command. Any deploy key you added earlier for another user (e.g. ubuntu)"
  echo "should be removed from GitHub once this one works: one key per purpose, and that purpose is this user."
fi

# The checkout itself, if not there yet.
if [[ ! -d "$CHECKOUT/.git" ]]; then
  say "Cloning as ${OWNER_USER} (expected to fail on the first run, until the key above is on GitHub)"
  sudo -u "$OWNER_USER" GIT_SSH_COMMAND="ssh -i $OWNER_SSH/id_ed25519 -o IdentitiesOnly=yes -o UserKnownHostsFile=$OWNER_SSH/known_hosts" \
    git clone "$GIT_URL" "$CHECKOUT" || {
    echo "Clone failed. Add the deploy key printed above on GitHub (read-only), then rerun this script." >&2
    exit 1
  }
fi
chown -R "$OWNER_USER:$APP_USER" "$CHECKOUT/.git"

# --- 5. systemd -----------------------------------------------------------------------
say "systemd units"
install -m 0644 "$HERE/cme@.service" /etc/systemd/system/cme@.service
install -m 0644 "$HERE/cme-backup@.service" /etc/systemd/system/cme-backup@.service
install -m 0644 "$HERE/cme-backup@.timer" /etc/systemd/system/cme-backup@.timer
echo "d /run/cme 0755 root root -" > /etc/tmpfiles.d/cme.conf
systemd-tmpfiles --create /etc/tmpfiles.d/cme.conf
systemctl daemon-reload
systemctl enable "cme@${ORG}" "cme-backup@${ORG}.timer"
systemctl start "cme-backup@${ORG}.timer"

# --- 6. Caddy -------------------------------------------------------------------------
say "Caddy"
install -d /etc/caddy/sites
if ! grep -q 'import /etc/caddy/sites/\*.caddy' /etc/caddy/Caddyfile 2>/dev/null; then
  cat > /etc/caddy/Caddyfile <<CADDY
# One site file per tenant in /etc/caddy/sites/. Certificates are automatic
# once the hostname resolves to this box. The email is where Let's Encrypt
# sends expiry warnings; written by server-setup.sh from its 4th argument.
{
	email ${ACME_EMAIL}
}
import /etc/caddy/sites/*.caddy
CADDY
fi
sed -e "s|__ORG__|${ORG}|g" -e "s|__HOSTNAME__|${HOSTNAME_FQDN}|g" \
  "$HERE/site.caddy.template" > "/etc/caddy/sites/${ORG}.caddy"
caddy validate --config /etc/caddy/Caddyfile
systemctl enable --now caddy
systemctl reload caddy

say "Done. Next:"
cat <<NEXT
  1. If the clone step failed: add the deploy key on GitHub (read-only), rerun this script,
     then remove any earlier deploy key for another user from GitHub and delete the
     temporary clone you ran this from.
  2. Point DNS: A record  ${HOSTNAME_FQDN}  ->  this instance's static IP.
  3. Deploy:  sudo ${CHECKOUT}/deploy/deploy.sh ${ORG}
  4. Create the first staff login:
       sudo ${CHECKOUT}/deploy/manage.sh ${ORG} createsuperuser
  5. Watch:  journalctl -u cme@${ORG} -f      and      curl -I https://${HOSTNAME_FQDN}/
NEXT
