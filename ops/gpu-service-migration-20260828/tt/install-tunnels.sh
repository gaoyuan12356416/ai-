#!/usr/bin/env bash
# Install only. Does not enable/start SSH or modify authorized_keys.
set -euo pipefail
umask 077
[[ $# -eq 2 ]] || { printf '%s\n' 'usage: install-tunnels.sh OPS_COMMIT RUN_ID' >&2; exit 2; }
ops_commit="$1"
run_id="$2"
[[ "$ops_commit" =~ ^[0-9a-f]{40}$ && "$run_id" =~ ^gpu-service-migration-[0-9]{8}T[0-9]{4}$ ]] || exit 2
script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
[[ "$script_dir" = "/data/migrations/$run_id/checkouts/$ops_commit/ops/gpu-service-migration-20260828/tt" ]] || exit 2
python_bin=/data/tt-post-gpu/runtime/bin/python
"$python_bin" "$script_dir/tt_migration.py" preflight
"$python_bin" "$script_dir/tt_migration.py" verify-source
for unit in tt-gpu-publisher.service tt-gpu-direct-outro.service tt-gpu-reverse-tunnel.service tt-gpu-direct-outro-reverse-tunnel.service; do
  state="$(systemctl show "$unit" --property=ActiveState --value)"
  [[ "$state" = inactive || "$state" = failed || -z "$state" ]] || exit 3
done
key=/etc/x-post-media-repair-tunnel/id_ed25519_cpu_tunnel
known=/etc/x-post-media-repair-tunnel/known_hosts
[[ -f "$key" && ! -L "$key" && -s "$known" ]] || exit 4
[[ "$(stat -c %a "$key")" = 600 || "$(stat -c %a "$key")" = 400 ]] || exit 4
backup="/data/migrations/$run_id/tt/target-tunnels-before"
mkdir -m 0700 "$backup"
for unit in tt-gpu-reverse-tunnel.service tt-gpu-direct-outro-reverse-tunnel.service; do
  destination="/etc/systemd/system/$unit"
  if [[ -e "$destination" || -L "$destination" ]]; then
    cp -a -- "$destination" "$backup/$unit"
  else
    : > "$backup/$unit.absent"
  fi
done
for unit in tt-gpu-reverse-tunnel.service tt-gpu-direct-outro-reverse-tunnel.service; do
  install -m 0644 "$script_dir/units/$unit" "/etc/systemd/system/$unit"
done
install -m 0644 "$script_dir/gate_handoff.py" /data/tt-post-gpu/ops/gate_handoff.py
systemd-analyze verify /etc/systemd/system/tt-gpu-reverse-tunnel.service /etc/systemd/system/tt-gpu-direct-outro-reverse-tunnel.service
printf '%s\n' "$ops_commit" > "$backup/operations-commit"
touch "$backup/complete"
systemctl daemon-reload
printf '%s\n' 'Installed only. No service enabled/started. No gate or SSH authorization changed.'
