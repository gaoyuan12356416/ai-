#!/usr/bin/env python3
"""Independent V3 runner release gate.

The UI/manual observe path is delivered first. Scheduled execution intentionally
remains fail-closed until account-timezone scheduling and natural-day observe
Canary are approved. This script never imports app.py or any V2 module.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from features.ad_control_v3 import AdControlV3Error, get_service  # noqa: E402


def _enabled(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    if not _enabled("AD_CONTROL_V3_RUNNER_ENABLED"):
        print(json.dumps({"ok": True, "status": "disabled", "meta_writes": 0}, ensure_ascii=False))
        return 0
    # A separate release flag prevents an operator from accidentally starting
    # scheduled scans merely by installing/enabling the unit file.
    if not _enabled("AD_CONTROL_V3_RUNNER_OBSERVE_RELEASED"):
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "blocked",
                    "error": "runner_observe_not_released",
                    "meta_writes": 0,
                },
                ensure_ascii=False,
            )
        )
        return 3
    try:
        # Dependency/data-root health validation is lazy and performs no Meta
        # request. Actual due-event scheduling is intentionally not connected.
        get_service()
        raise AdControlV3Error(
            "runner_scheduler_not_configured",
            "account-timezone scheduler has not passed observe Canary",
            status=503,
        )
    except AdControlV3Error as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
