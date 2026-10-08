#!/usr/bin/env bash
# The full drill: replace the LIVE database and uploads from the latest S3
# snapshot. Destructive by design; this is what December looks like.
#
#   sudo /srv/cme/<org>/deploy/restore-drill.sh <org> --replace-live-data [snapshot-id]
#
# Without the flag it only says what it would do. Expect about a minute of
# downtime. Steps, each printed as it happens:
#   1. stop gunicorn
#   2. restic restore <snapshot> into a scratch directory
#   3. drop and recreate the database, pg_restore the dump into it
#   4. re-apply grants.sql (a --no-privileges restore leaves the app role with nothing)
#   5. put the restored uploads in place
#   6. start gunicorn, check it answers over the socket
set -euo pipefail

ORG="${1:?usage: restore-drill.sh <org> --replace-live-data [snapshot-id]}"
FLAG="${2:-}"
SNAPSHOT="${3:-latest}"
DB="cme_${ORG}"
OWNER="cme_${ORG}_owner"
APP="cme_${ORG}"
CHECKOUT="/srv/cme/${ORG}"
UPLOADS="${CHECKOUT}/uploads"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
if [[ "$FLAG" != "--replace-live-data" ]]; then
  cat <<DRY
This would: stop cme@${ORG}; restore snapshot ${SNAPSHOT} from S3; DROP DATABASE ${DB} and
recreate it from the restored dump; re-apply grants; replace ${UPLOADS}/ with the restored
copy; start cme@${ORG}. Nothing was done. Rerun with --replace-live-data to do it.
DRY
  exit 0
fi

# Load the backup environment ourselves: one line for the operator.
BACKUP_ENV="/etc/cme/${ORG}.backup.env"
set -a
# shellcheck disable=SC1090
if [[ -f "$BACKUP_ENV" ]]; then source "$BACKUP_ENV"; else source "/etc/cme/${ORG}.env"; fi
set +a
: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY not set; see deploy/env.backup.template}"
export RESTIC_CACHE_DIR="/var/cache/restic/${ORG}"

WORK="$(mktemp -d /var/tmp/cme-drill-${ORG}.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
say() { printf '\n==> %s\n' "$*"; }

say "1. Stopping cme@${ORG}"
systemctl stop "cme@${ORG}"

say "2. Restoring snapshot ${SNAPSHOT} from S3"
restic snapshots --tag "$ORG" --compact | tail -3
restic restore --quiet --tag "$ORG" "$SNAPSHOT" --target "$WORK"
DUMP="$(find "$WORK" -type f -name "${DB}-*.dump" | sort | tail -1)"
[[ -f "$DUMP" ]] || { echo "no ${DB}-*.dump in the snapshot; gunicorn is STOPPED, start it with: systemctl start cme@${ORG}" >&2; exit 1; }
RESTORED_UPLOADS="$(find "$WORK" -type d -path "*/srv/cme/${ORG}/uploads" | head -1)"
chmod -R a+rX "$WORK"
echo "dump: $(basename "$DUMP")"

say "3. Replacing database ${DB}"
sudo -u postgres psql -qtAc "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${DB}' AND pid <> pg_backend_pid()" >/dev/null
sudo -u postgres psql -qtAc "DROP DATABASE IF EXISTS \"${DB}\""
sudo -u postgres createdb --owner="$OWNER" --template=template0 --encoding=UTF8 --locale=C.UTF-8 "$DB"
sudo -u "$OWNER" pg_restore --no-owner --no-privileges --exit-on-error -d "$DB" "$DUMP"

say "4. Grants for the app role"
sudo -u "$OWNER" psql -v ON_ERROR_STOP=1 -v app="$APP" -d "$DB" -f "$HERE/grants.sql" >/dev/null

say "5. Uploads"
if [[ -d "$RESTORED_UPLOADS" ]]; then
  rsync -a --delete "$RESTORED_UPLOADS/" "$UPLOADS/"
  chown -R "$APP:$APP" "$UPLOADS"
  echo "files in place: $(find "$UPLOADS" -type f | wc -l)"
else
  echo "no uploads directory in the snapshot (nothing had been uploaded); ${UPLOADS} left as is"
fi

say "6. Starting cme@${ORG}"
systemctl start "cme@${ORG}"
sleep 2
systemctl is-active --quiet "cme@${ORG}" || { echo "cme@${ORG} did not start: journalctl -u cme@${ORG}" >&2; exit 1; }
HOST="$(grep -E '^DJANGO_ALLOWED_HOSTS=' "/etc/cme/${ORG}.env" | cut -d= -f2 | cut -d, -f1)"
code="$(curl -s -o /dev/null -w '%{http_code}' --unix-socket "/run/cme/${ORG}/gunicorn.sock" \
  -H "Host: ${HOST}" -H "X-Forwarded-Proto: https" http://localhost/admin/login/)"
[[ "$code" == "200" ]] || { echo "expected 200 from /admin/login/, got $code" >&2; exit 1; }
people="$(sudo -u "$OWNER" psql -qtAc "SELECT count(*) FROM people_person" "$DB")"
audit="$(sudo -u "$OWNER" psql -qtAc "SELECT count(*) FROM audit_auditlog" "$DB")"
echo "DRILL OK: ${HOST} answers; ${people} people, ${audit} audit entries in the restored database."
