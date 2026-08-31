#!/usr/bin/env python3
"""Archive the retired US drama file history without mutating its sources.

The command is deliberately fixed to one host, two source directories and one
private directory on /data.  With no flag it performs a read-only dry run.
``--apply`` copies into a unique private staging directory and atomically
publishes it as ``archive``.  ``--verify`` only compares the live sources with
the published payload and its signed-by-content metadata.
"""
import argparse
import ctypes
import errno
import hashlib
import json
import math
import os
import pathlib
import platform
import re
import secrets
import socket
import stat
import subprocess
import sys
import time


sys.dont_write_bytecode = True

RUN_ID = "gpu-service-migration-20260828T1502"
EXPECTED_HOST = "VM-0-13-centos"
DATA_ROOT = pathlib.Path("/data")
BASE = DATA_ROOT / "migrations" / RUN_ID / "drama-history"
ARCHIVE = BASE / "archive"
PAYLOAD_NAME = "payload"
MANIFEST_NAME = "manifest.json"
RECEIPT_NAME = "receipt.json"
POST_COMMIT_STATE_NAME = "post-commit-state.json"
POST_COMMIT_FAILURE_NAME = "post-commit-failure.json"
POST_COMMIT_ERROR_TYPES = frozenset((
    "OSError", "BlockingIOError", "FileExistsError", "FileNotFoundError",
    "InterruptedError", "IsADirectoryError", "NotADirectoryError",
    "PermissionError", "TimeoutError", "RuntimeError",
))
SOURCE_SPECS = (
    ("root-drama_material_jobs", pathlib.Path("/root/drama_material_jobs")),
    ("usr-share-nginx-html-drama-materials",
     pathlib.Path("/usr/share/nginx/html/drama-materials")),
)
MIN_FREE_BYTES = 30 * 1024 * 1024 * 1024
POST_COMMIT_EVIDENCE_HEADROOM = 1024 * 1024
COPY_OVERHEAD_PER_ENTRY = 1024 * 1024
COPY_FIXED_OVERHEAD = 16 * 1024 * 1024
MAX_METADATA_BYTES = 128 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024


class PosixTreeIO(object):
    """All approved-source traversal is relative to already-open directory fds."""

    @staticmethod
    def directory_flags():
        required = ("O_DIRECTORY", "O_NOFOLLOW")
        if any(not hasattr(os, name) for name in required):
            raise RuntimeError("directory-fd no-follow traversal is unavailable")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOATIME", 0)
        return flags

    @staticmethod
    def file_flags():
        if not hasattr(os, "O_NOFOLLOW"):
            raise RuntimeError("file-fd no-follow traversal is unavailable")
        flags = os.O_RDONLY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOATIME", 0)
        return flags

    def open_root(self, path):
        return os.open(str(path), self.directory_flags())

    def listdir(self, directory_fd):
        return os.listdir(directory_fd)

    def stat_child(self, directory_fd, name):
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)

    def open_directory_child(self, directory_fd, name):
        return os.open(name, self.directory_flags(), dir_fd=directory_fd)

    def open_file_child(self, directory_fd, name):
        return os.open(name, self.file_flags(), dir_fd=directory_fd)

    def fstat(self, descriptor):
        return os.fstat(descriptor)

    def mount_id(self, descriptor):
        path = "/proc/self/fdinfo/%d" % descriptor
        try:
            with open(path, "r") as handle:
                values = [line.split(":", 1)[1].strip() for line in handle
                          if line.startswith("mnt_id:")]
        except OSError:
            raise RuntimeError("cannot read directory-fd mount identity")
        if len(values) != 1 or not values[0].isdigit():
            raise RuntimeError("directory-fd mount identity is missing or ambiguous")
        return int(values[0])

    def read(self, descriptor, size):
        return os.read(descriptor, size)

    def close(self, descriptor):
        os.close(descriptor)


TREE_IO = PosixTreeIO()


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def fsync_directory(path):
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_private_json_exclusive(path, value):
    encoded = json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    write_private_bytes_exclusive(path, encoded)
    fsync_directory(path.parent)
    return encoded


def write_private_bytes_exclusive(path, encoded):
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, "O_BINARY", 0), 0o600)
    try:
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise RuntimeError("private metadata write made no progress")
            offset += written
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def lstat_kind(path):
    info = os.lstat(str(path))
    if stat.S_ISLNK(info.st_mode):
        return "symlink", info
    if stat.S_ISDIR(info.st_mode):
        return "directory", info
    if stat.S_ISREG(info.st_mode):
        return "file", info
    return "special", info


def assert_host():
    if socket.gethostname() != EXPECTED_HOST:
        raise RuntimeError("US drama history archive is bound to the approved source host")
    if hasattr(os, "geteuid"):
        if (os.geteuid() != 0 or not hasattr(os, "getegid") or
                os.getegid() != 0):
            raise RuntimeError("US drama history archive must run with root user and group")


def findmnt_target(path):
    process = subprocess.run(
        ["findmnt", "-n", "-o", "TARGET", "-T", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if process.returncode:
        raise RuntimeError("cannot prove the /data mount identity")
    return process.stdout.strip()


def ancestry_from_data(path):
    try:
        relative = path.relative_to(DATA_ROOT)
    except ValueError:
        raise RuntimeError("drama history path escaped /data")
    current = DATA_ROOT
    result = [current]
    for part in relative.parts:
        current = current / part
        result.append(current)
    return result


def validate_no_symlink_ancestry(path):
    for member in ancestry_from_data(path):
        if not os.path.lexists(str(member)):
            continue
        kind, _ = lstat_kind(member)
        if kind == "symlink":
            raise RuntimeError("drama history path ancestry contains a symlink")
        if kind != "directory":
            raise RuntimeError("drama history path ancestry contains a non-directory")


def create_private_ancestry(path):
    members = ancestry_from_data(path)
    for member in members[1:]:
        if os.path.lexists(str(member)):
            kind, _ = lstat_kind(member)
            if kind == "symlink":
                raise RuntimeError("drama history path ancestry contains a symlink")
            if kind != "directory":
                raise RuntimeError("drama history path ancestry contains a non-directory")
            continue
        parent = member.parent
        os.mkdir(str(member), 0o700)
        os.chmod(str(member), 0o700)
        fsync_directory(parent)
    validate_no_symlink_ancestry(path)


def free_bytes(path):
    values = os.statvfs(str(path))
    return int(values.f_bavail) * int(values.f_frsize)


def storage_guard(create=False, require_reserve=False):
    if not DATA_ROOT.is_absolute() or not BASE.is_absolute():
        raise RuntimeError("drama history paths must be absolute")
    if not os.path.lexists(str(DATA_ROOT)):
        raise RuntimeError("/data is missing")
    kind, _ = lstat_kind(DATA_ROOT)
    if kind != "directory" or DATA_ROOT.is_symlink():
        raise RuntimeError("/data is not a real directory")
    if not os.path.ismount(str(DATA_ROOT)):
        raise RuntimeError("/data is not a real mount point")
    if findmnt_target(DATA_ROOT) != str(DATA_ROOT):
        raise RuntimeError("/data mount identity is not exact")
    validate_no_symlink_ancestry(BASE)
    if create:
        create_private_ancestry(BASE)
        if findmnt_target(BASE) != str(DATA_ROOT):
            raise RuntimeError("drama history target is not on /data")
    else:
        probe = BASE
        while not os.path.lexists(str(probe)):
            probe = probe.parent
        if findmnt_target(probe) != str(DATA_ROOT):
            raise RuntimeError("drama history target parent is not on /data")
    available = free_bytes(DATA_ROOT)
    if require_reserve and available < MIN_FREE_BYTES:
        raise RuntimeError("/data free space is below the required 30 GiB reserve")
    return available


def mode_bits(info):
    return stat.S_IMODE(info.st_mode)


def set_mtime_nofollow(path, value):
    try:
        os.utime(str(path), ns=(value, value), follow_symlinks=False)
    except NotImplementedError:
        # Windows unit tests lack follow_symlinks for utime.  The destination
        # lives below a newly-created private staging tree and is lstat-checked
        # immediately before this portable fallback.  Production Linux uses
        # the no-follow branch above.
        kind, _ = lstat_kind(path)
        if kind == "symlink":
            raise RuntimeError("archive destination changed into a symlink")
        os.utime(str(path), ns=(value, value))


def mtime_ns(info):
    value = getattr(info, "st_mtime_ns", None)
    if value is not None:
        return int(value)
    return int(info.st_mtime * 1000000000)


def ctime_ns(info):
    value = getattr(info, "st_ctime_ns", None)
    if value is not None:
        return int(value)
    return int(info.st_ctime * 1000000000)


def directory_runtime_identity(info):
    identity = (int(info.st_dev), int(info.st_ino), mode_bits(info), mtime_ns(info))
    # The approved host is POSIX/Linux.  Windows tests use a path-backed test
    # double whose CRT updates ctime merely by opening a file descriptor.
    if os.name == "posix":
        identity += (ctime_ns(info),)
    return identity


def file_runtime_identity(info):
    return directory_runtime_identity(info) + (int(info.st_size),)


def validate_private_info(info, kind, message):
    expected_mode = 0o700 if kind == "directory" else 0o600
    actual_kind = "directory" if stat.S_ISDIR(info.st_mode) else (
        "file" if stat.S_ISREG(info.st_mode) else "other")
    if actual_kind != kind:
        raise RuntimeError(message)
    # Windows is used only by the path-backed unit-test double.  The approved
    # Linux host enforces the exact POSIX owner and permission contract.
    if os.name == "posix" and (mode_bits(info) != expected_mode or
                               int(info.st_uid) != 0 or int(info.st_gid) != 0):
        raise RuntimeError(message)


def validate_private_path(path, kind, message):
    info = os.lstat(str(path))
    if stat.S_ISLNK(info.st_mode):
        raise RuntimeError(message)
    validate_private_info(info, kind, message)
    return info


def finite_positive_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(converted) and converted > 0


def assert_directory_info_matches(first, second, message):
    if (not stat.S_ISDIR(first.st_mode) or not stat.S_ISDIR(second.st_mode) or
            directory_runtime_identity(first) != directory_runtime_identity(second)):
        raise RuntimeError(message)


def assert_file_info_matches(first, second, message):
    if (not stat.S_ISREG(first.st_mode) or not stat.S_ISREG(second.st_mode) or
            file_runtime_identity(first) != file_runtime_identity(second)):
        raise RuntimeError(message)


def open_root_anchor(path):
    path = pathlib.Path(path)
    before = os.lstat(str(path))
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise RuntimeError("source or archive root is not a no-follow directory")
    descriptor = TREE_IO.open_root(path)
    try:
        opened = TREE_IO.fstat(descriptor)
        assert_directory_info_matches(
            before, opened, "source or archive root changed while it was opened")
        mount_id = TREE_IO.mount_id(descriptor)
    except Exception:
        TREE_IO.close(descriptor)
        raise
    return {"path": path, "descriptor": descriptor, "mount_id": mount_id}


def close_root_anchor(anchor):
    TREE_IO.close(anchor["descriptor"])


def verify_root_anchor_path(anchor, expected):
    opened = TREE_IO.fstat(anchor["descriptor"])
    assert_directory_info_matches(
        expected, opened, "source or archive root fd changed during traversal")
    current = os.lstat(str(anchor["path"]))
    assert_directory_info_matches(
        opened, current, "source or archive root path was replaced during traversal")
    current_fd = TREE_IO.open_root(anchor["path"])
    try:
        current_opened = TREE_IO.fstat(current_fd)
        assert_directory_info_matches(
            opened, current_opened,
            "source or archive root path identity changed during traversal")
        if TREE_IO.mount_id(current_fd) != anchor["mount_id"]:
            raise RuntimeError("source or archive root mount was replaced during traversal")
    finally:
        TREE_IO.close(current_fd)


def stable_file_record_handle(descriptor, relative_path, before):
    digest = hashlib.sha256()
    opened = TREE_IO.fstat(descriptor)
    assert_file_info_matches(
        before, opened, "source file identity changed during manifest scan")
    while True:
        block = TREE_IO.read(descriptor, CHUNK_SIZE)
        if not block:
            break
        digest.update(block)
    after = TREE_IO.fstat(descriptor)
    assert_file_info_matches(
        opened, after, "source file changed during manifest scan")
    return {"relative_path": relative_path, "kind": "file",
            "sha256": digest.hexdigest(), "size": int(after.st_size),
            "mtime_ns": mtime_ns(after), "mode": mode_bits(after)}


def validate_child_name(name):
    if (not isinstance(name, str) or not name or name in (".", "..") or
            "/" in name or "\x00" in name):
        raise RuntimeError("directory-fd traversal returned an unsafe child name")


def manifest_tree_from_anchor(anchor, private_archive=False):
    root_info = TREE_IO.fstat(anchor["descriptor"])
    if not stat.S_ISDIR(root_info.st_mode):
        raise RuntimeError("source or archive root fd is not a directory")
    if private_archive:
        validate_private_info(
            root_info, "directory", "archive directory owner or mode is unsafe")
    entries = [{"relative_path": ".", "kind": "directory",
                "mtime_ns": mtime_ns(root_info), "mode": mode_bits(root_info)}]

    def visit(directory_fd, relative_parts):
        directory_before = TREE_IO.fstat(directory_fd)
        if not stat.S_ISDIR(directory_before.st_mode):
            raise RuntimeError("source or archive directory fd changed type during scan")
        try:
            names = sorted(TREE_IO.listdir(directory_fd))
        except OSError:
            raise RuntimeError("cannot scan a source or archive directory")
        if len(names) != len(set(names)):
            raise RuntimeError("directory-fd traversal returned duplicate child names")
        for name in names:
            validate_child_name(name)
            child_parts = relative_parts + (name,)
            relative = pathlib.PurePosixPath(*child_parts).as_posix()
            info = TREE_IO.stat_child(directory_fd, name)
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError("source or archive tree contains a symlink")
            if stat.S_ISDIR(info.st_mode):
                if private_archive:
                    validate_private_info(
                        info, "directory", "archive directory owner or mode is unsafe")
                entries.append({"relative_path": relative, "kind": "directory",
                                "mtime_ns": mtime_ns(info), "mode": mode_bits(info)})
                child_fd = TREE_IO.open_directory_child(directory_fd, name)
                try:
                    opened = TREE_IO.fstat(child_fd)
                    assert_directory_info_matches(
                        info, opened, "source directory changed while it was opened")
                    if TREE_IO.mount_id(child_fd) != anchor["mount_id"]:
                        raise RuntimeError("source or archive tree crosses a mount boundary")
                    visit(child_fd, child_parts)
                finally:
                    TREE_IO.close(child_fd)
                current = TREE_IO.stat_child(directory_fd, name)
                assert_directory_info_matches(
                    info, current, "source directory name was replaced during scan")
            elif stat.S_ISREG(info.st_mode):
                if private_archive:
                    validate_private_info(
                        info, "file", "archive file owner or mode is unsafe")
                child_fd = TREE_IO.open_file_child(directory_fd, name)
                try:
                    if TREE_IO.mount_id(child_fd) != anchor["mount_id"]:
                        raise RuntimeError("source or archive tree crosses a mount boundary")
                    entries.append(stable_file_record_handle(child_fd, relative, info))
                finally:
                    TREE_IO.close(child_fd)
                current = TREE_IO.stat_child(directory_fd, name)
                assert_file_info_matches(
                    info, current, "source file name was replaced during scan")
            else:
                raise RuntimeError("source or archive tree contains a special file")
        directory_after = TREE_IO.fstat(directory_fd)
        assert_directory_info_matches(
            directory_before, directory_after,
            "source or archive directory changed during scan")

    visit(anchor["descriptor"], ())
    verify_root_anchor_path(anchor, root_info)
    entries.sort(key=lambda item: item["relative_path"])
    return entries


def manifest_tree(physical_root):
    anchor = open_root_anchor(physical_root)
    try:
        return manifest_tree_from_anchor(anchor)
    finally:
        close_root_anchor(anchor)


def open_root_anchors(root_by_id=None):
    if root_by_id is None:
        root_by_id = {source_id: path for source_id, path in SOURCE_SPECS}
    expected_ids = [source_id for source_id, _ in SOURCE_SPECS]
    if sorted(root_by_id) != sorted(expected_ids):
        raise RuntimeError("drama history source scope changed")
    anchors = {}
    try:
        for source_id, _ in SOURCE_SPECS:
            anchors[source_id] = open_root_anchor(pathlib.Path(root_by_id[source_id]))
    except Exception:
        for anchor in anchors.values():
            close_root_anchor(anchor)
        raise
    return anchors


def close_root_anchors(anchors):
    first_error = None
    for source_id, _ in reversed(SOURCE_SPECS):
        anchor = anchors.pop(source_id, None)
        if anchor is None:
            continue
        try:
            close_root_anchor(anchor)
        except Exception as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


def build_manifest_from_anchors(anchors, private_archive=False):
    expected_ids = [source_id for source_id, _ in SOURCE_SPECS]
    if sorted(anchors) != sorted(expected_ids):
        raise RuntimeError("drama history source anchor scope changed")
    sources = []
    file_count = 0
    directory_count = 0
    total_bytes = 0
    for source_id, source_path in SOURCE_SPECS:
        entries = manifest_tree_from_anchor(anchors[source_id], private_archive)
        file_count += sum(1 for item in entries if item["kind"] == "file")
        directory_count += sum(1 for item in entries if item["kind"] == "directory")
        total_bytes += sum(item.get("size", 0) for item in entries)
        sources.append({"source_id": source_id, "source_path": str(source_path),
                        "entries": entries})
    identity = {"format_version": 1, "run_id": RUN_ID, "sources": sources,
                "file_count": file_count, "directory_count": directory_count,
                "total_bytes": total_bytes}
    identity["fingerprint_sha256"] = sha256_bytes(canonical_bytes(identity))
    return identity


def build_manifest(root_by_id=None, private_archive=False):
    anchors = open_root_anchors(root_by_id)
    try:
        return build_manifest_from_anchors(anchors, private_archive)
    finally:
        close_root_anchors(anchors)


def manifest_without_fingerprint(manifest):
    value = dict(manifest)
    value.pop("fingerprint_sha256", None)
    return value


def validate_manifest(manifest):
    if not isinstance(manifest, dict):
        raise RuntimeError("archive manifest is not an object")
    if manifest.get("format_version") != 1 or manifest.get("run_id") != RUN_ID:
        raise RuntimeError("archive manifest identity changed")
    expected = sha256_bytes(canonical_bytes(manifest_without_fingerprint(manifest)))
    if manifest.get("fingerprint_sha256") != expected:
        raise RuntimeError("archive manifest fingerprint is invalid")
    expected_ids = [source_id for source_id, _ in SOURCE_SPECS]
    sources = manifest.get("sources")
    if (not isinstance(sources, list) or
            [item.get("source_id") for item in sources if isinstance(item, dict)] != expected_ids):
        raise RuntimeError("archive manifest source scope changed")
    for item, (_, path) in zip(sources, SOURCE_SPECS):
        if item.get("source_path") != str(path) or not isinstance(item.get("entries"), list):
            raise RuntimeError("archive manifest source identity changed")


def estimate_copy_bytes(manifest):
    entries = int(manifest["file_count"]) + int(manifest["directory_count"])
    return (int(manifest["total_bytes"]) + entries * COPY_OVERHEAD_PER_ENTRY +
            COPY_FIXED_OVERHEAD)


def ensure_copy_capacity(manifest, available):
    required = estimate_copy_bytes(manifest)
    if available - required < MIN_FREE_BYTES:
        raise RuntimeError("archive copy would violate the required 30 GiB reserve")
    return required


def entry_destination_path(root, relative):
    if relative == ".":
        return root
    pure = pathlib.PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        raise RuntimeError("manifest contains an unsafe relative path")
    return root.joinpath(*pure.parts)


def stat_matches_record(info, record):
    expected_kind = record["kind"]
    actual_kind = "file" if stat.S_ISREG(info.st_mode) else (
        "directory" if stat.S_ISDIR(info.st_mode) else "other")
    if actual_kind != expected_kind:
        return False
    if mode_bits(info) != record["mode"] or mtime_ns(info) != record["mtime_ns"]:
        return False
    if expected_kind == "file" and int(info.st_size) != record["size"]:
        return False
    return True


def copy_regular_file_handle(source_fd, destination, expected, before):
    destination_fd = None
    digest = hashlib.sha256()
    try:
        opened = TREE_IO.fstat(source_fd)
        assert_file_info_matches(
            before, opened, "source file identity changed before copy")
        if not stat_matches_record(opened, expected):
            raise RuntimeError("source file identity changed before copy")
        destination_fd = os.open(
            str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
            getattr(os, "O_BINARY", 0), 0o600)
        while True:
            block = TREE_IO.read(source_fd, CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
            offset = 0
            while offset < len(block):
                written = os.write(destination_fd, block[offset:])
                if written <= 0:
                    raise RuntimeError("archive payload write made no progress")
                offset += written
        after = TREE_IO.fstat(source_fd)
        assert_file_info_matches(
            opened, after, "source file changed during copy")
        if (not stat_matches_record(after, expected) or
                digest.hexdigest() != expected["sha256"]):
            raise RuntimeError("source file changed during copy")
        os.fchmod(destination_fd, 0o600)
        os.fsync(destination_fd)
        set_mtime_nofollow(destination, expected["mtime_ns"])
        os.fsync(destination_fd)
    finally:
        if destination_fd is not None:
            os.close(destination_fd)


def open_manifest_directory(anchor, parts, records):
    current = anchor["descriptor"]
    opened_descriptors = []
    accumulated = []
    try:
        for name in parts:
            validate_child_name(name)
            accumulated.append(name)
            relative = pathlib.PurePosixPath(*accumulated).as_posix()
            record = records.get(relative)
            if not record or record.get("kind") != "directory":
                raise RuntimeError("manifest directory ancestry is incomplete")
            info = TREE_IO.stat_child(current, name)
            if stat.S_ISLNK(info.st_mode) or not stat_matches_record(info, record):
                raise RuntimeError("source directory changed before copy")
            child_fd = TREE_IO.open_directory_child(current, name)
            try:
                opened = TREE_IO.fstat(child_fd)
                assert_directory_info_matches(
                    info, opened, "source directory changed while opening for copy")
                if TREE_IO.mount_id(child_fd) != anchor["mount_id"]:
                    raise RuntimeError("source tree crosses a mount boundary during copy")
            except Exception:
                TREE_IO.close(child_fd)
                raise
            opened_descriptors.append(child_fd)
            current = child_fd
        return current, opened_descriptors
    except Exception:
        for descriptor in reversed(opened_descriptors):
            TREE_IO.close(descriptor)
        raise


def close_manifest_directories(descriptors):
    for descriptor in reversed(descriptors):
        TREE_IO.close(descriptor)


def copy_source(anchor, destination_root, source_manifest):
    if os.path.lexists(str(destination_root)):
        raise RuntimeError("refuse to overwrite an archive payload root")
    records = {item.get("relative_path"): item for item in source_manifest["entries"]}
    if len(records) != len(source_manifest["entries"]) or "." not in records:
        raise RuntimeError("source manifest paths are missing or duplicated")
    root_before = TREE_IO.fstat(anchor["descriptor"])
    if not stat_matches_record(root_before, records["."]):
        raise RuntimeError("source root changed before copy")
    verify_root_anchor_path(anchor, root_before)
    os.mkdir(str(destination_root), 0o700)
    directories = [item for item in source_manifest["entries"] if item["kind"] == "directory"]
    files = [item for item in source_manifest["entries"] if item["kind"] == "file"]
    for record in sorted((item for item in directories if item["relative_path"] != "."),
                         key=lambda item: (len(pathlib.PurePosixPath(item["relative_path"]).parts),
                                           item["relative_path"])):
        target = entry_destination_path(destination_root, record["relative_path"])
        if os.path.lexists(str(target)):
            raise RuntimeError("refuse to overwrite an archive payload directory")
        os.mkdir(str(target), 0o700)
    # Open every source directory by components below the held root fd.  This
    # validates empty directories as well as ancestors used by regular files.
    for record in directories:
        relative = record["relative_path"]
        parts = () if relative == "." else pathlib.PurePosixPath(relative).parts
        directory_fd, opened_descriptors = open_manifest_directory(anchor, parts, records)
        try:
            current = TREE_IO.fstat(directory_fd)
            if not stat_matches_record(current, record):
                raise RuntimeError("source directory changed before copy")
        finally:
            close_manifest_directories(opened_descriptors)
    for record in files:
        pure = pathlib.PurePosixPath(record["relative_path"])
        if pure.is_absolute() or not pure.parts:
            raise RuntimeError("source file manifest path is unsafe")
        parent_fd, opened_descriptors = open_manifest_directory(
            anchor, pure.parts[:-1], records)
        source_fd = None
        try:
            name = pure.parts[-1]
            validate_child_name(name)
            before = TREE_IO.stat_child(parent_fd, name)
            if stat.S_ISLNK(before.st_mode) or not stat_matches_record(before, record):
                raise RuntimeError("source file changed before copy")
            source_fd = TREE_IO.open_file_child(parent_fd, name)
            opened = TREE_IO.fstat(source_fd)
            assert_file_info_matches(
                before, opened, "source file changed while opening for copy")
            if TREE_IO.mount_id(source_fd) != anchor["mount_id"]:
                raise RuntimeError("source tree crosses a mount boundary during copy")
            destination = entry_destination_path(destination_root, record["relative_path"])
            copy_regular_file_handle(source_fd, destination, record, opened)
            current = TREE_IO.stat_child(parent_fd, name)
            assert_file_info_matches(
                before, current, "source file name was replaced during copy")
        finally:
            if source_fd is not None:
                TREE_IO.close(source_fd)
            close_manifest_directories(opened_descriptors)
    root_after = TREE_IO.fstat(anchor["descriptor"])
    assert_directory_info_matches(
        root_before, root_after, "source root changed during copy")
    verify_root_anchor_path(anchor, root_before)
    for record in sorted(directories,
                         key=lambda item: len(pathlib.PurePosixPath(item["relative_path"]).parts),
                         reverse=True):
        target = entry_destination_path(destination_root, record["relative_path"])
        current = os.lstat(str(target))
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
            raise RuntimeError("archive payload directory changed type")
        os.chmod(str(target), 0o700)
        set_mtime_nofollow(target, record["mtime_ns"])
        fsync_directory(target)


def allocate_staging():
    for _ in range(128):
        name = ".staging-%d-%d-%s" % (int(time.time() * 1000000), os.getpid(),
                                       secrets.token_hex(8))
        staging = BASE / name
        try:
            os.mkdir(str(staging), 0o700)
        except FileExistsError:
            continue
        os.chmod(str(staging), 0o700)
        fsync_directory(BASE)
        return staging
    raise RuntimeError("cannot allocate a unique private drama archive staging directory")


def build_archive_manifest(payload):
    validate_private_path(
        payload, "directory", "archive payload owner or mode is unsafe")
    roots = {source_id: payload / source_id for source_id, _ in SOURCE_SPECS}
    return build_manifest(roots, private_archive=True)


def validate_archive_matches_source(archive_manifest, source_manifest):
    normalized = json.loads(json.dumps(archive_manifest))
    archive_sources = normalized.get("sources")
    source_sources = source_manifest.get("sources")
    if (not isinstance(archive_sources, list) or not isinstance(source_sources, list) or
            len(archive_sources) != len(source_sources)):
        raise RuntimeError("archive payload source scope differs from its source manifest")
    for archive_source, source_source in zip(archive_sources, source_sources):
        archive_entries = archive_source.get("entries")
        source_entries = source_source.get("entries")
        if (not isinstance(archive_entries, list) or not isinstance(source_entries, list) or
                len(archive_entries) != len(source_entries)):
            raise RuntimeError("archive payload entries differ from its source manifest")
        for archive_entry, source_entry in zip(archive_entries, source_entries):
            if (archive_entry.get("relative_path") != source_entry.get("relative_path") or
                    archive_entry.get("kind") != source_entry.get("kind")):
                raise RuntimeError("archive payload paths differ from its source manifest")
            archive_entry["mode"] = source_entry.get("mode")
    normalized.pop("fingerprint_sha256", None)
    normalized["fingerprint_sha256"] = sha256_bytes(canonical_bytes(normalized))
    if normalized != source_manifest:
        raise RuntimeError("archive payload content differs from its source manifest")


def read_private_json(path):
    kind, info = lstat_kind(path)
    if kind != "file" or info.st_size > MAX_METADATA_BYTES:
        raise RuntimeError("archive metadata is missing, unsafe or unexpectedly large")
    validate_private_info(info, "file", "archive metadata owner or mode is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NOATIME"):
        flags |= os.O_NOATIME
    descriptor = os.open(str(path), flags)
    blocks = []
    length = 0
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_dev != info.st_dev or
                opened.st_ino != info.st_ino or mode_bits(opened) != mode_bits(info) or
                int(opened.st_size) != int(info.st_size) or
                mtime_ns(opened) != mtime_ns(info)):
            raise RuntimeError("archive metadata identity changed during open")
        while length <= MAX_METADATA_BYTES:
            block = os.read(descriptor, min(CHUNK_SIZE, MAX_METADATA_BYTES + 1 - length))
            if not block:
                break
            blocks.append(block)
            length += len(block)
        after = os.fstat(descriptor)
        if (after.st_dev != opened.st_dev or after.st_ino != opened.st_ino or
                int(after.st_size) != int(opened.st_size) or
                mtime_ns(after) != mtime_ns(opened)):
            raise RuntimeError("archive metadata changed while being read")
    finally:
        os.close(descriptor)
    raw = b"".join(blocks)
    if len(raw) > MAX_METADATA_BYTES:
        raise RuntimeError("archive metadata is unexpectedly large")
    try:
        return json.loads(raw.decode("utf-8")), raw
    except (UnicodeDecodeError, ValueError):
        raise RuntimeError("archive metadata is not valid JSON")


def atomic_rename_noreplace(source, destination):
    if os.path.lexists(str(destination)):
        raise RuntimeError("refuse to overwrite the published drama archive")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    encoded_source = os.fsencode(str(source))
    encoded_destination = os.fsencode(str(destination))
    if renameat2 is not None:
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                              ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, encoded_source, -100, encoded_destination, 1)
    else:
        # CentOS releases can expose the kernel syscall while using a glibc
        # older than the renameat2 wrapper.  The approved US host is x86_64;
        # aarch64 is included for deterministic fail-closed portability.
        syscall_numbers = {"x86_64": 316, "amd64": 316, "aarch64": 276}
        number = syscall_numbers.get(platform.machine().lower())
        syscall = getattr(libc, "syscall", None)
        if number is None or syscall is None:
            raise RuntimeError("atomic no-replace rename is unavailable")
        syscall.restype = ctypes.c_long
        result = syscall(ctypes.c_long(number), ctypes.c_long(-100),
                         ctypes.c_char_p(encoded_source), ctypes.c_long(-100),
                         ctypes.c_char_p(encoded_destination), ctypes.c_uint(1))
    if result != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise RuntimeError("refuse to overwrite the published drama archive")
        raise RuntimeError("atomic no-replace drama archive publication failed")


def write_failure_evidence(staging, stage, error, initial_fingerprint=None,
                           copied_fingerprint=None, final_source_fingerprint=None):
    payload = {"run_id": RUN_ID, "host": EXPECTED_HOST, "result": "failed",
               "stage": stage, "error_type": type(error).__name__,
               "failed_at_epoch": time.time(),
               "source_writes_performed": False,
               "service_actions_performed": False}
    if initial_fingerprint:
        payload["initial_source_fingerprint_sha256"] = initial_fingerprint
    if copied_fingerprint:
        payload["copied_payload_fingerprint_sha256"] = copied_fingerprint
    if final_source_fingerprint:
        payload["final_source_fingerprint_sha256"] = final_source_fingerprint
    target = staging / "failure.json"
    if not os.path.lexists(str(target)):
        write_private_json_exclusive(target, payload)
    else:
        raise RuntimeError("refuse to overwrite private drama archive failure evidence")
    return target


def write_post_commit_failure(stage, error, initial_fingerprint):
    payload = {"format_version": 1, "run_id": RUN_ID, "host": EXPECTED_HOST,
               "result": "post_commit_failure", "stage": stage,
               "error_type": type(error).__name__, "failed_at_epoch": time.time(),
               "archive_path": str(ARCHIVE), "archive_published": True,
               "current_verify_required": True, "verified": False,
               "verification_status": "pending_current_read_only_verify",
               "source_fingerprint_sha256": initial_fingerprint,
               "source_writes_performed": False,
               "service_actions_performed": False}
    target = ARCHIVE / POST_COMMIT_FAILURE_NAME
    encoded = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    temporary = ARCHIVE / (".post-commit-failure-%d-%s.tmp" %
                           (os.getpid(), secrets.token_hex(8)))
    published = False
    try:
        write_private_bytes_exclusive(temporary, encoded)
        atomic_rename_noreplace(temporary, target)
        published = True
        try:
            fsync_directory(ARCHIVE)
            return target, "formal_file_and_parent_fsynced"
        except Exception:
            return target, "formal_file_parent_fsync_unconfirmed"
    except Exception as diagnostic_error:
        if not published and os.path.lexists(str(temporary)):
            try:
                os.unlink(str(temporary))
                try:
                    fsync_directory(ARCHIVE)
                except Exception:
                    pass
            except Exception:
                # An incomplete private temporary is never treated as formal
                # evidence and verify reports it only as a warning.
                pass
        return (ARCHIVE / POST_COMMIT_STATE_NAME,
                "prepublished_state_marker_only_%s" % type(diagnostic_error).__name__)


def dry_run():
    assert_host()
    available = storage_guard(create=False)
    manifest = build_manifest()
    required = ensure_copy_capacity(manifest, available)
    if os.path.lexists(str(ARCHIVE)):
        raise RuntimeError("published drama archive already exists; use --verify")
    return {"mode": "dry-run", "host": EXPECTED_HOST, "run_id": RUN_ID,
            "would_write": False, "source_count": len(SOURCE_SPECS),
            "file_count": manifest["file_count"],
            "directory_count": manifest["directory_count"],
            "total_bytes": manifest["total_bytes"],
            "source_fingerprint_sha256": manifest["fingerprint_sha256"],
            "available_bytes_before": available,
            "conservative_required_bytes": required,
            "minimum_free_bytes_after": MIN_FREE_BYTES,
            "archive_path": str(ARCHIVE)}


def apply_archive():
    assert_host()
    available = storage_guard(create=False)
    if os.path.lexists(str(ARCHIVE)):
        raise RuntimeError("refuse to overwrite the published drama archive")
    source_anchors = open_root_anchors()
    initial = None
    required = None
    available_before_copy = None
    staging = None
    stage = "scan-initial-sources"
    manifest_bytes = None
    copied_fingerprint = None
    final_source_fingerprint = None
    try:
        initial = build_manifest_from_anchors(source_anchors)
        required = ensure_copy_capacity(initial, available)
        available_before_copy = storage_guard(create=True)
        ensure_copy_capacity(initial, available_before_copy)
        if os.path.lexists(str(ARCHIVE)):
            raise RuntimeError("refuse to overwrite the published drama archive")
        staging = allocate_staging()
        stage = "allocate-staging"
        stage = "write-initial-manifest"
        manifest_bytes = write_private_json_exclusive(staging / MANIFEST_NAME, initial)
        payload = staging / PAYLOAD_NAME
        os.mkdir(str(payload), 0o700)
        fsync_directory(staging)
        stage = "copy-payload"
        initial_by_id = {item["source_id"]: item for item in initial["sources"]}
        for source_id, _ in SOURCE_SPECS:
            copy_source(source_anchors[source_id], payload / source_id,
                        initial_by_id[source_id])
        fsync_directory(payload)
        stage = "verify-copied-payload"
        copied = build_archive_manifest(payload)
        copied_fingerprint = copied.get("fingerprint_sha256")
        validate_archive_matches_source(copied, initial)
        stage = "rescan-live-sources"
        final_source = build_manifest_from_anchors(source_anchors)
        final_source_fingerprint = final_source.get("fingerprint_sha256")
        if final_source != initial:
            raise RuntimeError("live drama source manifest changed during archival copy")
        stage = "close-source-anchors"
        close_root_anchors(source_anchors)
        source_anchors = {}
        stage = "write-receipt"
        receipt = {"format_version": 1, "run_id": RUN_ID, "host": EXPECTED_HOST,
                   "result": "archived", "completed_at_epoch": time.time(),
                   "archive_path": str(ARCHIVE),
                   "manifest_file_sha256": sha256_bytes(manifest_bytes),
                   "source_fingerprint_sha256": initial["fingerprint_sha256"],
                   "file_count": initial["file_count"],
                   "directory_count": initial["directory_count"],
                   "total_bytes": initial["total_bytes"],
                   "available_bytes_before": available,
                   "available_bytes_before_copy": available_before_copy,
                   "conservative_required_bytes": required,
                   "minimum_free_bytes_after": MIN_FREE_BYTES,
                   "minimum_free_bytes_at_commit":
                   MIN_FREE_BYTES + POST_COMMIT_EVIDENCE_HEADROOM,
                   "final_storage_guard_before_commit": True,
                   "source_rescan_equal": True, "payload_verified": True,
                   "source_writes_performed": False,
                   "service_actions_performed": False}
        write_private_json_exclusive(staging / RECEIPT_NAME, receipt)
        post_commit_state = {
            "format_version": 1, "run_id": RUN_ID, "host": EXPECTED_HOST,
            "state": "current_read_only_verify_required",
            "archive_path": str(ARCHIVE), "archive_published_if_present": True,
            "source_fingerprint_sha256": initial["fingerprint_sha256"],
            "created_at_epoch": time.time(), "verified": False,
            "verification_status": "pending_current_read_only_verify",
            "archive_content_writes_after_publish_required": False,
            "source_writes_performed": False, "service_actions_performed": False}
        write_private_json_exclusive(staging / POST_COMMIT_STATE_NAME,
                                     post_commit_state)
        fsync_directory(staging)
        validate_private_path(
            staging, "directory", "archive staging owner or mode is unsafe")
        validate_private_path(
            payload, "directory", "archive payload owner or mode is unsafe")
        for metadata_name in (MANIFEST_NAME, RECEIPT_NAME, POST_COMMIT_STATE_NAME):
            validate_private_path(
                staging / metadata_name, "file",
                "archive metadata owner or mode is unsafe")
        stage = "precommit-storage-guard"
        remaining = storage_guard(create=False, require_reserve=True)
        if remaining < MIN_FREE_BYTES + POST_COMMIT_EVIDENCE_HEADROOM:
            raise RuntimeError(
                "/data lacks private headroom for post-commit failure evidence")
        stage = "atomic-publish"
        atomic_rename_noreplace(staging, ARCHIVE)
        # renameat2 is the commit point.  The reserve was checked immediately
        # before this same-filesystem rename, which allocates no payload data.
        try:
            fsync_directory(BASE)
        except Exception as post_commit_error:
            try:
                evidence, evidence_status = write_post_commit_failure(
                    "fsync-published-parent", post_commit_error,
                    initial["fingerprint_sha256"])
            except Exception as evidence_error:
                evidence = ARCHIVE / POST_COMMIT_STATE_NAME
                evidence_status = "prepublished_state_marker_only_%s" % type(
                    evidence_error).__name__
            raise RuntimeError(
                "archive_published=true; current --verify required; "
                "post_commit_evidence=%s; evidence_status=%s" %
                (evidence, evidence_status)) from post_commit_error
        return {"mode": "apply", "host": EXPECTED_HOST, "run_id": RUN_ID,
                "result": "archived", "archive_path": str(ARCHIVE),
                "archive_published": True,
                "receipt_path": str(ARCHIVE / RECEIPT_NAME),
                "post_commit_state_path": str(ARCHIVE / POST_COMMIT_STATE_NAME),
                "current_verify_required": True,
                "published_parent_fsync": "passed",
                "file_count": initial["file_count"],
                "total_bytes": initial["total_bytes"],
                "source_fingerprint_sha256": initial["fingerprint_sha256"],
                "available_bytes_at_commit": remaining,
                "source_writes_performed": False,
                "service_actions_performed": False}
    except Exception as error:
        if source_anchors:
            try:
                close_root_anchors(source_anchors)
                source_anchors = {}
            except Exception:
                if staging is None or not os.path.lexists(str(staging)):
                    raise RuntimeError(
                        "drama archival failed and source anchor close also failed") from error
        if staging is not None and os.path.lexists(str(staging)):
            try:
                failure = write_failure_evidence(
                    staging, stage, error,
                    initial.get("fingerprint_sha256") if initial else None,
                    copied_fingerprint, final_source_fingerprint)
                fsync_directory(staging)
                setattr(error, "private_failure_evidence", str(failure))
            except Exception:
                raise RuntimeError(
                    "drama archival failed and private failure evidence could not be written") from error
        raise


def verify_archive():
    assert_host()
    available = storage_guard(create=False)
    validate_private_path(
        ARCHIVE, "directory", "published drama archive owner or mode is unsafe")
    top_names = set(os.listdir(str(ARCHIVE)))
    required_names = {PAYLOAD_NAME, MANIFEST_NAME, RECEIPT_NAME,
                      POST_COMMIT_STATE_NAME}
    if not required_names.issubset(top_names):
        raise RuntimeError("published drama archive members are incomplete")
    temporary_pattern = re.compile(r"^\.post-commit-failure-[0-9]+-[0-9a-f]{16}\.tmp$")
    temporary_names = sorted(name for name in top_names if temporary_pattern.match(name))
    allowed_names = set(required_names)
    allowed_names.update(temporary_names)
    allowed_names.add(POST_COMMIT_FAILURE_NAME)
    if top_names - allowed_names:
        raise RuntimeError("published drama archive contains an unexpected member")
    for name in temporary_names:
        validate_private_path(
            ARCHIVE / name, "file", "post-commit temporary owner or mode is unsafe")
    validate_private_path(
        ARCHIVE / PAYLOAD_NAME, "directory", "archive payload owner or mode is unsafe")
    manifest, manifest_raw = read_private_json(ARCHIVE / MANIFEST_NAME)
    receipt, _ = read_private_json(ARCHIVE / RECEIPT_NAME)
    post_commit_state, _ = read_private_json(ARCHIVE / POST_COMMIT_STATE_NAME)
    validate_manifest(manifest)
    if (receipt.get("format_version") != 1 or receipt.get("run_id") != RUN_ID or
            receipt.get("host") != EXPECTED_HOST or receipt.get("result") != "archived" or
            not finite_positive_number(receipt.get("completed_at_epoch")) or
            receipt.get("archive_path") != str(ARCHIVE) or
            receipt.get("manifest_file_sha256") != sha256_bytes(manifest_raw) or
            receipt.get("source_fingerprint_sha256") != manifest["fingerprint_sha256"] or
            receipt.get("source_rescan_equal") is not True or
            receipt.get("payload_verified") is not True or
            receipt.get("minimum_free_bytes_at_commit") !=
            MIN_FREE_BYTES + POST_COMMIT_EVIDENCE_HEADROOM or
            receipt.get("final_storage_guard_before_commit") is not True or
            receipt.get("source_writes_performed") is not False or
            receipt.get("service_actions_performed") is not False):
        raise RuntimeError("drama archive receipt does not match the manifest")
    if (post_commit_state.get("format_version") != 1 or
            post_commit_state.get("run_id") != RUN_ID or
            post_commit_state.get("host") != EXPECTED_HOST or
            post_commit_state.get("state") != "current_read_only_verify_required" or
            post_commit_state.get("archive_path") != str(ARCHIVE) or
            post_commit_state.get("archive_published_if_present") is not True or
            post_commit_state.get("source_fingerprint_sha256") !=
            manifest["fingerprint_sha256"] or
            post_commit_state.get("verified") is not False or
            post_commit_state.get("verification_status") !=
            "pending_current_read_only_verify" or
            post_commit_state.get("archive_content_writes_after_publish_required") is not False or
            post_commit_state.get("source_writes_performed") is not False or
            post_commit_state.get("service_actions_performed") is not False):
        raise RuntimeError("drama archive post-commit state marker is invalid")
    post_failure_present = os.path.lexists(str(ARCHIVE / POST_COMMIT_FAILURE_NAME))
    if post_failure_present:
        post_failure, _ = read_private_json(ARCHIVE / POST_COMMIT_FAILURE_NAME)
        if (post_failure.get("format_version") != 1 or
                post_failure.get("run_id") != RUN_ID or
                post_failure.get("host") != EXPECTED_HOST or
                post_failure.get("result") != "post_commit_failure" or
                post_failure.get("stage") != "fsync-published-parent" or
                not isinstance(post_failure.get("error_type"), str) or
                re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}",
                             post_failure.get("error_type")) is None or
                post_failure.get("error_type") not in POST_COMMIT_ERROR_TYPES or
                not finite_positive_number(post_failure.get("failed_at_epoch")) or
                float(post_failure.get("failed_at_epoch")) <
                float(receipt.get("completed_at_epoch")) or
                post_failure.get("archive_path") != str(ARCHIVE) or
                post_failure.get("archive_published") is not True or
                post_failure.get("current_verify_required") is not True or
                post_failure.get("verified") is not False or
                post_failure.get("verification_status") !=
                "pending_current_read_only_verify" or
                post_failure.get("source_fingerprint_sha256") !=
                manifest["fingerprint_sha256"] or
                post_failure.get("source_writes_performed") is not False or
                post_failure.get("service_actions_performed") is not False):
            raise RuntimeError("drama archive post-commit failure evidence is invalid")
    archived = build_archive_manifest(ARCHIVE / PAYLOAD_NAME)
    validate_archive_matches_source(archived, manifest)
    current = build_manifest()
    if current != manifest:
        raise RuntimeError("live drama source manifest drifted from the archive")
    available = storage_guard(create=False, require_reserve=True)
    return {"mode": "verify", "host": EXPECTED_HOST, "run_id": RUN_ID,
            "verified": True, "verification_status": "passed_read_only",
            "archive_path": str(ARCHIVE), "archive_published": True,
            "file_count": manifest["file_count"],
            "directory_count": manifest["directory_count"],
            "total_bytes": manifest["total_bytes"],
            "source_fingerprint_sha256": manifest["fingerprint_sha256"],
            "available_bytes": available, "writes_performed": False,
            "post_commit_failure_present": post_failure_present,
            "recovered_from_post_commit_failure": post_failure_present,
            "ignored_incomplete_post_commit_temporary_count": len(temporary_names),
            "post_commit_temporary_warning": bool(temporary_names),
            "source_writes_performed": False,
            "service_actions_performed": False}


def main(argv=None):
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    assert_host()
    if args.apply:
        result = apply_archive()
    elif args.verify:
        result = verify_archive()
    else:
        result = dry_run()
    print(json.dumps(result, sort_keys=True, indent=2))
    return result


if __name__ == "__main__":
    main()
