#!/usr/bin/env python3
"""Quarantine only six approved HK drama artifacts; dry-run by default.

The operator never deletes, rebuilds, forges, adopts or copies an artifact.  A
successful apply performs six same-filesystem RENAME_NOREPLACE moves after the
worker is stopped, while the tunnel and eight protected units remain unchanged,
and every inode, size, SHA256, ffprobe result and
missing completed marker has been re-anchored through no-follow descriptors.
"""
from __future__ import print_function

import argparse
import contextlib
import ctypes
import errno
import json
import math
import os
import pathlib
import re
import signal
import stat
import sys
import time

import drama_operator_common as common


class OperatorInterrupted(common.OperatorError):
    pass


@contextlib.contextmanager
def interruption_guard():
    previous = {}

    def interrupted(signum, _frame):
        raise OperatorInterrupted("quarantine interrupted by signal %d" % int(signum))

    for name in ("SIGINT", "SIGTERM"):
        value = getattr(signal, name, None)
        if value is not None:
            previous[value] = signal.getsignal(value)
            signal.signal(value, interrupted)
    try:
        yield
    finally:
        for value, handler in previous.items():
            signal.signal(value, handler)


@contextlib.contextmanager
def blocked_mutation_signals():
    values = {value for value in
              (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None))
              if value is not None}
    if os.name == "nt":  # local unit tests only; production is Linux.
        yield
        return
    if not hasattr(signal, "pthread_sigmask"):
        raise common.OperatorError("signal masking is unavailable for quarantine rename")
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, values)
    try:
        yield
    finally:
        # A pending termination signal is delivered only after the move is in
        # RAM, both namespaces are fsynced and rename evidence is durable.
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


PREFLIGHT_PATH = pathlib.Path(
    "/data/migrations/gpu-service-migration-20260828T1502/"
    "drama-legacy-artifacts-preflight-20260901T0455Z.json")
PREFLIGHT_SHA256 = "25c3f30f8f366bcb4ee618ea0c1946652e3669e20f680fda93c97dfbb2ecb6bc"
FFPROBE_DEFAULT = pathlib.Path("/usr/bin/ffprobe")
CPU_DB_PATH = pathlib.Path("/root/drama_material_service/data/drama_material_jobs.sqlite3")
LEDGER_INPUT_ROOT = (common.HK_DATA_ROOT / "migrations" / common.RUN_ID /
                     "drama-ledger-evidence")
LEDGER_MAX_AGE_SECONDS = 1800
STAMP_RE = re.compile(r"^20[0-9]{6}T[0-9]{6}Z$")
LEDGER_NAME_RE = re.compile(r"^drama-ledger-before-20[0-9]{6}T[0-9]{6}Z\.json$")
JOB_IDS = (
    "679e7c49acbf4af79f78bf60d76c5dd7",
    "b6e0bc51bb3f44e19c12b20cef7b93fe",
)
WORKER_UNIT = common.HK_TARGET_UNITS[0]
TUNNEL_UNIT = common.HK_TARGET_UNITS[1]
PRESERVED_UNITS = (TUNNEL_UNIT,) + common.HK_PROTECTED_UNITS


APPROVED = (
    {
        "index": 1,
        "path": "/data/drama-synthesis-gpu/work/jobs/679e7c49acbf4af79f78bf60d76c5dd7/segments/000_intro.mp4",
        "job_id": JOB_IDS[0],
        "destination_relative": "%s/work/segments/000_intro.mp4" % JOB_IDS[0],
        "inode": 1709295, "size": 197134,
        "sha256": "6ae022b06b8c581ebf3190d08189d597771ed5e2f7c0e2008650dcaef6e0137d",
    },
    {
        "index": 2,
        "path": "/data/drama-synthesis-gpu/work/jobs/679e7c49acbf4af79f78bf60d76c5dd7/8HehaA3263_679e7c49_eps_1_70.mp4",
        "job_id": JOB_IDS[0],
        "destination_relative": "%s/work/8HehaA3263_679e7c49_eps_1_70.mp4" % JOB_IDS[0],
        "inode": 1709367, "size": 5139047136,
        "sha256": "5ba715a816999afef724215e7124ddf84638fae95ac74d1387a646df3b8162e0",
    },
    {
        "index": 3,
        "path": "/data/drama-synthesis-gpu/work/jobs/b6e0bc51bb3f44e19c12b20cef7b93fe/segments/000_intro.mp4",
        "job_id": JOB_IDS[1],
        "destination_relative": "%s/work/segments/000_intro.mp4" % JOB_IDS[1],
        "inode": 4065349, "size": 291805,
        "sha256": "0c1475d611a92ced8f3be8d2cfb99f26b1698aed722b6568a1867f9511265864",
    },
    {
        "index": 4,
        "path": "/data/drama-synthesis-gpu/work/jobs/b6e0bc51bb3f44e19c12b20cef7b93fe/6oTsxN8BO6_b6e0bc51_eps_1_60.mp4",
        "job_id": JOB_IDS[1],
        "destination_relative": "%s/work/6oTsxN8BO6_b6e0bc51_eps_1_60.mp4" % JOB_IDS[1],
        "inode": 4065412, "size": 4511337915,
        "sha256": "0366898789d8da0a9e0db54c5172b989b68d9038308f930c2baa56d9dd82f6a4",
    },
    {
        "index": 5,
        "path": "/data/drama-synthesis-gpu/work/jobs/b6e0bc51bb3f44e19c12b20cef7b93fe/material_no_bgm.mp4",
        "job_id": JOB_IDS[1],
        "destination_relative": "%s/work/material_no_bgm.mp4" % JOB_IDS[1],
        "inode": 4065413, "size": 4550359900,
        "sha256": "fe0bfe122eb11fbf4e28512d18cec368c1e84c7a357e3e3150292fe1f7c9c3ac",
    },
    {
        "index": 6,
        "path": "/data/drama-synthesis-gpu/results/public/b6e0bc51bb3f44e19c12b20cef7b93fe/material_no_bgm.mp4",
        "job_id": JOB_IDS[1],
        "destination_relative": "%s/public/material_no_bgm.mp4" % JOB_IDS[1],
        "inode": 4065414, "size": 4550359900,
        "sha256": "fe0bfe122eb11fbf4e28512d18cec368c1e84c7a357e3e3150292fe1f7c9c3ac",
    },
)
APPROVED_CONTRACT_SHA256 = "db8257608535f54908608e8acc77509d90527e4aa2120f8d7e8999c1964c00ce"


def quarantine_root(stamp):
    if not STAMP_RE.match(str(stamp or "")):
        raise common.OperatorError("quarantine stamp must be exact compact UTC")
    try:
        parsed = time.strptime(stamp, "%Y%m%dT%H%M%SZ")
    except ValueError:
        raise common.OperatorError("quarantine stamp is not a valid UTC timestamp")
    if time.strftime("%Y%m%dT%H%M%SZ", parsed) != stamp:
        raise common.OperatorError("quarantine stamp is not canonical UTC")
    return (common.HK_DATA_ROOT / "migrations" / common.RUN_ID /
            ("drama-legacy-artifacts-%s" % stamp))


def destination_path(root, item):
    relative = pathlib.PurePosixPath(item["destination_relative"])
    if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != item["job_id"]:
        raise common.OperatorError("approved quarantine destination escaped its job directory")
    return pathlib.Path(root) / relative


def create_quarantine_root(root):
    root = pathlib.Path(root)
    common.create_private_ancestry(common.HK_DATA_ROOT, root.parent)
    try:
        os.mkdir(str(root), 0o700)
    except FileExistsError:
        raise common.OperatorError("quarantine root appeared concurrently; adoption is forbidden")
    if os.name != "nt":
        os.chown(str(root), 0, 0)
        os.chmod(str(root), 0o700)
    common.fsync_directory(root.parent)
    common.real_directory(root)
    return root


def parse_fragment_tokens(values):
    result = {}
    for raw in values or []:
        parts = raw.split("|", 2)
        if (len(parts) != 3 or not parts[1].startswith("/") or
                not re.match(r"^[0-9a-f]{64}$", parts[2]) or parts[0] in result):
            raise common.OperatorError("invalid or duplicate fragment binding")
        result[parts[0]] = {"path": parts[1], "sha256": parts[2]}
    return result


def validate_approved_contract():
    if len(APPROVED) != 6 or [item["index"] for item in APPROVED] != list(range(1, 7)):
        raise common.OperatorError("approved quarantine artifact cardinality changed")
    if common.sha256_bytes(common.canonical_bytes(APPROVED)) != APPROVED_CONTRACT_SHA256:
        raise common.OperatorError("approved quarantine artifact contract changed")
    paths = [item["path"] for item in APPROVED]
    destinations = [item["destination_relative"] for item in APPROVED]
    if len(set(paths)) != 6 or len(set(destinations)) != 6:
        raise common.OperatorError("approved quarantine paths are not unique")
    for item in APPROVED:
        if (set(item) != {"index", "path", "job_id", "destination_relative",
                          "inode", "size", "sha256"} or
                type(item["index"]) is not int or type(item["inode"]) is not int or
                type(item["size"]) is not int or
                any(type(item[key]) is not str for key in
                    ("path", "job_id", "destination_relative", "sha256")) or
                item["job_id"] not in JOB_IDS or
                not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) or
                item["inode"] <= 0 or item["size"] <= 0):
            raise common.OperatorError("approved quarantine metadata is invalid")
        source = pathlib.PurePosixPath(item["path"])
        if not source.is_absolute() or str(source) != item["path"] or ".." in source.parts:
            raise common.OperatorError("approved quarantine source path is not canonical")
        roots = (
            (pathlib.PurePosixPath("/data/drama-synthesis-gpu/work/jobs") /
             item["job_id"], "work"),
            (pathlib.PurePosixPath("/data/drama-synthesis-gpu/results/public") /
             item["job_id"], "public"),
        )
        expected_destination = None
        for source_root, destination_lane in roots:
            try:
                relative = source.relative_to(source_root)
            except ValueError:
                continue
            if relative.parts and ".." not in relative.parts:
                expected_destination = str(
                    pathlib.PurePosixPath(item["job_id"]) /
                    destination_lane / relative)
                break
        if expected_destination != item["destination_relative"]:
            raise common.OperatorError(
                "approved source job/lane does not match its quarantine destination")


def validate_cli(args):
    validate_approved_contract()
    if (args.run_id != common.RUN_ID or args.expected_host != common.HK_HOST or
            not STAMP_RE.match(str(args.stamp or "")) or
            pathlib.Path(args.data_root) != common.HK_DATA_ROOT or
            pathlib.Path(args.preflight) != PREFLIGHT_PATH or
            args.preflight_sha256 != PREFLIGHT_SHA256 or
            pathlib.Path(args.ffprobe) != FFPROBE_DEFAULT or
            not re.match(r"^[0-9a-f]{64}$", args.ledger_evidence_sha256 or "") or
            tuple(args.unit or ()) != common.HK_TARGET_UNITS or
            tuple(args.protected_unit or ()) != common.HK_PROTECTED_UNITS or
            args.expected_current_sha not in (common.OLD_SHA, common.NEW_SHA) or
            not args.expected_data_device or any(ch.isspace() for ch in args.expected_data_device)):
        raise common.OperatorError("quarantine arguments differ from the exact approved contract")
    quarantine_root(args.stamp)
    ledger = pathlib.Path(args.ledger_evidence)
    if ledger.parent != LEDGER_INPUT_ROOT or not LEDGER_NAME_RE.match(ledger.name):
        raise common.OperatorError("CPU ledger evidence path is outside the exact HK input root")
    if args.apply and not re.match(r"^[0-9a-f]{64}$", args.expected_ffprobe_sha256 or ""):
        raise common.OperatorError("apply requires the exact ffprobe SHA256 from dry-run")


def read_fd_all(descriptor, limit):
    blocks = []
    offset = 0
    while offset <= limit:
        if hasattr(os, "pread"):
            block = os.pread(descriptor, min(1024 * 1024, limit + 1 - offset), offset)
        else:
            os.lseek(descriptor, offset, os.SEEK_SET)
            block = os.read(descriptor, min(1024 * 1024, limit + 1 - offset))
        if not block:
            break
        blocks.append(block)
        offset += len(block)
    raw = b"".join(blocks)
    if len(raw) > limit:
        raise common.OperatorError("approved preflight evidence is unexpectedly large")
    return raw


def reject_json_constant(value):
    raise ValueError("non-finite JSON constant: %s" % value)


def reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key: %s" % key)
        result[key] = value
    return result


def strict_json_loads(raw):
    return json.loads(raw, parse_constant=reject_json_constant,
                      object_pairs_hook=reject_duplicate_pairs)


def verify_preflight_evidence():
    common.validate_existing_ancestry(
        PREFLIGHT_PATH, trusted_root=common.HK_DATA_ROOT)
    descriptor, record, digest = common.anchored_file(
        PREFLIGHT_PATH, expected_sha256=PREFLIGHT_SHA256)
    try:
        value = os.fstat(descriptor)
        if os.name != "nt" and (value.st_uid != 0 or value.st_gid != 0 or
                                 stat.S_IMODE(value.st_mode) != 0o600 or value.st_nlink != 1):
            raise common.OperatorError("approved preflight evidence is not private")
        raw = read_fd_all(descriptor, 16 * 1024 * 1024)
    finally:
        os.close(descriptor)
    try:
        document = strict_json_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise common.OperatorError("approved preflight evidence is not valid JSON")
    if not isinstance(document, (dict, list)):
        raise common.OperatorError("approved preflight evidence JSON type changed")
    for item in APPROVED:
        if raw.count(item["path"].encode("utf-8")) < 1:
            raise common.OperatorError("approved preflight omits an exact artifact path")
    return {"path": str(PREFLIGHT_PATH), "sha256": digest, "bytes": len(raw),
            "stat": record, "json_type": type(document).__name__,
            "all_six_exact_paths_present": True}


def reject_credential_fields(value):
    forbidden = ("token", "cookie", "password", "secret", "authorization", "credential")
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if any(word in lowered for word in forbidden):
                raise common.OperatorError("ledger evidence contains a forbidden credential field")
            reject_credential_fields(child)
    elif isinstance(value, list):
        for child in value:
            reject_credential_fields(child)


def is_exact_cpu_data_path(value):
    try:
        path = pathlib.PurePosixPath(str(value))
        relative = path.relative_to(pathlib.PurePosixPath("/mnt/data-disk"))
    except ValueError:
        return False
    return path.is_absolute() and bool(relative.parts) and ".." not in relative.parts


def verify_ledger_evidence(args, now=None):
    path = pathlib.Path(args.ledger_evidence)
    common.validate_existing_ancestry(path, trusted_root=LEDGER_INPUT_ROOT)
    descriptor, record, digest = common.anchored_file(
        path, expected_sha256=args.ledger_evidence_sha256)
    try:
        value = os.fstat(descriptor)
        if os.name != "nt" and (value.st_uid != 0 or value.st_gid != 0 or
                                 stat.S_IMODE(value.st_mode) != 0o600 or value.st_nlink != 1):
            raise common.OperatorError("CPU ledger evidence is not a private single-link file")
        raw = read_fd_all(descriptor, 2 * 1024 * 1024)
    finally:
        os.close(descriptor)
    try:
        document = strict_json_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise common.OperatorError("CPU ledger evidence is not valid JSON")
    if not isinstance(document, dict):
        raise common.OperatorError("CPU ledger evidence must be an object")
    reject_credential_fields(document)
    captured = document.get("captured_at_epoch")
    current = time.time() if now is None else float(now)
    if (type(captured) not in (int, float) or not math.isfinite(float(captured)) or
            current - float(captured) > LEDGER_MAX_AGE_SECONDS or
            float(captured) - current > 30):
        raise common.OperatorError("CPU ledger evidence is stale or future-dated")
    database = document.get("database")
    snapshot = document.get("snapshot")
    sqlite = document.get("sqlite")
    expected_jobs = [{"job_id": job_id, "status": "failed"} for job_id in JOB_IDS]
    if (set(document) != {"schema", "kind", "run_id", "host", "captured_at_epoch",
                          "production_mutations", "database", "snapshot", "sqlite"} or
            type(document.get("schema")) is not int or document.get("schema") != 1 or
            document.get("kind") != "drama_quarantine_ledger_before" or
            document.get("run_id") != common.RUN_ID or
            document.get("host") != common.CPU_HOST or
            type(document.get("production_mutations")) is not int or
            document.get("production_mutations") != 0 or
            not isinstance(database, dict) or
            set(database) != {"path", "realpath", "device", "inode", "size", "mtime_ns"} or
            database.get("path") != str(CPU_DB_PATH) or
            database.get("realpath") != str(CPU_DB_PATH) or
            not all(isinstance(database.get(key), int) and
                    not isinstance(database.get(key), bool) and database.get(key) > 0
                    for key in ("device", "inode", "size", "mtime_ns")) or
            not isinstance(snapshot, dict) or
            set(snapshot) != {"method", "path", "sha256"} or
            snapshot.get("method") not in
            ("sqlite_online_backup", "immutable_read_only_snapshot") or
            not is_exact_cpu_data_path(snapshot.get("path") or "") or
            type(snapshot.get("sha256")) is not str or
            not re.fullmatch(r"[0-9a-f]{64}", snapshot.get("sha256")) or
            not isinstance(sqlite, dict) or
            set(sqlite) != {"quick_check", "foreign_key_violations", "active_jobs",
                            "active_leases", "approved_job_statuses"} or
            sqlite.get("quick_check") != "ok" or
            any(type(sqlite.get(key)) is not int or sqlite.get(key) != 0
                for key in ("foreign_key_violations", "active_jobs", "active_leases")) or
            sqlite.get("approved_job_statuses") != expected_jobs or
            any(not isinstance(row, dict) or set(row) != {"job_id", "status"}
                for row in sqlite.get("approved_job_statuses", []))):
        raise common.OperatorError("CPU ledger evidence differs from the exact drained-failed contract")
    database_receipt = {key: database[key]
                        for key in ("path", "realpath", "device", "inode", "size", "mtime_ns")}
    snapshot_receipt = {key: snapshot[key] for key in ("method", "path", "sha256")}
    sqlite_receipt = {
        "quick_check": "ok", "foreign_key_violations": 0,
        "active_jobs": 0, "active_leases": 0,
        "approved_job_statuses": [dict(row) for row in expected_jobs],
    }
    return {"path": str(path), "sha256": digest, "stat": record,
            "captured_at_epoch": float(captured), "max_age_seconds": LEDGER_MAX_AGE_SECONDS,
            "database": database_receipt, "snapshot": snapshot_receipt,
            "sqlite": sqlite_receipt,
            "credentials_present": False}


def verify_current(expected_sha):
    current = common.HK_BASE / "current"
    expected = common.HK_BASE / "releases" / expected_sha
    if not current.is_symlink() or os.path.realpath(str(current)) != str(expected):
        raise common.OperatorError("HK current release differs from explicit quarantine binding")
    common.real_directory(expected)
    return {"link": str(current), "expected_sha": expected_sha,
            "resolved": str(expected), "target": os.readlink(str(current))}


def validate_ffprobe(path, expected_sha256, required):
    path = pathlib.Path(path)
    common.validate_existing_ancestry(path)
    descriptor, record, digest = common.anchored_file(path)
    try:
        value = os.fstat(descriptor)
        if os.name != "nt" and (value.st_uid != 0 or value.st_gid != 0 or
                                 not value.st_mode & stat.S_IXUSR or
                                 stat.S_IMODE(value.st_mode) & 0o022):
            raise common.OperatorError("ffprobe binary owner/mode is unsafe")
        if required and digest != expected_sha256:
            raise common.OperatorError("ffprobe SHA256 differs from apply binding")
        return {"path": str(path), "sha256": digest, "stat": record,
                "descriptor": descriptor}
    except Exception:
        os.close(descriptor)
        raise


def verify_ffprobe_anchor(ffprobe):
    before = common.stat_record(os.fstat(ffprobe["descriptor"]))
    if before != ffprobe["stat"]:
        raise common.OperatorError("anchored ffprobe binary metadata changed")
    digest = common.sha256_fd(ffprobe["descriptor"])
    after = common.stat_record(os.fstat(ffprobe["descriptor"]))
    if after != ffprobe["stat"] or digest != ffprobe["sha256"]:
        raise common.OperatorError("anchored ffprobe binary changed")


def parent_root(path):
    path = pathlib.Path(path)
    work = common.HK_BASE / "work" / "jobs"
    public = common.HK_BASE / "results" / "public"
    for root in (work, public):
        try:
            path.relative_to(root)
            return root
        except ValueError:
            pass
    raise common.OperatorError("approved artifact escaped both exact drama roots")


def marker_absent(parent_fd, name):
    marker = name + ".completed.json"
    try:
        os.stat(marker, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return marker
    raise common.OperatorError("completed marker exists for an approved legacy artifact")


def directory_anchor(value):
    return {"device": int(value.st_dev), "inode": int(value.st_ino),
            "mode": int(value.st_mode), "uid": int(getattr(value, "st_uid", -1)),
            "gid": int(getattr(value, "st_gid", -1))}


def open_artifact(item):
    path = pathlib.Path(item["path"])
    root = parent_root(path)
    common.real_directory(root, require_root_owner=False)
    common.validate_existing_ancestry(path, trusted_root=root, require_root_owner=False)
    parent = path.parent
    before_parent = os.lstat(str(parent))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(str(parent), flags)
    descriptor = None
    try:
        opened_parent = os.fstat(parent_fd)
        if common.identity_tuple(before_parent) != common.identity_tuple(opened_parent):
            raise common.OperatorError("artifact parent changed during no-follow open")
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise common.OperatorError("approved artifact is not a regular file")
        descriptor = os.open(
            path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
            getattr(os, "O_BINARY", 0), dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if common.identity_tuple(before) != common.identity_tuple(opened):
            raise common.OperatorError("approved artifact changed during no-follow open")
        if int(opened.st_ino) != item["inode"] or int(opened.st_size) != item["size"]:
            raise common.OperatorError("approved artifact inode or size changed")
        digest = common.sha256_fd(descriptor)
        if digest != item["sha256"]:
            raise common.OperatorError("approved artifact SHA256 changed")
        if common.identity_tuple(opened) != common.identity_tuple(os.fstat(descriptor)):
            raise common.OperatorError("approved artifact changed while hashing")
        marker = marker_absent(parent_fd, path.name)
        return {"item": item, "path": path, "parent": parent,
                "parent_fd": parent_fd, "fd": descriptor,
                "parent_stat": directory_anchor(opened_parent),
                "stat": common.stat_record(opened), "sha256": digest,
                "marker": marker, "probe": None, "destination": None,
                "destination_candidate": None, "destination_parent_fd": None,
                "move_complete": False, "target_sha256": None}
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)
        raise


def close_artifact(handle):
    for key in ("fd", "parent_fd", "destination_parent_fd"):
        value = handle.get(key)
        if value is not None:
            os.close(value)
            handle[key] = None


def probe_artifact(handle, ffprobe):
    path = "/proc/self/fd/%d" % handle["fd"]
    executable = "/proc/self/fd/%d" % ffprobe["descriptor"]
    verify_ffprobe_anchor(ffprobe)
    _, stdout, _ = common.run(
        [executable, "-v", "error", "-show_streams", "-show_format",
         "-of", "json", path],
        pass_fds=(ffprobe["descriptor"], handle["fd"]))
    verify_ffprobe_anchor(ffprobe)
    if len(stdout.encode("utf-8")) > 4 * 1024 * 1024:
        raise common.OperatorError("ffprobe result is unexpectedly large")
    try:
        value = strict_json_loads(stdout)
    except ValueError:
        raise common.OperatorError("ffprobe result is not JSON")
    if (not isinstance(value, dict) or not isinstance(value.get("streams"), list) or
            not isinstance(value.get("format"), dict) or not value["streams"]):
        raise common.OperatorError("ffprobe result lacks stream/format evidence")
    current = os.fstat(handle["fd"])
    if common.stat_record(current) != handle["stat"]:
        raise common.OperatorError("artifact changed while ffprobe was running")
    handle["probe"] = {"sha256": common.sha256_bytes(common.canonical_bytes(value)),
                       "stream_count": len(value["streams"]),
                       "format_name": str(value["format"].get("format_name") or ""),
                       "duration": str(value["format"].get("duration") or ""),
                       "size": str(value["format"].get("size") or "")}
    return handle["probe"]


def artifact_receipt(handle):
    return {"index": handle["item"]["index"], "job_id": handle["item"]["job_id"],
            "source": str(handle["path"]),
            "destination": str(handle["destination"]) if handle["destination"] else None,
            "inode": handle["item"]["inode"], "size": handle["item"]["size"],
            "sha256": handle["sha256"], "ffprobe": handle["probe"],
            "target_sha256": handle["target_sha256"],
            "completed_marker": str(handle["path"]) + ".completed.json",
            "completed_marker_lexists": False,
            "destination_completed_marker_lexists": False,
            "move_complete": bool(handle["move_complete"])}


def assert_no_listener_8787():
    _, stdout, _ = common.run(["ss", "-Hltnp", "sport = :8787"])
    rows = [line for line in stdout.splitlines() if line.strip()]
    if rows:
        raise common.OperatorError(
            "orphan or unit-owned listener remains on the stopped drama port 8787")
    return {"port": 8787, "listener_count": 0}


def snapshot_and_guard(args, baseline=None):
    units = common.snapshot_units(common.HK_TARGET_UNITS + common.HK_PROTECTED_UNITS)
    common.assert_inactive_unit(units[WORKER_UNIT])
    for unit in PRESERVED_UNITS:
        common.assert_active_single_process(units[unit])
    if baseline is not None:
        common.assert_protected_units(
            baseline, {unit: units[unit] for unit in PRESERVED_UNITS})
    common.assert_no_media_processes()
    assert_no_listener_8787()
    common.assert_no_established_ports((8787,))
    supplied = parse_fragment_tokens(args.fragment)
    live = {unit: {"path": item["fragment"]["path"],
                   "sha256": item["fragment"]["sha256"]}
            for unit, item in units.items()}
    if supplied:
        if supplied != live:
            raise common.OperatorError("unit FragmentPath/SHA binding changed")
    elif args.apply:
        raise common.OperatorError("apply requires every exact fragment binding from dry-run")
    return units, live


def inspect(args, keep_open=False):
    common.require_host(common.HK_HOST)
    mount = common.validate_data_root("hk", common.HK_DATA_ROOT, args.expected_data_device)
    preflight = verify_preflight_evidence()
    ledger = verify_ledger_evidence(args)
    current = verify_current(args.expected_current_sha)
    ffprobe = validate_ffprobe(args.ffprobe, args.expected_ffprobe_sha256, args.apply)
    ffprobe_receipt = {key: value for key, value in ffprobe.items()
                       if key != "descriptor"}
    handles = []
    try:
        units, fragments = snapshot_and_guard(args)
        protected = {unit: units[unit] for unit in PRESERVED_UNITS}
        for item in APPROVED:
            handle = open_artifact(item)
            handles.append(handle)
            probe_artifact(handle, ffprobe)
            common.assert_no_media_processes()
        result = {"schema": 1, "mode": "apply" if args.apply else "dry-run",
                  "ready": True, "run_id": common.RUN_ID, "host": common.HK_HOST,
                  "data": mount, "preflight": preflight, "current": current,
                  "cpu_ledger_before": ledger,
                  "ffprobe": ffprobe_receipt, "fragments": fragments,
                  "required_fragment_arguments": [
                      "%s|%s|%s" % (unit, fragments[unit]["path"], fragments[unit]["sha256"])
                      for unit in sorted(fragments)],
                  "worker_unit_stopped": WORKER_UNIT,
                  "tunnel_unit_preserved": TUNNEL_UNIT,
                  "protected_units": {unit: common.protected_signature(units[unit])
                                      for unit in PRESERVED_UNITS},
                  "artifacts": [artifact_receipt(handle) for handle in handles],
                  "source_mutations": 0, "service_actions": 0,
                  "handles": handles, "protected_snapshot": protected}
        if not keep_open:
            for handle in handles:
                close_artifact(handle)
            result.pop("handles")
            result.pop("protected_snapshot")
        return result
    except Exception:
        for handle in handles:
            close_artifact(handle)
        raise
    finally:
        os.close(ffprobe["descriptor"])


def renameat2_noreplace(source_fd, source_name, destination_fd, destination_name):
    library = ctypes.CDLL(None, use_errno=True)
    wrapper = getattr(library, "renameat2", None)
    if wrapper is None:
        raise common.OperatorError("fd-relative renameat2 is unavailable")
    wrapper.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                        ctypes.c_char_p, ctypes.c_uint]
    wrapper.restype = ctypes.c_int
    result = wrapper(int(source_fd), os.fsencode(source_name), int(destination_fd),
                     os.fsencode(destination_name), 1)
    if result:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise common.OperatorError("quarantine no-replace destination already exists")
        raise common.OperatorError("fd-relative quarantine rename failed errno=%d" % code)


def entry_stat(directory_fd, name):
    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def verify_directory_anchor(path, descriptor, record):
    current = os.lstat(str(path))
    opened = os.fstat(descriptor)
    if directory_anchor(current) != record or directory_anchor(opened) != record:
        raise common.OperatorError("anchored artifact directory changed")


def verify_live_anchor(handle):
    verify_directory_anchor(handle["parent"], handle["parent_fd"], handle["parent_stat"])
    current = entry_stat(handle["parent_fd"], handle["path"].name)
    if common.stat_record(current) != handle["stat"] or common.stat_record(os.fstat(handle["fd"])) != handle["stat"]:
        raise common.OperatorError("artifact path no longer names its anchored inode")
    marker_absent(handle["parent_fd"], handle["path"].name)


def prepare_destination(root, handle):
    destination = destination_path(root, handle["item"])
    common.create_private_ancestry(common.HK_DATA_ROOT, destination.parent)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(destination.parent), flags)
    opened = os.fstat(descriptor)
    current = os.lstat(str(destination.parent))
    if (common.identity_tuple(opened) != common.identity_tuple(current) or
            opened.st_dev != os.fstat(handle["fd"]).st_dev):
        os.close(descriptor)
        raise common.OperatorError("quarantine destination is not the same anchored filesystem")
    try:
        try:
            entry_stat(descriptor, destination.name)
            raise common.OperatorError("quarantine no-replace destination already exists")
        except FileNotFoundError:
            pass
        marker_absent(descriptor, destination.name)
    except Exception:
        os.close(descriptor)
        raise
    handle["destination_candidate"] = destination
    handle["destination_parent_fd"] = descriptor
    handle["destination_parent_stat"] = directory_anchor(opened)
    return destination


def move_to_quarantine(handle, move_journal, evidence):
    destination = handle["destination_candidate"]
    destination_fd = handle["destination_parent_fd"]
    if destination is None or destination_fd is None:
        raise common.OperatorError("quarantine destination was not anchored")
    verify_live_anchor(handle)
    marker_absent(destination_fd, destination.name)
    intent = {"schema": 1, "state": "prepared", "run_id": common.RUN_ID,
              "index": handle["item"]["index"], "job_id": handle["item"]["job_id"],
              "source": str(handle["path"]), "destination": str(destination),
              "inode": handle["item"]["inode"], "size": handle["item"]["size"],
              "sha256": handle["item"]["sha256"],
              "source_marker_absent": True, "destination_marker_absent": True,
              "prepared_at_epoch": time.time()}
    common.write_exclusive_json(
        pathlib.Path(evidence) / ("move-intent-%02d.json" % handle["item"]["index"]),
        intent)
    with blocked_mutation_signals():
        renameat2_noreplace(handle["parent_fd"], handle["path"].name,
                            destination_fd, destination.name)
        # Journal immediately after the namespace mutation, before hashing.
        handle["destination"] = destination
        handle["move_complete"] = True
        move_journal.append(handle)
        common.fsync_directory(handle["parent"])
        common.fsync_directory(destination.parent)
        common.write_exclusive_json(
            pathlib.Path(evidence) / ("rename-%02d.json" % handle["item"]["index"]),
            {"schema": 1, "state": "renamed_unverified", "run_id": common.RUN_ID,
             "index": handle["item"]["index"], "source": str(handle["path"]),
             "destination": str(destination),
             "expected_sha256": handle["item"]["sha256"],
             "recovery_uses_no_replace_only": True, "renamed_at_epoch": time.time()})
    verify_quarantined_anchor(handle, hash_required=True)


def verify_quarantined_anchor(handle, hash_required=False):
    destination = handle["destination"]
    destination_fd = handle["destination_parent_fd"]
    if not handle.get("move_complete") or destination is None:
        raise common.OperatorError("artifact has no completed quarantine move")
    verify_directory_anchor(handle["parent"], handle["parent_fd"], handle["parent_stat"])
    verify_directory_anchor(destination.parent, destination_fd,
                            handle["destination_parent_stat"])
    try:
        entry_stat(handle["parent_fd"], handle["path"].name)
        raise common.OperatorError("artifact source entry remains after quarantine rename")
    except FileNotFoundError:
        pass
    moved = entry_stat(destination_fd, destination.name)
    if (common.stat_record(moved) != handle["stat"] or
            common.stat_record(os.fstat(handle["fd"])) != handle["stat"]):
        raise common.OperatorError("quarantine destination is not the anchored inode")
    if hash_required:
        handle["target_sha256"] = common.sha256_fd(handle["fd"])
    if handle.get("target_sha256") != handle["item"]["sha256"]:
        raise common.OperatorError("quarantine target SHA256 changed after rename")
    marker_absent(handle["parent_fd"], handle["path"].name)
    marker_absent(destination_fd, destination.name)


def rollback_moves(handles):
    errors = []
    for handle in reversed(handles):
        if not handle.get("move_complete"):
            continue
        try:
            destination_fd = handle["destination_parent_fd"]
            verify_directory_anchor(handle["parent"], handle["parent_fd"],
                                    handle["parent_stat"])
            verify_directory_anchor(handle["destination"].parent, destination_fd,
                                    handle["destination_parent_stat"])
            marker_absent(handle["parent_fd"], handle["path"].name)
            marker_absent(destination_fd, handle["destination"].name)
            try:
                entry_stat(handle["parent_fd"], handle["path"].name)
                raise common.OperatorError("rollback source path was recreated")
            except FileNotFoundError:
                pass
            moved = entry_stat(destination_fd, handle["destination"].name)
            if common.stat_record(moved) != handle["stat"]:
                raise common.OperatorError("rollback destination anchor changed")
            if common.sha256_fd(handle["fd"]) != handle["item"]["sha256"]:
                raise common.OperatorError("rollback destination SHA256 changed")
            renameat2_noreplace(destination_fd, handle["destination"].name,
                                handle["parent_fd"], handle["path"].name)
            common.fsync_directory(handle["parent"])
            common.fsync_directory(handle["destination"].parent)
            verify_live_anchor(handle)
            handle["destination"] = None
            handle["move_complete"] = False
            handle["target_sha256"] = None
        except Exception as error:
            errors.append({"index": handle["item"]["index"],
                           "error": type(error).__name__})
    return errors


def prove_all_sources_restored(handles):
    for handle in handles:
        verify_live_anchor(handle)
        destination_fd = handle.get("destination_parent_fd")
        destination = handle.get("destination_candidate")
        if destination_fd is None or destination is None:
            continue
        verify_directory_anchor(destination.parent, destination_fd,
                                handle["destination_parent_stat"])
        try:
            entry_stat(destination_fd, destination.name)
            raise common.OperatorError("quarantine destination remains after rollback")
        except FileNotFoundError:
            pass
        marker_absent(destination_fd, destination.name)
    return True


def apply(args, inspection):
    handles = inspection.pop("handles")
    protected = inspection.pop("protected_snapshot")
    evidence = quarantine_root(args.stamp)
    moved = []
    committed = False
    rollback = {"attempted": False, "complete": None, "errors": []}
    try:
        create_quarantine_root(evidence)
        common.write_exclusive_json(evidence / "before.json", inspection)
        for handle in handles:
            prepare_destination(evidence, handle)
        verify_ledger_evidence(args)
        for handle in handles:
            snapshot_and_guard(args, baseline=protected)
            verify_ledger_evidence(args)
            verify_current(args.expected_current_sha)
            move_to_quarantine(handle, moved, evidence)
            snapshot_and_guard(args, baseline=protected)
            verify_ledger_evidence(args)
            verify_current(args.expected_current_sha)
            common.write_exclusive_json(
                evidence / ("moved-%02d.json" % handle["item"]["index"]),
                artifact_receipt(handle))
        snapshot_and_guard(args, baseline=protected)
        final_ledger = verify_ledger_evidence(args)
        verify_current(args.expected_current_sha)
        if len(moved) != 6 or any(not handle["move_complete"] for handle in handles):
            raise common.OperatorError("quarantine did not move all six exact artifacts")
        for handle in handles:
            verify_quarantined_anchor(handle, hash_required=False)
        committed = True
        result = {"schema": 1, "result": "quarantined", "run_id": common.RUN_ID,
                  "host": common.HK_HOST, "preflight_sha256": PREFLIGHT_SHA256,
                  "stamp": args.stamp, "root": str(evidence),
                  "cpu_ledger_before": final_ledger,
                  "artifacts": [artifact_receipt(handle) for handle in handles],
                  "moved_count": 6, "deleted_count": 0, "copied_count": 0,
                  "adopted_count": 0, "markers_created": 0, "services_changed": 0,
                  "worker_remained_stopped": True,
                  "tunnel_and_protected_units_unchanged": True,
                  "durable_move_intents": 6,
                  "cpu_after_ledger_evidence_required": True,
                  "protected_units_unchanged": True, "rollback": rollback,
                  "completed_at_epoch": time.time()}
        result_sha = common.write_exclusive_json(evidence / "result.json", result)
        return {"ok": True, "result": str(evidence / "result.json"),
                "result_sha256": result_sha, "moved_count": 6}
    except BaseException as error:
        if committed:
            failure = {"schema": 1, "result": "post_commit_evidence_failed",
                       "run_id": common.RUN_ID, "host": common.HK_HOST,
                       "root": str(evidence), "error_type": type(error).__name__,
                       "automatic_rollback_suppressed": True,
                       "moved_count": len(moved), "failed_at_epoch": time.time()}
            try:
                common.write_exclusive_json(evidence / "post-commit-failure.json", failure)
            except Exception:
                pass
            raise common.OperatorError(
                "POST-COMMIT: all six artifacts remain quarantined; result evidence is incomplete")
        rollback["attempted"] = bool(moved)
        rollback["errors"] = rollback_moves(moved)
        try:
            prove_all_sources_restored(handles)
        except Exception as proof_error:
            rollback["errors"].append({"stage": "prove-source-restoration",
                                       "error": type(proof_error).__name__})
        try:
            snapshot_and_guard(args, baseline=protected)
        except Exception as guard_error:
            rollback["errors"].append({"stage": "final-guard",
                                       "error": type(guard_error).__name__})
        rollback["complete"] = not rollback["errors"]
        failure = {"schema": 1, "result": "failed", "run_id": common.RUN_ID,
                   "host": common.HK_HOST, "error_type": type(error).__name__,
                   "moved_before_failure": len(moved), "rollback": rollback,
                   "failed_at_epoch": time.time()}
        if common.path_lexists(evidence):
            try:
                common.write_exclusive_json(evidence / "failure.json", failure)
            except Exception:
                pass
        if not rollback["complete"]:
            raise common.OperatorError("HIGH RISK: quarantine failed and rollback is incomplete")
        raise
    finally:
        for handle in handles:
            close_artifact(handle)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--run-id", required=True)
    result.add_argument("--stamp", required=True)
    result.add_argument("--expected-host", required=True)
    result.add_argument("--data-root", required=True)
    result.add_argument("--expected-data-device", required=True)
    result.add_argument("--expected-current-sha", required=True)
    result.add_argument("--preflight", required=True)
    result.add_argument("--preflight-sha256", required=True)
    result.add_argument("--ledger-evidence", required=True)
    result.add_argument("--ledger-evidence-sha256", required=True)
    result.add_argument("--ffprobe", required=True)
    result.add_argument("--expected-ffprobe-sha256", default="")
    result.add_argument("--unit", action="append", default=[])
    result.add_argument("--protected-unit", action="append", default=[])
    result.add_argument("--fragment", action="append", default=[])
    result.add_argument("--apply", action="store_true")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    validate_cli(args)
    if not args.apply:
        result = inspect(args, keep_open=False)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    control = common.HK_DATA_ROOT / "migrations" / common.RUN_ID / "control"
    common.real_directory(control)
    # Share the exact HK mutation lock with drama_release.py.  This prevents a
    # current switch or worker start from racing any quarantine rename.
    with common.exclusive_lock(control / ".drama-release-hk.lock"):
        destination = quarantine_root(args.stamp)
        if common.path_lexists(destination):
            raise common.OperatorError("quarantine evidence/destination already exists")
        inspection = inspect(args, keep_open=True)
        with interruption_guard():
            result = apply(args, inspection)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except common.OperatorError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        sys.exit(78)
