#!/usr/bin/env python3
"""Deploy the exact reviewed drama release on CPU or HK; dry-run by default.

No command in this file publishes, retries or resumes a drama job.  The CPU
operation replaces only app.py and features/drama_synthesis/async_runtime.py.
The HK operation creates one immutable Git release and atomically changes the
current symlink.  Every apply is bound to the approved migration run, hosts,
commits, data roots, filesystem device and exact systemd unit names.
"""
from __future__ import print_function

import argparse
import contextlib
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import stat
import sys
import time

import drama_operator_common as common


JOB_IDS = (
    "679e7c49acbf4af79f78bf60d76c5dd7",
    "b6e0bc51bb3f44e19c12b20cef7b93fe",
)
CPU_DB = common.CPU_LIVE_ROOT / "data" / "drama_material_jobs.sqlite3"
HK_RELEASES = common.HK_BASE / "releases"
HK_CURRENT = common.HK_BASE / "current"
HK_WORK_ROOT = common.HK_BASE / "work" / "jobs"
HK_PUBLIC_ROOT = common.HK_BASE / "results" / "public"
HK_RUNTIME_ACTIVE = HK_WORK_ROOT / ".runtime" / "jobs"
HK_RUNTIME_DIAGNOSTICS = HK_WORK_ROOT / ".runtime" / "diagnostics"
MIN_FREE_BYTES = 30 * 1024 * 1024 * 1024


def role_contract(role):
    if role == "cpu":
        return {
            "host": common.CPU_HOST,
            "data_root": common.CPU_DATA_ROOT,
            "source_root": common.CPU_DATA_ROOT / "migrations" / common.RUN_ID /
                           "drama-release" / "source" / common.NEW_SHA,
            "target_units": common.CPU_TARGET_UNITS,
            "protected_units": (),
            "evidence": common.CPU_DATA_ROOT / "migrations" / common.RUN_ID /
                        "drama-release" / common.NEW_SHA / "cpu",
        }
    if role == "hk":
        return {
            "host": common.HK_HOST,
            "data_root": common.HK_DATA_ROOT,
            "source_root": common.HK_DATA_ROOT / "migrations" / common.RUN_ID /
                           "drama-release" / "source" / common.NEW_SHA,
            "target_units": common.HK_TARGET_UNITS,
            "protected_units": common.HK_PROTECTED_UNITS,
            "evidence": common.HK_DATA_ROOT / "migrations" / common.RUN_ID /
                        "drama-release" / common.NEW_SHA / "hk",
        }
    raise common.OperatorError("unknown host role")


def safe_git_env():
    return {
        "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent",
    }


def git_output(source, arguments):
    _, stdout, _ = common.run(
        ["git", "-C", str(source)] + list(arguments), env=safe_git_env())
    return stdout.strip()


def verify_source_checkout(source):
    source = pathlib.Path(source)
    common.real_directory(source, expected_parent=source.parent)
    if git_output(source, ["rev-parse", "--show-toplevel"]) != str(source):
        raise common.OperatorError("source checkout top-level path changed")
    if git_output(source, ["rev-parse", "HEAD"]) != common.NEW_SHA:
        raise common.OperatorError("source checkout is not the approved new commit")
    if git_output(source, ["rev-parse", common.NEW_REMOTE_REF]) != common.NEW_SHA:
        raise common.OperatorError("pushed remote branch does not resolve to the approved commit")
    if git_output(source, ["remote", "get-url", "origin"]) != common.GITHUB_REMOTE:
        raise common.OperatorError("source checkout origin is not the approved GitHub repository")
    if git_output(source, ["status", "--porcelain=v1", "--untracked-files=all"]):
        raise common.OperatorError("source checkout is not clean")
    tree = git_output(source, ["rev-parse", "HEAD^{tree}"])
    if not re.match(r"^[0-9a-f]{40}$", tree):
        raise common.OperatorError("source Git tree identity is invalid")
    files = {}
    for relative, expected in common.CPU_NEW_FILES.items():
        path = source / pathlib.PurePosixPath(relative)
        common.validate_existing_ancestry(path, trusted_root=source)
        actual = common.sha256_file(path)
        if actual != expected:
            raise common.OperatorError("approved source file SHA256 mismatch: %s" % relative)
        files[relative] = {"path": str(path), "sha256": actual,
                           "bytes": int(os.lstat(str(path)).st_size)}
    return {"path": str(source), "commit": common.NEW_SHA, "tree": tree,
            "remote": common.GITHUB_REMOTE, "remote_ref": common.NEW_REMOTE_REF,
            "clean": True, "files": files}


def fragment_token(item):
    return "%s|%s|%s" % (
        item["unit"], item["fragment"]["path"], item["fragment"]["sha256"])


def parse_fragment_tokens(values):
    result = {}
    for raw in values or []:
        parts = raw.split("|", 2)
        if len(parts) != 3 or not parts[1].startswith("/") or not re.match(r"^[0-9a-f]{64}$", parts[2]):
            raise common.OperatorError("fragment binding must be UNIT|ABSOLUTE_PATH|SHA256")
        if parts[0] in result:
            raise common.OperatorError("duplicate fragment binding")
        result[parts[0]] = {"path": parts[1], "sha256": parts[2]}
    return result


def validate_fragment_bindings(snapshot, supplied, required):
    live = {unit: {"path": item["fragment"]["path"],
                   "sha256": item["fragment"]["sha256"]}
            for unit, item in snapshot.items()}
    if supplied:
        if supplied != live:
            raise common.OperatorError("unit FragmentPath/SHA binding differs from live state")
    elif required:
        raise common.OperatorError("apply requires exact --fragment bindings from a fresh dry-run")
    return live


def validate_cli(args):
    contract = role_contract(args.role)
    if (args.run_id != common.RUN_ID or args.expected_host != contract["host"] or
            args.expected_old_sha != common.OLD_SHA or
            args.expected_new_sha != common.NEW_SHA or
            pathlib.Path(args.data_root) != contract["data_root"] or
            pathlib.Path(args.source_root) != contract["source_root"]):
        raise common.OperatorError("release identity/path arguments differ from the approved contract")
    if tuple(args.unit or ()) != contract["target_units"]:
        raise common.OperatorError("target unit scope or order changed")
    if tuple(args.protected_unit or ()) != contract["protected_units"]:
        raise common.OperatorError("protected unit scope or order changed")
    if not args.expected_data_device or any(ch.isspace() for ch in args.expected_data_device):
        raise common.OperatorError("expected data device binding is missing or invalid")
    return contract


def available_bytes(path):
    value = os.statvfs(str(path))
    return int(value.f_bavail) * int(value.f_frsize)


def inspect_cpu_database():
    value = common.validate_existing_ancestry(CPU_DB, trusted_root=common.CPU_LIVE_ROOT,
                                              require_root_owner=False)
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise common.OperatorError("CPU drama database is not a regular file")
    connection = sqlite3.connect("file:%s?mode=ro" % CPU_DB, uri=True, timeout=10)
    try:
        connection.execute("PRAGMA query_only=ON")
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick != "ok" or foreign:
            raise common.OperatorError("CPU drama database integrity check failed")
        tables = {str(row[0]) for row in
                  connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"drama_material_job", "drama_material_job_worker_lease"}
        if not required.issubset(tables):
            raise common.OperatorError("CPU drama job/lease schema is incomplete")
        active_jobs = int(connection.execute(
            "SELECT COUNT(*) FROM drama_material_job "
            "WHERE status NOT IN ('done','failed','cancelled')").fetchone()[0])
        active_leases = int(connection.execute(
            "SELECT COUNT(*) FROM drama_material_job_worker_lease "
            "WHERE status NOT IN ('idle','done','failed','missing','deleted')").fetchone()[0])
        columns = [str(row[1]) for row in
                   connection.execute("PRAGMA table_info(drama_material_job)")]
        identifier = "job_id" if "job_id" in columns else "id"
        rows = connection.execute(
            "SELECT %s,status FROM drama_material_job WHERE %s IN (?,?) ORDER BY %s" %
            (identifier, identifier, identifier), JOB_IDS).fetchall()
        expected = sorted([(JOB_IDS[0], "failed"), (JOB_IDS[1], "failed")])
        if active_jobs or active_leases or sorted(rows) != expected:
            raise common.OperatorError("CPU drama jobs/leases are not at the approved drain point")
        return {"path": str(CPU_DB), "quick_check": "ok",
                "foreign_key_violations": 0, "active_jobs": 0, "active_leases": 0,
                "approved_job_statuses": [{"job_id": row[0], "status": row[1]}
                                          for row in sorted(rows)]}
    finally:
        connection.close()


def directory_is_empty(path):
    common.real_directory(path, require_root_owner=False)
    return not any(pathlib.Path(path).iterdir())


def inspect_hk_runtime():
    common.real_directory(common.HK_BASE)
    common.real_directory(HK_RELEASES)
    common.real_directory(HK_WORK_ROOT, require_root_owner=False)
    common.real_directory(HK_PUBLIC_ROOT, require_root_owner=False)
    if not directory_is_empty(HK_RUNTIME_ACTIVE):
        raise common.OperatorError("HK active runtime job directory is not empty")
    if not directory_is_empty(HK_RUNTIME_DIAGNOSTICS):
        raise common.OperatorError("HK runtime diagnostics directory is not empty")
    parts = []
    for root in (HK_WORK_ROOT, HK_PUBLIC_ROOT):
        for path in pathlib.Path(root).rglob("*.part"):
            parts.append(str(path))
    if parts:
        raise common.OperatorError("HK drama roots contain diagnostic partial files")
    return {"active_jobs": 0, "diagnostics": 0, "part_files": 0,
            "work_root": str(HK_WORK_ROOT), "public_root": str(HK_PUBLIC_ROOT)}


def initial_snapshot(args, contract):
    common.require_host(contract["host"])
    mount = common.validate_data_root(args.role, contract["data_root"], args.expected_data_device)
    if available_bytes(contract["data_root"]) < MIN_FREE_BYTES:
        raise common.OperatorError("less than 30 GiB remains on the bound data filesystem")
    source = verify_source_checkout(contract["source_root"])
    units = common.snapshot_units(contract["target_units"] + contract["protected_units"])
    fragments = validate_fragment_bindings(
        units, parse_fragment_tokens(args.fragment), args.apply)
    common.assert_no_media_processes()
    if args.role == "cpu":
        common.real_directory(common.CPU_LIVE_ROOT, require_root_owner=False)
        common.assert_inactive_unit(units[common.CPU_TARGET_UNITS[0]])
        common.assert_active_single_process(units[common.CPU_TARGET_UNITS[1]])
        common.assert_no_established_ports((8787, 18788))
        database = inspect_cpu_database()
        runtime = None
        live_files = {}
        for relative, expected in common.CPU_OLD_FILES.items():
            path = common.CPU_LIVE_ROOT / pathlib.PurePosixPath(relative)
            descriptor, record, digest = common.anchored_file(path, expected_sha256=expected)
            os.close(descriptor)
            live_files[relative] = {"path": str(path), "sha256": digest, "stat": record}
        current = None
    else:
        for unit in common.HK_TARGET_UNITS:
            common.assert_inactive_unit(units[unit])
        for unit in common.HK_PROTECTED_UNITS:
            common.assert_active_single_process(units[unit])
        common.assert_no_established_ports((8787,))
        database = None
        runtime = inspect_hk_runtime()
        live_files = None
        if not HK_CURRENT.is_symlink():
            raise common.OperatorError("HK current is not a symlink")
        old_release = HK_RELEASES / common.OLD_SHA
        common.real_directory(old_release)
        if os.path.realpath(str(HK_CURRENT)) != str(old_release):
            raise common.OperatorError("HK current is not the expected old release")
        if git_output(old_release, ["rev-parse", "HEAD"]) != common.OLD_SHA:
            raise common.OperatorError("HK current Git commit differs from expected old SHA")
        if common.path_lexists(HK_RELEASES / common.NEW_SHA):
            raise common.OperatorError("new HK release path already exists; inspect instead of adopting")
        current = {"link": str(HK_CURRENT), "target": os.readlink(str(HK_CURRENT)),
                   "resolved": str(old_release),
                   "lstat": common.stat_record(os.lstat(str(HK_CURRENT)))}
    target = {unit: common.unit_config_signature(units[unit])
              for unit in contract["target_units"]}
    protected = {unit: common.protected_signature(units[unit])
                 for unit in contract["protected_units"]}
    return {"schema": 1, "mode": "apply" if args.apply else "dry-run",
            "run_id": common.RUN_ID, "host_role": args.role, "host": contract["host"],
            "expected_old_sha": common.OLD_SHA, "expected_new_sha": common.NEW_SHA,
            "data": mount, "source": source, "fragments": fragments,
            "target_units": target, "protected_units": protected,
            "database": database, "runtime": runtime, "live_files": live_files,
            "current": current, "media_processes": [], "established_connections": 0,
            "unit_snapshot": units}


def compact_snapshot(snapshot):
    result = dict(snapshot)
    result.pop("unit_snapshot", None)
    result["required_fragment_arguments"] = [
        fragment_token(snapshot["unit_snapshot"][unit])
        for unit in sorted(snapshot["unit_snapshot"])
    ]
    result["ready"] = True
    return result


def phase(evidence, name, value):
    payload = {"schema": 1, "run_id": common.RUN_ID, "phase": name,
               "recorded_at_epoch": time.time(), "value": value}
    common.write_exclusive_json(pathlib.Path(evidence) / ("phase-%02d-%s.json" %
                                (len(list(pathlib.Path(evidence).glob("phase-*.json"))) + 1,
                                 name)), payload)


def copy_fd_to_exclusive(descriptor, destination, mode=0o600, uid=0, gid=0):
    destination = pathlib.Path(destination)
    output = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), mode)
    try:
        if os.name != "nt":
            os.fchown(output, int(uid), int(gid))
            os.fchmod(output, int(mode))
        offset = 0
        while True:
            if hasattr(os, "pread"):
                block = os.pread(descriptor, 1024 * 1024, offset)
            else:  # local Windows unit tests only
                os.lseek(descriptor, offset, os.SEEK_SET)
                block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            written = 0
            while written < len(block):
                count = os.write(output, block[written:])
                if count <= 0:
                    raise common.OperatorError("file copy made no progress")
                written += count
            offset += len(block)
        os.fsync(output)
    finally:
        os.close(output)
    common.fsync_directory(destination.parent)


def backup_cpu_files(evidence):
    backup = pathlib.Path(evidence) / "backup"
    os.mkdir(str(backup), 0o700)
    if os.name != "nt":
        os.chown(str(backup), 0, 0)
        os.chmod(str(backup), 0o700)
    common.fsync_directory(backup.parent)
    manifest = {"schema": 1, "run_id": common.RUN_ID, "host": common.CPU_HOST,
                "expected_old_sha": common.OLD_SHA, "files": []}
    for index, relative in enumerate(sorted(common.CPU_OLD_FILES)):
        live = common.CPU_LIVE_ROOT / pathlib.PurePosixPath(relative)
        descriptor, record, digest = common.anchored_file(
            live, expected_sha256=common.CPU_OLD_FILES[relative])
        try:
            target = backup / ("%02d-%s" % (index, pathlib.Path(relative).name))
            copy_fd_to_exclusive(descriptor, target)
        finally:
            os.close(descriptor)
        if common.sha256_file(target) != digest:
            raise common.OperatorError("CPU backup verification failed")
        manifest["files"].append({"relative": relative, "live": str(live),
                                  "backup": str(target), "sha256": digest,
                                  "live_stat": record})
    manifest_path = backup / "manifest.json"
    manifest_sha = common.write_exclusive_json(manifest_path, manifest)
    return {"directory": str(backup), "manifest": str(manifest_path),
            "manifest_sha256": manifest_sha, "files": manifest["files"]}


def create_cpu_swap(relative, evidence, swap_journal):
    if not isinstance(swap_journal, list):
        raise common.OperatorError("CPU swap journal must be a caller-owned list")
    source = role_contract("cpu")["source_root"] / pathlib.PurePosixPath(relative)
    live = common.CPU_LIVE_ROOT / pathlib.PurePosixPath(relative)
    source_fd, _, _ = common.anchored_file(
        source, expected_sha256=common.CPU_NEW_FILES[relative])
    old_fd, old_record, _ = common.anchored_file(
        live, expected_sha256=common.CPU_OLD_FILES[relative])
    temporary = live.parent / (".drama-release-%s-%s" %
                               (common.NEW_SHA[:12], live.name))
    try:
        if common.path_lexists(temporary):
            raise common.OperatorError("CPU swap temporary already exists")
        live_stat = os.fstat(old_fd)
        copy_fd_to_exclusive(
            source_fd, temporary, mode=stat.S_IMODE(live_stat.st_mode),
            uid=getattr(live_stat, "st_uid", 0), gid=getattr(live_stat, "st_gid", 0))
        if common.sha256_file(temporary) != common.CPU_NEW_FILES[relative]:
            raise common.OperatorError("CPU replacement temporary SHA256 mismatch")
        if common.identity_tuple(os.lstat(str(live))) != common.identity_tuple(os.fstat(old_fd)):
            raise common.OperatorError("CPU live file changed before atomic exchange")
        record = {"relative": relative, "live": str(live),
                  "temporary_old": str(temporary), "old_stat": old_record,
                  "new_sha256": common.CPU_NEW_FILES[relative],
                  "old_sha256": common.CPU_OLD_FILES[relative],
                  "exchange_complete": False,
                  "rollback_anchor_retained": False}
        common.atomic_rename_exchange(temporary, live)
        # The exchange is the mutation boundary.  Journal it before every
        # fallible verification/fsync so the outer transaction can always
        # restore the exact old bytes.
        record["exchange_complete"] = True
        record["rollback_anchor_retained"] = True
        swap_journal.append(record)
        if (common.sha256_file(live) != common.CPU_NEW_FILES[relative] or
                common.sha256_file(temporary) != common.CPU_OLD_FILES[relative]):
            raise common.OperatorError("CPU atomic exchange verification failed")
        common.fsync_directory(live.parent)
        return record
    finally:
        os.close(source_fd)
        os.close(old_fd)


def restore_cpu_swaps(swaps):
    errors = []
    for item in reversed(swaps):
        if not item.get("exchange_complete"):
            continue
        live = pathlib.Path(item["live"])
        temporary = pathlib.Path(item["temporary_old"])
        try:
            if (not common.path_lexists(temporary) or
                    common.sha256_file(live) != item["new_sha256"] or
                    common.sha256_file(temporary) != item["old_sha256"]):
                raise common.OperatorError("CPU rollback exchange anchors changed")
            common.atomic_rename_exchange(temporary, live)
            item["exchange_complete"] = False
            common.fsync_directory(live.parent)
            if common.sha256_file(live) != item["old_sha256"]:
                raise common.OperatorError("CPU rollback did not restore old bytes")
        except Exception as error:  # retain every failure in the rollback receipt
            errors.append({"relative": item["relative"], "error": type(error).__name__})
    return errors


def systemctl(action, unit):
    if action not in ("start", "stop"):
        raise common.OperatorError("unapproved systemctl action")
    common.run(["systemctl", "--job-mode=ignore-dependencies", action, unit])


def wait_unit(unit, active, attempts=60):
    last = None
    for _ in range(attempts):
        last = common.unit_identity(unit)
        try:
            if active:
                common.assert_active_single_process(last)
            else:
                common.assert_inactive_unit(last)
            return last
        except common.OperatorError:
            time.sleep(0.5)
    raise common.OperatorError("unit did not converge to expected state: %s" % unit)


def config_unchanged(before, after, units):
    for unit in units:
        if common.unit_config_signature(before[unit]) != common.unit_config_signature(after[unit]):
            raise common.OperatorError("systemd unit definition drifted: %s" % unit)


def compile_cpu_files():
    code = ("import pathlib; "
            "[compile(pathlib.Path(p).read_bytes(), p, 'exec') for p in %r]" %
            [str(common.CPU_LIVE_ROOT / pathlib.PurePosixPath(relative))
             for relative in sorted(common.CPU_NEW_FILES)])
    common.run(["/usr/bin/python3", "-B", "-c", code])


def listener_owned_by(port, pid):
    _, stdout, _ = common.run(["ss", "-Hltnp", "sport = :%d" % int(port)])
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1 or ("pid=%d" % int(pid)) not in lines[0]:
        raise common.OperatorError("listener is not uniquely owned by the expected process")
    return {"port": int(port), "owner_pid": int(pid),
            "line_sha256": hashlib.sha256(lines[0].encode("utf-8")).hexdigest()}


def prove_cpu_rollback(baseline_units, restored_units, api):
    common.assert_inactive_unit(restored_units[common.CPU_TARGET_UNITS[0]])
    common.assert_active_single_process(restored_units[api])
    config_unchanged(baseline_units, restored_units, common.CPU_TARGET_UNITS)
    process = restored_units[api].get("process") or {}
    pid = int(process.get("pid") or 0)
    if pid <= 1:
        raise common.OperatorError("restored CPU API process identity is missing")
    return listener_owned_by(8787, pid)


def cleanup_cpu_temporaries(swaps):
    cleaned = []
    for item in swaps:
        temporary = pathlib.Path(item["temporary_old"])
        if common.sha256_file(temporary) != item["old_sha256"]:
            raise common.OperatorError("old CPU temporary changed before cleanup")
        os.unlink(str(temporary))
        item["rollback_anchor_retained"] = False
        cleaned.append({"relative": item["relative"], "path": str(temporary),
                        "old_sha256": item["old_sha256"]})
        common.fsync_directory(temporary.parent)
    return cleaned


def persist_cpu_result_and_cleanup(evidence, result, swaps, commit_state):
    if not isinstance(commit_state, dict) or commit_state.get("committed"):
        raise common.OperatorError("CPU commit journal is invalid")
    result_path = pathlib.Path(evidence) / "result.json"
    receipt_sha = common.write_exclusive_json(result_path, result)
    # Result durability is the commit boundary.  From this point onward a
    # cleanup failure must never trigger an automatic code rollback.
    commit_state.update({"committed": True, "result": str(result_path),
                         "result_sha256": receipt_sha})
    cleaned = cleanup_cpu_temporaries(swaps)
    cleanup = {"schema": 1, "result": "rollback_temporaries_cleaned",
               "host_role": "cpu", "run_id": common.RUN_ID,
               "result_sha256": receipt_sha, "cleaned": cleaned,
               "completed_at_epoch": time.time()}
    cleanup_path = pathlib.Path(evidence) / "cleanup.json"
    cleanup_sha = common.write_exclusive_json(cleanup_path, cleanup)
    return {"result": str(result_path), "result_sha256": receipt_sha,
            "cleanup": str(cleanup_path), "cleanup_sha256": cleanup_sha}


def apply_cpu(args, contract, before):
    evidence = contract["evidence"]
    common.create_private_ancestry(contract["data_root"], evidence)
    phase(evidence, "preflight", compact_snapshot(before))
    backup = backup_cpu_files(evidence)
    phase(evidence, "backup", backup)
    baseline_units = before["unit_snapshot"]
    swaps = []
    api = common.CPU_TARGET_UNITS[1]
    api_stop_started = False
    commit_state = {"committed": False}
    rollback = {"attempted": False, "complete": None, "errors": []}
    try:
        common.assert_no_media_processes()
        common.assert_no_established_ports((8787, 18788))
        api_stop_started = True
        systemctl("stop", api)
        wait_unit(api, False)
        stopped = common.snapshot_units(common.CPU_TARGET_UNITS)
        common.assert_inactive_unit(stopped[common.CPU_TARGET_UNITS[0]])
        common.assert_inactive_unit(stopped[api])
        config_unchanged(baseline_units, stopped, common.CPU_TARGET_UNITS)
        common.assert_no_media_processes()
        inspect_cpu_database()
        phase(evidence, "cpu-stopped", {unit: common.protected_signature(item)
                                        for unit, item in stopped.items()})
        for relative in sorted(common.CPU_NEW_FILES):
            common.assert_no_media_processes()
            inspect_cpu_database()
            create_cpu_swap(relative, evidence, swaps)
        compile_cpu_files()
        phase(evidence, "cpu-files-switched", {"swaps": swaps})
        systemctl("start", api)
        active_api = wait_unit(api, True)
        after = common.snapshot_units(common.CPU_TARGET_UNITS)
        common.assert_inactive_unit(after[common.CPU_TARGET_UNITS[0]])
        config_unchanged(baseline_units, after, common.CPU_TARGET_UNITS)
        restart_bound = target_restart_bound(baseline_units, after, api)
        common.assert_no_media_processes()
        health = common.exact_health("127.0.0.1", 18788)
        listener = listener_owned_by(8787, active_api["process"]["pid"])
        for relative, expected in common.CPU_NEW_FILES.items():
            if common.sha256_file(common.CPU_LIVE_ROOT / pathlib.PurePosixPath(relative)) != expected:
                raise common.OperatorError("CPU live file changed after API start")
        phase(evidence, "cpu-verified", {"health_hk": health, "api_listener": listener,
                                         "restart_bound": restart_bound,
                                         "units": {unit: common.protected_signature(item)
                                                   for unit, item in after.items()}})
        result = {"schema": 1, "result": "deployed", "host_role": "cpu",
                  "run_id": common.RUN_ID, "old_sha": common.OLD_SHA,
                  "new_sha": common.NEW_SHA, "backup": backup,
                  "changed_files": sorted(common.CPU_NEW_FILES),
                  "worker_remained_stopped": True, "api_health": health,
                   "api_listener": listener, "api_restart_bound": restart_bound,
                   "rollback": rollback,
                   "rollback_temporaries_retained_at_commit": [
                       {"relative": item["relative"],
                        "path": item["temporary_old"],
                        "old_sha256": item["old_sha256"]}
                       for item in swaps],
                   "cleanup_receipt_required": True,
                   "production_job_or_publish_calls": 0,
                   "completed_at_epoch": time.time()}
        receipts = persist_cpu_result_and_cleanup(evidence, result, swaps, commit_state)
        return dict({"ok": True, "host_role": "cpu"}, **receipts)
    except Exception as error:
        if commit_state.get("committed"):
            failure = {"schema": 1, "result": "post_commit_cleanup_failed",
                       "host_role": "cpu", "run_id": common.RUN_ID,
                       "old_sha": common.OLD_SHA, "new_sha": common.NEW_SHA,
                       "error_type": type(error).__name__,
                       "automatic_rollback_suppressed": True,
                       "live_release_remains": common.NEW_SHA,
                       "rollback_anchor_state": [
                           {"relative": item["relative"],
                            "path": item["temporary_old"],
                            "retained": item.get("rollback_anchor_retained", False)}
                           for item in swaps],
                       "failed_at_epoch": time.time()}
            try:
                common.write_exclusive_json(evidence / "post-commit-failure.json", failure)
            except Exception:
                pass
            raise common.OperatorError(
                "POST-COMMIT: CPU release is deployed; cleanup receipt is incomplete; "
                "automatic rollback suppressed")
        rollback["attempted"] = bool(api_stop_started or swaps)
        if rollback["attempted"]:
            try:
                current_api = common.unit_identity(api)
                if current_api["systemd"].get("ActiveState") in ("active", "activating", "failed"):
                    systemctl("stop", api)
                    wait_unit(api, False)
            except Exception as stop_error:
                rollback["errors"].append({"stage": "stop-new-api",
                                           "error": type(stop_error).__name__})
            rollback["errors"].extend(restore_cpu_swaps(swaps))
            if not rollback["errors"]:
                try:
                    for relative, expected in common.CPU_OLD_FILES.items():
                        live = common.CPU_LIVE_ROOT / pathlib.PurePosixPath(relative)
                        if common.sha256_file(live) != expected:
                            raise common.OperatorError("CPU rollback left mixed live bytes")
                except Exception as bytes_error:
                    rollback["errors"].append({"stage": "prove-old-bytes",
                                               "error": type(bytes_error).__name__})
            if not rollback["errors"]:
                try:
                    systemctl("start", api)
                    wait_unit(api, True)
                except Exception as start_error:
                    rollback["errors"].append({"stage": "start-old-api",
                                               "error": type(start_error).__name__})
            if not rollback["errors"]:
                try:
                    restored_units = common.snapshot_units(common.CPU_TARGET_UNITS)
                    prove_cpu_rollback(baseline_units, restored_units, api)
                except Exception as proof_error:
                    rollback["errors"].append({"stage": "prove-old-api",
                                               "error": type(proof_error).__name__})
        rollback["complete"] = not rollback["errors"]
        failure = {"schema": 1, "result": "failed", "host_role": "cpu",
                   "run_id": common.RUN_ID, "old_sha": common.OLD_SHA,
                   "new_sha": common.NEW_SHA, "error_type": type(error).__name__,
                   "rollback": rollback, "failed_at_epoch": time.time()}
        try:
            common.write_exclusive_json(evidence / "failure.json", failure)
        except Exception:
            pass
        if not rollback["complete"]:
            raise common.OperatorError("HIGH RISK: CPU release failed and rollback is incomplete")
        raise


def clone_hk_release(source, stage):
    if common.path_lexists(stage):
        raise common.OperatorError("HK release staging path already exists")
    common.run(["git", "clone", "--no-local", "--no-hardlinks", "--no-checkout",
                str(source), str(stage)], env=safe_git_env())
    common.run(["git", "-C", str(stage), "checkout", "--detach", common.NEW_SHA],
               env=safe_git_env())
    common.run(["git", "-C", str(stage), "remote", "set-url", "origin",
                common.GITHUB_REMOTE], env=safe_git_env())
    if git_output(stage, ["rev-parse", "HEAD"]) != common.NEW_SHA:
        raise common.OperatorError("HK staged release commit mismatch")
    if git_output(stage, ["status", "--porcelain=v1", "--untracked-files=all"]):
        raise common.OperatorError("HK staged release is not clean")
    if git_output(stage, ["remote", "get-url", "origin"]) != common.GITHUB_REMOTE:
        raise common.OperatorError("HK staged release origin mismatch")
    for relative, expected in common.CPU_NEW_FILES.items():
        if common.sha256_file(stage / pathlib.PurePosixPath(relative)) != expected:
            raise common.OperatorError("HK staged release file SHA256 mismatch")
    common.fsync_directory(stage.parent)


def current_link_anchor():
    value = os.lstat(str(HK_CURRENT))
    if not stat.S_ISLNK(value.st_mode):
        raise common.OperatorError("HK current stopped being a symlink")
    target = os.readlink(str(HK_CURRENT))
    if os.path.realpath(str(HK_CURRENT)) != str(HK_RELEASES / common.OLD_SHA):
        raise common.OperatorError("HK current no longer points at the old release")
    return {"stat": common.stat_record(value), "target": target}


def assert_hk_current_release(expected_release):
    value = os.lstat(str(HK_CURRENT))
    if not stat.S_ISLNK(value.st_mode):
        raise common.OperatorError("HK current is not a symlink")
    if os.path.realpath(str(HK_CURRENT)) != str(expected_release):
        raise common.OperatorError("HK current does not point at the expected release")


def switch_hk_current(record):
    if not isinstance(record, dict) or record:
        raise common.OperatorError("HK current journal must be an empty caller-owned dict")
    anchor = current_link_anchor()
    temporary = common.HK_BASE / (".current-%s-%s" % (common.RUN_ID, common.NEW_SHA[:12]))
    if common.path_lexists(temporary):
        raise common.OperatorError("HK current temporary already exists")
    os.symlink(str(HK_RELEASES / common.NEW_SHA), str(temporary))
    common.fsync_directory(common.HK_BASE)
    current = os.lstat(str(HK_CURRENT))
    if common.stat_record(current) != anchor["stat"] or os.readlink(str(HK_CURRENT)) != anchor["target"]:
        raise common.OperatorError("HK current changed before atomic exchange")
    record.update({"old_link_temporary": str(temporary),
                   "old_target": anchor["target"],
                   "new_target": str(HK_RELEASES / common.NEW_SHA),
                   "exchange_complete": False,
                   "old_link_retained": False})
    common.atomic_rename_exchange(temporary, HK_CURRENT)
    # Record the namespace mutation before every fallible durability/probe
    # operation so rollback never depends on the function returning normally.
    record["exchange_complete"] = True
    common.fsync_directory(common.HK_BASE)
    if (os.path.realpath(str(HK_CURRENT)) != str(HK_RELEASES / common.NEW_SHA) or
            not temporary.is_symlink() or os.readlink(str(temporary)) != anchor["target"]):
        raise common.OperatorError("HK current exchange verification failed")
    return record


def hk_old_link_anchor(record):
    candidates = [pathlib.Path(record["old_link_temporary"])]
    destination = record.get("old_link_destination")
    if destination and pathlib.Path(destination) not in candidates:
        candidates.append(pathlib.Path(destination))
    present = [path for path in candidates if common.path_lexists(path)]
    if len(present) != 1:
        raise common.OperatorError("HK old current link location is ambiguous")
    path = present[0]
    value = os.lstat(str(path))
    if not stat.S_ISLNK(value.st_mode) or os.readlink(str(path)) != record["old_target"]:
        raise common.OperatorError("HK old current link anchor changed")
    return path


def retain_hk_old_link(record, evidence):
    source = hk_old_link_anchor(record)
    destination = pathlib.Path(evidence) / "current-before"
    record["old_link_destination"] = str(destination)
    record["old_link_move_started"] = True
    common.atomic_rename_noreplace(source, destination)
    # Update immediately after the namespace mutation, before fsync.
    record["old_link_temporary"] = str(destination)
    record["old_link_retained"] = True
    common.fsync_directory(destination.parent)
    return destination


def restore_hk_current(record):
    if not record.get("exchange_complete"):
        raise common.OperatorError("HK current journal has no completed exchange")
    temporary = hk_old_link_anchor(record)
    if os.path.realpath(str(HK_CURRENT)) != record["new_target"]:
        raise common.OperatorError("HK current rollback anchors changed")
    common.atomic_rename_exchange(temporary, HK_CURRENT)
    record["exchange_complete"] = False
    record["restored"] = True
    common.fsync_directory(common.HK_BASE)
    assert_hk_current_release(HK_RELEASES / common.OLD_SHA)


def target_restart_bound(before, after, unit):
    old_restarts = int(before[unit]["systemd"].get("NRestarts") or 0)
    new_restarts = int(after[unit]["systemd"].get("NRestarts") or 0)
    if new_restarts not in (old_restarts, old_restarts + 1):
        raise common.OperatorError("target unit restart count exceeded one maintenance window")
    if common.unit_config_signature(before[unit]) != common.unit_config_signature(after[unit]):
        raise common.OperatorError("target unit definition changed during restart")
    return {"before": old_restarts, "after": new_restarts,
            "delta": new_restarts - old_restarts}


def apply_hk(args, contract, before):
    evidence = contract["evidence"]
    common.create_private_ancestry(contract["data_root"], evidence)
    phase(evidence, "preflight", compact_snapshot(before))
    baseline = before["unit_snapshot"]
    protected_before = {unit: baseline[unit] for unit in common.HK_PROTECTED_UNITS}
    release = HK_RELEASES / common.NEW_SHA
    stage = HK_RELEASES / (".stage-%s-%s" % (common.RUN_ID, common.NEW_SHA[:12]))
    current_record = {}
    published = False
    started = []
    rollback = {"attempted": False, "complete": None, "errors": []}

    def guard_protected_and_idle():
        common.assert_no_media_processes()
        inspect_hk_runtime()
        live = common.snapshot_units(common.HK_PROTECTED_UNITS)
        common.assert_protected_units(protected_before, live)
        return live

    try:
        common.assert_no_media_processes()
        common.assert_no_established_ports((8787,))
        inspect_hk_runtime()
        clone_hk_release(contract["source_root"], stage)
        guard_protected_and_idle()
        phase(evidence, "hk-release-staged", {"stage": str(stage),
                                               "commit": common.NEW_SHA})
        common.atomic_rename_noreplace(stage, release)
        published = True
        common.fsync_directory(HK_RELEASES)
        if git_output(release, ["rev-parse", "HEAD"]) != common.NEW_SHA:
            raise common.OperatorError("published HK release commit mismatch")
        phase(evidence, "hk-release-published", {"release": str(release),
                                                  "commit": common.NEW_SHA})
        guard_protected_and_idle()
        switch_hk_current(current_record)
        guard_protected_and_idle()
        phase(evidence, "hk-current-switched", current_record)
        systemctl("start", common.HK_TARGET_UNITS[0])
        wait_unit(common.HK_TARGET_UNITS[0], True, attempts=120)
        started.append(common.HK_TARGET_UNITS[0])
        guard_protected_and_idle()
        systemctl("start", common.HK_TARGET_UNITS[1])
        wait_unit(common.HK_TARGET_UNITS[1], True, attempts=120)
        started.append(common.HK_TARGET_UNITS[1])
        guard_protected_and_idle()
        after = common.snapshot_units(common.HK_TARGET_UNITS + common.HK_PROTECTED_UNITS)
        common.assert_protected_units(
            protected_before, {unit: after[unit] for unit in common.HK_PROTECTED_UNITS})
        restart_bounds = {unit: target_restart_bound(baseline, after, unit)
                          for unit in common.HK_TARGET_UNITS}
        worker = after[common.HK_TARGET_UNITS[0]]
        if os.path.realpath(worker["process"]["cwd"]) != str(release):
            raise common.OperatorError("HK worker cwd is not the new release")
        common.assert_no_media_processes()
        inspect_hk_runtime()
        health = common.exact_health("127.0.0.1", 8787)
        listener = listener_owned_by(8787, worker["process"]["pid"])
        if os.path.realpath(str(HK_CURRENT)) != str(release):
            raise common.OperatorError("HK current changed after service start")
        phase(evidence, "hk-local-verified", {
            "health": health, "listener": listener, "restart_bounds": restart_bounds,
            "protected_units": {unit: common.protected_signature(after[unit])
                                for unit in common.HK_PROTECTED_UNITS},
        })
        retain_hk_old_link(current_record, evidence)
        result = {"schema": 1, "result": "deployed_local_route_pending",
                  "host_role": "hk", "run_id": common.RUN_ID,
                  "old_sha": common.OLD_SHA, "new_sha": common.NEW_SHA,
                  "release": str(release), "current": str(HK_CURRENT),
                  "health_hk_8787": health, "listener_hk_8787": listener,
                  "restart_bounds": restart_bounds,
                  "protected_units_unchanged": True,
                  "cpu_route_verification_required": True,
                  "cpu_route_command": "drama_release.py route (read-only)",
                  "production_job_or_publish_calls": 0,
                  "rollback": rollback, "completed_at_epoch": time.time()}
        receipt_sha = common.write_exclusive_json(evidence / "result.json", result)
        return {"ok": True, "result": str(evidence / "result.json"),
                "result_sha256": receipt_sha, "host_role": "hk",
                "cpu_route_verification_required": True}
    except Exception as error:
        rollback["attempted"] = True
        for unit in reversed(common.HK_TARGET_UNITS):
            try:
                item = common.unit_identity(unit)
                if item["systemd"].get("ActiveState") in ("active", "activating", "failed"):
                    systemctl("stop", unit)
                    wait_unit(unit, False)
            except Exception as stop_error:
                rollback["errors"].append({"stage": "stop-new-target", "unit": unit,
                                           "error": type(stop_error).__name__})
        if current_record.get("exchange_complete"):
            try:
                restore_hk_current(current_record)
            except Exception as current_error:
                rollback["errors"].append({"stage": "restore-current",
                                           "error": type(current_error).__name__})
        # Both exact target units were required stopped before this release.
        # Rollback preserves that state; the protected eight units must remain
        # byte/process-identical and are never systemctl targets.
        try:
            stopped = common.snapshot_units(common.HK_TARGET_UNITS)
            for unit in common.HK_TARGET_UNITS:
                common.assert_inactive_unit(stopped[unit])
            assert_hk_current_release(HK_RELEASES / common.OLD_SHA)
            protected_after = common.snapshot_units(common.HK_PROTECTED_UNITS)
            common.assert_protected_units(protected_before, protected_after)
        except Exception as proof_error:
            rollback["errors"].append({"stage": "prove-rollback",
                                       "error": type(proof_error).__name__})
        rollback["complete"] = not rollback["errors"]
        failure = {"schema": 1, "result": "failed", "host_role": "hk",
                   "run_id": common.RUN_ID, "old_sha": common.OLD_SHA,
                   "new_sha": common.NEW_SHA, "release_published": published,
                   "error_type": type(error).__name__, "rollback": rollback,
                   "failed_at_epoch": time.time()}
        try:
            common.write_exclusive_json(evidence / "failure.json", failure)
        except Exception:
            pass
        if not rollback["complete"]:
            raise common.OperatorError("HIGH RISK: HK release failed and rollback is incomplete")
        raise


def process_ancestors(pid):
    result = []
    current = int(pid)
    for _ in range(32):
        if current <= 1 or current in result:
            break
        result.append(current)
        status = pathlib.Path("/proc/%d/status" % current).read_text()
        match = re.search(r"^PPid:\s+(\d+)\s*$", status, re.MULTILINE)
        if not match:
            raise common.OperatorError("listener process ancestry is unreadable")
        current = int(match.group(1))
    return result


def verify_cpu_route(args):
    if (args.run_id != common.RUN_ID or args.expected_host != common.CPU_HOST or
            args.expected_new_sha != common.NEW_SHA):
        raise common.OperatorError("CPU route verification binding changed")
    common.require_host(common.CPU_HOST)
    health = common.exact_health("127.0.0.1", 18788)
    _, stdout, _ = common.run(["ss", "-Hltnp", "sport = :18788"])
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise common.OperatorError("CPU 18788 listener count changed")
    pids = [int(value) for value in re.findall(r"pid=(\d+)", lines[0])]
    if len(set(pids)) != 1:
        raise common.OperatorError("CPU 18788 listener process identity is ambiguous")
    pid = pids[0]
    executable = os.path.basename(os.readlink("/proc/%d/exe" % pid)).lower()
    if "sshd" not in executable:
        raise common.OperatorError("CPU 18788 is not owned by sshd")
    ancestors = process_ancestors(pid)
    _, connections, _ = common.run(["ss", "-Hntp", "state", "established"])
    hk_lines = [line for line in connections.splitlines()
                if "43.154.250.89:" in line and any("pid=%d" % item in line
                                                    for item in ancestors)]
    if not hk_lines:
        raise common.OperatorError("CPU reverse listener cannot be attributed to the HK SSH peer")
    return {"ok": True, "mode": "read-only", "run_id": common.RUN_ID,
            "host": common.CPU_HOST, "expected_new_sha": common.NEW_SHA,
            "health_cpu_18788": health,
            "listener": {"port": 18788, "pid": pid,
                         "startticks": common.process_startticks(pid),
                         "exe": executable,
                         "line_sha256": hashlib.sha256(lines[0].encode("utf-8")).hexdigest()},
            "hk_peer": "43.154.250.89", "listener_owned_by_hk": True,
            "matching_connection_count": len(hk_lines),
            "production_job_or_publish_calls": 0,
            "checked_at_epoch": time.time()}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    for role in ("cpu", "hk"):
        item = sub.add_parser(role)
        item.add_argument("--run-id", required=True)
        item.add_argument("--expected-host", required=True)
        item.add_argument("--expected-old-sha", required=True)
        item.add_argument("--expected-new-sha", required=True)
        item.add_argument("--data-root", required=True)
        item.add_argument("--expected-data-device", required=True)
        item.add_argument("--source-root", required=True)
        item.add_argument("--unit", action="append", default=[])
        item.add_argument("--protected-unit", action="append", default=[])
        item.add_argument("--fragment", action="append", default=[])
        item.add_argument("--apply", action="store_true")
        item.set_defaults(role=role)
    route = sub.add_parser("route")
    route.add_argument("--run-id", required=True)
    route.add_argument("--expected-host", required=True)
    route.add_argument("--expected-new-sha", required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "route":
        result = verify_cpu_route(args)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    contract = validate_cli(args)
    before = initial_snapshot(args, contract)
    if not args.apply:
        print(json.dumps(compact_snapshot(before), sort_keys=True, indent=2))
        return 0
    control = contract["data_root"] / "migrations" / common.RUN_ID / "control"
    common.real_directory(control)
    lock = control / (".drama-release-%s.lock" % args.role)
    with common.exclusive_lock(lock):
        # The second full snapshot closes the dry-run/apply and lock-acquisition race.
        before = initial_snapshot(args, contract)
        if common.path_lexists(contract["evidence"]):
            raise common.OperatorError("release evidence path already exists")
        result = (apply_cpu(args, contract, before) if args.role == "cpu"
                  else apply_hk(args, contract, before))
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except common.OperatorError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        sys.exit(78)
