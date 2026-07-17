#!/usr/bin/env python3
"""One idempotent account-timezone scheduling tick for ad-control V3."""

from __future__ import annotations

import json
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from features.ad_control_v3 import AdControlV3Error, get_service  # noqa: E402
from features.ad_control_v3.scheduler import runner_event_key  # noqa: E402


def _enabled(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _time_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def main() -> int:
    if not _enabled("AD_CONTROL_V3_RUNNER_ENABLED"):
        print(json.dumps({"ok": True, "status": "disabled", "meta_writes": 0}, ensure_ascii=False))
        return 0
    if not _enabled("AD_CONTROL_V3_RUNNER_OBSERVE_RELEASED"):
        print(json.dumps({"ok": False, "status": "blocked", "error": "runner_observe_not_released", "meta_writes": 0}, ensure_ascii=False))
        return 3
    try:
        service = get_service()
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        lease_owner = "%s:%s" % (socket.gethostname(), os.getpid())
        timezone_values = list(service.timezone_loader() or [])
        totals = {"groups": 0, "claimed": 0, "executions": 0, "meta_writes": 0, "failed": 0}
        results = []
        for group in service.repository.list_enabled_rule_groups(limit=200):
            totals["groups"] += 1
            if not service.scheduled_group_due_now(group, now, timezone_values):
                continue
            event_key = runner_event_key(str(group.get("group_id") or ""), now)
            now_text = _time_text(now)
            claimed = service.repository.claim_runner_event(
                {
                    "event_key": event_key,
                    "rule_group_id": group["group_id"],
                    "scheduled_for": now_text,
                    "lease_owner": lease_owner,
                    "lease_expires_at": _time_text(now + timedelta(minutes=10)),
                    "created_at": now_text,
                    "updated_at": now_text,
                }
            )
            if not claimed:
                continue
            totals["claimed"] += 1
            try:
                result = service.run_scheduled_group(group["group_id"], now=now)
                totals["meta_writes"] += int(result.get("meta_write_count") or 0)
                if result.get("execution_id"):
                    totals["executions"] += 1
                service.repository.finish_runner_event(
                    event_key,
                    {
                        "status": "completed",
                        "execution_id": str(result.get("execution_id") or ""),
                        "error_code": "",
                        "error_message": "",
                        "updated_at": _time_text(datetime.now(timezone.utc)),
                    },
                )
                results.append({"group_id": group["group_id"], **result})
            except AdControlV3Error as exc:
                totals["failed"] += 1
                service.repository.finish_runner_event(
                    event_key,
                    {
                        "status": "failed",
                        "execution_id": "",
                        "error_code": exc.code,
                        "error_message": exc.message[:1000],
                        "updated_at": _time_text(datetime.now(timezone.utc)),
                    },
                )
                results.append({"group_id": group["group_id"], "status": "failed", "error": exc.code})
        print(json.dumps({"ok": totals["failed"] == 0, "status": "completed", **totals, "results": results[:50]}, ensure_ascii=False))
        return 0 if totals["failed"] == 0 else 2
    except AdControlV3Error as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False))
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "status": "failed", "error": "runner_failed", "message": str(exc)[:500]}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
