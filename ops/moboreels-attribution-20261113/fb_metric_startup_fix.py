"""Replace only the FB metric-reader startup, preserving the original deployment."""
import argparse
from contextlib import closing
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

BUSINESS_SHA = "b53d3ac4c34b9e5c5164b7e56696f13e9a59de09"
NAME = "fb-metric-startup"
OPS = "ops/moboreels-attribution-20261113"
MODULES = ("features/fb_auto_posts/core.py", "features/fb_auto_posts/service.py")
COORDINATOR = Path("/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def blob_digest(release, commit, relative):
    return hashlib.sha256(subprocess.check_output(
        ["git", "-C", str(release), "show", commit + ":" + relative])).hexdigest()


def approved_changes(names):
    # The separately reviewed main merge may share this release. This operation
    # never installs app/navigation; their original deployment fences still apply.
    allowed = set(MODULES) | {"features/moboreels_library/orders.py", "app.py", "static/quick-nav.js"}
    allowed.update(OPS + "/runtime/fb/" + name for name in MODULES)
    return all(name in allowed or name.startswith("tests/") or
               (name.startswith("ops/") and "/runtime/" not in name) for name in names)


def check_paused(receipt, require):
    require(receipt.get("status") == "committed" and receipt.get("lane") == "fb" and
            receipt.get("target_enabled") is False and receipt.get("changed") is True,
            "Missing committed scoped FB pause")
    with closing(sqlite3.connect(COORDINATOR.as_uri() + "?mode=ro", uri=True, timeout=10)) as conn:
        conn.execute("PRAGMA query_only=ON")
        version, payload = conn.execute("SELECT version,config_json FROM pipeline_config WHERE id=1").fetchone()
    require(version == receipt["after_version"] and "fb" not in json.loads(payload)["enabled_channels"],
            "FB admission/version changed; review the drain receipt")


def ensure_manifest(manifest, require):
    require(manifest["commit"] == BUSINESS_SHA, "Wrong original deployment")
    require(manifest["phases"].get("fb", {}).get("status") == "activated", "FB phase is not active")
    require("main" not in manifest["phases"], "Main phase has already started")
    require(not any(p.get("status") == "installing" for p in manifest["phases"].values()),
            "Wait for the active deployment phase")


def persist(manifest_path, record, durable_json, require):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ensure_manifest(manifest, require)
    existing = manifest.setdefault("hotfixes", {}).get(NAME)
    require(existing is None or existing.get("id") == record["id"], "Reconcile the earlier FB fix")
    for override in record["main_overrides"]:
        matches = [r for r in manifest["files"] if r["target"] == override["target"]]
        require(len(matches) == 1 and matches[0] in (override["original"], override["replacement"]),
                "Main planned entry changed")
        # The original staged artifact stays intact; the new artifact is an exact Git blob.
        matches[0].clear()
        matches[0].update(copy.deepcopy(override["replacement"]))
    manifest.setdefault("prepared_hotfixes", {})[NAME] = {
        "commit": record["commit"], "release": record["release"], "receipt": record["receipt"],
        "main_overrides": copy.deepcopy(record["main_overrides"])}
    manifest["hotfixes"][NAME] = copy.deepcopy(record)
    durable_json(record["receipt"], record)
    durable_json(manifest_path, manifest)


def health(timeout_seconds=45):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:18835/health", timeout=3) as response:
                result = json.load(response)
            if result.get("ok") is True and result.get("service") == "fb-auto-post":
                return {"ok": True, "service": "fb-auto-post"}
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("FB read-only health did not become ready")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sha", help="Exact new GitHub release SHA")
    parser.add_argument("--deployment", required=True, type=Path)
    parser.add_argument("--drain-receipt", required=True, type=Path)
    args = parser.parse_args()
    release = Path(__file__).resolve().parents[2]
    deploy = load_module("fb_fix_deploy", release / OPS / "deploy.py")
    durable_json = load_module("fb_fix_durable", release / OPS / "report_query_fix.py").durable_json
    deploy.verify(release, args.sha)
    require = deploy.require
    changed = deploy.output("git", "-C", release, "diff", "--name-only", BUSINESS_SHA, args.sha).splitlines()
    require(approved_changes(changed), "Unexpected business/runtime changes in fix release")
    deployment = args.deployment.resolve()
    require(deployment.parent == (deploy.DATA / "deployments").resolve(), "Unexpected deployment path")
    manifest_path = deployment / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ensure_manifest(manifest, require)
    require(NAME not in manifest.get("hotfixes", {}), "Existing FB fix requires reconciliation")
    previous_release = Path(manifest["release"])
    deploy.verify(previous_release, BUSINESS_SHA)
    drain_path = args.drain_receipt.resolve()
    require(drain_path.parent == deployment / "private", "Drain receipt must be in private evidence")
    drain = json.loads(drain_path.read_text(encoding="utf-8"))
    check_paused(drain, require)
    root = manifest["roots"]["fb"]
    link, previous = Path(root["logical"]), Path(manifest["candidates"]["fb"])
    require(link.is_symlink() and link.resolve() == previous, "FB current differs from b53 candidate")
    require(deploy.tree_digest(previous) == manifest["candidate_trees"]["fb"], "FB candidate tree drift")
    require(deploy.tree_digest(root["resolved"]) == manifest["source_trees"]["fb"], "Original FB tree drift")
    require(not (previous / "data").exists() and not list(previous.glob("*.sqlite*")), "Candidate contains business data")
    template = json.loads((release / OPS / "activation-checks.template.json").read_text(encoding="utf-8"))
    checks = copy.deepcopy(template["phases"]["fb"])
    # A harmless legacy dispatch_queued loop is not an active publication. Check
    # actual business leases/intents; the separate metric migration may keep reading.
    checks["must_be_inactive"] = []
    checks["checks"] = [r for r in checks["checks"] if r["name"] != "metric_task_table_must_be_empty"]
    deploy.busy(checks, deployment, NAME)
    receipt_dir = deployment / "private" / (NAME + "-" + str(time.time_ns()))
    receipt_dir.mkdir(mode=0o700)
    parent = deploy.CANDIDATES / args.sha / deployment.name
    for directory in (deploy.CANDIDATES / args.sha, parent):
        if not directory.exists():
            directory.mkdir(mode=0o755)
        require(directory.is_dir() and not directory.is_symlink(), "Candidate parent is not a directory")
        os.chmod(str(directory), 0o755)
    candidate = parent / "fb-metric-fix"
    require(not candidate.exists() and not candidate.is_symlink(), "Keep existing candidate for reconciliation")
    shutil.copytree(str(previous), str(candidate), symlinks=True)
    os.chmod(str(candidate), 0o755)
    require(deploy.tree_digest(candidate) == manifest["candidate_trees"]["fb"], "Clone is not exact")
    files, overrides = [], []
    for relative in MODULES:
        runtime_source = release / OPS / "runtime/fb" / relative
        before = blob_digest(previous_release, BUSINESS_SHA, OPS + "/runtime/fb/" + relative)
        require(deploy.digest(previous / relative) == before, "FB module differs from b53 Git blob")
        after = blob_digest(release, args.sha, OPS + "/runtime/fb/" + relative)
        require(deploy.digest(runtime_source) == after and before != after, "Runtime fix absent or modified")
        compile(runtime_source.read_bytes(), relative, "exec")
        backup = receipt_dir / (Path(relative).name + ".before")
        shutil.copy2(str(previous / relative), str(backup))
        os.chmod(str(backup), 0o600)
        require(deploy.digest(backup) == before, "Backup differs")
        deploy.atomic_copy(runtime_source, candidate / relative, (previous / relative).stat().st_mode & 0o777)
        files.append({"target": str(link / relative), "relative": relative, "source": str(runtime_source),
                      "before": before, "after": after, "backup": str(backup)})
        target = str(deploy.MAIN / relative)
        entries = [r for r in manifest["files"] if r["target"] == target]
        require(len(entries) == 1 and entries[0]["phase"] == "main", "Missing original main planned entry")
        original = copy.deepcopy(entries[0])
        require(deploy.digest(original["staged"]) == original["after"] ==
                blob_digest(previous_release, BUSINESS_SHA, relative), "Original main staged blob differs")
        deploy.fence(original)
        main_source = release / relative
        main_after = blob_digest(release, args.sha, relative)
        require(deploy.digest(main_source) == main_after and main_after != original["after"], "Main fix absent or modified")
        compile(main_source.read_bytes(), relative, "exec")
        main_staged = deployment / "staged" / (receipt_dir.name + "-" + Path(relative).name)
        require(not main_staged.exists(), "Occupied main fix artifact")
        deploy.atomic_copy(main_source, main_staged, 0o600)
        require(deploy.digest(main_staged) == main_after, "Main fix artifact differs")
        replacement = dict(original, source=str(main_source), staged=str(main_staged), after=main_after)
        overrides.append({"target": target, "original": original, "replacement": replacement})
    for relative in MODULES + ("features/moboreels_library/macro_client.py",):
        deploy.run("runuser", "-u", "fb-auto-post", "--", "test", "-r", candidate / relative)
    require(deploy.tree_digest(previous) == manifest["candidate_trees"]["fb"], "Live tree changed during clone")
    durable_json(receipt_dir / "current.before.json", {"current": str(previous), "business_sha": BUSINESS_SHA})
    record = {"id": receipt_dir.name, "name": NAME, "status": "intent", "created_at": deploy.utc(),
              "commit": args.sha, "release": str(release), "business_sha": BUSINESS_SHA,
              "candidate": str(candidate), "previous_current": str(previous), "current": str(candidate),
              "previous_tree": manifest["candidate_trees"]["fb"], "candidate_tree": deploy.tree_digest(candidate),
              "files": files, "main_overrides": overrides, "receipt": str(receipt_dir / "receipt.json"),
              "drain_receipt": str(drain_path), "drain_receipt_sha256": deploy.digest(drain_path),
              "coordinator_version": drain["after_version"], "service": "fb-auto-post-service.service"}
    deploy.busy(checks, deployment, NAME)
    check_paused(drain, require)
    require(deploy.digest(drain_path) == record["drain_receipt_sha256"] and link.resolve() == previous,
            "Drain/current changed")
    require(deploy.tree_digest(previous) == record["previous_tree"], "Live tree changed")
    for override in overrides:
        deploy.fence(override["original"])
    persist(manifest_path, record, durable_json, require)  # Durable intent before live switch.
    try:
        deploy.switch(link, candidate, previous)
        record["status"] = "current_switched"
        persist(manifest_path, record, durable_json, require)
        deploy.run("systemctl", "restart", record["service"], timeout=60)
        record["health"] = health()
        deploy.run("systemctl", "is-active", record["service"], timeout=10)
        require(link.resolve() == candidate and deploy.tree_digest(candidate) == record["candidate_tree"],
                "Post-start candidate verification failed")
        check_paused(drain, require)
        record.update(status="activated", completed_at=deploy.utc())
    except BaseException:
        record.update(status="needs_reconciliation", failed_at=deploy.utc())
        persist(manifest_path, record, durable_json, require)
        raise
    persist(manifest_path, record, durable_json, require)
    print(json.dumps({"status": "activated", "sha": args.sha, "receipt": record["receipt"],
                      "service": record["service"], "main": "prepared_only", "admission": "still_paused"}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "stopped", "type": type(exc).__name__,
                          "reason": str(exc) if isinstance(exc, RuntimeError) else "inspect restricted receipt"}))
        sys.exit(1)
