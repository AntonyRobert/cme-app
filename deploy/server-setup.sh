#!/usr/bin/env bash
# One-time server setup for one CME tenant. Read it before running it.
#
#   sudo bash server-setup.sh <org> <hostname> <git-ssh-url>
#   e.g.  sudo bash server-setup.sh mcgill cme.mri3.ca git@github.com:AntonyRobert/cme-app.git
#
# Ubuntu 24.04 LTS on Lightsail. Idempotent where it reasonably can be: running
# it twice is safe. It does NOT clone the repo or deploy (that is deploy.sh), and
# it does NOT write secrets: it creates /etc/cme/<org>.env from the template and
# tells you what to fill in.
#
# What it sets up, in order:
#   1. packages: Python 3.13 (deadsnakes), git, Postgres 17 (PGDG repo), Caddy (official repo)
#   2. OS users: cme_<org> (runs gunicorn, owns uploads) and cme_<org>_owner (runs migrate, owns the checkout)
#   3. Postgres: two peer-authenticated roles matching those users, one database owned by the owner role
#   4. directories per docs/deployment.md: /srv/cme/<org>, /etc/cme/<org>.env, /run/cme
#   5. systemd: cme@.service template, cme-backup@.service and .timer, /run/cme via tmpfiles
#   6. Caddy: a site block for the hostname proxying to the tenant's socket, static files served directly
set -euo pipefail

ORG="${1:?usage: server-setup.sh <org> <hostname> <git-ssh-url>}"
HOSTNAME_FQDN="${2:?usage: server-setup.sh <org> <hostname> <git-ssh-url>}"
GIT_URL="${3:?usage: server-setup.sh <org> <hostname> <git-ssh-url>}"

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

# --- 1. Packages ---------------------------------------------------------------------
say "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg lsb-release software-properties-common git \
  build-essential libpq-dev unattended-upgrades

# Python 3.13: Ubuntu 24.04 ships 3.12, and the stack says 3.13 to match local.
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update -q
apt-get install -y -q python3.13 python3.13-venv python3.13-dev

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
install -d -o "$OWNER_USER" -g "$APP_USER" -m 0750 /srv/cme "$CHECKOUT"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$CHECKOUT/uploads"
install -d -o root -g root -m 0755 /etc/cme
install -d -o root -g root -m 0750 "/var/backups/cme/${ORG}"
install -d -m 0755 /run/cme

if [[ ! -f "$ENV_FILE" ]]; then
  SECRET="$(python3.13 -c 'import secrets; print(secrets.token_urlsafe(64))')"
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
  say "Deploy key generated. Add this PUBLIC key to the GitHub repo as a read-only deploy key:"
  cat "$OWNER_SSH/id_ed25519.pub"
fi

# The checkout itself, if not there yet.
if [[ ! -d "$CHECKOUT/.git" ]]; then
  say "Cloning (this fails until the deploy key above is on the repo; rerun then)"
  sudo -u "$OWNER_USER" git clone "$GIT_URL" "$CHECKOUT" || {
    echo "Clone failed. Add the deploy key on GitHub, then rerun this script." >&2
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
  cat > /etc/caddy/Caddyfile <<'CADDY'
# One site file per tenant in /etc/caddy/sites/. Certificates are automatic
# once the hostname resolves to this box.
{
	email ops@mri3.ca
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
  1. If the clone step failed: add the deploy key on GitHub, rerun this script.
  2. Point DNS: A record  ${HOSTNAME_FQDN}  ->  this instance's static IP.
  3. Deploy:  sudo ${CHECKOUT}/deploy/deploy.sh ${ORG}
  4. Create the first staff login:
       sudo ${CHECKOUT}/deploy/manage.sh ${ORG} createsuperuser
  5. Watch:  journalctl -u cme@${ORG} -f      and      curl -I https://${HOSTNAME_FQDN}/
NEXT
