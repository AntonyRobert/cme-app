#!/usr/bin/env bash
# Nightly backup of one tenant, off the box. Run by cme-backup@<org>.timer as root.
#
#   backup.sh <org>
#
# Two things are backed up, and they matter equally: the database (pg_dump,
# custom format) and /srv/cme/<org>/uploads/, the raw Teams exports that are
# the evidence behind credit claims and exist nowhere else. Both go to the
# tenant's restic repository in S3 (RESTIC_REPOSITORY, one path per tenant,
# its own password and IAM user). A local dump is kept too, 14 days, which
# protects against a bad migration or a fat-fingered DELETE and nothing else:
# S3 is the copy that matters.
#
# Fails loudly: any non-zero step fails the unit, and the unit's OnFailure=
# logs at error priority. A backup that silently stops is worse than none.
#
# Environment: the unit loads /etc/cme/<org>.backup.env (RESTIC_REPOSITORY,
# RESTIC_PASSWORD, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY). Nothing here
# prints any of them.
set -euo pipefail

ORG="${1:?usage: backup.sh <org>}"
DB="cme_${ORG}"
OWNER="cme_${ORG}_owner"
UPLOADS="/srv/cme/${ORG}/uploads"
DEST="/var/backups/cme/${ORG}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOCAL_KEEP_DAYS=14

: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY is not set: put the restic variables in /etc/cme/${ORG}.backup.env}"
: "${RESTIC_PASSWORD:?RESTIC_PASSWORD is not set}"
: "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID is not set}"
: "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY is not set}"
export RESTIC_REPOSITORY RESTIC_PASSWORD AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY

say() { printf '==> %s\n' "$*"; }

install -d -m 0750 "$DEST"
DUMP="$DEST/${DB}-${STAMP}.dump"

say "pg_dump ${DB}"
# As the owner role over peer auth: no password anywhere. Custom format
# restores selectively and compresses.
sudo -u "$OWNER" pg_dump --format=custom --no-owner --no-privileges "$DB" > "$DUMP"
chmod 0640 "$DUMP"
[[ -s "$DUMP" ]] || { echo "pg_dump produced an empty file" >&2; exit 1; }

say "restic backup: the dump and ${UPLOADS}"
# One snapshot per night holding both. --tag names the tenant; --host is
# fixed so snapshots stay comparable if the box is rebuilt under a new name.
restic backup --quiet --tag "$ORG" --host "cme-${ORG}" "$DUMP" "$UPLOADS"

say "restic forget --prune (7 daily, 5 weekly, 12 monthly)"
restic forget --quiet --tag "$ORG" --keep-daily 7 --keep-weekly 5 --keep-monthly 12 --prune

say "restic check"
# Structural check of the repository every night; a 5% read of the data
# once a week, so a bit-rot or a bad upload is noticed within weeks, not years.
if [[ "$(date -u +%u)" == "7" ]]; then
  restic check --quiet --read-data-subset=5%
else
  restic check --quiet
fi

say "local copies: keeping ${LOCAL_KEEP_DAYS} days in ${DEST}"
find "$DEST" -type f -name "${DB}-*.dump" -mtime "+${LOCAL_KEEP_DAYS}" -delete

say "latest snapshot"
restic snapshots --tag "$ORG" --latest 1 --compact
echo "backup ${ORG} ${STAMP}: OK"
