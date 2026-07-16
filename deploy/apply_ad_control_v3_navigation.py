#!/usr/bin/env python3
"""Safely merge the reviewed V3 navigation group into the live navigation.

This is deliberately separate from the application runtime overlay.  The
source group is read from the exact ``HEAD`` Git blob of ``--repo-root`` and
the checked-out file must still match that blob.  The live navigation is
merged by top-level key, backed up outside the system disk, and written
atomically.  Rollback is allowed only from a generated checkpoint while the
live bytes still match that checkpoint's installed hash.
"""

import argparse
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows test path
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - Linux production path
    msvcrt = None


DEFAULT_LIVE_TARGET = Path("/usr/share/nginx/html/navigation.json")
SOURCE_RELATIVE_PATH = Path("static/navigation.json")
V3_GROUP_KEY = "ad_control_v3"
EXPECTED_V3_PAGES = {
    "adControlV3Rules": "/api/ad-control/v3/ui/rule-groups",
    "adControlV3Logs": "/api/ad-control/v3/ui/execution-logs",
}
CHECKPOINT_VERSION = 1


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fsync_directory(path):
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_directory(path, mode=0o700):
    path = Path(path)
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    if cursor.is_symlink() or not cursor.is_dir():
        raise RuntimeError("directory ancestor is not a regular directory: %s" % cursor)
    for directory in reversed(missing):
        directory.mkdir()
        os.chmod(str(directory), mode)
        fsync_directory(directory.parent)


def require_regular_file(path, label):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("%s must be a regular non-symlink file: %s" % (label, path))
    return path


def atomic_write(path, value, mode):
    path = Path(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s.ad-control-v3-navigation." % path.name,
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


def create_exclusive_file(path, value, mode=0o600):
    path = Path(path)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    fsync_directory(path.parent)


def run_git(repo_root, arguments):
    result = subprocess.run(
        ["git", "-C", str(repo_root)] + list(arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError("git command failed: %s" % (detail or result.returncode))
    return result.stdout


def parse_navigation(value, label):
    try:
        navigation = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("%s is not valid UTF-8 JSON: %s" % (label, exc))
    if not isinstance(navigation, list):
        raise RuntimeError("%s top level must be a JSON array" % label)

    group_keys = set()
    for index, group in enumerate(navigation):
        if not isinstance(group, dict):
            raise RuntimeError("%s group %s must be an object" % (label, index))
        key = group.get("key")
        if not isinstance(key, str) or not key.strip():
            raise RuntimeError("%s group %s has an invalid key" % (label, index))
        if key in group_keys:
            raise RuntimeError("%s contains duplicate group key: %s" % (label, key))
        group_keys.add(key)
        items = group.get("items")
        if not isinstance(items, list):
            raise RuntimeError("%s group %s items must be an array" % (label, key))
        item_keys = set()
        for item_index, item in enumerate(items):
            if not isinstance(item, dict):
                raise RuntimeError(
                    "%s group %s item %s must be an object" % (label, key, item_index)
                )
            item_key = item.get("key")
            if not isinstance(item_key, str) or not item_key.strip():
                raise RuntimeError(
                    "%s group %s item %s has an invalid key" % (label, key, item_index)
                )
            if item_key in item_keys:
                raise RuntimeError(
                    "%s group %s contains duplicate item key: %s"
                    % (label, key, item_key)
                )
            item_keys.add(item_key)
    return navigation


def validate_v3_group(group):
    if not isinstance(group, dict) or group.get("key") != V3_GROUP_KEY:
        raise RuntimeError("reviewed source is missing the %s group" % V3_GROUP_KEY)
    items = group.get("items")
    if not isinstance(items, list) or len(items) != 2:
        raise RuntimeError("%s must contain exactly two dynamic pages" % V3_GROUP_KEY)
    actual = {}
    for item in items:
        key = item.get("key")
        href = item.get("href")
        if item.get("kind") != "page" or not isinstance(href, str):
            raise RuntimeError("%s items must be dynamic page entries" % V3_GROUP_KEY)
        if key in actual:
            raise RuntimeError("%s contains duplicate page key: %s" % (V3_GROUP_KEY, key))
        actual[key] = href
    if actual != EXPECTED_V3_PAGES:
        raise RuntimeError("%s does not contain the two reviewed dynamic routes" % V3_GROUP_KEY)
    return group


def source_navigation_group(repo_root):
    raw_repo_root = Path(repo_root)
    if raw_repo_root.is_symlink() or not raw_repo_root.is_dir():
        raise RuntimeError("repo_root must be a regular non-symlink directory")
    repo_root = raw_repo_root.resolve()
    source_path = require_regular_file(
        repo_root / SOURCE_RELATIVE_PATH, "source navigation"
    )
    head = run_git(repo_root, ["rev-parse", "--verify", "HEAD^{commit}"])
    commit = head.decode("ascii", errors="strict").strip()
    if len(commit) != 40:
        raise RuntimeError("repo_root HEAD is not an exact commit")
    committed_bytes = run_git(
        repo_root, ["show", "%s:%s" % (commit, SOURCE_RELATIVE_PATH.as_posix())]
    )
    if source_path.read_bytes() != committed_bytes:
        raise RuntimeError("source navigation differs from exact HEAD commit")
    navigation = parse_navigation(committed_bytes, "reviewed source navigation")
    matches = [group for group in navigation if group.get("key") == V3_GROUP_KEY]
    if len(matches) != 1:
        raise RuntimeError(
            "reviewed source must contain exactly one %s group" % V3_GROUP_KEY
        )
    group = validate_v3_group(matches[0])
    canonical_group = json.dumps(
        group, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "commit": commit,
        "path": source_path,
        "group": copy.deepcopy(group),
        "group_sha256": sha256_bytes(canonical_group),
    }


def serialize_navigation(navigation):
    return (json.dumps(navigation, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def merge_navigation(live_bytes, source_group):
    navigation = parse_navigation(live_bytes, "live navigation")
    matches = [group for group in navigation if group.get("key") == V3_GROUP_KEY]
    if matches:
        if len(matches) != 1:  # parse_navigation normally catches this first
            raise RuntimeError("live navigation contains duplicate %s groups" % V3_GROUP_KEY)
        if matches[0] != source_group:
            raise RuntimeError("live %s group drift: existing value differs" % V3_GROUP_KEY)
        return "unchanged", live_bytes

    source_order = source_group.get("order")
    insert_at = len(navigation)
    if isinstance(source_order, (int, float)) and not isinstance(source_order, bool):
        for index, group in enumerate(navigation):
            group_order = group.get("order")
            if (
                isinstance(group_order, (int, float))
                and not isinstance(group_order, bool)
                and group_order > source_order
            ):
                insert_at = index
                break
    navigation.insert(insert_at, copy.deepcopy(source_group))
    merged_bytes = serialize_navigation(navigation)
    # Verify the emitted bytes before they are allowed near the live path.
    verified = parse_navigation(merged_bytes, "merged navigation")
    merged_matches = [group for group in verified if group.get("key") == V3_GROUP_KEY]
    if len(merged_matches) != 1 or merged_matches[0] != source_group:
        raise RuntimeError("merged navigation verification failed")
    return "would_change", merged_bytes


def validate_backup_root(backup_root, live_target):
    raw = Path(backup_root)
    if raw.is_symlink():
        raise RuntimeError("backup_root must not be a symbolic link")
    resolved = raw.resolve()
    if os.name != "nt" and Path(live_target) == DEFAULT_LIVE_TARGET:
        value = str(resolved)
        if value != "/mnt/data-disk" and not value.startswith("/mnt/data-disk/"):
            raise RuntimeError("production backup_root must be on /mnt/data-disk")
        mount = Path("/mnt/data-disk")
        if not mount.is_dir() or not os.path.ismount(str(mount)):
            raise RuntimeError("/mnt/data-disk is not mounted")
    ensure_directory(resolved, mode=0o700)
    if resolved.is_symlink() or not resolved.is_dir():
        raise RuntimeError("backup_root must be a regular non-symlink directory")
    os.chmod(str(resolved), 0o700)
    return resolved


@contextmanager
def exclusive_lock(path):
    path = Path(path)
    handle = open(path, "a+b")
    locked = False
    try:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                raise RuntimeError("navigation deployment lock busy")
        elif msvcrt is not None:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("navigation deployment lock busy")
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


def _new_checkpoint(backup_root):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return backup_root / ("ad-control-v3-navigation-%s-%s" % (stamp, uuid.uuid4().hex[:8]))


def apply_navigation(repo_root, live_target=DEFAULT_LIVE_TARGET, backup_root=None, check=False):
    source = source_navigation_group(repo_root)
    live_target = require_regular_file(Path(live_target), "live navigation")
    live_bytes = live_target.read_bytes()
    state, merged_bytes = merge_navigation(live_bytes, source["group"])
    if state == "unchanged":
        return {
            "status": "unchanged",
            "checkpoint": "",
            "before_sha256": sha256_bytes(live_bytes),
            "after_sha256": sha256_bytes(live_bytes),
            "source_commit": source["commit"],
        }
    if check:
        return {
            "status": "would_change",
            "checkpoint": "",
            "before_sha256": sha256_bytes(live_bytes),
            "after_sha256": sha256_bytes(merged_bytes),
            "source_commit": source["commit"],
        }
    if not backup_root:
        raise RuntimeError("backup_root on the data disk is required")
    backup_root = validate_backup_root(backup_root, live_target)
    lock_path = backup_root / ".ad-control-v3-navigation.lock"
    with exclusive_lock(lock_path):
        # Re-evaluate under the shared deployer lock before creating a checkpoint.
        require_regular_file(live_target, "live navigation")
        current_bytes = live_target.read_bytes()
        current_state, current_merged = merge_navigation(current_bytes, source["group"])
        if current_state == "unchanged":
            return {
                "status": "unchanged",
                "checkpoint": "",
                "before_sha256": sha256_bytes(current_bytes),
                "after_sha256": sha256_bytes(current_bytes),
                "source_commit": source["commit"],
            }

        checkpoint = _new_checkpoint(backup_root)
        checkpoint.mkdir(mode=0o700)
        fsync_directory(backup_root)
        backup_path = checkpoint / "navigation.before.json"
        manifest_path = checkpoint / "manifest.json"
        create_exclusive_file(backup_path, current_bytes)
        before_sha256 = sha256_bytes(current_bytes)
        after_sha256 = sha256_bytes(current_merged)
        manifest = {
            "version": CHECKPOINT_VERSION,
            "operation": "ad_control_v3_navigation",
            "created_at": utc_now(),
            "source_commit": source["commit"],
            "source_path": SOURCE_RELATIVE_PATH.as_posix(),
            "source_group_key": V3_GROUP_KEY,
            "source_group_sha256": source["group_sha256"],
            "live_target": str(live_target.resolve()),
            "backup_file": backup_path.name,
            "backup_sha256": before_sha256,
            "before_sha256": before_sha256,
            "after_sha256": after_sha256,
            "before_mode": stat.S_IMODE(live_target.stat().st_mode),
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        create_exclusive_file(manifest_path, manifest_bytes)
        fsync_directory(checkpoint)

        # Refuse a writer that raced checkpoint creation, even if it ignores our lock.
        if live_target.read_bytes() != current_bytes:
            raise RuntimeError("live navigation changed after checkpoint creation")
        try:
            atomic_write(live_target, current_merged, int(manifest["before_mode"]))
            if live_target.read_bytes() != current_merged:
                raise RuntimeError("installed navigation checksum mismatch")
        except Exception as install_error:
            current = live_target.read_bytes() if live_target.is_file() else None
            if current == current_merged:
                atomic_write(live_target, current_bytes, int(manifest["before_mode"]))
            elif current != current_bytes:
                raise RuntimeError(
                    "navigation install failed and automatic rollback was blocked by drift"
                ) from install_error
            raise
    return {
        "status": "changed",
        "checkpoint": str(manifest_path),
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "source_commit": source["commit"],
    }


def load_checkpoint(checkpoint):
    manifest_path = require_regular_file(Path(checkpoint), "checkpoint manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("checkpoint manifest is invalid JSON: %s" % exc)
    if not isinstance(manifest, dict):
        raise RuntimeError("checkpoint manifest must be an object")
    if (
        manifest.get("version") != CHECKPOINT_VERSION
        or manifest.get("operation") != "ad_control_v3_navigation"
        or manifest.get("source_group_key") != V3_GROUP_KEY
    ):
        raise RuntimeError("checkpoint manifest type is not supported")
    backup_name = manifest.get("backup_file")
    if not isinstance(backup_name, str) or Path(backup_name).name != backup_name:
        raise RuntimeError("checkpoint backup_file is invalid")
    backup_path = require_regular_file(manifest_path.parent / backup_name, "checkpoint backup")
    backup_bytes = backup_path.read_bytes()
    before_sha256 = sha256_bytes(backup_bytes)
    if before_sha256 != manifest.get("backup_sha256") or before_sha256 != manifest.get(
        "before_sha256"
    ):
        raise RuntimeError("checkpoint backup checksum mismatch")
    after_sha256 = manifest.get("after_sha256")
    if not isinstance(after_sha256, str) or len(after_sha256) != 64:
        raise RuntimeError("checkpoint after_sha256 is invalid")
    before_mode = manifest.get("before_mode")
    if not isinstance(before_mode, int) or before_mode < 0 or before_mode > 0o7777:
        raise RuntimeError("checkpoint before_mode is invalid")
    return manifest_path, manifest, backup_bytes


def rollback_navigation(checkpoint, live_target=None, check=False):
    manifest_path, manifest, backup_bytes = load_checkpoint(checkpoint)
    expected_target = Path(manifest.get("live_target", ""))
    if not expected_target.is_absolute():
        raise RuntimeError("checkpoint live_target is invalid")
    target = Path(live_target) if live_target is not None else expected_target
    if target.resolve() != expected_target.resolve():
        raise RuntimeError("rollback live_target does not match checkpoint")
    target = require_regular_file(target, "live navigation")
    current_bytes = target.read_bytes()
    current_sha256 = sha256_bytes(current_bytes)
    if current_sha256 != manifest["after_sha256"]:
        raise RuntimeError(
            "rollback blocked by current live drift: expected=%s actual=%s"
            % (manifest["after_sha256"], current_sha256)
        )
    if check:
        return {
            "status": "would_rollback",
            "checkpoint": str(manifest_path),
            "before_sha256": manifest["before_sha256"],
            "after_sha256": manifest["after_sha256"],
            "source_commit": manifest["source_commit"],
        }
    lock_path = manifest_path.parent.parent / ".ad-control-v3-navigation.lock"
    with exclusive_lock(lock_path):
        require_regular_file(target, "live navigation")
        locked_bytes = target.read_bytes()
        locked_sha256 = sha256_bytes(locked_bytes)
        if locked_sha256 != manifest["after_sha256"]:
            raise RuntimeError(
                "rollback blocked by current live drift: expected=%s actual=%s"
                % (manifest["after_sha256"], locked_sha256)
            )
        atomic_write(target, backup_bytes, int(manifest["before_mode"]))
        if target.read_bytes() != backup_bytes:
            raise RuntimeError("navigation rollback verification failed")
    return {
        "status": "rolled_back",
        "checkpoint": str(manifest_path),
        "before_sha256": manifest["before_sha256"],
        "after_sha256": manifest["after_sha256"],
        "source_commit": manifest["source_commit"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--live-target", default=str(DEFAULT_LIVE_TARGET))
    parser.add_argument("--backup-root", default="")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--rollback", metavar="CHECKPOINT", default="")
    args = parser.parse_args(argv)
    try:
        if args.rollback:
            result = rollback_navigation(
                checkpoint=args.rollback,
                live_target=args.live_target,
                check=args.check,
            )
        else:
            if not args.repo_root:
                raise RuntimeError("--repo-root is required for check/apply")
            result = apply_navigation(
                repo_root=args.repo_root,
                live_target=args.live_target,
                backup_root=args.backup_root or None,
                check=args.check,
            )
        print(
            "%s source_commit=%s before_sha256=%s after_sha256=%s checkpoint=%s"
            % (
                result["status"],
                result["source_commit"],
                result["before_sha256"],
                result["after_sha256"],
                result["checkpoint"],
            )
        )
    except Exception as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
