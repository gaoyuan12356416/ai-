#!/usr/bin/env bash
set -euo pipefail
cd /root/codex_test
exec /usr/bin/python3 /root/codex_test/fb_2446289_daily_replace_delete.py "$@"
