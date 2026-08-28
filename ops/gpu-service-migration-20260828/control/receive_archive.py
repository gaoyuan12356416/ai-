#!/usr/bin/env python3
"""Temporary forced SSH command: receive a checksum-pinned file into one run directory.

No shell, arbitrary path, archive extraction, reads, or port forwarding. The
authorized key additionally restricts source IP and expires via this receiver.
"""
import datetime
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time

ROOT = pathlib.Path("/data/migrations/gpu-service-migration-20260828T1502/direct-inputs")
UUID = "659e6f89-71fa-463d-842e-ccdf2c06e0fe"
EXPIRES = int(datetime.datetime(2026, 8, 28, 12, 0, tzinfo=datetime.timezone.utc).timestamp())
MAX_BYTES = 10 * 1024 ** 3


def parse_request(value):
    parts = shlex.split(value)
    if len(parts) != 4 or parts[0] != "receive":
        raise ValueError("receive NAME SIZE SHA256 only")
    _, name, size, expected = parts
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}\.(?:tar|tar\.gz|tgz|json|bundle)", name):
        raise ValueError("invalid archive name")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("invalid checksum")
    count = int(size)
    if not 0 < count <= MAX_BYTES:
        raise ValueError("invalid byte count")
    return name, count, expected


def main():
    import fcntl
    if socket.gethostname() != "VM-0-125-centos" or time.time() >= EXPIRES:
        raise ValueError("wrong host or expired receiver")
    name, count, expected = parse_request(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    uuid = subprocess.check_output(["findmnt", "-n", "-o", "UUID", "-T", "/data"],
                                   universal_newlines=True).strip()
    if uuid != UUID or pathlib.Path("/data").resolve() != pathlib.Path("/data"):
        raise ValueError("storage identity mismatch")
    if shutil.disk_usage("/data").free < count + 30 * 1024 ** 3:
        raise ValueError("insufficient disk headroom")
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    if ROOT.resolve() != ROOT:
        raise ValueError("destination contains symlink")
    os.chmod(str(ROOT), 0o700)
    target = ROOT / name
    partial = ROOT / ("." + name + ".partial")
    with os.fdopen(os.open(str(ROOT / ("." + name + ".lock")),
                           os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if target.exists() or target.is_symlink():
            raise ValueError("destination already exists; use a new snapshot name")
        received = 0
        sha = hashlib.sha256()
        with os.fdopen(os.open(str(partial), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                               0o600), "wb") as output:
            while received < count:
                chunk = sys.stdin.buffer.read(min(4 * 1024 * 1024, count - received))
                if not chunk:
                    raise ValueError("short archive stream")
                output.write(chunk)
                sha.update(chunk)
                received += len(chunk)
            if sys.stdin.buffer.read(1):
                raise ValueError("archive larger than declared")
            output.flush()
            os.fsync(output.fileno())
        if sha.hexdigest() != expected:
            raise ValueError("archive checksum mismatch")
        os.replace(str(partial), str(target))
        print(json.dumps({"file": name, "bytes": received, "sha256": expected,
                          "stored_under": str(ROOT), "verified_at_epoch": time.time()}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"ok": False, "error": type(error).__name__, "reason": str(error)}), file=sys.stderr)
        raise SystemExit(1)
