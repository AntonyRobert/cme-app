#!/usr/bin/env bash
# Run manage.py for a tenant with its production environment, as the owner user.
#
#   sudo /srv/cme/<org>/deploy/manage.sh <org> createsuperuser
#   sudo /srv/cme/<org>/deploy/manage.sh <org> shell
set -euo pipefail
ORG="${1:?usage: manage.sh <org> <manage.py args...>}"
shift
ENV_FILE="/etc/cme/${ORG}.env"
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
KEYS="$(grep -oE '^[A-Z_]+' "$ENV_FILE" | paste -sd, -)"
cd "/srv/cme/${ORG}"
exec sudo --preserve-env="$KEYS" -u "cme_${ORG}_owner" -H .venv/bin/python manage.py "$@"
