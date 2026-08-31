"""Single-threaded, durable COS multipart uploads for async drama media.

An uncertain create never creates another UploadId. Existing parts are checked
against the immutable local bytes; completion is accepted only after an
authenticated HEAD matches this upload's nonce, SHA-256 and length. No failure
aborts a multipart upload or removes the local media.

The caller must supply CosS3Client(retry=0): SDK-level POST retries would bypass
the durable unknown-create fence before this helper receives the exception.

COS part limits/receipts: https://cloud.tencent.com/document/product/436/7750
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import unicodedata
import uuid

from .async_runtime import _FileLock, runtime_error
from .core import DramaSynthesisError
from .local_checkpoint import atomic_write_record, durable_ensure_directory, file_fingerprint, read_record


MIB = 1024 * 1024
DEFAULT_PART_SIZE = 16 * MIB
MAX_PART_SIZE = 5 * 1024 * MIB
MAX_BUFFER_SIZE = 64 * MIB
MAX_PARTS = 10000
PAGE_SIZE = 1000
PHASES = {"creating", "uploading", "completing", "completed"}
SHA_HEADER = "x-cos-meta-drama-sha256"
SIZE_HEADER = "x-cos-meta-drama-size"
BINDING_HEADER = "x-cos-meta-drama-upload"
FORBID_OVERWRITE_HEADER = "x-cos-forbid-overwrite"


def _error(code="drama_upload_checkpoint_unverified", status=503):
    return runtime_error(code, status)


def _safe_text(value, maximum, *, empty=False):
    return (isinstance(value, str) and len(value) <= maximum and (bool(value) or empty)
            and not any(unicodedata.category(char) in {"Cc", "Cs"} for char in value))


def _number(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,20}", str(value)):
        raise _error()
    return int(value)


def _snapshot(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _source(path):
    try:
        before = _snapshot(path)
        value = file_fingerprint(path)
        if before != _snapshot(path):
            raise _error("drama_upload_source_changed", 409)
        return value, before
    except DramaSynthesisError as exc:
        if exc.code == "drama_upload_source_changed":
            raise
        raise _error("drama_upload_source_changed", 409) from None
    except (OSError, ValueError):
        raise _error("drama_upload_source_changed", 409) from None


def _unchanged(path, snapshot):
    try:
        if path.is_symlink() or _snapshot(path) != snapshot:
            raise _error("drama_upload_source_changed", 409)
    except OSError:
        raise _error("drama_upload_source_changed", 409) from None


@contextmanager
def _checkpoint_lock(path, source):
    try:
        # The upload directory is private. Refuse both leaf and ancestor
        # symlinks rather than following a record out of that directory.
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise _error()
        lock_path = path.with_name(path.name + ".lock")
        if path.resolve() == source.resolve() or lock_path.resolve() == source.resolve():
            raise _error("drama_upload_checkpoint_conflict", 409)
        try:
            durable_ensure_directory(path.parent)
        except Exception:
            raise _error() from None
        lock = _FileLock(lock_path)
        if not lock.acquire():
            raise _error("drama_upload_busy")
    except DramaSynthesisError:
        raise
    except (OSError, ValueError):
        raise _error() from None
    try:
        yield
    finally:
        lock.release()


def _load(path):
    try:
        return read_record(path)
    except Exception:
        raise _error() from None


def _save(path, value):
    try:
        atomic_write_record(path, value)
    except Exception:
        raise _error() from None


def _target(bucket, key, content_type, acl):
    content_type = "application/octet-stream" if content_type is None else content_type
    if (not _safe_text(bucket, 255) or not _safe_text(key, 4096)
            or not _safe_text(content_type, 255) or not isinstance(acl, str) or acl not in {"private", "public-read"}):
        raise _error("drama_upload_configuration_invalid", 400)
    return {"bucket": bucket, "key": key, "content_type": content_type, "acl": acl}


def _part_size(size):
    size_per_part = ((size + MAX_PARTS * MIB - 1) // (MAX_PARTS * MIB)) * MIB
    value = max(DEFAULT_PART_SIZE, size_per_part)
    if type(value) is not int or not MIB <= value <= min(MAX_PART_SIZE, MAX_BUFFER_SIZE):
        raise _error("drama_upload_configuration_invalid", 400)
    return value


def _validate_record(record, target, artifact):
    fields = {"version", "target", "artifact", "part_size", "binding", "phase", "upload_id", "result"}
    if (not isinstance(record, dict) or set(record) != fields
            or type(record["version"]) is not int or record["version"] != 1
            or not isinstance(record["target"], dict) or not isinstance(record["artifact"], dict)
            or set(record["artifact"]) != {"sha256", "size_bytes"}
            or type(record["artifact"]["size_bytes"]) is not int or record["artifact"]["size_bytes"] <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(record["artifact"]["sha256"]))
            or type(record["part_size"]) is not int
            or not MIB <= record["part_size"] <= min(MAX_PART_SIZE, MAX_BUFFER_SIZE)
            or not re.fullmatch(r"[0-9a-f]{32}", str(record["binding"]))
            or not isinstance(record["phase"], str) or record["phase"] not in PHASES
            or not _safe_text(record["upload_id"], 2048, empty=record["phase"] == "creating")):
        raise _error()
    if record["target"] != target or record["artifact"] != artifact:
        raise _error("drama_upload_checkpoint_conflict", 409)
    if (artifact["size_bytes"] + record["part_size"] - 1) // record["part_size"] > MAX_PARTS:
        raise _error()
    if record["phase"] == "creating" and record["upload_id"]:
        raise _error()
    if record["phase"] == "completed":
        result = record["result"]
        if (not isinstance(result, dict)
                or set(result) != {"bucket", "key", "sha256", "size_bytes", "etag", "binding"}
                or result["bucket"] != target["bucket"] or result["key"] != target["key"]
                or result["sha256"] != artifact["sha256"]
                or type(result["size_bytes"]) is not int or result["size_bytes"] != artifact["size_bytes"]
                or result["binding"] != record["binding"]
                or not _safe_text(result["etag"], 512)):
            raise _error()
    elif record["result"] is not None:
        raise _error()


def _not_found(exc):
    try:
        getter = getattr(exc, "get_status_code", None)
        return str(getter() if callable(getter) else getattr(exc, "status_code", None)) == "404"
    except Exception:
        return False


def _head(client, target):
    try:
        response = client.head_object(Bucket=target["bucket"], Key=target["key"])
    except Exception as exc:
        if _not_found(exc):
            return None
        raise _error("drama_upload_failed", 502) from None
    if not isinstance(response, dict):
        raise _error("drama_upload_recovery_required")
    headers = {}
    for name, value in response.items():
        if not isinstance(name, str):
            raise _error("drama_upload_recovery_required")
        name = name.lower()
        if name in headers and headers[name] != value:
            raise _error("drama_upload_recovery_required")
        headers[name] = value
    return headers


def _require_unversioned(client, target):
    # COS forbid-overwrite is not effective on versioned buckets. Never change
    # bucket configuration; require an authoritative, never-enabled state.
    try:
        response = client.get_bucket_versioning(Bucket=target["bucket"])
    except Exception:
        raise _error("drama_upload_bucket_state_unverified") from None
    if not isinstance(response, dict) or "Status" in response:
        raise _error("drama_upload_bucket_state_unverified")


def _verified_result(headers, record):
    artifact, target = record["artifact"], record["target"]
    try:
        matches = (headers[SHA_HEADER] == artifact["sha256"]
                   and headers[BINDING_HEADER] == record["binding"]
                   and _number(headers[SIZE_HEADER]) == artifact["size_bytes"]
                   and _number(headers["content-length"]) == artifact["size_bytes"]
                   and _safe_text(headers.get("etag"), 512))
    except (KeyError, DramaSynthesisError):
        matches = False
    if not matches:
        raise _error("drama_upload_object_conflict", 409)
    result = {"bucket": target["bucket"], "key": target["key"], "sha256": artifact["sha256"],
              "size_bytes": artifact["size_bytes"], "etag": headers["etag"],
              "binding": record["binding"]}
    if record["phase"] == "completed" and result != record["result"]:
        raise _error("drama_upload_object_conflict", 409)
    return result


def _finish(client, target, record, checkpoint_path, progress_callback, *, headers=None):
    if headers is None:
        headers = _head(client, target)
    if headers is None:
        raise _error("drama_upload_recovery_required")
    result = _verified_result(headers, record)
    if record["phase"] != "completed":
        record.update(phase="completed", result=result)
        _save(checkpoint_path, record)
    _progress(progress_callback, result["size_bytes"], result["size_bytes"])
    return deepcopy(result)


def _progress(callback, consumed, total):
    if callback is not None:
        try:
            callback(consumed, total)
        except Exception:
            raise _error("drama_upload_failed", 502) from None


def _part_etag(value):
    if not isinstance(value, str):
        raise _error()
    digest = value[1:-1] if value.startswith('"') and value.endswith('"') else value
    if not re.fullmatch(r"[0-9a-fA-F]{32}", digest):
        raise _error()
    return digest.lower()


def _list_parts(client, target, record):
    total = record["artifact"]["size_bytes"]
    part_size = record["part_size"]
    count = (total + part_size - 1) // part_size
    marker, parts = 0, {}
    for _page in range(MAX_PARTS + 1):
        try:
            response = client.list_parts(Bucket=target["bucket"], Key=target["key"],
                                         UploadId=record["upload_id"], MaxParts=PAGE_SIZE,
                                         PartNumberMarker=marker)
        except Exception as exc:
            if _not_found(exc):
                raise _error("drama_upload_recovery_required") from None
            raise _error("drama_upload_failed", 502) from None
        if not isinstance(response, dict):
            raise _error()
        entries = response.get("Part", [])
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list) or len(entries) > PAGE_SIZE:
            raise _error()
        last = marker
        for item in entries:
            if not isinstance(item, dict):
                raise _error()
            number = _number(item.get("PartNumber"))
            expected_size = min(part_size, total - (number - 1) * part_size)
            if not last < number <= count or number in parts or _number(item.get("Size")) != expected_size:
                raise _error()
            _part_etag(item.get("ETag"))
            parts[number] = {"ETag": item["ETag"], "PartNumber": number, "Size": expected_size}
            last = number
        truncated = response.get("IsTruncated")
        if type(truncated) is bool:
            truncated = "true" if truncated else "false"
        if truncated == "false":
            return parts
        if truncated != "true" or not entries:
            raise _error()
        next_marker = _number(response.get("NextPartNumberMarker"))
        if next_marker != last or next_marker <= marker or next_marker >= count:
            raise _error()
        marker = next_marker
    raise _error()


def _upload(client, target, path, record, checkpoint_path, callback, snapshot):
    try:
        parts = _list_parts(client, target, record)
    except DramaSynthesisError as exc:
        if exc.code == "drama_upload_recovery_required":
            # A lost complete response can invalidate the UploadId before the
            # first HEAD becomes visible. Recheck; never create another ID.
            return _finish(client, target, record, checkpoint_path, callback)
        raise
    total, part_size = record["artifact"]["size_bytes"], record["part_size"]
    count = (total + part_size - 1) // part_size
    if record["phase"] == "completing" and len(parts) != count:
        raise _error("drama_upload_recovery_required")
    consumed = sum(item["Size"] for item in parts.values())
    _progress(callback, consumed, total)
    digest, receipts = hashlib.sha256(), []
    _unchanged(path, snapshot)
    with path.open("rb") as handle:
        for number in range(1, count + 1):
            _unchanged(path, snapshot)
            expected_size = min(part_size, total - (number - 1) * part_size)
            body = handle.read(expected_size)
            if len(body) != expected_size:
                raise _error("drama_upload_source_changed", 409)
            digest.update(body)
            md5 = hashlib.md5(body).hexdigest()
            existing = parts.get(number)
            if existing is not None:
                if _part_etag(existing["ETag"]) != md5:
                    raise _error()
                etag = existing["ETag"]
            else:
                try:
                    response = client.upload_part(Bucket=target["bucket"], Key=target["key"],
                                                  UploadId=record["upload_id"], PartNumber=number,
                                                  Body=body, EnableMD5=True)
                except Exception:
                    # The part may already be durable in COS. The next call
                    # lists it and checks its receipt before sending any bytes.
                    raise _error("drama_upload_failed", 502) from None
                etag = response.get("ETag") if isinstance(response, dict) else None
                if _part_etag(etag) != md5:
                    raise _error()
                consumed += expected_size
                _progress(callback, consumed, total)
            receipts.append({"PartNumber": number, "ETag": etag})
        if handle.read(1) or digest.hexdigest() != record["artifact"]["sha256"]:
            raise _error("drama_upload_source_changed", 409)
    _unchanged(path, snapshot)
    # Recheck the destination immediately before committing. In the supported
    # topology the async job/target lock is its only writer.
    headers = _head(client, target)
    if headers is not None:
        return _finish(client, target, record, checkpoint_path, callback, headers=headers)
    _require_unversioned(client, target)
    if record["phase"] != "completing":
        record["phase"] = "completing"
        _save(checkpoint_path, record)
    try:
        client.complete_multipart_upload(Bucket=target["bucket"], Key=target["key"],
                                         UploadId=record["upload_id"], MultipartUpload={"Part": receipts},
                                         Metadata={FORBID_OVERWRITE_HEADER: "true"})
    except Exception:
        # HEAD, rather than the HTTP response, settles whether completion won.
        pass
    return _finish(client, target, record, checkpoint_path, callback)


def resume_upload(client, *, bucket, key, path, checkpoint_path, progress_callback=None,
                  content_type=None, acl="public-read"):
    """Return verified object metadata, preserving uploaded parts on any error.

    Call only from the async drama branch. The checkpoint must be outside the
    removable media workdir. A changed target/file or an unknown create outcome
    requires investigation; this helper never erases that evidence to retry.
    """
    try:
        if progress_callback is not None and not callable(progress_callback):
            raise _error("drama_upload_configuration_invalid", 400)
        target = _target(bucket, key, content_type, acl)
        path, checkpoint_path = Path(path).absolute(), Path(checkpoint_path).absolute()
        with _checkpoint_lock(checkpoint_path, path):
            record = _load(checkpoint_path)
            artifact, snapshot = _source(path)
            if record is not None:
                _validate_record(record, target, artifact)
                if record["phase"] == "creating":
                    raise _error("drama_upload_recovery_required")
            headers = _head(client, target)
            if record is None:
                if headers is not None:
                    raise _error("drama_upload_object_conflict", 409)
                _require_unversioned(client, target)
                _unchanged(path, snapshot)
                record = {"version": 1, "target": target, "artifact": artifact,
                          "part_size": _part_size(artifact["size_bytes"]), "binding": uuid.uuid4().hex,
                          "phase": "creating", "upload_id": "", "result": None}
                _save(checkpoint_path, record)
                try:
                    created = client.create_multipart_upload(
                        Bucket=bucket, Key=key, ACL=acl, ContentType=target["content_type"],
                        Metadata={SHA_HEADER: artifact["sha256"], SIZE_HEADER: str(artifact["size_bytes"]),
                                  BINDING_HEADER: record["binding"]})
                except Exception:
                    raise _error("drama_upload_recovery_required") from None
                upload_id = created.get("UploadId") if isinstance(created, dict) else None
                if not _safe_text(upload_id, 2048):
                    raise _error("drama_upload_recovery_required")
                record.update(phase="uploading", upload_id=upload_id)
                _save(checkpoint_path, record)
            elif headers is not None:
                _unchanged(path, snapshot)
                return _finish(client, target, record, checkpoint_path, progress_callback, headers=headers)
            elif record["phase"] == "completed":
                raise _error("drama_upload_recovery_required")
            return _upload(client, target, path, record, checkpoint_path, progress_callback, snapshot)
    except DramaSynthesisError:
        raise
    except Exception:
        raise _error("drama_upload_failed", 502) from None


__all__ = ["resume_upload"]
