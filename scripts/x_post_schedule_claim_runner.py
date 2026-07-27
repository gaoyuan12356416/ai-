#!/usr/bin/env python3
"""Persist due X Post schedule identities without holding the media worker lock."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts.selector import normalize_date, shanghai_now
from scripts.x_post_schedule_runner import (
    ScheduleConfig,
    ScheduleSidecarClient,
)


def execute_claim_tick(config, *, sidecar=None, now=None):
    config.validate()
    current = shanghai_now(now)
    run_date = current.date().isoformat()
    if config.start_date and run_date < normalize_date(
        config.start_date,
        "start_date",
    ):
        return {
            "status": "skipped_before_start_date",
            "run_date": run_date,
            "claimed_or_pending_count": 0,
        }
    client = sidecar or ScheduleSidecarClient(
        config.internal_url,
        config.internal_token,
        timeout=config.internal_timeout,
    )
    items = client.due_schedules(
        config.due_path,
        current=current,
        grace_seconds=config.grace_seconds,
        limit=config.max_due_batches,
    )
    return {
        "status": "claimed",
        "run_date": run_date,
        "claimed_or_pending_count": len(items),
    }


def main():
    try:
        result = execute_claim_tick(ScheduleConfig.from_env())
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": type(exc).__name__,
                    "message": str(exc)[:240],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
