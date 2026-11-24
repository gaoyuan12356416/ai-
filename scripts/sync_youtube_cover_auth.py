"""Give the CPU cover worker the selected account's short-lived login."""

import os
from pathlib import Path
import stat
import sys
import time

from codex_account_watch import access_expiry, atomic_json, auth_identity, read_json


ACCOUNT_ROOT = Path("/mnt/data-disk/codex-accounts")
ACTIVE_AUTH = Path("/root/.codex/auth.json")
WORKER_AUTH = Path("/mnt/data-disk/pipeline-node-cpu/pipeline-dispatch/codex-auth/auth.json")


def cover_snapshot(auth, expected_identity):
    if auth_identity(auth) != expected_identity:
        raise ValueError("active account differs from the account watch")
    if (access_expiry(auth) or 0) <= time.time():
        raise ValueError("active login has expired")
    tokens = {key: auth["tokens"][key] for key in ("account_id", "access_token", "id_token")}
    # Isolated CLI homes are discarded after each image. Only the account
    # manager should rotate the refresh credential, where it can be persisted.
    tokens["refresh_token"] = ""
    return {"auth_mode": "chatgptAuthTokens", "tokens": tokens, "last_refresh": auth.get("last_refresh")}


def install_snapshot(path, snapshot):
    directory = path.parent.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
            or stat.S_IMODE(directory.st_mode) != 0o700):
        raise PermissionError("cover login directory is not private to the worker")
    if path.is_file() and not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == 0o600:
        try:
            if read_json(path) == snapshot:
                return False
        except ValueError:
            pass
    atomic_json(path, snapshot)
    return True


def main():
    import fcntl
    import pwd

    with (ACCOUNT_ROOT / "watch.lock").open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        state = read_json(ACCOUNT_ROOT / "state.json")
        snapshot = cover_snapshot(read_json(ACTIVE_AUTH), state["identities"][state["active"]])
        worker = pwd.getpwnam("pipeline-worker")
        # All accesses to worker-owned paths happen as that worker.
        os.setgroups([])
        os.setgid(worker.pw_gid)
        os.setuid(worker.pw_uid)
        if install_snapshot(WORKER_AUTH, snapshot):
            print("Updated YouTube cover login for the selected CPU account.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Upstream/file contents must never reach the journal.
        print("YouTube cover login sync failed ({})".format(type(error).__name__), file=sys.stderr)
        raise SystemExit(1)
