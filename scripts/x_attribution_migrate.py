#!/usr/bin/env python3
"""Explicit offline migration; dry plan is default. Never invokes X."""
import argparse
from contextlib import ExitStack
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_posts.attribution_migration import create_manifest, run
from features.x_posts.attribution_runtime import resolver
from features.x_posts.attribution import AttributionError
from scripts.x_auto_post_runner import exclusive_lock

PUBLISH_LOCK = "/run/x-post-daily/runner.lock"
SCHEDULER_LOCK = "/run/x-auto-post/scheduler.lock"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", required=True)
    p.add_argument("--db", default="/var/lib/x-post-automation/accounts.sqlite3")
    p.add_argument("--auto-db", default="/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3")
    p.add_argument("--users-db", default="/root/drama_material_service/data/drama_material_jobs.sqlite3")
    p.add_argument("--public-root", default="/mnt/data-disk/x-post-automation/s2l")
    p.add_argument("--env-file", default="/root/drama_material_service/.env")
    p.add_argument("--manifest-sha256")
    p.add_argument("--publish-lock", default=PUBLISH_LOCK)
    p.add_argument("--scheduler-lock", default=SCHEDULER_LOCK)
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--rollback", action="store_true")
    a = p.parse_args(argv)
    if not a.publish_lock or not a.scheduler_lock:
        raise AttributionError("migration_lock_path_invalid")
    with ExitStack() as locks:
        # Same inode and lock order as the daily/manual/Auto publisher. Never
        # unlink a lock file: an existing worker could still own that inode.
        for path in (a.publish_lock, a.scheduler_lock):
            if not locks.enter_context(exclusive_lock(path)):
                raise AttributionError("migration_publisher_busy", 503)
        if a.apply or a.rollback:
            result = run(a.state, a.manifest_sha256, rollback=a.rollback)
        else:
            settings = {}
            for line in Path(a.env_file).read_text(encoding="utf-8-sig").splitlines():
                if line.startswith("ADMIN_MAPPING_MYSQL_") and "=" in line:
                    key, value = line.split("=", 1)
                    settings[key] = value.strip().strip('"').strip("'")
            settings.update(JOB_DB_PATH=a.users_db, SOCIAL_ANALYTICS_X_DB_PATH=a.db, X_ATTRIBUTION_AUTO_DB_PATH=a.auto_db)
            result = create_manifest(a.db, a.public_root, resolver(settings), a.state)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except AttributionError as exc:
        print(json.dumps(dict(status="blocked", error=exc.code)))
        sys.exit(1)
