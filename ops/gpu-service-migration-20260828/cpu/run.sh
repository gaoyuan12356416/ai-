#!/bin/bash
set -euo pipefail
slot="$1"
entry="$2"
case "$slot" in screenshot-primary|screenshot-burst|cover) ;; *) exit 64 ;; esac
case "$entry" in /mnt/data-disk/codex-workers/us-migrated/releases/*/*.py) ;; *) exit 64 ;; esac
exec >>"/mnt/data-disk/codex-workers/us-migrated/$slot/service.log" 2>&1
exec /usr/bin/python3 "$entry"
