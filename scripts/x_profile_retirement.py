#!/usr/bin/env python3
"""Plan/apply the explicitly authorized ordinary-to-profile X retirement."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from features.x_tag_publish.retirement import make_plan, apply_plan, process_locks


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("dry-run", "apply"))
    p.add_argument("--ordinary-post")
    p.add_argument("--ordinary-auto")
    p.add_argument("--profile-post")
    p.add_argument("--profile-auto")
    p.add_argument("--manifest", required=True)
    p.add_argument("--reviewed-no-post-queue-id", type=int, action="append", default=[])
    p.add_argument("--backup-directory")
    p.add_argument("--lock-path", action="append", default=[],
                   help="optional existing operator coordination lock; repeatable")
    a = p.parse_args(argv)
    if a.mode == "dry-run":
        paths = {k: getattr(a, k) for k in ("ordinary_post", "ordinary_auto", "profile_post", "profile_auto")}
        if not all(paths.values()):
            p.error("dry-run requires all four database paths")
        plan = make_plan(paths, reviewed_no_post_queue_ids=a.reviewed_no_post_queue_id)
        dest = Path(a.manifest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("x", encoding="utf-8") as f:
            os.chmod(dest, 0o600)
            json.dump(plan, f, ensure_ascii=False, indent=2)
        result = {"plan_sha256": plan["plan_sha256"], "changes": len(plan["changes"]),
                  "migrate": len(plan["migrate"]), "excluded": len(plan["excluded"]), "held": plan["held"]}
    else:
        if not a.backup_directory:
            p.error("apply requires backup directory")
        if a.reviewed_no_post_queue_id:
            p.error("reviewed IDs must be frozen by dry-run, never added during apply")
        plan = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
        with process_locks(a.lock_path):
            result = apply_plan(plan, backup_directory=a.backup_directory)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
