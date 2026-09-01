#!/usr/bin/env python3
"""Deploy the exact reviewed drama release on CPU or HK; dry-run by default.

No command in this file publishes, retries or resumes a drama job.  The CPU
operation replaces only app.py and features/drama_synthesis/async_runtime.py.
The HK operation creates one immutable Git release and atomically changes the
current symlink.  Every apply is bound to the approved migration run, hosts,
commits, data roots, filesystem device and exact systemd unit names.
"""
from __future__ import print_function

import argparse
import contextlib
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import stat
import sys
import time

import drama_operator_common as common


JOB_IDS = (
    "679e7c49acbf4af79f78bf60d76c5dd7",
    "b6e0bc51bb3f44e19c12b20cef7b93fe",
)
HK_RUNTIME_FINGERPRINTS = {
    JOB_IDS[0]: "f7f96fa4144c00f127e7b4f2b1dbc920f2a3902729ce4da77a0dbe76f2ba852e",
    JOB_IDS[1]: "60dac1dd63668b5a60724dc6c92b475fdb4ddd1d252ce9104e81749d16142c3c",
}
HK_RUNTIME_FILE_SHA256 = {
    JOB_IDS[0]: "fe204d9ce3931cb9c55d4328e26b99f9afa235c2453152284e5b20fc178c65f5",
    JOB_IDS[1]: "c92203d0baf1507d1d37e50252be0a0c8a341b583a60ae37e61540c15397512c",
}
HK_RUNTIME_RECORD_MAX_BYTES = 4 * 1024 * 1024
HK_DOWNLOAD_PARTS = (
    {
        "job_id": JOB_IDS[0], "episode": "002",
        "part_inode": 1709371, "part_size": 8388608,
        "part_sha256": "9c5b7b48d41b0e6503f1f9b894e2086b381d94de1f21453910f7bbeea9a754ad",
        "record_inode": 1709373, "record_size": 280,
        "record_sha256": "80c110ff1112e06f6fee80815ad855bbb81860c8a2ca69c62545d9fb2a2e923c",
        "expected_size": 214348452,
    },
    {
        "job_id": JOB_IDS[0], "episode": "003",
        "part_inode": 1709227, "part_size": 319029248,
        "part_sha256": "4268d78394d012bef2b09306db6ce2b74e7e9245c5600217cec37d48f8e4be2b",
        "record_inode": 1709372, "record_size": 282,
        "record_sha256": "7e5655ae516fad5e3d34102ebf6af532dc6e0a2dfbcf442657aef049ca863276",
        "expected_size": 349379561,
    },
    {
        "job_id": JOB_IDS[0], "episode": "004",
        "part_inode": 1709370, "part_size": 9437184,
        "part_sha256": "24eff40e573e808f6e973d14ff3615c43aa82c9861c4a706a52132051f40f3d1",
        "record_inode": 1709377, "record_size": 280,
        "record_sha256": "79522a771097eb96645425925ac9dbad373a237f7539effb9455f59e84d520ee",
        "expected_size": 226154892,
    },
    {
        "job_id": JOB_IDS[0], "episode": "005",
        "part_inode": 1709375, "part_size": 0,
        "part_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "record_inode": 1709376, "record_size": 274,
        "record_sha256": "244a61e9711a2208ebabc6b2e6feb6ab336980641b6af607b41f5fd6845365fc",
        "expected_size": 154755915,
    },
    {
        "job_id": JOB_IDS[1], "episode": "002",
        "part_inode": 4065235, "part_size": 72089600,
        "part_sha256": "f1418a20b25e594d01cae655a6075e2652c442b61ce6613429e84540dc773f18",
        "record_inode": 4065270, "record_size": 279,
        "record_sha256": "5e54dee5181ff6a38edd694b779b95747d020f44f033ee934490e7cdbad0bded",
        "expected_size": 81370743,
    },
    {
        "job_id": JOB_IDS[1], "episode": "003",
        "part_inode": 4065239, "part_size": 4194304,
        "part_sha256": "92e83d6ac16ae1b976d7b6fb8fda776566b7ba808d3f836a8d56f62a7a9595da",
        "record_inode": 4065418, "record_size": 278,
        "record_sha256": "f6de9ac5a7a0550731e49d3a44b6a0e07208c2677ee70586ae20ea4a611564a4",
        "expected_size": 63707705,
    },
    {
        "job_id": JOB_IDS[1], "episode": "004",
        "part_inode": 4065237, "part_size": 141819904,
        "part_sha256": "26480461441ab4573f43a99a32411b42b45dccc964c68e980a4d83aca9990c83",
        "record_inode": 4065238, "record_size": 282,
        "record_sha256": "2a2c5e09c38ea00f1f56023f51e4961c6eeddc306b9c464ea1417766afc72caf",
        "expected_size": 163071840,
    },
    {
        "job_id": JOB_IDS[1], "episode": "005",
        "part_inode": 4065240, "part_size": 0,
        "part_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "record_inode": 4065417, "record_size": 272,
        "record_sha256": "21854d740ce57361256930ab8706faa99b15c318f769d51ac464822df53e0d4f",
        "expected_size": 88911492,
    },
)
CPU_DB = common.CPU_LIVE_ROOT / "data" / "drama_material_jobs.sqlite3"
HK_RELEASES = common.HK_BASE / "releases"
HK_CURRENT = common.HK_BASE / "current"
HK_WORK_ROOT = common.HK_BASE / "work" / "jobs"
HK_PUBLIC_ROOT = common.HK_BASE / "results" / "public"
HK_RUNTIME_ACTIVE = HK_WORK_ROOT / ".runtime" / "jobs"
HK_RUNTIME_DIAGNOSTICS = HK_WORK_ROOT / ".runtime" / "diagnostics"
MIN_FREE_BYTES = 30 * 1024 * 1024 * 1024
HK_REVIEWED_FAILURE_PATH = (
    common.HK_DATA_ROOT / "migrations" / common.RUN_ID / "drama-release" /
    common.NEW_SHA / "hk" / "failure.json"
)
HK_REVIEWED_FAILURE_SHA256 = (
    "78ba694be60342a4b3f94468b3a88c35c92061550025deea5aa76cbd5745b005"
)
HK_RETRY_ID = "hk-retry-restart-counter-20260901"
HK_RETRY_EVIDENCE = (
    common.HK_DATA_ROOT / "migrations" / common.RUN_ID / "drama-release" /
    common.NEW_SHA / HK_RETRY_ID
)
REVIEWED_FAILURE_MAX_BYTES = 64 * 1024


def role_contract(role):
    if role == "cpu":
        return {
            "host": common.CPU_HOST,
            "data_root": common.CPU_DATA_ROOT,
            "source_root": common.CPU_DATA_ROOT / "migrations" / common.RUN_ID /
                           "drama-release" / "source" / common.NEW_SHA,
            "target_units": common.CPU_TARGET_UNITS,
            "protected_units": (),
            "evidence": common.CPU_DATA_ROOT / "migrations" / common.RUN_ID /
                        "drama-release" / common.NEW_SHA / "cpu",
            "reviewed_failure_resume": None,
        }
    if role == "hk":
        return {
            "host": common.HK_HOST,
            "data_root": common.HK_DATA_ROOT,
            "source_root": common.HK_DATA_ROOT / "migrations" / common.RUN_ID /
                           "drama-release" / "source" / common.NEW_SHA,
            "target_units": common.HK_TARGET_UNITS,
            "protected_units": common.HK_PROTECTED_UNITS,
            "evidence": common.HK_DATA_ROOT / "migrations" / common.RUN_ID /
                        "drama-release" / common.NEW_SHA / "hk",
            "reviewed_failure_resume": None,
        }
    raise common.OperatorError("unknown host role")


def safe_git_env():
    return {
        "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent",
    }


def git_output(source, arguments):
    _, stdout, _ = common.run(
        ["git", "-C", str(source)] + list(arguments), env=safe_git_env())
    return stdout.strip()


def verify_source_checkout(source):
    source = pathlib.Path(source)
    common.real_directory(source, expected_parent=source.parent)
    if git_output(source, ["rev-parse", "--show-toplevel"]) != str(source):
        raise common.OperatorError("source checkout top-level path changed")
    if git_output(source, ["rev-parse", "HEAD"]) != common.NEW_SHA:
        raise common.OperatorError("source checkout is not the approved new commit")
    if git_output(source, ["rev-parse", common.NEW_REMOTE_REF]) != common.NEW_SHA:
        raise common.OperatorError("pushed remote branch does not resolve to the approved commit")
    if git_output(source, ["remote", "get-url", "origin"]) != common.GITHUB_REMOTE:
        raise common.OperatorError("source checkout origin is not the approved GitHub repository")
    if git_output(source, ["status", "--porcelain=v1", "--untracked-files=all"]):
        raise common.OperatorError("source checkout is not clean")
    tree = git_output(source, ["rev-parse", "HEAD^{tree}"])
    if not re.match(r"^[0-9a-f]{40}$", tree):
        raise common.OperatorError("source Git tree identity is invalid")
    files = {}
    for relative, expected in common.CPU_NEW_FILES.items():
        path = source / pathlib.PurePosixPath(relative)
        common.validate_existing_ancestry(path, trusted_root=source)
        actual = common.sha256_file(path)
        if actual != expected:
            raise common.OperatorError("approved source file SHA256 mismatch: %s" % relative)
        files[relative] = {"path": str(path), "sha256": actual,
                           "bytes": int(os.lstat(str(path)).st_size)}
    return {"path": str(source), "commit": common.NEW_SHA, "tree": tree,
            "remote": common.GITHUB_REMOTE, "remote_ref": common.NEW_REMOTE_REF,
            "clean": True, "files": files}


def fragment_token(item):
    return "%s|%s|%s" % (
        item["unit"], item["fragment"]["path"], item["fragment"]["sha256"])


def parse_fragment_tokens(values):
    result = {}
    for raw in values or []:
        parts = raw.split("|", 2)
        if len(parts) != 3 or not parts[1].startswith("/") or not re.match(r"^[0-9a-f]{64}$", parts[2]):
            raise common.OperatorError("fragment binding must be UNIT|ABSOLUTE_PATH|SHA256")
        if parts[0] in result:
            raise common.OperatorError("duplicate fragment binding")
        result[parts[0]] = {"path": parts[1], "sha256": parts[2]}
    return result


def validate_fragment_bindings(snapshot, supplied, required):
    live = {unit: {"path": item["fragment"]["path"],
                   "sha256": item["fragment"]["sha256"]}
            for unit, item in snapshot.items()}
    if supplied:
        if supplied != live:
            raise common.OperatorError("unit FragmentPath/SHA binding differs from live state")
    elif required:
        raise common.OperatorError("apply requires exact --fragment bindings from a fresh dry-run")
    return live


def validate_cli(args):
    contract = role_contract(args.role)
    if (args.run_id != common.RUN_ID or args.expected_host != contract["host"] or
            args.expected_old_sha != common.OLD_SHA or
            args.expected_new_sha != common.NEW_SHA or
            pathlib.Path(args.data_root) != contract["data_root"] or
            pathlib.Path(args.source_root) != contract["source_root"]):
        raise common.OperatorError("release identity/path arguments differ from the approved contract")
    if tuple(args.unit or ()) != contract["target_units"]:
        raise common.OperatorError("target unit scope or order changed")
    if tuple(args.protected_unit or ()) != contract["protected_units"]:
        raise common.OperatorError("protected unit scope or order changed")
    if not args.expected_data_device or any(ch.isspace() for ch in args.expected_data_device):
        raise common.OperatorError("expected data device binding is missing or invalid")
    resume = bool(getattr(args, "reviewed_failure_resume", False))
    failure_path = getattr(args, "reviewed_failure_path", None)
    failure_sha256 = getattr(args, "reviewed_failure_sha256", None)
    retry_id = getattr(args, "retry_id", None)
    if resume:
        if args.role != "hk":
            raise common.OperatorError("reviewed-failure resume is limited to the exact HK retry")
        if (pathlib.Path(failure_path or "") != HK_REVIEWED_FAILURE_PATH or
                failure_sha256 != HK_REVIEWED_FAILURE_SHA256 or
                retry_id != HK_RETRY_ID):
            raise common.OperatorError("reviewed-failure resume binding differs from the approved retry")
        contract = dict(contract)
        contract["evidence"] = HK_RETRY_EVIDENCE
        contract["reviewed_failure_resume"] = {
            "failure_path": str(HK_REVIEWED_FAILURE_PATH),
            "failure_sha256": HK_REVIEWED_FAILURE_SHA256,
            "retry_id": HK_RETRY_ID,
            "evidence": str(HK_RETRY_EVIDENCE),
        }
    else:
        if any(value is not None for value in (failure_path, failure_sha256, retry_id)):
            raise common.OperatorError(
                "reviewed-failure bindings require explicit --reviewed-failure-resume")
        contract = dict(contract)
        contract["reviewed_failure_resume"] = None
    return contract


def available_bytes(path):
    value = os.statvfs(str(path))
    return int(value.f_bavail) * int(value.f_frsize)


def inspect_cpu_database():
    value = common.validate_existing_ancestry(CPU_DB, trusted_root=common.CPU_LIVE_ROOT,
                                              require_root_owner=False)
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise common.OperatorError("CPU drama database is not a regular file")
    connection = sqlite3.connect("file:%s?mode=ro" % CPU_DB, uri=True, timeout=10)
    try:
        connection.execute("PRAGMA query_only=ON")
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick != "ok" or foreign:
            raise common.OperatorError("CPU drama database integrity check failed")
        tables = {str(row[0]) for row in
                  connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"drama_material_job", "drama_material_job_worker_lease"}
        if not required.issubset(tables):
            raise common.OperatorError("CPU drama job/lease schema is incomplete")
        active_jobs = int(connection.execute(
            "SELECT COUNT(*) FROM drama_material_job "
            "WHERE status NOT IN ('done','failed','cancelled')").fetchone()[0])
        active_leases = int(connection.execute(
            "SELECT COUNT(*) FROM drama_material_job_worker_lease "
            "WHERE status NOT IN ('idle','done','failed','missing','deleted')").fetchone()[0])
        columns = [str(row[1]) for row in
                   connection.execute("PRAGMA table_info(drama_material_job)")]
        identifier = "job_id" if "job_id" in columns else "id"
        rows = connection.execute(
            "SELECT %s,status FROM drama_material_job WHERE %s IN (?,?) ORDER BY %s" %
            (identifier, identifier, identifier), JOB_IDS).fetchall()
        expected = sorted([(JOB_IDS[0], "failed"), (JOB_IDS[1], "failed")])
        if active_jobs or active_leases or sorted(rows) != expected:
            raise common.OperatorError("CPU drama jobs/leases are not at the approved drain point")
        return {"path": str(CPU_DB), "quick_check": "ok",
                "foreign_key_violations": 0, "active_jobs": 0, "active_leases": 0,
                "approved_job_statuses": [{"job_id": row[0], "status": row[1]}
                                          for row in sorted(rows)]}
    finally:
        connection.close()


def directory_is_empty(path):
    common.real_directory(path, require_root_owner=False)
    return not any(pathlib.Path(path).iterdir())


def _json_object_without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _read_exact_fd(descriptor, size):
    chunks = []
    offset = 0
    while offset < size:
        amount = min(1024 * 1024, size - offset)
        if hasattr(os, "pread"):
            block = os.pread(descriptor, amount, offset)
        else:  # pragma: no cover - Linux production always has pread.
            os.lseek(descriptor, offset, os.SEEK_SET)
            block = os.read(descriptor, amount)
        if not block:
            raise common.OperatorError("HK runtime record was truncated while reading")
        chunks.append(block)
        offset += len(block)
    return b"".join(chunks)


def validate_hk_runtime_contract():
    expected = set(JOB_IDS)
    if (len(JOB_IDS) != 2 or len(expected) != 2 or
            set(HK_RUNTIME_FINGERPRINTS) != expected or
            set(HK_RUNTIME_FILE_SHA256) != expected or
            any(not re.fullmatch(r"[0-9a-f]{32}", job_id) for job_id in JOB_IDS) or
            any(not re.fullmatch(r"[0-9a-f]{64}", value)
                for value in HK_RUNTIME_FINGERPRINTS.values()) or
            any(not re.fullmatch(r"[0-9a-f]{64}", value)
                for value in HK_RUNTIME_FILE_SHA256.values())):
        raise common.OperatorError("HK runtime failure contract is malformed")


def validate_hk_download_contract():
    fields = {
        "job_id", "episode", "part_inode", "part_size", "part_sha256",
        "record_inode", "record_size", "record_sha256", "expected_size",
    }
    expected_identities = [(job_id, episode) for job_id in JOB_IDS
                           for episode in ("002", "003", "004", "005")]
    if (type(HK_DOWNLOAD_PARTS) is not tuple or len(HK_DOWNLOAD_PARTS) != 8 or
            any(type(item) is not dict or set(item) != fields
                for item in HK_DOWNLOAD_PARTS)):
        raise common.OperatorError("HK partial download contract scope is malformed")
    identities = [(item["job_id"], item["episode"])
                  for item in HK_DOWNLOAD_PARTS]
    if identities != expected_identities:
        raise common.OperatorError("HK partial download contract scope is malformed")
    for item in HK_DOWNLOAD_PARTS:
        if (not isinstance(item["job_id"], str) or
                item["job_id"] not in JOB_IDS or
                not isinstance(item["episode"], str) or
                not re.fullmatch(r"00[2-5]", item["episode"]) or
                type(item["part_inode"]) is not int or item["part_inode"] <= 0 or
                type(item["record_inode"]) is not int or item["record_inode"] <= 0 or
                type(item["part_size"]) is not int or item["part_size"] < 0 or
                type(item["record_size"]) is not int or
                not 0 < item["record_size"] <= HK_RUNTIME_RECORD_MAX_BYTES or
                type(item["expected_size"]) is not int or
                item["expected_size"] <= 0 or
                item["expected_size"] < item["part_size"] or
                not isinstance(item["part_sha256"], str) or
                not re.fullmatch(r"[0-9a-f]{64}", item["part_sha256"]) or
                not isinstance(item["record_sha256"], str) or
                not re.fullmatch(r"[0-9a-f]{64}", item["record_sha256"])):
            raise common.OperatorError("HK partial download contract value is malformed")


def hk_download_paths(item):
    part = (pathlib.Path(HK_WORK_ROOT) / item["job_id"] / "downloads" /
            (item["episode"] + ".mp4.part"))
    return part, part.with_name(part.name + ".json")


def _anchor_hk_download_file(path, expected_inode, expected_size, expected_sha256,
                             kind):
    common.validate_existing_ancestry(
        path, trusted_root=HK_WORK_ROOT, require_root_owner=False)
    try:
        before = os.lstat(str(path))
    except OSError:
        raise common.OperatorError("HK approved %s path is unreadable" % kind)
    if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or
            int(before.st_ino) != int(expected_inode) or
            int(before.st_size) != int(expected_size)):
        raise common.OperatorError("HK approved %s inode or size changed" % kind)
    try:
        descriptor, anchored_stat, digest = common.anchored_file(
            path, expected_sha256=expected_sha256,
            expected_inode=expected_inode, expected_size=expected_size)
    except OSError:
        raise common.OperatorError("HK approved %s changed during anchored open" % kind)
    return descriptor, anchored_stat, digest


def inspect_hk_download_checkpoint(item):
    part, record_path = hk_download_paths(item)
    part_fd, part_stat, part_digest = _anchor_hk_download_file(
        part, item["part_inode"], item["part_size"], item["part_sha256"],
        "partial download")
    try:
        part_opened = os.fstat(part_fd)
        try:
            part_current = os.lstat(str(part))
        except OSError:
            raise common.OperatorError("HK approved partial download changed after hashing")
        if (stat.S_ISLNK(part_current.st_mode) or
                not stat.S_ISREG(part_current.st_mode) or
                common.identity_tuple(part_opened) != common.identity_tuple(part_current)):
            raise common.OperatorError("HK approved partial download changed after hashing")
    finally:
        os.close(part_fd)
    record_fd, record_stat, record_digest = _anchor_hk_download_file(
        record_path, item["record_inode"], item["record_size"],
        item["record_sha256"], "partial download record")
    try:
        opened = os.fstat(record_fd)
        raw = _read_exact_fd(record_fd, int(opened.st_size))
        after = os.fstat(record_fd)
        try:
            current = os.lstat(str(record_path))
        except OSError:
            raise common.OperatorError("HK partial download record changed while reading")
        if (stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode) or
                common.identity_tuple(opened) != common.identity_tuple(after) or
                common.identity_tuple(opened) != common.identity_tuple(current) or
                common.sha256_bytes(raw) != record_digest):
            raise common.OperatorError("HK partial download record changed while reading")
    finally:
        os.close(record_fd)
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_json_object_without_duplicate_keys)
    except (UnicodeDecodeError, ValueError, TypeError):
        raise common.OperatorError("HK partial download record is not strict UTF-8 JSON")
    keys = {"version", "source_identity", "etag", "expected_size",
            "partial_size", "partial_sha256"}
    if (not isinstance(value, dict) or set(value) != keys or
            type(value.get("version")) is not int or value.get("version") != 1 or
            not isinstance(value.get("source_identity"), str) or
            not isinstance(value.get("etag"), str) or
            type(value.get("expected_size")) is not int or
            value.get("expected_size") != item["expected_size"] or
            type(value.get("partial_size")) is not int or
            value.get("partial_size") != item["part_size"] or
            value.get("partial_sha256") != part_digest or
            value["expected_size"] < value["partial_size"]):
        raise common.OperatorError("HK partial download record is outside the approved checkpoint")
    return {
        "job_id": item["job_id"], "episode": item["episode"],
        "relative_part_path": str(part.relative_to(HK_WORK_ROOT)).replace("\\", "/"),
        "relative_record_path":
            str(record_path.relative_to(HK_WORK_ROOT)).replace("\\", "/"),
        "expected_size": item["expected_size"],
        "partial_size": item["part_size"], "partial_sha256": part_digest,
        "part_stat": part_stat, "record_sha256": record_digest,
        "record_stat": record_stat,
    }


def hk_partial_path_sets():
    parts = set()
    records = set()
    for root in (HK_WORK_ROOT, HK_PUBLIC_ROOT):
        parts.update(pathlib.Path(root).rglob("*.part"))
        records.update(pathlib.Path(root).rglob("*.part.json"))
    return parts, records


def inspect_hk_download_checkpoints():
    validate_hk_download_contract()
    expected_parts = set()
    expected_records = set()
    for item in HK_DOWNLOAD_PARTS:
        part, record = hk_download_paths(item)
        expected_parts.add(part)
        expected_records.add(record)
    parts, records = hk_partial_path_sets()
    if parts != expected_parts or records != expected_records:
        raise common.OperatorError("HK partial download paths differ from the eight approved pairs")
    summaries = [inspect_hk_download_checkpoint(item) for item in HK_DOWNLOAD_PARTS]
    final_parts, final_records = hk_partial_path_sets()
    if final_parts != expected_parts or final_records != expected_records:
        raise common.OperatorError("HK partial download paths changed while inspecting")
    for summary in summaries:
        item = next(row for row in HK_DOWNLOAD_PARTS
                    if row["job_id"] == summary["job_id"] and
                    row["episode"] == summary["episode"])
        part, record = hk_download_paths(item)
        try:
            common.validate_existing_ancestry(
                part, trusted_root=HK_WORK_ROOT, require_root_owner=False)
            common.validate_existing_ancestry(
                record, trusted_root=HK_WORK_ROOT, require_root_owner=False)
            part_current = os.lstat(str(part))
            record_current = os.lstat(str(record))
        except OSError:
            raise common.OperatorError("HK partial download pair changed while inspecting")
        if (stat.S_ISLNK(part_current.st_mode) or
                not stat.S_ISREG(part_current.st_mode) or
                stat.S_ISLNK(record_current.st_mode) or
                not stat.S_ISREG(record_current.st_mode) or
                common.stat_record(part_current) != summary["part_stat"] or
                common.stat_record(record_current) != summary["record_stat"]):
            raise common.OperatorError("HK partial download pair changed while inspecting")
    return {
        "part_files": len(summaries), "part_record_files": len(summaries),
        "recoverable_downloads": summaries,
        "recoverable_downloads_sha256":
            common.sha256_bytes(common.canonical_bytes(summaries)),
    }


def inspect_hk_runtime_record(path, expected_job_id):
    path = pathlib.Path(path)
    if (expected_job_id not in JOB_IDS or
            path != pathlib.Path(HK_RUNTIME_ACTIVE) / (expected_job_id + ".json")):
        raise common.OperatorError("HK runtime job record path is outside the approved scope")
    try:
        before = os.lstat(str(path))
    except OSError:
        raise common.OperatorError("HK runtime job record path is unreadable")
    if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or
            int(before.st_size) <= 0 or
            int(before.st_size) > HK_RUNTIME_RECORD_MAX_BYTES):
        raise common.OperatorError("HK runtime job record is not an approved regular JSON file")
    descriptor, anchored_stat, digest = common.anchored_file(
        path, expected_inode=before.st_ino, expected_size=before.st_size)
    try:
        opened = os.fstat(descriptor)
        raw = _read_exact_fd(descriptor, int(opened.st_size))
        after = os.fstat(descriptor)
        try:
            current = os.lstat(str(path))
        except OSError:
            raise common.OperatorError("HK runtime job record changed while reading")
        if (stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode) or
                common.identity_tuple(opened) != common.identity_tuple(after) or
                common.identity_tuple(opened) != common.identity_tuple(current) or
                common.sha256_bytes(raw) != digest):
            raise common.OperatorError("HK runtime job record changed while reading")
    finally:
        os.close(descriptor)
    if digest != HK_RUNTIME_FILE_SHA256[expected_job_id]:
        raise common.OperatorError("HK runtime job record SHA256 differs from the approved failure")
    try:
        record = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_json_object_without_duplicate_keys)
    except (UnicodeDecodeError, ValueError, TypeError):
        raise common.OperatorError("HK runtime job record is not strict UTF-8 JSON")
    if not isinstance(record, dict):
        raise common.OperatorError("HK runtime job record root is not an object")
    if (type(record.get("version")) is not int or record.get("version") != 1 or
            record.get("job_id") != expected_job_id or
            type(record.get("generation")) is not int or record.get("generation") != 1 or
            record.get("status") != "failed" or record.get("stage") != "failed" or
            record.get("fingerprint") != HK_RUNTIME_FINGERPRINTS[expected_job_id] or
            not isinstance(record.get("error"), dict) or
            record["error"].get("code") != "gpu_render_failed" or
            record.get("_children") != {} or record.get("_launches") != {} or
            record.get("_resource_blocked") is not False or
            record.get("_cache_blocked") is not False):
        raise common.OperatorError("HK runtime job record is outside the approved failed state")
    return {
        "job_id": expected_job_id,
        "status": "failed",
        "stage": "failed",
        "generation": 1,
        "fingerprint": record["fingerprint"],
        "file_sha256": digest,
        "stat": anchored_stat,
    }


def inspect_hk_runtime():
    validate_hk_runtime_contract()
    validate_hk_download_contract()
    common.real_directory(common.HK_BASE)
    common.real_directory(HK_RELEASES)
    common.real_directory(HK_WORK_ROOT, require_root_owner=False)
    common.real_directory(HK_PUBLIC_ROOT, require_root_owner=False)
    common.real_directory(HK_RUNTIME_ACTIVE, require_root_owner=False)
    expected_names = {job_id + ".json" for job_id in JOB_IDS}
    entries = sorted(pathlib.Path(HK_RUNTIME_ACTIVE).iterdir(), key=lambda item: item.name)
    if len(entries) != len(JOB_IDS) or {item.name for item in entries} != expected_names:
        raise common.OperatorError("HK runtime job directory differs from the two approved records")
    records = [inspect_hk_runtime_record(
        pathlib.Path(HK_RUNTIME_ACTIVE) / (job_id + ".json"), job_id)
        for job_id in JOB_IDS]
    if not directory_is_empty(HK_RUNTIME_DIAGNOSTICS):
        raise common.OperatorError("HK runtime diagnostics directory is not empty")
    downloads = inspect_hk_download_checkpoints()
    final_entries = sorted(pathlib.Path(HK_RUNTIME_ACTIVE).iterdir(),
                           key=lambda item: item.name)
    if (len(final_entries) != len(JOB_IDS) or
            {item.name for item in final_entries} != expected_names):
        raise common.OperatorError("HK runtime job directory changed while inspecting")
    for record in records:
        path = pathlib.Path(HK_RUNTIME_ACTIVE) / (record["job_id"] + ".json")
        try:
            current = os.lstat(str(path))
        except OSError:
            raise common.OperatorError("HK runtime job directory changed while inspecting")
        if (stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode) or
                common.stat_record(current) != record["stat"]):
            raise common.OperatorError("HK runtime job directory changed while inspecting")
    if not directory_is_empty(HK_RUNTIME_DIAGNOSTICS):
        raise common.OperatorError("HK runtime diagnostics directory is not empty")
    return {"active_jobs": 0, "durable_failed_jobs": len(records),
            "durable_records": records,
            "durable_records_sha256": common.sha256_bytes(common.canonical_bytes(records)),
            "diagnostics": 0, **downloads,
            "work_root": str(HK_WORK_ROOT), "public_root": str(HK_PUBLIC_ROOT)}


def verify_reviewed_hk_failure(path, expected_sha256):
    path = pathlib.Path(path)
    if path != HK_REVIEWED_FAILURE_PATH or expected_sha256 != HK_REVIEWED_FAILURE_SHA256:
        raise common.OperatorError("reviewed HK failure path or SHA256 binding changed")
    value = common.validate_existing_ancestry(path, trusted_root=common.HK_DATA_ROOT)
    if (stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode) or
            int(value.st_size) <= 0 or int(value.st_size) > REVIEWED_FAILURE_MAX_BYTES):
        raise common.OperatorError("reviewed HK failure is not a bounded regular file")
    descriptor, record, digest = common.anchored_file(
        path, expected_sha256=HK_REVIEWED_FAILURE_SHA256,
        expected_inode=int(value.st_ino), expected_size=int(value.st_size))
    try:
        raw = _read_exact_fd(descriptor, int(value.st_size))
        opened_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = os.lstat(str(path))
    if (common.stat_record(opened_after) != record or
            common.stat_record(current) != record):
        raise common.OperatorError("reviewed HK failure changed while reading")
    try:
        failure = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_json_object_without_duplicate_keys)
    except (UnicodeDecodeError, ValueError, TypeError):
        raise common.OperatorError("reviewed HK failure is not strict JSON")
    expected_keys = {
        "schema", "result", "host_role", "run_id", "old_sha", "new_sha",
        "release_published", "error_type", "rollback", "failed_at_epoch",
    }
    if not isinstance(failure, dict) or set(failure) != expected_keys:
        raise common.OperatorError("reviewed HK failure fields changed")
    rollback = failure.get("rollback")
    if (type(failure.get("schema")) is not int or failure.get("schema") != 1 or
            failure.get("result") != "failed" or
            failure.get("host_role") != "hk" or failure.get("run_id") != common.RUN_ID or
            failure.get("old_sha") != common.OLD_SHA or
            failure.get("new_sha") != common.NEW_SHA or
            failure.get("release_published") is not True or
            failure.get("error_type") != "OperatorError" or
            not isinstance(failure.get("failed_at_epoch"), (int, float)) or
            isinstance(failure.get("failed_at_epoch"), bool) or
            not isinstance(rollback, dict) or
            set(rollback) != {"attempted", "complete", "errors"} or
            rollback.get("attempted") is not True or rollback.get("complete") is not True or
            rollback.get("errors") != []):
        raise common.OperatorError("reviewed HK failure does not prove a complete safe rollback")
    return {
        "path": str(path), "sha256": digest, "stat": record,
        "result": "failed", "error_type": "OperatorError",
        "release_published": True, "rollback_complete": True,
        "rollback_errors": 0,
    }


def verify_existing_hk_release(release, expected_tree):
    release = pathlib.Path(release)
    expected = HK_RELEASES / common.NEW_SHA
    if release != expected:
        raise common.OperatorError("existing HK release path differs from the approved release")
    common.real_directory(release, expected_parent=HK_RELEASES)
    if git_output(release, ["rev-parse", "--show-toplevel"]) != str(release):
        raise common.OperatorError("existing HK release top-level path changed")
    if git_output(release, ["rev-parse", "HEAD"]) != common.NEW_SHA:
        raise common.OperatorError("existing HK release commit mismatch")
    if git_output(release, ["remote", "get-url", "origin"]) != common.GITHUB_REMOTE:
        raise common.OperatorError("existing HK release origin mismatch")
    if git_output(release, ["status", "--porcelain=v1", "--untracked-files=all"]):
        raise common.OperatorError("existing HK release is not clean")
    tree = git_output(release, ["rev-parse", "HEAD^{tree}"])
    if (not re.match(r"^[0-9a-f]{40}$", tree) or tree != expected_tree):
        raise common.OperatorError("existing HK release Git tree mismatch")
    files = {}
    for relative, expected_sha256 in common.CPU_NEW_FILES.items():
        path = release / pathlib.PurePosixPath(relative)
        common.validate_existing_ancestry(path, trusted_root=release)
        actual = common.sha256_file(path)
        if actual != expected_sha256:
            raise common.OperatorError("existing HK release file SHA256 mismatch: %s" % relative)
        files[relative] = {"sha256": actual, "bytes": int(os.lstat(str(path)).st_size)}
    return {
        "path": str(release), "commit": common.NEW_SHA, "tree": tree,
        "remote": common.GITHUB_REMOTE, "clean": True, "files": files,
    }


def initial_snapshot(args, contract):
    common.require_host(contract["host"])
    mount = common.validate_data_root(args.role, contract["data_root"], args.expected_data_device)
    if available_bytes(contract["data_root"]) < MIN_FREE_BYTES:
        raise common.OperatorError("less than 30 GiB remains on the bound data filesystem")
    source = verify_source_checkout(contract["source_root"])
    units = common.snapshot_units(contract["target_units"] + contract["protected_units"])
    fragments = validate_fragment_bindings(
        units, parse_fragment_tokens(args.fragment), args.apply)
    common.assert_no_media_processes()
    if args.role == "cpu":
        common.real_directory(common.CPU_LIVE_ROOT, require_root_owner=False)
        common.assert_inactive_unit(units[common.CPU_TARGET_UNITS[0]])
        common.assert_active_single_process(units[common.CPU_TARGET_UNITS[1]])
        common.assert_no_established_ports((8787, 18788))
        database = inspect_cpu_database()
        runtime = None
        live_files = {}
        for relative, expected in common.CPU_OLD_FILES.items():
            path = common.CPU_LIVE_ROOT / pathlib.PurePosixPath(relative)
            descriptor, record, digest = common.anchored_file(path, expected_sha256=expected)
            os.close(descriptor)
            live_files[relative] = {"path": str(path), "sha256": digest, "stat": record}
        current = None
    else:
        for unit in common.HK_TARGET_UNITS:
            common.assert_inactive_unit(units[unit])
        for unit in common.HK_PROTECTED_UNITS:
            common.assert_active_single_process(units[unit])
        common.assert_no_established_ports((8787,))
        database = None
        runtime = inspect_hk_runtime()
        live_files = None
        if not HK_CURRENT.is_symlink():
            raise common.OperatorError("HK current is not a symlink")
        old_release = HK_RELEASES / common.OLD_SHA
        common.real_directory(old_release)
        if os.path.realpath(str(HK_CURRENT)) != str(old_release):
            raise common.OperatorError("HK current is not the expected old release")
        if git_output(old_release, ["rev-parse", "HEAD"]) != common.OLD_SHA:
            raise common.OperatorError("HK current Git commit differs from expected old SHA")
        reviewed_failure = None
        existing_release = None
        existing_retry_link = None
        if contract.get("reviewed_failure_resume"):
            reviewed_failure = verify_reviewed_hk_failure(
                args.reviewed_failure_path, args.reviewed_failure_sha256)
            existing_release = verify_existing_hk_release(
                HK_RELEASES / common.NEW_SHA, source["tree"])
            existing_retry_link = inspect_hk_retry_link()
        elif common.path_lexists(HK_RELEASES / common.NEW_SHA):
            raise common.OperatorError("new HK release path already exists; inspect instead of adopting")
        current = {"link": str(HK_CURRENT), "target": os.readlink(str(HK_CURRENT)),
                   "resolved": str(old_release),
                   "lstat": common.stat_record(os.lstat(str(HK_CURRENT)))}
    target = {unit: common.unit_config_signature(units[unit])
              for unit in contract["target_units"]}
    protected = {unit: common.protected_signature(units[unit])
                 for unit in contract["protected_units"]}
    return {"schema": 1, "mode": "apply" if args.apply else "dry-run",
            "run_id": common.RUN_ID, "host_role": args.role, "host": contract["host"],
            "expected_old_sha": common.OLD_SHA, "expected_new_sha": common.NEW_SHA,
            "data": mount, "source": source, "fragments": fragments,
            "target_units": target, "protected_units": protected,
            "database": database, "runtime": runtime, "live_files": live_files,
            "current": current, "media_processes": [], "established_connections": 0,
            "reviewed_failure_resume": contract.get("reviewed_failure_resume"),
            "reviewed_failure": (reviewed_failure if args.role == "hk" else None),
            "existing_release": (existing_release if args.role == "hk" else None),
            "existing_retry_link": (
                existing_retry_link if args.role == "hk" else None),
            "unit_snapshot": units}


def compact_snapshot(snapshot):
    result = dict(snapshot)
    result.pop("unit_snapshot", None)
    result["required_fragment_arguments"] = [
        fragment_token(snapshot["unit_snapshot"][unit])
        for unit in sorted(snapshot["unit_snapshot"])
    ]
    result["ready"] = True
    return result


def phase(evidence, name, value):
    payload = {"schema": 1, "run_id": common.RUN_ID, "phase": name,
               "recorded_at_epoch": time.time(), "value": value}
    common.write_exclusive_json(pathlib.Path(evidence) / ("phase-%02d-%s.json" %
                                (len(list(pathlib.Path(evidence).glob("phase-*.json"))) + 1,
                                 name)), payload)


def copy_fd_to_exclusive(descriptor, destination, mode=0o600, uid=0, gid=0):
    destination = pathlib.Path(destination)
    output = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), mode)
    try:
        if os.name != "nt":
            os.fchown(output, int(uid), int(gid))
            os.fchmod(output, int(mode))
        offset = 0
        while True:
            if hasattr(os, "pread"):
                block = os.pread(descriptor, 1024 * 1024, offset)
            else:  # local Windows unit tests only
                os.lseek(descriptor, offset, os.SEEK_SET)
                block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            written = 0
            while written < len(block):
                count = os.write(output, block[written:])
                if count <= 0:
                    raise common.OperatorError("file copy made no progress")
                written += count
            offset += len(block)
        os.fsync(output)
    finally:
        os.close(output)
    common.fsync_directory(destination.parent)


def backup_cpu_files(evidence):
    backup = pathlib.Path(evidence) / "backup"
    os.mkdir(str(backup), 0o700)
    if os.name != "nt":
        os.chown(str(backup), 0, 0)
        os.chmod(str(backup), 0o700)
    common.fsync_directory(backup.parent)
    manifest = {"schema": 1, "run_id": common.RUN_ID, "host": common.CPU_HOST,
                "expected_old_sha": common.OLD_SHA, "files": []}
    for index, relative in enumerate(sorted(common.CPU_OLD_FILES)):
        live = common.CPU_LIVE_ROOT / pathlib.PurePosixPath(relative)
        descriptor, record, digest = common.anchored_file(
            live, expected_sha256=common.CPU_OLD_FILES[relative])
        try:
            target = backup / ("%02d-%s" % (index, pathlib.Path(relative).name))
            copy_fd_to_exclusive(descriptor, target)
        finally:
            os.close(descriptor)
        if common.sha256_file(target) != digest:
            raise common.OperatorError("CPU backup verification failed")
        manifest["files"].append({"relative": relative, "live": str(live),
                                  "backup": str(target), "sha256": digest,
                                  "live_stat": record})
    manifest_path = backup / "manifest.json"
    manifest_sha = common.write_exclusive_json(manifest_path, manifest)
    return {"directory": str(backup), "manifest": str(manifest_path),
            "manifest_sha256": manifest_sha, "files": manifest["files"]}


def create_cpu_swap(relative, evidence, swap_journal):
    if not isinstance(swap_journal, list):
        raise common.OperatorError("CPU swap journal must be a caller-owned list")
    source = role_contract("cpu")["source_root"] / pathlib.PurePosixPath(relative)
    live = common.CPU_LIVE_ROOT / pathlib.PurePosixPath(relative)
    source_fd, _, _ = common.anchored_file(
        source, expected_sha256=common.CPU_NEW_FILES[relative])
    old_fd, old_record, _ = common.anchored_file(
        live, expected_sha256=common.CPU_OLD_FILES[relative])
    temporary = live.parent / (".drama-release-%s-%s" %
                               (common.NEW_SHA[:12], live.name))
    try:
        if common.path_lexists(temporary):
            raise common.OperatorError("CPU swap temporary already exists")
        live_stat = os.fstat(old_fd)
        copy_fd_to_exclusive(
            source_fd, temporary, mode=stat.S_IMODE(live_stat.st_mode),
            uid=getattr(live_stat, "st_uid", 0), gid=getattr(live_stat, "st_gid", 0))
        if common.sha256_file(temporary) != common.CPU_NEW_FILES[relative]:
            raise common.OperatorError("CPU replacement temporary SHA256 mismatch")
        if common.identity_tuple(os.lstat(str(live))) != common.identity_tuple(os.fstat(old_fd)):
            raise common.OperatorError("CPU live file changed before atomic exchange")
        record = {"relative": relative, "live": str(live),
                  "temporary_old": str(temporary), "old_stat": old_record,
                  "new_sha256": common.CPU_NEW_FILES[relative],
                  "old_sha256": common.CPU_OLD_FILES[relative],
                  "exchange_complete": False,
                  "rollback_anchor_retained": False}
        common.atomic_rename_exchange(temporary, live)
        # The exchange is the mutation boundary.  Journal it before every
        # fallible verification/fsync so the outer transaction can always
        # restore the exact old bytes.
        record["exchange_complete"] = True
        record["rollback_anchor_retained"] = True
        swap_journal.append(record)
        if (common.sha256_file(live) != common.CPU_NEW_FILES[relative] or
                common.sha256_file(temporary) != common.CPU_OLD_FILES[relative]):
            raise common.OperatorError("CPU atomic exchange verification failed")
        common.fsync_directory(live.parent)
        return record
    finally:
        os.close(source_fd)
        os.close(old_fd)


def restore_cpu_swaps(swaps):
    errors = []
    for item in reversed(swaps):
        if not item.get("exchange_complete"):
            continue
        live = pathlib.Path(item["live"])
        temporary = pathlib.Path(item["temporary_old"])
        try:
            if (not common.path_lexists(temporary) or
                    common.sha256_file(live) != item["new_sha256"] or
                    common.sha256_file(temporary) != item["old_sha256"]):
                raise common.OperatorError("CPU rollback exchange anchors changed")
            common.atomic_rename_exchange(temporary, live)
            item["exchange_complete"] = False
            common.fsync_directory(live.parent)
            if common.sha256_file(live) != item["old_sha256"]:
                raise common.OperatorError("CPU rollback did not restore old bytes")
        except Exception as error:  # retain every failure in the rollback receipt
            errors.append({"relative": item["relative"], "error": type(error).__name__})
    return errors


def systemctl(action, unit):
    approved_units = set(common.CPU_TARGET_UNITS + common.HK_TARGET_UNITS)
    if action not in ("start", "stop") or unit not in approved_units:
        raise common.OperatorError("unapproved systemctl action or target")
    common.run(["systemctl", "--job-mode=ignore-dependencies", action, unit])


def wait_unit(unit, active, attempts=60):
    last = None
    for _ in range(attempts):
        last = common.unit_identity(unit)
        try:
            if active:
                common.assert_active_single_process(last)
            else:
                common.assert_inactive_unit(last)
            return last
        except common.OperatorError:
            time.sleep(0.5)
    raise common.OperatorError("unit did not converge to expected state: %s" % unit)


def config_unchanged(before, after, units):
    for unit in units:
        if common.unit_config_signature(before[unit]) != common.unit_config_signature(after[unit]):
            raise common.OperatorError("systemd unit definition drifted: %s" % unit)


def compile_cpu_files():
    code = ("import pathlib; "
            "[compile(pathlib.Path(p).read_bytes(), p, 'exec') for p in %r]" %
            [str(common.CPU_LIVE_ROOT / pathlib.PurePosixPath(relative))
             for relative in sorted(common.CPU_NEW_FILES)])
    common.run(["/usr/bin/python3", "-B", "-c", code])


def listener_owned_by(port, pid):
    _, stdout, _ = common.run(["ss", "-Hltnp", "sport = :%d" % int(port)])
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1 or ("pid=%d" % int(pid)) not in lines[0]:
        raise common.OperatorError("listener is not uniquely owned by the expected process")
    return {"port": int(port), "owner_pid": int(pid),
            "line_sha256": hashlib.sha256(lines[0].encode("utf-8")).hexdigest()}


def prove_cpu_rollback(baseline_units, restored_units, api):
    common.assert_inactive_unit(restored_units[common.CPU_TARGET_UNITS[0]])
    common.assert_active_single_process(restored_units[api])
    config_unchanged(baseline_units, restored_units, common.CPU_TARGET_UNITS)
    process = restored_units[api].get("process") or {}
    pid = int(process.get("pid") or 0)
    if pid <= 1:
        raise common.OperatorError("restored CPU API process identity is missing")
    return listener_owned_by(8787, pid)


def cleanup_cpu_temporaries(swaps):
    cleaned = []
    for item in swaps:
        temporary = pathlib.Path(item["temporary_old"])
        if common.sha256_file(temporary) != item["old_sha256"]:
            raise common.OperatorError("old CPU temporary changed before cleanup")
        os.unlink(str(temporary))
        item["rollback_anchor_retained"] = False
        cleaned.append({"relative": item["relative"], "path": str(temporary),
                        "old_sha256": item["old_sha256"]})
        common.fsync_directory(temporary.parent)
    return cleaned


def publish_authoritative_result(evidence, result, commit_state, host_role):
    evidence = pathlib.Path(evidence)
    if (not isinstance(commit_state, dict) or commit_state.get("committed") or
            host_role not in ("cpu", "hk")):
        raise common.OperatorError("authoritative result commit journal is invalid")
    result_path = evidence / "result.json"
    temporary = evidence / (".result-%s-%s.tmp" %
                            (host_role, common.NEW_SHA[:12]))
    if common.path_lexists(result_path) or common.path_lexists(temporary):
        raise common.OperatorError("authoritative result path already exists")
    payload = json.dumps(result, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    receipt_sha = common.sha256_bytes(payload)
    common.write_exclusive_bytes(temporary, payload)
    if common.sha256_file(temporary) != receipt_sha:
        raise common.OperatorError("authoritative result temporary SHA256 mismatch")
    common.atomic_rename_noreplace(temporary, result_path)
    # The no-replace rename is the authoritative result commit boundary.
    # Record it before every fallible directory fsync or anchored readback so
    # outer transactions can never roll back a published deployed result.
    commit_state.update({
        "committed": True, "result": str(result_path),
        "result_sha256": receipt_sha,
    })
    common.fsync_directory(evidence)
    descriptor, record, digest = common.anchored_file(
        result_path, expected_sha256=receipt_sha, expected_size=len(payload))
    try:
        raw = _read_exact_fd(descriptor, len(payload))
        opened_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = os.lstat(str(result_path))
    if (raw != payload or digest != receipt_sha or
            common.stat_record(opened_after) != record or
            common.stat_record(current) != record):
        raise common.OperatorError("authoritative result anchored readback failed")
    commit_state["result_stat"] = record
    return {"result": str(result_path), "result_sha256": receipt_sha,
            "result_stat": record}


def persist_cpu_result_and_cleanup(evidence, result, swaps, commit_state):
    if not isinstance(commit_state, dict) or commit_state.get("committed"):
        raise common.OperatorError("CPU commit journal is invalid")
    published = publish_authoritative_result(
        evidence, result, commit_state, "cpu")
    result_path = pathlib.Path(published["result"])
    receipt_sha = published["result_sha256"]
    cleaned = cleanup_cpu_temporaries(swaps)
    cleanup = {"schema": 1, "result": "rollback_temporaries_cleaned",
               "host_role": "cpu", "run_id": common.RUN_ID,
               "result_sha256": receipt_sha, "cleaned": cleaned,
               "completed_at_epoch": time.time()}
    cleanup_path = pathlib.Path(evidence) / "cleanup.json"
    cleanup_sha = common.write_exclusive_json(cleanup_path, cleanup)
    return {"result": str(result_path), "result_sha256": receipt_sha,
            "cleanup": str(cleanup_path), "cleanup_sha256": cleanup_sha}


def apply_cpu(args, contract, before):
    evidence = contract["evidence"]
    common.create_private_ancestry(contract["data_root"], evidence)
    phase(evidence, "preflight", compact_snapshot(before))
    backup = backup_cpu_files(evidence)
    phase(evidence, "backup", backup)
    baseline_units = before["unit_snapshot"]
    swaps = []
    api = common.CPU_TARGET_UNITS[1]
    api_stop_started = False
    commit_state = {"committed": False}
    rollback = {"attempted": False, "complete": None, "errors": []}
    try:
        common.assert_no_media_processes()
        common.assert_no_established_ports((8787, 18788))
        api_stop_started = True
        systemctl("stop", api)
        wait_unit(api, False)
        stopped = common.snapshot_units(common.CPU_TARGET_UNITS)
        common.assert_inactive_unit(stopped[common.CPU_TARGET_UNITS[0]])
        common.assert_inactive_unit(stopped[api])
        config_unchanged(baseline_units, stopped, common.CPU_TARGET_UNITS)
        common.assert_no_media_processes()
        inspect_cpu_database()
        phase(evidence, "cpu-stopped", {unit: common.protected_signature(item)
                                        for unit, item in stopped.items()})
        for relative in sorted(common.CPU_NEW_FILES):
            common.assert_no_media_processes()
            inspect_cpu_database()
            create_cpu_swap(relative, evidence, swaps)
        compile_cpu_files()
        phase(evidence, "cpu-files-switched", {"swaps": swaps})
        systemctl("start", api)
        wait_unit(api, True)
        start_anchor = common.snapshot_units(common.CPU_TARGET_UNITS)
        common.assert_inactive_unit(start_anchor[common.CPU_TARGET_UNITS[0]])
        common.assert_active_single_process(start_anchor[api])
        config_unchanged(baseline_units, start_anchor, common.CPU_TARGET_UNITS)
        # Validate the immediate post-start counter before any health or file
        # probes can extend the maintenance observation window.
        target_restart_bound(baseline_units, start_anchor, start_anchor, api)
        common.assert_no_media_processes()
        health = common.exact_health("127.0.0.1", 18788)
        for relative, expected in common.CPU_NEW_FILES.items():
            if common.sha256_file(common.CPU_LIVE_ROOT / pathlib.PurePosixPath(relative)) != expected:
                raise common.OperatorError("CPU live file changed after API start")
        after = common.snapshot_units(common.CPU_TARGET_UNITS)
        common.assert_inactive_unit(after[common.CPU_TARGET_UNITS[0]])
        common.assert_active_single_process(after[api])
        config_unchanged(baseline_units, after, common.CPU_TARGET_UNITS)
        restart_bound = target_restart_bound(
            baseline_units, start_anchor, after, api)
        listener = listener_owned_by(8787, after[api]["process"]["pid"])
        phase(evidence, "cpu-verified", {"health_hk": health, "api_listener": listener,
                                         "restart_bound": restart_bound,
                                         "units": {unit: common.protected_signature(item)
                                                   for unit, item in after.items()}})
        result = {"schema": 1, "result": "deployed", "host_role": "cpu",
                  "run_id": common.RUN_ID, "old_sha": common.OLD_SHA,
                  "new_sha": common.NEW_SHA, "backup": backup,
                  "changed_files": sorted(common.CPU_NEW_FILES),
                  "worker_remained_stopped": True, "api_health": health,
                   "api_listener": listener, "api_restart_bound": restart_bound,
                   "rollback": rollback,
                   "rollback_temporaries_retained_at_commit": [
                       {"relative": item["relative"],
                        "path": item["temporary_old"],
                        "old_sha256": item["old_sha256"]}
                       for item in swaps],
                   "cleanup_receipt_required": True,
                   "production_job_or_publish_calls": 0,
                   "completed_at_epoch": time.time()}
        receipts = persist_cpu_result_and_cleanup(evidence, result, swaps, commit_state)
        return dict({"ok": True, "host_role": "cpu"}, **receipts)
    except Exception as error:
        if commit_state.get("committed"):
            raise common.OperatorError(
                "HIGH RISK: authoritative CPU deployed result is published; "
                "automatic rollback and failure receipt are suppressed")
        rollback["attempted"] = bool(api_stop_started or swaps)
        if rollback["attempted"]:
            try:
                current_api = common.unit_identity(api)
                if current_api["systemd"].get("ActiveState") in ("active", "activating", "failed"):
                    systemctl("stop", api)
                    wait_unit(api, False)
            except Exception as stop_error:
                rollback["errors"].append({"stage": "stop-new-api",
                                           "error": type(stop_error).__name__})
            rollback["errors"].extend(restore_cpu_swaps(swaps))
            if not rollback["errors"]:
                try:
                    for relative, expected in common.CPU_OLD_FILES.items():
                        live = common.CPU_LIVE_ROOT / pathlib.PurePosixPath(relative)
                        if common.sha256_file(live) != expected:
                            raise common.OperatorError("CPU rollback left mixed live bytes")
                except Exception as bytes_error:
                    rollback["errors"].append({"stage": "prove-old-bytes",
                                               "error": type(bytes_error).__name__})
            if not rollback["errors"]:
                try:
                    systemctl("start", api)
                    wait_unit(api, True)
                except Exception as start_error:
                    rollback["errors"].append({"stage": "start-old-api",
                                               "error": type(start_error).__name__})
            if not rollback["errors"]:
                try:
                    restored_units = common.snapshot_units(common.CPU_TARGET_UNITS)
                    prove_cpu_rollback(baseline_units, restored_units, api)
                except Exception as proof_error:
                    rollback["errors"].append({"stage": "prove-old-api",
                                               "error": type(proof_error).__name__})
        rollback["complete"] = not rollback["errors"]
        failure = {"schema": 1, "result": "failed", "host_role": "cpu",
                   "run_id": common.RUN_ID, "old_sha": common.OLD_SHA,
                   "new_sha": common.NEW_SHA, "error_type": type(error).__name__,
                   "rollback": rollback, "failed_at_epoch": time.time()}
        try:
            common.write_exclusive_json(evidence / "failure.json", failure)
        except Exception:
            pass
        if not rollback["complete"]:
            raise common.OperatorError("HIGH RISK: CPU release failed and rollback is incomplete")
        raise


def clone_hk_release(source, stage):
    if common.path_lexists(stage):
        raise common.OperatorError("HK release staging path already exists")
    common.run(["git", "clone", "--no-local", "--no-hardlinks", "--no-checkout",
                str(source), str(stage)], env=safe_git_env())
    common.run(["git", "-C", str(stage), "checkout", "--detach", common.NEW_SHA],
               env=safe_git_env())
    common.run(["git", "-C", str(stage), "remote", "set-url", "origin",
                common.GITHUB_REMOTE], env=safe_git_env())
    if git_output(stage, ["rev-parse", "HEAD"]) != common.NEW_SHA:
        raise common.OperatorError("HK staged release commit mismatch")
    if git_output(stage, ["status", "--porcelain=v1", "--untracked-files=all"]):
        raise common.OperatorError("HK staged release is not clean")
    if git_output(stage, ["remote", "get-url", "origin"]) != common.GITHUB_REMOTE:
        raise common.OperatorError("HK staged release origin mismatch")
    for relative, expected in common.CPU_NEW_FILES.items():
        if common.sha256_file(stage / pathlib.PurePosixPath(relative)) != expected:
            raise common.OperatorError("HK staged release file SHA256 mismatch")
    common.fsync_directory(stage.parent)


def current_link_anchor():
    value = os.lstat(str(HK_CURRENT))
    if not stat.S_ISLNK(value.st_mode):
        raise common.OperatorError("HK current stopped being a symlink")
    target = os.readlink(str(HK_CURRENT))
    if os.path.realpath(str(HK_CURRENT)) != str(HK_RELEASES / common.OLD_SHA):
        raise common.OperatorError("HK current no longer points at the old release")
    return {"stat": common.stat_record(value), "target": target}


def hk_current_temporary_path():
    return common.HK_BASE / (".current-%s-%s" % (common.RUN_ID, common.NEW_SHA[:12]))


def inspect_hk_retry_link():
    path = hk_current_temporary_path()
    try:
        value = os.lstat(str(path))
    except OSError:
        raise common.OperatorError("reviewed HK retry current symlink is missing")
    target = os.readlink(str(path)) if stat.S_ISLNK(value.st_mode) else None
    release = HK_RELEASES / common.NEW_SHA
    if (not stat.S_ISLNK(value.st_mode) or target != str(release) or
            os.path.realpath(str(path)) != str(release)):
        raise common.OperatorError("reviewed HK retry current symlink target changed")
    return {
        "path": str(path), "target": target, "resolved": str(release),
        "stat": common.stat_record(value),
    }


def assert_hk_retry_link(anchor):
    if not isinstance(anchor, dict) or anchor.get("path") != str(hk_current_temporary_path()):
        raise common.OperatorError("reviewed HK retry current symlink anchor is invalid")
    current = inspect_hk_retry_link()
    if current != anchor:
        raise common.OperatorError("reviewed HK retry current symlink inode or target changed")
    return current


def assert_hk_current_release(expected_release):
    value = os.lstat(str(HK_CURRENT))
    if not stat.S_ISLNK(value.st_mode):
        raise common.OperatorError("HK current is not a symlink")
    if os.path.realpath(str(HK_CURRENT)) != str(expected_release):
        raise common.OperatorError("HK current does not point at the expected release")


def switch_hk_current(record, existing_retry_link=None):
    if not isinstance(record, dict) or record:
        raise common.OperatorError("HK current journal must be an empty caller-owned dict")
    anchor = current_link_anchor()
    temporary = hk_current_temporary_path()
    if existing_retry_link is None:
        if common.path_lexists(temporary):
            raise common.OperatorError("HK current temporary already exists")
        os.symlink(str(HK_RELEASES / common.NEW_SHA), str(temporary))
        common.fsync_directory(common.HK_BASE)
        new_link_anchor = {
            "path": str(temporary),
            "target": str(HK_RELEASES / common.NEW_SHA),
            "resolved": str(HK_RELEASES / common.NEW_SHA),
            "stat": common.stat_record(os.lstat(str(temporary))),
        }
        reused = False
    else:
        new_link_anchor = assert_hk_retry_link(existing_retry_link)
        reused = True
    current = os.lstat(str(HK_CURRENT))
    if common.stat_record(current) != anchor["stat"] or os.readlink(str(HK_CURRENT)) != anchor["target"]:
        raise common.OperatorError("HK current changed before atomic exchange")
    record.update({"old_link_temporary": str(temporary),
                   "old_target": anchor["target"],
                   "new_target": str(HK_RELEASES / common.NEW_SHA),
                   "new_link_anchor": new_link_anchor,
                   "retry_link_original_path": str(temporary),
                   "reused_existing_temporary": reused,
                   "exchange_complete": False,
                   "old_link_retained": False})
    common.atomic_rename_exchange(temporary, HK_CURRENT)
    # Record the namespace mutation before every fallible durability/probe
    # operation so rollback never depends on the function returning normally.
    record["exchange_complete"] = True
    common.fsync_directory(common.HK_BASE)
    if (os.path.realpath(str(HK_CURRENT)) != str(HK_RELEASES / common.NEW_SHA) or
            common.stat_record(os.lstat(str(HK_CURRENT))) != new_link_anchor["stat"] or
            not temporary.is_symlink() or os.readlink(str(temporary)) != anchor["target"]):
        raise common.OperatorError("HK current exchange verification failed")
    return record


def hk_old_link_anchor(record):
    candidates = [pathlib.Path(record["old_link_temporary"])]
    destination = record.get("old_link_destination")
    if destination and pathlib.Path(destination) not in candidates:
        candidates.append(pathlib.Path(destination))
    present = [path for path in candidates if common.path_lexists(path)]
    if len(present) != 1:
        raise common.OperatorError("HK old current link location is ambiguous")
    path = present[0]
    value = os.lstat(str(path))
    if not stat.S_ISLNK(value.st_mode) or os.readlink(str(path)) != record["old_target"]:
        raise common.OperatorError("HK old current link anchor changed")
    return path


def retain_hk_old_link(record, evidence):
    source = hk_old_link_anchor(record)
    destination = pathlib.Path(evidence) / "current-before"
    record["old_link_destination"] = str(destination)
    record["old_link_move_started"] = True
    common.atomic_rename_noreplace(source, destination)
    # Update immediately after the namespace mutation, before fsync.
    record["old_link_temporary"] = str(destination)
    record["old_link_retained"] = True
    common.fsync_directory(destination.parent)
    return destination


def restore_hk_current(record):
    if not record.get("exchange_complete"):
        raise common.OperatorError("HK current journal has no completed exchange")
    temporary = hk_old_link_anchor(record)
    if os.path.realpath(str(HK_CURRENT)) != record["new_target"]:
        raise common.OperatorError("HK current rollback anchors changed")
    common.atomic_rename_exchange(temporary, HK_CURRENT)
    record["exchange_complete"] = False
    record["restored"] = True
    common.fsync_directory(common.HK_BASE)
    assert_hk_current_release(HK_RELEASES / common.OLD_SHA)
    if record.get("reused_existing_temporary"):
        original = pathlib.Path(record["retry_link_original_path"])
        if temporary != original:
            common.atomic_rename_noreplace(temporary, original)
            record["old_link_temporary"] = str(original)
            common.fsync_directory(original.parent)
        assert_hk_retry_link(record["new_link_anchor"])


def target_restart_bound(baseline, start_anchor, final, unit):
    baseline_restarts = int(baseline[unit]["systemd"].get("NRestarts") or 0)
    start_restarts = int(start_anchor[unit]["systemd"].get("NRestarts") or 0)
    final_restarts = int(final[unit]["systemd"].get("NRestarts") or 0)
    baseline_config = common.unit_config_signature(baseline[unit])
    start_config = common.unit_config_signature(start_anchor[unit])
    final_config = common.unit_config_signature(final[unit])
    if baseline_config != start_config or start_config != final_config:
        raise common.OperatorError("target unit definition changed during restart")
    common.assert_active_single_process(start_anchor[unit])
    common.assert_active_single_process(final[unit])

    def process_identity(item):
        process = item["process"]
        values = item["systemd"]
        try:
            identity = {
                "pid": int(process["pid"]),
                "startticks": int(process["startticks"]),
                "exec_main_start_monotonic":
                    int(values["ExecMainStartTimestampMonotonic"]),
                "active_enter_monotonic":
                    int(values["ActiveEnterTimestampMonotonic"]),
            }
        except (KeyError, TypeError, ValueError):
            raise common.OperatorError("target unit process start identity is incomplete")
        if any(value <= 0 for value in identity.values()):
            raise common.OperatorError("target unit process start identity is invalid")
        return identity

    start_identity = process_identity(start_anchor[unit])
    final_identity = process_identity(final[unit])
    if baseline_restarts < 0 or start_restarts < 0 or final_restarts < 0:
        raise common.OperatorError("target unit restart count is negative")
    # The explicit stop/start may reset systemd's historical counter.  The
    # baseline is evidence only: it must never enlarge the post-start budget.
    # Once the immediate start anchor is captured, no further manual reset is
    # permitted, so the counter must remain monotonic and at most one.
    if start_restarts not in (0, 1):
        raise common.OperatorError(
            "target unit restart count exceeded one maintenance window at start anchor")
    if final_restarts < start_restarts or final_restarts > 1:
        raise common.OperatorError("target unit restart count exceeded one maintenance window")
    if final_restarts == start_restarts:
        if final_identity != start_identity:
            raise common.OperatorError(
                "target unit process identity changed without an observed automatic restart")
    else:
        if ((final_identity["pid"], final_identity["startticks"]) ==
                (start_identity["pid"], start_identity["startticks"]) or
                final_identity["exec_main_start_monotonic"] <=
                start_identity["exec_main_start_monotonic"] or
                final_identity["active_enter_monotonic"] <
                start_identity["active_enter_monotonic"]):
            raise common.OperatorError(
                "target unit automatic restart process identity is not newer")
    return {
        "baseline": baseline_restarts,
        "start": start_restarts,
        "final": final_restarts,
        "counter_reset_possible": baseline_restarts > 0 and start_restarts <= 1,
        "automatic_restarts_after_start_anchor": final_restarts - start_restarts,
        "allowed_final_min": start_restarts,
        "allowed_final_max": 1,
        "automatic_restart_limit": 1,
        "start_process_identity": start_identity,
        "final_process_identity": final_identity,
    }


def hk_runtime_identity(runtime):
    if not isinstance(runtime, dict):
        raise common.OperatorError("HK runtime summary is missing")
    fields = (
        "durable_records_sha256", "recoverable_downloads_sha256",
        "durable_failed_jobs", "part_files", "part_record_files",
    )
    result = {field: runtime.get(field) for field in fields}
    if (not re.fullmatch(r"[0-9a-f]{64}", str(result["durable_records_sha256"])) or
            not re.fullmatch(r"[0-9a-f]{64}", str(result["recoverable_downloads_sha256"])) or
            result["durable_failed_jobs"] != len(JOB_IDS) or
            result["part_files"] != len(HK_DOWNLOAD_PARTS) or
            result["part_record_files"] != len(HK_DOWNLOAD_PARTS)):
        raise common.OperatorError("HK runtime identity is outside the approved failed checkpoint")
    result["exact_summary_sha256"] = common.sha256_bytes(common.canonical_bytes(runtime))
    return result


def assert_hk_runtime_unchanged(expected, actual):
    expected_identity = hk_runtime_identity(expected)
    actual_identity = hk_runtime_identity(actual)
    if expected_identity != actual_identity or expected != actual:
        raise common.OperatorError("HK failed runtime or partial checkpoint changed in maintenance window")
    return actual_identity


def apply_hk(args, contract, before):
    evidence = contract["evidence"]
    common.create_private_ancestry(contract["data_root"], evidence)
    phase(evidence, "preflight", compact_snapshot(before))
    baseline = before["unit_snapshot"]
    protected_before = {unit: baseline[unit] for unit in common.HK_PROTECTED_UNITS}
    release = HK_RELEASES / common.NEW_SHA
    stage = HK_RELEASES / (".stage-%s-%s" % (common.RUN_ID, common.NEW_SHA[:12]))
    current_record = {}
    resume = contract.get("reviewed_failure_resume")
    published = bool(resume)
    release_reused = bool(resume)
    started = []
    rollback = {"attempted": False, "complete": None, "errors": []}
    rollback_runtime_proof = None
    commit_state = {"committed": False}

    def guard_protected_and_idle():
        common.assert_no_media_processes()
        observed_runtime = inspect_hk_runtime()
        assert_hk_runtime_unchanged(before["runtime"], observed_runtime)
        live = common.snapshot_units(common.HK_PROTECTED_UNITS)
        common.assert_protected_units(protected_before, live)
        return live

    try:
        common.assert_no_media_processes()
        common.assert_no_established_ports((8787,))
        assert_hk_runtime_unchanged(before["runtime"], inspect_hk_runtime())
        if resume:
            reviewed_failure = verify_reviewed_hk_failure(
                args.reviewed_failure_path, args.reviewed_failure_sha256)
            existing_release = verify_existing_hk_release(
                release, before["source"]["tree"])
            existing_retry_link = assert_hk_retry_link(
                before["existing_retry_link"])
            if (reviewed_failure != before.get("reviewed_failure") or
                    existing_release != before.get("existing_release") or
                    existing_retry_link != before.get("existing_retry_link")):
                raise common.OperatorError(
                    "reviewed failure, release or retry symlink changed after preflight")
            phase(evidence, "hk-reviewed-failure-release-reused", {
                "retry_id": resume["retry_id"],
                "reviewed_failure": reviewed_failure,
                "existing_release": existing_release,
                "existing_retry_link": existing_retry_link,
                "clone_or_publish_calls": 0,
            })
        else:
            clone_hk_release(contract["source_root"], stage)
            guard_protected_and_idle()
            phase(evidence, "hk-release-staged", {"stage": str(stage),
                                                   "commit": common.NEW_SHA})
            common.atomic_rename_noreplace(stage, release)
            published = True
            common.fsync_directory(HK_RELEASES)
            if git_output(release, ["rev-parse", "HEAD"]) != common.NEW_SHA:
                raise common.OperatorError("published HK release commit mismatch")
            phase(evidence, "hk-release-published", {"release": str(release),
                                                      "commit": common.NEW_SHA})
        guard_protected_and_idle()
        switch_hk_current(
            current_record,
            before.get("existing_retry_link") if resume else None)
        guard_protected_and_idle()
        phase(evidence, "hk-current-switched", current_record)
        systemctl("start", common.HK_TARGET_UNITS[0])
        wait_unit(common.HK_TARGET_UNITS[0], True, attempts=120)
        started.append(common.HK_TARGET_UNITS[0])
        worker_start_anchor = common.snapshot_units(
            (common.HK_TARGET_UNITS[0],) + common.HK_PROTECTED_UNITS)
        common.assert_active_single_process(
            worker_start_anchor[common.HK_TARGET_UNITS[0]])
        common.assert_protected_units(
            protected_before,
            {unit: worker_start_anchor[unit] for unit in common.HK_PROTECTED_UNITS})
        worker_start_bound = target_restart_bound(
            baseline, worker_start_anchor, worker_start_anchor,
            common.HK_TARGET_UNITS[0])
        guard_protected_and_idle()
        worker = worker_start_anchor[common.HK_TARGET_UNITS[0]]
        if os.path.realpath(worker["process"]["cwd"]) != str(release):
            raise common.OperatorError("HK worker cwd is not the new release")
        if os.path.realpath(str(HK_CURRENT)) != str(release):
            raise common.OperatorError("HK current changed after worker start")
        common.assert_no_media_processes()
        worker_runtime = inspect_hk_runtime()
        assert_hk_runtime_unchanged(before["runtime"], worker_runtime)
        worker_health = common.exact_health("127.0.0.1", 8787)
        worker_listener = listener_owned_by(8787, worker["process"]["pid"])
        phase(evidence, "hk-worker-verified-before-tunnel", {
            "health": worker_health, "listener": worker_listener,
            "restart_start_anchor": worker_start_bound,
            "runtime": worker_runtime,
            "protected_units": {
                unit: common.protected_signature(worker_start_anchor[unit])
                for unit in common.HK_PROTECTED_UNITS
            },
        })
        systemctl("start", common.HK_TARGET_UNITS[1])
        wait_unit(common.HK_TARGET_UNITS[1], True, attempts=120)
        started.append(common.HK_TARGET_UNITS[1])
        tunnel_start_anchor = common.snapshot_units(
            common.HK_TARGET_UNITS + common.HK_PROTECTED_UNITS)
        for unit in common.HK_TARGET_UNITS:
            common.assert_active_single_process(tunnel_start_anchor[unit])
        common.assert_protected_units(
            protected_before,
            {unit: tunnel_start_anchor[unit] for unit in common.HK_PROTECTED_UNITS})
        worker_bound_at_tunnel_start = target_restart_bound(
            baseline, worker_start_anchor, tunnel_start_anchor,
            common.HK_TARGET_UNITS[0])
        tunnel_start_bound = target_restart_bound(
            baseline, tunnel_start_anchor, tunnel_start_anchor,
            common.HK_TARGET_UNITS[1])
        guard_protected_and_idle()
        phase(evidence, "hk-tunnel-start-anchored", {
            "worker_restart_bound": worker_bound_at_tunnel_start,
            "tunnel_restart_start_anchor": tunnel_start_bound,
            "protected_units": {
                unit: common.protected_signature(tunnel_start_anchor[unit])
                for unit in common.HK_PROTECTED_UNITS
            },
        })
        after = common.snapshot_units(common.HK_TARGET_UNITS + common.HK_PROTECTED_UNITS)
        for unit in common.HK_TARGET_UNITS:
            common.assert_active_single_process(after[unit])
        common.assert_protected_units(
            protected_before, {unit: after[unit] for unit in common.HK_PROTECTED_UNITS})
        restart_bounds = {
            common.HK_TARGET_UNITS[0]: target_restart_bound(
                baseline, worker_start_anchor, after, common.HK_TARGET_UNITS[0]),
            common.HK_TARGET_UNITS[1]: target_restart_bound(
                baseline, tunnel_start_anchor, after, common.HK_TARGET_UNITS[1]),
        }
        worker = after[common.HK_TARGET_UNITS[0]]
        if os.path.realpath(worker["process"]["cwd"]) != str(release):
            raise common.OperatorError("HK worker cwd is not the new release")
        common.assert_no_media_processes()
        runtime = inspect_hk_runtime()
        assert_hk_runtime_unchanged(before["runtime"], runtime)
        health = common.exact_health("127.0.0.1", 8787)
        listener = listener_owned_by(8787, worker["process"]["pid"])
        if os.path.realpath(str(HK_CURRENT)) != str(release):
            raise common.OperatorError("HK current changed after service start")
        phase(evidence, "hk-local-verified", {
            "health": health, "listener": listener, "restart_bounds": restart_bounds,
            "runtime": runtime,
            "protected_units": {unit: common.protected_signature(after[unit])
                                for unit in common.HK_PROTECTED_UNITS},
        })
        retain_hk_old_link(current_record, evidence)
        result = {"schema": 1, "result": "deployed_local_route_pending",
                  "host_role": "hk", "run_id": common.RUN_ID,
                  "old_sha": common.OLD_SHA, "new_sha": common.NEW_SHA,
                  "release": str(release), "current": str(HK_CURRENT),
                  "health_hk_8787": health, "listener_hk_8787": listener,
                  "restart_bounds": restart_bounds,
                  "release_reused": release_reused,
                  "retry_id": resume["retry_id"] if resume else None,
                  "reviewed_failure": before.get("reviewed_failure"),
                  "protected_units_unchanged": True,
                  "cpu_route_verification_required": True,
                  "cpu_route_command": "drama_release.py route (read-only)",
                  "production_job_or_publish_calls": 0,
                  "rollback": rollback, "completed_at_epoch": time.time()}
        published_result = publish_authoritative_result(
            evidence, result, commit_state, "hk")
        return {"ok": True, "result": published_result["result"],
                "result_sha256": published_result["result_sha256"], "host_role": "hk",
                "cpu_route_verification_required": True}
    except Exception as error:
        if commit_state.get("committed"):
            raise common.OperatorError(
                "HIGH RISK: authoritative HK deployed result is published; "
                "automatic rollback and failure receipt are suppressed")
        rollback["attempted"] = True
        for unit in reversed(common.HK_TARGET_UNITS):
            try:
                item = common.unit_identity(unit)
                if item["systemd"].get("ActiveState") in ("active", "activating", "failed"):
                    systemctl("stop", unit)
                    wait_unit(unit, False)
            except Exception as stop_error:
                rollback["errors"].append({"stage": "stop-new-target", "unit": unit,
                                           "error": type(stop_error).__name__})
        if current_record.get("exchange_complete"):
            try:
                restore_hk_current(current_record)
            except Exception as current_error:
                rollback["errors"].append({"stage": "restore-current",
                                           "error": type(current_error).__name__})
        # Both exact target units were required stopped before this release.
        # Rollback preserves that state; the protected eight units must remain
        # byte/process-identical and are never systemctl targets.
        try:
            stopped = common.snapshot_units(common.HK_TARGET_UNITS)
            for unit in common.HK_TARGET_UNITS:
                common.assert_inactive_unit(stopped[unit])
            assert_hk_current_release(HK_RELEASES / common.OLD_SHA)
            protected_after = common.snapshot_units(common.HK_PROTECTED_UNITS)
            common.assert_protected_units(protected_before, protected_after)
            if resume:
                assert_hk_retry_link(before["existing_retry_link"])
        except Exception as proof_error:
            rollback["errors"].append({"stage": "prove-rollback",
                                       "error": type(proof_error).__name__})
        try:
            rollback_runtime = inspect_hk_runtime()
            rollback_runtime_proof = assert_hk_runtime_unchanged(
                before["runtime"], rollback_runtime)
        except Exception as runtime_error:
            rollback["errors"].append({"stage": "prove-runtime-unchanged",
                                       "error": type(runtime_error).__name__})
        rollback["complete"] = not rollback["errors"]
        failure = {"schema": 1, "result": "failed", "host_role": "hk",
                   "run_id": common.RUN_ID, "old_sha": common.OLD_SHA,
                   "new_sha": common.NEW_SHA, "release_published": published,
                   "release_reused": release_reused,
                   "retry_id": resume["retry_id"] if resume else None,
                   "reviewed_failure": before.get("reviewed_failure"),
                   "error_type": type(error).__name__, "rollback": rollback,
                   "runtime_rollback_proof": rollback_runtime_proof,
                   "failed_at_epoch": time.time()}
        try:
            common.write_exclusive_json(evidence / "failure.json", failure)
        except Exception:
            pass
        if not rollback["complete"]:
            raise common.OperatorError("HIGH RISK: HK release failed and rollback is incomplete")
        raise


def process_ancestors(pid):
    result = []
    current = int(pid)
    for _ in range(32):
        if current <= 1 or current in result:
            break
        result.append(current)
        status = pathlib.Path("/proc/%d/status" % current).read_text()
        match = re.search(r"^PPid:\s+(\d+)\s*$", status, re.MULTILINE)
        if not match:
            raise common.OperatorError("listener process ancestry is unreadable")
        current = int(match.group(1))
    return result


def verify_cpu_route(args):
    if (args.run_id != common.RUN_ID or args.expected_host != common.CPU_HOST or
            args.expected_new_sha != common.NEW_SHA):
        raise common.OperatorError("CPU route verification binding changed")
    common.require_host(common.CPU_HOST)
    health = common.exact_health("127.0.0.1", 18788)
    _, stdout, _ = common.run(["ss", "-Hltnp", "sport = :18788"])
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise common.OperatorError("CPU 18788 listener count changed")
    pids = [int(value) for value in re.findall(r"pid=(\d+)", lines[0])]
    if len(set(pids)) != 1:
        raise common.OperatorError("CPU 18788 listener process identity is ambiguous")
    pid = pids[0]
    executable = os.path.basename(os.readlink("/proc/%d/exe" % pid)).lower()
    if "sshd" not in executable:
        raise common.OperatorError("CPU 18788 is not owned by sshd")
    ancestors = process_ancestors(pid)
    _, connections, _ = common.run(["ss", "-Hntp", "state", "established"])
    hk_lines = [line for line in connections.splitlines()
                if "43.154.250.89:" in line and any("pid=%d" % item in line
                                                    for item in ancestors)]
    if not hk_lines:
        raise common.OperatorError("CPU reverse listener cannot be attributed to the HK SSH peer")
    return {"ok": True, "mode": "read-only", "run_id": common.RUN_ID,
            "host": common.CPU_HOST, "expected_new_sha": common.NEW_SHA,
            "health_cpu_18788": health,
            "listener": {"port": 18788, "pid": pid,
                         "startticks": common.process_startticks(pid),
                         "exe": executable,
                         "line_sha256": hashlib.sha256(lines[0].encode("utf-8")).hexdigest()},
            "hk_peer": "43.154.250.89", "listener_owned_by_hk": True,
            "matching_connection_count": len(hk_lines),
            "production_job_or_publish_calls": 0,
            "checked_at_epoch": time.time()}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    for role in ("cpu", "hk"):
        item = sub.add_parser(role)
        item.add_argument("--run-id", required=True)
        item.add_argument("--expected-host", required=True)
        item.add_argument("--expected-old-sha", required=True)
        item.add_argument("--expected-new-sha", required=True)
        item.add_argument("--data-root", required=True)
        item.add_argument("--expected-data-device", required=True)
        item.add_argument("--source-root", required=True)
        item.add_argument("--unit", action="append", default=[])
        item.add_argument("--protected-unit", action="append", default=[])
        item.add_argument("--fragment", action="append", default=[])
        item.add_argument("--reviewed-failure-resume", action="store_true")
        item.add_argument("--reviewed-failure-path")
        item.add_argument("--reviewed-failure-sha256")
        item.add_argument("--retry-id")
        item.add_argument("--apply", action="store_true")
        item.set_defaults(role=role)
    route = sub.add_parser("route")
    route.add_argument("--run-id", required=True)
    route.add_argument("--expected-host", required=True)
    route.add_argument("--expected-new-sha", required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "route":
        result = verify_cpu_route(args)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    contract = validate_cli(args)
    before = initial_snapshot(args, contract)
    if not args.apply:
        if (contract.get("reviewed_failure_resume") and
                common.path_lexists(contract["evidence"])):
            raise common.OperatorError("reviewed-failure retry evidence path already exists")
        print(json.dumps(compact_snapshot(before), sort_keys=True, indent=2))
        return 0
    control = contract["data_root"] / "migrations" / common.RUN_ID / "control"
    common.real_directory(control)
    lock = control / (".drama-release-%s.lock" % args.role)
    with common.exclusive_lock(lock):
        # The second full snapshot closes the dry-run/apply and lock-acquisition race.
        before = initial_snapshot(args, contract)
        if common.path_lexists(contract["evidence"]):
            raise common.OperatorError("release evidence path already exists")
        result = (apply_cpu(args, contract, before) if args.role == "cpu"
                  else apply_hk(args, contract, before))
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except common.OperatorError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        sys.exit(78)
