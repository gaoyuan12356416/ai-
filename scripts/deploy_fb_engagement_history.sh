#!/usr/bin/env bash
# Install the independent historic collector alongside the existing read-only report.
set -euo pipefail
action=${1:?stage, apply or rollback}
release=${2:?release directory}
base=/mnt/data-disk/social-material-channels-20261126
[[ "$(realpath -m "$release")" == "$base"/history-* ]]
[[ "$(findmnt -rn -o UUID --mountpoint /mnt/data-disk)" == 3e8ac4e8-7770-456d-9e89-2ec5dd405fa8 ]]
report=/opt/social-material-effects/current
link=/opt/social-material-effects-collector/current
units=/etc/systemd/system
drop="$units/social-material-effects-refresh.service.d/20-fb-history.conf"
timer=social-material-effects-backfill.timer
service=social-material-effects-backfill.service
files=("$units/$service" "$units/$timer" "$drop")
unit_sources=("$service" "$timer" social-material-effects-refresh-collector.conf)
quiet_wait() {
    while [[ "$(systemctl show -p ActiveState --value "$1")" =~ ^(active|activating|deactivating|reloading)$ ]]; do sleep 2; done
}
case "$action" in
stage)
    src=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
    rev=$(git -C "$src" rev-parse HEAD)
    [[ -z "$(git -C "$src" status --porcelain)" && -n "$(git -C "$src" branch -r --contains "$rev")" ]]
    git -C "$src" show 068a9f716b1d1c132449cc4a2bea13a355f15b44:features/social_material_effects/graph.py | cmp - "$report/features/social_material_effects/graph.py"
    mkdir "$release"
    mkdir "$release/units"
    cp -a "$report/." "$release/runtime"
    install -m 644 "$src/features/social_material_effects/graph.py" "$src/features/social_material_effects/backfill.py" "$release/runtime/features/social_material_effects/"
    install -m 755 "$0" "$release/deploy.sh"
    for i in "${!files[@]}"; do
        if [[ -e "${files[i]}" || -L "${files[i]}" ]]; then cp -a "${files[i]}" "$release/units/$i.before"; fi
        install -m 644 "$src/deploy/${unit_sources[i]}" "$release/units/$i.after"
    done
    if [[ -L "$link" ]]; then readlink -f "$link" > "$release/previous-link"; else [[ ! -e "$link" ]]; fi
    systemctl is-enabled "$timer" > "$release/timer-before" 2>/dev/null || true
    systemctl is-active "$timer" > "$release/timer-state-before" 2>/dev/null || true
    echo "$rev" > "$release/commit"
    ln /mnt/data-disk/social-material-effects/facebook.sqlite3 "$release/facebook-at-start.sqlite3"
    python3 - "$release" <<'PY'
from contextlib import closing
from pathlib import Path
import sqlite3,sys
p=Path('/mnt/data-disk/social-material-effects/facebook-observations.sqlite3')
with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(Path(sys.argv[1])/'observations-at-start.sqlite3')) as dest:
    src.backup(dest)
PY
    (cd "$release/runtime" && /usr/bin/python3 -B -m features.social_material_effects.backfill --help >/dev/null)
    ;;
apply)
    for i in "${!files[@]}"; do
        if [[ -e "$release/units/$i.before" ]]; then cmp "${files[i]}" "$release/units/$i.before"; else [[ ! -e "${files[i]}" && ! -L "${files[i]}" ]]; fi
    done
    if [[ -s "$release/previous-link" ]]; then [[ "$(readlink -f "$link")" == "$(cat "$release/previous-link")" ]]; else [[ ! -e "$link" && ! -L "$link" ]]; fi
    mkdir -p "$(dirname "$link")" "$(dirname "$drop")"
    ln -s "$release/runtime" "$link.next"
    mv -T "$link.next" "$link"
    for i in "${!files[@]}"; do install -m 644 "$release/units/$i.after" "${files[i]}"; done
    systemctl daemon-reload
    # Any older hourly writer started before the override finishes normally.
    quiet_wait social-material-effects-refresh.service
    systemctl enable --now "$timer"
    ;;
rollback)
    [[ "$(readlink -f "$link")" == "$release/runtime" ]]
    for i in "${!files[@]}"; do cmp "${files[i]}" "$release/units/$i.after"; done
    systemctl disable --now "$timer"
    quiet_wait "$service"
    for i in "${!files[@]}"; do
        if [[ -e "$release/units/$i.before" ]]; then cp -a "$release/units/$i.before" "${files[i]}"; else rm -f "${files[i]}"; fi
    done
    if [[ -s "$release/previous-link" ]]; then ln -s "$(cat "$release/previous-link")" "$link.next"; mv -T "$link.next" "$link"; else rm "$link"; fi
    systemctl daemon-reload
    if [[ "$(cat "$release/timer-before")" == enabled ]]; then systemctl enable "$timer"; fi
    if [[ "$(cat "$release/timer-state-before")" == active ]]; then systemctl start "$timer"; fi
    ;;
*) exit 2 ;;
esac
