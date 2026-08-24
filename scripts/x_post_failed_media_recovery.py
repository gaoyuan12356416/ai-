#!/usr/bin/env python3
"""Validate or apply one-time repaired-media recovery for exact X schedule runs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts.service import (  # noqa: E402
    FAILED_MEDIA_PREFLIGHT_RECOVERY_REASON,
    XPostError,
    XPostStore,
    redact_text,
)


def _manifest(path: Path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("recovery manifest is unreadable") from exc
    runs = raw.get("runs") if isinstance(raw, dict) else None
    if not isinstance(runs, list) or not runs:
        raise ValueError("recovery manifest runs must be a non-empty list")
    normalized = []
    seen = set()
    for item in runs:
        if not isinstance(item, dict):
            raise ValueError("recovery manifest run must be an object")
        try:
            run_id = int(item.get("run_id"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("recovery manifest run_id is invalid") from exc
        if run_id <= 0 or run_id in seen:
            raise ValueError("recovery manifest run_id is invalid or duplicated")
        queues = item.get("queues")
        if not isinstance(queues, list) or not queues:
            raise ValueError("recovery manifest queues must be a non-empty list")
        seen.add(run_id)
        normalized.append((run_id, queues))
    return normalized


def execute(args):
    store = XPostStore(Path(args.db_path))
    results = []
    for run_id, queues in _manifest(Path(args.manifest)):
        result = store.recover_failed_material_schedule_queues(
            run_id,
            queues,
            reason=FAILED_MEDIA_PREFLIGHT_RECOVERY_REASON,
            actor=args.actor,
            deployed_commit=args.deployed_commit,
            validate_only=not args.apply,
        )
        results.append(result)
    return {
        "status": "applied" if args.apply else "validated",
        "run_count": len(results),
        "queue_count": sum(item["validated_queue_count"] for item in results),
        "updated_count": sum(item["updated_count"] for item in results),
        "runs": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--deployed-commit", required=True)
    parser.add_argument("--actor", default="codex_operator")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply after all store-level invariants pass; default is validate-only.",
    )
    args = parser.parse_args(argv)
    try:
        result = execute(args)
    except (ValueError, XPostError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": str(getattr(exc, "code", "invalid_manifest"))[:64],
                    "message": redact_text(str(exc), 240),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
