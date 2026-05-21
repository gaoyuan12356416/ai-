#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/root/drama_material_service}"
LOG_DIR="${LOG_DIR:-$APP_DIR/logs}"
RUNNER="$APP_DIR/scripts/run_auto_cover_synthesis.sh"
CRON_LINE="0 10,22 * * * $RUNNER >> $LOG_DIR/auto_cover_synthesis.log 2>&1"

mkdir -p "$LOG_DIR"
TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT

crontab -l 2>/dev/null | grep -v "$RUNNER" > "$TMP_FILE" || true
echo "$CRON_LINE" >> "$TMP_FILE"
crontab "$TMP_FILE"
echo "installed cron: $CRON_LINE"
