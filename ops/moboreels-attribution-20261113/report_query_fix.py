"""Apply the report DATE-bound fix without restaging or rewriting the b53 deployment."""
import argparse
from contextlib import closing
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

BUSINESS_SHA = "b53d3ac4c34b9e5c5164b7e56696f13e9a59de09"
MODULE = "features/moboreels_library/orders.py"
CHECKS = (
    ("sync.sqlite3", "sync_run", "payload", ("queued", "running")),
    ("promotions.sqlite3", "promotions", "record_json", ("creating",)),
    ("reports.sqlite3", "report_run", "payload", ("queued", "running")),
    ("short-links.sqlite3", "links", "record_json", ("preparing", "creating")),
    ("media-accounts/registrations.sqlite3", "registrations", "payload", ("queued", "submitting")),
)


def durable_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(path))
    descriptor = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def check_idle(data_root, require):
    result = []
    for relative, table, column, statuses in CHECKS:
        path = data_root / relative
        if not path.exists():
            result.append({"ledger": relative, "exists": False, "active": 0})
            continue
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as conn:
            conn.execute("PRAGMA query_only=ON")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(" + table + ")")}
            require(column in columns, "Unexpected live ledger schema: " + relative)
            count = conn.execute("SELECT COUNT(*) FROM " + table + " WHERE json_extract(" + column +
                ",'$.status') IN (" + ",".join("?" for _ in statuses) + ")", statuses).fetchone()[0]
        result.append({"ledger": relative, "exists": True, "active": count})
    require(not any(row["active"] for row in result), "Moboreels work active; defer report fix")
    return result


def update_hotfix(manifest_path, record, require):
    # Re-read immediately so prior completed lane records are preserved. Execute this
    # operation between lane activations, never concurrently with a manifest writer.
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(manifest["commit"] == BUSINESS_SHA, "Original deployment changed")
    require(not any(p.get("status") == "installing" for p in manifest["phases"].values()),
            "A deployment phase is still installing")
    hotfixes = manifest.setdefault("hotfixes", {})
    require(isinstance(hotfixes, dict), "Unexpected hotfix history format")
    existing = hotfixes.get("report-query-date")
    require(existing is None or existing.get("id") == record["id"], "Reconcile the earlier report fix receipt")
    hotfixes["report-query-date"] = dict(record)
    durable_json(manifest_path, manifest)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sha", help="Exact new GitHub release SHA")
    parser.add_argument("--deployment", required=True, type=Path)
    args = parser.parse_args()
    release = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("report_fix_deploy", release / "ops/moboreels-attribution-20261113/deploy.py")
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    deploy.verify(release, args.sha)  # Includes root, /usr/bin/python3, UUID, clean Git and fetched exact SHA.
    require = deploy.require
    changed = deploy.output("git", "-C", release, "diff", "--name-only", BUSINESS_SHA, args.sha).splitlines()
    business = [name for name in changed if not name.startswith(("ops/", "tests/"))]
    require(business == [MODULE], "Only orders.py may differ in business source")
    previous = deploy.CURRENT.resolve()
    expected_previous = deploy.DATA / "releases" / BUSINESS_SHA
    require(deploy.CURRENT.is_symlink() and previous == expected_previous, "Current sidecar is not b53")
    deploy.verify(previous, BUSINESS_SHA)
    expected = subprocess.check_output(["git", "-C", str(release), "show", BUSINESS_SHA + ":" + MODULE])
    before = hashlib.sha256(expected).hexdigest()
    target, source = deploy.MAIN / MODULE, release / MODULE
    require(deploy.digest(previous / MODULE) == before and deploy.digest(target) == before, "Original orders.py SHA differs")
    after = deploy.digest(source)
    require(after != before, "Report fix is absent")
    compile(source.read_bytes(), MODULE, "exec")
    deployment = args.deployment.resolve()
    require(deployment.parent == (deploy.DATA / "deployments").resolve(), "Unexpected deployment path")
    manifest_path = deployment / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(manifest["commit"] == BUSINESS_SHA and manifest["release"] == str(previous), "Wrong original deployment")
    require(manifest["phases"].get("core", {}).get("status") == "activated", "Original core is not active")
    require(not any(p.get("status") == "installing" for p in manifest["phases"].values()), "Wait for current lane activation")
    sys.path.insert(0, str(release))
    from features.moboreels_library.settings import Settings
    data_root = Settings.from_env().data_dir.resolve()
    require(data_root.is_dir() and deploy.DATA.resolve() in data_root.parents, "Unexpected Moboreels data directory")
    idle = check_idle(data_root, require)
    receipt_dir = deployment / "private" / ("report-query-fix-" + str(time.time_ns()))
    receipt_dir.mkdir(mode=0o700)
    backup = receipt_dir / "orders.py.before"
    shutil.copy2(str(target), str(backup))
    os.chmod(str(backup), 0o600)
    durable_json(receipt_dir / "current.before.json", {"current": str(previous), "business_sha": BUSINESS_SHA})
    record = {"id": receipt_dir.name, "name": "report-query-date", "status": "intent",
              "created_at": deploy.utc(), "commit": args.sha, "release": str(release), "business_sha": BUSINESS_SHA,
              "source": str(source),
              "files": [{"target": str(target), "before": before, "after": after,
                         "source": str(source), "backup": str(backup)}],
              "previous_current": str(previous), "current": str(release), "receipt": str(receipt_dir / "receipt.json"),
              "idle_checks": idle}
    require(deploy.digest(backup) == before, "Backup differs")
    check_idle(data_root, require)
    require(deploy.CURRENT.resolve() == previous and deploy.digest(target) == before, "Live baseline changed")
    durable_json(record["receipt"], record)  # Durable intent before any live file/link mutation.
    update_hotfix(manifest_path, record, require)
    try:
        deploy.atomic_copy(source, target, target.stat().st_mode & 0o777)
        record.update(status="module_installed")
        durable_json(record["receipt"], record)
        deploy.switch(deploy.CURRENT, release, previous)
        record.update(status="current_switched")
        durable_json(record["receipt"], record)
        deploy.run("systemctl", "restart", "moboreels-library.service")
        deploy.run("systemctl", "is-active", "moboreels-library.service")
        require(deploy.CURRENT.resolve() == release and deploy.digest(target) == after, "Post-switch verification failed")
        record.update(status="activated", completed_at=deploy.utc())
    except Exception:
        record.update(status="needs_reconciliation", failed_at=deploy.utc())
        durable_json(record["receipt"], record)
        update_hotfix(manifest_path, record, require)
        raise
    durable_json(record["receipt"], record)
    update_hotfix(manifest_path, record, require)
    print(json.dumps({"status": "activated", "sha": args.sha, "receipt": record["receipt"],
                      "service": "moboreels-library.service", "original_manifest_files_preserved": True}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "stopped", "type": type(exc).__name__,
                          "reason": str(exc) if isinstance(exc, RuntimeError) else "inspect restricted receipt"}))
        sys.exit(1)

