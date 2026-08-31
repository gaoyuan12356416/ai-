#!/usr/bin/env python3
"""Create a read-only CPU checkpoint for fencing only the retired US drama API."""
import argparse
import hashlib
import http.client
import json
import os
import pathlib
import re
import shlex
import socket
import sqlite3
import sys
import time

sys.dont_write_bytecode = True

from maintenance import (BASE, GATE, MAP, RUN_ID, TRIGGERS, atomic_write,
                         gate_text, run, storage_guard)

CPU_HOST = "VM-0-108-centos"
API_UNIT = "drama-material-api.service"
ENV_PATHS = (pathlib.Path("/etc/drama-synthesis/cpu.env"),
             pathlib.Path("/root/drama_material_service/.env"))
DB_PATH = pathlib.Path("/root/drama_material_service/data/drama_material_jobs.sqlite3")
EXPECTED_URL = "http://127.0.0.1:18788"
GPU_KEYS = ("GPU_VIDEO_WORKER_URL", "GPU_VIDEO_WORKER_TOKEN")
HEALTH_BODY = {"ok": True, "role": "media-only"}
JOB_TERMINAL = ("done", "failed", "cancelled")
LEASE_TERMINAL = ("idle", "done", "failed", "missing", "deleted")
CRON_MARKER = "# " + RUN_ID + " PAUSED "
EXPECTED_CONTROL_GROUP = "/system.slice/drama-material-api.service"
SYSTEMD_CGROUP_ROOT = pathlib.Path("/sys/fs/cgroup/systemd")


def unit_state(unit):
    def value(name):
        return run(["systemctl", "show", unit, "-p", name, "--value"]).stdout.strip()
    return {"active": value("ActiveState"), "substate": value("SubState"),
            "pid": int(value("MainPID") or 0),
            "control_pid": int(value("ControlPID") or 0),
            "control_group": value("ControlGroup"),
            "nrestarts": int(value("NRestarts") or 0),
            "start_monotonic": value("ExecMainStartTimestampMonotonic")}


def config_file_identity(path):
    link = path.is_symlink()
    before_link = path.lstat()
    link_target = os.readlink(str(path)) if link else None
    resolved = path.resolve()
    if not resolved.is_file():
        raise RuntimeError("GPU configuration path is not a regular file")
    before = resolved.stat()
    text = resolved.read_text()
    after_link = path.lstat()
    after = resolved.stat()
    link_fields = (before_link.st_dev, before_link.st_ino, before_link.st_size,
                   before_link.st_mtime_ns)
    after_link_fields = (after_link.st_dev, after_link.st_ino, after_link.st_size,
                         after_link.st_mtime_ns)
    fields = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_fields = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if link_fields != after_link_fields or fields != after_fields:
        raise RuntimeError("GPU configuration changed while it was read")
    if link and os.readlink(str(path)) != link_target:
        raise RuntimeError("GPU configuration symlink changed while it was read")
    identity = {"path": str(path), "kind": "symlink" if link else "file",
                "resolved_path": str(resolved), "device": before.st_dev,
                "inode": before.st_ino, "size": before.st_size,
                "mtime_ns": before.st_mtime_ns}
    if link:
        identity["link_target"] = link_target
    return text, identity


def read_gpu_pair(text):
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.strip()
        if key not in GPU_KEYS:
            continue
        parsed = shlex.split(raw, comments=True)
        if len(parsed) != 1 or key in values or not parsed[0]:
            raise RuntimeError("GPU URL/token configuration is missing, duplicated or ambiguous")
        values[key] = parsed[0]
    if set(values) != set(GPU_KEYS):
        raise RuntimeError("GPU URL/token configuration is incomplete")
    return values


def process_start_ticks(pid):
    raw = pathlib.Path("/proc/%d/stat" % pid).read_text()
    end = raw.rfind(")")
    fields = raw[end + 2:].split() if end >= 0 else []
    if len(fields) <= 19:
        raise RuntimeError("running API process identity is unreadable")
    return fields[19]


def process_gpu_pair(pid):
    values = {}
    for item in pathlib.Path("/proc/%d/environ" % pid).read_bytes().split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        try:
            name = key.decode("ascii")
        except UnicodeDecodeError:
            continue
        if name in GPU_KEYS:
            if name in values or not value:
                raise RuntimeError("running API GPU configuration is ambiguous")
            values[name] = value
    if set(values) != set(GPU_KEYS):
        raise RuntimeError("running API GPU configuration is incomplete")
    return values


def inspect_api_configuration():
    before = unit_state(API_UNIT)
    if (before["active"] != "active" or before["substate"] != "running" or
            before["pid"] <= 0 or before["control_pid"] != 0 or
            before["control_group"] != EXPECTED_CONTROL_GROUP):
        raise RuntimeError("CPU main API is not stably running")
    start_ticks = process_start_ticks(before["pid"])
    loaded = [config_file_identity(path) for path in ENV_PATHS]
    configured = [read_gpu_pair(item[0]) for item in loaded]
    if any(values[GPU_KEYS[0]] != EXPECTED_URL for values in configured):
        raise RuntimeError("CPU drama configuration does not point to the HK endpoint")
    running = process_gpu_pair(before["pid"])
    if running[GPU_KEYS[0]] != EXPECTED_URL.encode("utf-8"):
        raise RuntimeError("running CPU API does not point to the HK endpoint")
    tokens = [values[GPU_KEYS[1]].encode("utf-8") for values in configured]
    if tokens[0] != tokens[1] or running[GPU_KEYS[1]] != tokens[0]:
        raise RuntimeError("CPU drama tokens do not match across effective configuration")
    after = unit_state(API_UNIT)
    if after != before or process_start_ticks(after["pid"]) != start_ticks:
        raise RuntimeError("CPU main API changed during configuration inspection")
    return {"unit": API_UNIT, "active": before["active"], "substate": before["substate"],
            "pid": before["pid"], "process_start_ticks": start_ticks,
            "control_pid": before["control_pid"], "control_group": before["control_group"],
            "nrestarts": before["nrestarts"], "start_monotonic": before["start_monotonic"],
            "configuration_files": [item[1] for item in loaded],
            "effective_url": EXPECTED_URL, "both_files_point_to_expected_url": True,
            "tokens_match_without_disclosure": True, "running_environment_matches": True}


def inspect_health():
    connection = http.client.HTTPConnection("127.0.0.1", 18788, timeout=5)
    try:
        connection.request("GET", "/healthz", headers={"Accept": "application/json",
                                                        "Connection": "close",
                                                        "Host": "127.0.0.1:18788"})
        response = connection.getresponse()
        body = response.read(65537)
        if len(body) > 65536:
            raise RuntimeError("HK drama health response is unexpectedly large")
        if response.status != 200:
            raise RuntimeError("HK drama health status is not 200")
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise RuntimeError("HK drama health body is not valid JSON")
        if decoded != HEALTH_BODY:
            raise RuntimeError("HK drama health identity changed")
        return {"url": "http://127.0.0.1:18788/healthz", "method": "GET",
                "status": 200, "body": HEALTH_BODY,
                "body_sha256": hashlib.sha256(body).hexdigest()}
    finally:
        connection.close()


def inspect_gate():
    state_path = BASE / "gates.json"
    state = json.loads(state_path.read_text())
    groups = state.get("groups")
    if not isinstance(groups, list) or len(groups) != len(set(groups)) or "materials" not in groups:
        raise RuntimeError("materials maintenance gate is not recorded active")
    expected_map, expected_gate = gate_text(set(groups))
    if MAP.read_text() != expected_map or GATE.read_text() != expected_gate:
        raise RuntimeError("active nginx maintenance gate differs from its recorded groups")
    nginx = unit_state("nginx.service")
    if nginx["active"] != "active" or nginx["substate"] != "running" or nginx["pid"] <= 0:
        raise RuntimeError("nginx maintenance gate is not active")
    return {"groups": sorted(groups), "materials_active": True,
            "map_sha256": hashlib.sha256(expected_map.encode("utf-8")).hexdigest(),
            "gate_sha256": hashlib.sha256(expected_gate.encode("utf-8")).hexdigest(),
            "nginx_active": True, "nginx_pid": nginx["pid"]}


def paused_crontab(before, current):
    lines = before.splitlines()
    candidates = [index for index, line in enumerate(lines)
                  if not line.lstrip().startswith("#") and "run_auto_cover_synthesis.sh" in line]
    if len(candidates) != 1:
        raise RuntimeError("original screenshot cron evidence is ambiguous")
    lines[candidates[0]] = CRON_MARKER + lines[candidates[0]]
    expected = "\n".join(lines) + "\n"
    if current != expected:
        raise RuntimeError("screenshot cron is not the exact recorded paused form")
    live = [line for line in current.splitlines()
            if not line.lstrip().startswith("#") and "run_auto_cover_synthesis.sh" in line]
    if live:
        raise RuntimeError("screenshot cron remains active")
    return expected


def inspect_pause():
    pause_path = BASE / "materials-triggers.json"
    pause = json.loads(pause_path.read_text())
    if pause.get("restored") is not False:
        raise RuntimeError("materials pause record was restored or is ambiguous")
    original = pause.get("original")
    if not isinstance(original, dict) or set(original) != set(TRIGGERS["materials"]):
        raise RuntimeError("materials pause trigger scope changed")
    services = {unit: unit_state(unit) for unit in TRIGGERS["materials"]}
    for unit, state in services.items():
        if state["active"] != "inactive" or state["substate"] != "dead" or state["pid"] != 0:
            raise RuntimeError("paused test service is still active: " + unit)
    before = (BASE / "materials-crontab-before.txt").read_text()
    current = run(["crontab", "-l"]).stdout
    paused_crontab(before, current)
    return {"record_restored": False, "test_services": services,
            "cron_paused": True,
            "cron_before_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(),
            "cron_current_sha256": hashlib.sha256(current.encode("utf-8")).hexdigest()}


def status_counts(connection, table):
    return {str(status): int(count) for status, count in
            connection.execute("SELECT status,COUNT(*) FROM " + table + " GROUP BY status")}


def table_columns(connection, table):
    return [str(row[1]) for row in connection.execute("PRAGMA table_info(" + table + ")")]


def inspect_database(path=DB_PATH):
    if not path.is_file():
        raise RuntimeError("drama SQLite database is missing")
    connection = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("drama SQLite quick_check failed")
        required = ("drama_material_job", "drama_material_job_worker_lease")
        existing = {str(row[0]) for row in
                    connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if any(table not in existing for table in required):
            raise RuntimeError("drama job or lease table is missing")
        jobs = status_counts(connection, required[0])
        leases = status_counts(connection, required[1])
        active_jobs = sum(count for status, count in jobs.items() if status not in JOB_TERMINAL)
        active_leases = sum(count for status, count in leases.items() if status not in LEASE_TERMINAL)
        unknown_columns = ["%s.%s" % (table, column) for table in required
                           for column in table_columns(connection, table)
                           if "unknown" in column.lower()]
        unknown_statuses = sum(count for status, count in list(jobs.items()) + list(leases.items())
                               if status.strip().lower() in ("unknown", "needs_review") or
                               "unknown" in status.strip().lower())
        if active_jobs:
            raise RuntimeError("drama jobs have not drained")
        if active_leases:
            raise RuntimeError("drama worker leases have not drained")
        if unknown_columns or unknown_statuses:
            raise RuntimeError("drama schema or state gained unknown-outcome semantics")
        return {"path": str(path), "quick_check": "ok", "job_status_counts": jobs,
                "lease_status_counts": leases, "active_jobs": 0, "active_leases": 0,
                "unknown_semantics": "not_applicable_and_absent", "no_unknown": True}
    finally:
        connection.close()


def endpoint_connection_counts():
    connected = run(["ss", "-Hntp", "state", "connected"]).stdout.splitlines()
    established = run(["ss", "-Hntp", "state", "established"]).stdout.splitlines()
    return {"legacy_18787_connected": sum(1 for line in connected if re.search(r":18787\b", line)),
            "legacy_18787_established": sum(1 for line in established if re.search(r":18787\b", line)),
            "hk_18788_established_after_health": sum(
                1 for line in established if re.search(r":18788\b", line))}


def task_children(pid):
    children = set()
    task_root = pathlib.Path("/proc/%d/task" % pid)
    for task in task_root.iterdir():
        if not task.name.isdigit():
            continue
        raw = (task / "children").read_text().split()
        children.update(int(value) for value in raw)
    return children


def process_descendants(pid):
    found = set()
    pending = [pid]
    while pending:
        parent = pending.pop()
        for child in task_children(parent):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def process_category(pid):
    comm = pathlib.Path("/proc/%d/comm" % pid).read_text().strip().lower()
    cmdline = pathlib.Path("/proc/%d/cmdline" % pid).read_bytes().lower()
    if comm in ("ffmpeg", "ffprobe"):
        return comm
    if comm == "codex" or b"codex" in cmdline:
        return "codex"
    return "other"


def inspect_api_process_scope(api):
    if api["control_group"] != EXPECTED_CONTROL_GROUP:
        raise RuntimeError("CPU main API cgroup identity changed")
    cgroup_file = SYSTEMD_CGROUP_ROOT / api["control_group"].lstrip("/") / "cgroup.procs"
    if cgroup_file.is_symlink() or not cgroup_file.is_file():
        raise RuntimeError("CPU main API systemd cgroup.procs is unavailable")
    cgroup_pids = sorted({int(value) for value in cgroup_file.read_text().split()})
    descendants = sorted(process_descendants(api["pid"]))
    related = sorted((set(cgroup_pids) | set(descendants)) - {api["pid"]})
    categories = {"ffmpeg": 0, "ffprobe": 0, "codex": 0, "other": 0}
    for pid in related:
        categories[process_category(pid)] += 1
    if api["pid"] not in cgroup_pids:
        raise RuntimeError("CPU main API MainPID is absent from its systemd cgroup")
    if related:
        raise RuntimeError("CPU main API cgroup or process tree still has drama-related children")
    return {"cgroup_version": 1, "controller": "systemd",
            "control_group": EXPECTED_CONTROL_GROUP, "main_pid": api["pid"],
            "cgroup_pids": cgroup_pids, "descendant_pids": descendants,
            "drama_related_child_categories": categories,
            "host_wide_process_scan_performed": False,
            "unrelated_host_media_processes_ignored": True}


def inspect_drained_samples(samples=3, interval=0.5):
    results = []
    for index in range(samples):
        database = inspect_database()
        connections = endpoint_connection_counts()
        if connections["legacy_18787_connected"] or connections["legacy_18787_established"]:
            raise RuntimeError("legacy US drama endpoint still has connections")
        if connections["hk_18788_established_after_health"]:
            raise RuntimeError("HK drama endpoint still has a business HTTP connection")
        results.append({"database": database, "connections": connections})
        if index + 1 < samples:
            time.sleep(interval)
    stable = [{"job_status_counts": row["database"]["job_status_counts"],
               "lease_status_counts": row["database"]["lease_status_counts"],
               "active_jobs": row["database"]["active_jobs"],
               "active_leases": row["database"]["active_leases"],
               "no_unknown": row["database"]["no_unknown"],
               "connections": row["connections"]} for row in results]
    if any(row != stable[0] for row in stable[1:]):
        raise RuntimeError("drama drain state changed across read-only samples")
    return results[-1]["database"], {"sample_count": samples,
                                     "interval_seconds": interval,
                                     "stable": True}


def inspection_pass():
    gate = inspect_gate()
    pause = inspect_pause()
    api = inspect_api_configuration()
    health = inspect_health()
    database, drain_samples = inspect_drained_samples()
    process_scope = inspect_api_process_scope(api)
    # Reassert process identity after all HTTP, SQLite, cgroup, and connection checks.
    final_api = unit_state(API_UNIT)
    if (final_api["pid"] != api["pid"] or final_api["active"] != api["active"] or
            final_api["substate"] != api["substate"] or
            final_api["control_pid"] != api["control_pid"] or
            final_api["control_group"] != api["control_group"] or
            final_api["nrestarts"] != api["nrestarts"] or
            final_api["start_monotonic"] != api["start_monotonic"] or
            process_start_ticks(final_api["pid"]) != api["process_start_ticks"]):
        raise RuntimeError("CPU main API changed during a drain verification pass")
    return {"materials_gate": gate, "materials_pause": pause, "cpu_api": api,
            "hk_health": health, "database": database, "drain_samples": drain_samples,
            "process_scope": process_scope}


def inspect():
    storage_guard()
    if socket.gethostname() != CPU_HOST:
        raise RuntimeError("CPU-only drama drain check")
    first = inspection_pass()
    second = inspection_pass()
    if first != second:
        raise RuntimeError("drama gate, pause, configuration, process, connection, or database state changed between verification passes")
    canonical = json.dumps(second, sort_keys=True, separators=(",", ":")).encode("utf-8")
    checked = time.time()
    return {"run_id": RUN_ID, "group": "drama", "coordinator_host": CPU_HOST,
            "checked_at_epoch": checked,
            "ready": True, "new_admission_closed": True, "triggers_paused": True,
            "cpu_drained": True, "no_unknown": True,
            "materials_gate": second["materials_gate"],
            "materials_pause": second["materials_pause"],
            "cpu_api": second["cpu_api"], "hk_health": second["hk_health"],
            "database": second["database"], "drain_samples": second["drain_samples"],
            "process_scope": second["process_scope"],
            "stability": {"verification_passes": 2, "identical": True,
                          "critical_snapshot_sha256": hashlib.sha256(canonical).hexdigest()},
            "legacy_18787_connections": 0,
            "legacy_18787_established_connections": 0,
            "hk_18788_business_http_connections": 0,
            "health_get_requests_completed": 2,
            "business_requests_sent": 0,
            "inspection_policy": "two identical read-only passes; systemd cgroup v1, SQLite, sockets, and GET /healthz only"}


def output_path(value):
    target = value.resolve(strict=False)
    base = BASE.resolve(strict=False)
    if target.suffix != ".json" or base not in target.parents:
        raise RuntimeError("evidence must stay within the CPU data-disk private control directory")
    if target.exists():
        raise RuntimeError("refuse to overwrite existing drama drain evidence")
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    target = output_path(args.output)
    result = inspect()
    BASE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(BASE), 0o700)
    atomic_write(target, json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
