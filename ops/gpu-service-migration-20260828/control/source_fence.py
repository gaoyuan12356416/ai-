#!/usr/bin/env python3
"""Fence only approved US services after a fresh coordinator drain checkpoint."""
import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import time

RUN_ID = "gpu-service-migration-20260828T1502"
DATA_ROOT = pathlib.Path("/data")
BASE = DATA_ROOT / "migrations" / RUN_ID / "source-fence"
GROUPS = {
    "materials": ["ad-material-generation.service", "ad-material-vision.service",
                  "codex-cover-generator.service", "codex-screenshot-batch.service",
                  "codex-screenshot-batch-burst.service", "codex-screenshot-square.service",
                  "codex-screenshot-portrait.service", "codex-screenshot-landscape.service",
                  "gpu-worker-reverse-tunnel.service",
                  "gpu-screenshot-batch-burst-tunnel.service"],
    "drama": ["drama-material-api.service"],
    "tt": ["tt-gpu-publisher.service", "tt-gpu-direct-outro.service",
           "tt-gpu-reverse-tunnel.service", "tt-gpu-direct-outro-reverse-tunnel.service"],
    "x": ["x-post-media-repair.service", "x-post-media-repair-tunnel.service"],
}
DRAMA_UNIT = "drama-material-api.service"
DRAMA_SHARED_TUNNEL = "gpu-worker-reverse-tunnel.service"
DRAMA_PORT = 8787


def run(args, check=True):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    if check and p.returncode:
        raise RuntimeError("command failed: %s: %s" % (args[0], p.stderr.strip()))
    return p


def prop(unit, name):
    return run(["systemctl", "show", unit, "-p", name, "--value"]).stdout.strip()


def path_lexists(path):
    return os.path.lexists(str(path))


def path_is_symlink(path):
    return path.is_symlink()


def resolve_definition_path(path):
    return path.resolve()


def source_storage_guard(create=False):
    try:
        relative = BASE.relative_to(DATA_ROOT)
    except ValueError:
        raise RuntimeError("US source evidence path escaped /data")
    paths = [DATA_ROOT]
    for part in relative.parts:
        paths.append(paths[-1] / part)
    for path in paths:
        if path_lexists(path) and path_is_symlink(path):
            raise RuntimeError("US source evidence path contains a symlink")
    if create:
        BASE.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(BASE), 0o700)
        # Recheck every component after mkdir so a concurrent symlink swap
        # cannot redirect evidence away from /data between the first guard and
        # the write.
        for path in paths:
            if not path_lexists(path) or path_is_symlink(path):
                raise RuntimeError("US source evidence path changed or contains a symlink")
    probe = BASE
    while not path_lexists(probe):
        probe = probe.parent
    target = run(["findmnt", "-n", "-o", "TARGET", "-T", str(probe)]).stdout.strip()
    if target != str(DATA_ROOT):
        raise RuntimeError("US source evidence path is not on the /data mount")
    if create and run(["findmnt", "-n", "-o", "TARGET", "-T", str(BASE)]).stdout.strip() != str(DATA_ROOT):
        raise RuntimeError("US source evidence directory escaped the /data mount")


def definition_path_record(raw):
    path = pathlib.Path(raw)
    if not path.is_absolute() or not path_lexists(path):
        raise RuntimeError("unit definition path is missing or non-absolute")
    if path_is_symlink(path):
        target = os.readlink(str(path))
        resolved = resolve_definition_path(path)
        if not resolved.is_file():
            raise RuntimeError("unit definition symlink target is not a regular file")
        content = resolved.read_bytes()
        if (not path_lexists(path) or not path_is_symlink(path) or
                os.readlink(str(path)) != target or resolve_definition_path(path) != resolved):
            raise RuntimeError("unit definition symlink changed while it was read")
        return {"path": raw, "kind": "symlink", "link_target": target,
                "link_target_sha256": hashlib.sha256(target.encode("utf-8")).hexdigest(),
                "resolved_path": str(resolved),
                "content_sha256": hashlib.sha256(content).hexdigest()}
    if not path.is_file():
        raise RuntimeError("unit definition member is not a regular file")
    before = path.stat()
    content = path.read_bytes()
    after = path.stat()
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if path_is_symlink(path) or identity_before != identity_after:
        raise RuntimeError("unit definition file changed while it was read")
    return {"path": raw, "kind": "file",
            "content_sha256": hashlib.sha256(content).hexdigest()}


def unit_definition_snapshot(unit):
    fragment = prop(unit, "FragmentPath")
    dropins = prop(unit, "DropInPaths").split()
    if not fragment or len(dropins) != len(set(dropins)):
        raise RuntimeError("unit definition paths are missing or duplicated: " + unit)
    definition = {"fragment": definition_path_record(fragment),
                  "dropins": [definition_path_record(path) for path in dropins]}
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode("utf-8")
    definition["definition_sha256"] = hashlib.sha256(canonical).hexdigest()
    return definition


def inspect(unit):
    pid = int(prop(unit, "MainPID") or 0)
    result = {"unit": unit, "pid": pid, "active": prop(unit, "ActiveState"),
              "substate": prop(unit, "SubState"),
              "control_pid": int(prop(unit, "ControlPID") or 0),
              "control_group": prop(unit, "ControlGroup"),
              "enabled": prop(unit, "UnitFileState"), "fragment": prop(unit, "FragmentPath")}
    if pid:
        status = pathlib.Path("/proc/%s/status" % pid).read_text()
        result["threads"] = int(next(line.split()[1] for line in status.splitlines()
                                     if line.startswith("Threads:")))
        result["children"] = pathlib.Path("/proc/%s/task/%s/children" % (pid, pid)).read_text().split()
    return result


def assert_idle(states):
    for state in states:
        if "tunnel" not in state["unit"] and state["pid"]:
            if state.get("threads") != 1 or state.get("children"):
                raise RuntimeError("source not idle: " + state["unit"])


def port_rows(port, listening=False):
    args = ["ss", "-H", "-ltnp"] if listening else ["ss", "-Hntp", "state", "established"]
    marker = ":%d" % port
    return [line for line in run(args).stdout.splitlines()
            if marker in line and re.search(r":%d\b" % port, line)]


def unit_definition_sha256(unit):
    return unit_definition_snapshot(unit)["definition_sha256"]


def shared_tunnel_snapshot():
    return {
        "unit": DRAMA_SHARED_TUNNEL,
        "active": prop(DRAMA_SHARED_TUNNEL, "ActiveState"),
        "substate": prop(DRAMA_SHARED_TUNNEL, "SubState"),
        "enabled": prop(DRAMA_SHARED_TUNNEL, "UnitFileState"),
        "pid": int(prop(DRAMA_SHARED_TUNNEL, "MainPID") or 0),
        "control_pid": int(prop(DRAMA_SHARED_TUNNEL, "ControlPID") or 0),
        "control_group": prop(DRAMA_SHARED_TUNNEL, "ControlGroup"),
        "nrestarts": int(prop(DRAMA_SHARED_TUNNEL, "NRestarts") or 0),
        "start_monotonic": prop(DRAMA_SHARED_TUNNEL, "ExecMainStartTimestampMonotonic"),
        "active_enter_monotonic": prop(DRAMA_SHARED_TUNNEL, "ActiveEnterTimestampMonotonic"),
        "unit_sha256": unit_definition_sha256(DRAMA_SHARED_TUNNEL),
    }


def validate_shared_tunnel_baseline(state):
    if state["active"] != "active" or state["substate"] != "running" or state["pid"] <= 0:
        raise RuntimeError("shared drama tunnel is not stably active")
    if state["control_pid"] != 0 or not state["control_group"]:
        raise RuntimeError("shared drama tunnel process identity is incomplete")
    try:
        started = int(state["start_monotonic"])
        active_enter = int(state["active_enter_monotonic"])
    except (TypeError, ValueError):
        started = active_enter = 0
    if started <= 0 or active_enter <= 0:
        raise RuntimeError("shared drama tunnel start identity is incomplete")


def validate_drama_checkpoint(proof):
    if proof.get("coordinator_host") != "VM-0-108-centos" or proof.get("ready") is not True:
        raise RuntimeError("drama checkpoint is not a ready CPU coordinator snapshot")
    if (proof.get("business_requests_sent") != 0 or
            proof.get("legacy_18787_connections") != 0 or
            proof.get("legacy_18787_established_connections") != 0 or
            proof.get("hk_18788_business_http_connections") != 0 or
            proof.get("health_get_requests_completed") != 2):
        raise RuntimeError("drama checkpoint request or legacy-connection proof changed")
    health = proof.get("hk_health", {})
    if (health.get("url") != "http://127.0.0.1:18788/healthz" or
            health.get("method") != "GET" or health.get("status") != 200 or
            health.get("body") != {"ok": True, "role": "media-only"}):
        raise RuntimeError("drama checkpoint HK health identity changed")
    api = proof.get("cpu_api", {})
    if (api.get("effective_url") != "http://127.0.0.1:18788" or
            api.get("active") != "active" or api.get("substate") != "running" or
            api.get("control_pid") != 0 or
            api.get("control_group") != "/system.slice/drama-material-api.service" or
            api.get("both_files_point_to_expected_url") is not True or
            api.get("tokens_match_without_disclosure") is not True or
            api.get("running_environment_matches") is not True or
            int(api.get("pid", 0)) <= 0):
        raise RuntimeError("drama checkpoint CPU API identity changed")
    config_paths = [row.get("path") for row in api.get("configuration_files", [])]
    if config_paths != ["/etc/drama-synthesis/cpu.env", "/root/drama_material_service/.env"]:
        raise RuntimeError("drama checkpoint configuration-file identity changed")
    gate = proof.get("materials_gate", {})
    pause = proof.get("materials_pause", {})
    database = proof.get("database", {})
    drain_samples = proof.get("drain_samples", {})
    if gate.get("materials_active") is not True or "materials" not in gate.get("groups", []):
        raise RuntimeError("drama checkpoint materials gate is not active")
    if (pause.get("record_restored") is not False or pause.get("cron_paused") is not True or
            pause.get("journal_version") != 2 or
            pause.get("journal_run_id") != RUN_ID or
            pause.get("journal_group") != "materials" or
            pause.get("journal_phase") != "paused" or
            type(pause.get("journal_revision")) is not int or
            pause.get("journal_revision") <= 0):
        raise RuntimeError("drama checkpoint materials pause proof changed")
    expected_test_units = {"ad-material-frontend-test.service", "drama-material-api-test.service"}
    test_services = pause.get("test_services", {})
    if set(test_services) != expected_test_units or any(
            state.get("active") != "inactive" or state.get("substate") != "dead" or
            state.get("pid") != 0 for state in test_services.values()):
        raise RuntimeError("drama checkpoint test-service pause proof changed")
    if (database.get("active_jobs") != 0 or database.get("active_leases") != 0 or
            database.get("no_unknown") is not True or
            database.get("unknown_semantics") != "not_applicable_and_absent"):
        raise RuntimeError("drama checkpoint job or lease proof changed")
    if (drain_samples.get("sample_count") != 3 or
            drain_samples.get("stable") is not True):
        raise RuntimeError("drama checkpoint stable drain samples changed")
    process_scope = proof.get("process_scope", {})
    categories = process_scope.get("drama_related_child_categories", {})
    if (process_scope.get("cgroup_version") != 1 or
            process_scope.get("controller") != "systemd" or
            process_scope.get("control_group") != "/system.slice/drama-material-api.service" or
            process_scope.get("main_pid") != api.get("pid") or
            process_scope.get("cgroup_pids") != [api.get("pid")] or
            process_scope.get("descendant_pids") != [] or
            any(categories.get(name) != 0 for name in ("ffmpeg", "ffprobe", "codex", "other")) or
            process_scope.get("host_wide_process_scan_performed") is not False):
        raise RuntimeError("drama checkpoint process-scope proof changed")
    stability = proof.get("stability", {})
    critical_fields = ("materials_gate", "materials_pause", "cpu_api", "hk_health",
                       "database", "drain_samples", "process_scope")
    critical_snapshot = {name: proof.get(name) for name in critical_fields}
    canonical = json.dumps(critical_snapshot, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    expected_snapshot_sha256 = hashlib.sha256(canonical).hexdigest()
    supplied_snapshot_sha256 = str(stability.get("critical_snapshot_sha256", ""))
    if (stability.get("verification_passes") != 2 or stability.get("identical") is not True or
            not re.match(r"^[0-9a-f]{64}$", supplied_snapshot_sha256) or
            supplied_snapshot_sha256 != expected_snapshot_sha256):
        raise RuntimeError("drama checkpoint double-verification proof changed")


def validate_drama_preflight(states, resume=False):
    if len(states) != 1 or states[0]["unit"] != DRAMA_UNIT:
        raise RuntimeError("drama source scope changed")
    state = states[0]
    if state["pid"]:
        if state["threads"] != 1 or state["children"]:
            raise RuntimeError("drama source is not single-threaded and idle")
    elif not resume:
        raise RuntimeError("drama source is not running before the initial fence")
    if not resume and (state["active"] != "active" or state["substate"] != "running"):
        raise RuntimeError("drama source active state changed")
    if port_rows(DRAMA_PORT):
        raise RuntimeError("drama source still has established requests")


def validate_drama_fenced(shared_before):
    state = inspect(DRAMA_UNIT)
    if (state["active"] != "inactive" or state["substate"] != "dead" or
            state["enabled"] != "masked" or state["pid"] != 0 or
            state["control_pid"] != 0 or state["control_group"]):
        raise RuntimeError("drama source final fence verification failed")
    if port_rows(DRAMA_PORT, listening=True) or port_rows(DRAMA_PORT):
        raise RuntimeError("drama source port remains owned after fencing")
    shared_after = shared_tunnel_snapshot()
    if shared_after != shared_before:
        raise RuntimeError("shared drama tunnel changed during source fencing")
    return state, shared_after


def directory_manifest(root):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            result[relative] = {"kind": "symlink", "target": os.readlink(str(path))}
        elif path.is_file():
            result[relative] = {"kind": "file", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        elif path.is_dir():
            result[relative] = {"kind": "directory"}
        else:
            raise RuntimeError("unsupported drop-in backup member")
    return result


def backup_unit_definition(state, unit_backup, resume=False):
    definition = state.get("definition")
    if definition:
        manifest = unit_backup / "definition.json"
        if manifest.exists():
            if json.loads(manifest.read_text()) != definition:
                raise RuntimeError("archived unit definition manifest changed: " + state["unit"])
        else:
            if unit_definition_snapshot(state["unit"]) != definition:
                raise RuntimeError("unit definition changed before backup: " + state["unit"])
            write_private_json(manifest, definition)
        members = [("original.service", definition["fragment"])]
        members.extend(("dropins/%03d.service" % index, row)
                       for index, row in enumerate(definition["dropins"]))
        for relative, row in members:
            target = unit_backup / relative
            if target.exists():
                content = target.read_bytes()
            else:
                source = pathlib.Path(row["path"])
                if definition_path_record(row["path"]) != row:
                    raise RuntimeError("unit definition source changed during backup")
                if not source.is_file():
                    raise RuntimeError("unit definition source disappeared during backup")
                content = source.read_bytes()
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.open("xb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(str(target), 0o600)
                if definition_path_record(row["path"]) != row:
                    raise RuntimeError("unit definition source changed during backup")
            if hashlib.sha256(content).hexdigest() != row["content_sha256"]:
                raise RuntimeError("unit definition backup content mismatch: " + state["unit"])
        return
    fragment = pathlib.Path(state["fragment"]) if state["fragment"] else None
    if fragment and fragment.is_file():
        original = unit_backup / "original.service"
        fragment_bytes = fragment.read_bytes()
        if original.exists():
            if original.read_bytes() != fragment_bytes:
                raise RuntimeError("original unit changed after partial fence: " + state["unit"])
        else:
            with original.open("xb") as output:
                output.write(fragment_bytes)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(str(original), 0o600)
    dropins = pathlib.Path("/etc/systemd/system") / (state["unit"] + ".d")
    archived = unit_backup / "dropins"
    if dropins.is_dir():
        if archived.exists():
            if directory_manifest(archived) != directory_manifest(dropins):
                raise RuntimeError("unit drop-ins changed after partial fence: " + state["unit"])
        else:
            shutil.copytree(str(dropins), str(archived), symlinks=True)
            for path in archived.rglob("*"):
                if path.is_symlink():
                    continue
                os.chmod(str(path), 0o700 if path.is_dir() else 0o600)
            if directory_manifest(archived) != directory_manifest(dropins):
                raise RuntimeError("unit drop-in backup verification failed: " + state["unit"])
    elif archived.exists() and resume:
        raise RuntimeError("unit drop-ins disappeared after partial fence: " + state["unit"])


def write_private_json(target, payload):
    with target.open("x") as output:
        json.dump(payload, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(str(target), 0o600)


def write_failure_evidence(stage, original_exception_type, command_results,
                           final_state, final_closed, shared_before,
                           final_verification_error=None):
    source_storage_guard(create=True)
    payload = {"group": "drama", "stage": stage,
               "original_exception_type": original_exception_type,
               "failed_at_epoch": time.time(), "command_results": command_results,
               "source": final_state, "final_closed": final_closed,
               "shared_tunnel_before": shared_before, "shared_tunnel_after": {}}
    if final_verification_error:
        payload["final_verification_error_type"] = final_verification_error
    try:
        payload["port_8787_listener_count"] = len(port_rows(DRAMA_PORT, listening=True))
        payload["port_8787_established_count"] = len(port_rows(DRAMA_PORT))
    except Exception as error:
        payload["port_capture_error_type"] = type(error).__name__
    try:
        payload["shared_tunnel_after"] = shared_tunnel_snapshot()
    except Exception as error:
        payload["shared_tunnel_capture_error"] = type(error).__name__
    for attempt in range(100):
        target = BASE / ("drama-failure-%d-%d-%02d.json" %
                         (int(time.time()), os.getpid(), attempt))
        if not target.exists():
            write_private_json(target, payload)
            return target
    raise RuntimeError("cannot allocate unique drama failure evidence path")


def write_drama_success_evidence(final_state, shared_before, shared_after, checkpoint_sha256):
    payload = {"group": "drama", "completed_at_epoch": time.time(),
               "checkpoint_sha256": checkpoint_sha256,
               "source": final_state, "port_8787_listener_count": 0,
               "port_8787_established_count": 0,
               "shared_tunnel_before": shared_before, "shared_tunnel_after": shared_after,
               "shared_tunnel_unchanged": shared_after == shared_before}
    target = BASE / "drama-after.json"
    if target.exists():
        existing = json.loads(target.read_text())
        comparable = dict(existing)
        comparable.pop("completed_at_epoch", None)
        expected = dict(payload)
        expected.pop("completed_at_epoch", None)
        if comparable != expected:
            raise RuntimeError("existing drama success evidence differs")
        return
    write_private_json(target, payload)


def best_effort_command(args):
    try:
        result = run(args, check=False)
        return {"rc": int(result.returncode), "error_type": None}
    except Exception as error:
        return {"rc": None, "error_type": type(error).__name__}


def verified_definition_backup(unit_backup):
    try:
        manifest = unit_backup / "definition.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise RuntimeError("unit definition manifest is missing")
        definition = json.loads(manifest.read_text())
        core = {"fragment": definition["fragment"], "dropins": definition["dropins"]}
        canonical = json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != definition.get("definition_sha256"):
            raise RuntimeError("unit definition manifest hash changed")
        members = [("original.service", definition["fragment"])]
        members.extend(("dropins/%03d.service" % index, row)
                       for index, row in enumerate(definition["dropins"]))
        for relative, row in members:
            archived = unit_backup / relative
            if archived.is_symlink() or not archived.is_file():
                raise RuntimeError("unit definition archive is incomplete")
            if hashlib.sha256(archived.read_bytes()).hexdigest() != row["content_sha256"]:
                raise RuntimeError("unit definition archive hash changed")
        return True, None
    except Exception as error:
        return False, type(error).__name__


def closure_state(require_masked):
    state = {}
    try:
        state.update({"active": prop(DRAMA_UNIT, "ActiveState"),
                      "substate": prop(DRAMA_UNIT, "SubState"),
                      "enabled": prop(DRAMA_UNIT, "UnitFileState"),
                      "pid": int(prop(DRAMA_UNIT, "MainPID") or 0),
                      "control_pid": int(prop(DRAMA_UNIT, "ControlPID") or 0),
                      "control_group": prop(DRAMA_UNIT, "ControlGroup")})
        # A stopped unit is not sufficient proof if an orphan or independently
        # launched process still owns the retired API port. Treat an ss failure
        # as an unverifiable closure rather than assuming an empty result.
        state["port_8787_listener_count"] = len(port_rows(DRAMA_PORT, listening=True))
        state["port_8787_established_count"] = len(port_rows(DRAMA_PORT))
    except Exception as error:
        return state, False, type(error).__name__
    closed = (state["active"] == "inactive" and state["substate"] == "dead" and
              state["pid"] == 0 and state["control_pid"] == 0 and
              not state["control_group"] and state["port_8787_listener_count"] == 0 and
              state["port_8787_established_count"] == 0 and
              (not require_masked or state["enabled"] == "masked"))
    return state, closed, None


def fail_closed_drama(stage, shared_before, original_error, unit_backup):
    # All commands are scoped to the retired API. The shared reverse tunnel is
    # evidence-only and is never a systemctl command argument.
    commands = {
        "stop": best_effort_command(["systemctl", "stop", DRAMA_UNIT]),
        "disable": best_effort_command(["systemctl", "disable", DRAMA_UNIT]),
    }
    backup_available, backup_error = ((False, "MissingBackup") if unit_backup is None else
                                      verified_definition_backup(unit_backup))
    commands["verified_backup_available"] = {"value": backup_available,
                                               "error_type": backup_error}
    if backup_available:
        local = pathlib.Path("/etc/systemd/system") / DRAMA_UNIT
        retire_result = {"attempted": True, "ok": False, "error_type": None}
        try:
            if path_lexists(local) and not is_persistent_mask(local):
                retire_local_unit(local, unit_backup)
            retire_result["ok"] = True
        except Exception as error:
            retire_result["error_type"] = type(error).__name__
        commands["retire_local_unit"] = retire_result
        commands["mask"] = best_effort_command(["systemctl", "mask", DRAMA_UNIT])
        commands["daemon_reload"] = best_effort_command(["systemctl", "daemon-reload"])
    final_state, final_closed, verification_error = closure_state(require_masked=True)
    evidence_error = None
    try:
        write_failure_evidence(stage, type(original_error).__name__, commands,
                               final_state, final_closed, shared_before,
                               final_verification_error=verification_error)
    except Exception as error:
        evidence_error = type(error).__name__
    if not final_closed:
        message = "HIGH RISK: retired drama API closure could not be proven after fence failure"
        if evidence_error:
            message += "; failure evidence write also failed (%s)" % evidence_error
        raise RuntimeError(message) from original_error
    if evidence_error:
        raise RuntimeError("HIGH RISK: retired drama API closed but private failure evidence was not written (%s)" %
                           evidence_error) from original_error


def retire_local_unit(local, unit_backup):
    """/etc and /data are different filesystems on US: verify copy before unlink."""
    manifest = unit_backup / "definition.json"
    if manifest.is_file():
        definition = json.loads(manifest.read_text())
        expected = definition["fragment"]
        if expected.get("path") != str(local) or definition_path_record(str(local)) != expected:
            raise RuntimeError("local unit no longer matches its archived definition")
    retired = unit_backup / "retired-local.service"
    original = local.read_bytes()
    if manifest.is_file() and hashlib.sha256(original).hexdigest() != expected["content_sha256"]:
        raise RuntimeError("local unit content differs from its archived definition")
    if retired.exists():
        if retired.read_bytes() != original:
            raise RuntimeError("retired unit archive differs from current unit")
    else:
        with retired.open("xb") as output:
            output.write(original)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(str(retired), 0o600)
    if retired.read_bytes() != original or local.read_bytes() != original:
        raise RuntimeError("unit changed during retirement archive")
    if manifest.is_file() and definition_path_record(str(local)) != expected:
        raise RuntimeError("local unit changed before retirement")
    local.unlink()


def is_persistent_mask(local):
    try:
        return local.is_symlink() and local.resolve() == pathlib.Path("/dev/null")
    except (OSError, RuntimeError):
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("group", choices=sorted(GROUPS))
    ap.add_argument("--checkpoint", type=pathlib.Path)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--resume", action="store_true", help="resume a recorded partial fence with fresh drain proof")
    a = ap.parse_args()
    if socket.gethostname() != "VM-0-13-centos":
        raise RuntimeError("wrong host: US source only")
    source_storage_guard(create=False)
    states = [inspect(u) for u in GROUPS[a.group]]
    assert_idle(states)
    shared_before = None
    current_drama_definition = None
    if a.group == "drama":
        # A resume may skip the live definition only after the unit is already
        # persistently masked and has no process. A stopped/disabled but
        # unmasked unit can still gain a new fragment or drop-in and must be
        # compared with the original snapshot before it is retired.
        already_masked_without_process = (a.resume and states[0]["enabled"] == "masked" and
                                          states[0]["pid"] == 0)
        if not already_masked_without_process:
            states[0]["definition"] = unit_definition_snapshot(DRAMA_UNIT)
            current_drama_definition = states[0]["definition"]
        validate_drama_preflight(states, resume=a.resume)
        shared_before = shared_tunnel_snapshot()
        validate_shared_tunnel_baseline(shared_before)
    if not a.apply:
        result = {"dry_run": True, "group": a.group, "states": states}
        if shared_before is not None:
            result["shared_tunnel"] = shared_before
            result["port_8787_established"] = []
        print(json.dumps(result))
        return
    if a.checkpoint is None:
        raise RuntimeError("fresh coordinator checkpoint required")
    proof = json.loads(a.checkpoint.read_text())
    if proof.get("group") != a.group or proof.get("run_id") != RUN_ID:
        raise RuntimeError("checkpoint scope mismatch")
    if not 0 <= time.time() - float(proof.get("checked_at_epoch", 0)) <= 300:
        raise RuntimeError("checkpoint stale")
    for field in ("new_admission_closed", "triggers_paused", "cpu_drained"):
        if proof.get(field) is not True:
            raise RuntimeError("checkpoint not ready: " + field)
    # X publication ledgers remain on CPU and may contain a pre-existing
    # needs-review outcome. This media-only handoff must preserve that record,
    # not falsify a global no-unknown assertion or attempt a publication retry.
    outcome_field = "no_unknown_repairs" if a.group == "x" else "no_unknown"
    if proof.get(outcome_field) is not True:
        raise RuntimeError("checkpoint not ready: " + outcome_field)
    if a.group == "drama":
        validate_drama_checkpoint(proof)
    checkpoint_sha256 = hashlib.sha256(a.checkpoint.read_bytes()).hexdigest()
    source_storage_guard(create=True)
    snapshot = BASE / (a.group + "-before.json")
    if snapshot.exists() and not a.resume:
        raise RuntimeError("fence snapshot already exists; inspect partial result before retry")
    if a.resume:
        if not snapshot.is_file():
            raise RuntimeError("resume requires an existing initial fence snapshot")
        initial = json.loads(snapshot.read_text())
        if [s["unit"] for s in initial["states"]] != GROUPS[a.group]:
            raise RuntimeError("initial snapshot service scope changed")
        states = initial["states"]
        if a.group == "drama":
            if not states[0].get("definition"):
                raise RuntimeError("initial drama unit definition evidence is missing")
            if current_drama_definition and current_drama_definition != states[0]["definition"]:
                raise RuntimeError("drama unit definition changed since initial fence snapshot")
            if initial.get("shared_tunnel") != shared_before:
                raise RuntimeError("shared drama tunnel changed since initial fence snapshot")
            if initial.get("port_8787_established") != []:
                raise RuntimeError("initial drama request proof is invalid")
    else:
        snapshot_payload = {"checkpoint_sha256": checkpoint_sha256,
                            "states": states, "created_at_epoch": time.time()}
        if a.group == "drama":
            snapshot_payload["shared_tunnel"] = shared_before
            snapshot_payload["port_8787_established"] = []
        write_private_json(snapshot, snapshot_payload)
    mutation_started = False
    stage = "pre-mutation"
    try:
        for state in states:
            u = state["unit"]
            unit_backup = BASE / u
            if prop(u, "UnitFileState") == "masked":
                if int(prop(u, "MainPID") or 0) or prop(u, "ActiveState") not in ("inactive", "failed"):
                    raise RuntimeError("masked source is still active: " + u)
                if a.group == "drama":
                    backup_ok, backup_error = verified_definition_backup(unit_backup)
                    if not backup_ok:
                        raise RuntimeError("masked drama source definition backup is not verified: " +
                                           str(backup_error))
                    final_state, shared_after = validate_drama_fenced(shared_before)
                    write_drama_success_evidence(final_state, shared_before, shared_after,
                                                 checkpoint_sha256)
                print(json.dumps({"already_fenced": u}))
                continue
            stage = "backup-unit-definition"
            unit_backup.mkdir(mode=0o700, exist_ok=a.resume)
            backup_unit_definition(state, unit_backup, resume=a.resume)
            if a.group == "drama":
                backup_ok, backup_error = verified_definition_backup(unit_backup)
                if not backup_ok:
                    raise RuntimeError("drama source unit was not fully archived before mutation: " +
                                       str(backup_error))
                # Reassert the request/process and shared-tunnel guards as the
                # last read-only action before stopping the old source.
                current_source = inspect(DRAMA_UNIT)
                validate_drama_preflight([current_source], resume=a.resume)
                if unit_definition_snapshot(DRAMA_UNIT) != state.get("definition"):
                    raise RuntimeError("drama source unit definition changed before stop")
                if shared_tunnel_snapshot() != shared_before:
                    raise RuntimeError("shared drama tunnel changed before source stop")
            stage = "stop-source"
            mutation_started = True
            run(["systemctl", "stop", u])
            stage = "disable-source"
            run(["systemctl", "disable", u])
            if int(prop(u, "MainPID") or 0) or prop(u, "ActiveState") not in ("inactive", "failed"):
                raise RuntimeError("source still running: " + u)
            stage = "retire-local-unit"
            local = pathlib.Path("/etc/systemd/system") / u
            if path_lexists(local) and not is_persistent_mask(local):
                retire_local_unit(local, unit_backup)
            stage = "mask-source"
            run(["systemctl", "mask", u])
            run(["systemctl", "daemon-reload"])
            if prop(u, "UnitFileState") != "masked":
                raise RuntimeError("persistent mask verification failed: " + u)
            if a.group == "drama":
                stage = "verify-final-drama-fence"
                final_state, shared_after = validate_drama_fenced(shared_before)
                final = {"fenced": u, "active": final_state["active"],
                         "substate": final_state["substate"], "enabled": final_state["enabled"],
                         "pid": final_state["pid"], "port_8787_listener": False,
                         "port_8787_process": False,
                         "shared_tunnel_unchanged": shared_after == shared_before}
                write_drama_success_evidence(final_state, shared_before, shared_after,
                                             checkpoint_sha256)
                print(json.dumps(final))
            else:
                print(json.dumps({"fenced": u, "active": prop(u, "ActiveState"),
                                  "enabled": prop(u, "UnitFileState")}))
    except Exception as original_error:
        if a.group == "drama" and mutation_started:
            fail_closed_drama(stage, shared_before, original_error, unit_backup)
        raise


if __name__ == "__main__":
    main()
