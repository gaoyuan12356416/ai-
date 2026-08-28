#!/usr/bin/env bash
# Configuration rollback only. NEVER restores GPU ledgers or CPU SQLite.
# Coordinator must first drain and stop target, preserving newer facts.
set -euo pipefail
umask 077
[[ $# -eq 1 ]] || { printf '%s\n' 'usage: rollback-target.sh RUN_ID' >&2; exit 2; }
run_id="$1"
[[ "$run_id" =~ ^gpu-service-migration-[0-9]{8}T[0-9]{4}$ ]] || exit 2
backup="/data/migrations/$run_id/tt/target-units-before"
[[ -f "$backup/complete" ]] || exit 3
for unit in tt-gpu-publisher.service tt-gpu-direct-outro.service tt-gpu-reverse-tunnel.service tt-gpu-direct-outro-reverse-tunnel.service; do
  state="$(systemctl show "$unit" --property=ActiveState --value)"
  [[ "$state" = inactive || "$state" = failed || -z "$state" ]] || exit 4
done
tunnels_backup="/data/migrations/$run_id/tt/target-tunnels-before"
[[ ! -d "$tunnels_backup" || -f "$tunnels_backup/complete" ]] || exit 5
/data/tt-post-gpu/runtime/bin/python /data/tt-post-gpu/ops/tt_migration.py restore-config --run-id "$run_id"
for unit in tt-gpu-publisher.service tt-gpu-direct-outro.service; do
  if [[ -e "$backup/$unit" || -L "$backup/$unit" ]]; then
    cp -a -- "$backup/$unit" "/etc/systemd/system/$unit"
  elif [[ -f "$backup/$unit.absent" ]]; then
    rm -f -- "/etc/systemd/system/$unit"
  else
    printf '%s\n' "Missing rollback manifest for $unit" >&2
    exit 5
  fi
done
if [[ -d "$tunnels_backup" ]]; then
  for unit in tt-gpu-reverse-tunnel.service tt-gpu-direct-outro-reverse-tunnel.service; do
    if [[ -e "$tunnels_backup/$unit" || -L "$tunnels_backup/$unit" ]]; then
      cp -a -- "$tunnels_backup/$unit" "/etc/systemd/system/$unit"
    elif [[ -f "$tunnels_backup/$unit.absent" ]]; then
      rm -f -- "/etc/systemd/system/$unit"
    else
      printf '%s\n' "Missing tunnel rollback manifest for $unit" >&2
      exit 5
    fi
  done
fi
systemctl daemon-reload
printf '%s\n' 'Configuration restored. No ledger/database restored. No service started.'
