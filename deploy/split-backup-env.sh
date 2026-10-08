#!/usr/bin/env bash
# Move the backup credentials out of the web process's environment.
#
#   sudo /srv/cme/<org>/deploy/split-backup-env.sh <org>
#
# /etc/cme/<org>.env is handed to gunicorn (EnvironmentFile=) and to migrate
# (deploy.sh). The restic repository password and the AWS keys that can delete
# the backups have no business there; only cme-backup@<org> needs them. This
# extracts the four lines into /etc/cme/<org>.backup.env, verifies all four
# landed, and only then removes them from the main file. If the count is
# wrong at any point it changes nothing and says so. Prints no values.
set -euo pipefail

ORG="${1:?usage: split-backup-env.sh <org>}"
MAIN="/etc/cme/${ORG}.env"
BACKUP="/etc/cme/${ORG}.backup.env"
KEYS=(RESTIC_REPOSITORY RESTIC_PASSWORD AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY)

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
[[ -f "$MAIN" ]] || { echo "no $MAIN" >&2; exit 1; }

count_keys() {  # how many of the four keys have a non-empty value in a file
  local file="$1" n=0 key
  for key in "${KEYS[@]}"; do
    grep -qE "^${key}=.+" "$file" 2>/dev/null && n=$((n + 1))
  done
  echo "$n"
}

in_main="$(count_keys "$MAIN")"
in_backup="$([[ -f "$BACKUP" ]] && count_keys "$BACKUP" || echo 0)"

if [[ "$in_backup" -eq 4 && "$in_main" -eq 0 ]]; then
  echo "Already split: $BACKUP has all four, $MAIN has none. Nothing to do."
  exit 0
fi
if [[ "$in_main" -ne 4 ]]; then
  echo "Refusing: $MAIN has $in_main of the four backup variables set (need exactly 4, non-empty)." >&2
  echo "Nothing changed. Expected: ${KEYS[*]}" >&2
  exit 1
fi
if [[ -f "$BACKUP" && "$in_backup" -gt 0 ]]; then
  echo "Refusing: $BACKUP already exists with $in_backup of the variables; resolve that by hand first. Nothing changed." >&2
  exit 1
fi

# Extract to a temp file beside the target, verify, then move into place.
TMP="$(mktemp "${BACKUP}.XXXXXX")"
trap 'rm -f "$TMP"' EXIT
{
  echo "# /etc/cme/${ORG}.backup.env  read ONLY by cme-backup@${ORG}. Split out of ${ORG}.env by deploy/split-backup-env.sh."
  echo "# RESTIC_PASSWORD must also exist somewhere other than this server; without it the backups are unrecoverable."
  for key in "${KEYS[@]}"; do grep -E "^${key}=" "$MAIN" | head -1; done
} > "$TMP"
chmod 0600 "$TMP"
chown root:root "$TMP"
if [[ "$(count_keys "$TMP")" -ne 4 ]]; then
  echo "Refusing: the extracted file does not hold all four values. Nothing changed." >&2
  exit 1
fi
mv "$TMP" "$BACKUP"
trap - EXIT

# Only now remove them from the main file, via a verified temp copy as well.
TMP_MAIN="$(mktemp "${MAIN}.XXXXXX")"
grep -vE "^(RESTIC_REPOSITORY|RESTIC_PASSWORD|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY)=" "$MAIN" > "$TMP_MAIN"
chmod 0600 "$TMP_MAIN"; chown root:root "$TMP_MAIN"
if [[ "$(count_keys "$TMP_MAIN")" -ne 0 || "$(count_keys "$BACKUP")" -ne 4 ]]; then
  rm -f "$TMP_MAIN"
  echo "Refusing at the last check; $MAIN left as it was, $BACKUP written." >&2
  exit 1
fi
mv "$TMP_MAIN" "$MAIN"

echo "Moved 4 variables: $MAIN -> $BACKUP (0600 root)."
if systemctl is-active --quiet "cme@${ORG}"; then
  systemctl restart "cme@${ORG}"
  echo "Restarted cme@${ORG} so gunicorn no longer carries them."
fi
echo "Reminder: RESTIC_PASSWORD must be in the password manager too."
