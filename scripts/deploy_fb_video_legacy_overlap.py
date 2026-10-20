"""Deploy only the manual publisher's legacy-overlap policy, preserving ledgers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
from deploy_fb_video_publish import (
    API_UNIT, BASE, FB, FB_UNIT, MAIN, OLD_TIMERS, ROOT, active_work,
    atomic_bytes, check_mount, health, manual_facts, require, run, sha,
    snapshot_db, state,
)

RELATIVE = Path("features/fb_video_publish/sources.py")
# Exact known production source, not a release-wide baseline.
EXPECTED_BEFORE = "048e0e7385782f9e663980c29ae2066fc8d01e6e1341e854c07fec1a1c1f227c"
WORKER = "fb-video-publish-worker.service"
PREPARE = "fb-video-publish-prepare.service"
TIMERS = [*OLD_TIMERS, "fb-video-publish-prepare.timer"]
MANAGED = [FB_UNIT, WORKER, *TIMERS]
KIND = "fb-video-legacy-overlap"


def command(*args):
    subprocess.run(args, check=True)


def release():
    """Verify exact tracked bytes in a GitHub remote-tracked commit, read-only."""
    remote = run("git", "-C", str(ROOT), "remote", "get-url", "origin")
    require(remote in {
        "git@github.com:gaoyuan12356416/ai-.git",
        "https://github.com/gaoyuan12356416/ai-.git",
        "https://github.com/gaoyuan12356416/ai-",
    }, "Release origin must be the approved GitHub repository")
    require(not run("git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"),
            "Tracked release files are dirty")
    commit = run("git", "-C", str(ROOT), "rev-parse", "HEAD")
    containing = run("git", "-C", str(ROOT), "for-each-ref", "--contains=" + commit,
                     "--format=%(refname)", "refs/remotes/origin/")
    require(bool(containing), "Release HEAD is not contained in a GitHub origin-tracked branch")
    for relative in (RELATIVE.as_posix(), "scripts/deploy_fb_video_legacy_overlap.py"):
        tracked = subprocess.check_output(["git", "-C", str(ROOT), "show", commit + ":" + relative])
        require((ROOT / relative).read_bytes() == tracked, "Release file differs from committed bytes: " + relative)
        compile(tracked.decode("utf-8"), relative, "exec")
    return commit, (ROOT / RELATIVE).read_bytes()


def targets():
    # Pin the actual checkout behind /opt/.../current for the entire operation.
    result = [FB.resolve() / RELATIVE, MAIN.resolve() / RELATIVE]
    require(len(set(result)) == 2, "Expected two separate source copies")
    for path in result:
        require(path.is_file() and not path.is_symlink(), "Source target missing or symlink: " + str(path))
    return result


def saved_states():
    result = {unit: state(unit) for unit in [*MANAGED, API_UNIT]}
    require(all(value["active"] in {"active", "inactive"} and value["enabled"]
                for value in result.values()), "A service/timer is not in a stable active/inactive state")
    require(result[FB_UNIT]["active"] == "active" or result[WORKER]["active"] == "inactive",
            "Manual worker cannot be active with an inactive sidecar")
    return result


def idle():
    old_services, old_tasks = active_work()
    manual_tasks, _ = manual_facts()
    preparing = state(PREPARE)["active"] != "inactive"
    return {"old_services": old_services, "old_inflight_tasks": old_tasks,
            "manual_nonterminal_tasks": manual_tasks, "manual_prepare_busy": preparing}


def require_idle():
    facts = idle()
    require(not facts["old_services"] and not facts["old_inflight_tasks"]
            and not facts["manual_nonterminal_tasks"] and not facts["manual_prepare_busy"],
            "Publishing work is busy; no source switch: " + json.dumps(facts))


def restore_states(saved):
    """Never enable/disable units; restore only the active state we changed."""
    errors = []
    # Restore admission before clients/timers. Main API is observed, never restarted.
    for unit in MANAGED:
        try:
            current = state(unit)
            require(current["enabled"] == saved[unit]["enabled"], "Unit enabled state drift: " + unit)
            if current["active"] != saved[unit]["active"]:
                command("systemctl", "start" if saved[unit]["active"] == "active" else "stop", unit)
            require(state(unit) == saved[unit], "Cannot restore unit state: " + unit)
        except Exception as exc:
            errors.append(str(exc))
    require(state(API_UNIT) == saved[API_UNIT], "Main API state changed unexpectedly")
    require(not errors, "Unit state recovery failed: " + "; ".join(errors))


def validate_current(paths, allowed):
    require(paths == targets(), "The current release pointer changed")
    for path in paths:
        require(sha(path) in allowed[str(path)], "Source SHA drift: " + str(path))


def write_source(data, path, metadata):
    atomic_bytes(data, path, metadata["mode"])
    os.chown(path, metadata["uid"], metadata["gid"])
    require(sha(path) == hashlib.sha256(data).hexdigest(), "Source readback mismatch: " + str(path))


def switch(paths, desired, allowed, commit, *, action, rollback_of=None):
    saved = saved_states()
    validate_current(paths, allowed)
    original = {str(path): path.read_bytes() for path in paths}
    metadata = {str(path): {"mode": path.stat().st_mode & 0o777,
                           "uid": path.stat().st_uid, "gid": path.stat().st_gid} for path in paths}
    changed, resuming, states_restored, manifest_path, manifest = False, False, False, None, None
    try:
        running_timers = [unit for unit in TIMERS if saved[unit]["active"] == "active"]
        if running_timers:
            command("systemctl", "stop", *running_timers)
        require_idle()
        if saved[WORKER]["active"] == "active":
            command("systemctl", "stop", WORKER)
        require(state(WORKER)["active"] == "inactive", "Manual worker did not stop")
        require_idle()
        if saved[FB_UNIT]["active"] == "active":
            command("systemctl", "stop", FB_UNIT)
        require(state(FB_UNIT)["active"] == "inactive", "Sidecar did not stop")
        # A submission could commit between the first idle check and admission stop.
        require_idle()
        validate_current(paths, allowed)
        require(all(path.read_bytes() == original[str(path)] for path in paths), "Source changed while draining")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = BASE / "backups" / (KIND + "-" + stamp + "-" + commit[:8])
        backup.mkdir(parents=True, mode=0o700)
        files = []
        for index, path in enumerate(paths):
            saved_file = backup / ("source-" + str(index) + ".py")
            saved_file.write_bytes(original[str(path)])
            files.append({"target": str(path), "backup": str(saved_file),
                          "before": sha(saved_file), "after": hashlib.sha256(desired[str(path)]).hexdigest(),
                          **metadata[str(path)]})
        snapshot_db(backup / "operational.sqlite3")
        manifest = {"kind": KIND, "action": action, "commit": commit, "states": saved,
                    "files": files, "rollback_of": rollback_of, "deployed": False}
        manifest_path = backup / "manifest.json"
        atomic_bytes((json.dumps(manifest, indent=2) + "\n").encode(), manifest_path, 0o600)
        changed = True
        for path in paths:
            write_source(desired[str(path)], path, metadata[str(path)])
        # Persist the completed source switch before reopening admission. Once
        # clients can submit work, a state-recovery error must not roll code back.
        manifest["deployed"] = True
        atomic_bytes((json.dumps(manifest, indent=2) + "\n").encode(), manifest_path, 0o600)
        resuming = True
        if saved[FB_UNIT]["active"] == "active":
            command("systemctl", "start", FB_UNIT)
            health("http://127.0.0.1:18835/health")
        restore_states(saved)
        states_restored = True
    except Exception as exc:
        states_restored = False
        if changed and not resuming:
            # Still isolated: restore both originals, including a partial copy.
            for path in paths:
                current = sha(path)
                expected = {hashlib.sha256(original[str(path)]).hexdigest(),
                            hashlib.sha256(desired[str(path)]).hexdigest()}
                require(current in expected, "Recovery stopped on unknown source SHA: " + str(path))
            for path in paths:
                write_source(original[str(path)], path, metadata[str(path)])
            manifest["automatically_restored"] = True
            manifest["deployed"] = False
            atomic_bytes((json.dumps(manifest, indent=2) + "\n").encode(), manifest_path, 0o600)
        if resuming:
            raise RuntimeError("Source switch retained; service state recovery failed after admission "
                               "resumed. Inspect units and manifest: " + str(manifest_path)) from exc
        raise
    finally:
        if not states_restored:
            restore_states(saved)
    print(json.dumps({"ok": True, "action": action, "manifest": str(manifest_path),
                      "only_sidecar_restarted": saved[FB_UNIT]["active"] == "active",
                      "database_and_links_retained": True}))


def rollback(manifest_path, paths, commit):
    path = manifest_path.resolve()
    require(path.is_relative_to((BASE / "backups").resolve()) and path.name == "manifest.json",
            "Rollback manifest must belong to the deployment backups")
    manifest = json.loads(path.read_text())
    require(manifest.get("kind") == KIND and manifest.get("action") == "apply",
            "Not a legacy-overlap apply manifest")
    files = manifest.get("files", [])
    require(len(files) == 2 and {item["target"] for item in files} == {str(p) for p in paths},
            "Rollback manifest targets differ from the two current source copies")
    desired, allowed = {}, {}
    for item in files:
        stored = Path(item["backup"]).resolve()
        require(stored.parent == path.parent and stored.name in {"source-0.py", "source-1.py"},
                "Rollback backup path is invalid")
        require(item["before"] == EXPECTED_BEFORE and sha(stored) == EXPECTED_BEFORE,
                "Rollback backup SHA drift")
        require(re.fullmatch(r"[a-f0-9]{64}", item["after"]) is not None, "Rollback after SHA invalid")
        desired[item["target"]] = stored.read_bytes()
        allowed[item["target"]] = {item["before"], item["after"]}
    switch(paths, desired, allowed, commit, action="rollback", rollback_of=str(path))


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    check_mount()
    commit, source = release()
    paths = targets()
    if args.rollback:
        rollback(args.rollback, paths, commit)
        return
    allowed = {str(path): {EXPECTED_BEFORE} for path in paths}
    validate_current(paths, allowed)
    compile(source.decode("utf-8"), RELATIVE.as_posix(), "exec")
    before_states, facts = saved_states(), idle()
    print(json.dumps({"preflight_ok": True, "commit": commit, "after_sha256": hashlib.sha256(source).hexdigest(),
                      "targets": [str(path) for path in paths], "states": before_states, "work": facts}), flush=True)
    if args.apply:
        switch(paths, {str(path): source for path in paths}, allowed, commit, action="apply")


if __name__ == "__main__":
    main()
