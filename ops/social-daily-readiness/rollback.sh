#!/usr/bin/env bash
# Installed beside the verified previous release pointer and scoped unit backups.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
root=/etc/systemd/system
pointer=/opt/youtube-publisher-daily-report/current
previous=$(cat before/current)
test -d "$previous"
test "$(readlink -f "$pointer")" = "$(realpath release)"
(cd "$root" && sha256sum --check --quiet "$OLDPWD/installed-units.sha256")
for service in youtube-publisher-daily-report.service social-daily-prepare.service; do
    test "$(systemctl show "$service" -p MainPID --value)" = 0 || {
        echo "Wait for the running report to finish: $service" >&2
        exit 1
    }
done
while IFS= read -r file; do
    if test -f "before/units/$file"; then
        install -m 0644 "before/units/$file" "$root/$file"
    else
        rm -f -- "$root/$file"
    fi
done < unit-paths
ln -s "$previous" "$pointer.rollback.$$"
mv -Tf "$pointer.rollback.$$" "$pointer"
systemctl daemon-reload
for timer in youtube-publisher-daily-report.timer social-daily-prepare.timer; do
    if grep -qx enabled "before/$timer.enabled"; then systemctl enable "$timer"; else systemctl disable "$timer"; fi
    if grep -qx active "before/$timer.active"; then systemctl restart "$timer"; else systemctl stop "$timer"; fi
done
