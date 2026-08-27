#!/usr/bin/env python3
"""Narrow, GitHub-release-only CPU deployment and non-sending verification.

Run from a clean, exact Git checkout on the CPU host. Never restores live
SQLite on rollback, never emits credentials, and never submits a valid batch.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request


LIVE = Path("/root/drama_material_service")
DB = LIVE / "data/drama_material_jobs.sqlite3"
DATA = Path("/mnt/data-disk/material-replication-broadcast")
ENV = Path("/etc/material-replication-webhook.env")
DROPIN = Path("/etc/systemd/system/drama-material-api.service.d/60-material-replication-webhook.conf")
NGINX = Path("/etc/nginx/default.d/material-replication-webhook.conf")
UNIT = "drama-material-api.service"
EXPECTED_DISK = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def command(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_bytes(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".replication-incoming")
    with open(temporary, "xb") as output:
        os.chmod(temporary, mode)
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def stats():
    connection = sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        result = {}
        for table in ("material_status_broadcast_outbox", "material_replication_broadcast_outbox",
                      "drama_material_job", "drama_screenshot_job", "ad_material_task"):
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                result[table] = dict(connection.execute("SELECT status,count(*) FROM " + table + " GROUP BY status"))
        return result
    finally:
        connection.close()


def restart_safe(snapshot):
    for table in ("material_status_broadcast_outbox", "material_replication_broadcast_outbox"):
        if any(snapshot.get(table, {}).get(status, 0) for status in ("queued", "processing", "retry")):
            raise RuntimeError("pending broadcast rows; postpone restart")
    for table in ("drama_material_job", "drama_screenshot_job"):
        if any(count for status, count in snapshot.get(table, {}).items()
               if status not in ("done", "failed", "canceled", "cancelled")):
            raise RuntimeError("active material job; postpone restart")
    if any(count for status, count in snapshot.get("ad_material_task", {}).items()
           if status not in ("draft", "done", "failed", "demand_review", "material_review",
                             "material_abandoned", "cancelled", "canceled", "demand_rejected", "material_rejected")):
        raise RuntimeError("active ad material job; postpone restart")


def storage_guard():
    if command(["findmnt", "-rn", "-o", "UUID", "/mnt/data-disk"]) != EXPECTED_DISK:
        raise RuntimeError("expected data disk is not mounted")
    if shutil.disk_usage("/mnt/data-disk").free < DB.stat().st_size * 3 + 100 * 1024 * 1024:
        raise RuntimeError("insufficient backup space")


def probe(base, path, expected, token="", body=b"{}"):
    headers = {"Content-Type": "application/json", "Idempotency-Key": "mrb-rejected-validation-only"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(base + path, data=body, headers=headers, method="POST")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
            status, content = response.status, response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            status, content = exc.code, exc.read()
    if status != expected:
        raise RuntimeError("non-sending probe returned unexpected HTTP %s (wanted %s)" % (status, expected))
    payload = json.loads(content)
    expected_code = {401: "invalid_token", 422: "invalid_payload", 413: "payload_too_large"}[expected]
    if payload.get("code") != expected_code:
        raise RuntimeError("non-sending probe returned unexpected error code")
    return {"base": base, "status": status, "code": payload.get("code")}


def verify(backup):
    manifest = json.loads((backup / "manifest.json").read_text())
    if command(["systemctl", "is-active", UNIT]) != "active":
        raise RuntimeError("main service inactive")
    for relative, expected in manifest["deployed_hashes"].items():
        if digest(LIVE / relative) != expected:
            raise RuntimeError("live source mismatch: " + relative)
    if digest(LIVE / "features/material_status_broadcast/service.py") != manifest["legacy_sha256"]:
        raise RuntimeError("legacy module changed")
    token = ENV.read_text().strip().split("=", 1)[1]
    new_path = "/api/integrations/v1/material-replication-events"
    invalid = json.dumps({"event_type": "replication_started", "editor_username": "", "items": []}).encode()
    probes = []
    for base in ("http://127.0.0.1:8787", "https://ai.yingliangads.com"):
        probes.append(probe(base, new_path, 401))
        probes.append(probe(base, new_path, 422, token, invalid))
        probes.append(probe(base, new_path, 413, token, b"x" * 32769))
        probes.append(probe(base, "/api/integrations/v1/material-task-status-events", 401))
    snapshot = stats()
    if snapshot.get("material_replication_broadcast_outbox"):
        raise RuntimeError("new outbox is not empty after rejection-only verification")
    result = {"verified_at": datetime.now(timezone.utc).isoformat(), "probes": probes,
              "stats": snapshot, "real_test_messages": 0,
              "service_pid": command(["systemctl", "show", UNIT, "-p", "MainPID", "--value"])}
    atomic_bytes(backup / "verification.json", json.dumps(result, ensure_ascii=False, indent=2).encode())
    print(json.dumps(result, ensure_ascii=False))


def apply(source, commit, expected_app, expected_legacy):
    storage_guard()
    if command(["git", "-C", str(source), "rev-parse", "HEAD"]) != commit:
        raise RuntimeError("release commit mismatch")
    if command(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"]):
        raise RuntimeError("release tracked files are dirty")
    if digest(LIVE / "app.py") != expected_app or digest(LIVE / "features/material_status_broadcast/service.py") != expected_legacy:
        raise RuntimeError("live baseline drift; reconcile before deploying")
    if any(path.exists() for path in (ENV, DROPIN, NGINX, LIVE / "features/material_replication_broadcast")):
        raise RuntimeError("new feature destination already exists; inspect rather than overwrite")
    before = stats()
    restart_safe(before)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DATA / "backups" / (stamp + "-pre-" + commit[:8])
    backup.mkdir(parents=True, exist_ok=False)
    os.chmod(backup, 0o700)
    shutil.copy2(LIVE / "app.py", backup / "app.py")
    shutil.copy2(LIVE / "features/material_status_broadcast/service.py", backup / "legacy-service.py")
    # Back up existing configuration without displaying it or replacing it on rollback.
    config_backup = backup / "configuration"
    config_backup.mkdir(mode=0o700)
    config_sources = [LIVE / ".env", Path("/etc/systemd/system") / UNIT,
                      Path("/etc/nginx/default.d/material-status-webhook.conf")]
    config_sources.extend(sorted(DROPIN.parent.glob("*.conf")))
    config_manifest = {}
    for index, path in enumerate(config_sources):
        if path.is_file():
            target_path = config_backup / ("%03d-" % index + path.name)
            shutil.copy2(path, target_path)
            os.chmod(target_path, 0o600)
            config_manifest[str(path)] = {"backup": str(target_path), "sha256": digest(target_path)}
    connection = sqlite3.connect(str(DB), timeout=10)
    target = sqlite3.connect(str(backup / "jobs-before.sqlite3"))
    try:
        connection.backup(target)
        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("backup integrity check failed")
    finally:
        target.close()
        connection.close()
    os.chmod(backup / "jobs-before.sqlite3", 0o600)
    with sqlite3.connect(str(backup / "jobs-before.sqlite3")) as original_backup:
        backup_legacy_counts = dict(original_backup.execute(
            "SELECT status,count(*) FROM material_status_broadcast_outbox GROUP BY status"))
    shutil.copy2(backup / "jobs-before.sqlite3", backup / "jobs-rehearsal.sqlite3")
    sys.path.insert(0, str(source))
    from features.material_replication_broadcast import service
    service.ReplicationOutbox(backup / "jobs-rehearsal.sqlite3")
    with sqlite3.connect(str(backup / "jobs-rehearsal.sqlite3")) as rehearsal:
        if rehearsal.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("migration rehearsal failed")
        legacy_counts = dict(rehearsal.execute("SELECT status,count(*) FROM material_status_broadcast_outbox GROUP BY status"))
        if legacy_counts != backup_legacy_counts:
            raise RuntimeError("migration rehearsal altered legacy queue")
    relatives = ["app.py", "features/material_replication_broadcast/__init__.py",
                 "features/material_replication_broadcast/service.py", "features/material_replication_broadcast/delivery.py"]
    token = secrets.token_urlsafe(48)
    configuration = {
        ENV: ("MATERIAL_REPLICATION_WEBHOOK_TOKENS=" + token + "\n").encode(),
        DROPIN: ("[Service]\nEnvironmentFile=" + str(ENV) + "\n").encode(),
        NGINX: (source / "deploy/nginx/material-replication-webhook.conf").read_bytes(),
    }
    manifest = {
        "commit": commit, "release": str(source), "backup": str(backup),
        "original_app_sha256": expected_app, "legacy_sha256": expected_legacy,
        "before_stats": before, "deployed_hashes": {name: digest(source / name) for name in relatives},
        "backup_hashes": {name: digest(backup / name) for name in ("app.py", "legacy-service.py", "jobs-before.sqlite3")},
        "configuration_backup": config_manifest,
        "configuration_hashes": {str(path): hashlib.sha256(content).hexdigest()
                                 for path, content in configuration.items()},
        "pre_service_pid": command(["systemctl", "show", UNIT, "-p", "MainPID", "--value"]),
    }
    atomic_bytes(backup / "manifest.json", json.dumps(manifest, indent=2).encode())
    print(json.dumps({"prepared_backup": str(backup), "commit": commit}, ensure_ascii=False), flush=True)
    # Recheck immediately before any production mutation.
    restart_safe(stats())
    if (digest(LIVE / "app.py") != expected_app
            or digest(LIVE / "features/material_status_broadcast/service.py") != expected_legacy):
        raise RuntimeError("live source changed during backup")
    for name in relatives[1:]:
        atomic_bytes(LIVE / name, (source / name).read_bytes())
    for path, content in configuration.items():
        atomic_bytes(path, content, 0o600 if path == ENV else 0o644)
    command(["nginx", "-t"])
    restart_safe(stats())
    if digest(LIVE / "app.py") != expected_app:
        raise RuntimeError("app changed before main-service switch")
    atomic_bytes(LIVE / "app.py", (source / "app.py").read_bytes())
    command(["systemctl", "daemon-reload"])
    command(["systemctl", "restart", UNIT])
    command(["systemctl", "reload", "nginx"])
    print(json.dumps({"deployed_commit": commit, "backup": str(backup), "token_file": str(ENV)}, ensure_ascii=False), flush=True)
    for _ in range(30):
        try:
            probe("http://127.0.0.1:8787", "/api/integrations/v1/material-replication-events", 401)
            break
        except Exception:
            time.sleep(0.5)
    verify(backup)


def rollback(backup):
    storage_guard()
    if not backup.resolve().is_relative_to((DATA / "backups").resolve()):
        raise RuntimeError("rollback path outside exact backup root")
    manifest = json.loads((backup / "manifest.json").read_text())
    if digest(LIVE / "app.py") not in (manifest["deployed_hashes"]["app.py"], manifest["original_app_sha256"]):
        raise RuntimeError("newer main API changes exist; manual reconciliation required")
    for name, expected in manifest["backup_hashes"].items():
        if digest(backup / name) != expected:
            raise RuntimeError("backup checksum mismatch")
    for path in (ENV, DROPIN, NGINX):
        if path.exists() and digest(path) != manifest["configuration_hashes"].get(str(path)):
            raise RuntimeError("newer feature configuration exists; manual reconciliation required")
    # The same gate applies to partial deployments and ordinary rollbacks.
    restart_safe(stats())
    command(["systemctl", "stop", UNIT])
    atomic_bytes(LIVE / "app.py", (backup / "app.py").read_bytes())
    # Recoverable withdrawal, not deletion; keep token, module and all DB rows.
    for path in (DROPIN, NGINX):
        if path.exists():
            shutil.move(str(path), str(backup / ("withdrawn-" + path.name)))
    command(["systemctl", "daemon-reload"])
    command(["nginx", "-t"])
    command(["systemctl", "start", UNIT])
    command(["systemctl", "reload", "nginx"])
    print(json.dumps({"rolled_back": True, "backup": str(backup), "live_database_preserved": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "verify", "rollback"))
    parser.add_argument("--commit")
    parser.add_argument("--expected-app-sha256")
    parser.add_argument("--expected-legacy-sha256")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if args.action == "apply":
        if not all((args.commit, args.expected_app_sha256, args.expected_legacy_sha256)):
            parser.error("apply requires exact commit and both baseline hashes")
        apply(Path(__file__).resolve().parents[1], args.commit, args.expected_app_sha256, args.expected_legacy_sha256)
    elif args.backup is None:
        parser.error("verify/rollback requires --backup")
    elif args.action == "verify":
        verify(args.backup)
    else:
        rollback(args.backup)


if __name__ == "__main__":
    main()
