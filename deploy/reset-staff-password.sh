#!/usr/bin/env bash
# Reset a staff (admin) login's password for one tenant.
#
#   sudo /srv/cme/<org>/deploy/reset-staff-password.sh <org> <username>
#
# Prompts for the new password twice, in the terminal, through Django's own
# changepassword. The password is never on a command line, in a file, in
# the journal or on screen. Also lists the staff logins if the username is
# unknown, so a typo does not go anywhere.
set -euo pipefail

ORG="${1:?usage: reset-staff-password.sh <org> <username>}"
USERNAME="${2:?usage: reset-staff-password.sh <org> <username>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
[[ -t 0 ]] || { echo "needs a terminal: the new password is typed, never passed in" >&2; exit 1; }

known="$("$HERE/manage.sh" "$ORG" shell -c "
from django.contrib.auth import get_user_model
print(chr(10).join(sorted(u.username for u in get_user_model().objects.filter(is_staff=True))))" 2>/dev/null)"
if ! grep -qx -- "$USERNAME" <<<"$known"; then
  echo "No staff login called '$USERNAME' in tenant $ORG. Staff logins are:" >&2
  sed 's/^/  /' <<<"$known" >&2
  exit 1
fi
exec "$HERE/manage.sh" "$ORG" changepassword "$USERNAME"
