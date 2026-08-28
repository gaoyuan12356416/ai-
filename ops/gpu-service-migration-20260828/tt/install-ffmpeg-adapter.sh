#!/usr/bin/env bash
# Install the approved private direct-outro adapter, without starting workers.
set -euo pipefail
umask 077
[[ $# -eq 2 ]] || { printf '%s\n' 'usage: install-ffmpeg-adapter.sh OPS_COMMIT RUN_ID' >&2; exit 2; }
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
binary_dir=/data/tt-post-gpu/ffmpeg
expected=c34815e5271aecd549e2334a659eebee62de5c86f763d1f15026b11582f1184d
[[ ! -e "$binary_dir/ffmpeg.bin" && ! -L "$binary_dir/ffmpeg.bin" ]] || exit 4
[[ -f "$binary_dir/ffmpeg" && ! -L "$binary_dir/ffmpeg" ]] || exit 4
[[ "$(sha256sum "$binary_dir/ffmpeg" | cut -d' ' -f1)" = "$expected" ]] || exit 5
backup="/data/migrations/$run_id/tt/ffmpeg-adapter-before"
mkdir -m 0700 "$backup"
cp -a -- "$binary_dir/ffmpeg" "$backup/ffmpeg"
cp -a -- "$binary_dir/ffmpeg" "$binary_dir/ffmpeg.bin"
[[ "$(sha256sum "$binary_dir/ffmpeg.bin" | cut -d' ' -f1)" = "$expected" ]] || exit 5
install -m 0755 "$script_dir/ffmpeg_adapter.py" "$binary_dir/.ffmpeg-launcher"
mv -f -- "$binary_dir/.ffmpeg-launcher" "$binary_dir/ffmpeg"
printf '%s\n' "$ops_commit" > "$backup/operations-commit"
sha256sum "$binary_dir/ffmpeg" "$binary_dir/ffmpeg.bin" "$binary_dir/ffprobe" > "$backup/installed.sha256"
"$binary_dir/ffmpeg" -version > "$backup/version.txt"
printf '%s\n' 'Private adapter installed. All production workers/tunnels remain stopped.'
