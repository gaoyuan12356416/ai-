#!/usr/bin/env bash
# Restore only the reporting components installed by this deployment.
set -euo pipefail
root=/mnt/data-disk/social-tt-20261125
if [[ $# != 2 ]]; then echo "usage: rollback.sh ROLE RELEASE_SHA8" >&2; exit 2; fi
role=$1
rev=$2
[[ "$rev" =~ ^[0-9a-f]{8}$ ]]
if [[ "$role" == hk ]]; then root=/data/ai-backend-offload/social-tt-20261125; fi
tool="$root/src-$rev/ops/social-tiktok-20261125/deploy.py"
bundle="$root/$rev"
case "$role" in
  warehouse|kit|daily) python3 "$tool" rollback "$role" "$bundle" ;;
  web)
    scheduler=/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py
    python3 "$scheduler" snapshot --lane youtube --output "$bundle/rollback-scheduler-before.json"
    python3 "$tool" rollback web "$bundle"
    bash /root/drama_material_service/scripts/safe_restart_drama_api.sh
    python3 "$scheduler" restore --baseline "$bundle/rollback-scheduler-before.json" --wait-seconds 25 --output "$bundle/rollback-scheduler-after.json"
    ;;
  hk)
    python3 "$tool" rollback hk "$bundle"
    systemctl restart ai-backend-offload-reader.service
    ;;
  *) exit 2 ;;
esac
