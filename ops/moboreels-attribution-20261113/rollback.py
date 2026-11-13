"""Rollback social code only. Keep /m/, Moboreels sidecar, all data and receipts alive."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


def load_deployer(manifest, deployer_sha=None):
    # Tool fixes may be newer than the immutable business deployment manifest.
    # Always load the helper beside this GitHub release's rollback script.
    tool_release = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("moboreels_deploy", tool_release / "ops/moboreels-attribution-20261113/deploy.py")
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    deploy.verify(tool_release, deployer_sha or manifest["commit"])
    deploy.verify(Path(manifest["release"]), manifest["commit"])
    return deploy


def fb_hotfix_candidate(manifest, deployment, deploy, entries):
    """Recognize only the durable, activated two-module FB startup fix."""
    hotfix = manifest.get("hotfixes", {}).get("fb-metric-startup")
    if hotfix is None:
        return manifest["candidates"]["fb"], entries
    require = deploy.require
    require(hotfix.get("status") == "activated" and hotfix.get("business_sha") == manifest["commit"],
            "FB startup fix requires reconciliation")
    receipt = Path(hotfix["receipt"]).resolve()
    require((deployment / "private").resolve() in receipt.parents and
            json.loads(receipt.read_text(encoding="utf-8")) == hotfix, "FB hotfix receipt differs")
    release = Path(hotfix["release"])
    deploy.verify(release, hotfix["commit"])
    candidate = str(deploy.CANDIDATES / hotfix["commit"] / deployment.name / "fb-metric-fix")
    require(hotfix["candidate"] == hotfix["current"] == candidate and
            hotfix["previous_current"] == manifest["candidates"]["fb"] and
            hotfix["previous_tree"] == manifest["candidate_trees"]["fb"], "FB hotfix candidate provenance differs")
    require(deploy.tree_digest(candidate) == hotfix["candidate_tree"] and
            deploy.tree_digest(hotfix["previous_current"]) == hotfix["previous_tree"], "FB hotfix tree drift")
    expected = {"features/fb_auto_posts/core.py", "features/fb_auto_posts/service.py"}
    files = hotfix.get("files", [])
    require(len(files) == 2 and {row.get("relative") for row in files} == expected, "Unexpected FB hotfix scope")
    revised = [dict(row) for row in entries]
    for row in files:
        source = release / "ops/moboreels-attribution-20261113/runtime/fb" / row["relative"]
        target = str(Path(manifest["roots"]["fb"]["logical"]) / row["relative"])
        matches = [entry for entry in revised if entry["target"] == target]
        require(len(matches) == 1 and row["target"] == target and row["source"] == str(source), "FB source/target differs")
        git_hash = hashlib.sha256(subprocess.check_output(["git", "-C", str(release), "show",
            hotfix["commit"] + ":ops/moboreels-attribution-20261113/runtime/fb/" + row["relative"]])).hexdigest()
        require(matches[0]["after"] == row["before"] and deploy.digest(row["backup"]) == row["before"] and
                deploy.digest(source) == row["after"] == git_hash, "FB module receipt/source differs")
        matches[0]["after"] = row["after"]
    return candidate, revised


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("deployment", type=Path)
    parser.add_argument("phase", choices=("main", "x", "fb", "tt", "tt_auto", "core-ui"))
    parser.add_argument("--checks", required=True, type=Path)
    parser.add_argument("--deployer-sha", help="Exact tool-release SHA; defaults to the business manifest commit")
    args = parser.parse_args()
    deployment = args.deployment.resolve()
    manifest = json.loads((deployment / "manifest.json").read_text(encoding="utf-8"))
    deploy = load_deployer(manifest, args.deployer_sha)
    deploy.require(deployment.parent == (deploy.DATA / "deployments").resolve(), "Unexpected deployment")
    checks = json.loads(args.checks.read_text(encoding="utf-8"))
    deploy.require(checks.get("commit") == manifest["commit"], "Checks commit differs")
    phase = "core" if args.phase == "core-ui" else args.phase
    activation = manifest["phases"].get(phase)
    deploy.require(activation is not None, "Phase was never installed")
    review = checks.get("phases", {}).get("rollback-" + args.phase)
    deploy.require(isinstance(review, dict), "Missing reviewed rollback busy checks")
    entries = [r for r in manifest["files"] if r["phase"] == phase]
    active_candidate = manifest["candidates"].get(phase)
    if phase == "fb":
        active_candidate, entries = fb_hotfix_candidate(manifest, deployment, deploy, entries)
    if args.phase == "core-ui":
        # Preserve sidecar package/current, /m/ Nginx route, macro config and report timer.
        entries = [r for r in entries if r.get("nav") or
                   (Path(r["target"]).name.startswith("moboreels-") and
                    Path(r["target"]).parent in (deploy.MAIN / 'static', deploy.PUBLIC))]
    for entry in entries:
        target = Path(entry["target"])
        if entry["before"] is None and not target.exists():
            continue
        if target.is_file() and deploy.digest(target) == entry["before"]:
            continue
        deploy.fence(entry, installed=True)
        deploy.require(entry["before"] is None or Path(entry["backup"]).is_file(), "Missing original backup")
    rollback_phase = "rollback-" + args.phase
    deploy.busy(review, deployment, rollback_phase)
    deploy.scheduler(review, deployment, rollback_phase)
    deploy.backups(review, deployment, rollback_phase)
    deploy.busy(review, deployment, rollback_phase)
    if phase in manifest["candidates"]:
        root = manifest["roots"][phase]
        current = str(Path(root["logical"]).resolve())
        deploy.require(current in (root["resolved"], active_candidate), "Current was replaced by another release")
        if current != root["resolved"]:
            deploy.switch(root["logical"], root["resolved"], active_candidate)
    else:
        for entry in entries:
            target = Path(entry["target"])
            if entry["before"] is None:
                if target.exists():
                    deploy.fence(entry, installed=True)
                    target.unlink()  # Exact newly introduced source/static file; never a data directory.
            elif not target.is_file() or deploy.digest(target) != entry["before"]:
                deploy.fence(entry, installed=True)
                deploy.atomic_copy(entry["backup"], target, entry["mode"])
    for unit in review.get("restart_services", []):
        deploy.run("systemctl", "restart", unit)
        deploy.run("systemctl", "is-active", unit)
    deploy.scheduler(review, deployment, rollback_phase, restore=True)
    for entry in entries:
        deploy.fence(entry)
    manifest.setdefault("rollbacks", {})[args.phase] = {"completed_at": deploy.utc(), "preserved_moboreels": True}
    deploy.save(deployment / "manifest.json", manifest)
    print(json.dumps({"phase": args.phase, "status": "rolled_back",
                      "preserved": ["Moboreels sidecar", "/m/", "MySQL tables", "SQLite ledgers", "durable receipts"]}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "stopped", "type": type(exc).__name__,
                          "reason": str(exc) if isinstance(exc, RuntimeError) else "inspect restricted evidence"}))
        sys.exit(1)
