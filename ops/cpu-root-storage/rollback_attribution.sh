#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" != --apply ]; then
  echo 'Revert only attribution temp isolation; cold data and audit remain on data disk.'
  echo 'Run with --apply during an approved attribution maintenance window.'
  exit 0
fi
test "$(hostname)" = VM-0-108-centos
test "$(findmnt -n -o UUID -T /mnt/data-disk)" = 3e8ac4e8-7770-456d-9e89-2ec5dd405fa8
src=$(cd -- "$(dirname -- "$0")" && pwd)
main=/etc/systemd/system/dramawave-attribution-comparison.service.d/90-data-disk-temp.conf
refresh=/etc/systemd/system/dramawave-attribution-comparison-refresh.service.d/90-data-disk-temp.conf
# Never overwrite subsequent operator edits.
cmp "$src/attribution-temp.conf" "$main"
cmp "$src/attribution-refresh-temp.conf" "$refresh"
systemctl stop dramawave-attribution-comparison-refresh.timer
timer_restore() { systemctl start dramawave-attribution-comparison-refresh.timer; }
trap timer_restore EXIT
for attempt in $(seq 1 150); do
  state=$(systemctl show dramawave-attribution-comparison-refresh.service -p ActiveState --value)
  if [ "$state" = inactive ] || [ "$state" = failed ]; then break; fi
  sleep 10
done
state=$(systemctl show dramawave-attribution-comparison-refresh.service -p ActiveState --value)
test "$state" = inactive || test "$state" = failed
# Keep the startup-timeout safety extension, but remove all changed temporary
# storage environment, mount restrictions, and prestart guard. No app/data rollback.
install -m0644 "$src/attribution-rollback-timeout.conf" "$main"
rm -- "$refresh"
systemctl daemon-reload
systemctl restart dramawave-attribution-comparison.service
curl --fail --silent http://127.0.0.1:8832/healthz
echo
