#!/usr/bin/env python3
"""Fence only approved US services after a fresh coordinator drain checkpoint."""
import argparse
import contextlib
import hashlib
import json
import os
import pathlib
import posixpath
import re
import shutil
import socket
import stat
import subprocess
import time

RUN_ID = "gpu-service-migration-20260828T1502"
DATA_ROOT = pathlib.Path("/data")
BASE = DATA_ROOT / "migrations" / RUN_ID / "source-fence"
MATERIAL_IMAGE_UNITS = [
    "codex-cover-generator.service", "codex-screenshot-batch.service",
    "codex-screenshot-batch-burst.service", "codex-screenshot-square.service",
    "codex-screenshot-portrait.service", "codex-screenshot-landscape.service",
    "gpu-worker-reverse-tunnel.service",
    "gpu-screenshot-batch-burst-tunnel.service",
]
MATERIAL_AD_UNITS = [
    "ad-material-generation.service", "ad-material-vision.service",
    "gpu-ad-only-reverse-tunnel.service",
]
LEGACY_MATERIAL_UNITS = MATERIAL_AD_UNITS[:2] + MATERIAL_IMAGE_UNITS
GROUPS = {
    # Retained for read-only inventory compatibility. Applying this legacy
    # coupled scope is prohibited after the ad-only tunnel split.
    "materials": LEGACY_MATERIAL_UNITS,
    "materials-images": MATERIAL_IMAGE_UNITS,
    "materials-ad": MATERIAL_AD_UNITS,
    "drama": ["drama-material-api.service"],
    "tt": ["tt-gpu-publisher.service", "tt-gpu-direct-outro.service",
           "tt-gpu-reverse-tunnel.service", "tt-gpu-direct-outro-reverse-tunnel.service"],
    "x": ["x-post-media-repair.service", "x-post-media-repair-tunnel.service"],
}
AD_ONLY_TUNNEL = "gpu-ad-only-reverse-tunnel.service"
LEGACY_MATERIAL_TUNNELS = {
    "gpu-worker-reverse-tunnel.service",
    "gpu-screenshot-batch-burst-tunnel.service",
}
DRAMA_UNIT = "drama-material-api.service"
DRAMA_SHARED_TUNNEL = "gpu-worker-reverse-tunnel.service"
DRAMA_PORT = 8787
DRAMA_LOCAL_FRAGMENT = pathlib.Path("/etc/systemd/system") / DRAMA_UNIT
DRAMA_DROPIN_DIR = pathlib.Path("/etc/systemd/system") / (DRAMA_UNIT + ".d")
DRAMA_WANTS_LINK = pathlib.Path("/etc/systemd/system/multi-user.target.wants") / DRAMA_UNIT
SYSTEMD_UNIT_PATH_ALLOWLIST = (
    "/etc/systemd/system.control", "/run/systemd/system.control",
    "/run/systemd/transient", "/run/systemd/generator.early",
    "/etc/systemd/system", "/etc/systemd/system.attached",
    "/run/systemd/system", "/run/systemd/system.attached",
    "/run/systemd/generator", "/usr/local/lib/systemd/system",
    "/usr/lib/systemd/system", "/run/systemd/generator.late",
)
SYSTEMD_REQUIRED_UNIT_PATHS = ("/etc/systemd/system", "/run/systemd/system",
                               "/usr/lib/systemd/system")
SYSTEMD_VOLATILE_UNIT_PATHS = (
    "/etc/systemd/system.control", "/run/systemd/system.control",
    "/run/systemd/transient", "/run/systemd/generator.early",
    "/run/systemd/generator", "/run/systemd/generator.late",
)
MAX_SYSTEMD_SYMLINK_HOPS = 16
SYMLINK_IDENTITY_FIELDS = ("path", "kind", "link_target",
                           "link_target_sha256", "target_path",
                           "resolved_path", "lstat")


def run(args, check=True):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    if check and p.returncode:
        raise RuntimeError("command failed: %s: %s" % (args[0], p.stderr.strip()))
    return p


def prop(unit, name):
    return run(["systemctl", "show", unit, "-p", name, "--value"]).stdout.strip()


def parse_systemd_unit_paths(raw, source):
    values = raw.split()
    if not values or len(values) != len(set(values)):
        raise RuntimeError(source + " systemd unit paths are missing or duplicated")
    allowed_positions = {value: index for index, value in
                         enumerate(SYSTEMD_UNIT_PATH_ALLOWLIST)}
    positions = []
    for value in values:
        if (value not in allowed_positions or not value.startswith("/") or
                str(pathlib.PurePosixPath(value)) != value):
            raise RuntimeError(source + " systemd unit path is outside the approved CentOS scope")
        positions.append(allowed_positions[value])
    if positions != sorted(positions):
        raise RuntimeError(source + " systemd unit path precedence changed")
    if not set(SYSTEMD_REQUIRED_UNIT_PATHS).issubset(values):
        raise RuntimeError(source + " systemd unit paths omit a required root")
    return values


def systemd_unit_root_record(raw):
    path = pathlib.Path(raw)
    if not path_lexists(path):
        return {"path": raw, "kind": "absent"}
    if path_is_symlink(path) or not path.is_dir():
        raise RuntimeError("systemd unit path root is not a real directory")
    identity = os.lstat(str(path))
    if os.name != "nt":
        validate_source_directory_identity(identity, require_private=False)
    return {"path": raw, "kind": "directory",
            "lstat": {"device": identity.st_dev, "inode": identity.st_ino,
                      "mode": identity.st_mode, "uid": getattr(identity, "st_uid", 0),
                      "gid": getattr(identity, "st_gid", 0)}}


def systemd_unit_path_snapshot():
    manager = parse_systemd_unit_paths(
        run(["systemctl", "show", "--property=UnitPath", "--value"]).stdout,
        "manager")
    analyzed = parse_systemd_unit_paths(
        run(["systemd-analyze", "unit-paths"]).stdout, "systemd-analyze")
    if not set(manager).issubset(analyzed):
        raise RuntimeError("manager UnitPath is not a subset of systemd-analyze unit paths")
    roots = [systemd_unit_root_record(value) for value in analyzed]
    manager_after = parse_systemd_unit_paths(
        run(["systemctl", "show", "--property=UnitPath", "--value"]).stdout,
        "manager")
    analyzed_after = parse_systemd_unit_paths(
        run(["systemd-analyze", "unit-paths"]).stdout, "systemd-analyze")
    roots_after = [systemd_unit_root_record(value) for value in analyzed_after]
    if manager_after != manager or analyzed_after != analyzed or roots_after != roots:
        raise RuntimeError("systemd unit path identity changed while it was inspected")
    return {"schema_version": 1, "manager": manager, "analyzed": analyzed,
            "roots": roots}


def path_lexists(path):
    return os.path.lexists(str(path))


def path_is_symlink(path):
    return path.is_symlink()


def fsync_directory(path):
    """Persist a directory entry boundary; inability to prove durability is fatal."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags)
    try:
        identity = os.fstat(fd)
        if not stat.S_ISDIR(identity.st_mode):
            raise RuntimeError("durability boundary is not a directory")
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_private_file_identity(identity):
    if not stat.S_ISREG(identity.st_mode):
        raise RuntimeError("private evidence member is not a regular file")
    if (os.name != "nt" and
            (identity.st_uid != 0 or identity.st_gid != 0 or
             stat.S_IMODE(identity.st_mode) != 0o600)):
        raise RuntimeError("private evidence member ownership or mode is unsafe")


def write_private_bytes(target, payload):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(target), flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        validate_private_file_identity(os.fstat(fd))
        with os.fdopen(fd, "wb") as output:
            fd = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if fd is not None:
            os.close(fd)
    fsync_directory(target.parent)


def read_private_bytes(target):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(target), flags)
    try:
        validate_private_file_identity(os.fstat(fd))
        with os.fdopen(fd, "rb") as source:
            fd = None
            return source.read()
    finally:
        if fd is not None:
            os.close(fd)


def mkdir_private(path, exist_ok=False):
    existed = path_lexists(path)
    if existed and (not exist_ok or path_is_symlink(path)):
        raise RuntimeError("private evidence directory already exists or is a symlink")
    if not existed:
        path.mkdir(mode=0o700)
        fsync_directory(path.parent)
    if os.name != "nt":
        validate_source_directory_identity(os.lstat(str(path)), require_private=True)


def fsync_private_tree(root):
    if os.name == "nt":
        return
    members = sorted(root.rglob("*"),
                     key=lambda path: len(path.parts), reverse=True)
    for path in members:
        if path.is_symlink():
            continue
        if path.is_file():
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(str(path), flags)
            try:
                validate_private_file_identity(os.fstat(fd))
                os.fsync(fd)
            finally:
                os.close(fd)
        elif path.is_dir():
            fsync_directory(path)
        else:
            raise RuntimeError("private evidence tree contains an unsupported member")
    fsync_directory(root)
    fsync_directory(root.parent)


def validate_source_directory_identity(identity, require_private=False):
    if (not stat.S_ISDIR(identity.st_mode) or identity.st_uid != 0 or
            identity.st_gid != 0 or identity.st_mode & 0o022):
        raise RuntimeError("US source evidence directory ownership or mode is unsafe")
    if require_private and stat.S_IMODE(identity.st_mode) != 0o700:
        raise RuntimeError("US source private evidence directory is not mode 0700")


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
        for path in paths[1:]:
            existed = path_lexists(path)
            path.mkdir(exist_ok=True, mode=0o700)
            if not existed:
                fsync_directory(path.parent)
        # Recheck every component after mkdir so a concurrent symlink swap
        # cannot redirect evidence away from /data between the first guard and
        # the write.
        for path in paths:
            if not path_lexists(path) or path_is_symlink(path):
                raise RuntimeError("US source evidence path changed or contains a symlink")
    if os.name != "nt":
        for index, path in enumerate(paths):
            if not path_lexists(path):
                continue
            identity = os.lstat(str(path))
            validate_source_directory_identity(identity, require_private=path == BASE)
    probe = BASE
    while not path_lexists(probe):
        probe = probe.parent
    target = run(["findmnt", "-n", "-o", "TARGET", "-T", str(probe)]).stdout.strip()
    if target != str(DATA_ROOT):
        raise RuntimeError("US source evidence path is not on the /data mount")
    if create and run(["findmnt", "-n", "-o", "TARGET", "-T", str(BASE)]).stdout.strip() != str(DATA_ROOT):
        raise RuntimeError("US source evidence directory escaped the /data mount")


def acquire_source_lock():
    if os.name == "nt":
        return contextlib.ExitStack()
    import fcntl
    target = BASE / ".source-fence.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(target), flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        identity = os.fstat(fd)
        if (not stat.S_ISREG(identity.st_mode) or identity.st_uid != 0 or
                identity.st_gid != 0 or stat.S_IMODE(identity.st_mode) != 0o600):
            raise RuntimeError("source fence lock identity is unsafe")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            raise RuntimeError("another source fence operation holds the lock")
        os.fsync(fd)
        fsync_directory(BASE)
        return os.fdopen(fd, "r+")
    except Exception:
        os.close(fd)
        raise


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


def symlink_identity_record(path):
    if not path_lexists(path) or not path_is_symlink(path):
        raise RuntimeError("systemd link is missing or not a symlink")
    before = os.lstat(str(path))
    target = os.readlink(str(path))
    target_path = os.path.normpath(target if os.path.isabs(target)
                                   else os.path.join(str(path.parent), target))
    resolved = os.path.realpath(str(path))
    after = os.lstat(str(path))
    before_identity = (before.st_dev, before.st_ino, before.st_mode, before.st_size,
                       before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_mode, after.st_size,
                      after.st_mtime_ns)
    if (before_identity != after_identity or not path_lexists(path) or
            not path_is_symlink(path) or os.readlink(str(path)) != target or
            os.path.realpath(str(path)) != resolved):
        raise RuntimeError("systemd link changed while it was inspected")
    return {"path": str(path), "kind": "dangling-symlink", "link_target": target,
            "link_target_sha256": hashlib.sha256(target.encode("utf-8")).hexdigest(),
            "target_path": target_path, "resolved_path": resolved,
            "lstat": {"device": before.st_dev, "inode": before.st_ino,
                      "mode": before.st_mode, "size": before.st_size,
                      "mtime_ns": before.st_mtime_ns}}


def parent_path_component_record(path):
    if not path_lexists(path):
        return {"path": str(path), "kind": "absent"}
    if path_is_symlink(path):
        return {"path": str(path), "kind": "symlink",
                "symlink": symlink_identity_record(path)}
    before = os.lstat(str(path))
    if stat.S_ISDIR(before.st_mode):
        kind = "directory"
    elif stat.S_ISREG(before.st_mode):
        kind = "regular"
    else:
        raise RuntimeError("systemd alias parent contains a special filesystem member")
    after = os.lstat(str(path))
    before_identity = (before.st_dev, before.st_ino, before.st_mode,
                       before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_mode,
                      after.st_size, after.st_mtime_ns)
    if (before_identity != after_identity or path_is_symlink(path) or
            not path_lexists(path)):
        raise RuntimeError("systemd alias parent changed while it was inspected")
    return {"path": str(path), "kind": kind}


def target_parent_resolution_once(target_path):
    target = pathlib.Path(target_path)
    parent = target.parent
    if not target.is_absolute() or not parent.is_absolute():
        raise RuntimeError("systemd alias target parent is not absolute")
    anchor = pathlib.Path(parent.anchor)
    pending = list(parent.parts[1:])
    resolved = anchor
    directory_symlinks = []
    seen = set()
    hops = 0
    while pending:
        component = pending.pop(0)
        candidate = resolved / component
        record = parent_path_component_record(candidate)
        if record["kind"] in ("absent", "regular"):
            return {"schema_version": 1, "kind": "broken",
                    "lexical_parent": str(parent), "broken_at": str(candidate),
                    "directory_symlinks": directory_symlinks}
        if record["kind"] == "directory":
            resolved = candidate
            continue
        link = record["symlink"]
        key = os.path.normcase(os.path.normpath(link["path"]))
        if key in seen:
            raise RuntimeError("systemd alias parent chain contains a cycle")
        seen.add(key)
        hops += 1
        if hops > MAX_SYSTEMD_SYMLINK_HOPS:
            raise RuntimeError("systemd alias parent chain exceeds the hop limit")
        directory_symlinks.append(link)
        destination = pathlib.Path(link["target_path"])
        if not destination.is_absolute():
            raise RuntimeError("systemd alias parent symlink target is not absolute")
        resolved = pathlib.Path(destination.anchor)
        pending = list(destination.parts[1:]) + pending
    return {"schema_version": 1, "kind": "resolved",
            "lexical_parent": str(parent), "canonical_parent": str(resolved),
            "canonical_leaf_path": str(resolved / target.name),
            "directory_symlinks": directory_symlinks}


def stable_target_parent_resolution(target_path):
    before = target_parent_resolution_once(target_path)
    after = target_parent_resolution_once(target_path)
    if after != before:
        raise RuntimeError("systemd alias target parent changed while it was inspected")
    return before


def stable_systemd_directory_entries(path):
    if not path_lexists(path) or path_is_symlink(path) or not path.is_dir():
        raise RuntimeError("systemd unit path member is not a real directory")
    before = os.lstat(str(path))
    if os.name != "nt":
        validate_source_directory_identity(before, require_private=False)
    try:
        with os.scandir(str(path)) as scan:
            names = sorted(entry.name for entry in scan)
    except OSError:
        raise RuntimeError("systemd unit path enumeration failed")
    after = os.lstat(str(path))
    before_identity = (before.st_dev, before.st_ino, before.st_mode,
                       before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_mode,
                      after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        raise RuntimeError("systemd unit path directory changed while it was enumerated")
    return [path / name for name in names]


def systemd_candidate_link_records(unit_path_snapshot):
    result = []
    for root_record in unit_path_snapshot["roots"]:
        if root_record.get("kind") == "absent":
            continue
        root = pathlib.Path(root_record["path"])
        if systemd_unit_root_record(root_record["path"]) != root_record:
            raise RuntimeError("systemd unit path root identity changed")
        for entry in stable_systemd_directory_entries(root):
            if path_is_symlink(entry):
                if entry.name.endswith((".wants", ".requires")):
                    raise RuntimeError("systemd dependency directory is a symlink")
                if entry != DRAMA_LOCAL_FRAGMENT:
                    result.append(symlink_identity_record(entry))
                continue
            if entry.name.endswith((".wants", ".requires")) and path_lexists(entry):
                if not entry.is_dir():
                    raise RuntimeError("systemd dependency entry root is not a directory")
                for candidate in stable_systemd_directory_entries(entry):
                    if path_is_symlink(candidate):
                        result.append(symlink_identity_record(candidate))
    result.sort(key=lambda row: row["path"])
    if len({row["path"] for row in result}) != len(result):
        raise RuntimeError("duplicate systemd alias or dependency link evidence")
    return result


def resolve_systemd_symlink_chain(first_record, fragment, roots):
    entry = pathlib.Path(first_record["path"])
    entry_in_unit_path = False
    for root in roots:
        try:
            entry.relative_to(root)
            entry_in_unit_path = True
            break
        except ValueError:
            continue
    if not entry_in_unit_path:
        raise RuntimeError("systemd alias entry is outside the validated unit paths")
    current_record = first_record
    hops = []
    seen = set()
    for _unused in range(MAX_SYSTEMD_SYMLINK_HOPS):
        current = pathlib.Path(current_record["path"])
        current_key = os.path.normcase(os.path.normpath(str(current)))
        if current_key in seen:
            raise RuntimeError("systemd alias chain contains a cycle")
        seen.add(current_key)
        parent_resolution = stable_target_parent_resolution(
            current_record["target_path"])
        if parent_resolution["kind"] == "broken":
            return None
        enriched_record = dict(current_record)
        enriched_record["target_parent_resolution"] = parent_resolution
        hops.append(enriched_record)
        target = pathlib.Path(parent_resolution["canonical_leaf_path"])
        if target == fragment:
            for hop in hops:
                recorded_link = {name: hop[name] for name in SYMLINK_IDENTITY_FIELDS}
                if symlink_identity_record(pathlib.Path(hop["path"])) != recorded_link:
                    raise RuntimeError("systemd alias chain changed while it was inspected")
                if (stable_target_parent_resolution(hop["target_path"]) !=
                        hop["target_parent_resolution"]):
                    raise RuntimeError("systemd alias parent chain changed while it was inspected")
            result = dict(hops[0])
            result.update({"chain_schema_version": 1, "chain": hops,
                           "terminal_path": str(fragment)})
            return result
        if str(target) == "/dev/null":
            return None
        if not path_lexists(target):
            return None
        if not path_is_symlink(target):
            if not target.is_file():
                raise RuntimeError("systemd alias chain target is not a regular unit file")
            return None
        current_record = symlink_identity_record(target)
    raise RuntimeError("systemd alias chain exceeds the hop limit")


def drama_fragment_symlink_records(fragment, unit_path_snapshot):
    roots = [pathlib.Path(row["path"]) for row in unit_path_snapshot["roots"]
             if row.get("kind") == "directory"]
    candidates = systemd_candidate_link_records(unit_path_snapshot)
    result = []
    for candidate in candidates:
        resolved = resolve_systemd_symlink_chain(candidate, fragment, roots)
        if resolved is not None:
            result.append(resolved)
    if systemd_candidate_link_records(unit_path_snapshot) != candidates:
        raise RuntimeError("systemd alias set changed while it was inspected")
    result.sort(key=lambda row: row["path"])
    return result


def drama_wants_link_record(fragment, unit_path_snapshot, allow_absent=False):
    records = drama_fragment_symlink_records(fragment, unit_path_snapshot)
    if not path_lexists(DRAMA_WANTS_LINK):
        if allow_absent and records == []:
            return {"path": str(DRAMA_WANTS_LINK), "kind": "absent"}
        raise RuntimeError("expected dangling drama wants link is missing")
    if not path_is_symlink(DRAMA_WANTS_LINK):
        raise RuntimeError("drama wants entry is not a symlink")
    if len(records) != 1 or records[0]["path"] != str(DRAMA_WANTS_LINK):
        raise RuntimeError("drama enablement or alias link scope changed")
    record = records[0]
    expected_parent = {"schema_version": 1, "kind": "resolved",
                       "lexical_parent": str(fragment.parent),
                       "canonical_parent": str(fragment.parent),
                       "canonical_leaf_path": str(fragment),
                       "directory_symlinks": []}
    if (record["link_target"] != str(fragment) or record["target_path"] != str(fragment) or
            record.get("target_parent_resolution") != expected_parent or
            len(record.get("chain", [])) != 1 or
            path_lexists(fragment)):
        raise RuntimeError("drama wants link is not the expected absolute dangling fragment link")
    return record


def current_systemd_unit_paths(expected):
    current = systemd_unit_path_snapshot()
    validate_recorded_systemd_unit_paths(expected)
    validate_recorded_systemd_unit_paths(current)
    if (current["manager"] != expected["manager"] or
            current["analyzed"] != expected["analyzed"]):
        raise RuntimeError("systemd manager unit paths changed from the initial snapshot")
    current_roots = {row["path"]: row for row in current["roots"]}
    expected_roots = {row["path"]: row for row in expected["roots"]}
    if set(current_roots) != set(expected_roots):
        raise RuntimeError("systemd unit path roots changed from the initial snapshot")
    for path, expected_root in expected_roots.items():
        if path not in SYSTEMD_VOLATILE_UNIT_PATHS and current_roots[path] != expected_root:
            raise RuntimeError("static systemd unit path identity changed from the initial snapshot")
    return current


def remove_loaded_absent_enablement(expected_unit_paths):
    unit_paths = current_systemd_unit_paths(expected_unit_paths)
    before = drama_wants_link_record(DRAMA_LOCAL_FRAGMENT, unit_paths,
                                     allow_absent=True)
    if before.get("kind") == "absent":
        fsync_directory(DRAMA_WANTS_LINK.parent)
        unit_paths = current_systemd_unit_paths(expected_unit_paths)
        if drama_wants_link_record(DRAMA_LOCAL_FRAGMENT, unit_paths,
                                   allow_absent=True).get("kind") != "absent":
            raise RuntimeError("drama enablement link changed after directory sync")
        return {"attempted": False, "rc": None, "postcondition": "absent"}
    result = run(["systemctl", "disable", "--no-reload", DRAMA_UNIT], check=False)
    unit_paths = current_systemd_unit_paths(expected_unit_paths)
    after = drama_wants_link_record(DRAMA_LOCAL_FRAGMENT, unit_paths,
                                    allow_absent=True)
    if after.get("kind") != "absent":
        raise RuntimeError("drama enablement link remains after disable")
    fsync_directory(DRAMA_WANTS_LINK.parent)
    unit_paths = current_systemd_unit_paths(expected_unit_paths)
    if drama_wants_link_record(DRAMA_LOCAL_FRAGMENT, unit_paths,
                               allow_absent=True).get("kind") != "absent":
        raise RuntimeError("drama enablement link changed after directory sync")
    return {"attempted": True, "rc": int(result.returncode), "postcondition": "absent"}


def loaded_absent_fragment_record(unit, raw, dropins, state):
    path = pathlib.Path(raw)
    if (unit != DRAMA_UNIT or path != DRAMA_LOCAL_FRAGMENT or not path.is_absolute() or
            dropins or path_lexists(path) or path_lexists(DRAMA_DROPIN_DIR)):
        raise RuntimeError("unsupported absent unit fragment topology")
    if state is None:
        raise RuntimeError("absent drama fragment requires a bound process snapshot")
    manager_pid = int(prop(unit, "MainPID") or 0)
    manager_control_group = prop(unit, "ControlGroup")
    loaded = {
        "load_state": prop(unit, "LoadState"),
        "id": prop(unit, "Id"),
        "names": prop(unit, "Names").split(),
        "fragment_path": prop(unit, "FragmentPath"),
        "drop_in_paths": prop(unit, "DropInPaths").split(),
        "active_state": prop(unit, "ActiveState"),
        "sub_state": prop(unit, "SubState"),
        "unit_file_state": prop(unit, "UnitFileState"),
        "main_pid": manager_pid,
        "control_pid": int(prop(unit, "ControlPID") or 0),
        "control_group": manager_control_group,
        "exec_main_start_monotonic": prop(unit, "ExecMainStartTimestampMonotonic"),
        "active_enter_monotonic": prop(unit, "ActiveEnterTimestampMonotonic"),
        "nrestarts": int(prop(unit, "NRestarts") or 0),
        "pid_start_ticks": proc_start_ticks(manager_pid) if manager_pid else 0,
        "cgroup_pids": (systemd_cgroup_pids(manager_control_group)
                         if manager_control_group else []),
        "threads": state.get("threads"),
        "children": state.get("children"),
    }
    if (loaded["load_state"] != "loaded" or loaded["id"] != unit or
            loaded["names"] != [unit] or
            loaded["fragment_path"] != raw or
            loaded["drop_in_paths"] != [] or loaded["active_state"] != "active" or
            loaded["sub_state"] != "running" or loaded["unit_file_state"] != "enabled" or
            loaded["main_pid"] <= 0 or loaded["control_pid"] != 0 or
            loaded["control_group"] != "/system.slice/" + unit or
            loaded["pid_start_ticks"] is None or loaded["pid_start_ticks"] <= 0 or
            loaded["cgroup_pids"] != [loaded["main_pid"]] or
            loaded["threads"] != 1 or loaded["children"] != []):
        raise RuntimeError("absent drama fragment is not a loaded active unit with exact identity")
    try:
        started = int(loaded["exec_main_start_monotonic"])
        active_enter = int(loaded["active_enter_monotonic"])
    except (TypeError, ValueError):
        started = active_enter = 0
    if started <= 0 or active_enter <= 0:
        raise RuntimeError("absent drama fragment process start identity is incomplete")
    if (state.get("unit") != unit or state.get("fragment") != raw or
            state.get("active") != loaded["active_state"] or
            state.get("substate") != loaded["sub_state"] or
            state.get("enabled") != loaded["unit_file_state"] or
            state.get("pid") != loaded["main_pid"] or
            state.get("control_pid") != loaded["control_pid"] or
            state.get("control_group") != loaded["control_group"] or
            state.get("pid_start_ticks") != loaded["pid_start_ticks"] or
            state.get("cgroup_pids") != loaded["cgroup_pids"] or
            state.get("nrestarts") != loaded["nrestarts"] or
            state.get("start_monotonic") != loaded["exec_main_start_monotonic"] or
            state.get("active_enter_monotonic") != loaded["active_enter_monotonic"] or
            state.get("load_state") != loaded["load_state"] or state.get("id") != loaded["id"] or
            state.get("names") != loaded["names"]):
        raise RuntimeError("absent drama fragment process snapshot changed")
    unit_paths = systemd_unit_path_snapshot()
    wants = drama_wants_link_record(path, unit_paths)
    if path_lexists(path):
        raise RuntimeError("absent drama fragment appeared while it was inspected")
    return {"schema_version": 1, "path": raw, "kind": "loaded_fragment_absent",
            "content_archived": False, "restorable": False, "can_retire_local": False,
            "can_mask_if_still_absent": True, "loaded_unit": loaded,
            "systemd_unit_paths": unit_paths, "enablement_links": [wants]}


def unit_definition_snapshot(unit, state=None):
    fragment = prop(unit, "FragmentPath")
    dropins = prop(unit, "DropInPaths").split()
    if not fragment or len(dropins) != len(set(dropins)):
        raise RuntimeError("unit definition paths are missing or duplicated: " + unit)
    if path_lexists(pathlib.Path(fragment)):
        fragment_record = definition_path_record(fragment)
    else:
        fragment_record = loaded_absent_fragment_record(unit, fragment, dropins, state)
    definition = {"fragment": fragment_record,
                  "dropins": [definition_path_record(path) for path in dropins]}
    if fragment_record.get("kind") == "loaded_fragment_absent":
        definition.update({"schema_version": 1,
                           "definition_mode": "loaded_fragment_absent", "unit": unit,
                           "restorable": False})
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode("utf-8")
    definition["definition_sha256"] = hashlib.sha256(canonical).hexdigest()
    return definition


def is_loaded_absent_definition(definition):
    return (definition.get("definition_mode") == "loaded_fragment_absent" and
            definition.get("fragment", {}).get("kind") == "loaded_fragment_absent")


def validate_recorded_definition_current(definition, state, resume=False,
                                         require_enablement_removed=False):
    if not is_loaded_absent_definition(definition):
        if unit_definition_snapshot(state["unit"], state=state) != definition:
            raise RuntimeError("unit definition changed from its recorded identity")
        return
    fragment = definition["fragment"]
    loaded = fragment["loaded_unit"]
    if (fragment.get("path") != str(DRAMA_LOCAL_FRAGMENT) or
            path_lexists(DRAMA_LOCAL_FRAGMENT) or path_lexists(DRAMA_DROPIN_DIR)):
        raise RuntimeError("recorded absent drama fragment or drop-in appeared")
    if state.get("pid"):
        if unit_definition_snapshot(DRAMA_UNIT, state=state) != definition:
            raise RuntimeError("loaded absent drama unit identity changed")
        return
    if (state.get("unit") != DRAMA_UNIT or state.get("active") != "inactive" or
            state.get("substate") not in ("dead", "failed") or state.get("pid") != 0 or
            state.get("control_pid") != 0 or state.get("control_group")):
        raise RuntimeError("stopped absent drama unit closure changed")
    if state.get("load_state") == "not-found":
        if not resume or state.get("fragment") or state.get("enabled") == "masked":
            raise RuntimeError("unexpected not-found drama resume state")
    else:
        if (state.get("load_state") != loaded["load_state"] or state.get("id") != loaded["id"] or
                state.get("names") != loaded["names"] or
                state.get("fragment") != loaded["fragment_path"] or
                state.get("start_monotonic") != loaded["exec_main_start_monotonic"] or
                state.get("active_enter_monotonic") != loaded["active_enter_monotonic"] or
                state.get("nrestarts") != loaded["nrestarts"] or
                prop(DRAMA_UNIT, "DropInPaths").split() != []):
            raise RuntimeError("stopped absent drama unit manager identity changed")
    unit_paths = current_systemd_unit_paths(fragment["systemd_unit_paths"])
    wants = drama_wants_link_record(DRAMA_LOCAL_FRAGMENT, unit_paths,
                                    allow_absent=True)
    original_wants = fragment["enablement_links"][0]
    if require_enablement_removed:
        if wants.get("kind") != "absent":
            raise RuntimeError("drama enablement link remains after disable")
    elif wants != original_wants and wants.get("kind") != "absent":
        raise RuntimeError("drama enablement link identity changed")
    if path_lexists(DRAMA_LOCAL_FRAGMENT) or path_lexists(DRAMA_DROPIN_DIR):
        raise RuntimeError("recorded absent drama definition changed while it was verified")


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
        if unit == DRAMA_UNIT:
            raw_stat = pathlib.Path("/proc/%s/stat" % pid).read_text()
            result["pid_start_ticks"] = int(raw_stat[raw_stat.rfind(")") + 2:].split()[19])
    if unit == DRAMA_UNIT:
        result["cgroup_pids"] = (systemd_cgroup_pids(result["control_group"])
                                  if result["control_group"] else [])
        result["nrestarts"] = int(prop(unit, "NRestarts") or 0)
        result["start_monotonic"] = prop(unit, "ExecMainStartTimestampMonotonic")
        result["active_enter_monotonic"] = prop(unit, "ActiveEnterTimestampMonotonic")
        result["load_state"] = prop(unit, "LoadState")
        result["id"] = prop(unit, "Id")
        result["names"] = prop(unit, "Names").split()
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


def proc_start_ticks(pid):
    raw_stat = pathlib.Path("/proc/%s/stat" % pid).read_text()
    return int(raw_stat[raw_stat.rfind(")") + 2:].split()[19])


def split_service_identity(unit):
    """Return non-secret process/unit identity for the temporary US ad lane."""
    state = inspect(unit)
    pid = state["pid"]
    control_group = state.get("control_group", "")
    return {
        "unit": unit,
        "active": state["active"],
        "substate": state["substate"],
        "enabled": state["enabled"],
        "pid": pid,
        "pid_start_ticks": proc_start_ticks(pid) if pid else 0,
        "control_pid": state["control_pid"],
        "control_group": control_group,
        "cgroup_pids": systemd_cgroup_pids(control_group) if control_group else [],
        "nrestarts": int(prop(unit, "NRestarts") or 0),
        "start_monotonic": prop(unit, "ExecMainStartTimestampMonotonic"),
        "active_enter_monotonic": prop(unit, "ActiveEnterTimestampMonotonic"),
        "unit_sha256": unit_definition_sha256(unit),
    }


def split_ad_baseline():
    baseline = {
        "services": [split_service_identity(unit) for unit in MATERIAL_AD_UNITS[:2]],
        "tunnel": split_service_identity(AD_ONLY_TUNNEL),
    }
    for state in baseline["services"] + [baseline["tunnel"]]:
        if (state["active"] != "active" or state["substate"] != "running" or
                state["pid"] <= 0 or state["control_pid"] != 0 or
                state["pid_start_ticks"] <= 0 or state["pid"] not in state["cgroup_pids"] or
                not str(state["start_monotonic"]).isdigit() or
                int(state["start_monotonic"]) <= 0 or
                not str(state["active_enter_monotonic"]).isdigit() or
                int(state["active_enter_monotonic"]) <= 0 or
                not re.fullmatch(r"[0-9a-f]{64}", state["unit_sha256"] or "")):
            raise RuntimeError("temporary US ad lane identity is not stable: " + state["unit"])
    tunnel = baseline["tunnel"]
    if tunnel["enabled"] != "enabled" or tunnel["cgroup_pids"] != [tunnel["pid"]]:
        raise RuntimeError("temporary US ad-only tunnel is not singly owned and enabled")
    return baseline


def validate_materials_split_checkpoint(proof, group, states, ad_baseline=None):
    """Validate the coordinator-owned split handoff without reading credentials."""
    if proof.get("coordinator_host") != "VM-0-108-centos" or proof.get("ready") is not True:
        raise RuntimeError("materials split checkpoint is not a ready CPU coordinator snapshot")
    if proof.get("ad_requests_drained") is not True:
        raise RuntimeError("materials split checkpoint has not drained ad requests")
    if group == "materials-images":
        if (proof.get("split_mode") != "us-ad-only" or
                proof.get("legacy_shared_tunnel_stopped") is not True or
                proof.get("legacy_burst_tunnel_stopped") is not True or
                proof.get("cpu_image_ports_owned_by_local_units") is not True or
                proof.get("cpu_ad_ports_owned_by_us_ad_only_tunnel") is not True or
                proof.get("ad_services_healthy") is not True or
                proof.get("us_ad_baseline") != ad_baseline):
            raise RuntimeError("materials-images tunnel or ad-lane proof changed")
        tunnel_states = {state["unit"]: state for state in states
                         if state["unit"] in LEGACY_MATERIAL_TUNNELS}
        if set(tunnel_states) != LEGACY_MATERIAL_TUNNELS or any(
                state["pid"] != 0 or state["control_pid"] != 0 or
                state["active"] not in ("inactive", "failed")
                for state in tunnel_states.values()):
            raise RuntimeError("legacy material tunnels are not stopped before image fencing")
    elif group == "materials-ad":
        if (proof.get("split_mode") != "hk-ad" or
                proof.get("ad_only_tunnel_stopped") is not True or
                proof.get("cpu_ad_ports_owned_by_hk_tunnel") is not True or
                proof.get("hk_ad_target_ready") is not True):
            raise RuntimeError("materials-ad target or tunnel proof changed")
        tunnel = next((state for state in states if state["unit"] == AD_ONLY_TUNNEL), None)
        if (not tunnel or tunnel["pid"] != 0 or tunnel["control_pid"] != 0 or
                tunnel["active"] not in ("inactive", "failed")):
            raise RuntimeError("US ad-only tunnel is not stopped before ad fencing")
    else:
        raise RuntimeError("unsupported split materials group")


def systemd_cgroup_pids(control_group):
    target = pathlib.Path("/sys/fs/cgroup/systemd") / control_group.lstrip("/") / "cgroup.procs"
    return sorted(int(value) for value in target.read_text().split())


def shared_tunnel_snapshot():
    pid = int(prop(DRAMA_SHARED_TUNNEL, "MainPID") or 0)
    control_group = prop(DRAMA_SHARED_TUNNEL, "ControlGroup")
    return {
        "unit": DRAMA_SHARED_TUNNEL,
        "active": prop(DRAMA_SHARED_TUNNEL, "ActiveState"),
        "substate": prop(DRAMA_SHARED_TUNNEL, "SubState"),
        "enabled": prop(DRAMA_SHARED_TUNNEL, "UnitFileState"),
        "pid": pid,
        "pid_start_ticks": proc_start_ticks(pid) if pid else 0,
        "control_pid": int(prop(DRAMA_SHARED_TUNNEL, "ControlPID") or 0),
        "control_group": control_group,
        "cgroup_pids": systemd_cgroup_pids(control_group) if control_group else [],
        "nrestarts": int(prop(DRAMA_SHARED_TUNNEL, "NRestarts") or 0),
        "start_monotonic": prop(DRAMA_SHARED_TUNNEL, "ExecMainStartTimestampMonotonic"),
        "active_enter_monotonic": prop(DRAMA_SHARED_TUNNEL, "ActiveEnterTimestampMonotonic"),
        "unit_sha256": unit_definition_sha256(DRAMA_SHARED_TUNNEL),
    }


def validate_shared_tunnel_baseline(state):
    if state["active"] != "active" or state["substate"] != "running" or state["pid"] <= 0:
        raise RuntimeError("shared drama tunnel is not stably active")
    if (state["control_pid"] != 0 or not state["control_group"] or
            state.get("pid_start_ticks", 0) <= 0 or state.get("cgroup_pids") != [state["pid"]]):
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


def original_process_identity_is_gone(pid, start_ticks):
    proc_root = pathlib.Path("/proc/%s" % pid)
    if not path_lexists(proc_root):
        return True
    try:
        current_start_ticks = proc_start_ticks(pid)
    except Exception:
        if not path_lexists(proc_root):
            return True
        raise RuntimeError("original drama process identity cannot be verified")
    return current_start_ticks != start_ticks


def capture_drama_final_observation(definition=None):
    original_process_gone = None
    unit_paths = None
    fragment_links = []
    frozen_entry_paths_present = []
    if definition is not None and is_loaded_absent_definition(definition):
        fragment = definition["fragment"]
        loaded = fragment["loaded_unit"]
        original_process_gone = original_process_identity_is_gone(
            loaded["main_pid"], loaded["pid_start_ticks"])
        unit_paths = current_systemd_unit_paths(fragment["systemd_unit_paths"])
        frozen_entry_paths_present = [
            row["path"] for row in fragment["enablement_links"]
            if path_lexists(pathlib.Path(row["path"]))]
        fragment_links = drama_fragment_symlink_records(
            DRAMA_LOCAL_FRAGMENT, unit_paths)
    return {
        "source": inspect(DRAMA_UNIT),
        "persistent_mask": is_persistent_mask(DRAMA_LOCAL_FRAGMENT),
        "dropin_present": path_lexists(DRAMA_DROPIN_DIR),
        "wants_present": path_lexists(DRAMA_WANTS_LINK),
        "systemd_unit_paths": unit_paths,
        "frozen_entry_paths_present": frozen_entry_paths_present,
        "fragment_links": fragment_links,
        "original_process_gone": original_process_gone,
        "port_8787_listener_rows": port_rows(DRAMA_PORT, listening=True),
        "port_8787_established_rows": port_rows(DRAMA_PORT),
        "shared_tunnel": shared_tunnel_snapshot(),
    }


def validate_drama_final_observation(observation, shared_before,
                                     definition=None, original_state=None):
    state = observation["source"]
    if (state["active"] != "inactive" or state["substate"] != "dead" or
            state["enabled"] != "masked" or state.get("load_state") != "masked" or state["pid"] != 0 or
            state["control_pid"] != 0 or state["control_group"] or
            state.get("cgroup_pids") != []):
        raise RuntimeError("drama source final fence verification failed")
    if observation["persistent_mask"] is not True:
        raise RuntimeError("drama source persistent mask verification failed")
    if definition is not None and is_loaded_absent_definition(definition):
        loaded = definition["fragment"]["loaded_unit"]
        if (original_state is None or original_state.get("pid") != loaded["main_pid"] or
                original_state.get("pid_start_ticks") != loaded["pid_start_ticks"] or
                original_state.get("control_group") != loaded["control_group"] or
                observation["dropin_present"] or observation["wants_present"] or
                observation["frozen_entry_paths_present"] != [] or
                observation["fragment_links"] != [] or
                observation["original_process_gone"] is not True):
            raise RuntimeError("drama loaded-absent final topology verification failed")
    if (observation["port_8787_listener_rows"] or
            observation["port_8787_established_rows"]):
        raise RuntimeError("drama source port remains owned after fencing")
    shared_after = observation["shared_tunnel"]
    if shared_after != shared_before:
        raise RuntimeError("shared drama tunnel changed during source fencing")
    if definition is not None and is_loaded_absent_definition(definition):
        state = dict(state)
        state["verified_systemd_unit_paths"] = observation["systemd_unit_paths"]
    return state, shared_after


def validate_drama_fenced(shared_before, definition=None, original_state=None,
                          observation=None):
    if observation is None:
        observation = capture_drama_final_observation(definition=definition)
    return validate_drama_final_observation(
        observation, shared_before, definition=definition,
        original_state=original_state)


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
        if is_loaded_absent_definition(definition):
            live_before = inspect(state["unit"])
            validate_recorded_definition_current(definition, live_before, resume=resume)
        elif unit_definition_snapshot(state["unit"]) != definition:
            raise RuntimeError("unit definition changed before backup: " + state["unit"])
        manifest = unit_backup / "definition.json"
        if path_lexists(manifest):
            if path_is_symlink(manifest):
                raise RuntimeError("archived unit definition manifest is a symlink")
            if read_private_json(manifest) != definition:
                raise RuntimeError("archived unit definition manifest changed: " + state["unit"])
            fsync_directory(manifest.parent)
            if read_private_json(manifest) != definition:
                raise RuntimeError("archived unit definition manifest changed during sync")
        else:
            write_private_json(manifest, definition)
        members = [("original.service", definition["fragment"])]
        members.extend(("dropins/%03d.service" % index, row)
                       for index, row in enumerate(definition["dropins"]))
        for relative, row in members:
            target = unit_backup / relative
            mkdir_private(target.parent, exist_ok=True)
            if row.get("kind") == "loaded_fragment_absent":
                if path_lexists(target):
                    raise RuntimeError("absent unit definition has unexpected archived content")
                continue
            if path_lexists(target):
                content = read_private_bytes(target)
                fsync_directory(target.parent)
                if read_private_bytes(target) != content:
                    raise RuntimeError("unit definition backup changed during directory sync")
            else:
                source = pathlib.Path(row["path"])
                if definition_path_record(row["path"]) != row:
                    raise RuntimeError("unit definition source changed during backup")
                if not source.is_file():
                    raise RuntimeError("unit definition source disappeared during backup")
                content = source.read_bytes()
                write_private_bytes(target, content)
                if definition_path_record(row["path"]) != row:
                    raise RuntimeError("unit definition source changed during backup")
            if hashlib.sha256(content).hexdigest() != row["content_sha256"]:
                raise RuntimeError("unit definition backup content mismatch: " + state["unit"])
        if is_loaded_absent_definition(definition):
            live_after = inspect(state["unit"])
            validate_recorded_definition_current(definition, live_after, resume=resume)
        elif unit_definition_snapshot(state["unit"]) != definition:
            raise RuntimeError("unit definition changed during backup: " + state["unit"])
        return
    fragment = pathlib.Path(state["fragment"]) if state["fragment"] else None
    if fragment and fragment.is_file():
        original = unit_backup / "original.service"
        fragment_bytes = fragment.read_bytes()
        if path_lexists(original):
            if read_private_bytes(original) != fragment_bytes:
                raise RuntimeError("original unit changed after partial fence: " + state["unit"])
        else:
            write_private_bytes(original, fragment_bytes)
    dropins = pathlib.Path("/etc/systemd/system") / (state["unit"] + ".d")
    archived = unit_backup / "dropins"
    if dropins.is_dir():
        if path_lexists(archived):
            if path_is_symlink(archived) or not archived.is_dir():
                raise RuntimeError("unit drop-in backup root is unsafe")
            fsync_private_tree(archived)
            if directory_manifest(archived) != directory_manifest(dropins):
                raise RuntimeError("unit drop-ins changed after partial fence: " + state["unit"])
        else:
            shutil.copytree(str(dropins), str(archived), symlinks=True)
            os.chmod(str(archived), 0o700)
            for path in archived.rglob("*"):
                if path.is_symlink():
                    continue
                os.chmod(str(path), 0o700 if path.is_dir() else 0o600)
            fsync_private_tree(archived)
            if directory_manifest(archived) != directory_manifest(dropins):
                raise RuntimeError("unit drop-in backup verification failed: " + state["unit"])
    elif path_lexists(archived) and resume:
        raise RuntimeError("unit drop-ins disappeared after partial fence: " + state["unit"])


def write_private_json(target, payload):
    write_private_bytes(target, json.dumps(payload, indent=2).encode("utf-8"))


def read_private_json(target):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(target), flags)
    try:
        identity = os.fstat(fd)
        validate_private_file_identity(identity)
        with os.fdopen(fd, "r") as source:
            fd = None
            return json.load(source)
    finally:
        if fd is not None:
            os.close(fd)


def write_failure_evidence(stage, original_exception_type, command_results,
                           final_state, final_closed, shared_before,
                           shared_after=None,
                           final_verification_error=None):
    source_storage_guard(create=True)
    payload = {"group": "drama", "stage": stage,
               "original_exception_type": original_exception_type,
               "failed_at_epoch": time.time(), "command_results": command_results,
               "source": final_state, "final_closed": final_closed,
               "shared_tunnel_before": shared_before,
               "shared_tunnel_after": shared_after or {}}
    if final_verification_error:
        payload["final_verification_error_type"] = final_verification_error
    payload["port_8787_listener_count"] = final_state.get("port_8787_listener_count")
    payload["port_8787_established_count"] = final_state.get("port_8787_established_count")
    for attempt in range(100):
        target = BASE / ("drama-failure-%d-%d-%02d.json" %
                         (int(time.time()), os.getpid(), attempt))
        if not path_lexists(target):
            write_private_json(target, payload)
            return target
    raise RuntimeError("cannot allocate unique drama failure evidence path")


def write_drama_success_evidence(final_state, shared_before, shared_after, checkpoint_sha256,
                                 transition_results=None):
    payload = {"group": "drama", "completed_at_epoch": time.time(),
               "checkpoint_sha256": checkpoint_sha256,
               "source": final_state, "port_8787_listener_count": 0,
               "port_8787_established_count": 0,
               "shared_tunnel_before": shared_before, "shared_tunnel_after": shared_after,
               "shared_tunnel_unchanged": shared_after == shared_before,
               "transition_results": transition_results or {}}
    target = BASE / "drama-after.json"
    if path_lexists(target):
        if path_is_symlink(target):
            raise RuntimeError("drama success evidence is a symlink")
        existing = read_private_json(target)
        fsync_directory(target.parent)
        if read_private_json(target) != existing:
            raise RuntimeError("drama success evidence changed during directory sync")
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


def validate_recorded_systemd_unit_paths(snapshot):
    if type(snapshot) is not dict or snapshot.get("schema_version") != 1:
        raise RuntimeError("recorded systemd unit path snapshot is malformed")
    manager = snapshot.get("manager")
    analyzed = snapshot.get("analyzed")
    roots = snapshot.get("roots")
    if (type(manager) is not list or type(analyzed) is not list or
            type(roots) is not list or not manager or not analyzed or
            len(manager) != len(set(manager)) or len(analyzed) != len(set(analyzed)) or
            not set(manager).issubset(analyzed) or len(roots) != len(analyzed)):
        raise RuntimeError("recorded systemd unit path lists are malformed")
    for value in manager + analyzed:
        path = pathlib.Path(value)
        native_canonical = path.is_absolute() and os.path.normpath(value) == value
        posix_canonical = value.startswith("/") and posixpath.normpath(value) == value
        if not native_canonical and not posix_canonical:
            raise RuntimeError("recorded systemd unit path is not canonical and absolute")
    for value, record in zip(analyzed, roots):
        if record.get("path") != value or record.get("kind") not in ("absent", "directory"):
            raise RuntimeError("recorded systemd unit path root identity is malformed")
        if record["kind"] == "directory":
            identity = record.get("lstat", {})
            if (set(identity) != {"device", "inode", "mode", "uid", "gid"} or
                    not stat.S_ISDIR(identity.get("mode", 0))):
                raise RuntimeError("recorded systemd unit path directory identity is malformed")


def verified_definition_backup(unit_backup):
    try:
        if not path_lexists(unit_backup) or path_is_symlink(unit_backup):
            raise RuntimeError("unit definition evidence directory is missing or a symlink")
        if os.name != "nt":
            validate_source_directory_identity(
                os.lstat(str(unit_backup)), require_private=True)
        manifest = unit_backup / "definition.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise RuntimeError("unit definition manifest is missing")
        definition = read_private_json(manifest)
        core = {"fragment": definition["fragment"], "dropins": definition["dropins"]}
        if is_loaded_absent_definition(definition):
            core.update({"schema_version": definition.get("schema_version"),
                         "definition_mode": definition.get("definition_mode"),
                         "unit": definition.get("unit"),
                         "restorable": definition.get("restorable")})
        canonical = json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != definition.get("definition_sha256"):
            raise RuntimeError("unit definition manifest hash changed")
        members = [("original.service", definition["fragment"])]
        members.extend(("dropins/%03d.service" % index, row)
                       for index, row in enumerate(definition["dropins"]))
        for relative, row in members:
            archived = unit_backup / relative
            kind = row.get("kind")
            if kind == "loaded_fragment_absent":
                if row is not definition["fragment"]:
                    raise RuntimeError("only the unit fragment may be recorded absent")
                if (definition.get("schema_version") != 1 or
                        definition.get("definition_mode") != "loaded_fragment_absent" or
                        definition.get("unit") != DRAMA_UNIT or definition.get("restorable") is not False or
                        row.get("schema_version") != 1 or row.get("path") != str(DRAMA_LOCAL_FRAGMENT) or
                        row.get("content_archived") is not False or row.get("restorable") is not False or
                        row.get("can_retire_local") is not False or
                        row.get("can_mask_if_still_absent") is not True or
                        definition.get("dropins") != []):
                    raise RuntimeError("absent unit definition evidence is malformed")
                loaded = row.get("loaded_unit", {})
                if (loaded.get("load_state") != "loaded" or loaded.get("id") != DRAMA_UNIT or
                        loaded.get("names") != [DRAMA_UNIT] or
                        loaded.get("fragment_path") != str(DRAMA_LOCAL_FRAGMENT) or
                        loaded.get("drop_in_paths") != [] or loaded.get("active_state") != "active" or
                        loaded.get("sub_state") != "running" or loaded.get("unit_file_state") != "enabled" or
                        type(loaded.get("main_pid")) is not int or loaded.get("main_pid") <= 0 or
                        loaded.get("control_pid") != 0 or
                        loaded.get("control_group") != "/system.slice/" + DRAMA_UNIT or
                        type(loaded.get("nrestarts")) is not int or loaded.get("nrestarts") < 0 or
                        type(loaded.get("pid_start_ticks")) is not int or
                        loaded.get("pid_start_ticks") <= 0 or
                        loaded.get("cgroup_pids") != [loaded.get("main_pid")] or
                        loaded.get("threads") != 1 or
                        loaded.get("children") != []):
                    raise RuntimeError("absent unit loaded identity evidence is malformed")
                for name in ("exec_main_start_monotonic", "active_enter_monotonic"):
                    if not str(loaded.get(name, "")).isdigit() or int(loaded[name]) <= 0:
                        raise RuntimeError("absent unit start identity evidence is malformed")
                validate_recorded_systemd_unit_paths(row.get("systemd_unit_paths"))
                links = row.get("enablement_links")
                if type(links) is not list or len(links) != 1:
                    raise RuntimeError("absent unit enablement evidence is malformed")
                link = links[0]
                expected_parent = {
                    "schema_version": 1, "kind": "resolved",
                    "lexical_parent": str(DRAMA_LOCAL_FRAGMENT.parent),
                    "canonical_parent": str(DRAMA_LOCAL_FRAGMENT.parent),
                    "canonical_leaf_path": str(DRAMA_LOCAL_FRAGMENT),
                    "directory_symlinks": []}
                expected_hop = {name: link[name] for name in SYMLINK_IDENTITY_FIELDS}
                expected_hop["target_parent_resolution"] = expected_parent
                if (link.get("path") != str(DRAMA_WANTS_LINK) or
                        link.get("kind") != "dangling-symlink" or
                        link.get("link_target") != str(DRAMA_LOCAL_FRAGMENT) or
                        link.get("target_path") != str(DRAMA_LOCAL_FRAGMENT) or
                        link.get("resolved_path") != str(DRAMA_LOCAL_FRAGMENT) or
                        link.get("terminal_path") != str(DRAMA_LOCAL_FRAGMENT) or
                        link.get("chain_schema_version") != 1 or
                        link.get("target_parent_resolution") != expected_parent or
                        link.get("chain") != [expected_hop] or
                        link.get("link_target_sha256") != hashlib.sha256(
                            str(DRAMA_LOCAL_FRAGMENT).encode("utf-8")).hexdigest() or
                        set(link.get("lstat", {})) != {"device", "inode", "mode", "size", "mtime_ns"} or
                        not stat.S_ISLNK(link.get("lstat", {}).get("mode", 0))):
                    raise RuntimeError("absent unit enablement link evidence is malformed")
                if path_lexists(archived):
                    raise RuntimeError("absent unit definition has unexpected archived content")
                continue
            if kind not in ("file", "symlink"):
                raise RuntimeError("unit definition archive kind is unsupported")
            if archived.is_symlink() or not archived.is_file():
                raise RuntimeError("unit definition archive is incomplete")
            if hashlib.sha256(archived.read_bytes()).hexdigest() != row["content_sha256"]:
                raise RuntimeError("unit definition archive hash changed")
        if is_loaded_absent_definition(definition):
            if path_lexists(unit_backup / "dropins") or path_lexists(unit_backup / "retired-local.service"):
                raise RuntimeError("absent unit definition evidence contains invented archives")
            return {"verified": True, "definition_mode": "loaded_fragment_absent",
                    "restorable": False, "can_retire_local": False,
                    "can_mask_if_still_absent": True, "error_type": None}
        return {"verified": True, "definition_mode": "archived",
                "restorable": True, "can_retire_local": True,
                "can_mask_if_still_absent": False, "error_type": None}
    except Exception as error:
        return {"verified": False, "definition_mode": None, "restorable": False,
                "can_retire_local": False, "can_mask_if_still_absent": False,
                "error_type": type(error).__name__}


def closure_state(require_masked):
    state = {}
    try:
        state.update({"active": prop(DRAMA_UNIT, "ActiveState"),
                      "substate": prop(DRAMA_UNIT, "SubState"),
                      "enabled": prop(DRAMA_UNIT, "UnitFileState"),
                      "load_state": prop(DRAMA_UNIT, "LoadState"),
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
    persistent_mask = (not require_masked or
                       is_persistent_mask(pathlib.Path("/etc/systemd/system") / DRAMA_UNIT))
    state["persistent_mask"] = persistent_mask
    closed = (state["active"] == "inactive" and state["substate"] == "dead" and
              state["pid"] == 0 and state["control_pid"] == 0 and
              not state["control_group"] and state["port_8787_listener_count"] == 0 and
              state["port_8787_established_count"] == 0 and
              (not require_masked or (state["enabled"] == "masked" and
                                      state["load_state"] == "masked")) and persistent_mask)
    return state, closed, None


def loaded_absent_mutation_guard(definition):
    if not is_loaded_absent_definition(definition):
        raise RuntimeError("loaded absent mutation guard received a different definition mode")
    local = DRAMA_LOCAL_FRAGMENT
    persistent_mask = is_persistent_mask(local)
    fragment_absent = not path_lexists(local)
    if (not persistent_mask and not fragment_absent) or path_lexists(DRAMA_DROPIN_DIR):
        raise RuntimeError("loaded absent drama definition topology changed")
    fragment = definition["fragment"]
    unit_paths = current_systemd_unit_paths(fragment["systemd_unit_paths"])
    link = drama_wants_link_record(local, unit_paths, allow_absent=True)
    original_link = fragment["enablement_links"][0]
    if link.get("kind") == "dangling-symlink" and link != original_link:
        raise RuntimeError("loaded absent drama enablement identity changed")
    if persistent_mask and link.get("kind") != "absent":
        raise RuntimeError("masked drama source still has an enablement link")
    return {"fragment_absent": fragment_absent, "persistent_mask": persistent_mask,
            "dropin_absent": True, "enablement_kind": link.get("kind"),
            "enablement_record": link, "systemd_unit_paths": unit_paths}


def fail_closed_drama(stage, shared_before, original_error, unit_backup,
                      original_state=None):
    # All commands are scoped to the retired API. The shared reverse tunnel is
    # evidence-only and is never a systemctl command argument.
    commands = {"stop": best_effort_command(["systemctl", "stop", DRAMA_UNIT])}
    capability = ({"verified": False, "definition_mode": None, "restorable": False,
                   "can_retire_local": False, "can_mask_if_still_absent": False,
                   "error_type": "MissingBackup"} if unit_backup is None else
                  verified_definition_backup(unit_backup))
    commands["definition_capability"] = capability
    mask_allowed = False
    definition = None
    local = DRAMA_LOCAL_FRAGMENT
    if capability["verified"]:
        if capability["definition_mode"] == "loaded_fragment_absent":
            try:
                definition = read_private_json(unit_backup / "definition.json")
                absent_guard = loaded_absent_mutation_guard(definition)
                commands["loaded_absent_guard_before_disable"] = absent_guard
                commands["disable"] = remove_loaded_absent_enablement(
                    definition["fragment"]["systemd_unit_paths"])
                after_disable = loaded_absent_mutation_guard(definition)
                commands["loaded_absent_guard_after_disable"] = after_disable
                mask_allowed = (after_disable["enablement_kind"] == "absent" and
                                after_disable["dropin_absent"] and
                                (after_disable["fragment_absent"] or
                                 after_disable["persistent_mask"]))
            except Exception as error:
                commands["loaded_absent_guard_error_type"] = type(error).__name__
        else:
            commands["archived_definition_recovery_refused"] = {
                "value": True, "reason": "enablement topology was not recorded"}
    fence_sequence_complete = False
    if mask_allowed:
        commands["mask"] = best_effort_command(["systemctl", "mask", "--no-reload", DRAMA_UNIT])
        mask_succeeded = (commands["mask"].get("rc") == 0 and
                          commands["mask"].get("error_type") is None)
        commands["direct_persistent_mask"] = {
            "value": mask_succeeded and is_persistent_mask(local),
            "error_type": None}
        if commands["direct_persistent_mask"]["value"]:
            try:
                fsync_directory(local.parent)
                if not is_persistent_mask(local):
                    raise RuntimeError("drama persistent mask changed after directory sync")
                commands["mask_parent_fsync"] = {"value": True, "error_type": None}
            except Exception as error:
                commands["mask_parent_fsync"] = {
                    "value": False, "error_type": type(error).__name__}
            if commands["mask_parent_fsync"]["value"]:
                commands["daemon_reload"] = best_effort_command(["systemctl", "daemon-reload"])
                commands["post_reload_stop"] = best_effort_command(["systemctl", "stop", DRAMA_UNIT])
                fence_sequence_complete = all(
                    commands[name].get("rc") == 0 and commands[name].get("error_type") is None
                    for name in ("daemon_reload", "post_reload_stop"))
    final_state = {}
    final_closed = False
    verification_error = None
    observation_error = None
    full_topology_verified = False
    shared_unchanged = False
    shared_after = {}
    try:
        observation = capture_drama_final_observation(definition=definition)
        final_state.update(observation["source"])
        final_state["port_8787_listener_count"] = len(
            observation["port_8787_listener_rows"])
        final_state["port_8787_established_count"] = len(
            observation["port_8787_established_rows"])
        shared_after = observation["shared_tunnel"]
        shared_unchanged = shared_after == shared_before
    except Exception as error:
        observation_error = type(error).__name__
        verification_error = observation_error
    if (observation_error is None and fence_sequence_complete and
            definition is not None and original_state is not None):
        try:
            verified_state, shared_after = validate_drama_fenced(
                shared_before, definition=definition, original_state=original_state,
                observation=observation)
            final_state.update(verified_state)
            full_topology_verified = True
        except Exception as error:
            verification_error = type(error).__name__
    commands["shared_tunnel_unchanged"] = {"value": shared_unchanged,
                                            "error_type": observation_error}
    commands["full_topology_verified"] = {"value": full_topology_verified,
                                           "error_type": verification_error}
    final_state["shared_tunnel_unchanged"] = shared_unchanged
    final_closed = shared_unchanged and full_topology_verified
    evidence_error = None
    try:
        write_failure_evidence(stage, type(original_error).__name__, commands,
                               final_state, final_closed, shared_before,
                               shared_after=shared_after,
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
    mkdir_private(unit_backup, exist_ok=True)
    manifest = unit_backup / "definition.json"
    manifest_present = path_lexists(manifest)
    if manifest_present:
        if path_is_symlink(manifest):
            raise RuntimeError("unit definition manifest is a symlink")
        definition = read_private_json(manifest)
        fsync_directory(manifest.parent)
        if read_private_json(manifest) != definition:
            raise RuntimeError("unit definition manifest changed during directory sync")
        expected = definition["fragment"]
        if expected.get("kind") == "loaded_fragment_absent":
            raise RuntimeError("originally absent unit fragment appeared before masking")
        if expected.get("path") != str(local) or definition_path_record(str(local)) != expected:
            raise RuntimeError("local unit no longer matches its archived definition")
    retired = unit_backup / "retired-local.service"
    original = local.read_bytes()
    if manifest_present and hashlib.sha256(original).hexdigest() != expected["content_sha256"]:
        raise RuntimeError("local unit content differs from its archived definition")
    if path_lexists(retired):
        if read_private_bytes(retired) != original:
            raise RuntimeError("retired unit archive differs from current unit")
        fsync_directory(retired.parent)
        if read_private_bytes(retired) != original:
            raise RuntimeError("retired unit archive changed during directory sync")
    else:
        write_private_bytes(retired, original)
    if read_private_bytes(retired) != original or local.read_bytes() != original:
        raise RuntimeError("unit changed during retirement archive")
    if manifest_present and definition_path_record(str(local)) != expected:
        raise RuntimeError("local unit changed before retirement")
    local.unlink()
    fsync_directory(local.parent)
    if path_lexists(local):
        raise RuntimeError("local unit retirement was not durable")


def is_persistent_mask(local):
    try:
        return (path_lexists(local) and path_is_symlink(local) and
                os.readlink(str(local)) == "/dev/null")
    except (OSError, RuntimeError):
        return False


def apply_locked_source_fence(a, states, shared_before, checkpoint_sha256,
                              split_ad_before=None):
    snapshot = BASE / (a.group + "-before.json")
    if path_lexists(snapshot) and not a.resume:
        raise RuntimeError("fence snapshot already exists; inspect partial result before retry")
    if a.resume:
        if (not path_lexists(snapshot) or path_is_symlink(snapshot) or
                not snapshot.is_file()):
            raise RuntimeError("resume requires an existing initial fence snapshot")
        initial = read_private_json(snapshot)
        fsync_directory(snapshot.parent)
        if read_private_json(snapshot) != initial:
            raise RuntimeError("initial fence snapshot changed during directory sync")
        if [s["unit"] for s in initial["states"]] != GROUPS[a.group]:
            raise RuntimeError("initial snapshot service scope changed")
        if a.group == "materials-images" and initial.get("us_ad_baseline") != split_ad_before:
            raise RuntimeError("temporary US ad lane changed since initial fence snapshot")
        live_states = states
        states = initial["states"]
        if a.group == "drama":
            if not states[0].get("definition"):
                raise RuntimeError("initial drama unit definition evidence is missing")
            if initial.get("shared_tunnel") != shared_before:
                raise RuntimeError("shared drama tunnel changed since initial fence snapshot")
            if initial.get("port_8787_established") != []:
                raise RuntimeError("initial drama request proof is invalid")
            if not (live_states[0]["enabled"] == "masked" and live_states[0]["pid"] == 0):
                validate_recorded_definition_current(states[0]["definition"], live_states[0],
                                                     resume=True)
    else:
        snapshot_payload = {"checkpoint_sha256": checkpoint_sha256,
                            "states": states, "created_at_epoch": time.time()}
        if a.group == "drama":
            snapshot_payload["shared_tunnel"] = shared_before
            snapshot_payload["port_8787_established"] = []
        if a.group == "materials-images":
            snapshot_payload["us_ad_baseline"] = split_ad_before
        write_private_json(snapshot, snapshot_payload)
    mutation_started = False
    stage = "pre-mutation"
    try:
        for state in states:
            u = state["unit"]
            unit_backup = BASE / u
            transition_results = {}
            if prop(u, "UnitFileState") == "masked":
                if int(prop(u, "MainPID") or 0) or prop(u, "ActiveState") not in ("inactive", "failed"):
                    raise RuntimeError("masked source is still active: " + u)
                if a.group == "drama":
                    capability = verified_definition_backup(unit_backup)
                    if not capability["verified"]:
                        raise RuntimeError("masked drama source definition backup is not verified: " +
                                           str(capability["error_type"]))
                    if read_private_json(unit_backup / "definition.json") != state["definition"]:
                        raise RuntimeError("masked drama definition evidence differs from initial snapshot")
                    final_state, shared_after = validate_drama_fenced(
                        shared_before, definition=state["definition"], original_state=state)
                    write_drama_success_evidence(final_state, shared_before, shared_after,
                                                 checkpoint_sha256,
                                                 transition_results={"resume_already_masked": True})
                print(json.dumps({"already_fenced": u}))
                continue
            stage = "backup-unit-definition"
            mkdir_private(unit_backup, exist_ok=a.resume)
            backup_unit_definition(state, unit_backup, resume=a.resume)
            if a.group == "drama":
                capability = verified_definition_backup(unit_backup)
                if not capability["verified"]:
                    raise RuntimeError("drama source definition evidence was not verified before mutation: " +
                                       str(capability["error_type"]))
                if read_private_json(unit_backup / "definition.json") != state["definition"]:
                    raise RuntimeError("drama definition evidence differs from initial snapshot")
                # Reassert the request/process and shared-tunnel guards as the
                # last read-only action before stopping the old source.
                current_source = inspect(DRAMA_UNIT)
                validate_drama_preflight([current_source], resume=a.resume)
                validate_recorded_definition_current(state["definition"], current_source,
                                                     resume=a.resume)
                if shared_tunnel_snapshot() != shared_before:
                    raise RuntimeError("shared drama tunnel changed before source stop")
            stage = "stop-source"
            if a.group != "drama" or current_source.get("pid") or current_source.get("active") != "inactive":
                mutation_started = True
                run(["systemctl", "stop", u])
            stage = "disable-source"
            if a.group == "drama":
                stopped_state, stopped_closed, stopped_error = closure_state(require_masked=False)
                if not stopped_closed:
                    raise RuntimeError("drama source did not close after stop: " + str(stopped_error))
                stopped_source = inspect(DRAMA_UNIT)
                validate_recorded_definition_current(state["definition"], stopped_source,
                                                     resume=True)
                if capability["definition_mode"] == "loaded_fragment_absent":
                    mutation_started = True
                    transition_results["disable_no_reload"] = remove_loaded_absent_enablement(
                        state["definition"]["fragment"]["systemd_unit_paths"])
                else:
                    mutation_started = True
                    run(["systemctl", "disable", "--no-reload", u])
                disabled_source = inspect(DRAMA_UNIT)
                validate_recorded_definition_current(state["definition"], disabled_source,
                                                     resume=True,
                                                     require_enablement_removed=True)
            else:
                mutation_started = True
                run(["systemctl", "disable", u])
                if int(prop(u, "MainPID") or 0) or prop(u, "ActiveState") not in ("inactive", "failed"):
                    raise RuntimeError("source still running: " + u)
            stage = "retire-local-unit"
            local = pathlib.Path("/etc/systemd/system") / u
            if a.group == "drama" and capability["definition_mode"] == "loaded_fragment_absent":
                if path_lexists(local) or path_lexists(DRAMA_DROPIN_DIR):
                    raise RuntimeError("originally absent drama definition appeared before mask")
            elif path_lexists(local) and not is_persistent_mask(local):
                retire_local_unit(local, unit_backup)
            stage = "mask-source"
            mutation_started = True
            run(["systemctl", "mask", "--no-reload", u] if a.group == "drama" else
                ["systemctl", "mask", u])
            if a.group == "drama" and not is_persistent_mask(local):
                raise RuntimeError("direct persistent mask verification failed: " + u)
            if a.group == "drama":
                fsync_directory(local.parent)
                if not is_persistent_mask(local):
                    raise RuntimeError("persistent mask changed after directory sync: " + u)
            stage = "daemon-reload"
            run(["systemctl", "daemon-reload"])
            if a.group == "drama":
                stage = "post-reload-stop"
                run(["systemctl", "stop", u])
            if (prop(u, "UnitFileState") != "masked" or
                    (a.group == "drama" and prop(u, "LoadState") != "masked")):
                raise RuntimeError("persistent mask verification failed: " + u)
            if a.group == "drama":
                stage = "verify-final-drama-fence"
                final_state, shared_after = validate_drama_fenced(
                    shared_before, definition=state["definition"], original_state=state)
                final = {"fenced": u, "active": final_state["active"],
                         "substate": final_state["substate"], "enabled": final_state["enabled"],
                         "pid": final_state["pid"], "port_8787_listener": False,
                         "port_8787_process": False,
                         "shared_tunnel_unchanged": shared_after == shared_before}
                write_drama_success_evidence(final_state, shared_before, shared_after,
                                             checkpoint_sha256,
                                             transition_results=transition_results)
                print(json.dumps(final))
            else:
                print(json.dumps({"fenced": u, "active": prop(u, "ActiveState"),
                                  "enabled": prop(u, "UnitFileState")}))
                if (a.group == "materials-images" and
                        split_ad_baseline() != split_ad_before):
                    raise RuntimeError("temporary US ad lane changed while fencing image sources")
    except Exception as original_error:
        if a.group == "drama" and mutation_started:
            fail_closed_drama(stage, shared_before, original_error, unit_backup,
                              original_state=state)
        raise
    if a.group == "materials-images":
        split_ad_after = split_ad_baseline()
        if split_ad_after != split_ad_before:
            raise RuntimeError("temporary US ad lane changed after image source fence")
        evidence_path = BASE / "materials-images-after.json"
        evidence = {
            "group": a.group, "completed_at_epoch": time.time(),
            "checkpoint_sha256": checkpoint_sha256,
            "us_ad_baseline_before": split_ad_before,
            "us_ad_baseline_after": split_ad_after,
            "ad_lane_unchanged": True,
        }
        if path_lexists(evidence_path):
            existing = read_private_json(evidence_path)
            for value in (existing, evidence):
                value.pop("completed_at_epoch", None)
            if existing != evidence:
                raise RuntimeError("existing materials-images success evidence differs")
        else:
            write_private_json(evidence_path, evidence)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("group", choices=sorted(GROUPS))
    ap.add_argument("--checkpoint", type=pathlib.Path)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--resume", action="store_true", help="resume a recorded partial fence with fresh drain proof")
    a = ap.parse_args()
    if socket.gethostname() != "VM-0-13-centos":
        raise RuntimeError("wrong host: US source only")
    if a.group == "materials" and a.apply:
        raise RuntimeError(
            "legacy coupled materials --apply is disabled; use reviewed materials-images or materials-ad scope")
    source_storage_guard(create=False)
    states = [inspect(u) for u in GROUPS[a.group]]
    assert_idle(states)
    shared_before = None
    split_ad_before = None
    if a.group == "drama":
        if not a.resume:
            states[0]["definition"] = unit_definition_snapshot(DRAMA_UNIT, state=states[0])
        validate_drama_preflight(states, resume=a.resume)
        shared_before = shared_tunnel_snapshot()
        validate_shared_tunnel_baseline(shared_before)
    if a.group == "materials-images":
        split_ad_before = split_ad_baseline()
    if not a.apply:
        result = {"dry_run": True, "group": a.group, "states": states}
        if shared_before is not None:
            result["shared_tunnel"] = shared_before
            result["port_8787_established"] = []
        if split_ad_before is not None:
            result["us_ad_baseline"] = split_ad_before
        if a.group == "materials":
            result["deprecated_apply_scope"] = True
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
    if a.group in ("materials-images", "materials-ad"):
        validate_materials_split_checkpoint(
            proof, a.group, states, ad_baseline=split_ad_before)
    checkpoint_sha256 = hashlib.sha256(a.checkpoint.read_bytes()).hexdigest()
    source_storage_guard(create=True)
    lock_handle = acquire_source_lock()
    try:
        apply_locked_source_fence(a, states, shared_before, checkpoint_sha256,
                                  split_ad_before=split_ad_before)
    finally:
        lock_handle.close()


if __name__ == "__main__":
    main()
