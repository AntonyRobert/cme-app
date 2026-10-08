#!/usr/bin/env bash
# Prove the S3 backup restores. An untested backup is decoration, and a
# restore test that reads a file on the same disk is not testing the thing
# that would actually fail.
#
#   sudo /srv/cme/<org>/deploy/restore-test.sh <org> [snapshot-id]
#
# Pulls the latest snapshot (or the one given) from the tenant's restic
# repository into a scratch directory, restores the dump it holds into a
# scratch database cme_<org>_restoretest, compares row counts with the live
# database, checks the restored uploads against the live directory, and
# drops the scratch database. The live database and uploads are never
# touched. The full drill (replace the live database from S3) is in the
# README and is done by hand, on purpose.
set -euo pipefail

ORG="${1:?usage: restore-test.sh <org> [snapshot-id]}"
SNAPSHOT="${2:-latest}"
DB="cme_${ORG}"
SCRATCH_DB="cme_${ORG}_restoretest"
OWNER="cme_${ORG}_owner"
WORK="$(mktemp -d /var/tmp/cme-restore-${ORG}.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
# Load the backup environment ourselves. Nothing is printed.
BACKUP_ENV="/etc/cme/${ORG}.backup.env"
set -a
# shellcheck disable=SC1090
if [[ -f "$BACKUP_ENV" ]]; then source "$BACKUP_ENV"; else source "/etc/cme/${ORG}.env"; fi
set +a
: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY is not set: see deploy/env.backup.template}"
: "${RESTIC_PASSWORD:?RESTIC_PASSWORD is not set}"
export RESTIC_REPOSITORY RESTIC_PASSWORD AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
export RESTIC_CACHE_DIR="/var/cache/restic/${ORG}"

say() { printf '==> %s\n' "$*"; }

say "Snapshots in the repository (tag ${ORG})"
restic snapshots --tag "$ORG" --compact

say "Restoring snapshot ${SNAPSHOT} from S3 into ${WORK}"
restic restore --quiet --tag "$ORG" "$SNAPSHOT" --target "$WORK"
DUMP="$(find "$WORK" -type f -name "${DB}-*.dump" | sort | tail -1)"
[[ -f "$DUMP" ]] || { echo "no ${DB}-*.dump in the snapshot" >&2; exit 1; }
RESTORED_UPLOADS="$(find "$WORK" -type d -path "*/srv/cme/${ORG}/uploads" | head -1)"
[[ -d "$RESTORED_UPLOADS" ]] || { echo "no uploads directory in the snapshot" >&2; exit 1; }
chmod -R a+rX "$WORK"  # the owner role must read the dump
echo "dump: $(basename "$DUMP") ($(du -h "$DUMP" | cut -f1))"

say "Restoring the dump into ${SCRATCH_DB} (live database untouched)"
sudo -u postgres psql -qtAc "DROP DATABASE IF EXISTS \"$SCRATCH_DB\""
sudo -u postgres createdb --owner="$OWNER" --template=template0 --encoding=UTF8 --locale=C.UTF-8 "$SCRATCH_DB"
sudo -u "$OWNER" pg_restore --no-owner --no-privileges --exit-on-error -d "$SCRATCH_DB" "$DUMP"

say "Row counts, live versus restored"
TABLES="people_person rounds_roundsevent rounds_session rounds_coideclaration attendance_attendanceupload attendance_attendancerecord attendance_sessionattendancedecision credits_evaluationsubmission certificates_certificate audit_auditlog"
printf '%-45s %10s %10s\n' table live restored
status=0
for t in $TABLES; do
  live="$(sudo -u "$OWNER" psql -qtAc "SELECT count(*) FROM $t" "$DB")"
  rest="$(sudo -u "$OWNER" psql -qtAc "SELECT count(*) FROM $t" "$SCRATCH_DB")"
  flag=""
  [[ "$live" == "$rest" ]] || { flag="  <-- differs (rows written since the snapshot?)"; status=1; }
  printf '%-45s %10s %10s%s\n' "$t" "$live" "$rest" "$flag"
done
latest="$(sudo -u "$OWNER" psql -qtAc "SELECT max(created_at) FROM audit_auditlog" "$SCRATCH_DB")"
echo "Newest audit entry in the restored copy: ${latest:-none}"

say "Uploads, live versus restored"
live_n="$(find "/srv/cme/${ORG}/uploads" -type f | wc -l)"
rest_n="$(find "$RESTORED_UPLOADS" -type f | wc -l)"
echo "files: live ${live_n}, restored ${rest_n}"
if [[ "$live_n" -gt 0 ]]; then
  # Every live file must come back byte-identical.
  (cd "/srv/cme/${ORG}/uploads" && find . -type f -exec sha256sum {} + | sort) > "$WORK/live.sha"
  (cd "$RESTORED_UPLOADS" && find . -type f -exec sha256sum {} + | sort) > "$WORK/rest.sha"
  if diff -q "$WORK/live.sha" "$WORK/rest.sha" >/dev/null; then
    echo "uploads: identical"
  else
    echo "uploads: DIFFER (files added since the snapshot, or a bad restore)"; diff "$WORK/live.sha" "$WORK/rest.sha" | head -20; status=1
  fi
fi

sudo -u postgres psql -qtAc "DROP DATABASE \"$SCRATCH_DB\""
echo "Scratch database dropped; scratch directory removed."
[[ $status -eq 0 ]] && echo "RESTORE TEST OK" || echo "RESTORE TEST: differences above (expected if the box was busy since the snapshot; rerun right after a backup)"
exit $status
