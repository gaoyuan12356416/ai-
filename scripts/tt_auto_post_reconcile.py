#!/usr/bin/env python3
"""Bounded CPU readback of existing legacy TT auto publish IDs only."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.tt_auto_post_runner import RunnerConfig, RunnerError, SidecarClient, exclusive_lock

PATH = "/internal/tt-auto-post/reconcile-next"


def reconcile_once(config, *, limit=20, timeout=120, client=None):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
        raise RunnerError("tt_auto_reconcile_limit_invalid", "readback limit must be between 1 and 20")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 15 <= timeout <= 120:
        raise RunnerError("tt_auto_reconcile_timeout_invalid", "readback timeout must be between 15 and 120 seconds")
    client = client or SidecarClient(config)
    cursor, checked = 0, 0
    for _ in range(limit):
        result = client.post(PATH, {"worker_id":config.worker_id, "after_task_id":cursor}, timeout=timeout)
        if not result.get("claimed"):
            break
        task = result.get("task") or {}
        task_id = task.get("id")
        if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= cursor or not task.get("publish_id"):
            raise RunnerError("tt_auto_reconcile_response_invalid", "readback cursor or frozen publish ID is invalid")
        cursor, checked = task_id, checked + 1
    return {"ok": True, "checked": checked, "limit": limit}


def main():
    try:
        config = replace(RunnerConfig.from_env(),
            worker_id=os.environ.get("TT_AUTO_POST_RECONCILE_WORKER_ID", "tt-auto-post-readback"),
            lock_path=os.environ.get("TT_AUTO_POST_RECONCILE_LOCK_PATH", "/run/tt-auto-post/reconcile.lock"),
            worker_count=1, max_tasks_per_worker=1)
        config.validate()
        with exclusive_lock(config.lock_path) as acquired:
            result = reconcile_once(config,
                limit=int(os.environ.get("TT_AUTO_POST_RECONCILE_MAX_TASKS", "20")),
                timeout=int(os.environ.get("TT_AUTO_POST_RECONCILE_TIMEOUT", "120"))) if acquired else {"ok": True, "skipped_locked": True}
        print(json.dumps(result, sort_keys=True))
        return 0
    except RunnerError as exc:
        print(json.dumps({"ok":False,"error_code":exc.code}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"ok":False,"error_code":"tt_auto_reconcile_failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
