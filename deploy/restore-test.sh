#!/usr/bin/env bash
# Prove a backup restores. An untested backup is decoration.
#
#   sudo /srv/cme/<org>/deploy/restore-test.sh <org> [dump-file]
#
# Restores the latest dump (or the one given) into a scratch database
# cme_<org>_restoretest, counts rows in the tables that matter, compares the
# counts with the live database, and drops the scratch database. The live
# database is never touched. For the full drill (drop the live database and
# restore into it) see deploy/README.md: that one is done by hand, on purpose.
set -euo pipefail

ORG="${1:?usage: restore-test.sh <org> [dump-file]}"
DB="cme_${ORG}"
SCRATCH="cme_${ORG}_restoretest"
OWNER="cme_${ORG}_owner"
DEST="/var/backups/cme/${ORG}"
DUMP="${2:-$(ls -1t "$DEST"/${DB}-*.dump 2>/dev/null | head -1)}"
[[ -f "$DUMP" ]] || { echo "no dump found in $DEST; run backup.sh first" >&2; exit 1; }

echo "Restoring $DUMP into $SCRATCH"
sudo -u postgres psql -qtAc "DROP DATABASE IF EXISTS \"$SCRATCH\""
sudo -u postgres createdb --owner="$OWNER" --template=template0 --encoding=UTF8 --locale=C.UTF-8 "$SCRATCH"
sudo -u "$OWNER" pg_restore --no-owner --no-privileges --exit-on-error -d "$SCRATCH" "$DUMP"

TABLES="people_person rounds_roundsevent rounds_session attendance_attendancerecord attendance_sessionattendancedecision credits_evaluationsubmission certificates_certificate audit_auditlog"
printf '%-45s %10s %10s\n' table live restored
status=0
for t in $TABLES; do
  live="$(sudo -u "$OWNER" psql -qtAc "SELECT count(*) FROM $t" "$DB")"
  rest="$(sudo -u "$OWNER" psql -qtAc "SELECT count(*) FROM $t" "$SCRATCH")"
  flag=""
  [[ "$live" == "$rest" ]] || { flag="  <-- differs (rows written since the dump?)"; status=1; }
  printf '%-45s %10s %10s%s\n' "$t" "$live" "$rest" "$flag"
done
latest="$(sudo -u "$OWNER" psql -qtAc "SELECT max(created_at) FROM audit_auditlog" "$SCRATCH")"
echo "Newest audit entry in the restored copy: ${latest:-none}"

sudo -u postgres psql -qtAc "DROP DATABASE \"$SCRATCH\""
echo "Scratch database dropped."
exit $status
