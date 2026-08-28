#!/usr/bin/env python3
"""Read-only TT drain evidence; explicit backup uses SQLite's online backup API.

No live application imports, HTTP calls, state refreshes, token values, or unit
mutations. Run on the CPU host with system Python 3.9 or newer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from tt_migration import atomic_json, digest, run_backup_root

CPU_UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
DATABASES = {
    "legacy": (Path("/mnt/data-disk/tt-post-publisher/tt-post.sqlite3"),
               ("tt_post_queue", "tt_post_material_intake", "tt_post_direct_test", "tt_post_schedule_run")),
    "auto": (Path("/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3"), ("tt_auto_task",)),
}
TRIGGERS = (
    "tt-post-prepare.timer", "tt-post-prepare.path", "tt-post-runner.timer", "tt-post-runner.path",
    "tt-auto-post-scheduler.timer", "tt-auto-post-runner.timer", "tt-auto-post-runner.path",
)
RUNNERS = (
    "tt-post-prepare.service", "tt-post-runner.service", "tt-auto-post-scheduler.service",
    "tt-auto-post-runner.service", "tt-post-profile-upgrade.service",
)
RISK_STATUSES = ("preparing", "claimed", "publishing", "reconciling", "unknown", "processing",
                 "init_inflight", "init_outcome_unknown", "initialized")
PORTS = {18829, 18831, 18830, 18834}
FACT_FIELDS = ("id", "status", "gpu_job_id", "publish_id", "unknown_outcome", "scheduled_at_utc",
               "claim_phase", "lease_expires_at_utc", "execution_lease_expires_at_utc",
               "preparation_attempt_count", "publish_attempt_count", "attempt_count",
               "prepared_output_sha256", "prepared_output_size", "preparation_profile")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect_readonly(path: Path, timeout_seconds: int = 5) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise ValueError("database is absent or a symlink")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    deadline = time.monotonic() + timeout_seconds
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    return connection


def table_snapshot(connection: sqlite3.Connection, table: str) -> dict:
    # Table identifiers come only from the fixed inventory above.
    columns = {row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")}
    if not {"id", "status"}.issubset(columns):
        raise ValueError("required task table/schema is missing: " + table)
    token = "execution_token" if table == "tt_post_schedule_run" else "claim_token"
    lease = "execution_lease_expires_at_utc" if table == "tt_post_schedule_run" else "lease_expires_at_utc"
    if not {token, lease}.issubset(columns):
        raise ValueError("claim schema changed: " + table)
    has_claim = "(COALESCE(" + token + ",'')<>'' OR COALESCE(" + lease + ",'')<>'')"
    lease_valid = "julianday(" + lease + ")>julianday('now')"
    unknown = "COALESCE(unknown_outcome,0)<>0" if "unknown_outcome" in columns else "0"
    has_publish = "COALESCE(publish_id,'')<>''" if "publish_id" in columns else "0"
    active = "status IN (" + ",".join("?" for _ in RISK_STATUSES) + ")"
    expressions = ["count(*)", "sum(" + has_claim + ")", "sum(" + has_claim + " AND " + lease_valid + ")",
                   "sum(" + has_claim + " AND julianday(" + lease + ") IS NULL)",
                   "sum(" + unknown + ")", "sum(" + has_publish + ")", "sum(" + active + ")"]
    values = connection.execute("SELECT " + ",".join(expressions) + " FROM " + table,
                                RISK_STATUSES).fetchone()
    names = ("total", "claims_present", "claims_effective", "claims_invalid_lease", "unknown_outcome",
             "publish_id_present", "executing_status")
    result = dict(zip(names, (int(value or 0) for value in values)))
    result["claims_expired"] = result["claims_present"] - result["claims_effective"] - result["claims_invalid_lease"]
    result["statuses"] = dict(connection.execute("SELECT status,count(*) FROM " + table + " GROUP BY status"))
    # Never emit or hash claim_token/worker contents. Only presence is relevant.
    fields = [field for field in FACT_FIELDS if field in columns]
    rows = connection.execute("SELECT " + ",".join(fields) + "," + has_claim +
                              " AS claim_present FROM " + table + " ORDER BY id LIMIT 20001")
    fingerprint = hashlib.sha256()
    count = 0
    for row in rows:
        count += 1
        if count > 20000:
            raise ValueError("bounded task inventory exceeded; manual review required")
        fingerprint.update((json.dumps(list(row), separators=(",", ":")) + "\n").encode())
    result["publication_facts_sha256"] = fingerprint.hexdigest()
    result["blocked"] = bool(result["claims_present"] or result["unknown_outcome"] or result["executing_status"])
    if "scheduled_at_utc" in columns:
        result["future_unclaimed_ready"] = int(connection.execute(
            "SELECT count(*) FROM " + table + " WHERE status='ready' AND NOT " + has_claim +
            " AND julianday(scheduled_at_utc)>julianday('now') AND NOT (" + unknown + ")"
        ).fetchone()[0])
    return result


def database_snapshot(path: Path, tables: tuple) -> dict:
    with closing(connect_readonly(path)) as connection:
        connection.execute("BEGIN")
        result = {
            "path": str(path),
            "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
            "schema_version": connection.execute("PRAGMA schema_version").fetchone()[0],
            "tables": {table: table_snapshot(connection, table) for table in tables},
        }
        connection.rollback()
    result["blocked"] = any(table["blocked"] for table in result["tables"].values())
    return result


def system_units() -> dict:
    output = subprocess.check_output(
        ["systemctl", "show", *RUNNERS, *TRIGGERS, "-p", "Id", "-p", "LoadState", "-p", "ActiveState",
         "-p", "SubState", "-p", "MainPID"], text=True, timeout=10
    )
    units = {}
    for block in output.strip().split("\n\n"):
        value = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" in value:
            units[value["Id"]] = value
    if set(units) != set(RUNNERS + TRIGGERS):
        raise ValueError("unit inventory incomplete")
    return units


def connection_snapshot(output: str) -> list:
    pairs = set()
    for line in output.splitlines():
        words = line.split()
        if len(words) < 4:
            continue
        local, peer = words[2:4]
        try:
            selected = int(local.rsplit(":", 1)[-1]) in PORTS or int(peer.rsplit(":", 1)[-1]) in PORTS
        except ValueError:
            continue
        if selected:
            pairs.add(tuple(sorted((local, peer))))
    return [list(pair) for pair in sorted(pairs)]


def snapshot() -> dict:
    result = {"at_utc": utc_now(), "databases": {}, "units": system_units()}
    for name, (path, tables) in DATABASES.items():
        result["databases"][name] = database_snapshot(path, tables)
    result["http_connections"] = connection_snapshot(subprocess.check_output(
        ["ss", "-Htn", "state", "established"], text=True, timeout=10
    ))
    inactive = lambda unit: (unit.get("ActiveState") in {"inactive", "failed"} and
                             unit.get("SubState") not in {"start", "stop", "auto-restart"} and
                             unit.get("MainPID", "0") == "0" and unit.get("LoadState") == "loaded")
    result["runners_inactive"] = all(inactive(result["units"][name]) for name in RUNNERS)
    result["triggers_paused"] = all(inactive(result["units"][name]) for name in TRIGGERS)
    result["drained"] = (result["runners_inactive"] and not result["http_connections"] and
                         not any(db["blocked"] for db in result["databases"].values()))
    result["cutover_safe_after_ingress_gate"] = result["drained"] and result["triggers_paused"]
    result["versions"] = {name: str(path.resolve()) for name, path in {
        "legacy": Path("/opt/tt-post/current"), "auto": Path("/opt/tt-auto-post/current")}.items()}
    return result


def publication_facts(value: dict) -> dict:
    return {name: {table: data["publication_facts_sha256"] for table, data in db["tables"].items()}
            for name, db in value["databases"].items()}


def sqlite_backup(source: Path, destination: Path, timeout_seconds: int = 45) -> dict:
    if destination.exists() or destination.with_suffix(destination.suffix + ".partial").exists():
        raise ValueError("backup destination already exists")
    partial = destination.with_suffix(destination.suffix + ".partial")
    fd = os.open(str(partial), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    deadline = time.monotonic() + timeout_seconds

    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise ValueError("SQLite backup deadline exceeded; inspect partial backup")

    with closing(connect_readonly(source, timeout_seconds)) as original:
        with closing(sqlite3.connect(str(partial), timeout=3)) as target:
            original.backup(target, pages=512, progress=progress, sleep=0.1)
            target.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("SQLite backup integrity check failed")
    with partial.open("r+b") as stream:
        os.fsync(stream.fileno())
    os.replace(str(partial), str(destination))
    return {"path": str(destination), "bytes": destination.stat().st_size, "sha256": digest(destination),
            "quick_check": "ok", "method": "sqlite3.Connection.backup from mode=ro source"}


def final_backup(run_id: str, ingress_gate_confirmed: bool) -> dict:
    if not ingress_gate_confirmed:
        raise ValueError("coordinator must confirm the HTTP write gate first")
    run_backup_root(run_id)  # Validate the shared run-ID grammar without creating anything.
    uuid = subprocess.check_output(["findmnt", "-T", "/mnt/data-disk", "-n", "-o", "UUID"],
                                   text=True, timeout=10).strip()
    if uuid != CPU_UUID:
        raise ValueError("CPU data filesystem UUID changed")
    before = snapshot()
    if not before["cutover_safe_after_ingress_gate"]:
        raise ValueError("CPU TT is not drained with triggers paused")
    root = Path("/mnt/data-disk/migrations") / run_id / "tt/cpu-sqlite-final"
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    atomic_json(root / "before.json", before)
    result = {"at_utc": utc_now(), "run_id": run_id, "databases": {}, "ingress_gate_confirmed": True}
    for name, (path, tables) in DATABASES.items():
        target = root / path.name
        result["databases"][name] = sqlite_backup(path, target)
        result["databases"][name]["task_snapshot"] = database_snapshot(target, tables)
    after = snapshot()
    atomic_json(root / "after.json", after)
    backup_facts = {name: {table: data["publication_facts_sha256"] for table, data in
                         db["task_snapshot"]["tables"].items()} for name, db in result["databases"].items()}
    result["publication_facts_stable"] = publication_facts(before) == publication_facts(after) == backup_facts
    result["ok"] = result["publication_facts_stable"] and after["cutover_safe_after_ingress_gate"]
    atomic_json(root / "manifest.json", result)
    if not result["ok"]:
        raise ValueError("publication facts changed during backup; preserve evidence, do not cut over")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("snapshot")
    check.add_argument("--require-drained", action="store_true")
    check.add_argument("--require-paused", action="store_true")
    check.add_argument("--samples", type=int, default=1, choices=(1, 2, 3))
    check.add_argument("--output", type=Path)
    backup = sub.add_parser("backup")
    backup.add_argument("--run-id", required=True)
    backup.add_argument("--ingress-gate-confirmed", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "backup":
            result = final_backup(args.run_id, args.ingress_gate_confirmed)
        else:
            values = []
            for index in range(args.samples):
                if index:
                    time.sleep(2)
                values.append(snapshot())
            result = values[-1]
            result["stable_publication_facts"] = all(publication_facts(item) == publication_facts(result) for item in values)
            result["sample_count"] = len(values)
            if args.output:
                atomic_json(args.output, result)
            print(json.dumps(result, sort_keys=True))
            if args.require_drained and not all(item["drained"] for item in values):
                return 2
            if args.require_paused and not all(item["cutover_safe_after_ingress_gate"] for item in values):
                return 2
            return 0 if result["stable_publication_facts"] else 2
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, ValueError) else "CPU evidence collection failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
