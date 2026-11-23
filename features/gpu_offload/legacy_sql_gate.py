#!/usr/bin/env python3
"""FIFO connection gate for shared MySQL CLI workloads.

The daemon never receives SQL, argv, or credentials. Clients request a lease over
an AF_UNIX socket and keep that socket open while the real mysql process runs.
"""

import argparse
import collections
import concurrent.futures
import datetime
import errno
import json
import os
import re
import select
import signal
import socket
import socketserver
import stat
import struct
import sys
import threading
import time
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows developer machines
    fcntl = None


VERSION = "1.1.0"
PROTOCOL_VERSION = 2
DEFAULT_SOCKET_PATH = "/run/sql-connection-gate/gate.sock"
DEFAULT_STATE_PATH = "/run/sql-connection-gate/state.json"
DEFAULT_LOCK_PATH = "/run/sql-connection-gate/daemon.lock"
DEFAULT_SESSION_LOCK_DIRECTORY = "/var/lib/sql-connection-gate/session-locks"
DEFAULT_ACTIVE_LEDGER_PATH = "/var/lib/sql-connection-gate/active-sessions.json"
DEFAULT_MYSQL_ENTRYPOINT = "/usr/bin/mysql"
DEFAULT_MAINTENANCE_HOLD_PATH = "/usr/local/libexec/mysql-sql-gate-maintenance-hold"
ACTIVE_LEDGER_SCHEMA_VERSION = 1
SESSION_CLEANUP_LOCK_STRIPES = 256
DEFAULT_POOL = "ads_aius-read"
MAX_MESSAGE_BYTES = 4096
SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9_.:@/-]+")
_THREAD_SESSION_LOCKS = {}
_THREAD_SESSION_LOCKS_GUARD = threading.Lock()


class GateProtocolError(RuntimeError):
    pass


class GateQueueTimeout(RuntimeError):
    pass


class GateClientDisconnected(RuntimeError):
    pass


class GateStopping(RuntimeError):
    pass


def utc_now_text():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def monotonic_seconds():
    return time.monotonic()


def safe_label(value, default="unknown", limit=96):
    text = SAFE_LABEL_RE.sub("_", str(value or "").strip())
    return (text or default)[:limit]


def parse_positive_int(value, name, minimum=1, maximum=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("{} must be an integer".format(name))
    if number < minimum or (maximum is not None and number > maximum):
        raise ValueError("{} must be between {} and {}".format(name, minimum, maximum))
    return number


def send_json_line(sock, payload):
    data = (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise GateProtocolError("message too large")
    sock.sendall(data)


def recv_json_line(sock, max_bytes=MAX_MESSAGE_BYTES):
    chunks = bytearray()
    while True:
        chunk = sock.recv(min(1024, max_bytes - len(chunks) + 1))
        if not chunk:
            raise GateClientDisconnected("peer closed before a complete message")
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            raise GateProtocolError("message too large")
        newline = chunks.find(b"\n")
        if newline >= 0:
            raw = bytes(chunks[:newline])
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise GateProtocolError("invalid JSON: {}".format(exc))
            if not isinstance(payload, dict):
                raise GateProtocolError("message must be a JSON object")
            return payload


def get_peer_credentials(sock):
    if hasattr(socket, "SO_PEERCRED"):
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)
    return (0, os.getuid() if hasattr(os, "getuid") else 0, os.getgid() if hasattr(os, "getgid") else 0)


def peer_is_closed(sock):
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True


def read_proc_identity(pid):
    if not pid or int(pid) <= 0:
        return None
    stat_path = "/proc/{}/stat".format(int(pid))
    try:
        with open(stat_path, encoding="utf-8") as handle:
            raw = handle.read().strip()
        comm_end = raw.rfind(")")
        if comm_end < 0:
            return None
        fields = raw[comm_end + 2:].split()
        if len(fields) < 20:
            return None
        return {
            "pid": int(pid),
            "state": fields[0],
            "ppid": int(fields[1]),
            "pgrp": int(fields[2]),
            "session": int(fields[3]),
            "starttime": int(fields[19]),
            "uid": os.stat(stat_path).st_uid,
        }
    except (OSError, ValueError, IndexError):
        return None


def command_option_value(argv, short_name, long_name):
    value = ""
    for index, argument in enumerate(argv):
        if argument in (short_name, long_name) and index + 1 < len(argv):
            value = argv[index + 1]
        elif argument.startswith(short_name) and argument != short_name:
            value = argument[len(short_name):]
        elif argument.startswith(long_name + "="):
            value = argument.split("=", 1)[1]
    return value


def legacy_managed_sql_processes():
    """Return PIDs that make a one-time empty-ledger migration unsafe."""
    try:
        proc_entries = os.listdir("/proc")
    except OSError as exc:
        raise RuntimeError("cannot scan /proc before ledger initialization: {}".format(exc))
    matches = []
    for entry in proc_entries:
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            with open("/proc/{}/cmdline".format(entry), "rb") as handle:
                argv = [item.decode("utf-8", errors="replace") for item in handle.read().split(b"\0") if item]
        except OSError:
            if os.path.exists("/proc/{}".format(entry)):
                raise RuntimeError("cannot inspect live process {} during ledger initialization".format(entry))
            continue
        if not argv:
            continue
        joined = " ".join(argv)
        managed_marker = any(marker in joined for marker in (
            "mysql_gated.py",
            "mysql_process_supervisor.py",
            "mysql-guard-before-sql-gate",
        ))
        exact_target = (
            command_option_value(argv, "-h", "--host") == "101.32.56.53"
            and command_option_value(argv, "-P", "--port") == "63350"
            and command_option_value(argv, "-u", "--user") == "ads_aius"
        )
        held_waiter = (
            len(argv) >= 2
            and "python" in os.path.basename(argv[0]).lower()
            and os.path.realpath(argv[1]).endswith("mysql-sql-gate-maintenance-hold")
        )
        if held_waiter:
            exact_target = False
        if managed_marker or exact_target:
            matches.append(int(entry))
    return sorted(matches)


def assert_maintenance_entrypoint_held():
    if not os.path.isfile(DEFAULT_MAINTENANCE_HOLD_PATH) or not os.access(
        DEFAULT_MAINTENANCE_HOLD_PATH,
        os.X_OK,
    ):
        raise RuntimeError("maintenance hold executable is unavailable")
    try:
        entrypoint_target = os.path.realpath(DEFAULT_MYSQL_ENTRYPOINT)
        hold_target = os.path.realpath(DEFAULT_MAINTENANCE_HOLD_PATH)
    except OSError as exc:
        raise RuntimeError("cannot verify maintenance hold: {}".format(exc))
    if entrypoint_target != hold_target:
        raise RuntimeError("/usr/bin/mysql must be atomically switched to the maintenance hold")


def assert_empty_legacy_migration_window(stable_seconds=1.0):
    assert_maintenance_entrypoint_held()
    first = legacy_managed_sql_processes()
    if first:
        raise RuntimeError("legacy managed SQL processes are still active: {}".format(
            ",".join(str(pid) for pid in first)
        ))
    time.sleep(max(0.0, float(stable_seconds)))
    second = legacy_managed_sql_processes()
    if second:
        raise RuntimeError("legacy managed SQL processes appeared during drain proof: {}".format(
            ",".join(str(pid) for pid in second)
        ))
    assert_maintenance_entrypoint_held()


def process_exists(pid):
    if not pid or pid <= 0:
        return False
    identity = read_proc_identity(pid)
    if identity is not None:
        return identity["state"] not in ("Z", "X")
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM


def read_boot_id():
    with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
        value = handle.read().strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{16,64}", value):
        raise RuntimeError("invalid kernel boot id")
    return value.lower()


def durable_replace_json(path, payload, mode=0o600):
    """Atomically replace a security-sensitive JSON file and fsync it."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    directory_stat = os.stat(directory, follow_symlinks=False)
    expected_uid = os.getuid() if hasattr(os, "getuid") else directory_stat.st_uid
    if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != expected_uid:
        raise RuntimeError("active-session ledger directory ownership is invalid")
    if directory_stat.st_mode & 0o022:
        raise RuntimeError("active-session ledger directory is writable by group or other")
    temp_path = "{}.{}.{}.tmp".format(path, os.getpid(), uuid.uuid4().hex)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(temp_path, flags, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            descriptor = None
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        directory_fd = os.open(directory, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def empty_active_session_ledger(boot_id=None, revision=0):
    return {
        "schema_version": ACTIVE_LEDGER_SCHEMA_VERSION,
        "boot_id": boot_id or read_boot_id(),
        "revision": int(revision),
        "updated_at": utc_now_text(),
        "sessions": {},
    }


def load_active_session_ledger(path):
    """Load and strictly validate the persistent active-session ledger."""
    try:
        ledger_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        raise RuntimeError("active-session ledger is missing: {}".format(path))
    expected_uid = os.getuid() if hasattr(os, "getuid") else ledger_stat.st_uid
    if not stat.S_ISREG(ledger_stat.st_mode) or ledger_stat.st_uid != expected_uid:
        raise RuntimeError("active-session ledger ownership is invalid")
    if ledger_stat.st_mode & 0o022:
        raise RuntimeError("active-session ledger is writable by group or other")
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RuntimeError("active-session ledger is unreadable: {}".format(exc))
    if not isinstance(payload, dict):
        raise RuntimeError("active-session ledger must be an object")
    if payload.get("schema_version") != ACTIVE_LEDGER_SCHEMA_VERSION:
        raise RuntimeError("unsupported active-session ledger schema")
    boot_id = payload.get("boot_id")
    if not isinstance(boot_id, str) or not boot_id:
        raise RuntimeError("active-session ledger boot id is invalid")
    revision = payload.get("revision")
    if not isinstance(revision, int) or revision < 0:
        raise RuntimeError("active-session ledger revision is invalid")
    sessions = payload.get("sessions")
    if not isinstance(sessions, dict):
        raise RuntimeError("active-session ledger sessions must be an object")
    seen_sessions = set()
    validated = {}
    required_positive = (
        "sid",
        "leader_pid",
        "leader_pgid",
        "leader_starttime_ticks",
        "wrapper_pid",
        "wrapper_starttime_ticks",
    )
    for ticket_id, record in sessions.items():
        if not isinstance(ticket_id, str) or not ticket_id or not isinstance(record, dict):
            raise RuntimeError("active-session ledger ticket is invalid")
        normalized = dict(record)
        for field in required_positive:
            value = normalized.get(field)
            if not isinstance(value, int) or value <= 1:
                raise RuntimeError("active-session ledger {} is invalid".format(field))
        uid = normalized.get("uid")
        if not isinstance(uid, int) or uid < 0:
            raise RuntimeError("active-session ledger uid is invalid")
        if not (
            normalized["sid"]
            == normalized["leader_pid"]
            == normalized["leader_pgid"]
        ):
            raise RuntimeError("active-session ledger leader identity is inconsistent")
        if normalized["sid"] in seen_sessions:
            raise RuntimeError("active-session ledger contains a duplicate session")
        seen_sessions.add(normalized["sid"])
        normalized["ticket_id"] = ticket_id
        validated[ticket_id] = normalized
    payload["sessions"] = validated
    return payload


def terminate_exact_process(pid, expected_uid, expected_starttime, wait_seconds=2.0):
    """Kill one exact process identity, or succeed if that identity is gone."""
    pid = int(pid or 0)
    if pid <= 1 or pid == os.getpid():
        return False
    identity = read_proc_identity(pid)
    if identity is None or identity["state"] in ("Z", "X"):
        return True
    if identity["uid"] != int(expected_uid) or identity["starttime"] != int(expected_starttime):
        return True
    try:
        os.kill(pid, signal.SIGSTOP)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    stop_deadline = monotonic_seconds() + max(0.0, float(wait_seconds))
    while True:
        current = read_proc_identity(pid)
        if current is None:
            try:
                os.kill(pid, signal.SIGCONT)
            except (ProcessLookupError, PermissionError):
                pass
            return False
        if current["state"] in ("Z", "X"):
            return True
        if current["uid"] != int(expected_uid) or current["starttime"] != int(expected_starttime):
            try:
                os.kill(pid, signal.SIGCONT)
            except (ProcessLookupError, PermissionError):
                pass
            return True
        if current["state"] in ("T", "t"):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                return True
            except PermissionError:
                try:
                    os.kill(pid, signal.SIGCONT)
                except (ProcessLookupError, PermissionError):
                    pass
                return False
            break
        if monotonic_seconds() >= stop_deadline:
            try:
                os.kill(pid, signal.SIGCONT)
            except (ProcessLookupError, PermissionError):
                pass
            return False
        time.sleep(0.01)
    kill_deadline = monotonic_seconds() + max(0.0, float(wait_seconds))
    while monotonic_seconds() < kill_deadline:
        current = read_proc_identity(pid)
        if current is None or current["state"] in ("Z", "X"):
            return True
        if current["uid"] != int(expected_uid) or current["starttime"] != int(expected_starttime):
            return True
        time.sleep(0.02)
    return False


def terminate_wrapper_and_session(
    wrapper_pid,
    wrapper_uid,
    wrapper_starttime,
    session_id,
    session_uid,
    session_starttime,
):
    # The permit socket has already been shut down.  Give a live wrapper's
    # monitor the first chance to perform its normal fail-closed cleanup; this
    # avoids racing two flock owners and then killing a wrapper that was about
    # to return the expected daemon-lost status.  A stopped or unresponsive
    # wrapper still falls through to exact termination below.
    wrapper_deadline = monotonic_seconds() + 5.0
    while wrapper_starttime is not None and monotonic_seconds() < wrapper_deadline:
        identity = read_proc_identity(wrapper_pid)
        if (
            identity is None
            or identity["state"] in ("Z", "X")
            or identity["uid"] != int(wrapper_uid)
            or identity["starttime"] != int(wrapper_starttime)
        ):
            break
        if identity["state"] in ("T", "t"):
            if not terminate_exact_process(wrapper_pid, wrapper_uid, wrapper_starttime):
                return False
            if not process_session_exists(session_id, session_uid, session_starttime):
                return True
            break
        if not process_session_exists(session_id, session_uid, session_starttime):
            return True
        time.sleep(0.05)

    terminated = terminate_process_session(session_id, session_uid, session_starttime)
    if not terminated and wrapper_starttime is not None:
        if not terminate_exact_process(wrapper_pid, wrapper_uid, wrapper_starttime):
            return False
        return terminate_process_session(session_id, session_uid, session_starttime)
    if not terminated or wrapper_starttime is None:
        return terminated
    identity = read_proc_identity(wrapper_pid)
    if (
        identity is None
        or identity["state"] in ("Z", "X")
        or identity["uid"] != int(wrapper_uid)
        or identity["starttime"] != int(wrapper_starttime)
    ):
        return True
    # A running wrapper has already seen the socket shutdown and can finish its
    # fail-closed handshake naturally.  Only a stopped wrapper needs an exact
    # kill; leaving it frozen would otherwise block migration scans forever.
    if identity["state"] in ("T", "t"):
        return terminate_exact_process(wrapper_pid, wrapper_uid, wrapper_starttime)
    return True


def process_group_members(pgid):
    if not pgid or int(pgid) <= 1:
        return []
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        return None
    members = []
    for entry in proc_entries:
        if not entry.isdigit():
            continue
        identity = read_proc_identity(int(entry))
        if identity and identity["pgrp"] == int(pgid) and identity["state"] not in ("Z", "X"):
            members.append(identity)
    return members


def process_group_exists(pgid):
    if not pgid or pgid <= 1 or not hasattr(os, "killpg"):
        return False
    members = process_group_members(pgid)
    if members is not None:
        return bool(members)
    try:
        os.killpg(pgid, 0)
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM


def process_session_members(session_id):
    """Return live /proc identities in one Linux session.

    A preserved guard may create additional process groups (GNU timeout does
    this by default), so the registered supervisor PGID is not a sufficient
    cleanup boundary.  The supervisor is started with start_new_session=True;
    its SID is therefore the stable boundary for the whole invocation.
    """
    if not session_id or int(session_id) <= 1:
        return []
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        return None
    members = []
    for entry in proc_entries:
        if not entry.isdigit():
            continue
        identity = read_proc_identity(int(entry))
        if identity is None:
            # ENOENT after the directory snapshot is a normal exit race.  If
            # the PID directory still exists, ownership/session cannot be
            # proven and cleanup must remain fail-closed.
            if os.path.exists("/proc/{}".format(entry)):
                return None
            continue
        if identity and identity["session"] == int(session_id) and identity["state"] not in ("Z", "X"):
            members.append(identity)
    return members


def validated_session_members(session_id, expected_uid=None, min_starttime=None):
    """Return owned session members, [] when empty, or None on ambiguity."""
    members = process_session_members(session_id)
    if members is None:
        return None
    for member in members:
        if expected_uid is not None and member["uid"] != int(expected_uid):
            return None
        if min_starttime is not None and member["starttime"] < int(min_starttime):
            return None
        if (
            min_starttime is not None
            and member["pid"] == int(session_id)
            and member["starttime"] != int(min_starttime)
        ):
            # The numeric SID/leader PID has been reused by another process.
            return None
    return members


def process_session_exists(session_id, expected_uid=None, min_starttime=None):
    members = validated_session_members(session_id, expected_uid, min_starttime)
    # Fail closed when /proc is unavailable or ownership is ambiguous.
    return members is None or bool(members)


def _signal_session_members(session_id, expected_uid, min_starttime, signum, exclude_pids):
    members = validated_session_members(session_id, expected_uid, min_starttime)
    if members is None:
        return None
    # Descendants first, session leader last.  Revalidate immutable identity
    # immediately before signaling to narrow the numeric-PID reuse window.
    members.sort(key=lambda item: item["pid"] == int(session_id))
    for member in members:
        if member["pid"] in exclude_pids:
            continue
        current = read_proc_identity(member["pid"])
        if current is None or current["state"] in ("Z", "X"):
            continue
        immutable_match = (
            current["session"] == int(session_id)
            and current["uid"] == member["uid"]
            and current["starttime"] == member["starttime"]
        )
        if not immutable_match:
            return None
        try:
            # This kernel/Python lacks pidfd.  Freeze first, then revalidate;
            # a numeric PID reused between the scan and SIGSTOP is only paused
            # and immediately resumed, never terminated as the old identity.
            os.kill(member["pid"], signal.SIGSTOP)
        except ProcessLookupError:
            continue
        except PermissionError:
            return None
        stop_deadline = monotonic_seconds() + 0.5
        while True:
            stopped = read_proc_identity(member["pid"])
            if stopped is None:
                try:
                    os.kill(member["pid"], signal.SIGCONT)
                except (ProcessLookupError, PermissionError):
                    pass
                return None
            if stopped["state"] in ("Z", "X"):
                break
            stopped_matches = (
                stopped["session"] == int(session_id)
                and stopped["uid"] == member["uid"]
                and stopped["starttime"] == member["starttime"]
            )
            if not stopped_matches:
                try:
                    os.kill(member["pid"], signal.SIGCONT)
                except (ProcessLookupError, PermissionError):
                    pass
                return None
            if stopped["state"] in ("T", "t"):
                try:
                    os.kill(member["pid"], signum)
                    if signum != signal.SIGKILL:
                        os.kill(member["pid"], signal.SIGCONT)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    try:
                        os.kill(member["pid"], signal.SIGCONT)
                    except (ProcessLookupError, PermissionError):
                        pass
                    return None
                break
            if monotonic_seconds() >= stop_deadline:
                # SIGSTOP may be pending behind uninterruptible sleep.  Leave
                # the ticket held and retry after the process becomes stoppable.
                try:
                    os.kill(member["pid"], signal.SIGCONT)
                except (ProcessLookupError, PermissionError):
                    pass
                return None
            time.sleep(0.01)
    return members


def _acquire_session_cleanup_lock(session_id, timeout_seconds=10.0):
    """Serialize every in-product cleaner for one numeric Linux session."""
    if fcntl is None:  # pragma: no cover - Windows unit-test fallback
        stripe = int(session_id) % SESSION_CLEANUP_LOCK_STRIPES
        with _THREAD_SESSION_LOCKS_GUARD:
            lock = _THREAD_SESSION_LOCKS.setdefault(stripe, threading.Lock())
        return lock if lock.acquire(timeout=max(0.0, float(timeout_seconds))) else None

    lock_directory = session_cleanup_lock_directory()
    try:
        directory_stat = os.stat(lock_directory, follow_symlinks=False)
        expected_uid = os.getuid() if hasattr(os, "getuid") else directory_stat.st_uid
        if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != expected_uid:
            return None
        if directory_stat.st_mode & 0o022:
            return None
        # Stripe files are pre-created by the daemon.  Cleaners only need a
        # read-only descriptor for local flock(2), so callers in a
        # ProtectSystem=strict sandbox do not need /var/lib write access.
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        stripe = int(session_id) % SESSION_CLEANUP_LOCK_STRIPES
        lock_path = os.path.join(lock_directory, "stripe-{:03d}.lock".format(stripe))
        descriptor = os.open(lock_path, flags)
        handle = os.fdopen(descriptor, "r")
        lock_stat = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != expected_uid
            or lock_stat.st_nlink != 1
        ):
            handle.close()
            return None
        deadline = monotonic_seconds() + max(0.0, float(timeout_seconds))
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    handle.close()
                    return None
                if monotonic_seconds() >= deadline:
                    handle.close()
                    return None
                time.sleep(0.05)
    except OSError:
        return None


def session_cleanup_lock_directory():
    if os.environ.get("SQL_GATE_TEST_MODE") == "1":
        test_directory = os.environ.get("SQL_GATE_SESSION_LOCK_DIRECTORY")
        if not test_directory:
            raise RuntimeError("test session lock directory is missing")
        return test_directory
    return DEFAULT_SESSION_LOCK_DIRECTORY


def prepare_session_cleanup_locks(lock_directory=None):
    """Create the fixed lock stripes before any client can enter the gate."""
    lock_directory = lock_directory or session_cleanup_lock_directory()
    if not os.path.isabs(lock_directory):
        raise RuntimeError("session cleanup lock directory must be absolute")
    os.makedirs(lock_directory, mode=0o700, exist_ok=True)
    directory_stat = os.stat(lock_directory, follow_symlinks=False)
    expected_uid = os.getuid() if hasattr(os, "getuid") else directory_stat.st_uid
    if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != expected_uid:
        raise RuntimeError("session cleanup lock directory ownership is invalid")
    if directory_stat.st_mode & 0o022:
        raise RuntimeError("session cleanup lock directory is writable by group or other")
    for stripe in range(SESSION_CLEANUP_LOCK_STRIPES):
        lock_path = os.path.join(lock_directory, "stripe-{:03d}.lock".format(stripe))
        flags = os.O_WRONLY | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            lock_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != expected_uid
                or lock_stat.st_nlink != 1
                or lock_stat.st_mode & 0o077
            ):
                raise RuntimeError("session cleanup lock file ownership is invalid")
        finally:
            os.close(descriptor)
    return lock_directory


def _release_session_cleanup_lock(lock):
    if lock is None:
        return
    if fcntl is None:  # pragma: no cover - Windows unit-test fallback
        lock.release()
        return
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    finally:
        lock.close()


def _terminate_process_session_locked(
    session_id,
    expected_uid=None,
    min_starttime=None,
    grace_seconds=2.0,
    kill_wait_seconds=2.0,
    exclude_pids=(),
):
    """Terminate every validated process in a supervisor-owned session.

    Returns True only after the session is confirmed empty.  Any /proc read
    failure, identity conflict, permission error, or unkillable process keeps
    the lease fail-closed.
    """
    session_id = int(session_id or 0)
    if session_id <= 1:
        return True
    excluded = {int(pid) for pid in exclude_pids if int(pid) > 0}
    own_pid = os.getpid()
    try:
        own_session = os.getsid(0)
    except (AttributeError, OSError):
        own_session = None
    if own_session == session_id and own_pid not in excluded:
        return False

    term_deadline = monotonic_seconds() + max(0.0, float(grace_seconds))
    while True:
        members = _signal_session_members(
            session_id,
            expected_uid,
            min_starttime,
            signal.SIGTERM,
            excluded,
        )
        if members is None:
            return False
        remaining = [item for item in members if item["pid"] not in excluded]
        if not remaining:
            return True
        if monotonic_seconds() >= term_deadline:
            break
        time.sleep(0.05)

    kill_deadline = monotonic_seconds() + max(0.0, float(kill_wait_seconds))
    while True:
        members = _signal_session_members(
            session_id,
            expected_uid,
            min_starttime,
            signal.SIGKILL,
            excluded,
        )
        if members is None:
            return False
        remaining = [item for item in members if item["pid"] not in excluded]
        if not remaining:
            return True
        if monotonic_seconds() >= kill_deadline:
            return False
        time.sleep(0.05)


def terminate_process_session(
    session_id,
    expected_uid=None,
    min_starttime=None,
    grace_seconds=2.0,
    kill_wait_seconds=2.0,
    exclude_pids=(),
):
    """Terminate a session while holding its cross-process cleaner lock.

    Production kernel 4.18 has no pidfd support.  Serializing all wrapper,
    supervisor, and daemon cleanup for one SID closes the in-product
    revalidate-to-signal race; identity validation still fails closed if an
    unrelated external actor replaces a PID.
    """
    session_id = int(session_id or 0)
    if session_id <= 1:
        return True
    cleanup_lock = _acquire_session_cleanup_lock(session_id)
    if cleanup_lock is None:
        return False
    try:
        return _terminate_process_session_locked(
            session_id,
            expected_uid,
            min_starttime,
            grace_seconds,
            kill_wait_seconds,
            exclude_pids,
        )
    finally:
        _release_session_cleanup_lock(cleanup_lock)


def initialize_empty_active_session_ledger(path):
    if os.path.lexists(path):
        raise RuntimeError("active-session ledger already exists: {}".format(path))
    payload = empty_active_session_ledger(revision=0)
    durable_replace_json(path, payload)
    return payload


def reconcile_active_session_ledger(path):
    """Clean every same-boot recorded session before opening the gate socket."""
    ledger = load_active_session_ledger(path)
    current_boot_id = read_boot_id()
    revision = int(ledger["revision"])
    sessions = dict(ledger["sessions"])
    if ledger["boot_id"] != current_boot_id:
        payload = empty_active_session_ledger(current_boot_id, revision + 1)
        durable_replace_json(path, payload)
        print(json.dumps({
            "event": "startup_ledger_previous_boot_cleared",
            "session_count": len(sessions),
        }, sort_keys=True), flush=True)
        return payload
    if not sessions:
        return ledger

    def clean_record(record):
        wrapper_stopped = terminate_exact_process(
            record["wrapper_pid"],
            record["uid"],
            record["wrapper_starttime_ticks"],
        )
        if not wrapper_stopped:
            return False
        if not process_session_exists(
            record["sid"],
            record["uid"],
            record["leader_starttime_ticks"],
        ):
            return True
        terminated = terminate_process_session(
            record["sid"],
            record["uid"],
            record["leader_starttime_ticks"],
        )
        return terminated and not process_session_exists(
            record["sid"],
            record["uid"],
            record["leader_starttime_ticks"],
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(sessions), 32)) as executor:
        futures = {
            ticket_id: executor.submit(clean_record, record)
            for ticket_id, record in sessions.items()
        }
        results = {
            ticket_id: future.result()
            for ticket_id, future in futures.items()
        }
    remaining = {
        ticket_id: record
        for ticket_id, record in sessions.items()
        if not results.get(ticket_id)
    }
    if remaining != sessions:
        ledger = {
            "schema_version": ACTIVE_LEDGER_SCHEMA_VERSION,
            "boot_id": current_boot_id,
            "revision": revision + 1,
            "updated_at": utc_now_text(),
            "sessions": remaining,
        }
        durable_replace_json(path, ledger)
    print(json.dumps({
        "event": "startup_ledger_reconciled",
        "cleaned": len(sessions) - len(remaining),
        "remaining": len(remaining),
    }, sort_keys=True), flush=True)
    if remaining:
        raise RuntimeError("active-session reconciliation left {} unsafe session(s)".format(len(remaining)))
    return ledger


def terminate_process_groups(pgids, grace_seconds=2.0, kill_wait_seconds=2.0):
    own_group = os.getpgrp() if hasattr(os, "getpgrp") else None
    targets = sorted(set(
        int(pgid) for pgid in pgids
        if pgid and int(pgid) > 1 and hasattr(os, "killpg") and int(pgid) != own_group
    ))
    for pgid in targets:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = monotonic_seconds() + max(0.0, float(grace_seconds))
    while monotonic_seconds() < deadline:
        if not any(process_group_exists(pgid) for pgid in targets):
            return []
        time.sleep(0.05)
    for pgid in targets:
        if not process_group_exists(pgid):
            continue
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    kill_deadline = monotonic_seconds() + max(0.0, float(kill_wait_seconds))
    while monotonic_seconds() < kill_deadline:
        remaining = [pgid for pgid in targets if process_group_exists(pgid)]
        if not remaining:
            return []
        time.sleep(0.05)
    return [pgid for pgid in targets if process_group_exists(pgid)]


def terminate_process_group(pgid, grace_seconds=2.0):
    return not terminate_process_groups([pgid], grace_seconds=grace_seconds)


class Ticket(object):
    def __init__(
        self,
        job,
        pool,
        peer_pid,
        peer_uid,
        peer_gid,
        wait_timeout,
        connection,
        peer_starttime=None,
    ):
        self.ticket_id = uuid.uuid4().hex
        self.job = safe_label(job)
        self.pool = safe_label(pool)
        self.peer_pid = int(peer_pid)
        self.peer_uid = int(peer_uid)
        self.peer_gid = int(peer_gid)
        self.peer_starttime = int(peer_starttime) if peer_starttime is not None else None
        self.queued_at = monotonic_seconds()
        self.queued_at_text = utc_now_text()
        self.deadline = self.queued_at + float(wait_timeout)
        self.granted_at = None
        self.active_deadline = None
        self.child_pid = None
        self.child_pgid = None
        self.child_starttime = None
        self.child_uid = None
        self.child_session = None
        self.child_registered_at = None
        self.terminating = False
        self.cleanup_failed = False
        self.connection = connection


class GateCoordinator(object):
    def __init__(
        self,
        capacity,
        max_queue,
        queue_timeout,
        pool,
        state_path,
        active_timeout=1200,
        ledger_path=None,
        ledger_revision=0,
        boot_id=None,
    ):
        self.capacity = int(capacity)
        self.max_queue = int(max_queue)
        self.queue_timeout = float(queue_timeout)
        self.active_timeout = float(active_timeout)
        self.pool = safe_label(pool)
        self.state_path = state_path
        self.ledger_path = ledger_path
        self.ledger_revision = int(ledger_revision)
        self.boot_id = boot_id
        self._last_ledger_signature = None
        self.started_at = utc_now_text()
        self.condition = threading.Condition(threading.RLock())
        self.queue = collections.deque()
        self.active = {}
        self.stopping = False
        self.counters = collections.Counter()
        self.job_metrics = {}
        self.max_observed_active = 0
        self.max_observed_queued = 0
        self.write_state()

    def _ledger_projection_unlocked(self, exclude_ticket_id=None):
        sessions = {}
        for ticket_id, ticket in sorted(self.active.items()):
            if ticket_id == exclude_ticket_id or not ticket.child_session:
                continue
            if ticket.peer_starttime is None or ticket.child_starttime is None:
                raise RuntimeError("active session is missing a durable process identity")
            sessions[ticket_id] = {
                "ticket_id": ticket_id,
                "sid": int(ticket.child_session),
                "leader_pid": int(ticket.child_pid),
                "leader_pgid": int(ticket.child_pgid),
                "leader_starttime_ticks": int(ticket.child_starttime),
                "uid": int(ticket.child_uid),
                "wrapper_pid": int(ticket.peer_pid),
                "wrapper_starttime_ticks": int(ticket.peer_starttime),
                "registered_at": ticket.child_registered_at or utc_now_text(),
                "job": ticket.job,
            }
        return sessions

    def _persist_ledger_unlocked(self, exclude_ticket_id=None):
        if not self.ledger_path:
            return
        sessions = self._ledger_projection_unlocked(exclude_ticket_id=exclude_ticket_id)
        signature = json.dumps(sessions, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if signature == self._last_ledger_signature:
            return
        payload = {
            "schema_version": ACTIVE_LEDGER_SCHEMA_VERSION,
            "boot_id": self.boot_id or read_boot_id(),
            "revision": self.ledger_revision + 1,
            "updated_at": utc_now_text(),
            "sessions": sessions,
        }
        durable_replace_json(self.ledger_path, payload)
        self.ledger_revision = payload["revision"]
        self._last_ledger_signature = signature

    def _job_metrics(self, job):
        return self.job_metrics.setdefault(job, collections.Counter())

    def _snapshot_unlocked(self):
        now = monotonic_seconds()
        queued_jobs = collections.Counter(ticket.job for ticket in self.queue)
        active_jobs = collections.Counter(ticket.job for ticket in self.active.values())
        oldest_wait = max([0.0] + [now - ticket.queued_at for ticket in self.queue])
        job_metrics = {}
        for job, metrics in sorted(self.job_metrics.items()):
            job_metrics[job] = {
                "granted": int(metrics.get("granted", 0)),
                "released": int(metrics.get("released", 0)),
                "wait_timeout": int(metrics.get("wait_timeout", 0)),
                "disconnect": int(metrics.get("disconnect", 0)),
                "wait_seconds_total": round(float(metrics.get("wait_seconds_total", 0.0)), 3),
                "wait_seconds_max": round(float(metrics.get("wait_seconds_max", 0.0)), 3),
            }
        return {
            "version": VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "status": "stopping" if self.stopping else "ok",
            "pool": self.pool,
            "capacity": self.capacity,
            "active_timeout_seconds": self.active_timeout,
            "in_use": len(self.active),
            "queued": len(self.queue),
            "available": max(0, self.capacity - len(self.active)),
            "oldest_wait_seconds": round(oldest_wait, 3),
            "max_observed_active": self.max_observed_active,
            "max_observed_queued": self.max_observed_queued,
            "active_jobs": dict(sorted(active_jobs.items())),
            "queued_jobs": dict(sorted(queued_jobs.items())),
            "counters": {key: int(value) for key, value in sorted(self.counters.items())},
            "job_metrics": job_metrics,
            "started_at": self.started_at,
            "updated_at": utc_now_text(),
        }

    def snapshot(self):
        with self.condition:
            if self._reap_cleanup_failures_unlocked():
                self.write_state()
                self.condition.notify_all()
            return self._snapshot_unlocked()

    def _reap_cleanup_failures_unlocked(self):
        recovered = 0
        for ticket in list(self.active.values()):
            if not ticket.cleanup_failed or not ticket.child_session:
                continue
            if process_session_exists(
                ticket.child_session,
                ticket.child_uid,
                ticket.child_starttime,
            ):
                continue
            self._clear_ticket_child_unlocked(ticket)
            self.active.pop(ticket.ticket_id, None)
            self.counters["released"] += 1
            self.counters["released_cleanup_recovered"] += 1
            self._job_metrics(ticket.job)["released"] += 1
            recovered += 1
        if recovered:
            self.counters["child_cleanup_recovered"] += recovered
        return recovered

    def write_state(self):
        with self.condition:
            self._persist_ledger_unlocked()
            if not self.state_path:
                return
            payload = self._snapshot_unlocked()
            directory = os.path.dirname(self.state_path) or "."
            os.makedirs(directory, mode=0o750, exist_ok=True)
            temp_path = "{}.{}.tmp".format(self.state_path, os.getpid())
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
                handle.write("\n")
            os.chmod(temp_path, 0o640)
            os.replace(temp_path, self.state_path)

    def register(self, job, pool, peer_pid, peer_uid, peer_gid, requested_wait, connection):
        wait_timeout = min(float(requested_wait), self.queue_timeout)
        peer_identity = read_proc_identity(peer_pid)
        if (
            peer_identity is None
            or peer_identity["state"] in ("Z", "X")
            or peer_identity["uid"] != int(peer_uid)
        ):
            raise GateProtocolError("wrapper process identity is unavailable")
        ticket = Ticket(
            job,
            pool,
            peer_pid,
            peer_uid,
            peer_gid,
            wait_timeout,
            connection,
            peer_starttime=peer_identity["starttime"],
        )
        with self.condition:
            self._reap_cleanup_failures_unlocked()
            if self.stopping:
                raise GateStopping("gate is stopping")
            if ticket.pool != self.pool:
                raise GateProtocolError("unknown pool")
            if len(self.queue) >= self.max_queue:
                self.counters["queue_full"] += 1
                self.write_state()
                raise GateProtocolError("queue is full")
            self.queue.append(ticket)
            self.counters["accepted"] += 1
            self.max_observed_queued = max(self.max_observed_queued, len(self.queue))
            self.write_state()
            self.condition.notify_all()
        return ticket

    def _remove_queued_unlocked(self, ticket):
        try:
            self.queue.remove(ticket)
            return True
        except ValueError:
            return False

    def wait_for_grant(self, ticket):
        with self.condition:
            while True:
                if self._reap_cleanup_failures_unlocked():
                    self.write_state()
                    self.condition.notify_all()
                if self.stopping:
                    self._remove_queued_unlocked(ticket)
                    self.write_state()
                    raise GateStopping("gate is stopping")
                if peer_is_closed(ticket.connection):
                    self._remove_queued_unlocked(ticket)
                    self.counters["queue_disconnect"] += 1
                    self._job_metrics(ticket.job)["disconnect"] += 1
                    self.write_state()
                    self.condition.notify_all()
                    raise GateClientDisconnected("client disconnected while queued")
                now = monotonic_seconds()
                if now >= ticket.deadline:
                    self._remove_queued_unlocked(ticket)
                    self.counters["wait_timeout"] += 1
                    self._job_metrics(ticket.job)["wait_timeout"] += 1
                    self.write_state()
                    self.condition.notify_all()
                    raise GateQueueTimeout("queue wait timeout")
                if self.queue and self.queue[0] is ticket and len(self.active) < self.capacity:
                    self.queue.popleft()
                    ticket.granted_at = now
                    ticket.active_deadline = now + self.active_timeout
                    self.active[ticket.ticket_id] = ticket
                    wait_seconds = now - ticket.queued_at
                    metrics = self._job_metrics(ticket.job)
                    metrics["granted"] += 1
                    metrics["wait_seconds_total"] += wait_seconds
                    metrics["wait_seconds_max"] = max(metrics.get("wait_seconds_max", 0.0), wait_seconds)
                    self.counters["granted"] += 1
                    self.max_observed_active = max(self.max_observed_active, len(self.active))
                    self.write_state()
                    return wait_seconds
                remaining = max(0.01, ticket.deadline - now)
                self.condition.wait(timeout=min(0.25, remaining))

    def set_child(self, ticket, child_pid, child_pgid):
        with self.condition:
            if ticket.ticket_id not in self.active:
                return
            child_pid = int(child_pid or 0)
            child_pgid = int(child_pgid or 0)
            if self.stopping and (child_pid != 0 or child_pgid != 0):
                raise GateStopping("gate is stopping")
            if child_pid == 0 and child_pgid == 0:
                if ticket.child_session and process_session_exists(
                    ticket.child_session,
                    ticket.child_uid,
                    ticket.child_starttime,
                ):
                    raise GateProtocolError("cannot clear a live child process session")
                self._clear_ticket_child_unlocked(ticket)
                self.write_state()
                return
            if ticket.child_session:
                raise GateProtocolError("a child process session is already registered")
            if child_pid <= 1 or child_pid != child_pgid:
                raise GateProtocolError("child pid and pgid must match and be greater than 1")
            peer_identity = read_proc_identity(ticket.peer_pid)
            if (
                peer_identity is None
                or peer_identity["state"] in ("Z", "X")
                or peer_identity["uid"] != ticket.peer_uid
                or peer_identity["starttime"] != ticket.peer_starttime
            ):
                raise GateProtocolError("wrapper process identity changed")
            identity = read_proc_identity(child_pid)
            if identity is None or identity["state"] in ("Z", "X"):
                raise GateProtocolError("child process is not running")
            if identity["pgrp"] != child_pgid:
                raise GateProtocolError("child process group does not match")
            if identity["session"] != child_pid:
                raise GateProtocolError("child must be a process-session leader")
            if identity["uid"] != ticket.peer_uid:
                raise GateProtocolError("child uid does not match peer uid")
            if identity["ppid"] != ticket.peer_pid:
                raise GateProtocolError("child is not a direct descendant of the wrapper")
            ticket.child_pid = child_pid
            ticket.child_pgid = child_pgid
            ticket.child_starttime = identity["starttime"]
            ticket.child_uid = identity["uid"]
            ticket.child_session = identity["session"]
            ticket.child_registered_at = utc_now_text()
            ticket.cleanup_failed = False
            self.write_state()

    def _clear_ticket_child_unlocked(self, ticket):
        # The durable ledger must forget the session only after callers have
        # proven it empty, and before the in-memory identity is discarded.
        self._persist_ledger_unlocked(exclude_ticket_id=ticket.ticket_id)
        ticket.child_pid = None
        ticket.child_pgid = None
        ticket.child_starttime = None
        ticket.child_uid = None
        ticket.child_session = None
        ticket.child_registered_at = None
        ticket.terminating = False
        ticket.cleanup_failed = False

    def _ticket_child_members_unlocked(self, ticket):
        if not ticket.child_pid or not ticket.child_session or ticket.child_starttime is None:
            return []
        members = process_session_members(ticket.child_session)
        if members is None:
            return None
        if not members:
            return []
        if all(
            member["uid"] == ticket.child_uid
            and member["session"] == ticket.child_session
            and member["starttime"] >= ticket.child_starttime
            and (
                member["pid"] != ticket.child_pid
                or member["starttime"] == ticket.child_starttime
            )
            for member in members
        ):
            return members
        return None

    def _ticket_child_identity_unlocked(self, ticket):
        members = self._ticket_child_members_unlocked(ticket)
        if not members:
            return None
        identity = read_proc_identity(ticket.child_pid)
        if identity is not None and identity["state"] not in ("Z", "X"):
            if (
                identity["pgrp"] == ticket.child_pgid
                and identity["session"] == ticket.child_session
                and identity["starttime"] == ticket.child_starttime
                and identity["uid"] == ticket.child_uid
            ):
                return identity
            return None
        return members[0]

    def terminate_ticket_child(self, ticket, reason):
        with self.condition:
            if ticket.terminating:
                return False
            members = self._ticket_child_members_unlocked(ticket)
            if members is None:
                ticket.cleanup_failed = True
                self.counters["child_cleanup_identity_conflict"] += 1
                self.write_state()
                return False
            if not members:
                self._clear_ticket_child_unlocked(ticket)
                self.write_state()
                return True
            ticket.terminating = True
            child_session = ticket.child_session
            child_uid = ticket.child_uid
            child_starttime = ticket.child_starttime
            peer_pid = ticket.peer_pid
            peer_uid = ticket.peer_uid
            peer_starttime = ticket.peer_starttime
        terminated = terminate_process_session(
            child_session,
            child_uid,
            child_starttime,
        )
        # Usually the session cleaner can finish without killing the wrapper.
        # If a stopped wrapper owns the stripe flock, terminate that exact
        # identity and retry.  The active watchdog also removes the stopped
        # wrapper after its SQL session is gone.
        if not terminated:
            wrapper_stopped = peer_starttime is None or terminate_exact_process(
                peer_pid,
                peer_uid,
                peer_starttime,
            )
            if wrapper_stopped:
                terminated = terminate_process_session(
                    child_session,
                    child_uid,
                    child_starttime,
                )
        elif reason == "active_lease_timeout" and peer_starttime is not None:
            terminated = terminate_exact_process(peer_pid, peer_uid, peer_starttime)
        with self.condition:
            ticket.terminating = False
            if not terminated:
                ticket.cleanup_failed = True
                self.counters["child_cleanup_failed"] += 1
                self.write_state()
                return False
            self._clear_ticket_child_unlocked(ticket)
            self.counters["child_terminated_{}".format(safe_label(reason, limit=48))] += 1
            self.write_state()
            return True

    def release(self, ticket, reason="client_closed"):
        with self.condition:
            if ticket.child_session and process_session_exists(
                ticket.child_session,
                ticket.child_uid,
                ticket.child_starttime,
            ):
                ticket.cleanup_failed = True
                self.counters["release_blocked_live_session"] += 1
                self.write_state()
                return False
            if ticket.child_session:
                self._clear_ticket_child_unlocked(ticket)
            removed = self.active.pop(ticket.ticket_id, None)
            if removed is None:
                self._remove_queued_unlocked(ticket)
                return False
            self.counters["released"] += 1
            self.counters["released_{}".format(safe_label(reason, limit=48))] += 1
            self._job_metrics(ticket.job)["released"] += 1
            self.write_state()
            self.condition.notify_all()
            return True

    def stop(self):
        identities = []
        release_without_child = []
        with self.condition:
            if self.stopping:
                return
            self.stopping = True
            self.counters["shutdown"] += 1
            connections = [ticket.connection for ticket in list(self.queue) + list(self.active.values())]
            active_tickets = list(self.active.values())
            for ticket in active_tickets:
                members = self._ticket_child_members_unlocked(ticket)
                if members is None:
                    ticket.cleanup_failed = True
                    self.counters["child_cleanup_identity_conflict_daemon_stop"] += 1
                elif members:
                    owns_cleanup = not ticket.terminating
                    if owns_cleanup:
                        ticket.terminating = True
                    identities.append((
                        ticket,
                        ticket.child_session,
                        ticket.child_uid,
                        ticket.child_starttime,
                        ticket.peer_pid,
                        ticket.peer_uid,
                        ticket.peer_starttime,
                        owns_cleanup,
                    ))
                else:
                    self._clear_ticket_child_unlocked(ticket)
                    release_without_child.append(ticket)
            self.write_state()
            self.condition.notify_all()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        cleanup_results = {}
        if identities:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(identities)) as executor:
                futures = {
                    ticket.ticket_id: executor.submit(
                        terminate_wrapper_and_session,
                        peer_pid,
                        peer_uid,
                        peer_starttime,
                        session_id,
                        child_uid,
                        child_starttime,
                    )
                    for (
                        ticket,
                        session_id,
                        child_uid,
                        child_starttime,
                        peer_pid,
                        peer_uid,
                        peer_starttime,
                        _owns_cleanup,
                    ) in identities
                }
                cleanup_results = {
                    ticket_id: future.result()
                    for ticket_id, future in futures.items()
                }
        release_after_cleanup = list(release_without_child)
        with self.condition:
            for (
                ticket,
                _session_id,
                _child_uid,
                _child_starttime,
                _peer_pid,
                _peer_uid,
                _peer_starttime,
                owns_cleanup,
            ) in identities:
                if not cleanup_results.get(ticket.ticket_id):
                    if owns_cleanup:
                        ticket.terminating = False
                    ticket.cleanup_failed = True
                    self.counters["child_cleanup_failed_daemon_stop"] += 1
                    continue
                self._clear_ticket_child_unlocked(ticket)
                if owns_cleanup:
                    self.counters["child_terminated_daemon_stop"] += 1
                release_after_cleanup.append(ticket)
            self.write_state()
        for ticket in release_after_cleanup:
            self.release(ticket, "daemon_stop")

        # A failed or ambiguous cleanup must keep the daemon alive and the
        # capacity fail-closed.  Retry until every active session is confirmed
        # empty; systemd is configured not to SIGKILL this safety owner.
        while True:
            with self.condition:
                if self._reap_cleanup_failures_unlocked():
                    self.write_state()
                    self.condition.notify_all()
                pending = [
                    ticket for ticket in self.active.values()
                    if ticket.child_session and not ticket.terminating
                ]
                no_child = [
                    ticket for ticket in self.active.values()
                    if not ticket.child_session
                ]
                if not pending and not no_child and not self.active:
                    break
                for ticket in pending:
                    ticket.terminating = True
            for ticket in no_child:
                self.release(ticket, "daemon_stop")
            if not pending:
                time.sleep(0.1)
                continue
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(pending)) as executor:
                futures = {
                    ticket.ticket_id: executor.submit(
                        terminate_wrapper_and_session,
                        ticket.peer_pid,
                        ticket.peer_uid,
                        ticket.peer_starttime,
                        ticket.child_session,
                        ticket.child_uid,
                        ticket.child_starttime,
                    )
                    for ticket in pending
                }
                retry_results = {
                    ticket_id: future.result()
                    for ticket_id, future in futures.items()
                }
            retry_releases = []
            with self.condition:
                for ticket in pending:
                    ticket.terminating = False
                    if retry_results.get(ticket.ticket_id):
                        self._clear_ticket_child_unlocked(ticket)
                        self.counters["child_terminated_daemon_stop_retry"] += 1
                        retry_releases.append(ticket)
                    else:
                        ticket.cleanup_failed = True
                self.write_state()
            for ticket in retry_releases:
                self.release(ticket, "daemon_stop")
            if self.active:
                time.sleep(0.25)


class GateRequestHandler(socketserver.BaseRequestHandler):
    def _error(self, code, message):
        try:
            send_json_line(self.request, {
                "protocol_version": PROTOCOL_VERSION,
                "status": "error",
                "code": code,
                "message": message,
            })
        except OSError:
            pass

    def handle(self):
        coordinator = self.server.coordinator
        ticket = None
        peer_pid, peer_uid, peer_gid = get_peer_credentials(self.request)
        if peer_uid not in self.server.allowed_uids:
            self._error("permission_denied", "peer uid is not allowed")
            return
        try:
            self.request.settimeout(5.0)
            message = recv_json_line(self.request)
            if int(message.get("protocol_version", 0)) != PROTOCOL_VERSION:
                raise GateProtocolError("unsupported protocol version")
            operation = message.get("op")
            if operation in ("status", "health", "metrics"):
                send_json_line(self.request, coordinator.snapshot())
                return
            if operation != "acquire":
                raise GateProtocolError("unknown operation")
            requested_wait = parse_positive_int(
                message.get("wait_timeout_seconds", int(coordinator.queue_timeout)),
                "wait_timeout_seconds",
                minimum=1,
                maximum=int(coordinator.queue_timeout),
            )
            ticket = coordinator.register(
                job=message.get("job"),
                pool=message.get("pool"),
                peer_pid=peer_pid,
                peer_uid=peer_uid,
                peer_gid=peer_gid,
                requested_wait=requested_wait,
                connection=self.request,
            )
            wait_seconds = coordinator.wait_for_grant(ticket)
            send_json_line(self.request, {
                "protocol_version": PROTOCOL_VERSION,
                "status": "granted",
                "ticket_id": ticket.ticket_id,
                "pool": ticket.pool,
                "wait_seconds": round(wait_seconds, 3),
                "capacity": coordinator.capacity,
            })
            self.request.settimeout(0.5)
            buffer = bytearray()
            reason = "client_closed"
            while True:
                try:
                    data = self.request.recv(1024)
                except socket.timeout:
                    if ticket.active_deadline and monotonic_seconds() >= ticket.active_deadline:
                        reason = "active_lease_timeout"
                        with coordinator.condition:
                            coordinator.counters["active_lease_timeout"] += 1
                            coordinator.write_state()
                        break
                    peer_identity = read_proc_identity(ticket.peer_pid)
                    if (
                        peer_identity is None
                        or peer_identity["state"] in ("Z", "X")
                        or peer_identity["uid"] != ticket.peer_uid
                        or peer_identity["starttime"] != ticket.peer_starttime
                    ):
                        reason = "wrapper_disappeared"
                        break
                    continue
                if not data:
                    reason = "client_eof"
                    break
                buffer.extend(data)
                if len(buffer) > MAX_MESSAGE_BYTES and b"\n" not in buffer:
                    reason = "control_message_too_large"
                    raise GateProtocolError("control message too large")
                while b"\n" in buffer:
                    raw, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    if len(raw) > MAX_MESSAGE_BYTES:
                        reason = "control_message_too_large"
                        raise GateProtocolError("control message too large")
                    try:
                        control = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        reason = "invalid_control_message"
                        raise GateProtocolError("invalid control message")
                    if not isinstance(control, dict):
                        reason = "invalid_control_message"
                        raise GateProtocolError("control message must be an object")
                    if control.get("op") == "child":
                        coordinator.set_child(ticket, control.get("pid"), control.get("pgid"))
                        if control.get("pid") or control.get("pgid"):
                            send_json_line(self.request, {
                                "protocol_version": PROTOCOL_VERSION,
                                "status": "child_registered",
                            })
                        else:
                            send_json_line(self.request, {
                                "protocol_version": PROTOCOL_VERSION,
                                "status": "child_cleared",
                            })
                    elif control.get("op") == "release":
                        reason = safe_label(control.get("reason"), "client_release", 48)
                        return
                    else:
                        reason = "invalid_control_operation"
                        raise GateProtocolError("unknown control operation")
        except GateQueueTimeout as exc:
            self._error("wait_timeout", str(exc))
        except GateClientDisconnected:
            pass
        except GateStopping as exc:
            self._error("stopping", str(exc))
        except (GateProtocolError, ValueError) as exc:
            self._error("invalid_request", str(exc))
        except (OSError, RuntimeError) as exc:
            self._error("internal_error", str(exc))
        finally:
            if ticket is not None:
                cleanup_complete = True
                if ticket.granted_at is not None:
                    cleanup_complete = coordinator.terminate_ticket_child(
                        ticket,
                        locals().get("reason", "handler_exit"),
                    )
                if cleanup_complete:
                    coordinator.release(ticket, locals().get("reason", "handler_exit"))


if hasattr(socketserver, "UnixStreamServer"):
    class ThreadingUnixGateServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
        block_on_close = False
        request_queue_size = 128
else:  # pragma: no cover - production target is Linux
    class ThreadingUnixGateServer(object):
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("AF_UNIX stream servers are not supported on this platform")


def request_once(socket_path, payload, timeout=5.0, keep_open=False):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(float(timeout))
        client.connect(socket_path)
        send_json_line(client, payload)
        response = recv_json_line(client)
        if keep_open:
            client.settimeout(None)
            return client, response
        client.close()
        return None, response
    except Exception:
        client.close()
        raise


def acquire_permit(socket_path, pool, job, wait_timeout_seconds):
    return request_once(
        socket_path,
        {
            "protocol_version": PROTOCOL_VERSION,
            "op": "acquire",
            "pool": pool,
            "job": job,
            "wait_timeout_seconds": int(wait_timeout_seconds),
        },
        timeout=float(wait_timeout_seconds) + 10.0,
        keep_open=True,
    )


def status_request(socket_path, timeout=5.0):
    _, response = request_once(
        socket_path,
        {"protocol_version": PROTOCOL_VERSION, "op": "status"},
        timeout=timeout,
        keep_open=False,
    )
    return response


def acquire_daemon_lock(path):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o750, exist_ok=True)
    handle = open(path, "a+")
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("another gate daemon holds {}".format(path))
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()) + "\n")
    handle.flush()
    return handle


def serve(args):
    socket_path = args.socket_path
    directory = os.path.dirname(socket_path) or "."
    os.makedirs(directory, mode=0o750, exist_ok=True)
    lock_handle = acquire_daemon_lock(args.lock_path)
    try:
        prepare_session_cleanup_locks()
        reconciled_ledger = reconcile_active_session_ledger(args.ledger_path)
    except Exception:
        lock_handle.close()
        raise
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    coordinator = GateCoordinator(
        args.capacity,
        args.max_queue,
        args.queue_timeout,
        args.pool,
        args.state_path,
        active_timeout=args.active_timeout,
        ledger_path=args.ledger_path,
        ledger_revision=reconciled_ledger["revision"],
        boot_id=reconciled_ledger["boot_id"],
    )
    server = ThreadingUnixGateServer(socket_path, GateRequestHandler)
    server.coordinator = coordinator
    server.allowed_uids = set(args.allowed_uid)
    os.chmod(socket_path, args.socket_mode)
    stopping = threading.Event()

    def stop_server(signum, _frame):
        if stopping.is_set():
            return
        stopping.set()

        def drain_and_shutdown():
            coordinator.stop()
            server.shutdown()

        threading.Thread(
            target=drain_and_shutdown,
            name="gate-drain-shutdown",
            daemon=False,
        ).start()

    previous_handlers = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, stop_server)
    print(json.dumps({
        "event": "gate_started",
        "version": VERSION,
        "socket": socket_path,
        "pool": coordinator.pool,
        "capacity": coordinator.capacity,
        "max_queue": coordinator.max_queue,
        "queue_timeout": coordinator.queue_timeout,
        "active_timeout": coordinator.active_timeout,
    }, sort_keys=True), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        coordinator.stop()
        server.server_close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        lock_handle.close()
    return 0


def add_common_socket_argument(parser):
    parser.add_argument("--socket-path", default=os.environ.get("SQL_GATE_SOCKET", DEFAULT_SOCKET_PATH))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the gate daemon")
    add_common_socket_argument(serve_parser)
    serve_parser.add_argument("--state-path", default=os.environ.get("SQL_GATE_STATE_PATH", DEFAULT_STATE_PATH))
    serve_parser.add_argument("--lock-path", default=os.environ.get("SQL_GATE_LOCK_PATH", DEFAULT_LOCK_PATH))
    serve_parser.add_argument(
        "--ledger-path",
        default=os.environ.get("SQL_GATE_ACTIVE_LEDGER_PATH", DEFAULT_ACTIVE_LEDGER_PATH),
    )
    serve_parser.add_argument("--pool", default=os.environ.get("SQL_GATE_POOL", DEFAULT_POOL))
    serve_parser.add_argument("--capacity", type=int, default=int(os.environ.get("SQL_GATE_CAPACITY", "14")))
    serve_parser.add_argument("--max-queue", type=int, default=int(os.environ.get("SQL_GATE_MAX_QUEUE", "100")))
    serve_parser.add_argument("--queue-timeout", type=int, default=int(os.environ.get("SQL_GATE_QUEUE_TIMEOUT_SECONDS", "300")))
    serve_parser.add_argument("--active-timeout", type=int, default=int(os.environ.get("SQL_GATE_ACTIVE_LEASE_TIMEOUT_SECONDS", "1200")))
    serve_parser.add_argument("--allowed-uid", type=int, action="append", default=None)
    serve_parser.add_argument("--socket-mode", type=lambda value: int(value, 8), default=int(os.environ.get("SQL_GATE_SOCKET_MODE", "0660"), 8))

    status_parser = subparsers.add_parser("status", help="Read live daemon status")
    add_common_socket_argument(status_parser)
    status_parser.add_argument("--timeout", type=float, default=5.0)
    status_parser.add_argument("--wait-seconds", type=float, default=0.0)

    init_parser = subparsers.add_parser(
        "init-ledger",
        help="Create the one-time empty active-session ledger while the daemon is stopped",
    )
    init_parser.add_argument(
        "--ledger-path",
        default=os.environ.get("SQL_GATE_ACTIVE_LEDGER_PATH", DEFAULT_ACTIVE_LEDGER_PATH),
    )
    init_parser.add_argument("--lock-path", default=os.environ.get("SQL_GATE_LOCK_PATH", DEFAULT_LOCK_PATH))

    args = parser.parse_args(argv)
    if args.command == "serve":
        args.capacity = parse_positive_int(args.capacity, "capacity", 1, 100)
        args.max_queue = parse_positive_int(args.max_queue, "max_queue", 1, 10000)
        args.queue_timeout = parse_positive_int(args.queue_timeout, "queue_timeout", 1, 86400)
        args.active_timeout = parse_positive_int(args.active_timeout, "active_timeout", 1, 86400)
        if args.allowed_uid is None:
            args.allowed_uid = [0]
    return args


def status_main(args):
    deadline = monotonic_seconds() + max(0.0, args.wait_seconds)
    last_error = None
    while True:
        try:
            response = status_request(args.socket_path, timeout=args.timeout)
            print(json.dumps(response, ensure_ascii=False, sort_keys=True))
            return 0 if response.get("status") == "ok" else 1
        except (OSError, RuntimeError) as exc:
            last_error = exc
            if monotonic_seconds() >= deadline:
                print("gate status failed: {}".format(last_error), file=sys.stderr)
                return 1
            time.sleep(0.2)


def main(argv=None):
    args = parse_args(argv)
    if args.command == "serve":
        return serve(args)
    if args.command == "status":
        return status_main(args)
    if args.command == "init-ledger":
        lock_handle = acquire_daemon_lock(args.lock_path)
        try:
            assert_empty_legacy_migration_window()
            prepare_session_cleanup_locks()
            payload = initialize_empty_active_session_ledger(args.ledger_path)
            print(json.dumps({
                "event": "active_session_ledger_initialized",
                "path": args.ledger_path,
                "revision": payload["revision"],
            }, sort_keys=True))
            return 0
        finally:
            lock_handle.close()
    raise SystemExit("unknown command")


if __name__ == "__main__":
    sys.exit(main())
