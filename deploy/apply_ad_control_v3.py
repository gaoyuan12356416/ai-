#!/usr/bin/env python3
"""Apply the reviewed V3 dispatcher as an exact-source monolith overlay.

The live service is a shared ``app.py``.  This deployer therefore never copies
``app.py`` from a checkout.  It reads the reviewed source and target Git blobs,
validates that the app change is an additive V3-only dispatcher overlay,
applies that patch in isolation, then installs the verified result only when
the live bytes still equal the reviewed source.  A different live hash is
drift, not something this tool is allowed to merge or overwrite.
"""

import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
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
V3_PREFIX = b"/api/ad-control/v3"
REQUIRED_TARGET_MARKERS = (
    b"def _dispatch_ad_control_v3(self, parsed):",
    b"from features.ad_control_v3 import routes as ad_control_v3_routes",
    b"ad_control_v3_routes.dispatch(self, self.command, parsed)",
)
RUNTIME_PREFIX = "features/ad_control_v3/"
RUNTIME_RUNNER = "scripts/ad_control_v3_runner.py"
RUNTIME_SUFFIXES = frozenset({".py", ".html", ".css", ".js"})
REQUIRED_RUNTIME_TARGETS = frozenset(
    {
        "features/ad_control_v3/__init__.py",
        "features/ad_control_v3/catalog.py",
        "features/ad_control_v3/errors.py",
        "features/ad_control_v3/repository.py",
        "features/ad_control_v3/rule_engine.py",
        "features/ad_control_v3/routes.py",
        "features/ad_control_v3/schemas.py",
        "features/ad_control_v3/service.py",
        "features/ad_control_v3/storage.py",
        "features/ad_control_v3/page_renderer.py",
        "features/ad_control_v3/channels/__init__.py",
        "features/ad_control_v3/channels/base.py",
        "features/ad_control_v3/channels/facebook.py",
        "features/ad_control_v3/channels/tiktok.py",
        "features/ad_control_v3/templates/rule-groups.html",
        "features/ad_control_v3/templates/execution-logs.html",
        "features/ad_control_v3/assets/app.css",
        "features/ad_control_v3/assets/app.js",
        RUNTIME_RUNNER,
    }
)


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def run_git(repo, arguments, input_bytes=None):
    result = subprocess.run(
        ["git", "-C", str(repo)] + list(arguments),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError("git command failed: %s" % (detail or result.returncode))
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
        ["diff", "--binary", "--full-index", source_commit, target_commit, "--", path],
    )


def git_paths(repo, commit):
    raw = run_git(
        repo,
        [
            "ls-tree",
            "-r",
            "--name-only",
            commit,
            "--",
            "features/ad_control_v3",
            RUNTIME_RUNNER,
        ],
    )
    return {
        line.strip()
        for line in raw.decode("utf-8", errors="strict").splitlines()
        if line.strip()
    }


def is_allowed_runtime_path(path):
    value = str(path or "").replace("\\", "/")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        return False
    if value == RUNTIME_RUNNER:
        return True
    return value.startswith(RUNTIME_PREFIX) and pure.suffix in RUNTIME_SUFFIXES


def git_file(repo, commit, path):
    result = subprocess.run(
        ["git", "-C", str(repo), "show", "%s:%s" % (commit, path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 0:
        return result.stdout
    return None


def git_file_mode(repo, commit, path):
    raw = run_git(repo, ["ls-tree", commit, "--", path]).decode("utf-8", errors="strict").strip()
    if not raw:
        return None
    mode = raw.split(None, 1)[0]
    if mode not in {"100644", "100755"}:
        raise RuntimeError("runtime path is not a regular reviewed file: %s mode=%s" % (path, mode))
    return 0o755 if mode == "100755" else 0o644


def verified_runtime_manifest(repo, source_commit, target_commit):
    source_paths = git_paths(repo, source_commit)
    target_paths = git_paths(repo, target_commit)
    # The Git tree may contain documentation or local notes under the feature
    # directory. They are deliberately outside the install manifest instead
    # of becoming deployable merely because they share the prefix.
    candidates = sorted(
        path for path in (source_paths | target_paths) if is_allowed_runtime_path(path)
    )
    allowed_target_paths = {path for path in target_paths if is_allowed_runtime_path(path)}
    missing_required = sorted(REQUIRED_RUNTIME_TARGETS - allowed_target_paths)
    if missing_required:
        raise RuntimeError("reviewed target runtime is incomplete: %s" % missing_required[0])
    entries = []
    for path in candidates:
        source_bytes = git_file(repo, source_commit, path)
        target_bytes = git_file(repo, target_commit, path)
        if source_bytes is None and target_bytes is None:
            raise RuntimeError("runtime manifest lost both reviewed blobs: %s" % path)
        source_mode = git_file_mode(repo, source_commit, path) if source_bytes is not None else None
        target_mode = git_file_mode(repo, target_commit, path) if target_bytes is not None else None
        if target_bytes is not None and path.endswith(".py"):
            try:
                compile(target_bytes, path, "exec")
            except (SyntaxError, ValueError, TypeError) as exc:
                raise RuntimeError(
                    "reviewed target Python does not compile: %s: %s" % (path, exc)
                )
        entries.append(
            {
                "path": path,
                "source_bytes": source_bytes,
                "target_bytes": target_bytes,
                "source_mode": source_mode,
                "target_mode": target_mode,
                "source_sha256": sha256_bytes(source_bytes) if source_bytes is not None else "",
                "target_sha256": sha256_bytes(target_bytes) if target_bytes is not None else "",
            }
        )
    if not any(entry["path"].startswith(RUNTIME_PREFIX) for entry in entries):
        raise RuntimeError("reviewed target contains no V3 feature package")
    return entries


def validate_v3_only_patch(source_bytes, target_bytes, patch_bytes):
    if source_bytes == target_bytes:
        raise RuntimeError("reviewed app overlay is empty")
    for marker in REQUIRED_TARGET_MARKERS:
        if marker in source_bytes:
            raise RuntimeError("reviewed source already contains V3 dispatcher marker")
        if marker not in target_bytes:
            raise RuntimeError("reviewed target is missing V3 dispatcher marker")
    if V3_PREFIX not in target_bytes:
        raise RuntimeError("reviewed target is missing V3 route prefix")

    removed = []
    added = []
    for raw_line in patch_bytes.splitlines():
        if raw_line.startswith(b"---") or raw_line.startswith(b"+++"):
            continue
        if raw_line.startswith(b"-"):
            removed.append(raw_line[1:])
        elif raw_line.startswith(b"+"):
            added.append(raw_line[1:])
    if removed:
        raise RuntimeError("V3 app overlay must be additive; reviewed patch removes source lines")
    if not added or not any(V3_PREFIX in line for line in added):
        raise RuntimeError("reviewed patch does not add the V3 dispatcher")

    unrelated = [
        line
        for line in added
        if line.strip()
        and b"ad_control_v3" not in line
        and V3_PREFIX not in line
        and line.strip()
        not in {
            b'try:',
            b'except Exception:',
            b'"""Lazily dispatch the isolated V3 surface after its prefix matched."""',
            b'logging.exception("ad-control V3 route dispatcher failed")',
            b'json_response(',
            b'self,',
            b'return',
            b'return True',
            b'500,',
            b'no_store=True,',
            b')',
            b'{',
            b'},',
            b'"code": "internal_error",',
            b'"error": "internal server error",',
            b'"message": "internal server error",',
        }
    ]
    if unrelated:
        raise RuntimeError(
            "V3 app overlay contains unrelated added line: %s"
            % unrelated[0].decode("utf-8", errors="replace").strip()
        )


def apply_patch_in_isolation(source_bytes, patch_bytes):
    with tempfile.TemporaryDirectory(prefix="ad-control-v3-app-overlay-") as value:
        root = Path(value)
        target = root / APP_PATH
        target.write_bytes(source_bytes)
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


def verified_overlay(repo, source_revision, target_revision):
    repo = Path(repo).resolve()
    source_commit = resolve_commit(repo, source_revision)
    target_commit = resolve_commit(repo, target_revision)
    source_bytes = git_blob(repo, source_commit)
    target_bytes = git_blob(repo, target_commit)
    patch_bytes = git_patch(repo, source_commit, target_commit)
    validate_v3_only_patch(source_bytes, target_bytes, patch_bytes)
    merged_bytes = apply_patch_in_isolation(source_bytes, patch_bytes)
    if merged_bytes != target_bytes:
        raise RuntimeError(
            "isolated overlay target mismatch: expected=%s actual=%s"
            % (sha256_bytes(target_bytes), sha256_bytes(merged_bytes))
        )
    return {
        "source_commit": source_commit,
        "target_commit": target_commit,
        "source_bytes": source_bytes,
        "target_bytes": target_bytes,
        "patch_bytes": patch_bytes,
        "source_sha256": sha256_bytes(source_bytes),
        "target_sha256": sha256_bytes(target_bytes),
        "patch_sha256": sha256_bytes(patch_bytes),
    }


def fsync_directory(path):
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path, value, mode):
    path = Path(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s.ad-control-v3." % path.name,
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        if not hasattr(os, "fchmod"):  # pragma: no cover - Windows fallback
            os.chmod(str(temporary_path), mode)
        os.replace(str(temporary_path), str(path))
        fsync_directory(path.parent)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def durable_backup(path, value):
    path = Path(path)
    if path.exists():
        if path.read_bytes() != value:
            raise RuntimeError("existing backup checksum mismatch: %s" % path)
        return False
    descriptor, temporary_name = tempfile.mkstemp(prefix=".%s.pending." % path.name, dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        try:
            os.link(str(temporary_path), str(path))
        except FileExistsError:
            if path.read_bytes() != value:
                raise RuntimeError("existing backup checksum mismatch: %s" % path)
            return False
        fsync_directory(path.parent)
        if path.read_bytes() != value:
            raise RuntimeError("backup checksum mismatch: %s" % path)
        return True
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def ensure_directory(path, mode=0o755):
    path = Path(path)
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir()
        os.chmod(str(directory), mode)
        fsync_directory(directory.parent)


def remove_file_durably(path):
    path = Path(path)
    if not path.exists():
        return
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("refusing to remove non-regular runtime path: %s" % path)
    path.unlink()
    fsync_directory(path.parent)


def safe_runtime_path(root, relative_path):
    root = Path(root).resolve()
    path = root / Path(*PurePosixPath(relative_path).parts)
    resolved = path.resolve(strict=False)
    try:
        inside = os.path.commonpath([str(root), str(resolved)]) == str(root)
    except ValueError:
        inside = False
    if not inside:
        raise RuntimeError("runtime path escapes live root: %s" % relative_path)
    cursor = path
    while cursor != root:
        if cursor.is_symlink():
            raise RuntimeError("runtime path contains a symbolic link: %s" % relative_path)
        cursor = cursor.parent
    return path


def live_file_state(root, entry):
    path = safe_runtime_path(root, entry["path"])
    if path.exists() and (not path.is_file() or path.is_symlink()):
        raise RuntimeError("live runtime path is not a regular file: %s" % entry["path"])
    live_bytes = path.read_bytes() if path.exists() else None
    live_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    source_bytes = entry["source_bytes"]
    target_bytes = entry["target_bytes"]
    if source_bytes == target_bytes and live_bytes == target_bytes:
        state = "target"
    elif live_bytes is None and source_bytes is None:
        state = "source"
    elif live_bytes is not None and source_bytes is not None and live_bytes == source_bytes:
        state = "source"
    elif live_bytes is None and target_bytes is None:
        state = "target"
    elif live_bytes is not None and target_bytes is not None and live_bytes == target_bytes:
        state = "target"
    else:
        raise RuntimeError(
            "live runtime source drift: path=%s source_sha256=%s target_sha256=%s actual_sha256=%s"
            % (
                entry["path"],
                entry["source_sha256"] or "absent",
                entry["target_sha256"] or "absent",
                sha256_bytes(live_bytes) if live_bytes is not None else "absent",
            )
        )
    target_mode = entry["target_mode"]
    mode_ready = target_bytes is None or (
        live_mode is not None and (os.name == "nt" or live_mode == target_mode)
    )
    return {
        "path": path,
        "state": state,
        "bytes": live_bytes,
        "mode": live_mode,
        "target_ready": state == "target" and mode_ready,
    }


def inspect_release_state(root, overlay, runtime_manifest):
    app_path = root / APP_PATH
    current_app = app_path.read_bytes()
    if current_app == overlay["source_bytes"]:
        app_state = "source"
    elif current_app == overlay["target_bytes"]:
        app_state = "target"
    else:
        raise RuntimeError(
            "live app source drift: expected_commit=%s expected_sha256=%s actual_sha256=%s"
            % (overlay["source_commit"], overlay["source_sha256"], sha256_bytes(current_app))
        )
    runtime_states = [live_file_state(root, entry) for entry in runtime_manifest]
    ready = app_state == "target" and all(state["target_ready"] for state in runtime_states)
    return {
        "app_path": app_path,
        "app_state": app_state,
        "app_bytes": current_app,
        "app_mode": stat.S_IMODE(app_path.stat().st_mode),
        "runtime": runtime_states,
        "ready": ready,
    }


def backup_release_sources(backup_root, overlay, runtime_manifest):
    release_root = backup_root / (
        "ad-control-v3-%s-to-%s"
        % (overlay["source_commit"][:12], overlay["target_commit"][:12])
    )
    ensure_directory(release_root, mode=0o700)
    app_backup = release_root / APP_PATH
    durable_backup(app_backup, overlay["source_bytes"])
    for entry in runtime_manifest:
        if entry["source_bytes"] is None:
            continue
        target = release_root / "runtime" / Path(*PurePosixPath(entry["path"]).parts)
        ensure_directory(target.parent, mode=0o700)
        durable_backup(target, entry["source_bytes"])
    fsync_directory(release_root)
    return release_root, app_backup


def install_runtime_entry(root, entry, live_state):
    path = live_state["path"]
    target_bytes = entry["target_bytes"]
    if live_state["target_ready"]:
        return None
    change = {
        "path": path,
        "original_bytes": live_state["bytes"],
        "original_mode": live_state["mode"],
        "installed_bytes": target_bytes,
    }
    if target_bytes is None:
        remove_file_durably(path)
    else:
        ensure_directory(path.parent, mode=0o755)
        atomic_write(path, target_bytes, int(entry["target_mode"]))
    return change


def rollback_changes(changes):
    rollback_errors = []
    for change in reversed(changes):
        path = change["path"]
        try:
            current = path.read_bytes() if path.exists() else None
            installed = change["installed_bytes"]
            original = change["original_bytes"]
            if current == original:
                continue
            if current != installed:
                raise RuntimeError("rollback drift at %s" % path)
            if original is None:
                remove_file_durably(path)
            else:
                ensure_directory(path.parent, mode=0o755)
                atomic_write(path, original, int(change["original_mode"] or 0o644))
        except Exception as exc:  # keep reversing every path before surfacing failure
            rollback_errors.append("%s: %s" % (path, exc))
    if rollback_errors:
        raise RuntimeError("release rollback failed: %s" % "; ".join(rollback_errors))


@contextmanager
def exclusive_deploy_lock(path):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    locked = False
    try:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                raise RuntimeError("deployment lock busy: %s" % path)
        elif msvcrt is not None:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("deployment lock busy: %s" % path)
        else:  # pragma: no cover
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


def apply_release(root, repo, source_revision, target_revision, backup_dir, check=False, lock_file=None):
    root = Path(root).resolve()
    app_path = root / APP_PATH
    if not app_path.is_file() or app_path.is_symlink():
        raise RuntimeError("missing target app: %s" % app_path)
    overlay = verified_overlay(repo, source_revision, target_revision)
    runtime_manifest = verified_runtime_manifest(
        Path(repo).resolve(), overlay["source_commit"], overlay["target_commit"]
    )
    release_state = inspect_release_state(root, overlay, runtime_manifest)
    if release_state["ready"]:
        return {
            "status": "unchanged",
            "backup": "",
            "runtime_file_count": len(runtime_manifest),
            **overlay,
        }
    if check:
        return {
            "status": "would_change",
            "backup": "",
            "runtime_file_count": len(runtime_manifest),
            **overlay,
        }
    if not backup_dir:
        raise RuntimeError("backup_dir on the data disk is required")

    raw_backup_root = Path(backup_dir)
    if raw_backup_root.is_symlink():
        raise RuntimeError("backup_dir must not be a symbolic link")
    backup_root = raw_backup_root.resolve()
    try:
        backup_inside_live = os.path.commonpath([str(root), str(backup_root)]) == str(root)
    except ValueError:
        backup_inside_live = False
    if backup_inside_live:
        raise RuntimeError("backup_dir must be outside the live application root")
    if os.name != "nt" and str(root).startswith("/root/"):
        if not (
            str(backup_root) == "/mnt/data-disk"
            or str(backup_root).startswith("/mnt/data-disk/")
        ):
            raise RuntimeError("production backup_dir must be on /mnt/data-disk")
        data_mount = Path("/mnt/data-disk")
        if not data_mount.is_dir() or not os.path.ismount(str(data_mount)):
            raise RuntimeError("/mnt/data-disk is not mounted; refusing system-disk backup")
    ensure_directory(backup_root, mode=0o700)
    os.chmod(str(backup_root), 0o700)
    lock_path = Path(lock_file).resolve() if lock_file else root / ".deployment.lock"
    with exclusive_deploy_lock(lock_path):
        release_state = inspect_release_state(root, overlay, runtime_manifest)
        if release_state["ready"]:
            return {
                "status": "unchanged",
                "backup": "",
                "runtime_file_count": len(runtime_manifest),
                **overlay,
            }
        release_backup, app_backup = backup_release_sources(
            backup_root, overlay, runtime_manifest
        )
        # The checkpoint for every reviewed source exists before the first
        # runtime file is replaced. Re-read all live paths under the shared
        # lock so a writer racing the backup cannot be overwritten.
        release_state = inspect_release_state(root, overlay, runtime_manifest)
        changes = []
        try:
            for entry, live_state in zip(runtime_manifest, release_state["runtime"]):
                if live_state["target_ready"]:
                    continue
                change = {
                    "path": live_state["path"],
                    "original_bytes": live_state["bytes"],
                    "original_mode": live_state["mode"],
                    "installed_bytes": entry["target_bytes"],
                }
                # Record before the operation so a failure after replace/fsync
                # still participates in reverse rollback.
                changes.append(change)
                install_runtime_entry(root, entry, live_state)

            if release_state["app_state"] != "target":
                app_change = {
                    "path": app_path,
                    "original_bytes": release_state["app_bytes"],
                    "original_mode": release_state["app_mode"],
                    "installed_bytes": overlay["target_bytes"],
                }
                changes.append(app_change)
                atomic_write(app_path, overlay["target_bytes"], release_state["app_mode"])

            if app_path.read_bytes() != overlay["target_bytes"]:
                raise RuntimeError("installed overlay checksum mismatch")
            final_state = inspect_release_state(root, overlay, runtime_manifest)
            if not final_state["ready"]:
                raise RuntimeError("installed runtime manifest verification failed")
        except Exception as install_error:
            try:
                rollback_changes(changes)
            except Exception as rollback_error:
                raise RuntimeError(
                    "release install failed and automatic rollback was incomplete: %s"
                    % rollback_error
                ) from install_error
            raise
    return {
        "status": "changed",
        "backup": str(release_backup),
        "app_backup": str(app_backup),
        "runtime_file_count": len(runtime_manifest),
        **overlay,
    }


def rollback_release(root, repo, source_revision, target_revision, backup_dir, check=False, lock_file=None):
    """Restore a complete reviewed target to its exact checkpointed source."""

    root = Path(root).resolve()
    app_path = root / APP_PATH
    if not app_path.is_file() or app_path.is_symlink():
        raise RuntimeError("missing target app: %s" % app_path)
    overlay = verified_overlay(repo, source_revision, target_revision)
    runtime_manifest = verified_runtime_manifest(
        Path(repo).resolve(), overlay["source_commit"], overlay["target_commit"]
    )
    if not backup_dir:
        raise RuntimeError("backup_dir on the data disk is required")
    backup_root = Path(backup_dir).resolve()
    release_root = backup_root / (
        "ad-control-v3-%s-to-%s"
        % (overlay["source_commit"][:12], overlay["target_commit"][:12])
    )
    app_backup = release_root / APP_PATH
    if not app_backup.is_file() or app_backup.is_symlink():
        raise RuntimeError("release checkpoint is missing app.py")
    if app_backup.read_bytes() != overlay["source_bytes"]:
        raise RuntimeError("release checkpoint app checksum mismatch")
    for entry in runtime_manifest:
        if entry["source_bytes"] is None:
            continue
        checkpoint = release_root / "runtime" / Path(*PurePosixPath(entry["path"]).parts)
        if not checkpoint.is_file() or checkpoint.is_symlink():
            raise RuntimeError("release checkpoint is missing runtime source: %s" % entry["path"])
        if checkpoint.read_bytes() != entry["source_bytes"]:
            raise RuntimeError("release checkpoint runtime checksum mismatch: %s" % entry["path"])

    lock_path = Path(lock_file).resolve() if lock_file else root / ".deployment.lock"
    with exclusive_deploy_lock(lock_path):
        state = inspect_release_state(root, overlay, runtime_manifest)
        all_source = state["app_state"] == "source" and all(
            live["state"] == "source" or entry["source_bytes"] == entry["target_bytes"]
            for entry, live in zip(runtime_manifest, state["runtime"])
        )
        if all_source:
            return {
                "status": "unchanged",
                "backup": str(release_root),
                "runtime_file_count": len(runtime_manifest),
                **overlay,
            }
        if not state["ready"]:
            raise RuntimeError("rollback requires the complete reviewed target state")
        if check:
            return {
                "status": "would_rollback",
                "backup": str(release_root),
                "runtime_file_count": len(runtime_manifest),
                **overlay,
            }
        changes = []
        for entry, live_state in zip(runtime_manifest, state["runtime"]):
            changes.append(
                {
                    "path": live_state["path"],
                    "original_bytes": entry["source_bytes"],
                    "original_mode": entry["source_mode"] or 0o644,
                    "installed_bytes": entry["target_bytes"],
                }
            )
        changes.append(
            {
                "path": state["app_path"],
                "original_bytes": overlay["source_bytes"],
                "original_mode": 0o644,
                "installed_bytes": overlay["target_bytes"],
            }
        )
        rollback_changes(changes)
        restored = inspect_release_state(root, overlay, runtime_manifest)
        if restored["app_state"] != "source" or not all(
            live["state"] == "source" or entry["source_bytes"] == entry["target_bytes"]
            for entry, live in zip(runtime_manifest, restored["runtime"])
        ):
            raise RuntimeError("release rollback verification failed")
    return {
        "status": "rolled_back",
        "backup": str(release_root),
        "runtime_file_count": len(runtime_manifest),
        **overlay,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--target-commit", required=True)
    parser.add_argument("--backup-dir", default="")
    parser.add_argument("--lock-file", default="")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args(argv)
    try:
        operation = rollback_release if args.rollback else apply_release
        result = operation(
            root=args.root,
            repo=args.repo,
            source_revision=args.source_commit,
            target_revision=args.target_commit,
            backup_dir=args.backup_dir or None,
            check=args.check,
            lock_file=args.lock_file or None,
        )
        print(
            "%s target_commit=%s target_sha256=%s patch_sha256=%s runtime_files=%s backup=%s"
            % (
                result["status"],
                result["target_commit"],
                result["target_sha256"],
                result["patch_sha256"],
                result["runtime_file_count"],
                result["backup"],
            )
        )
    except Exception as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
