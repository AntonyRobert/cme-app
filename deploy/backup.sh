#!/usr/bin/env bash
# Nightly backup of one tenant: the database (pg_dump, custom format) and the
# uploads directory, into /var/backups/cme/<org>/, keeping 14 days. Run by
# cme-backup@<org>.timer as root.
#
# This is the local copy. Shipping it off the box (restic to S3, per
# docs/deployment.md) is wired in here once the bucket exists: see OFFSITE.
set -euo pipefail

ORG="${1:?usage: backup.sh <org>}"
DB="cme_${ORG}"
OWNER="cme_${ORG}_owner"
DEST="/var/backups/cme/${ORG}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
KEEP_DAYS=14

install -d -m 0750 "$DEST"
# pg_dump as the owner role over peer auth: no password anywhere. Custom
# format restores selectively and compresses.
sudo -u "$OWNER" pg_dump --format=custom --no-owner --no-privileges "$DB" > "$DEST/${DB}-${STAMP}.dump"
tar -C "/srv/cme/${ORG}" -czf "$DEST/uploads-${STAMP}.tar.gz" uploads
chmod 0640 "$DEST"/*"${STAMP}"*
find "$DEST" -type f -mtime "+${KEEP_DAYS}" -delete

# OFFSITE: when the S3 bucket exists, add here (restic keeps encryption and
# dedup out of our hands):
#   export RESTIC_REPOSITORY=s3:s3.ca-central-1.amazonaws.com/<bucket>/<org>
#   restic backup "$DEST" --tag "$ORG" && restic forget --keep-daily 14 --keep-weekly 8 --keep-monthly 24 --prune
# with RESTIC_PASSWORD and the AWS keys in /etc/cme/<org>.backup.env (0600).

echo "backup ${ORG} ${STAMP}: $(du -sh "$DEST" | cut -f1) in $DEST"
