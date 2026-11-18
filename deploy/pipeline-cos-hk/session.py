"""Install only the CPU-issued short-lived session received over the private SSH socket."""
import grp
import json
import os
import socket
import tempfile
import time
from pathlib import Path


def main():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(15)
        sock.connect("/run/pipeline-cos-hk/session.sock")
        with sock.makefile("rb") as stream:
            raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError("unexpected session size")
    record = json.loads(raw)
    if (record["expires"] <= time.time() + 60 or
            not all(record["credentials"].get(k) for k in ("TmpSecretId", "TmpSecretKey", "Token"))):
        raise ValueError("scoped COS session unavailable")
    target = Path("/etc/pipeline-cos-cloud/ci-session.json")
    if target.exists() and target.read_bytes() == raw:
        return
    fd, tmp = tempfile.mkstemp(dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
            out.flush()
            os.fchown(out.fileno(), 0, grp.getgrnam("pipeline-cos").gr_gid)
            os.fchmod(out.fileno(), 0o640)
            os.fsync(out.fileno())
        os.replace(tmp, str(target))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


if __name__ == "__main__":
    main()
