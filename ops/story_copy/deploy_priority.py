"""Deploy deadline ordering and concurrent caption workers without restarting publishers."""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from features.story_copy import store
from ops.story_copy import deploy as d
from ops.story_copy import deploy_no_history as release

release.FILES = tuple("features/story_copy/" + name for name in
                      ("sources.py", "store.py", "worker.py"))
release.SERVICES = ("story-copy-worker.service", "story-copy-settings.service",
                    "fb-auto-post-service.service", "x-post-automation.service",
                    "x-tag-post.service", "pipeline-coordinator.service")


def prepare(folder):
    release.prepare(folder)
    backup = folder / "captions-before.sqlite3"
    with store.connection(readonly=True) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    backup.chmod(0o600)
    record = json.loads((folder / "record.json").read_text())
    record["database_backup"] = str(backup)
    d.save(folder, record)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "apply", "rollback"))
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if (folder.parent != Path("/mnt/data-disk/story-copy-deploy")
            or not os.path.ismount("/mnt/data-disk")
            or d.read("findmnt", "-n", "-o", "UUID", "--mountpoint", "/mnt/data-disk")
            != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"):
        raise SystemExit("unexpected deployment path or data disk")
    prepare(folder) if args.action == "prepare" else release.switch(folder, args.action == "rollback")
