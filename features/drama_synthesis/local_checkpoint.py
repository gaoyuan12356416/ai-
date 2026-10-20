"""Atomic, identity-bound checkpoints for private local media artifacts.

A missing record is a cache miss. An existing, corrupt or conflicting record
is never a cache miss: overwriting its artifact could destroy the only result.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .core import DramaSynthesisError


def checkpoint_error(conflict=False):
    return DramaSynthesisError(
        "drama_media_checkpoint_conflict" if conflict else "drama_media_checkpoint_unverified",
        "本地制作记录与当前任务不一致，已停止重制" if conflict else "本地制作记录暂时无法校验，已停止重制",
        409 if conflict else 503,
    )


def file_fingerprint(path):
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            raise checkpoint_error()
        before = path.stat()
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        after = path.stat()
        if size <= 0 or (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino
        ) or size != after.st_size:
            raise checkpoint_error()
        return {"sha256": digest.hexdigest(), "size_bytes": size}
    except DramaSynthesisError:
        raise
    except (OSError, ValueError):
        raise checkpoint_error() from None


def read_record(path):
    path = Path(path)
    try:
        if path.is_symlink():
            raise checkpoint_error()
        if not path.exists():
            return None
        if not path.is_file() or not 0 < path.stat().st_size <= 65536:
            raise checkpoint_error()
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise checkpoint_error()
        return value
    except DramaSynthesisError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise checkpoint_error() from None


def durable_ensure_directory(path, mode=0o700):
    """Create a private checkpoint directory and persist each new entry.

    POSIX requires the parent directory to be fsynced after mkdir; fsyncing only
    a file inside the new directory cannot make the directory entry durable.
    The Windows branch retains logical atomicity but is not the production
    power-loss durability contract.
    """
    path = Path(path)
    missing = []
    cursor = path
    try:
        while not cursor.exists():
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise checkpoint_error()
            cursor = parent
        if cursor.is_symlink() or not cursor.is_dir():
            raise checkpoint_error()
        for directory in reversed(missing):
            try:
                directory.mkdir(mode=mode)
            except FileExistsError:
                pass
            if directory.is_symlink() or not directory.is_dir():
                raise checkpoint_error()
            if os.name == "posix":
                for durable in (directory, directory.parent):
                    fd = os.open(str(durable), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
        return path
    except DramaSynthesisError:
        raise
    except OSError:
        raise checkpoint_error() from None


def atomic_write_record(path, value):
    path = Path(path)
    if path.is_symlink():
        raise checkpoint_error()
    durable_ensure_directory(path.parent)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw.encode("utf-8")) > 65536:
        raise checkpoint_error()
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_completed(record_path, artifact_path, identity):
    value = read_record(record_path)
    if value is None:
        return None
    if set(value) != {"version", "identity", "artifact", "result"} or type(value["version"]) is not int or value["version"] != 1:
        raise checkpoint_error()
    if not isinstance(value["identity"], dict) or value["identity"] != dict(identity):
        raise checkpoint_error(conflict=True)
    artifact = value["artifact"]
    if not isinstance(artifact, dict) or set(artifact) != {"size_bytes", "sha256"}:
        raise checkpoint_error()
    if type(artifact["size_bytes"]) is not int or artifact["size_bytes"] <= 0 or not re.fullmatch(r"[0-9a-f]{64}", str(artifact["sha256"])):
        raise checkpoint_error()
    if not isinstance(value["result"], dict) or file_fingerprint(artifact_path) != artifact:
        raise checkpoint_error()
    for key in ("sha256", "output_sha256"):
        if key in value["result"] and value["result"][key] != artifact["sha256"]:
            raise checkpoint_error()
    for key in ("size_bytes", "output_size"):
        if key in value["result"] and (type(value["result"][key]) is not int or value["result"][key] != artifact["size_bytes"]):
            raise checkpoint_error()
    return dict(value["result"])


def save_completed(record_path, artifact_path, identity, result, *, fingerprint=None):
    artifact_path = Path(artifact_path)
    if fingerprint is None:
        fingerprint = file_fingerprint(artifact_path)
    else:
        fingerprint = dict(fingerprint)
        if (set(fingerprint) != {"size_bytes", "sha256"} or
                type(fingerprint["size_bytes"]) is not int or fingerprint["size_bytes"] <= 0 or
                not re.fullmatch(r"[0-9a-f]{64}", str(fingerprint["sha256"])) or
                artifact_path.is_symlink() or not artifact_path.is_file() or
                artifact_path.stat().st_size != fingerprint["size_bytes"]):
            raise checkpoint_error()
    atomic_write_record(record_path, {
        "version": 1, "identity": dict(identity), "artifact": fingerprint, "result": dict(result),
    })
