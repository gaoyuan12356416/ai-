#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/root/drama_material_service}"
LOG_DIR="${LOG_DIR:-$APP_DIR/logs}"
LOCK_FILE="${LOCK_FILE:-$LOG_DIR/auto_cover_synthesis.lock}"

mkdir -p "$LOG_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date '+%F %T') auto_cover_synthesis already running; skip"
  exit 0
fi

cd "$APP_DIR"
exec /usr/bin/python3 "$APP_DIR/scripts/auto_submit_cover_synthesis.py" "$@"
