#!/usr/bin/env python3
"""Scoped CPU maintenance gates. Dry-run unless --apply; no publishing calls."""
import argparse
import contextlib
import errno
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import time
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - production target is Linux.
    fcntl = None

RUN_ID = "gpu-service-migration-20260828T1502"
DATA_ROOT = pathlib.Path("/mnt/data-disk")
BASE = DATA_ROOT / "migrations" / RUN_ID / "control"
MAP = pathlib.Path("/etc/nginx/conf.d/00-gpu-service-migration-map.conf")
GATE = pathlib.Path("/etc/nginx/default.d/00-gpu-service-migration-gate.conf")
CPU_UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
PATTERNS = {
    # Drama rendering has completed its independent CPU/HK cutover. Keep the
    # remaining image/ad migration fenced without blocking the live drama API.
    "materials": r"/api/(ad-material|drama-screenshot-material)(/|$)",
    "tt": r"/api/admin/(tt-posts|tt-auto-publish)(/|$)",
    "x": r"/api/admin/(x-posts|x-auto-posts|x-auto-publish)(/|$)",
}
TRIGGERS = {
    "tt": ["tt-post-prepare.timer", "tt-post-prepare.path", "tt-post-runner.timer",
           "tt-post-runner.path", "tt-auto-post-scheduler.timer",
           "tt-auto-post-runner.timer", "tt-auto-post-runner.path"],
    "x": ["x-post-daily.timer", "x-post-manual.timer", "x-post-schedule-claim.timer",
          "x-post-schedule.timer", "x-auto-post-scheduler.timer",
          "x-auto-post-runner.timer", "x-auto-post-runner.path"],
    "materials": ["ad-material-frontend-test.service", "drama-material-api-test.service"],
}
RUNNING_STATES = ("active", "activating")
JOURNAL_VERSION = 2
ACCEPTED_JOURNAL_VERSIONS = (None, JOURNAL_VERSION)
JOURNAL_PHASES = ("prepared", "pausing", "paused", "pause_rollback",
                  "pause_rollback_incomplete", "resuming", "resume_incomplete",
                  "restored")
CRON_MARKER = "# " + RUN_ID + " PAUSED "
MATERIALS_CRON_SNAPSHOT = BASE / "materials-crontab-before.txt"
SNAPSHOT_DIR = BASE / "snapshots"
CONTROL_LOCK = BASE / ".maintenance.lock"


def run(args, check=True, input_text=None):
    p = subprocess.run(args, input=input_text, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True)
    if check and p.returncode:
        raise RuntimeError("command failed: %s: %s" % (args[0], p.stderr.strip()))
    return p


def require_root_identity():
    if not hasattr(os, "geteuid") or not hasattr(os, "getegid"):
        raise RuntimeError("maintenance control requires Linux effective IDs")
    if os.geteuid() != 0 or os.getegid() != 0:
        raise RuntimeError("maintenance control requires euid=egid=0")


def fsync_directory(path):
    if not hasattr(os, "O_DIRECTORY"):
        return
    directory_fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def mode_bits(value):
    return stat.S_IMODE(value.st_mode)


def validate_directory_node(path, value, exact_mode=None):
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise RuntimeError("unsafe control directory type: " + str(path))
    if value.st_uid != 0 or value.st_gid != 0:
        raise RuntimeError("control directory is not root:root: " + str(path))
    current_mode = mode_bits(value)
    if exact_mode is not None and current_mode != exact_mode:
        raise RuntimeError("control directory mode is not %04o: %s" %
                           (exact_mode, str(path)))
    if exact_mode is None and current_mode & 0o022:
        raise RuntimeError("control parent directory is group/world writable: " + str(path))


def secure_control_base(create=True):
    """Establish the static root-owned data-disk trust chain before BASE I/O."""
    require_root_identity()
    expected = DATA_ROOT / "migrations" / RUN_ID / "control"
    if not DATA_ROOT.is_absolute() or BASE != expected:
        raise RuntimeError("control base is outside the static data-root layout")
    nodes = [DATA_ROOT, DATA_ROOT / "migrations",
             DATA_ROOT / "migrations" / RUN_ID, expected]
    for index, path in enumerate(nodes):
        created = False
        try:
            value = os.lstat(str(path))
        except OSError as error:
            if error.errno != errno.ENOENT or not create or index == 0:
                raise RuntimeError("control directory is unavailable: " + str(path))
            os.mkdir(str(path), 0o700)
            created = True
            value = os.lstat(str(path))
        validate_directory_node(path, value,
                                0o700 if created or path == BASE else None)
    resolved_root = DATA_ROOT.resolve(strict=True)
    resolved_base = BASE.resolve(strict=True)
    if resolved_root != DATA_ROOT:
        raise RuntimeError("data root resolves through an unexpected path")
    try:
        relative = resolved_base.relative_to(resolved_root)
    except ValueError:
        raise RuntimeError("resolved control base escaped the data root")
    if relative != pathlib.Path("migrations") / RUN_ID / "control":
        raise RuntimeError("resolved control base identity changed")
    mount_lines = run(["findmnt", "-n", "-o", "TARGET,UUID", "-T",
                       str(BASE)]).stdout.splitlines()
    fields = mount_lines[0].split() if len(mount_lines) == 1 else []
    if len(fields) != 2 or fields[0] != str(DATA_ROOT) or fields[1] != CPU_UUID:
        raise RuntimeError("control base is not on the expected CPU data disk")
    validate_directory_node(BASE, os.lstat(str(BASE)), 0o700)
    if not os.access(str(BASE), os.W_OK):
        raise RuntimeError("control base is not writable")
    return BASE


def secure_snapshot_directory():
    path = SNAPSHOT_DIR
    if path != BASE / "snapshots":
        raise RuntimeError("snapshot directory identity changed")
    try:
        value = os.lstat(str(path))
        created = False
    except OSError as error:
        if error.errno != errno.ENOENT:
            raise RuntimeError("snapshot directory is unavailable")
        os.mkdir(str(path), 0o700)
        created = True
        value = os.lstat(str(path))
    validate_directory_node(path, value, 0o700)
    if created:
        fsync_directory(BASE)
    return path


def private_path(path):
    path = pathlib.Path(path)
    if path.parent == BASE or path.parent == SNAPSHOT_DIR:
        return path
    raise RuntimeError("private control artifact escaped the control directory")


def validate_private_stat(path, value):
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise RuntimeError("private control artifact is not a regular file: " + str(path))
    if value.st_uid != 0 or value.st_gid != 0 or mode_bits(value) != 0o600:
        raise RuntimeError("private control artifact must be root:root mode 0600: " + str(path))


def validate_private_file(path):
    require_root_identity()
    path = private_path(path)
    try:
        value = os.lstat(str(path))
    except OSError:
        raise RuntimeError("private control artifact is missing: " + str(path))
    validate_private_stat(path, value)
    return value


def same_file_identity(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def read_private_text(path):
    before = validate_private_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags)
    try:
        opened = os.fstat(fd)
        validate_private_stat(path, opened)
        if not same_file_identity(before, opened):
            raise RuntimeError("private control artifact changed before read")
        with os.fdopen(fd, "r", encoding="utf-8", newline="") as handle:
            fd = None
            text = handle.read()
            after = os.fstat(handle.fileno())
            if not same_file_identity(opened, after):
                raise RuntimeError("private control artifact changed during read")
            return text
    finally:
        if fd is not None:
            os.close(fd)


def read_optional_private_text(path):
    require_root_identity()
    path = private_path(path)
    try:
        os.lstat(str(path))
    except OSError as error:
        if error.errno == errno.ENOENT:
            return None
        raise
    return read_private_text(path)


def storage_guard():
    uuid = run(["findmnt", "-n", "-o", "UUID", "--target", "/mnt/data-disk"]).stdout.strip()
    target = run(["findmnt", "-n", "-o", "TARGET", "--target", "/mnt/data-disk"]).stdout.strip()
    if target != "/mnt/data-disk" or uuid != CPU_UUID or not os.access(target, os.W_OK):
        raise RuntimeError("CPU data disk guard failed")


def atomic_write(path, data, mode=0o600):
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise RuntimeError("atomic-write parent is missing or unsafe")
    fd, tmp = tempfile.mkstemp(prefix=".migration-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
        fsync_directory(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
            fsync_directory(path.parent)


def write_private_text(path, data):
    require_root_identity()
    path = private_path(path)
    atomic_write(path, data, 0o600)
    validate_private_file(path)


def create_private_text(path, data):
    """Create a never-overwritten private artifact after its content is durable."""
    require_root_identity()
    path = private_path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    complete = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        complete = True
        fsync_directory(path.parent)
        validate_private_file(path)
    finally:
        if fd is not None:
            os.close(fd)
        if not complete:
            try:
                os.unlink(str(path))
                fsync_directory(path.parent)
            except OSError:
                pass


def validate_regular_file(path, value):
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise RuntimeError("configuration target is not a regular file: " + str(path))
    if value.st_uid != 0 or value.st_gid != 0 or mode_bits(value) != 0o644:
        raise RuntimeError("configuration target must be root:root mode 0644: " + str(path))


def read_optional_regular_bytes(path):
    require_root_identity()
    try:
        before = os.lstat(str(path))
    except OSError as error:
        if error.errno == errno.ENOENT:
            return None
        raise
    validate_regular_file(path, before)
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        validate_regular_file(path, opened)
        if not same_file_identity(before, opened):
            raise RuntimeError("configuration target changed before read")
        with os.fdopen(fd, "rb") as handle:
            fd = None
            return handle.read()
    finally:
        if fd is not None:
            os.close(fd)


def safe_unlink(path, private=False):
    require_root_identity()
    try:
        before = os.lstat(str(path))
    except OSError as error:
        if error.errno == errno.ENOENT:
            return
        raise
    if private:
        private_path(path)
        validate_private_stat(path, before)
    else:
        validate_regular_file(path, before)
    current = os.lstat(str(path))
    if not same_file_identity(before, current):
        raise RuntimeError("refuse to unlink a changed target")
    os.unlink(str(path))
    fsync_directory(path.parent)


@contextlib.contextmanager
def control_lock():
    require_root_identity()
    if fcntl is None:
        raise RuntimeError("POSIX flock is required for maintenance control")
    path = private_path(CONTROL_LOCK)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        fsync_directory(BASE)
        value = os.fstat(fd)
        validate_private_stat(path, value)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            raise RuntimeError("another maintenance control operation is running")
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextlib.contextmanager
def control_transaction():
    secure_control_base(create=True)
    with control_lock():
        yield


def gate_text(groups):
    lines = ['map "$request_method:$uri" $gpu_service_migration_block {', '    default 0;']
    if "materials" in groups:
        # The deployed legacy handler accepts a JSON-body GET at this exact
        # batch path and submits jobs. Ordinary GET job/list queries stay open.
        lines.append('    "~^[A-Z]+:/api/drama-screenshot-material/jobs/batch$" 1;')
    for group in sorted(groups):
        lines.append('    "~^(POST|PUT|PATCH|DELETE):%s" 1;' % PATTERNS[group])
    lines.append("}\n")
    body = ('if ($gpu_service_migration_block) {\n'
            '    return 503 \'{"error":"service_migration_maintenance",'
            '"message":"业务迁移中，请稍后重试"}\';\n}\n')
    return "\n".join(lines), body


def gate(group, enabled):
    with control_transaction():
        return gate_locked(group, enabled)


def gate_locked(group, enabled):
    state_path = BASE / "gates.json"
    state_text = read_optional_private_text(state_path)
    state = json.loads(state_text) if state_text is not None else {"groups": []}
    if (not isinstance(state.get("groups"), list) or
            len(state["groups"]) != len(set(state["groups"])) or
            any(item not in PATTERNS for item in state["groups"])):
        raise RuntimeError("maintenance gate state is invalid")
    groups = set(state["groups"])
    if enabled:
        groups.add(group)
    else:
        groups.discard(group)
    old = {p: read_optional_regular_bytes(p) for p in (MAP, GATE)}
    old_state = state_text
    reload_attempted = False
    try:
        if groups:
            m, g = gate_text(groups)
            atomic_write(MAP, m, 0o644)
            atomic_write(GATE, g, 0o644)
            read_optional_regular_bytes(MAP)
            read_optional_regular_bytes(GATE)
        else:
            for p in (MAP, GATE):
                safe_unlink(p)
        write_private_text(state_path,
                           json.dumps({"groups": sorted(groups)}, indent=2))
        run(["nginx", "-t"])
        reload_attempted = True
        run(["systemctl", "reload", "nginx"])
    except Exception as original_error:
        rollback_errors = []
        for path, data in list(old.items()) + [(state_path, old_state)]:
            try:
                if data is None:
                    safe_unlink(path, private=(path == state_path))
                else:
                    if path in (MAP, GATE):
                        atomic_write(path, data.decode("utf-8"), 0o644)
                        read_optional_regular_bytes(path)
                    else:
                        write_private_text(path, data)
            except Exception as error:
                rollback_errors.append({"target": str(path),
                                        "error_type": type(error).__name__})
        if reload_attempted:
            try:
                run(["nginx", "-t"])
                run(["systemctl", "reload", "nginx"])
            except Exception as error:
                rollback_errors.append({"target": "nginx-runtime",
                                        "error_type": type(error).__name__})
        if rollback_errors:
            raise RuntimeError("HIGH RISK: maintenance gate rollback failed: " +
                               json.dumps(rollback_errors, sort_keys=True)) from original_error
        raise
    print(json.dumps({"active_maintenance_groups": sorted(groups)}))


def digest_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def journal_path(group):
    return BASE / (group + "-triggers.json")


def read_unit_states(group):
    states = {}
    for unit in TRIGGERS[group]:
        result = run(["systemctl", "is-active", unit], check=False)
        value = result.stdout.strip()
        if not value:
            raise RuntimeError("systemctl returned an empty state for " + unit)
        states[unit] = value
    return states


def read_unit_state(unit):
    result = run(["systemctl", "is-active", unit], check=False)
    value = result.stdout.strip()
    if not value:
        raise RuntimeError("systemctl returned an empty state for " + unit)
    return value


def read_crontab():
    result = run(["crontab", "-l"], check=False)
    if result.returncode:
        raise RuntimeError("cannot read the exact root crontab")
    return result.stdout


def paused_crontab(original):
    lines = original.splitlines()
    candidates = [index for index, line in enumerate(lines)
                  if not line.lstrip().startswith("#") and
                  "run_auto_cover_synthesis.sh" in line]
    if len(candidates) != 1:
        raise RuntimeError("expected exactly one screenshot cron entry")
    lines[candidates[0]] = CRON_MARKER + lines[candidates[0]]
    return "\n".join(lines) + "\n"


def cron_contract_from_text(original, snapshot_path):
    paused = paused_crontab(original)
    return {"required": True, "snapshot_path": str(snapshot_path),
            "compatibility_snapshot_path": str(MATERIALS_CRON_SNAPSHOT),
            "before_sha256": digest_text(original),
            "paused_sha256": digest_text(paused)}


def is_materials_snapshot_path(path):
    if path == MATERIALS_CRON_SNAPSHOT:
        return True
    return (path.parent == SNAPSHOT_DIR and
            path.name.startswith("materials-crontab-") and path.suffix == ".txt")


def create_cycle_snapshot(original):
    secure_snapshot_directory()
    for unused in range(8):
        path = SNAPSHOT_DIR / ("materials-crontab-" + uuid.uuid4().hex + ".txt")
        try:
            create_private_text(path, original)
            return path
        except OSError as error:
            if error.errno != errno.EEXIST:
                raise
    raise RuntimeError("could not allocate a unique materials cron snapshot")


def cron_texts(state):
    if state["group"] != "materials":
        return None, None
    path = pathlib.Path(state["cron"]["snapshot_path"])
    if not is_materials_snapshot_path(path):
        raise RuntimeError("materials cron snapshot is missing or unsafe")
    original = read_private_text(path)
    paused = paused_crontab(original)
    if (digest_text(original) != state["cron"].get("before_sha256") or
            digest_text(paused) != state["cron"].get("paused_sha256")):
        raise RuntimeError("materials cron snapshot changed")
    return original, paused


def ensure_compatibility_snapshot(state):
    original, unused = cron_texts(state)
    current = read_optional_private_text(MATERIALS_CRON_SNAPSHOT)
    if current != original:
        write_private_text(MATERIALS_CRON_SNAPSHOT, original)


def normalize_journal(group, state):
    if not isinstance(state, dict) or not isinstance(state.get("original"), dict):
        raise RuntimeError("maintenance trigger journal is invalid")
    if set(state["original"]) != set(TRIGGERS[group]):
        raise RuntimeError("maintenance trigger journal scope changed")
    if state.get("version") not in ACCEPTED_JOURNAL_VERSIONS:
        raise RuntimeError("unsupported maintenance trigger journal version")
    if state.get("run_id") not in (None, RUN_ID) or state.get("group") not in (None, group):
        raise RuntimeError("maintenance trigger journal identity changed")
    if type(state.get("restored")) is not bool:
        raise RuntimeError("maintenance trigger journal restored flag is invalid")
    if ("revision" in state and
            (type(state["revision"]) is not int or state["revision"] < 0)):
        raise RuntimeError("maintenance trigger journal revision is invalid")
    if ("history" in state and
            (not isinstance(state["history"], list) or
             any(not isinstance(item, dict) for item in state["history"]))):
        raise RuntimeError("maintenance trigger journal history is invalid")
    if any(not isinstance(value, str) or not value for value in state["original"].values()):
        raise RuntimeError("maintenance trigger journal unit state is invalid")
    result = dict(state)
    result.update({"version": JOURNAL_VERSION, "run_id": RUN_ID, "group": group})
    result.setdefault("phase", "restored" if result["restored"] else "paused")
    result.setdefault("revision", 0)
    if result["phase"] not in JOURNAL_PHASES:
        raise RuntimeError("maintenance trigger journal phase is invalid")
    if result["restored"] != (result["phase"] == "restored"):
        raise RuntimeError("maintenance trigger journal phase/restored state conflicts")
    if group == "materials" and not isinstance(result.get("cron"), dict):
        before = read_optional_private_text(MATERIALS_CRON_SNAPSHOT)
        if before is None:
            raise RuntimeError("legacy materials journal has no safe cron snapshot")
        result["cron"] = cron_contract_from_text(before, MATERIALS_CRON_SNAPSHOT)
    if group != "materials":
        result["cron"] = {"required": False}
    else:
        cron_texts(result)
    return result


def write_journal(path, state, phase, restored, errors=None, current=None):
    if phase not in JOURNAL_PHASES or bool(restored) != (phase == "restored"):
        raise RuntimeError("refuse invalid maintenance journal phase transition")
    updated = dict(state)
    updated_at = time.time()
    history = list(state.get("history", []))
    previous_errors = state.get("errors")
    if previous_errors:
        previous = {"phase": state.get("phase", "unknown"),
                    "errors": previous_errors}
        if (not history or history[-1].get("phase") != previous["phase"] or
                history[-1].get("errors") != previous["errors"]):
            previous["archived_at_epoch"] = updated_at
            history.append(previous)
    updated.update({"version": JOURNAL_VERSION, "run_id": RUN_ID,
                    "phase": phase, "restored": bool(restored),
                    "revision": int(state.get("revision", 0)) + 1,
                    "updated_at_epoch": updated_at, "history": history})
    if errors is not None:
        updated["errors"] = errors
    if current is not None:
        updated["current"] = current
    write_private_text(path, json.dumps(updated, indent=2))
    return updated


def current_snapshot(group, state):
    units = read_unit_states(group)
    cron = None
    if group == "materials":
        current = read_crontab()
        original, paused = cron_texts(state)
        if current == original:
            cron = "original"
        elif current == paused:
            cron = "paused"
        else:
            cron = "drift"
    return {"units": units, "cron": cron}


def original_units_restored(original, current):
    for unit, desired in original.items():
        actual = current.get(unit)
        if actual != desired:
            return False
    return True


def paused_units(states):
    return bool(states) and all(value == "inactive" for value in states.values())


def converge_paused(group, state):
    errors = []
    if group == "materials":
        try:
            ensure_compatibility_snapshot(state)
            original, paused = cron_texts(state)
            current = read_crontab()
            if current == original:
                run(["crontab", "-"], input_text=paused)
            elif current != paused:
                errors.append({"action": "pause-cron", "error_type": "CronDrift"})
        except Exception as error:
            errors.append({"action": "pause-cron", "error_type": type(error).__name__})
    if not errors:
        for unit in TRIGGERS[group]:
            try:
                current = read_unit_state(unit)
                if current in RUNNING_STATES:
                    run(["systemctl", "stop", unit])
            except Exception as error:
                errors.append({"action": "stop", "unit": unit,
                               "error_type": type(error).__name__})
                break
    try:
        current = current_snapshot(group, state)
    except Exception as error:
        errors.append({"action": "verify-paused", "error_type": type(error).__name__})
        current = {"units": {}, "cron": "unavailable" if group == "materials" else None}
    exact = (not errors and paused_units(current["units"]) and
             (group != "materials" or current["cron"] == "paused"))
    if not exact and not errors:
        errors.append({"action": "verify-paused", "error_type": "StateMismatch"})
    return exact, errors, current


def converge_original(group, state):
    errors = []
    original_states = state["original"]
    for unit in TRIGGERS[group]:
        desired = original_states[unit]
        try:
            current = read_unit_state(unit)
            if desired == "active" and current != "active":
                run(["systemctl", "start", unit])
            elif desired == "inactive" and current != "inactive":
                run(["systemctl", "stop", unit])
            elif desired not in RUNNING_STATES + ("inactive",) and current != desired:
                errors.append({"action": "restore-state", "unit": unit,
                               "error_type": "UnsupportedStateDrift"})
        except Exception as error:
            errors.append({"action": "restore-state", "unit": unit,
                           "error_type": type(error).__name__})
    if group == "materials":
        try:
            original, paused = cron_texts(state)
            current_cron = read_crontab()
            if current_cron == paused:
                run(["crontab", "-"], input_text=original)
            elif current_cron != original:
                errors.append({"action": "restore-cron", "error_type": "CronDrift"})
        except Exception as error:
            errors.append({"action": "restore-cron", "error_type": type(error).__name__})
    try:
        current = current_snapshot(group, state)
    except Exception as error:
        errors.append({"action": "verify-restored", "error_type": type(error).__name__})
        current = {"units": {}, "cron": "unavailable" if group == "materials" else None}
    exact = (not errors and original_units_restored(original_states, current["units"]) and
             (group != "materials" or current["cron"] == "original"))
    if not exact and not errors:
        errors.append({"action": "verify-restored", "error_type": "StateMismatch"})
    return exact, errors, current


def failure_item(action, error, **values):
    item = {"action": action, "error_type": type(error).__name__}
    item.update(values)
    return item


def rollback_failed_pause(group, path, state, original_error, pause_errors=None):
    intermediate_journal_error = None
    initiating_errors = list(pause_errors or [])
    initiating_errors.insert(0, failure_item("pause", original_error))
    try:
        state = write_journal(path, state, "pause_rollback", False,
                              errors=initiating_errors)
    except Exception as error:
        intermediate_journal_error = type(error).__name__
        initiating_errors.append(failure_item("write-pause-rollback-journal", error))
    exact, errors, current = converge_original(group, state)
    final_errors = initiating_errors + errors
    final_journal_error = None
    try:
        write_journal(path, state, "restored" if exact else "pause_rollback_incomplete",
                      exact, errors=final_errors, current=current)
    except Exception as error:
        final_journal_error = type(error).__name__
    if exact and final_journal_error is None:
        raise original_error
    message = "maintenance pause failed and exact rollback could not be recorded"
    if not exact:
        message = "maintenance pause failed and exact rollback is incomplete"
    if final_journal_error:
        message += "; journal_error=" + final_journal_error
    elif intermediate_journal_error:
        message += "; intermediate_journal_error=" + intermediate_journal_error
    raise RuntimeError(message) from original_error


def new_pause_journal(group):
    original = read_unit_states(group)
    if any(value not in ("active", "inactive") for value in original.values()):
        raise RuntimeError("trigger state must be stable active or inactive before pausing")
    state = {"version": JOURNAL_VERSION, "run_id": RUN_ID, "group": group,
             "original": original, "restored": False, "phase": "prepared",
             "revision": 0, "cron": {"required": False}}
    if group == "materials":
        original_cron = read_crontab()
        snapshot_path = create_cycle_snapshot(original_cron)
        state["cron"] = cron_contract_from_text(original_cron, snapshot_path)
    path = journal_path(group)
    return path, write_journal(path, state, "prepared", False)


def pause(group):
    with control_transaction():
        return pause_locked(group)


def pause_locked(group):
    path = journal_path(group)
    prior_text = read_optional_private_text(path)
    if prior_text is not None:
        prior = normalize_journal(group, json.loads(prior_text))
        if not prior["restored"]:
            state = prior
            if group == "materials":
                ensure_compatibility_snapshot(state)
            try:
                current = current_snapshot(group, state)
            except Exception:
                current = None
            if (current is not None and paused_units(current["units"]) and
                    (group != "materials" or current["cron"] == "paused")):
                state = write_journal(path, state, "paused", False,
                                      errors=[], current=current)
                print(json.dumps({"paused": group, "original": state["original"],
                                  "idempotent": True}))
                return
        else:
            path, state = new_pause_journal(group)
    else:
        path, state = new_pause_journal(group)
    pause_errors = []
    try:
        state = write_journal(path, state, "pausing", False, errors=[])
        exact, errors, current = converge_paused(group, state)
        pause_errors = errors
        if not exact:
            raise RuntimeError("maintenance pause did not reach the exact paused state")
        state = write_journal(path, state, "paused", False,
                              errors=errors, current=current)
    except Exception as original_error:
        rollback_failed_pause(group, path, state, original_error, pause_errors)
    print(json.dumps({"paused": group, "original": state["original"],
                      "idempotent": False}))


def resume(group):
    with control_transaction():
        return resume_locked(group)


def resume_locked(group):
    path = journal_path(group)
    state_text = read_optional_private_text(path)
    if state_text is None:
        raise RuntimeError("maintenance trigger journal is missing")
    state = normalize_journal(group, json.loads(state_text))
    if state["restored"]:
        current = current_snapshot(group, state)
        if (not original_units_restored(state["original"], current["units"]) or
                (group == "materials" and current["cron"] != "original")):
            raise RuntimeError("restored maintenance journal no longer matches live state")
        print(json.dumps({"restored": group, "idempotent": True}))
        return
    state = write_journal(path, state, "resuming", False, errors=[])
    exact, errors, current = converge_original(group, state)
    state = write_journal(path, state, "restored" if exact else "resume_incomplete",
                          exact, errors=errors, current=current)
    if not exact:
        raise RuntimeError("maintenance resume is incomplete; retry resume after reviewing the journal")
    print(json.dumps({"restored": group, "idempotent": False}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["gate-on", "gate-off", "pause", "resume"])
    ap.add_argument("group", choices=sorted(PATTERNS))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    storage_guard()
    if not a.apply:
        print(json.dumps({"dry_run": True, "action": a.action, "group": a.group,
                          "triggers": TRIGGERS[a.group], "pattern": PATTERNS[a.group]}))
        return
    if a.action.startswith("gate-"):
        gate(a.group, a.action == "gate-on")
    else:
        globals()[a.action](a.group)


if __name__ == "__main__":
    main()
