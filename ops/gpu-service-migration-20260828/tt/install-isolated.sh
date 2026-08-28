#!/usr/bin/env bash
# Run only from the root coordinator's verified GitHub operations release.
# Installs CLOSED configuration and units. Never starts/enables services.
set -euo pipefail
umask 077
[[ $# -eq 2 ]] || { printf '%s\n' 'usage: install-isolated.sh OPS_COMMIT RUN_ID' >&2; exit 2; }
ops_commit="$1"
run_id="$2"
[[ "$ops_commit" =~ ^[0-9a-f]{40}$ ]] || exit 2
[[ "$run_id" =~ ^gpu-service-migration-[0-9]{8}T[0-9]{4}$ ]] || exit 2
script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
python_bin=/data/tt-post-gpu/runtime/bin/python
backup="/data/migrations/$run_id/tt/target-units-before"
"$python_bin" "$script_dir/tt_migration.py" preflight
"$python_bin" "$script_dir/tt_migration.py" verify-source
for unit in tt-gpu-publisher.service tt-gpu-direct-outro.service; do
  state="$(systemctl show "$unit" --property=ActiveState --value)"
  [[ "$state" = inactive || "$state" = failed || -z "$state" ]] || {
    printf '%s\n' "Target unit is active; coordinator must drain first: $unit" >&2
    exit 3
  }
done
[[ ! -e "$backup/complete" ]] || {
  printf '%s\n' 'Unit checkpoint exists; inspect instead of overwriting.' >&2
  exit 4
}
mkdir -p "$backup"
for unit in tt-gpu-publisher.service tt-gpu-direct-outro.service; do
  destination="/etc/systemd/system/$unit"
  if [[ -e "$destination" || -L "$destination" ]]; then
    cp -a -- "$destination" "$backup/$unit"
  else
    : > "$backup/$unit.absent"
  fi
done
"$python_bin" "$script_dir/tt_migration.py" configure --run-id "$run_id"
install -m 0644 "$script_dir/tt_migration.py" /data/tt-post-gpu/ops/tt_migration.py
install -m 0644 "$script_dir/verify_offline.py" /data/tt-post-gpu/ops/verify_offline.py
for unit in tt-gpu-publisher.service tt-gpu-direct-outro.service; do
  install -m 0644 "$script_dir/units/$unit" "/etc/systemd/system/$unit"
done
printf '%s\n' "$ops_commit" > "$backup/operations-commit"
touch "$backup/complete"
systemd-analyze verify /etc/systemd/system/tt-gpu-publisher.service /etc/systemd/system/tt-gpu-direct-outro.service
systemctl daemon-reload
printf '%s\n' 'Installed only: live gate OFF, no service start, no enable, no tunnel.'
