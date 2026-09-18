#!/usr/bin/env python3
"""Bounded scheduler/executor client; never prints credentials."""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.fb_auto_posts.validation import valid_internal_bearer

ROUTE_TIMEOUT_SECONDS = {
    "/internal/fb-auto-post/tick": 50,
    "/internal/fb-auto-post/plan-next": 3600,
    "/internal/fb-auto-post/prepare-next": 9600,
    "/internal/fb-auto-post/execute-next": 1300,
    "/internal/fb-auto-post/reconcile-next": 1300,
}


def call(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    token = str(os.environ.get("FB_AUTO_POST_INTERNAL_TOKEN", "") or "")
    if not valid_internal_bearer(token):
        raise RuntimeError("FB自动发布内部凭证未配置")
    if path not in {"/internal/fb-auto-post/tick", "/internal/fb-auto-post/plan-next", "/internal/fb-auto-post/prepare-next", "/internal/fb-auto-post/execute-next", "/internal/fb-auto-post/reconcile-next"}:
        raise RuntimeError("FB自动发布内部路由无效")
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timeout = ROUTE_TIMEOUT_SECONDS[path]
    connection = http.client.HTTPConnection("127.0.0.1", 18835, timeout=timeout)
    try:
        connection.request("POST", path, body=raw, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=UTF-8", "Accept": "application/json", "Connection": "close"})
        response = connection.getresponse(); content = response.read(2 * 1024 * 1024 + 1)
    finally:
        connection.close()
    if len(content) > 2 * 1024 * 1024:
        raise RuntimeError("FB自动发布服务响应过大")
    try: decoded = json.loads(content.decode("utf-8"))
    except (UnicodeError, ValueError): raise RuntimeError("FB自动发布服务响应无效") from None
    if not isinstance(decoded, dict): raise RuntimeError("FB自动发布服务响应无效")
    if not 200 <= response.status < 300: raise RuntimeError(str(decoded.get("message") or "FB自动发布服务请求失败"))
    return decoded


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("tick", "plan", "prepare", "execute", "reconcile"))
    parser.add_argument("--worker-id", default="fb-auto-post-runner-primary")
    parser.add_argument("--lease-seconds", type=int, default=1200)
    parser.add_argument("--workers", type=int, default=int(os.environ.get("FB_AUTO_POST_WORKERS", "4")))
    parser.add_argument("--max-tasks", type=int, default=int(os.environ.get("FB_AUTO_POST_MAX_TASKS_PER_RUN", "100")))
    parser.add_argument("--max-seconds", type=int, default=600, help="Stop new claims after this time; always finish in-flight work")
    args = parser.parse_args()
    max_lease = 10800 if args.mode == "prepare" else 3600
    if not re.fullmatch(r"[A-Za-z0-9._:@-]{1,120}", args.worker_id) or not 120 <= args.lease_seconds <= max_lease or not 1 <= args.workers <= 8 or not 1 <= args.max_tasks <= 1000 or not 1 <= args.max_seconds <= 3600:
        raise SystemExit("runner参数无效")
    if args.mode == "tick":
        result = call("/internal/fb-auto-post/tick", {})
    else:
        path = {"plan": "/internal/fb-auto-post/plan-next", "prepare": "/internal/fb-auto-post/prepare-next", "execute": "/internal/fb-auto-post/execute-next", "reconcile": "/internal/fb-auto-post/reconcile-next"}[args.mode]
        terminal = {"plan": "no_due_slot", "prepare": "no_planned", "execute": "no_pending", "reconcile": "no_submitted"}[args.mode]
        completed = drain(lambda lane: call(path, {"worker_id": f"{args.worker_id}-{lane}", "lease_seconds": args.lease_seconds}), workers=args.workers, max_tasks=args.max_tasks, max_seconds=args.max_seconds, terminal=terminal)
        result = {"ok": True, "mode": args.mode, "attempted": len(completed), "items": completed}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def drain(one, *, workers, max_tasks, max_seconds, terminal, monotonic=time.monotonic):
    """Refill each finished lane independently, without interrupting requests."""
    deadline = monotonic() + max_seconds
    completed, issued = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        for lane in range(min(workers, max_tasks)):
            pending[pool.submit(one, lane)] = lane
            issued += 1
        while pending:
            done, _ = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                lane = pending.pop(future)
                item = future.result()
                completed.append(item)
                if item.get("status") != terminal and issued < max_tasks and monotonic() < deadline:
                    pending[pool.submit(one, lane)] = lane
                    issued += 1
    return completed


if __name__ == "__main__":
    raise SystemExit(main())
