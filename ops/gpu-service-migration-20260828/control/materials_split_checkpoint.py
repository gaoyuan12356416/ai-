#!/usr/bin/env python3
"""Create fresh, non-secret evidence for the split image source fence.

``snapshot-us`` runs the existing read-only source-fence inspection on the US
host after the legacy tunnels have stopped and the temporary ad-only lane is
stable.  ``checkpoint-cpu`` consumes that private snapshot on the CPU host and
creates the short-lived checkpoint accepted by ``source_fence.py
materials-images --apply``.  Neither mode starts, stops, enables, masks or
restarts a service and neither mode reads a private key or application token.
"""
import argparse
import contextlib
import hashlib
import http.client
import json
import os
import pathlib
import re
import socket
import sqlite3
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True

RUN_ID = "gpu-service-migration-20260828T1502"
US_HOST = "VM-0-13-centos"
CPU_HOST = "VM-0-108-centos"
US_ADDRESS = "43.166.178.132"
CPU_DISK = pathlib.Path("/mnt/data-disk")
CPU_DISK_UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
DATABASE = pathlib.Path("/root/drama_material_service/data/drama_material_jobs.sqlite3")
SOURCE_FENCE = pathlib.Path(__file__).with_name("source_fence.py")
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
US_OUTPUT = pathlib.Path("/data/migrations") / RUN_ID / "materials-images-checkpoints"

IMAGE_SOURCE_UNITS = (
    "codex-cover-generator.service",
    "codex-screenshot-batch.service",
    "codex-screenshot-batch-burst.service",
    "codex-screenshot-square.service",
    "codex-screenshot-portrait.service",
    "codex-screenshot-landscape.service",
)
LEGACY_TUNNELS = (
    "gpu-worker-reverse-tunnel.service",
    "gpu-screenshot-batch-burst-tunnel.service",
)
AD_UNITS = ("ad-material-generation.service", "ad-material-vision.service")
AD_ONLY_TUNNEL = "gpu-ad-only-reverse-tunnel.service"
CPU_IMAGE_UNITS = {
    18790: "codex-cover-migrated.service",
    18795: "codex-screenshot-migrated-primary.service",
    18798: "codex-screenshot-migrated-burst.service",
}
CPU_EXISTING_IMAGE_UNITS = (
    "codex-cover-generator.service",
    "codex-screenshot-batch.service",
    "codex-screenshot-batch-burst.service",
)
PATH_TARGETS = {
    pathlib.Path("/usr/share/nginx/html/drama-screenshot-materials"):
        pathlib.Path("/mnt/data-disk/codex-workers/us-migrated/storage/drama-screenshot-materials"),
    pathlib.Path("/usr/share/nginx/html/drama-materials"):
        pathlib.Path("/mnt/data-disk/codex-workers/us-migrated/storage/drama-materials"),
    pathlib.Path("/root/drama_material_jobs"):
        pathlib.Path("/mnt/data-disk/codex-workers/us-migrated/storage/drama-material-jobs"),
}
SCREENSHOT_JOBS = pathlib.Path("/root/drama_screenshot_jobs")
CHECKPOINT_MAX_AGE = 120


def command(args, check=True, timeout=30):
    result = subprocess.run([str(value) for value in args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True,
                            timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("command failed: %s" % pathlib.Path(str(args[0])).name)
    return result


def property_value(unit, name):
    return command(["systemctl", "show", unit, "-p", name, "--value",
                    "--no-pager"]).stdout.strip()


def exact_checkout():
    head = command(["git", "-C", REPO_ROOT, "rev-parse", "HEAD"]).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise RuntimeError("control checkout has no exact commit")
    for args in (["git", "-C", REPO_ROOT, "diff", "--quiet", "--", "."],
                 ["git", "-C", REPO_ROOT, "diff", "--cached", "--quiet", "--", "."]):
        if command(args, check=False).returncode:
            raise RuntimeError("control checkout has tracked changes")
    return head


def process_start_ticks(pid):
    raw = pathlib.Path("/proc/%d/stat" % pid).read_text()
    return int(raw[raw.rfind(")") + 2:].split()[19])


def process_snapshot(pid):
    status = pathlib.Path("/proc/%d/status" % pid).read_text()
    threads = int(re.search(r"^Threads:\s+(\d+)", status, re.M).group(1))
    children = pathlib.Path("/proc/%d/task/%d/children" % (pid, pid)).read_text().split()
    return {"pid": pid, "pid_start_ticks": process_start_ticks(pid),
            "threads": threads, "children": [int(value) for value in children]}


def cgroup_pids(control_group):
    for root in (pathlib.Path("/sys/fs/cgroup/systemd"), pathlib.Path("/sys/fs/cgroup")):
        target = root / control_group.lstrip("/") / "cgroup.procs"
        if target.is_file():
            return [int(value) for value in target.read_text().split()]
    raise RuntimeError("service cgroup is unavailable")


def unit_process(unit):
    pid = int(property_value(unit, "MainPID") or 0)
    if pid <= 0:
        raise RuntimeError("required unit has no MainPID: " + unit)
    control_group = property_value(unit, "ControlGroup")
    result = process_snapshot(pid)
    result.update({
        "unit": unit,
        "active": property_value(unit, "ActiveState"),
        "substate": property_value(unit, "SubState"),
        "enabled": property_value(unit, "UnitFileState"),
        "control_pid": int(property_value(unit, "ControlPID") or 0),
        "control_group": control_group,
        "cgroup_pids": cgroup_pids(control_group),
        "nrestarts": int(property_value(unit, "NRestarts") or 0),
        "start_monotonic": property_value(unit, "ExecMainStartTimestampMonotonic"),
        "active_enter_monotonic": property_value(unit, "ActiveEnterTimestampMonotonic"),
    })
    return result


def http_json(port, path, method="GET"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=b"" if method != "GET" else None,
                           headers={"Host": "ai.yingliangads.com",
                                    "Accept": "application/json"})
        response = connection.getresponse()
        body = response.read(65537)
        if len(body) > 65536:
            raise RuntimeError("HTTP response is unexpectedly large")
        try:
            decoded = json.loads(body.decode("utf-8"))
        except Exception:
            raise RuntimeError("HTTP response is not JSON")
        return {"status": response.status, "body": decoded}
    finally:
        connection.close()


def target_connections(ports):
    output = command(["ss", "-Hntp", "state", "established"]).stdout
    pattern = re.compile(r":(?:%s)\b" % "|".join(str(port) for port in ports))
    return [line for line in output.splitlines() if pattern.search(line)]


def validate_ad_baseline(value):
    if not isinstance(value, dict) or not isinstance(value.get("services"), list):
        raise RuntimeError("US ad baseline is missing")
    rows = value["services"] + [value.get("tunnel")]
    if any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("US ad baseline row is invalid")
    if [row.get("unit") for row in value["services"]] != list(AD_UNITS):
        raise RuntimeError("US ad service baseline scope changed")
    if value["tunnel"].get("unit") != AD_ONLY_TUNNEL:
        raise RuntimeError("US ad tunnel baseline scope changed")
    for row in rows:
        pid = row.get("pid")
        if (row.get("active") != "active" or row.get("substate") != "running" or
                type(pid) is not int or pid <= 0 or row.get("control_pid") != 0 or
                row.get("pid_start_ticks", 0) <= 0 or pid not in row.get("cgroup_pids", []) or
                not str(row.get("start_monotonic", "")).isdigit() or
                int(row["start_monotonic"]) <= 0 or
                not str(row.get("active_enter_monotonic", "")).isdigit() or
                int(row["active_enter_monotonic"]) <= 0 or
                type(row.get("nrestarts")) is not int or row["nrestarts"] < 0 or
                not re.fullmatch(r"[0-9a-f]{64}", row.get("unit_sha256", ""))):
            raise RuntimeError("US ad baseline identity is not stable")
        if row.get("cgroup_pids") != [pid]:
            raise RuntimeError("US ad lane has an unexpected child process")
    if value["tunnel"].get("enabled") != "enabled":
        raise RuntimeError("US ad-only tunnel is not enabled")
    return value


def validate_source_dry_run(value):
    if value.get("dry_run") is not True or value.get("group") != "materials-images":
        raise RuntimeError("US source dry run has the wrong scope")
    states = value.get("states")
    if not isinstance(states, list) or set(row.get("unit") for row in states) != \
            set(IMAGE_SOURCE_UNITS + LEGACY_TUNNELS):
        raise RuntimeError("US source dry run unit scope changed")
    by_unit = {row["unit"]: row for row in states}
    for unit in IMAGE_SOURCE_UNITS:
        row = by_unit[unit]
        if row.get("active") == "active":
            if (row.get("pid", 0) <= 0 or row.get("threads") != 1 or row.get("children") != []):
                raise RuntimeError("US image source is not idle: " + unit)
        elif (row.get("active") != "inactive" or row.get("substate") not in ("dead", "failed") or
              row.get("pid") != 0 or row.get("control_pid") != 0):
            raise RuntimeError("US image source state is invalid: " + unit)
    for unit in LEGACY_TUNNELS:
        row = by_unit[unit]
        if (row.get("active") != "inactive" or row.get("substate") not in ("dead", "failed") or
                row.get("pid") != 0 or row.get("control_pid") != 0):
            raise RuntimeError("legacy source tunnel has not stopped: " + unit)
    validate_ad_baseline(value.get("us_ad_baseline"))
    return value


def validate_us_envelope(value, expected_commit, now=None):
    now = time.time() if now is None else now
    if (value.get("schema_version") != 1 or value.get("run_id") != RUN_ID or
            value.get("source_host") != US_HOST or value.get("control_commit") != expected_commit):
        raise RuntimeError("US materials snapshot identity changed")
    age = now - float(value.get("checked_at_epoch", 0))
    if age < -5 or age > CHECKPOINT_MAX_AGE:
        raise RuntimeError("US materials snapshot is stale")
    dry_run = validate_source_dry_run(value.get("source_fence_dry_run", {}))
    samples = value.get("ad_idle_samples")
    if not isinstance(samples, list) or len(samples) != 2 or samples[0] != samples[1]:
        raise RuntimeError("US ad idle samples are missing or changed")
    baseline = {row["unit"]: row for row in
                dry_run["us_ad_baseline"]["services"] + [dry_run["us_ad_baseline"]["tunnel"]]}
    for unit in AD_UNITS + (AD_ONLY_TUNNEL,):
        row = samples[0].get(unit, {})
        expected = baseline[unit]
        if (row.get("pid") != expected["pid"] or
                row.get("pid_start_ticks") != expected["pid_start_ticks"] or
                row.get("cgroup_pids") != [expected["pid"]] or row.get("threads") != 1 or
                row.get("children") != [] or row.get("nrestarts") != expected["nrestarts"] or
                row.get("start_monotonic") != expected["start_monotonic"] or
                row.get("active_enter_monotonic") != expected["active_enter_monotonic"]):
            raise RuntimeError("US ad idle identity changed: " + unit)
    if value.get("established_samples") != [[], []]:
        raise RuntimeError("US ad requests have not drained")
    if value.get("health") != {
            "8796": {"status": 200, "body": {"ok": True, "service": "ad-material-vision"}},
            "8797": {"status": 200, "body": {"ok": True, "service": "ad-material-generation"}}}:
        raise RuntimeError("US ad service health identity changed")
    return value


def secure_us_output():
    if socket.gethostname() != US_HOST or os.geteuid() != 0:
        raise RuntimeError("US root host required")
    target = command(["findmnt", "-rn", "-o", "TARGET", "-T", "/data"]).stdout.strip()
    if target != "/data" or not os.access("/data", os.W_OK):
        raise RuntimeError("US data disk is unavailable")
    US_OUTPUT.mkdir(parents=True, exist_ok=True)
    os.chmod(str(US_OUTPUT), 0o700)
    if US_OUTPUT.resolve(strict=True) != US_OUTPUT:
        raise RuntimeError("US checkpoint directory resolves through a symlink")
    return US_OUTPUT


def exclusive_json(path, value):
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise RuntimeError("private evidence write did not make progress")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return hashlib.sha256(data).hexdigest()


def us_ad_idle_snapshot():
    result = {unit: unit_process(unit) for unit in AD_UNITS + (AD_ONLY_TUNNEL,)}
    for unit, row in result.items():
        if (row["active"] != "active" or row["substate"] != "running" or
                row["control_pid"] != 0 or row["threads"] != 1 or row["children"] or
                row["cgroup_pids"] != [row["pid"]]):
            raise RuntimeError("US ad process is not idle: " + unit)
    return result


def snapshot_us():
    commit = exact_checkout()
    output_dir = secure_us_output()
    inspected = command([sys.executable, SOURCE_FENCE, "materials-images"], timeout=60)
    dry_run = validate_source_dry_run(json.loads(inspected.stdout))
    health = {"8796": http_json(8796, "/health"), "8797": http_json(8797, "/health")}
    first_connections = target_connections((8796, 8797))
    first = us_ad_idle_snapshot()
    time.sleep(1)
    second_connections = target_connections((8796, 8797))
    second = us_ad_idle_snapshot()
    envelope = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "source_host": US_HOST,
        "control_commit": commit,
        "checked_at_epoch": time.time(),
        "source_fence_dry_run": dry_run,
        "health": health,
        "established_samples": [first_connections, second_connections],
        "ad_idle_samples": [first, second],
        "credentials_read": False,
        "service_mutations": False,
    }
    validate_us_envelope(envelope, commit)
    path = output_dir / ("us-materials-images-%s-%s.json" %
                         (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), uuid.uuid4().hex))
    digest = exclusive_json(path, envelope)
    print(json.dumps({"path": str(path), "sha256": digest,
                      "checked_at_epoch": envelope["checked_at_epoch"]}, sort_keys=True))


def database_status():
    result = {}
    with sqlite3.connect("file:%s?mode=ro" % DATABASE, uri=True, timeout=5) as connection:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("CPU SQLite quick_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("CPU SQLite foreign-key violations exist")
        for table in ("drama_screenshot_job", "drama_material_job",
                      "drama_material_job_worker_lease", "ad_material_task",
                      "ad_material_asset"):
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                  (table,)).fetchone():
                result[table] = dict(connection.execute(
                    "SELECT status,COUNT(*) FROM " + table + " GROUP BY status"))
    if any(status not in ("done", "failed")
           for status in result.get("drama_screenshot_job", {})):
        raise RuntimeError("CPU screenshot jobs have not drained")
    if any(status not in ("done", "failed", "cancelled")
           for status in result.get("drama_material_job", {})):
        raise RuntimeError("CPU drama jobs have not drained")
    if result.get("drama_material_job_worker_lease", {}).get("running", 0):
        raise RuntimeError("CPU drama worker lease has not drained")
    if (result.get("ad_material_task", {}).get("generating_demand", 0) or
            result.get("ad_material_task", {}).get("generating_material", 0) or
            result.get("ad_material_asset", {}).get("regenerating", 0)):
        raise RuntimeError("CPU ad work has not drained")
    if any("unknown" in status.lower() for counts in result.values() for status in counts):
        raise RuntimeError("CPU material database contains an unknown status")
    return result


def listener_rows():
    output = command(["ss", "-Hlntp"]).stdout
    result = {}
    for port in tuple(CPU_IMAGE_UNITS) + (18796, 18797):
        rows = [line for line in output.splitlines()
                if re.search(r"127\.0\.0\.1:%d\b" % port, line)]
        if len(rows) != 1:
            raise RuntimeError("CPU port does not have one loopback owner: %d" % port)
        matches = re.findall(r"pid=(\d+)", rows[0])
        if len(set(matches)) != 1:
            raise RuntimeError("CPU port owner PID is ambiguous: %d" % port)
        process = re.search(r'users:\(\(\"([^\"]+)\"', rows[0])
        if not process:
            raise RuntimeError("CPU port owner process is missing: %d" % port)
        result[port] = {"pid": int(matches[0]), "process": process.group(1),
                        "loopback": True}
    return result


def verify_path_switches():
    for source, target in PATH_TARGETS.items():
        if (not source.is_symlink() or source.resolve(strict=True) != target.resolve(strict=True) or
                not target.is_dir()):
            raise RuntimeError("CPU image storage path has not switched: " + str(source))
    if not SCREENSHOT_JOBS.is_symlink():
        raise RuntimeError("CPU screenshot jobs compatibility link is missing")
    resolved = SCREENSHOT_JOBS.resolve(strict=True)
    disk = CPU_DISK.resolve(strict=True)
    if not resolved.is_dir() or (resolved != disk and disk not in resolved.parents):
        raise RuntimeError("CPU screenshot jobs link escaped the data disk")
    for path in tuple(PATH_TARGETS) + (SCREENSHOT_JOBS,):
        fields = command(["findmnt", "-rn", "-o", "TARGET,UUID", "-T", path]).stdout.split()
        if fields != [str(CPU_DISK), CPU_DISK_UUID]:
            raise RuntimeError("CPU image path is not on the approved data disk")
    return {str(source): str(target) for source, target in PATH_TARGETS.items()}


def verify_ssh_owner(pid):
    output = command(["ss", "-Hntp", "state", "established"]).stdout
    matches = [line for line in output.splitlines()
               if re.search(r"\b%s:\d+\b" % re.escape(US_ADDRESS), line) and
               re.search(r"pid=%d\b" % pid, line) and re.search(r":22\b", line)]
    if len(matches) != 1:
        raise RuntimeError("CPU ad tunnel SSH owner is not the US host")
    return {"pid": pid, "remote_address": US_ADDRESS, "connections": 1}


def verify_gate_and_pause(maintenance):
    gates = json.loads(maintenance.read_private_text(maintenance.BASE / "gates.json"))
    groups = gates.get("groups")
    if (not isinstance(groups, list) or "materials" not in groups or
            len(groups) != len(set(groups)) or any(group not in maintenance.PATTERNS for group in groups)):
        raise RuntimeError("CPU materials gate state changed")
    expected_map, expected_gate = maintenance.gate_text(groups)
    if (maintenance.read_optional_regular_bytes(maintenance.MAP) != expected_map.encode() or
            maintenance.read_optional_regular_bytes(maintenance.GATE) != expected_gate.encode()):
        raise RuntimeError("CPU materials gate configuration drifted")
    probes = {
        "GET batch": http_json(80, "/api/drama-screenshot-material/jobs/batch", "GET"),
        "POST screenshot": http_json(80, "/api/drama-screenshot-material/jobs", "POST"),
        "POST ad": http_json(80, "/api/ad-material/tasks", "POST"),
    }
    if any(row != {"status": 503, "body": {
            "error": "service_migration_maintenance", "message": "业务迁移中，请稍后重试"}}
           for row in probes.values()):
        raise RuntimeError("CPU materials admission probe was not blocked")
    state = maintenance.normalize_journal("materials", json.loads(
        maintenance.read_private_text(maintenance.BASE / "materials-triggers.json")))
    current = maintenance.current_snapshot("materials", state)
    if (state.get("phase") != "paused" or state.get("restored") is not False or
            state.get("errors") not in (None, []) or not maintenance.paused_units(current["units"]) or
            current.get("cron") != "paused"):
        raise RuntimeError("CPU materials trigger pause changed")
    return {"groups": groups, "probes": probes, "pause_revision": state.get("revision"),
            "trigger_state": current}


def collect_cpu_observation(maintenance):
    gate = verify_gate_and_pause(maintenance)
    status_counts = database_status()
    paths = verify_path_switches()
    listeners = listener_rows()
    image_units = {}
    for port, unit in CPU_IMAGE_UNITS.items():
        row = unit_process(unit)
        if (row["active"] != "active" or row["substate"] != "running" or
                row["enabled"] != "enabled" or
                row["control_pid"] != 0 or row["threads"] != 1 or row["children"] or
                row["cgroup_pids"] != [row["pid"]] or listeners[port]["pid"] != row["pid"] or
                listeners[port]["process"] == "sshd"):
            raise RuntimeError("CPU image unit is not singly idle: " + unit)
        health = http_json(port, "/healthz")
        if health["status"] != 200 or health["body"].get("status") != "ok":
            raise RuntimeError("CPU image health failed: " + unit)
        image_units[unit] = {"identity": row, "port": port, "health": health}
    existing_units = {}
    for unit in CPU_EXISTING_IMAGE_UNITS:
        row = unit_process(unit)
        if (row["active"] != "active" or row["substate"] != "running" or
                row["threads"] != 1 or row["children"] or row["cgroup_pids"] != [row["pid"]]):
            raise RuntimeError("existing CPU image unit is not idle: " + unit)
        existing_units[unit] = row
    ad_pid = listeners[18796]["pid"]
    if (listeners[18797]["pid"] != ad_pid or listeners[18796]["process"] != "sshd" or
            listeners[18797]["process"] != "sshd"):
        raise RuntimeError("CPU ad ports are not owned by one SSH session")
    ssh_owner = verify_ssh_owner(ad_pid)
    ad_health = {"18796": http_json(18796, "/health"),
                 "18797": http_json(18797, "/health")}
    if ad_health != {
            "18796": {"status": 200, "body": {"ok": True, "service": "ad-material-vision"}},
            "18797": {"status": 200, "body": {"ok": True, "service": "ad-material-generation"}}}:
        raise RuntimeError("CPU ad lane health identity changed")
    established = target_connections((8790, 8795, 8798, 18790, 18795, 18796, 18797, 18798))
    if established:
        raise RuntimeError("CPU material requests have not drained")
    return {"gate": gate, "status_counts": status_counts, "paths": paths,
            "image_units": image_units, "existing_image_units": existing_units,
            "listeners": {str(port): value for port, value in listeners.items()},
            "ad_ssh_owner": ssh_owner, "ad_health": ad_health,
            "affected_established": established}


def validate_cpu_observation(value):
    required = ("gate", "status_counts", "paths", "image_units",
                "existing_image_units", "listeners", "ad_ssh_owner", "ad_health")
    if not isinstance(value, dict) or any(key not in value for key in required):
        raise RuntimeError("CPU materials observation is incomplete")
    if value.get("affected_established") != []:
        raise RuntimeError("CPU materials observation has active requests")
    return value


def build_checkpoint(us_envelope, cpu_observation, now=None):
    now = time.time() if now is None else now
    baseline = us_envelope["source_fence_dry_run"]["us_ad_baseline"]
    validate_cpu_observation(cpu_observation)
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "group": "materials-images",
        "checked_at_epoch": now,
        "coordinator_host": CPU_HOST,
        "ready": True,
        "new_admission_closed": True,
        "triggers_paused": True,
        "cpu_drained": True,
        "no_unknown": True,
        "split_mode": "us-ad-only",
        "ad_requests_drained": True,
        "legacy_shared_tunnel_stopped": True,
        "legacy_burst_tunnel_stopped": True,
        "cpu_image_ports_owned_by_local_units": True,
        "cpu_ad_ports_owned_by_us_ad_only_tunnel": True,
        "ad_services_healthy": True,
        "us_ad_baseline": baseline,
        "control_commit": us_envelope["control_commit"],
        "source_snapshot_checked_at_epoch": us_envelope["checked_at_epoch"],
        "cpu_observation_sha256": hashlib.sha256(json.dumps(
            cpu_observation, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "credentials_read": False,
        "service_mutations": False,
    }


def checkpoint_cpu(us_snapshot):
    if socket.gethostname() != CPU_HOST or os.geteuid() != 0:
        raise RuntimeError("CPU root host required")
    commit = exact_checkout()
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import maintenance
    maintenance.storage_guard()
    with maintenance.control_transaction():
        snapshot_dir = maintenance.secure_snapshot_directory()
        source_path = pathlib.Path(us_snapshot)
        if (not source_path.is_absolute() or source_path.is_symlink() or
                source_path.parent.resolve(strict=True) != snapshot_dir):
            raise RuntimeError("US snapshot must be a private CPU snapshot artifact")
        maintenance.validate_private_file(source_path)
        envelope = json.loads(maintenance.read_private_text(source_path))
        validate_us_envelope(envelope, commit)
        first = collect_cpu_observation(maintenance)
        time.sleep(1)
        second = collect_cpu_observation(maintenance)
        if first != second:
            raise RuntimeError("CPU materials state changed between final passes")
        validate_us_envelope(envelope, commit)
        checkpoint = build_checkpoint(envelope, second)
        output = snapshot_dir / ("materials-images-checkpoint-%s-%s.json" %
                                 (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                                  uuid.uuid4().hex))
        maintenance.atomic_write(output, json.dumps(
            checkpoint, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(json.dumps({"path": str(output), "sha256": digest,
                      "checked_at_epoch": checkpoint["checked_at_epoch"]}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    # ``required=`` on subparsers is unavailable on the US host's Python 3.6.
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("snapshot-us")
    cpu = sub.add_parser("checkpoint-cpu")
    cpu.add_argument("--us-snapshot", required=True)
    args = parser.parse_args()
    if args.action is None:
        parser.error("an action is required")
    if args.action == "snapshot-us":
        snapshot_us()
    else:
        checkpoint_cpu(args.us_snapshot)


if __name__ == "__main__":
    main()
