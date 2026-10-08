#!/usr/bin/env bash
# Run tonight's backup now and show how it went.
#
#   sudo /srv/cme/<org>/deploy/backup-now.sh <org>
set -euo pipefail
ORG="${1:?usage: backup-now.sh <org>}"
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
echo "==> starting cme-backup@${ORG} (the same unit the timer runs)"
if systemctl start "cme-backup@${ORG}"; then
  journalctl -u "cme-backup@${ORG}" --no-pager -n 40 --since "-10min"
  echo "==> OK"
else
  journalctl -u "cme-backup@${ORG}" --no-pager -n 60 --since "-10min"
  echo "==> FAILED. The failure unit logged it too:  journalctl -p err -t cme-backup" >&2
  exit 1
fi
