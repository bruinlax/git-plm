#!/bin/sh
# plm — load a sample project into a running stock docker-compose.yml instance.
#
#   scripts/load-example.sh [name]        (default: camper-ipad-dock)
#
# The sample is a plm.carry bundle under examples/<name>/ (DEPLOY.md § Run it
# anywhere). It is imported straight into ./data through a one-off container of
# the same image, as the admin named in .env (PLM_ADMIN). Refuses, writing
# nothing, if the project is already there — so it is safe to re-run.
set -eu
cd "$(dirname "$0")/.."

name="${1:-camper-ipad-dock}"
[ -f "examples/$name/carry.json" ] || { echo "no sample at examples/$name" >&2; exit 2; }
# Import verification opens rollback-only write transactions. Keep the running
# services from writing concurrently, and restore only services active on entry.
running=$(docker compose ps --status running --services | grep -E '^(plm|plm-mcp|plm-git)$' || true)
restore_services() {
  if [ -n "$running" ]; then docker compose start $running; fi
}
trap restore_services EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [ -n "$running" ]; then docker compose stop $running; fi
admin=$(grep -s '^PLM_ADMIN=' .env | cut -d= -f2-)
docker compose run --rm --no-deps -T -v "$PWD/examples:/examples:ro" plm \
  python -m plm.carry import --bundle "/examples/$name" \
    --into /var/plm/db/plm.sqlite3 --repos /var/plm/repos --lfs /var/plm/lfs \
    --mode write --stub-external --actor "${admin:-admin}"
