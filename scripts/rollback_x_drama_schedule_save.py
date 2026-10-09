#!/usr/bin/env python3
"""Roll back code only, keeping publisher databases and token state live."""
import argparse
import json
from pathlib import Path
import shutil
import time

from install_x_drama_schedule_save import AUTO_TIMERS, CURRENT, DATA, MAIN, ready, run, sha, state, switch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    path = Path(args.manifest).resolve()
    if DATA / "maintenance" not in path.parents:
        raise ValueError("manifest outside the maintenance directory")
    manifest = json.loads(path.read_text())
    previous = Path(manifest["previous_release"]).resolve()
    if DATA / "releases" not in previous.parents:
        raise ValueError("previous release outside release directory")
    for name, expected in manifest["new_hashes"].items():
        if sha(MAIN / name) != expected:
            raise RuntimeError("main runtime changed since this deployment: " + name)
        if sha(path.parent / "main" / name) != manifest["old_hashes"][name]:
            raise RuntimeError("backup integrity failure: " + name)
    active = [u for u in AUTO_TIMERS if state(u) == "active"]
    try:
        if active:
            run("systemctl", "stop", *active)
        deadline = time.monotonic() + 100
        while any(state(u) in {"active", "activating", "deactivating"} for u in ["x-auto-post-runner.service", "x-auto-post-scheduler.service"]):
            if time.monotonic() >= deadline:
                raise RuntimeError("active Auto work did not drain")
            time.sleep(1)
        for name in manifest["old_hashes"]:
            shutil.copy2(path.parent / "main" / name, MAIN / name)
        switch(previous)
        run("systemctl", "restart", "x-post-automation.service", "drama-material-api.service")
        ready()
        print(json.dumps({"status": "rolled_back_code_only", "release": str(previous)}))
    finally:
        if active:
            run("systemctl", "start", *active)


if __name__ == "__main__":
    main()
