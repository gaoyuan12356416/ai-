#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/root/drama_material_service}"
DB_PATH="${DRAMA_JOB_DB_PATH:-$APP_DIR/data/drama_material_jobs.sqlite3}"
ENV_FILE="$APP_DIR/.env"

worker_mode="${DRAMA_JOB_USE_WORKER:-}"
if [ -z "$worker_mode" ] && [ -f "$ENV_FILE" ]; then
  worker_mode="$(grep -E '^DRAMA_JOB_USE_WORKER=' "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true)"
fi
worker_mode="$(printf '%s' "${worker_mode:-0}" | tr '[:upper:]' '[:lower:]')"

running_count="$(python3 - "$DB_PATH" <<'PY'
import sqlite3
import sys

db_path = sys.argv[1]
try:
    con = sqlite3.connect(db_path)
    count = con.execute(
        "select count(*) from drama_material_job where status not in ('done','failed')"
    ).fetchone()[0]
    con.close()
except Exception:
    count = -1
print(count)
PY
)"

if [ "$running_count" != "0" ] && [ "$worker_mode" != "1" ] && [ "$worker_mode" != "true" ] && [ "$worker_mode" != "yes" ] && [ "$worker_mode" != "on" ]; then
  echo "Refusing to restart drama-material-api.service: $running_count in-flight drama job(s) and DRAMA_JOB_USE_WORKER is not enabled." >&2
  exit 2
fi

if systemctl list-unit-files drama-material-job-worker.service --no-pager --no-legend >/dev/null 2>&1; then
  systemctl is-active --quiet drama-material-job-worker.service || {
    echo "Warning: drama-material-job-worker.service is not active." >&2
  }
fi

systemctl restart drama-material-api.service
systemctl status drama-material-api.service --no-pager
