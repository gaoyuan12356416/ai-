#!/usr/bin/env python3
"""Shared fail-closed primitives for the reviewed drama production operators.

This module contains no business or network mutation.  Callers must bind every
host, path, unit and release identity before using the mutation helpers.
"""
from __future__ import print_function

import contextlib
import ctypes
import errno
import hashlib
import http.client
import json
import os
import pathlib
import platform
import socket
import stat
import subprocess
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - production is Linux; tests patch locks.
    fcntl = None


RUN_ID = "gpu-service-migration-20260828T1502"
OLD_SHA = "a1519413b23d20acab035853b0f5aeebee53e9ac"
NEW_SHA = "0d2dc5ee90d056a58231fd0292186d73b0d083f8"
NEW_REMOTE_REF = "refs/remotes/origin/codex/drama-legacy-intro-resume-20260901"
GITHUB_REMOTE = "https://github.com/gaoyuan12356416/ai-.git"
CPU_HOST = "VM-0-108-centos"
HK_HOST = "VM-0-125-centos"

CPU_DATA_ROOT = pathlib.Path("/mnt/data-disk")
HK_DATA_ROOT = pathlib.Path("/data")
CPU_LIVE_ROOT = pathlib.Path("/root/drama_material_service")
HK_BASE = pathlib.Path("/data/drama-synthesis-gpu")

CPU_TARGET_UNITS = (
    "drama-material-job-worker.service",
    "drama-material-api.service",
)
HK_TARGET_UNITS = (
    "drama-synthesis-gpu-worker.service",
    "drama-synthesis-gpu-tunnel.service",
)
HK_PROTECTED_UNITS = (
    "fb-page-random-overlay-gpu.service",
    "fb-page-random-overlay-tunnel.service",
    "tt-gpu-direct-outro-reverse-tunnel.service",
    "tt-gpu-direct-outro.service",
    "tt-gpu-publisher.service",
    "tt-gpu-reverse-tunnel.service",
    "x-post-media-repair-tunnel.service",
    "x-post-media-repair.service",
)

CPU_OLD_FILES = {
    "app.py": "aef231f60cb81886a5745583b430ba1e131f4bb6db96090e179a100cf2b512a1",
    "features/drama_synthesis/async_runtime.py":
        "a8a0815b877dbe4536cc291c26fad88186b19a1dc313d51e254d6e246334cb2e",
}
CPU_NEW_FILES = {
    "app.py": "792986214f49e3355aae4b7adf61547d8cedd33ff96f1b12b00bcfcbd4ec2ce6",
    "features/drama_synthesis/async_runtime.py":
        "2981974839134254fe573b82c62170150f258f9c444c3175f5c240c594d1f45b",
}

SHOW_PROPERTIES = (
    "Id", "LoadState", "ActiveState", "SubState", "MainPID", "ControlPID",
    "NRestarts", "ExecMainStartTimestampMonotonic",
    "ActiveEnterTimestampMonotonic", "FragmentPath", "DropInPaths",
    "ControlGroup", "UnitFileState", "NeedDaemonReload", "Restart",
)


class OperatorError(RuntimeError):
    pass


def run(argv, check=True, pass_fds=(), env=None):
    options = {
        "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
        "universal_newlines": True, "close_fds": True,
    }
    if pass_fds:
        options["pass_fds"] = tuple(pass_fds)
    if env is not None:
        options["env"] = env
    process = subprocess.Popen(list(argv), **options)
    stdout, stderr = process.communicate()
    if check and process.returncode:
        raise OperatorError("command failed: %s rc=%d stderr_sha256=%s" % (
            argv[0], process.returncode,
            hashlib.sha256(stderr.encode("utf-8", "replace")).hexdigest(),
        ))
    return process.returncode, stdout, stderr


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_fd(descriptor):
    digest = hashlib.sha256()
    offset = 0
    while True:
        if hasattr(os, "pread"):
            block = os.pread(descriptor, 1024 * 1024, offset)
        else:  # pragma: no cover - Linux production always has pread.
            os.lseek(descriptor, offset, os.SEEK_SET)
            block = os.read(descriptor, 1024 * 1024)
        if not block:
            break
        digest.update(block)
        offset += len(block)
    return digest.hexdigest()


def sha256_file(path):
    descriptor = open_nofollow_regular(path)
    try:
        return sha256_fd(descriptor)
    finally:
        os.close(descriptor)


def mtime_ns(value):
    return int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1000000000)))


def identity_tuple(value):
    return (int(value.st_dev), int(value.st_ino), int(value.st_size),
            mtime_ns(value), int(value.st_mode))


def stat_record(value):
    return {
        "device": int(value.st_dev), "inode": int(value.st_ino),
        "size": int(value.st_size), "mtime_ns": mtime_ns(value),
        "mode": int(value.st_mode), "uid": int(getattr(value, "st_uid", -1)),
        "gid": int(getattr(value, "st_gid", -1)),
    }


def fsync_directory(path):
    if os.name == "nt":  # exercised only by local unit tests.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISDIR(value.st_mode):
            raise OperatorError("durability boundary is not a directory")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def path_lexists(path):
    return os.path.lexists(str(path))


def real_directory(path, expected_parent=None, require_root_owner=True):
    path = pathlib.Path(path)
    if not path.is_absolute() or not path_lexists(path):
        raise OperatorError("required absolute directory is missing: %s" % path)
    value = os.lstat(str(path))
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise OperatorError("directory is not a real directory: %s" % path)
    if os.path.realpath(str(path)) != str(path):
        raise OperatorError("directory canonical identity changed: %s" % path)
    if require_root_owner and os.name != "nt":
        if value.st_uid != 0 or value.st_gid != 0 or stat.S_IMODE(value.st_mode) & 0o022:
            raise OperatorError("trusted directory owner or mode is unsafe: %s" % path)
    if expected_parent is not None:
        try:
            path.relative_to(pathlib.Path(expected_parent))
        except ValueError:
            raise OperatorError("directory escaped its approved root: %s" % path)
    return value


def validate_existing_ancestry(path, trusted_root=None, require_root_owner=True):
    path = pathlib.Path(path)
    if not path.is_absolute():
        raise OperatorError("path is not absolute")
    if trusted_root is None:
        current = pathlib.Path(path.anchor)
        parts = path.parts[1:]
    else:
        trusted_root = pathlib.Path(trusted_root)
        real_directory(trusted_root, require_root_owner=require_root_owner)
        try:
            relative = path.relative_to(trusted_root)
        except ValueError:
            raise OperatorError("path escaped trusted root")
        current = trusted_root
        parts = relative.parts
    for part in parts:
        current = current / part
        if not path_lexists(current):
            raise OperatorError("required path ancestry is missing: %s" % current)
        value = os.lstat(str(current))
        if stat.S_ISLNK(value.st_mode):
            raise OperatorError("path ancestry contains a symlink: %s" % current)
        if current != path and not stat.S_ISDIR(value.st_mode):
            raise OperatorError("path ancestry contains a non-directory: %s" % current)
    return os.lstat(str(path))


def create_private_ancestry(root, target):
    root = pathlib.Path(root)
    target = pathlib.Path(target)
    real_directory(root)
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise OperatorError("private path escaped data root")
    current = root
    for part in relative.parts:
        current = current / part
        if path_lexists(current):
            value = os.lstat(str(current))
            if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
                raise OperatorError("private path collision: %s" % current)
            if os.name != "nt" and (value.st_uid != 0 or value.st_gid != 0 or
                                     stat.S_IMODE(value.st_mode) & 0o077):
                raise OperatorError("private path permissions changed: %s" % current)
            continue
        os.mkdir(str(current), 0o700)
        if os.name != "nt":
            os.chown(str(current), 0, 0)
            os.chmod(str(current), 0o700)
        fsync_directory(current.parent)
    return target


def write_exclusive_bytes(path, payload, mode=0o600):
    path = pathlib.Path(path)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
             getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    descriptor = os.open(str(path), flags, mode)
    try:
        if os.name != "nt":
            os.fchown(descriptor, 0, 0)
            os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OperatorError("exclusive evidence write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)


def write_exclusive_json(path, value):
    payload = json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    write_exclusive_bytes(path, payload)
    return sha256_bytes(payload)


def open_nofollow_regular(path, writable=False):
    flags = ((os.O_RDWR if writable else os.O_RDONLY) |
             getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    descriptor = os.open(str(path), flags)
    value = os.fstat(descriptor)
    if not stat.S_ISREG(value.st_mode):
        os.close(descriptor)
        raise OperatorError("path is not a nofollow regular file: %s" % path)
    return descriptor


def anchored_file(path, expected_sha256=None, expected_inode=None, expected_size=None):
    path = pathlib.Path(path)
    before = os.lstat(str(path))
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise OperatorError("anchored path is not a regular file: %s" % path)
    descriptor = open_nofollow_regular(path)
    opened = os.fstat(descriptor)
    if identity_tuple(before) != identity_tuple(opened):
        os.close(descriptor)
        raise OperatorError("anchored path changed during open: %s" % path)
    if expected_inode is not None and int(opened.st_ino) != int(expected_inode):
        os.close(descriptor)
        raise OperatorError("anchored inode mismatch: %s" % path)
    if expected_size is not None and int(opened.st_size) != int(expected_size):
        os.close(descriptor)
        raise OperatorError("anchored size mismatch: %s" % path)
    digest = sha256_fd(descriptor)
    after = os.fstat(descriptor)
    current = os.lstat(str(path))
    if identity_tuple(opened) != identity_tuple(after) or identity_tuple(opened) != identity_tuple(current):
        os.close(descriptor)
        raise OperatorError("anchored file changed while hashing: %s" % path)
    if expected_sha256 is not None and digest != expected_sha256:
        os.close(descriptor)
        raise OperatorError("anchored SHA256 mismatch: %s" % path)
    return descriptor, stat_record(opened), digest


def atomic_rename_noreplace(source, destination):
    source = pathlib.Path(source)
    destination = pathlib.Path(destination)
    if path_lexists(destination):
        raise OperatorError("no-replace destination already exists")
    result = _renameat2(source, destination, 1)
    if result != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise OperatorError("no-replace destination appeared concurrently")
        raise OperatorError("atomic no-replace rename failed errno=%d" % code)


def atomic_rename_exchange(left, right):
    if not path_lexists(left) or not path_lexists(right):
        raise OperatorError("exchange requires two existing entries")
    result = _renameat2(pathlib.Path(left), pathlib.Path(right), 2)
    if result != 0:
        raise OperatorError("atomic exchange failed errno=%d" % ctypes.get_errno())


def _renameat2(source, destination, flags):
    if os.name == "nt":
        raise OperatorError("renameat2 is unavailable outside Linux")
    library = ctypes.CDLL(None, use_errno=True)
    wrapper = getattr(library, "renameat2", None)
    encoded_source = os.fsencode(str(source))
    encoded_destination = os.fsencode(str(destination))
    if wrapper is not None:
        wrapper.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                            ctypes.c_char_p, ctypes.c_uint]
        wrapper.restype = ctypes.c_int
        return wrapper(-100, encoded_source, -100, encoded_destination, flags)
    numbers = {"x86_64": 316, "amd64": 316, "aarch64": 276}
    number = numbers.get(platform.machine().lower())
    syscall = getattr(library, "syscall", None)
    if number is None or syscall is None:
        raise OperatorError("renameat2 syscall is unavailable")
    syscall.restype = ctypes.c_long
    return int(syscall(ctypes.c_long(number), ctypes.c_long(-100),
                       ctypes.c_char_p(encoded_source), ctypes.c_long(-100),
                       ctypes.c_char_p(encoded_destination), ctypes.c_uint(flags)))


@contextlib.contextmanager
def exclusive_lock(path):
    if fcntl is None:
        raise OperatorError("Linux flock support is unavailable")
    descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name != "nt":
            os.fchown(descriptor, 0, 0)
            os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            raise OperatorError("another drama operator holds the lock")
        yield descriptor
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def require_host(expected_host):
    if socket.gethostname() != expected_host:
        raise OperatorError("operator is bound to a different host")
    if hasattr(os, "geteuid") and (os.geteuid() != 0 or os.getegid() != 0):
        raise OperatorError("operator requires root user and group")


def mount_identity(data_root):
    code, stdout, _ = run(["findmnt", "-rn", "-o", "TARGET,SOURCE,FSTYPE,UUID", "-T", str(data_root)])
    rows = [line.split() for line in stdout.splitlines() if line.strip()]
    if code or len(rows) != 1 or len(rows[0]) != 4:
        raise OperatorError("data filesystem identity is ambiguous")
    target, source, fstype, uuid = rows[0]
    return {"target": target, "source": source, "fstype": fstype, "uuid": uuid,
            "binding": "%s|%s|%s|%s" % (target, source, fstype, uuid)}


def validate_data_root(role, data_root, expected_device):
    approved = CPU_DATA_ROOT if role == "cpu" else HK_DATA_ROOT
    if pathlib.Path(data_root) != approved:
        raise OperatorError("data root is outside the approved host scope")
    real_directory(approved)
    identity = mount_identity(approved)
    if identity["source"] != expected_device:
        raise OperatorError("data device does not match explicit apply binding")
    if role == "cpu" and identity["target"] != str(CPU_DATA_ROOT):
        raise OperatorError("CPU data disk is not an exact mount point")
    if role == "hk" and identity["target"] not in ("/", str(HK_DATA_ROOT)):
        raise OperatorError("HK /data does not resolve to the approved filesystem")
    if role == "cpu":
        root_identity = os.stat("/")
        data_identity = os.stat(str(CPU_DATA_ROOT))
        if root_identity.st_dev == data_identity.st_dev:
            raise OperatorError("CPU data disk unexpectedly fell back to the root filesystem")
    return identity


def process_startticks(pid):
    raw = pathlib.Path("/proc/%d/stat" % int(pid)).read_text()
    close = raw.rfind(")")
    fields = raw[close + 2:].split() if close >= 0 else []
    if len(fields) <= 19:
        raise OperatorError("process start identity is unreadable")
    return int(fields[19])


def process_children(pid):
    path = pathlib.Path("/proc/%d/task/%d/children" % (int(pid), int(pid)))
    if not path.is_file():
        raise OperatorError("process children identity is unreadable")
    return sorted(int(value) for value in path.read_text().split())


def cgroup_pids(control_group):
    candidates = [pathlib.Path("/sys/fs/cgroup") / control_group.lstrip("/"),
                  pathlib.Path("/sys/fs/cgroup/systemd") / control_group.lstrip("/")]
    found = []
    for base in candidates:
        path = base / "cgroup.procs"
        if path.is_file():
            found.append((str(path), sorted(int(value) for value in path.read_text().split())))
    if len(found) != 1:
        raise OperatorError("unit cgroup identity is ambiguous")
    return {"path": found[0][0], "pids": found[0][1]}


def fragment_record(path):
    path = pathlib.Path(path)
    descriptor, record, digest = anchored_file(path)
    os.close(descriptor)
    record.update({"path": str(path), "sha256": digest})
    return record


def unit_identity(unit):
    argv = ["systemctl", "show", "--no-pager"]
    for name in SHOW_PROPERTIES:
        argv.extend(["-p", name])
    argv.append(unit)
    _, stdout, _ = run(argv)
    values = dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)
    if values.get("Id") != unit or values.get("LoadState") != "loaded":
        raise OperatorError("exact systemd unit is not loaded: %s" % unit)
    if values.get("NeedDaemonReload") != "no":
        raise OperatorError("systemd manager has a stale unit definition: %s" % unit)
    fragment_path = values.get("FragmentPath") or ""
    if not fragment_path.startswith("/"):
        raise OperatorError("unit FragmentPath is missing or non-absolute: %s" % unit)
    fragment = fragment_record(fragment_path)
    dropins = []
    for raw in (values.get("DropInPaths") or "").split():
        dropins.append(fragment_record(raw))
    pid = int(values.get("MainPID") or 0)
    control_pid = int(values.get("ControlPID") or 0)
    process = None
    cgroup = None
    if pid:
        process = {
            "pid": pid, "startticks": process_startticks(pid),
            "children": process_children(pid),
            "cwd": os.readlink("/proc/%d/cwd" % pid),
            "exe": os.readlink("/proc/%d/exe" % pid),
        }
    group = values.get("ControlGroup") or ""
    if group:
        cgroup = cgroup_pids(group)
    return {
        "unit": unit, "systemd": values, "fragment": fragment,
        "dropins": dropins, "process": process, "cgroup": cgroup,
    }


def snapshot_units(units):
    if tuple(units) != tuple(dict.fromkeys(units)):
        raise OperatorError("unit scope contains duplicates")
    return {unit: unit_identity(unit) for unit in units}


def unit_config_signature(item):
    return {
        "unit": item["unit"], "fragment": item["fragment"],
        "dropins": item["dropins"],
        "unit_file_state": item["systemd"].get("UnitFileState"),
        "restart": item["systemd"].get("Restart"),
    }


def protected_signature(item):
    process = item.get("process")
    return {
        "config": unit_config_signature(item),
        "active": item["systemd"].get("ActiveState"),
        "substate": item["systemd"].get("SubState"),
        "pid": process.get("pid") if process else 0,
        "startticks": process.get("startticks") if process else 0,
        "nrestarts": int(item["systemd"].get("NRestarts") or 0),
        "control_pid": int(item["systemd"].get("ControlPID") or 0),
        "cgroup_pids": (item.get("cgroup") or {}).get("pids", []),
    }


def assert_protected_units(before, after):
    if set(before) != set(after):
        raise OperatorError("protected unit scope changed")
    changed = [unit for unit in before
               if protected_signature(before[unit]) != protected_signature(after[unit])]
    if changed:
        raise OperatorError("protected unit identity changed: %s" % ",".join(sorted(changed)))


def assert_active_single_process(item):
    values = item["systemd"]
    process = item.get("process")
    if (values.get("ActiveState") != "active" or values.get("SubState") != "running" or
            not process or int(values.get("ControlPID") or 0) != 0 or
            process.get("children") or (item.get("cgroup") or {}).get("pids") != [process["pid"]]):
        raise OperatorError("unit is not a drained single-process service: %s" % item["unit"])


def assert_inactive_unit(item):
    values = item["systemd"]
    if (values.get("ActiveState") != "inactive" or values.get("SubState") != "dead" or
            int(values.get("MainPID") or 0) != 0 or int(values.get("ControlPID") or 0) != 0 or
            (item.get("cgroup") or {}).get("pids", [])):
        raise OperatorError("unit is not fully stopped: %s" % item["unit"])


def assert_no_media_processes():
    matches = []
    for path in pathlib.Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            executable = os.path.basename(os.readlink(str(path / "exe"))).lower()
        except OSError:
            continue
        if executable in ("ffmpeg", "ffprobe"):
            matches.append({"pid": int(path.name), "exe": executable,
                            "startticks": process_startticks(int(path.name))})
    if matches:
        raise OperatorError("ffmpeg or ffprobe is active")
    return []


def assert_no_established_ports(ports):
    _, stdout, _ = run(["ss", "-Hnt", "state", "established"])
    matches = []
    for line in stdout.splitlines():
        if any((":%d " % port) in (line + " ") or (":%d\t" % port) in line
               for port in ports):
            matches.append(line)
    if matches:
        raise OperatorError("drama endpoint still has established connections")
    return []


def exact_health(host, port):
    connection = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        connection.request("GET", "/healthz", headers={
            "Accept": "application/json", "Connection": "close",
            "Host": "%s:%d" % (host, int(port)),
        })
        response = connection.getresponse()
        body = response.read(65537)
        if response.status != 200 or len(body) > 65536:
            raise OperatorError("drama health response was rejected")
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise OperatorError("drama health response is not JSON")
        if decoded != {"ok": True, "role": "media-only"}:
            raise OperatorError("drama health role changed")
        return {"method": "GET", "url": "http://%s:%d/healthz" % (host, port),
                "status": 200, "body": decoded, "body_sha256": sha256_bytes(body)}
    finally:
        connection.close()


def now_epoch():
    return time.time()
