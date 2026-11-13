"""Rollback social code only. Keep /m/, Moboreels sidecar, all data and receipts alive."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("deployment", type=Path)
    parser.add_argument("phase", choices=("main", "x", "fb", "tt", "tt_auto", "core-ui"))
    parser.add_argument("--checks", required=True, type=Path)
    args = parser.parse_args()
    deployment = args.deployment.resolve()
    manifest = json.loads((deployment / "manifest.json").read_text(encoding="utf-8"))
    release = Path(manifest["release"])
    spec = importlib.util.spec_from_file_location("moboreels_deploy", release / "ops/moboreels-attribution-20261113/deploy.py")
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    deploy.verify(release, manifest["commit"])
    deploy.require(deployment.parent == (deploy.DATA / "deployments").resolve(), "Unexpected deployment")
    checks = json.loads(args.checks.read_text(encoding="utf-8"))
    deploy.require(checks.get("commit") == manifest["commit"], "Checks commit differs")
    phase = "core" if args.phase == "core-ui" else args.phase
    activation = manifest["phases"].get(phase)
    deploy.require(activation is not None, "Phase was never installed")
    review = checks.get("phases", {}).get("rollback-" + args.phase)
    deploy.require(isinstance(review, dict), "Missing reviewed rollback busy checks")
    entries = [r for r in manifest["files"] if r["phase"] == phase]
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
        deploy.require(current in (root["resolved"], manifest["candidates"][phase]), "Current was replaced by another release")
        if current != root["resolved"]:
            deploy.switch(root["logical"], root["resolved"], manifest["candidates"][phase])
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

