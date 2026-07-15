#!/usr/bin/env python3
"""Apply the reviewed account-copy V2 app merge from exact Git blobs.

Production is a shared monolith, so this tool never trusts a checkout copy of
``app.py`` as a replacement.  It verifies that the live source is byte-for-byte
the reviewed source commit, applies the source-to-target Git diff in an isolated
temporary directory, verifies the resulting target blob, then creates one
byte-identical backup and atomically installs the verified result.
"""

import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows test path
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - Linux production path
    msvcrt = None


APP_PATH = "app.py"


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def run_git(repo, arguments, input_bytes=None):
    command = ["git", "-C", str(repo)] + list(arguments)
    result = subprocess.run(
        command,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError("git command failed: %s" % (detail or "exit %s" % result.returncode))
    return result.stdout


def resolve_commit(repo, revision):
    value = run_git(repo, ["rev-parse", "--verify", "%s^{commit}" % revision])
    commit = value.decode("ascii", errors="strict").strip()
    if len(commit) != 40:
        raise RuntimeError("invalid resolved commit: %s" % revision)
    return commit


def git_blob(repo, commit, path=APP_PATH):
    return run_git(repo, ["show", "%s:%s" % (commit, path)])


def git_patch(repo, source_commit, target_commit, path=APP_PATH):
    return run_git(
        repo,
        [
            "diff",
            "--binary",
            "--full-index",
            source_commit,
            target_commit,
            "--",
            path,
        ],
    )


def apply_patch_in_isolation(source_bytes, patch_bytes):
    with tempfile.TemporaryDirectory(prefix="ad-control-v2-app-merge-") as value:
        root = Path(value)
        target = root / APP_PATH
        target.write_bytes(source_bytes)
        if patch_bytes:
            for check_only in (True, False):
                arguments = ["apply", "--whitespace=nowarn"]
                if check_only:
                    arguments.append("--check")
                arguments.append("-")
                result = subprocess.run(
                    ["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf"] + arguments,
                    cwd=str(root),
                    input=patch_bytes,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if result.returncode != 0:
                    detail = result.stderr.decode("utf-8", errors="replace").strip()
                    raise RuntimeError(
                        "isolated git apply%s failed: %s"
                        % (" check" if check_only else "", detail or result.returncode)
                    )
        return target.read_bytes()


def verified_merge(repo, source_revision, target_revision):
    repo = Path(repo).resolve()
    source_commit = resolve_commit(repo, source_revision)
    target_commit = resolve_commit(repo, target_revision)
    source_bytes = git_blob(repo, source_commit)
    target_bytes = git_blob(repo, target_commit)
    patch_bytes = git_patch(repo, source_commit, target_commit)
    merged_bytes = apply_patch_in_isolation(source_bytes, patch_bytes)
    if merged_bytes != target_bytes:
        raise RuntimeError(
            "isolated merge target mismatch: expected=%s actual=%s"
            % (sha256_bytes(target_bytes), sha256_bytes(merged_bytes))
        )
    return {
        "source_commit": source_commit,
        "target_commit": target_commit,
        "source_bytes": source_bytes,
        "target_bytes": target_bytes,
        "source_sha256": sha256_bytes(source_bytes),
        "target_sha256": sha256_bytes(target_bytes),
        "patch_sha256": sha256_bytes(patch_bytes),
        "patch_bytes": patch_bytes,
    }


def fsync_directory(path):
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_durable_file(descriptor, path, value, mode):
    """Write content and mode to one durable temporary inode."""
    path = Path(path)
    used_fd_chmod = hasattr(os, "fchmod")
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)
        handle.flush()
        if used_fd_chmod:
            # Production Linux persists the content and final mode together
            # before the inode becomes visible through replace/link.
            os.fchmod(handle.fileno(), mode)
        os.fsync(handle.fileno())
    if not used_fd_chmod:  # pragma: no cover - Windows-specific fallback
        os.chmod(str(path), mode)
        with path.open("rb+") as handle:
            os.fsync(handle.fileno())


def atomic_write(path, value, mode):
    path = Path(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s.ad-control-v2." % path.name,
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        write_durable_file(descriptor, temporary_path, value, mode)
        os.replace(str(temporary_path), str(path))
        fsync_directory(path.parent)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def verify_durable_backup(path, value):
    """Verify and durably acknowledge an already-visible backup checkpoint."""
    path = Path(path)
    # Windows rejects fsync on a read-only CRT descriptor; rb+ remains
    # read-preserving while keeping the same durability contract as Linux.
    with path.open("rb+") as handle:
        existing = handle.read()
        if existing != value:
            raise RuntimeError("existing backup checksum mismatch: %s" % path)
        os.fsync(handle.fileno())
    fsync_directory(path.parent)
    if path.read_bytes() != value:
        raise RuntimeError("existing backup checksum mismatch: %s" % path)


def atomic_create_backup(path, value, mode=0o600):
    """Durably create a backup without replacing an existing checkpoint."""
    path = Path(path)
    if path.exists():
        verify_durable_backup(path, value)
        return False
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s.pending." % path.name,
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        write_durable_file(descriptor, temporary_path, value, mode)
        try:
            os.link(str(temporary_path), str(path))
        except FileExistsError:
            verify_durable_backup(path, value)
            return False
        fsync_directory(path.parent)
        if path.read_bytes() != value:
            raise RuntimeError("backup checksum mismatch: %s" % path)
        return True
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


@contextmanager
def exclusive_deploy_lock(path):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    os.chmod(str(path), 0o600)
    locked = False
    try:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                raise RuntimeError("deployment lock busy: %s" % path)
        elif msvcrt is not None:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("deployment lock busy: %s" % path)
        else:  # pragma: no cover - supported runtimes have one implementation
            raise RuntimeError("no supported deployment lock implementation")
        locked = True
        yield
    finally:
        if locked:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def apply_verified_under_lock(app_path, merge, backup_dir=None):
    current_bytes = app_path.read_bytes()
    current_sha256 = sha256_bytes(current_bytes)
    if current_bytes == merge["target_bytes"]:
        print(
            "%s: unchanged target_commit=%s sha256=%s"
            % (app_path, merge["target_commit"], merge["target_sha256"])
        )
        return {"status": "unchanged", "backup": "", **merge}
    if current_bytes != merge["source_bytes"]:
        raise RuntimeError(
            "live app source mismatch: expected_commit=%s expected_sha256=%s actual_sha256=%s"
            % (merge["source_commit"], merge["source_sha256"], current_sha256)
        )

    root = app_path.parent
    backup_root = Path(backup_dir).resolve() if backup_dir else root / "deploy_backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    os.chmod(str(backup_root), 0o700)
    # Persist the checkpoint directory entry before trusting a file inside it.
    fsync_directory(backup_root)
    fsync_directory(backup_root.parent)
    backup_path = backup_root / (
        "app.py.before-ad-control-v2-%s-to-%s"
        % (merge["source_sha256"][:12], merge["target_sha256"][:12])
    )
    atomic_create_backup(backup_path, merge["source_bytes"], mode=0o600)

    # The shared deployment lock is held across this final source read and the
    # atomic replacement.  Every reviewed monolith deploy must use the same
    # lock file; an uncoordinated writer is still detected by this last read.
    if app_path.read_bytes() != merge["source_bytes"]:
        raise RuntimeError("live app source changed after backup; refusing install")

    mode = stat.S_IMODE(app_path.stat().st_mode)
    try:
        atomic_write(app_path, merge["target_bytes"], mode)
        if app_path.read_bytes() != merge["target_bytes"]:
            raise RuntimeError("installed target checksum mismatch")
    except Exception as install_error:
        failed_bytes = app_path.read_bytes() if app_path.exists() else None
        if failed_bytes is None or failed_bytes == merge["target_bytes"]:
            atomic_write(app_path, merge["source_bytes"], mode)
            if app_path.read_bytes() != merge["source_bytes"]:
                raise RuntimeError("automatic source rollback checksum mismatch") from install_error
        elif failed_bytes != merge["source_bytes"]:
            raise RuntimeError(
                "install failed and live app changed unexpectedly; "
                "refusing to overwrite unknown bytes"
            ) from install_error
        raise

    print("backup: %s sha256=%s" % (backup_path, merge["source_sha256"]))
    print(
        "%s: changed target_commit=%s sha256=%s patch_sha256=%s"
        % (app_path, merge["target_commit"], merge["target_sha256"], merge["patch_sha256"])
    )
    return {"status": "changed", "backup": str(backup_path), **merge}


def apply_release(
    root,
    repo,
    source_revision,
    target_revision,
    backup_dir=None,
    check=False,
    lock_file=None,
):
    root = Path(root).resolve()
    app_path = root / APP_PATH
    if not app_path.is_file():
        raise RuntimeError("missing target app: %s" % app_path)

    merge = verified_merge(repo, source_revision, target_revision)
    if check:
        current_bytes = app_path.read_bytes()
        current_sha256 = sha256_bytes(current_bytes)
        if current_bytes == merge["target_bytes"]:
            print(
                "%s: unchanged target_commit=%s sha256=%s"
                % (app_path, merge["target_commit"], merge["target_sha256"])
            )
            return {"status": "unchanged", "backup": "", **merge}
        if current_bytes != merge["source_bytes"]:
            raise RuntimeError(
                "live app source mismatch: expected_commit=%s expected_sha256=%s actual_sha256=%s"
                % (merge["source_commit"], merge["source_sha256"], current_sha256)
            )
        print(
            "%s: would change source_commit=%s target_commit=%s source_sha256=%s target_sha256=%s patch_sha256=%s"
            % (
                app_path,
                merge["source_commit"],
                merge["target_commit"],
                merge["source_sha256"],
                merge["target_sha256"],
                merge["patch_sha256"],
            )
        )
        return {"status": "would_change", "backup": "", **merge}

    lock_path = Path(lock_file).resolve() if lock_file else root / ".deployment.lock"
    with exclusive_deploy_lock(lock_path):
        return apply_verified_under_lock(app_path, merge, backup_dir=backup_dir)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--target-commit", required=True)
    parser.add_argument("--backup-dir", default="")
    parser.add_argument("--lock-file", default="")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        apply_release(
            root=args.root,
            repo=args.repo,
            source_revision=args.source_commit,
            target_revision=args.target_commit,
            backup_dir=args.backup_dir or None,
            check=args.check,
            lock_file=args.lock_file or None,
        )
    except Exception as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
