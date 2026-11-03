#!/usr/bin/env python3
"""Plan/apply exact X Auto receipt corrections without calling X or publishers."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_auto_posts.receipt_reconciliation import apply_plan, build_plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", type=Path, metavar="PLAN_JSON")
    action.add_argument("--apply", type=Path, metavar="PLAN_JSON")
    parser.add_argument("--auto-db", required=True)
    parser.add_argument("--native-db", required=True)
    parser.add_argument("--task-ids", help="exact comma-separated positive task IDs; required when planning")
    parser.add_argument("--backup-dir", help="new backup directory; required when applying")
    args = parser.parse_args()
    if args.plan:
        if not args.task_ids or args.backup_dir:
            parser.error("planning requires --task-ids and does not take --backup-dir")
        if args.plan.exists():
            parser.error("plan destination already exists")
        plan = build_plan(args.auto_db, args.native_db, args.task_ids.split(","))
        args.plan.parent.mkdir(parents=True, exist_ok=True)
        args.plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"plan": str(args.plan), "plan_sha256": plan["plan_sha256"], "task_ids": plan["task_ids"], "publish_requests": 0}))
    else:
        if not args.backup_dir or args.task_ids:
            parser.error("application requires --backup-dir and uses only the task IDs frozen in the plan")
        plan = json.loads(args.apply.read_text(encoding="utf-8"))
        print(json.dumps(apply_plan(plan, args.auto_db, args.native_db, args.backup_dir), ensure_ascii=False))


if __name__ == "__main__":
    main()
