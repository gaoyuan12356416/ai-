#!/usr/bin/env python3
"""Fail-closed launcher for the one reviewed Drama media acceptance case.

The public command is preview-only unless --apply is present. It accepts only a
reviewed candidate SHA, a fixed run id/trial, the reviewed short/long case and
one of three CPU/thread profiles. Fixed actions prepare one shared short source,
render, fully decode a matching result, or run a non-media 16 GiB guard probe.
It never accepts commands, paths, environment files, URLs, credentials, uploads
or production API operations.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import time


ACCEPTANCE_ROOT = Path("/data/drama-synthesis-gpu/acceptance/20260828-reliability")
CODE_ROOT = Path("/data/drama-synthesis-gpu/acceptance/code")
RUNS_ROOT = ACCEPTANCE_ROOT / "runs"
INPUT_ROOT = ACCEPTANCE_ROOT / "inputs"
CONTROL_ROOT = Path("/data/drama-synthesis-gpu/acceptance/control")
LOCK_PATH = CONTROL_ROOT / "media-benchmark.lock"
SUBMISSIONS_ROOT = CONTROL_ROOT / "submissions"
LONG_SOURCE = INPUT_ROOT / "case-679e7c49-concat.mp4"
LONG_SOURCE_SIZE = 5139047136
RUN_SOURCE_MANIFEST_NAME = "long-source.json"
PREPARED_SHORT_NAME = "case-679e7c49-intro-first120s.mp4"
PREPARED_SHORT_PART_NAME = PREPARED_SHORT_NAME + ".part"
PREPARE_EVIDENCE_NAME = "prepare-short.json"
RECIPE_PATH = INPUT_ROOT / "case-679e7c49-recipe.json"
ASSET_ROOT = Path("/data/drama-synthesis-gpu/assets/fb-v3-028326ab2114")
ASSET_MANIFEST_SHA256 = "028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f"
RECIPE_SHA256 = "56d60ff057e0da8eb08b0ef8063be0ef75d37d28a970c1c912ad915f8de9793f"
RECIPE_PROFILE = "drama-random-overlay-h264-720x1280-v1"
PYTHON_PATH = Path("/data/drama-synthesis-gpu/runtime/current/bin/python")
FFMPEG_PATH = Path("/data/drama-synthesis-gpu/runtime/bin/ffmpeg")
FFPROBE_PATH = Path("/data/drama-synthesis-gpu/runtime/bin/ffprobe")
RUNTIME_ROOT = Path("/data/drama-synthesis-gpu/runtime")
SYSTEMD_RUN_PATH = Path("/usr/bin/systemd-run")
SYSTEMCTL_PATH = Path("/usr/bin/systemctl")
NICE_PATH = Path("/usr/bin/nice")
GIT_PATH = Path("/usr/bin/git")
TARGET_USER = "drama-synthesis-gpu"
MEMORY_BYTES = 16 * 1024 * 1024 * 1024
MIN_START_MEM_AVAILABLE_BYTES = 24 * 1024 * 1024 * 1024
MIN_RUNNING_MEM_AVAILABLE_BYTES = 8 * 1024 * 1024 * 1024
TASKS_MAX = 128
RENDER_TIMEOUT_SECONDS = 43200
PREPARE_SHORT_SECONDS = 120
CHILD_REAP_TIMEOUT_SECONDS = 30
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
RUN_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{6,28}[a-z0-9])")
MAX_CANDIDATE_TRACKED_FILES = 4096
MAX_CANDIDATE_TRACKED_BYTES = 64 * 1024 * 1024
MAX_CANDIDATE_SINGLE_FILE_BYTES = 8 * 1024 * 1024
MAX_CANDIDATE_FILESYSTEM_ENTRIES = 8192


class LaunchFailure(RuntimeError):
    pass


class SubmissionUncertain(LaunchFailure):
    """The durable submit intent exists, so replay can no longer be assumed safe."""

    pass


def require(condition, code):
    if not condition:
        raise LaunchFailure(code)


def path_text(path):
    """Render reviewed Linux paths consistently, including offline Windows QA."""
    return Path(path).as_posix()


@dataclass(frozen=True)
class MediaConfig:
    name: str
    cpu_cores: int
    filter_threads: int
    cpu_quota: str


CONFIGS = (
    MediaConfig("2c2t", 2, 2, "200%"),
    MediaConfig("4c2t", 4, 2, "400%"),
    MediaConfig("4c4t", 4, 4, "400%"),
)
SHORT_TRIAL_CONFIG_ORDER = (
    ("r1", ("2c2t", "4c2t", "4c4t")),
    ("r2", ("4c4t", "4c2t", "2c2t")),
)


@dataclass(frozen=True)
class AcceptanceSpec:
    operation: str
    candidate_sha: str
    run_id: str
    sample_kind: str
    trial: str
    config: MediaConfig
    candidate_root: Path
    script_path: Path
    source_path: Path
    run_root: Path
    output_dir: Path
    prepared_short_path: Path
    prepare_evidence_path: Path
    launcher_guard_path: Path
    launcher_result_path: Path
    decode_evidence_path: Path
    run_source_manifest_path: Path
    resource_evidence_path: Path
    completion_evidence_path: Path
    submission_guard_path: Path
    submission_receipt_path: Path
    unit: str


def selected_config(name):
    matches = [value for value in CONFIGS if value.name == name]
    require(len(matches) == 1, "invalid_media_configuration")
    return matches[0]


def build_spec(candidate_sha, run_id, sample_kind, config_name, operation="render", trial="r1"):
    require(isinstance(candidate_sha, str) and SHA_PATTERN.fullmatch(candidate_sha),
            "invalid_candidate_sha")
    require(isinstance(run_id, str) and RUN_ID_PATTERN.fullmatch(run_id), "invalid_run_id")
    require(sample_kind in ("short", "long"), "invalid_sample_kind")
    require(trial in ("r1", "r2"), "invalid_trial")
    require(operation in ("render", "prepare-short", "decode", "guard-only"),
            "invalid_media_operation")
    config = selected_config(config_name)
    require(operation != "prepare-short" or
            (sample_kind == "short" and config.name == "2c2t"),
            "prepare_short_requires_short_2c2t")
    require(operation != "guard-only" or
            (sample_kind == "short" and config.name == "2c2t" and trial == "r1"),
            "guard_only_requires_short_2c2t")
    require(operation != "prepare-short" or trial == "r1",
            "prepare_short_requires_r1")
    candidate_root = CODE_ROOT / candidate_sha
    run_root = RUNS_ROOT / (candidate_sha + "-" + run_id)
    prepared_short = run_root / PREPARED_SHORT_NAME
    if operation == "prepare-short":
        unit = "drama-media-prepare-%s-%s.service" % (candidate_sha[:12], run_id)
    elif operation == "guard-only":
        unit = "drama-media-guard-%s-%s.service" % (candidate_sha[:12], run_id)
    elif operation == "decode":
        unit = "drama-media-decode-%s-%s-%s-%s-%s.service" % (
            candidate_sha[:12], run_id, sample_kind, config.name, trial
        )
    else:
        unit = "drama-media-accept-%s-%s-%s-%s-%s.service" % (
            candidate_sha[:12], run_id, sample_kind, config.name, trial
        )
    suffix = sample_kind + "-" + config.name + "-" + trial
    output = run_root / suffix
    action_suffix = operation + "-" + suffix
    submission_name = candidate_sha + "-" + run_id + "-" + action_suffix
    return AcceptanceSpec(
        operation=operation,
        candidate_sha=candidate_sha,
        run_id=run_id,
        sample_kind=sample_kind,
        trial=trial,
        config=config,
        candidate_root=candidate_root,
        script_path=candidate_root / "scripts/run_drama_media_acceptance.py",
        source_path=(LONG_SOURCE if operation == "prepare-short" or sample_kind == "long"
                     else prepared_short),
        run_root=run_root,
        output_dir=output,
        prepared_short_path=prepared_short,
        prepare_evidence_path=run_root / PREPARE_EVIDENCE_NAME,
        launcher_guard_path=run_root / ("launcher-guard-%s-%s.json" %
                                        (operation, suffix)),
        launcher_result_path=run_root / ("launcher-result-%s-%s.json" %
                                         (operation, suffix)),
        decode_evidence_path=run_root / ("decode-%s.json" % suffix),
        run_source_manifest_path=run_root / RUN_SOURCE_MANIFEST_NAME,
        resource_evidence_path=run_root / ("resource-%s.json" % action_suffix),
        completion_evidence_path=run_root / ("completion-%s.json" % action_suffix),
        submission_guard_path=SUBMISSIONS_ROOT / (submission_name + ".intent.json"),
        submission_receipt_path=SUBMISSIONS_ROOT / (submission_name + ".accepted.json"),
        unit=unit,
    )


def clean_environment():
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }


def preview(spec):
    value = {
        "version": 1,
        "ok": True,
        "apply": False,
        "media_started": False,
        "operation": spec.operation,
        "candidate_sha": spec.candidate_sha,
        "run_id": spec.run_id,
        "sample_kind": spec.sample_kind,
        "trial": spec.trial,
        "configuration": spec.config.name,
        "cpu_cores": spec.config.cpu_cores,
        "filter_threads": spec.config.filter_threads,
        "memory_bytes": MEMORY_BYTES,
        "tasks_max": TASKS_MAX,
        "minimum_start_mem_available_bytes": MIN_START_MEM_AVAILABLE_BYTES,
        "minimum_running_mem_available_bytes": MIN_RUNNING_MEM_AVAILABLE_BYTES,
        "render_timeout_seconds": RENDER_TIMEOUT_SECONDS,
        "unit": spec.unit,
        "cos_uploads": 0,
        "production_requests": 0,
    }
    if spec.operation == "render":
        value.update(source=path_text(spec.source_path),
                     output_dir=path_text(spec.output_dir))
        if spec.sample_kind == "short":
            order = dict(SHORT_TRIAL_CONFIG_ORDER)[spec.trial]
            value.update(prepared_short=path_text(spec.prepared_short_path),
                         prepare_evidence=path_text(spec.prepare_evidence_path),
                         trial_configuration_order=list(order),
                         trial_position=order.index(spec.config.name) + 1)
    elif spec.operation == "prepare-short":
        value.update(source=path_text(LONG_SOURCE),
                     prepared_short=path_text(spec.prepared_short_path),
                     prepare_evidence=path_text(spec.prepare_evidence_path))
    elif spec.operation == "decode":
        value.update(source=path_text(spec.output_dir / "result.mp4"),
                     decode_evidence=path_text(spec.decode_evidence_path))
    else:
        value.update(
            allocated_bytes=8 * 1024 * 1024,
            observed_seconds=3,
            ffmpeg_processes=0,
            ffprobe_processes=0,
            media_acceptance=False,
        )
    return value


def require_linux():
    require(sys.platform == "linux", "linux_required")


def require_regular_file(path, *, executable=False, allow_symlink=False):
    try:
        direct = os.lstat(path)
        target = os.stat(path)
    except OSError:
        raise LaunchFailure("fixed_input_unavailable") from None
    require(allow_symlink or not stat.S_ISLNK(direct.st_mode), "fixed_input_symlink_rejected")
    require(stat.S_ISREG(target.st_mode), "fixed_input_not_regular")
    require(not executable or os.access(path, os.X_OK), "fixed_executable_unavailable")
    return target


def require_secure_git_binary():
    try:
        direct = os.lstat(GIT_PATH)
        target = os.stat(GIT_PATH)
    except OSError:
        raise LaunchFailure("candidate_git_binary_unsafe") from None
    require(not stat.S_ISLNK(direct.st_mode) and stat.S_ISREG(direct.st_mode) and
            stat.S_ISREG(target.st_mode) and
            (direct.st_dev, direct.st_ino) == (target.st_dev, target.st_ino) and
            direct.st_uid == target.st_uid == 0 and
            stat.S_IMODE(target.st_mode) & 0o022 == 0 and
            os.access(GIT_PATH, os.X_OK), "candidate_git_binary_unsafe")


def require_root_owned_secure_path(path, kind, code):
    try:
        direct = os.lstat(path)
        target = os.stat(path)
    except OSError:
        raise LaunchFailure(code) from None
    expected_kind = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    require(kind in ("directory", "file") and not stat.S_ISLNK(direct.st_mode) and
            expected_kind(direct.st_mode) and expected_kind(target.st_mode) and
            (direct.st_dev, direct.st_ino) == (target.st_dev, target.st_ino) and
            direct.st_uid == target.st_uid == 0 and
            stat.S_IMODE(direct.st_mode) & 0o022 == 0 and
            stat.S_IMODE(target.st_mode) & 0o022 == 0,
            code)
    return target


def require_secure_directory_ancestors(path, code):
    path = Path(path)
    require(path.is_absolute(), code)
    for directory in reversed((path, *path.parents)):
        require_root_owned_secure_path(directory, "directory", code)


def require_secure_tracked_file(path, git_mode):
    value = require_root_owned_secure_path(
        path, "file", "candidate_worktree_file_permissions_unsafe"
    )
    executable = bool(stat.S_IMODE(value.st_mode) & 0o111)
    require(git_mode in ("100644", "100755") and
            executable == (git_mode == "100755"),
            "candidate_worktree_file_mode_mismatch")
    return value


def verify_candidate_filesystem_permissions(candidate_root, tree_entries):
    require_secure_directory_ancestors(
        candidate_root, "candidate_directory_permissions_unsafe"
    )
    root = Path(candidate_root)
    pending = [root]
    seen = 0
    marker_seen = False
    actual_files = set()
    actual_directories = {""}
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            raise LaunchFailure("candidate_directory_permissions_unsafe") from None
        seen += len(entries)
        require(seen <= MAX_CANDIDATE_FILESYSTEM_ENTRIES,
                "candidate_filesystem_too_large")
        for entry in entries:
            path = Path(entry.path)
            if directory == root and entry.name == ".git":
                marker_seen = True
                try:
                    marker = os.lstat(path)
                except OSError:
                    raise LaunchFailure("candidate_git_directory_unsafe") from None
                marker_kind = "directory" if stat.S_ISDIR(marker.st_mode) else "file"
                require_root_owned_secure_path(
                    path, marker_kind, "candidate_git_directory_unsafe"
                )
                continue
            try:
                value = os.lstat(path)
            except OSError:
                raise LaunchFailure("candidate_directory_permissions_unsafe") from None
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                raise LaunchFailure("candidate_filesystem_entry_unsafe") from None
            if stat.S_ISDIR(value.st_mode) and not stat.S_ISLNK(value.st_mode):
                require_root_owned_secure_path(
                    path, "directory", "candidate_directory_permissions_unsafe"
                )
                actual_directories.add(relative)
                pending.append(path)
            elif stat.S_ISREG(value.st_mode) and not stat.S_ISLNK(value.st_mode):
                require_root_owned_secure_path(
                    path, "file", "candidate_worktree_file_permissions_unsafe"
                )
                actual_files.add(relative)
            else:
                raise LaunchFailure("candidate_filesystem_entry_unsafe")
    require(marker_seen, "candidate_git_directory_unsafe")
    expected_files = set(tree_entries)
    expected_directories = {""}
    for relative in expected_files:
        parent = PurePosixPath(relative).parent
        while str(parent) != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    require(actual_files == expected_files and
            actual_directories == expected_directories,
            "candidate_checkout_not_clean_exact_sha")
    for relative, (git_mode, _blob) in tree_entries.items():
        pure = PurePosixPath(relative)
        require_secure_tracked_file(root.joinpath(*pure.parts), git_mode)


def verify_git_directory_security(raw_git_dir):
    require(isinstance(raw_git_dir, str) and raw_git_dir and
            "\r" not in raw_git_dir and "\n" not in raw_git_dir,
            "candidate_git_directory_unsafe")
    git_dir = Path(raw_git_dir)
    try:
        resolved = git_dir.resolve()
    except (OSError, ValueError):
        raise LaunchFailure("candidate_git_directory_unsafe") from None
    require(git_dir.is_absolute() and resolved == git_dir,
            "candidate_git_directory_unsafe")
    require_secure_directory_ancestors(git_dir, "candidate_git_directory_unsafe")
    return git_dir


def bounded_git(args, candidate_root, *, maximum=65536, input_bytes=None):
    require(type(args) is list and all(isinstance(item, str) and "\x00" not in item
                                       for item in args) and
            (input_bytes is None or
             (isinstance(input_bytes, bytes) and len(input_bytes) <= 1048576)),
            "candidate_git_check_failed")
    command = [
        path_text(GIT_PATH), "--no-pager", "--no-replace-objects",
        "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false",
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.bare=false",
        "-C", path_text(candidate_root),
        "--work-tree=" + path_text(candidate_root),
        *args,
    ]
    environment = {
        "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
    }
    try:
        with tempfile.TemporaryFile() as output:
            options = {
                "stdout": output, "stderr": subprocess.DEVNULL,
                "env": environment, "timeout": 15, "check": False,
            }
            if input_bytes is None:
                options["stdin"] = subprocess.DEVNULL
            else:
                options["input"] = input_bytes
            result = subprocess.run(command, **options)
            output.seek(0, os.SEEK_END)
            require(output.tell() <= maximum, "candidate_git_check_failed")
            output.seek(0)
            raw = output.read(maximum + 1)
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise LaunchFailure("candidate_git_check_failed") from None
    require(result.returncode == 0 and len(raw) <= maximum, "candidate_git_check_failed")
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeError:
        raise LaunchFailure("candidate_git_check_failed") from None


def verify_candidate(spec):
    require_secure_git_binary()
    try:
        candidate_stat = os.lstat(spec.candidate_root)
    except OSError:
        raise LaunchFailure("candidate_checkout_unavailable") from None
    require(stat.S_ISDIR(candidate_stat.st_mode) and not stat.S_ISLNK(candidate_stat.st_mode),
            "candidate_checkout_invalid")
    require(spec.candidate_root.resolve() == spec.candidate_root,
            "candidate_checkout_invalid")
    critical = (
        "scripts/run_drama_media_acceptance.py",
        "scripts/benchmark_drama_synthesis_media.py",
        "scripts/check_drama_media_resource_guard.py",
    )
    for relative in critical:
        require_regular_file(spec.candidate_root / relative)
    top_level = bounded_git(
        ["rev-parse", "--show-toplevel"], spec.candidate_root
    ).strip()
    bare = bounded_git(
        ["rev-parse", "--is-bare-repository"], spec.candidate_root
    ).strip()
    git_dir_text = bounded_git(
        ["rev-parse", "--absolute-git-dir"], spec.candidate_root
    ).strip()
    try:
        resolved_top_level = Path(top_level).resolve()
    except (OSError, ValueError):
        raise LaunchFailure("candidate_worktree_binding_invalid") from None
    require(top_level and "\n" not in top_level and "\r" not in top_level and
            resolved_top_level == spec.candidate_root and bare == "false",
            "candidate_worktree_binding_invalid")
    git_dir = verify_git_directory_security(git_dir_text)
    head = bounded_git(["rev-parse", "--verify", "HEAD^{commit}"], spec.candidate_root).strip()
    tree = bounded_git(["rev-parse", "--verify", "HEAD^{tree}"], spec.candidate_root).strip()
    replacements = bounded_git(
        ["for-each-ref", "--format=%(refname)", "refs/replace"], spec.candidate_root
    )
    require(SHA_PATTERN.fullmatch(head) and SHA_PATTERN.fullmatch(tree) and
            head == spec.candidate_sha and not replacements,
            "candidate_checkout_not_clean_exact_sha")
    tree_rows = bounded_git(
        ["ls-tree", "-r", "-z", "--full-tree", "HEAD"], spec.candidate_root,
        maximum=1048576,
    ).split("\x00")
    require(tree_rows[-1] == "" and
            1 <= len(tree_rows) - 1 <= MAX_CANDIDATE_TRACKED_FILES,
            "candidate_tree_invalid")
    tree_entries = {}
    tree_order = []
    for row in tree_rows[:-1]:
        match = re.fullmatch(r"(100644|100755) blob ([0-9a-f]{40})\t([^\x00\r\n]+)", row)
        require(match is not None, "candidate_tree_unsafe_entry")
        relative = match[3]
        pure = PurePosixPath(relative)
        require(not pure.is_absolute() and "\\" not in relative and
                ".." not in pure.parts and str(pure) == relative and
                relative not in tree_entries,
                "candidate_tree_unsafe_entry")
        tree_entries[relative] = (match[1], match[2])
        tree_order.append(relative)
    require(set(critical) <= set(tree_entries), "candidate_critical_files_untracked")
    index_rows = bounded_git(
        ["ls-files", "--stage", "-z"], spec.candidate_root, maximum=1048576
    ).split("\x00")
    require(index_rows[-1] == "", "candidate_index_invalid")
    indexed = {}
    for row in index_rows[:-1]:
        match = re.fullmatch(r"(100644|100755) ([0-9a-f]{40}) 0\t(.+)", row)
        require(match is not None and match[3] not in indexed,
                "candidate_index_invalid")
        indexed[match[3]] = (match[1], match[2])
    require(indexed == tree_entries, "candidate_index_not_exact_head")
    tracked = bounded_git(
        ["ls-files", "-v", "-z"], spec.candidate_root, maximum=1048576
    ).split("\x00")
    require(tracked[-1] == "", "candidate_index_flags_unsafe")
    tracked_paths = []
    for row in tracked[:-1]:
        match = re.fullmatch(r"H ([^\x00\r\n]+)", row)
        require(match is not None, "candidate_index_flags_unsafe")
        tracked_paths.append(match[1])
    require(set(tracked_paths) == set(tree_entries) and
            len(tracked_paths) == len(tree_entries), "candidate_index_flags_unsafe")
    verify_candidate_filesystem_permissions(spec.candidate_root, tree_entries)
    identities = {}
    total_bytes = 0
    for relative in tree_order:
        path = spec.candidate_root.joinpath(*PurePosixPath(relative).parts)
        before = require_secure_tracked_file(path, tree_entries[relative][0])
        require(stat.S_ISREG(before.st_mode) and not stat.S_ISLNK(before.st_mode) and
                0 <= before.st_size <= MAX_CANDIDATE_SINGLE_FILE_BYTES and
                before.st_nlink >= 1, "candidate_worktree_file_invalid")
        total_bytes += before.st_size
        require(total_bytes <= MAX_CANDIDATE_TRACKED_BYTES,
                "candidate_worktree_too_large")
        identities[relative] = {
            "blob": tree_entries[relative][1], "git_mode": tree_entries[relative][0],
            "device": before.st_dev, "inode": before.st_ino,
            "size_bytes": before.st_size, "mtime_ns": before.st_mtime_ns,
            "nlink": before.st_nlink, "uid": before.st_uid,
            "mode_bits": stat.S_IMODE(before.st_mode),
        }
    stdin_paths = b"".join(relative.encode("utf-8") + b"\n" for relative in tree_order)
    hashed = bounded_git(
        ["hash-object", "--no-filters", "--stdin-paths"], spec.candidate_root,
        maximum=262144, input_bytes=stdin_paths,
    ).splitlines()
    require(len(hashed) == len(tree_order) and all(SHA_PATTERN.fullmatch(value)
                                                   for value in hashed),
            "candidate_worktree_hash_invalid")
    for relative, blob in zip(tree_order, hashed):
        path = spec.candidate_root.joinpath(*PurePosixPath(relative).parts)
        after = require_secure_tracked_file(path, tree_entries[relative][0])
        expected = identities[relative]
        require(blob == expected["blob"] and stat.S_ISREG(after.st_mode) and
                not stat.S_ISLNK(after.st_mode) and
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                 after.st_nlink, after.st_uid, stat.S_IMODE(after.st_mode)) ==
                (expected["device"], expected["inode"], expected["size_bytes"],
                 expected["mtime_ns"], expected["nlink"], expected["uid"],
                 expected["mode_bits"]),
                "candidate_worktree_blob_mismatch")
    snapshot = hashlib.sha256(json.dumps(
        identities, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return {
        "head": head, "tree": tree, "snapshot_sha256": snapshot,
        "git_dir": path_text(git_dir),
        "tracked": identities,
        "critical": {relative: identities[relative] for relative in critical},
    }


def read_host_memory(path=Path("/proc/meminfo"), *,
                     minimum_bytes=MIN_START_MEM_AVAILABLE_BYTES,
                     low_code="host_memory_below_media_start_gate"):
    try:
        with path.open(encoding="ascii") as stream:
            text = stream.read(65537)
    except (OSError, UnicodeError):
        raise LaunchFailure("host_memory_unavailable") from None
    values = {}
    for line in text.splitlines():
        match = re.fullmatch(r"(MemTotal|MemAvailable):\s+([0-9]+) kB\s*", line)
        if match:
            require(match[1] not in values, "host_memory_invalid")
            values[match[1]] = int(match[2]) * 1024
    require(len(text) <= 65536 and set(values) == {"MemTotal", "MemAvailable"} and
            0 <= values["MemAvailable"] <= values["MemTotal"], "host_memory_invalid")
    require(type(minimum_bytes) is int and minimum_bytes >= 0 and
            re.fullmatch(r"[a-z0-9_]{1,100}", str(low_code)) is not None,
            "host_memory_invalid")
    require(values["MemAvailable"] >= minimum_bytes, low_code)
    return values


def validate_fixed_inputs(spec):
    if spec.operation == "guard-only":
        return
    if spec.operation == "prepare-short" or (
            spec.operation == "render" and spec.sample_kind == "long"):
        source = require_regular_file(LONG_SOURCE)
        require(source.st_size == LONG_SOURCE_SIZE, "fixed_long_source_size_mismatch")
    if spec.operation == "prepare-short":
        require_regular_file(FFMPEG_PATH, executable=True, allow_symlink=True)
        require_regular_file(FFPROBE_PATH, executable=True, allow_symlink=True)
        return
    if spec.operation == "decode":
        require_regular_file(FFMPEG_PATH, executable=True, allow_symlink=True)
        return
    require_regular_file(RECIPE_PATH)
    require_regular_file(FFMPEG_PATH, executable=True, allow_symlink=True)
    require_regular_file(FFPROBE_PATH, executable=True, allow_symlink=True)
    try:
        asset = os.lstat(ASSET_ROOT)
    except OSError:
        raise LaunchFailure("fixed_input_unavailable") from None
    require(stat.S_ISDIR(asset.st_mode) and not stat.S_ISLNK(asset.st_mode),
            "fixed_asset_root_invalid")
    try:
        with RECIPE_PATH.open(encoding="utf-8") as stream:
            raw = stream.read(131073)
        recipe = json.loads(raw)
    except (OSError, UnicodeError, ValueError):
        raise LaunchFailure("fixed_recipe_invalid") from None
    require(len(raw) <= 131072 and isinstance(recipe, dict) and
            recipe.get("recipe_sha256") == RECIPE_SHA256 and
            recipe.get("profile") == RECIPE_PROFILE and
            recipe.get("source") == "concat_video",
            "fixed_recipe_mismatch")


def fixed_runtime_python():
    require_regular_file(PYTHON_PATH, executable=True, allow_symlink=True)
    executable = Path(os.path.realpath(PYTHON_PATH))
    require_regular_file(executable, executable=True)
    return executable


def build_systemd_command(spec):
    python = fixed_runtime_python()
    for path in (SYSTEMD_RUN_PATH, NICE_PATH):
        require_regular_file(path, executable=True, allow_symlink=True)
    command = [
        path_text(SYSTEMD_RUN_PATH),
        "--unit=" + spec.unit,
        "--service-type=simple",
        "--property=WorkingDirectory=" + path_text(spec.candidate_root),
        "--property=RemainAfterExit=no",
        "--property=KillMode=control-group",
        "--property=TimeoutStopSec=90",
        "--property=RuntimeMaxSec=" + str(RENDER_TIMEOUT_SECONDS),
        "--property=PrivateDevices=no",
        "--property=ReadOnlyPaths=" + " ".join(path_text(path) for path in (
            spec.candidate_root, INPUT_ROOT, ASSET_ROOT, RUNTIME_ROOT
        )),
        "--property=CPUQuota=" + spec.config.cpu_quota,
        "--property=TasksMax=" + str(TASKS_MAX),
        "--property=MemoryLimit=" + str(MEMORY_BYTES),
        "--property=CPUAccounting=yes",
        "--property=MemoryAccounting=yes",
        "--property=TasksAccounting=yes",
        "--property=UMask=0077",
        "--property=NoNewPrivileges=yes",
        "--property=CapabilityBoundingSet=CAP_SETUID CAP_SETGID",
        "--property=IOSchedulingClass=best-effort",
        "--property=IOSchedulingPriority=7",
        path_text(NICE_PATH), "-n", "10",
        path_text(python), "-I", "-S", "-B", path_text(spec.script_path),
        "--candidate-sha", spec.candidate_sha,
        "--run-id", spec.run_id,
        "--sample-kind", spec.sample_kind,
        "--config", spec.config.name,
        "--trial", spec.trial,
    ]
    if spec.operation == "prepare-short":
        command.append("--prepare-short")
    elif spec.operation == "decode":
        command.append("--decode")
    elif spec.operation == "guard-only":
        command.append("--guard-only")
    command.extend(["--internal-stage", "guard"])
    return command


def ensure_public_apply_preflight(spec):
    require_linux()
    require(os.geteuid() == 0, "root_required_for_systemd_guard")
    verify_candidate(spec)
    validate_fixed_inputs(spec)
    if spec.operation == "prepare-short":
        require(not spec.run_root.exists() and not spec.run_root.is_symlink(),
                "run_root_must_be_new")
    elif spec.operation == "render" and spec.sample_kind == "long":
        if spec.run_root.exists() or spec.run_root.is_symlink():
            uid, gid = target_identity()
            verify_private_run_root(spec, uid, gid)
            validate_existing_action_inputs(spec, uid, gid)
    elif spec.operation != "guard-only":
        uid, gid = target_identity()
        verify_private_run_root(spec, uid, gid)
        validate_existing_action_inputs(spec, uid, gid)


def run_public_preflight(spec):
    """Read-only/no-media checks; the in-unit guard repeats authoritative gates."""
    ensure_public_apply_preflight(spec)
    read_host_memory()
    fixed_runtime_python()
    for path in (SYSTEMD_RUN_PATH, SYSTEMCTL_PATH, NICE_PATH):
        require_regular_file(path, executable=True, allow_symlink=True)
    if spec.operation != "guard-only":
        try:
            parent = os.open(RUNS_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC |
                             getattr(os, "O_NOFOLLOW", 0))
            parent_stat = os.fstat(parent)
        except OSError:
            raise LaunchFailure("run_root_parent_unavailable") from None
        finally:
            try:
                os.close(parent)
            except (NameError, OSError):
                pass
        require(parent_stat.st_uid == 0 and stat.S_IMODE(parent_stat.st_mode) & 0o022 == 0,
                "run_root_parent_unsafe")
        lock_fd, _ = acquire_media_lock()
        os.close(lock_fd)
    value = preview(spec)
    value.update(preflight=True, preflight_passed=True,
                 apply=False, media_started=False, unit_submitted=False,
                 ffprobe_processes=0, ffmpeg_processes=0)
    return value


def parse_systemd_duration(value, *, maximum_seconds=90, exact_seconds=None):
    units = {"us": 0.000001, "ms": 0.001, "s": 1, "min": 60, "h": 3600}
    require(isinstance(value, str) and value and value != "infinity",
            "media_unit_contract_invalid")
    total, position = 0.0, 0
    for match in re.finditer(r"(?:^| )([0-9]+(?:\.[0-9]+)?)(us|ms|s|min|h)(?= |$)", value):
        require(match.start() == position, "media_unit_contract_invalid")
        total += float(match[1]) * units[match[2]]
        position = match.end()
    require(position == len(value) and 0 < total <= maximum_seconds and
            (exact_seconds is None or total == exact_seconds),
            "media_unit_contract_invalid")
    return total


def verify_media_unit_contract(spec):
    require_regular_file(SYSTEMCTL_PATH, executable=True, allow_symlink=True)
    properties = ("Id", "MainPID", "KillMode", "RemainAfterExit",
                  "TimeoutStopUSec", "RuntimeMaxUSec", "PrivateDevices",
                  "ReadOnlyPaths")
    command = [path_text(SYSTEMCTL_PATH), "show", spec.unit]
    for name in properties:
        command.append("--property=" + name)
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            timeout=10,
            check=False,
            text=True,
            encoding="utf-8",
            errors="strict",
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise LaunchFailure("media_unit_contract_unavailable") from None
    require(result.returncode == 0 and len(result.stdout) <= 4096,
            "media_unit_contract_unavailable")
    values = {}
    for line in result.stdout.splitlines():
        key, separator, raw = line.partition("=")
        require(separator and key in properties and key not in values,
                "media_unit_contract_invalid")
        values[key] = raw
    require(set(values) == set(properties) and values["Id"] == spec.unit and
            values["MainPID"] == str(os.getpid()) and
            values["KillMode"] == "control-group" and
            values["RemainAfterExit"] == "no" and values["PrivateDevices"] == "no",
            "media_unit_contract_invalid")
    require(values["ReadOnlyPaths"].split() == [path_text(path) for path in (
        spec.candidate_root, INPUT_ROOT, ASSET_ROOT, RUNTIME_ROOT
    )], "media_unit_contract_invalid")
    parse_systemd_duration(values["TimeoutStopUSec"])
    parse_systemd_duration(
        values["RuntimeMaxUSec"], maximum_seconds=RENDER_TIMEOUT_SECONDS,
        exact_seconds=RENDER_TIMEOUT_SECONDS,
    )
    return values


def require_submission_root():
    try:
        direct = os.lstat(SUBMISSIONS_ROOT)
        target = os.stat(SUBMISSIONS_ROOT)
    except OSError:
        raise LaunchFailure("submission_guard_directory_unavailable") from None
    require(not stat.S_ISLNK(direct.st_mode) and stat.S_ISDIR(direct.st_mode) and
            stat.S_ISDIR(target.st_mode) and
            (direct.st_dev, direct.st_ino) == (target.st_dev, target.st_ino) and
            direct.st_uid == target.st_uid == 0 and
            stat.S_IMODE(target.st_mode) & 0o022 == 0,
            "submission_guard_directory_unsafe")


def existing_submission_guard(spec):
    require_submission_root()
    for path in (spec.submission_guard_path, spec.submission_receipt_path):
        try:
            os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError:
            raise SubmissionUncertain("media_acceptance_submission_state_unknown") from None
        raise SubmissionUncertain("media_acceptance_submission_already_recorded")


def submission_record(spec, state):
    require(state in ("submitting", "accepted"), "invalid_submission_state")
    return {
        "version": 1,
        "state": state,
        "candidate_sha": spec.candidate_sha,
        "run_id": spec.run_id,
        "operation": spec.operation,
        "sample_kind": spec.sample_kind,
        "configuration": spec.config.name,
        "trial": spec.trial,
        "unit": spec.unit,
        "replay_forbidden": True,
    }


def write_submission_record(spec, state):
    path = (spec.submission_guard_path if state == "submitting"
            else spec.submission_receipt_path)
    expected = submission_record(spec, state)
    try:
        write_exclusive_json(path, expected, code="submission_guard_write_failed")
        actual = read_owned_json(path, 0, 0, "submission_guard_readback_failed", mode=0o400)
        require(actual == expected, "submission_guard_readback_failed")
    except LaunchFailure as exc:
        try:
            os.lstat(path)
        except FileNotFoundError:
            raise exc from None
        except OSError:
            raise SubmissionUncertain("media_acceptance_submission_state_unknown") from None
        raise SubmissionUncertain(str(exc)) from None
    return expected


def submit(spec):
    existing_submission_guard(spec)
    ensure_public_apply_preflight(spec)
    command = build_systemd_command(spec)
    write_submission_record(spec, "submitting")
    try:
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                timeout=30,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired):
            raise SubmissionUncertain("media_acceptance_submit_outcome_unknown") from None
        if result.returncode != 0 or not isinstance(result.stdout, str) or len(result.stdout) > 4096:
            raise SubmissionUncertain("media_acceptance_submit_outcome_unknown")
        write_submission_record(spec, "accepted")
    except SubmissionUncertain:
        raise
    except BaseException:
        raise SubmissionUncertain("media_acceptance_submit_outcome_unknown") from None
    return {"ok": True, "submitted": True, "completed": False, "unit": spec.unit,
            "candidate_sha": spec.candidate_sha, "run_id": spec.run_id,
            "operation": spec.operation, "sample_kind": spec.sample_kind,
            "configuration": spec.config.name, "trial": spec.trial,
            "media_started": None, "completion_unknown": False,
            "replay_forbidden": True, "media_result_available": False}


def verify_candidate_file_identity(path, expected):
    try:
        value = os.lstat(path)
    except OSError:
        raise LaunchFailure("candidate_critical_identity_changed") from None
    require(isinstance(expected, dict) and set(expected) == {
        "blob", "git_mode", "device", "inode", "size_bytes", "mtime_ns",
        "nlink", "uid", "mode_bits",
    } and SHA_PATTERN.fullmatch(str(expected["blob"])) and
            expected["git_mode"] in ("100644", "100755") and
            not stat.S_ISLNK(value.st_mode) and stat.S_ISREG(value.st_mode) and
            expected["device"] == value.st_dev and expected["inode"] == value.st_ino and
            expected["size_bytes"] == value.st_size and
            expected["mtime_ns"] == value.st_mtime_ns and
            expected["nlink"] == value.st_nlink and
            expected["uid"] == value.st_uid == 0 and
            stat.S_IMODE(value.st_mode) & 0o022 == 0 and
            bool(stat.S_IMODE(value.st_mode) & 0o111) ==
            (expected["git_mode"] == "100755") and
            expected["mode_bits"] == stat.S_IMODE(value.st_mode),
            "candidate_critical_identity_changed")


def verify_candidate_tree_identities(candidate_root, expected):
    require(isinstance(expected, dict) and
            1 <= len(expected) <= MAX_CANDIDATE_TRACKED_FILES,
            "candidate_tree_identity_invalid")
    total = 0
    for relative, identity in expected.items():
        require(isinstance(relative, str) and "\r" not in relative and "\n" not in relative,
                "candidate_tree_identity_invalid")
        pure = PurePosixPath(relative)
        require(not pure.is_absolute() and "\\" not in relative and
                ".." not in pure.parts and str(pure) == relative,
                "candidate_tree_identity_invalid")
        verify_candidate_file_identity(
            Path(candidate_root).joinpath(*pure.parts), identity
        )
        total += identity["size_bytes"]
        require(total <= MAX_CANDIDATE_TRACKED_BYTES,
                "candidate_tree_identity_invalid")


def load_candidate_module(name, path, *, expected_identity=None,
                          candidate_root=None, expected_tree=None):
    if expected_tree is not None:
        require(candidate_root is not None, "candidate_tree_identity_invalid")
        verify_candidate_tree_identities(candidate_root, expected_tree)
    if expected_identity is not None:
        verify_candidate_file_identity(path, expected_identity)
    try:
        module_spec = importlib.util.spec_from_file_location(name, path)
        require(module_spec is not None and module_spec.loader is not None,
                "candidate_module_load_failed")
        module = importlib.util.module_from_spec(module_spec)
        require(name not in sys.modules, "candidate_module_name_collision")
        sys.modules[name] = module
        try:
            module_spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
        if expected_identity is not None:
            verify_candidate_file_identity(path, expected_identity)
        if expected_tree is not None:
            verify_candidate_tree_identities(candidate_root, expected_tree)
        return module
    except LaunchFailure:
        raise
    except Exception:
        raise LaunchFailure("candidate_module_load_failed") from None


def ensure_python_stage(stage):
    flags = sys.flags
    require(flags.isolated == 1 and flags.ignore_environment == 1 and
            flags.dont_write_bytecode == 1, "python_isolation_flags_missing")
    require(stage in ("guard", "verified") and flags.no_site == 1,
            "python_stage_flags_invalid")
    require(Path(os.path.realpath(sys.executable)) == Path(os.path.realpath(PYTHON_PATH)),
            "unexpected_python_runtime")


def target_identity():
    try:
        import pwd
        user = pwd.getpwnam(TARGET_USER)
    except (ImportError, KeyError):
        raise LaunchFailure("target_identity_unavailable") from None
    require(user.pw_uid > 0 and user.pw_gid > 0, "target_identity_invalid")
    return user.pw_uid, user.pw_gid


def create_private_run_root(spec, uid, gid):
    require(not spec.run_root.exists() and not spec.run_root.is_symlink(),
            "run_root_must_be_new")
    name = spec.run_root.name
    require(name == spec.candidate_sha + "-" + spec.run_id, "run_root_invalid")
    try:
        parent = os.open(RUNS_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC |
                         getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise LaunchFailure("run_root_parent_unavailable") from None
    try:
        parent_stat = os.fstat(parent)
        require(parent_stat.st_uid == 0 and stat.S_IMODE(parent_stat.st_mode) & 0o022 == 0,
                "run_root_parent_unsafe")
        os.mkdir(name, 0o700, dir_fd=parent)
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC |
                             getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
        try:
            os.fchmod(descriptor, 0o700)
            os.fchown(descriptor, uid, gid)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(parent)
    except LaunchFailure:
        raise
    except Exception:
        raise LaunchFailure("private_run_root_create_failed") from None
    finally:
        os.close(parent)


def verify_private_run_root(spec, uid, gid):
    try:
        value = os.lstat(spec.run_root)
    except OSError:
        raise LaunchFailure("private_run_root_invalid") from None
    require(stat.S_ISDIR(value.st_mode) and not stat.S_ISLNK(value.st_mode) and
            stat.S_IMODE(value.st_mode) == 0o700 and value.st_uid == uid and value.st_gid == gid,
            "private_run_root_invalid")


def require_owned_regular(path, uid, gid, code, *, mode=None):
    try:
        direct = os.lstat(path)
        target = os.stat(path)
    except OSError:
        raise LaunchFailure(code) from None
    require(not stat.S_ISLNK(direct.st_mode) and stat.S_ISREG(direct.st_mode) and
            stat.S_ISREG(target.st_mode) and direct.st_nlink == target.st_nlink == 1 and
            (direct.st_dev, direct.st_ino) == (target.st_dev, target.st_ino) and
            direct.st_uid == target.st_uid == uid and direct.st_gid == target.st_gid == gid and
            (mode is None or stat.S_IMODE(target.st_mode) == mode), code)
    return target


def fingerprint_regular(path, uid, gid, code, *, mode=None):
    expected = require_owned_regular(path, uid, gid, code, mode=mode)
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC |
                             getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            require((opened.st_dev, opened.st_ino, opened.st_size) ==
                    (expected.st_dev, expected.st_ino, expected.st_size), code)
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            final = os.fstat(stream.fileno())
            require((final.st_dev, final.st_ino, final.st_size) ==
                    (expected.st_dev, expected.st_ino, expected.st_size), code)
    except LaunchFailure:
        raise
    except OSError:
        raise LaunchFailure(code) from None
    return {"sha256": digest.hexdigest(), "size_bytes": expected.st_size}


def owned_regular_identity(path, uid, gid, code, *, mode=None):
    value = require_owned_regular(path, uid, gid, code, mode=mode)
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "size_bytes": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "nlink": value.st_nlink,
    }


def fingerprint_fixed_input(path, code, *, expected_size=None):
    digest = hashlib.sha256()
    try:
        direct = os.lstat(path)
        target = os.stat(path)
        require(not stat.S_ISLNK(direct.st_mode) and stat.S_ISREG(direct.st_mode) and
                stat.S_ISREG(target.st_mode) and
                (direct.st_dev, direct.st_ino) == (target.st_dev, target.st_ino) and
                direct.st_nlink == target.st_nlink and target.st_nlink >= 1 and
                (expected_size is None or target.st_size == expected_size), code)
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC |
                             getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            identity = (target.st_dev, target.st_ino, target.st_size,
                        target.st_mtime_ns, target.st_nlink)
            require((opened.st_dev, opened.st_ino, opened.st_size,
                     opened.st_mtime_ns, opened.st_nlink) == identity, code)
            size = 0
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
            final = os.fstat(stream.fileno())
            require(size == target.st_size and
                    (final.st_dev, final.st_ino, final.st_size,
                     final.st_mtime_ns, final.st_nlink) == identity, code)
    except LaunchFailure:
        raise
    except (OSError, ValueError):
        raise LaunchFailure(code) from None
    return {
        "sha256": digest.hexdigest(), "size_bytes": target.st_size,
        "device": target.st_dev, "inode": target.st_ino,
        "mtime_ns": target.st_mtime_ns, "nlink": target.st_nlink,
    }


def verify_fixed_input_unchanged(path, expected, code, *, expected_size=None):
    current = fingerprint_fixed_input(path, code, expected_size=expected_size)
    require(current == expected, "fixed_long_source_changed")
    return current


def run_source_record(spec, fingerprint):
    return {
        "version": 1,
        "candidate_sha": spec.candidate_sha,
        "run_id": spec.run_id,
        "source": path_text(LONG_SOURCE),
        "source_sha256": fingerprint["sha256"],
        "source_size": fingerprint["size_bytes"],
        "source_device": fingerprint["device"],
        "source_inode": fingerprint["inode"],
        "source_mtime_ns": fingerprint["mtime_ns"],
        "source_nlink": fingerprint["nlink"],
    }


def validate_run_source_record(spec, value):
    expected_keys = {
        "version", "candidate_sha", "run_id", "source", "source_sha256",
        "source_size", "source_device", "source_inode", "source_mtime_ns",
        "source_nlink",
    }
    require(isinstance(value, dict) and set(value) == expected_keys and
            value["version"] == 1 and value["candidate_sha"] == spec.candidate_sha and
            value["run_id"] == spec.run_id and value["source"] == path_text(LONG_SOURCE) and
            re.fullmatch(r"[0-9a-f]{64}", str(value["source_sha256"])) is not None and
            value["source_size"] == LONG_SOURCE_SIZE and
            all(type(value[key]) is int and value[key] >= 0 for key in
                ("source_device", "source_inode", "source_mtime_ns")) and
            type(value["source_nlink"]) is int and value["source_nlink"] >= 1,
            "run_source_manifest_invalid")
    return value


def read_run_source_record(spec, uid, gid):
    return validate_run_source_record(spec, read_owned_json(
        spec.run_source_manifest_path, uid, gid, "run_source_manifest_invalid", mode=0o400
    ))


def ensure_run_source_frozen(spec, uid, gid):
    current = fingerprint_fixed_input(
        LONG_SOURCE, "fixed_long_source_fingerprint_failed", expected_size=LONG_SOURCE_SIZE
    )
    expected = run_source_record(spec, current)
    try:
        os.lstat(spec.run_source_manifest_path)
    except FileNotFoundError:
        write_exclusive_json(
            spec.run_source_manifest_path, expected, code="run_source_manifest_write_failed"
        )
    except OSError:
        raise LaunchFailure("run_source_manifest_invalid") from None
    frozen = read_run_source_record(spec, uid, gid)
    require(frozen == expected, "fixed_long_source_changed")
    return frozen


def require_prepared_source_binding(spec, prepared, uid, gid):
    frozen = read_run_source_record(spec, uid, gid)
    require(prepared["source_sha256"] == frozen["source_sha256"] and
            prepared["source_size"] == frozen["source_size"] and
            prepared["source_device"] == frozen["source_device"] and
            prepared["source_inode"] == frozen["source_inode"] and
            prepared["source_mtime_ns"] == frozen["source_mtime_ns"] and
            prepared["source_nlink"] == frozen["source_nlink"],
            "prepared_source_run_binding_mismatch")
    return frozen


def read_owned_json(path, uid, gid, code, *, mode=None):
    value = require_owned_regular(path, uid, gid, code, mode=mode)
    require(0 < value.st_size <= 131072, code)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC |
                             getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            require((opened.st_dev, opened.st_ino, opened.st_size) ==
                    (value.st_dev, value.st_ino, value.st_size), code)
            raw = stream.read(131073)
            final = os.fstat(stream.fileno())
            require((final.st_dev, final.st_ino, final.st_size) ==
                    (value.st_dev, value.st_ino, value.st_size), code)
        require(len(raw) <= 131072, code)
        result = json.loads(raw.decode("utf-8"))
    except LaunchFailure:
        raise
    except (OSError, UnicodeError, ValueError):
        raise LaunchFailure(code) from None
    require(isinstance(result, dict), code)
    return result


def validate_pressure_value(value):
    require(isinstance(value, dict) and set(value) == {
        "memory_failcnt", "memsw_failcnt", "swap_bytes", "oom_control"
    } and all(type(value[key]) is int and value[key] >= 0 for key in
              ("memory_failcnt", "memsw_failcnt", "swap_bytes")) and
            isinstance(value["oom_control"], dict) and
            set(value["oom_control"]) == {
                "oom_kill_disable", "under_oom", "oom_kill", "oom_kill_available"
            } and value["oom_control"]["oom_kill_disable"] == 0 and
            value["oom_control"]["oom_kill_available"] is True and
            type(value["oom_control"]["under_oom"]) is int and
            value["oom_control"]["under_oom"] == 0 and
            type(value["oom_control"]["oom_kill"]) is int and
            value["oom_control"]["oom_kill"] >= 0,
            "resource_completion_evidence_invalid")
    return value


def validate_action_completion(spec, uid, gid):
    completion = read_owned_json(
        spec.completion_evidence_path, uid, gid,
        "action_completion_evidence_invalid", mode=0o400,
    )
    resource = read_owned_json(
        spec.resource_evidence_path, uid, gid,
        "resource_completion_evidence_invalid", mode=0o400,
    )
    resource_fp = fingerprint_regular(
        spec.resource_evidence_path, uid, gid,
        "resource_completion_evidence_invalid", mode=0o400,
    )
    common = (
        completion.get("candidate_sha") == resource.get("candidate_sha") ==
        spec.candidate_sha and
        completion.get("run_id") == resource.get("run_id") == spec.run_id and
        completion.get("operation") == resource.get("operation") == spec.operation and
        completion.get("sample_kind") == resource.get("sample_kind") == spec.sample_kind and
        completion.get("configuration") == resource.get("configuration") == spec.config.name and
        completion.get("trial") == resource.get("trial") == spec.trial and
        completion.get("unit") == resource.get("unit") == spec.unit
    )
    require(set(resource) == {
        "version", "ok", "candidate_sha", "run_id", "operation", "sample_kind",
        "configuration", "trial", "unit", "resources_sha256", "before", "after",
        "failcnt_unchanged", "oom_unchanged", "under_oom_clear", "swap_zero",
        "operation_succeeded", "long_source_before", "long_source_after",
        "minimum_mem_available_bytes", "host_memory_stop_threshold_bytes",
        "host_memory_sampling_interval_seconds",
        "cos_uploads", "production_requests",
    } and resource["version"] == 1 and resource["ok"] is True and common and
            re.fullmatch(r"[0-9a-f]{64}", str(resource["resources_sha256"])) is not None and
            resource["failcnt_unchanged"] is True and resource["oom_unchanged"] is True and
            resource["under_oom_clear"] is True and resource["swap_zero"] is True and
            resource["operation_succeeded"] is True and resource["cos_uploads"] == 0 and
            type(resource["minimum_mem_available_bytes"]) is int and
            resource["minimum_mem_available_bytes"] >= MIN_RUNNING_MEM_AVAILABLE_BYTES and
            resource["host_memory_stop_threshold_bytes"] ==
            MIN_RUNNING_MEM_AVAILABLE_BYTES and
            resource["host_memory_sampling_interval_seconds"] == 1 and
            resource["production_requests"] == 0,
            "resource_completion_evidence_invalid")
    for source in (resource["long_source_before"], resource["long_source_after"]):
        validate_run_source_record(spec, source)
    require(resource["long_source_before"] == resource["long_source_after"],
            "resource_completion_evidence_invalid")
    before, after = validate_pressure_value(resource["before"]), validate_pressure_value(resource["after"])
    require(before["memory_failcnt"] == after["memory_failcnt"] and
            before["memsw_failcnt"] == after["memsw_failcnt"] and
            before["oom_control"]["oom_kill"] == after["oom_control"]["oom_kill"] and
            before["swap_bytes"] == after["swap_bytes"] == 0,
            "resource_completion_evidence_invalid")
    require(set(completion) == {
        "version", "ok", "candidate_sha", "run_id", "operation", "sample_kind",
        "configuration", "trial", "unit", "resource_evidence",
        "resource_evidence_sha256", "resource_evidence_size", "cos_uploads",
        "production_requests",
    } and completion["version"] == 1 and completion["ok"] is True and common and
            completion["resource_evidence"] == path_text(spec.resource_evidence_path) and
            completion["resource_evidence_sha256"] == resource_fp["sha256"] and
            completion["resource_evidence_size"] == resource_fp["size_bytes"] and
            completion["cos_uploads"] == 0 and completion["production_requests"] == 0,
            "action_completion_evidence_invalid")
    return {"completion": completion, "resource": resource}


def validate_prepared_short(spec, uid, gid):
    evidence = read_owned_json(
        spec.prepare_evidence_path, uid, gid, "prepared_short_evidence_invalid", mode=0o400
    )
    expected_keys = {
        "version", "ok", "operation", "candidate_sha", "run_id", "sample_kind",
        "configuration", "unit", "source", "source_size", "prepared_path",
        "prepared_sha256", "prepared_size", "duration_seconds", "cos_uploads",
        "production_requests", "source_sha256", "source_device", "source_inode",
        "source_mtime_ns", "source_nlink", "source_fingerprint_elapsed_seconds",
        "minimum_mem_available_bytes", "host_memory_stop_threshold_bytes",
        "host_memory_sampling_interval_seconds",
    }
    prepare_unit = build_spec(
        spec.candidate_sha, spec.run_id, "short", "2c2t", "prepare-short"
    ).unit
    require(set(evidence) == expected_keys and evidence["version"] == 1 and
            evidence["ok"] is True and evidence["operation"] == "prepare-short" and
            evidence["candidate_sha"] == spec.candidate_sha and
            evidence["run_id"] == spec.run_id and evidence["sample_kind"] == "short" and
            evidence["configuration"] == "2c2t" and evidence["unit"] == prepare_unit and
            evidence["source"] == path_text(LONG_SOURCE) and
            evidence["source_size"] == LONG_SOURCE_SIZE and
            re.fullmatch(r"[0-9a-f]{64}", str(evidence["source_sha256"])) is not None and
            all(type(evidence[key]) is int and evidence[key] >= 0 for key in
                ("source_device", "source_inode", "source_mtime_ns")) and
            type(evidence["source_nlink"]) is int and evidence["source_nlink"] >= 1 and
            type(evidence["source_fingerprint_elapsed_seconds"]) in (int, float) and
            math.isfinite(evidence["source_fingerprint_elapsed_seconds"]) and
            evidence["source_fingerprint_elapsed_seconds"] >= 0 and
            type(evidence["minimum_mem_available_bytes"]) is int and
            evidence["minimum_mem_available_bytes"] >= MIN_RUNNING_MEM_AVAILABLE_BYTES and
            evidence["host_memory_stop_threshold_bytes"] ==
            MIN_RUNNING_MEM_AVAILABLE_BYTES and
            evidence["host_memory_sampling_interval_seconds"] == 1 and
            evidence["prepared_path"] == path_text(spec.prepared_short_path) and
            re.fullmatch(r"[0-9a-f]{64}", str(evidence["prepared_sha256"])) is not None and
            type(evidence["prepared_size"]) is int and evidence["prepared_size"] > 0 and
            type(evidence["duration_seconds"]) in (int, float) and
            math.isfinite(evidence["duration_seconds"]) and
            115 <= evidence["duration_seconds"] <= 125 and
            evidence["cos_uploads"] == 0 and evidence["production_requests"] == 0,
            "prepared_short_evidence_invalid")
    actual = fingerprint_regular(
        spec.prepared_short_path, uid, gid, "prepared_short_invalid", mode=0o400
    )
    require(actual == {"sha256": evidence["prepared_sha256"],
                       "size_bytes": evidence["prepared_size"]},
            "prepared_short_sha256_mismatch")
    return evidence


def validate_render_result(spec, uid, gid):
    try:
        output = os.lstat(spec.output_dir)
    except OSError:
        raise LaunchFailure("render_result_invalid") from None
    require(stat.S_ISDIR(output.st_mode) and not stat.S_ISLNK(output.st_mode) and
            stat.S_IMODE(output.st_mode) == 0o700 and output.st_uid == uid and
            output.st_gid == gid, "render_result_invalid")
    render_spec = build_spec(
        spec.candidate_sha, spec.run_id, spec.sample_kind, spec.config.name,
        "render", spec.trial
    )
    validate_action_completion(render_spec, uid, gid)
    launcher = read_owned_json(
        render_spec.launcher_result_path, uid, gid,
        "render_launcher_evidence_invalid", mode=0o400,
    )
    evidence_path = spec.output_dir / "evidence.json"
    evidence = read_owned_json(evidence_path, uid, gid, "render_benchmark_evidence_invalid")
    result_path = spec.output_dir / "result.mp4"
    artifact_identity_before = owned_regular_identity(
        result_path, uid, gid, "render_result_invalid"
    )
    artifact = fingerprint_regular(result_path, uid, gid, "render_result_invalid")
    artifact_identity = owned_regular_identity(
        result_path, uid, gid, "render_result_invalid"
    )
    require(artifact_identity == artifact_identity_before and
            artifact_identity["size_bytes"] == artifact["size_bytes"],
            "render_result_invalid")
    evidence_fp = fingerprint_regular(
        evidence_path, uid, gid, "render_benchmark_evidence_invalid"
    )
    expected_launcher = {
        "version", "ok", "operation", "candidate_sha", "run_id", "sample_kind",
        "configuration", "trial", "unit", "benchmark_evidence", "benchmark_evidence_sha256",
        "output_sha256", "output_size", "source_sha256", "source_size",
        "minimum_mem_available_bytes", "host_memory_stop_threshold_bytes",
        "host_memory_sampling_interval_seconds",
        "cos_uploads", "production_requests",
    }
    render_unit = render_spec.unit
    require(set(launcher) == expected_launcher and launcher["version"] == 1 and
            launcher["ok"] is True and launcher["operation"] == "render" and
            launcher["candidate_sha"] == spec.candidate_sha and
            launcher["run_id"] == spec.run_id and
            launcher["sample_kind"] == spec.sample_kind and
            launcher["configuration"] == spec.config.name and
            launcher["trial"] == spec.trial and
            launcher["unit"] == render_unit and
            launcher["benchmark_evidence"] == path_text(evidence_path) and
            launcher["benchmark_evidence_sha256"] == evidence_fp["sha256"] and
            launcher["output_sha256"] == artifact["sha256"] and
            launcher["output_size"] == artifact["size_bytes"] and
            type(launcher["minimum_mem_available_bytes"]) is int and
            launcher["minimum_mem_available_bytes"] >= MIN_RUNNING_MEM_AVAILABLE_BYTES and
            launcher["host_memory_stop_threshold_bytes"] ==
            MIN_RUNNING_MEM_AVAILABLE_BYTES and
            launcher["host_memory_sampling_interval_seconds"] == 1 and
            re.fullmatch(r"[0-9a-f]{64}", str(launcher["source_sha256"])) is not None and
            type(launcher["source_size"]) is int and launcher["source_size"] > 0 and
            launcher["cos_uploads"] == 0 and launcher["production_requests"] == 0,
            "render_launcher_evidence_invalid")
    if spec.sample_kind == "short":
        prepared = validate_prepared_short(spec, uid, gid)
        require(launcher["source_sha256"] == prepared["prepared_sha256"] and
                launcher["source_size"] == prepared["prepared_size"],
                "render_source_fingerprint_mismatch")
    result = evidence.get("result")
    require(evidence.get("version") == 1 and evidence.get("kind") == "render" and
            evidence.get("ok") is True and
            evidence.get("sample_kind") == spec.sample_kind and
            evidence.get("filter_threads") == spec.config.filter_threads and
            evidence.get("recipe_sha256") == RECIPE_SHA256 and
            evidence.get("asset_manifest_sha256") == ASSET_MANIFEST_SHA256 and
            evidence.get("acceptance_launcher_lock_inherited") is True and
            evidence.get("minimum_mem_available_bytes") ==
            launcher["minimum_mem_available_bytes"] and
            evidence.get("source") == {
                "sha256": launcher["source_sha256"],
                "size_bytes": launcher["source_size"],
            } and evidence.get("source_final") == evidence.get("source") and
            evidence.get("source_unchanged") is True and
            evidence.get("source_identity") == evidence.get("source_final_identity") and
            evidence.get("cos_uploads") == 0 and evidence.get("production_requests") == 0 and
            isinstance(result, dict) and result.get("output_sha256") == artifact["sha256"] and
            result.get("output_size") == artifact["size_bytes"],
            "render_benchmark_evidence_invalid")
    return {"artifact": artifact, "artifact_identity": artifact_identity,
            "evidence": evidence,
            "benchmark_evidence_sha256": evidence_fp["sha256"]}


def validate_existing_action_inputs(spec, uid, gid):
    require(not spec.resource_evidence_path.exists() and
            not spec.resource_evidence_path.is_symlink() and
            not spec.completion_evidence_path.exists() and
            not spec.completion_evidence_path.is_symlink(),
            "action_evidence_must_be_new")
    if spec.operation == "render":
        require(not spec.output_dir.exists() and not spec.output_dir.is_symlink() and
                not spec.launcher_guard_path.exists() and
                not spec.launcher_result_path.exists(),
                "render_output_must_be_new")
        if spec.sample_kind == "short":
            validate_prepared_short(spec, uid, gid)
            prepare_spec = build_spec(
                spec.candidate_sha, spec.run_id, "short", "2c2t", "prepare-short", "r1"
            )
            validate_action_completion(prepare_spec, uid, gid)
    elif spec.operation == "decode":
        validate_render_result(spec, uid, gid)
        require(not spec.decode_evidence_path.exists() and
                not spec.decode_evidence_path.is_symlink() and
                not spec.launcher_guard_path.exists() and
                not spec.launcher_result_path.exists(),
                "decode_evidence_must_be_new")
    elif spec.operation == "prepare-short":
        require(not spec.prepared_short_path.exists() and
                not spec.prepare_evidence_path.exists() and
                not (spec.run_root / PREPARED_SHORT_PART_NAME).exists(),
                "prepared_short_output_must_be_new")


def acquire_media_lock():
    try:
        import fcntl
        parent = os.open(CONTROL_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC |
                         getattr(os, "O_NOFOLLOW", 0))
        try:
            control = os.fstat(parent)
            require(stat.S_ISDIR(control.st_mode) and control.st_uid == 0 and
                    stat.S_IMODE(control.st_mode) & 0o022 == 0,
                    "media_lock_control_directory_unsafe")
            descriptor = os.open(
                LOCK_PATH.name,
                os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            direct = os.stat(LOCK_PATH.name, dir_fd=parent, follow_symlinks=False)
        finally:
            os.close(parent)
        opened = os.fstat(descriptor)
        require(stat.S_ISREG(opened.st_mode) and stat.S_ISREG(direct.st_mode) and
                opened.st_nlink == direct.st_nlink == 1 and
                (opened.st_dev, opened.st_ino) == (direct.st_dev, direct.st_ino),
                "media_lock_invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LaunchFailure("media_acceptance_already_running") from None
        return descriptor, (opened.st_dev, opened.st_ino)
    except LaunchFailure:
        try:
            os.close(descriptor)
        except (NameError, OSError):
            pass
        raise
    except (ImportError, OSError):
        try:
            os.close(descriptor)
        except (NameError, OSError):
            pass
        raise LaunchFailure("media_lock_unavailable") from None


def verify_inherited_media_lock(lock_fd, expected_identity=None):
    require(type(lock_fd) is int and lock_fd >= 3, "media_lock_invalid")
    try:
        import fcntl
        descriptor = os.fstat(lock_fd)
        control = os.stat(CONTROL_ROOT, follow_symlinks=False)
        direct = os.stat(LOCK_PATH, follow_symlinks=False)
        target = os.stat(LOCK_PATH)
        link = os.readlink("/proc/self/fd/%d" % lock_fd)
        identity = (descriptor.st_dev, descriptor.st_ino)
        require(stat.S_ISDIR(control.st_mode) and control.st_uid == 0 and
                stat.S_IMODE(control.st_mode) & 0o022 == 0 and
                os.get_inheritable(lock_fd) and stat.S_ISREG(descriptor.st_mode) and
                stat.S_ISREG(direct.st_mode) and stat.S_ISREG(target.st_mode) and
                descriptor.st_nlink == direct.st_nlink == target.st_nlink == 1 and
                identity == (direct.st_dev, direct.st_ino) == (target.st_dev, target.st_ino) and
                link == path_text(LOCK_PATH) and
                (expected_identity is None or identity == expected_identity),
                "media_lock_invalid")
        independent = os.open(LOCK_PATH, os.O_RDWR | os.O_CLOEXEC |
                              getattr(os, "O_NOFOLLOW", 0))
        try:
            try:
                fcntl.flock(independent, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(independent, fcntl.LOCK_UN)
                raise LaunchFailure("media_lock_not_held")
        finally:
            os.close(independent)
        return identity
    except LaunchFailure:
        raise
    except (ImportError, OSError, OverflowError, ValueError):
        raise LaunchFailure("media_lock_invalid") from None


def write_exclusive_json(path, value, code="launcher_evidence_write_failed"):
    encoded = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
    require(len(encoded) <= 131072, "launcher_evidence_too_large")
    parent = None
    try:
        path = Path(path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC |
                         getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), 0o600,
                             dir_fd=parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fchmod(stream.fileno(), 0o400)
            os.fsync(stream.fileno())
        os.fsync(parent)
    except OSError:
        raise LaunchFailure(code) from None
    finally:
        if parent is not None:
            try:
                os.close(parent)
            except OSError:
                pass


def operation_argument(spec):
    return {
        "render": None,
        "prepare-short": "--prepare-short",
        "decode": "--decode",
        "guard-only": "--guard-only",
    }[spec.operation]


def exec_verified_stage(spec, proof, lock_fd=None):
    content = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
    require(len(content) <= 1024, "invalid_guard_proof")
    read_fd, write_fd = os.pipe()
    try:
        require(os.write(write_fd, content) == len(content), "guard_proof_write_failed")
        os.close(write_fd)
        write_fd = None
        os.set_inheritable(read_fd, True)
        if lock_fd is not None:
            os.set_inheritable(lock_fd, True)
        executable = Path(os.path.realpath(sys.executable))
        arguments = [
            path_text(executable), "-I", "-S", "-B", path_text(spec.script_path),
            "--candidate-sha", spec.candidate_sha,
            "--run-id", spec.run_id,
            "--sample-kind", spec.sample_kind,
            "--config", spec.config.name,
            "--trial", spec.trial,
        ]
        action = operation_argument(spec)
        if action is not None:
            arguments.append(action)
        arguments.extend([
            "--internal-stage", "verified", "--guard-proof-fd", str(read_fd),
        ])
        if lock_fd is not None:
            arguments.extend(["--lock-fd", str(lock_fd)])
        os.execve(executable, arguments, clean_environment())
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def internal_guard_stage(spec):
    require_linux()
    ensure_python_stage("guard")
    require(os.geteuid() == 0, "root_required_for_cgroup_configuration")
    verify_media_unit_contract(spec)
    verify_candidate(spec)
    validate_fixed_inputs(spec)
    read_host_memory()
    uid, gid = target_identity()
    candidate = verify_candidate(spec)
    guard = load_candidate_module(
        "_drama_media_resource_guard", spec.candidate_root /
        "scripts/check_drama_media_resource_guard.py",
        expected_identity=candidate["critical"]["scripts/check_drama_media_resource_guard.py"],
        candidate_root=spec.candidate_root, expected_tree=candidate["tracked"],
    )
    if spec.operation == "guard-only":
        try:
            return guard.run_guard(
                spec.unit,
                spec.config.cpu_cores,
                profile=guard.MEDIA_16_GIB_PROFILE,
                launch_probe=lambda proof: exec_verified_stage(spec, proof),
            )
        except guard.GuardFailure as exc:
            raise LaunchFailure(str(exc)) from None
        except (OSError, KeyError, ValueError, IndexError, TypeError):
            raise LaunchFailure("media_cgroup_guard_failed") from None
    lock_fd, lock_identity = acquire_media_lock()
    try:
        if spec.operation == "prepare-short" or (
                spec.operation == "render" and spec.sample_kind == "long" and
                not spec.run_root.exists() and not spec.run_root.is_symlink()):
            create_private_run_root(spec, uid, gid)
        verify_inherited_media_lock(lock_fd, lock_identity)
        verify_private_run_root(spec, uid, gid)
        validate_existing_action_inputs(spec, uid, gid)
        try:
            return guard.run_guard(
                spec.unit,
                spec.config.cpu_cores,
                profile=guard.MEDIA_16_GIB_PROFILE,
                launch_probe=lambda proof: exec_verified_stage(spec, proof, lock_fd),
            )
        except guard.GuardFailure as exc:
            raise LaunchFailure(str(exc)) from None
        except (OSError, KeyError, ValueError, IndexError, TypeError):
            raise LaunchFailure("media_cgroup_guard_failed") from None
    finally:
        os.close(lock_fd)


def benchmark_arguments(spec):
    return argparse.Namespace(
        source=path_text(spec.source_path),
        recipe=path_text(RECIPE_PATH),
        asset_root=path_text(ASSET_ROOT),
        asset_manifest_sha256=ASSET_MANIFEST_SHA256,
        output_dir=path_text(spec.output_dir),
        sample_kind=spec.sample_kind,
        filter_threads=spec.config.filter_threads,
        ffmpeg=path_text(FFMPEG_PATH),
        ffprobe=path_text(FFPROBE_PATH),
        timeout=RENDER_TIMEOUT_SECONDS,
    )


def benchmark_guard_failure(exc, fallback):
    code = getattr(exc, "code", "")
    allowed = {
        "benchmark_media_lock_invalid", "benchmark_media_lock_not_held",
        "benchmark_media_lock_inheritance_failed", "benchmark_renderer_cleanup_failed",
        "benchmark_launcher_start_memory_low",
    }
    raise LaunchFailure(code if code in allowed else fallback) from None


def cleanup_owned_child(proc, cleanup_code):
    cleanup_failed = False
    try:
        if proc.poll() is None:
            proc.kill()
    except BaseException:
        cleanup_failed = True
    try:
        proc.wait(timeout=CHILD_REAP_TIMEOUT_SECONDS)
    except BaseException:
        cleanup_failed = True
    try:
        if proc.poll() is None:
            cleanup_failed = True
    except BaseException:
        cleanup_failed = True
    if cleanup_failed:
        raise LaunchFailure(cleanup_code) from None


def attach_memory_observation(error, minimum_mem_available_bytes):
    try:
        error.minimum_mem_available_bytes = minimum_mem_available_bytes
        error.host_memory_stop_threshold_bytes = MIN_RUNNING_MEM_AVAILABLE_BYTES
        error.host_memory_sampling_interval_seconds = 1
    except Exception:
        pass
    return error


def run_fixed_child(benchmark, command, lock_fd, *, timeout, failure_code,
                    timeout_code, cleanup_code, capture_stdout=False):
    initial_memory = read_host_memory()
    minimum_mem_available = initial_memory["MemAvailable"]
    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                proc = benchmark.launch_renderer_process(
                    command,
                    inherited_lock_fd=lock_fd,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file if capture_stdout else subprocess.DEVNULL,
                    stderr=stderr_file,
                    close_fds=True,
                )
            except Exception as exc:
                benchmark_guard_failure(exc, failure_code)
            started = time.monotonic()
            deadline = started + timeout

            def cleanup_with_observation():
                try:
                    cleanup_owned_child(proc, cleanup_code)
                except BaseException as exc:
                    attach_memory_observation(exc, minimum_mem_available)
                    raise

            def observe_or_cleanup():
                nonlocal minimum_mem_available
                try:
                    sample = read_host_memory(minimum_bytes=0)
                    minimum_mem_available = min(
                        minimum_mem_available, sample["MemAvailable"]
                    )
                    if sample["MemAvailable"] < MIN_RUNNING_MEM_AVAILABLE_BYTES:
                        raise LaunchFailure("host_memory_below_media_stop_gate")
                except BaseException as exc:
                    attach_memory_observation(exc, minimum_mem_available)
                    cleanup_with_observation()
                    raise

            observe_or_cleanup()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = attach_memory_observation(
                        LaunchFailure(timeout_code), minimum_mem_available
                    )
                    cleanup_with_observation()
                    raise failure
                try:
                    returncode = proc.wait(timeout=min(1.0, remaining))
                except subprocess.TimeoutExpired:
                    observe_or_cleanup()
                    continue
                except BaseException as exc:
                    attach_memory_observation(exc, minimum_mem_available)
                    cleanup_with_observation()
                    raise
                observe_or_cleanup()
                break
            try:
                reaped = proc.poll()
            except Exception:
                raise attach_memory_observation(
                    LaunchFailure(cleanup_code), minimum_mem_available
                ) from None
            if reaped is None:
                raise attach_memory_observation(
                    LaunchFailure(cleanup_code), minimum_mem_available
                )
            if returncode != 0 or reaped != 0:
                raise attach_memory_observation(
                    LaunchFailure(failure_code), minimum_mem_available
                )
            output = b""
            if capture_stdout:
                stdout_file.seek(0, os.SEEK_END)
                require(stdout_file.tell() <= 131072, failure_code)
                stdout_file.seek(0)
                output = stdout_file.read(131073)
                require(len(output) <= 131072, failure_code)
            return {"stdout": output, "elapsed_seconds": round(time.monotonic() - started, 3),
                    "exit_code": returncode,
                    "minimum_mem_available_bytes": minimum_mem_available,
                    "host_memory_stop_threshold_bytes": MIN_RUNNING_MEM_AVAILABLE_BYTES,
                    "host_memory_sampling_interval_seconds": 1}
    except LaunchFailure:
        raise
    except OSError:
        raise LaunchFailure(failure_code) from None


def parse_prepared_probe(raw):
    try:
        value = json.loads(raw.decode("utf-8"))
        streams = value.get("streams")
        duration = float(value.get("format", {}).get("duration"))
    except (AttributeError, TypeError, ValueError, UnicodeError):
        raise LaunchFailure("prepare_short_probe_invalid") from None
    require(isinstance(value, dict) and isinstance(streams, list) and
            all(isinstance(item, dict) for item in streams) and
            {item.get("codec_type") for item in streams} >= {"video", "audio"} and
            math.isfinite(duration) and 115 <= duration <= 125,
            "prepare_short_probe_invalid")
    return duration


def prepare_short_command(spec):
    require(spec.operation == "prepare-short", "invalid_media_operation")
    return [
        path_text(FFMPEG_PATH), "-nostdin", "-hide_banner", "-loglevel", "error",
        "-xerror", "-y", "-i", path_text(LONG_SOURCE), "-t",
        str(PREPARE_SHORT_SECONDS), "-map", "0:v:0", "-map", "0:a:0",
        "-c", "copy", "-f", "mp4",
        path_text(spec.run_root / PREPARED_SHORT_PART_NAME),
    ]


def prepare_short_probe_command(spec):
    require(spec.operation == "prepare-short", "invalid_media_operation")
    return [
        path_text(FFPROBE_PATH), "-v", "error", "-show_entries",
        "format=duration:stream=codec_type", "-of", "json",
        path_text(spec.run_root / PREPARED_SHORT_PART_NAME),
    ]


def decode_command(spec):
    require(spec.operation == "decode", "invalid_media_operation")
    return [
        path_text(FFMPEG_PATH), "-nostdin", "-hide_banner", "-loglevel", "error",
        "-xerror", "-err_detect", "explode", "-hwaccel", "none", "-threads", "2",
        "-i", path_text(spec.output_dir / "result.mp4"), "-map", "0:v:0", "-map",
        "0:a:0", "-sn", "-dn", "-threads", "2", "-f", "null", "-",
    ]


def run_prepare_short(spec, uid, gid, lock_fd, benchmark):
    validate_existing_action_inputs(spec, uid, gid)
    root_fd = None
    part_fd = None
    source_fingerprint_started = time.monotonic()
    source_before = fingerprint_fixed_input(
        LONG_SOURCE, "fixed_long_source_fingerprint_failed",
        expected_size=LONG_SOURCE_SIZE,
    )
    source_fingerprint_elapsed = time.monotonic() - source_fingerprint_started
    try:
        root_fd = os.open(spec.run_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC |
                          getattr(os, "O_NOFOLLOW", 0))
        part_fd = os.open(
            PREPARED_SHORT_PART_NAME,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC |
            getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_fd,
        )
        initial = os.fstat(part_fd)
        require(stat.S_ISREG(initial.st_mode) and initial.st_nlink == 1 and
                initial.st_uid == uid and initial.st_gid == gid,
                "prepare_short_part_create_failed")
        os.close(part_fd)
        part_fd = None
        command = prepare_short_command(spec)
        prepare_outcome = run_fixed_child(
            benchmark, command, lock_fd, timeout=RENDER_TIMEOUT_SECONDS,
            failure_code="prepare_short_failed", timeout_code="prepare_short_timeout",
            cleanup_code="prepare_short_cleanup_failed",
        )
        after = require_owned_regular(
            spec.run_root / PREPARED_SHORT_PART_NAME, uid, gid,
            "prepare_short_part_invalid"
        )
        require((after.st_dev, after.st_ino) == (initial.st_dev, initial.st_ino) and
                after.st_size > 0, "prepare_short_part_invalid")
        probe_command = prepare_short_probe_command(spec)
        probe = run_fixed_child(
            benchmark, probe_command, lock_fd, timeout=300,
            failure_code="prepare_short_probe_failed",
            timeout_code="prepare_short_probe_timeout",
            cleanup_code="prepare_short_probe_cleanup_failed", capture_stdout=True,
        )
        duration = parse_prepared_probe(probe["stdout"])
        minimum_mem_available = min(
            prepare_outcome["minimum_mem_available_bytes"],
            probe["minimum_mem_available_bytes"],
        )
        source_fingerprint_started = time.monotonic()
        source_after = verify_fixed_input_unchanged(
            LONG_SOURCE, source_before, "fixed_long_source_fingerprint_failed",
            expected_size=LONG_SOURCE_SIZE,
        )
        source_fingerprint_elapsed += time.monotonic() - source_fingerprint_started
        source_fingerprint_elapsed = round(source_fingerprint_elapsed, 3)
        part_fd = os.open(
            PREPARED_SHORT_PART_NAME,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        opened = os.fstat(part_fd)
        require((opened.st_dev, opened.st_ino, opened.st_size) ==
                (initial.st_dev, initial.st_ino, after.st_size),
                "prepare_short_part_invalid")
        os.fchmod(part_fd, 0o400)
        os.fsync(part_fd)
        os.close(part_fd)
        part_fd = None
        fingerprint = fingerprint_regular(
            spec.run_root / PREPARED_SHORT_PART_NAME, uid, gid,
            "prepare_short_part_invalid", mode=0o400,
        )
        os.link(
            PREPARED_SHORT_PART_NAME, PREPARED_SHORT_NAME,
            src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False,
        )
        os.fsync(root_fd)
        os.unlink(PREPARED_SHORT_PART_NAME, dir_fd=root_fd)
        os.fsync(root_fd)
    except LaunchFailure:
        raise
    except FileExistsError:
        raise LaunchFailure("prepared_short_output_must_be_new") from None
    except OSError:
        raise LaunchFailure("prepare_short_commit_failed") from None
    finally:
        if part_fd is not None:
            try:
                os.close(part_fd)
            except OSError:
                pass
        if root_fd is not None:
            try:
                os.close(root_fd)
            except OSError:
                pass
    write_exclusive_json(spec.prepare_evidence_path, {
        "version": 1,
        "ok": True,
        "operation": "prepare-short",
        "candidate_sha": spec.candidate_sha,
        "run_id": spec.run_id,
        "sample_kind": "short",
        "configuration": "2c2t",
        "unit": spec.unit,
        "source": path_text(LONG_SOURCE),
        "source_size": LONG_SOURCE_SIZE,
        "source_sha256": source_before["sha256"],
        "source_device": source_before["device"],
        "source_inode": source_before["inode"],
        "source_mtime_ns": source_before["mtime_ns"],
        "source_nlink": source_before["nlink"],
        "source_fingerprint_elapsed_seconds": source_fingerprint_elapsed,
        "minimum_mem_available_bytes": minimum_mem_available,
        "host_memory_stop_threshold_bytes": MIN_RUNNING_MEM_AVAILABLE_BYTES,
        "host_memory_sampling_interval_seconds": 1,
        "prepared_path": path_text(spec.prepared_short_path),
        "prepared_sha256": fingerprint["sha256"],
        "prepared_size": fingerprint["size_bytes"],
        "duration_seconds": duration,
        "cos_uploads": 0,
        "production_requests": 0,
    }, code="prepare_short_evidence_write_failed")
    validate_prepared_short(spec, uid, gid)
    return {"ok": True, "operation": spec.operation, "unit": spec.unit,
            "completed": True, "media_started": True,
            "prepared_sha256": fingerprint["sha256"],
            "minimum_mem_available_bytes": minimum_mem_available,
            "host_memory_stop_threshold_bytes": MIN_RUNNING_MEM_AVAILABLE_BYTES,
            "host_memory_sampling_interval_seconds": 1}


def run_render(spec, uid, gid, lock_fd, benchmark):
    prepared = None
    if spec.sample_kind == "short":
        prepared = validate_prepared_short(spec, uid, gid)
    require(not spec.output_dir.exists() and not spec.output_dir.is_symlink() and
            not spec.launcher_result_path.exists(), "render_output_must_be_new")
    try:
        result = benchmark.benchmark_render(
            benchmark_arguments(spec), inherited_lock_fd=lock_fd
        )
    except Exception as exc:
        benchmark_guard_failure(exc, "media_benchmark_preflight_failed")
    require(isinstance(result, dict), "media_benchmark_result_invalid")
    minimum_mem_available = result.get("minimum_mem_available_bytes")
    require(type(minimum_mem_available) is int and
            minimum_mem_available >= MIN_RUNNING_MEM_AVAILABLE_BYTES,
            "media_benchmark_memory_evidence_invalid")
    source = result.get("source")
    source_final = result.get("source_final")
    require(isinstance(source, dict) and
            set(source) == {"sha256", "size_bytes"} and
            source_final == source and result.get("source_unchanged") is True and
            result.get("source_identity") == result.get("source_final_identity"),
            "render_source_fingerprint_mismatch")
    if prepared is not None:
        require(source == {"sha256": prepared["prepared_sha256"],
                           "size_bytes": prepared["prepared_size"]},
                "render_source_fingerprint_mismatch")
    else:
        frozen_source = read_run_source_record(spec, uid, gid)
        require(source == {"sha256": frozen_source["source_sha256"],
                           "size_bytes": frozen_source["source_size"]},
                "render_source_run_binding_mismatch")
    evidence_path = spec.output_dir / "evidence.json"
    evidence_fp = fingerprint_regular(
        evidence_path, uid, gid, "render_benchmark_evidence_invalid"
    )
    detail = result.get("result") if result.get("ok") is True else None
    output_sha = detail.get("output_sha256") if isinstance(detail, dict) else None
    output_size = detail.get("output_size") if isinstance(detail, dict) else None
    if result.get("ok") is True:
        require(re.fullmatch(r"[0-9a-f]{64}", str(output_sha)) is not None and
                type(output_size) is int and output_size > 0,
                "media_benchmark_result_invalid")
        artifact = fingerprint_regular(
            spec.output_dir / "result.mp4", uid, gid, "render_result_invalid"
        )
        require(artifact == {"sha256": output_sha, "size_bytes": output_size},
                "render_result_invalid")
    write_exclusive_json(spec.launcher_result_path, {
        "version": 1,
        "ok": result.get("ok") is True,
        "operation": "render",
        "candidate_sha": spec.candidate_sha,
        "run_id": spec.run_id,
        "sample_kind": spec.sample_kind,
        "configuration": spec.config.name,
        "trial": spec.trial,
        "unit": spec.unit,
        "benchmark_evidence": path_text(evidence_path),
        "benchmark_evidence_sha256": evidence_fp["sha256"],
        "output_sha256": output_sha,
        "output_size": output_size,
        "source_sha256": source["sha256"],
        "source_size": source["size_bytes"],
        "minimum_mem_available_bytes": minimum_mem_available,
        "host_memory_stop_threshold_bytes": MIN_RUNNING_MEM_AVAILABLE_BYTES,
        "host_memory_sampling_interval_seconds": 1,
        "cos_uploads": 0,
        "production_requests": 0,
    }, code="render_launcher_evidence_write_failed")
    return {"ok": result.get("ok") is True, "operation": spec.operation,
            "unit": spec.unit, "completed": True, "media_started": True,
            "minimum_mem_available_bytes": minimum_mem_available,
            "host_memory_stop_threshold_bytes": MIN_RUNNING_MEM_AVAILABLE_BYTES,
            "host_memory_sampling_interval_seconds": 1}


def run_decode(spec, uid, gid, lock_fd, benchmark):
    require(not spec.decode_evidence_path.exists() and
            not spec.decode_evidence_path.is_symlink(), "decode_evidence_must_be_new")
    frozen = validate_render_result(spec, uid, gid)
    require(isinstance(frozen.get("artifact_identity"), dict),
            "decode_result_freeze_invalid")
    command = decode_command(spec)
    outcome = run_fixed_child(
        benchmark, command, lock_fd, timeout=RENDER_TIMEOUT_SECONDS,
        failure_code="decode_failed", timeout_code="decode_timeout",
        cleanup_code="decode_cleanup_failed",
    )
    require(outcome.get("exit_code") == 0, "decode_exit_status_invalid")
    verified = validate_render_result(spec, uid, gid)
    require(verified["artifact"] == frozen["artifact"] and
            verified.get("artifact_identity") == frozen["artifact_identity"] and
            verified["benchmark_evidence_sha256"] == frozen["benchmark_evidence_sha256"],
            "decode_result_changed_during_decode")
    write_exclusive_json(spec.decode_evidence_path, {
        "version": 1,
        "ok": True,
        "operation": "decode",
        "candidate_sha": spec.candidate_sha,
        "run_id": spec.run_id,
        "sample_kind": spec.sample_kind,
        "configuration": spec.config.name,
        "trial": spec.trial,
        "unit": spec.unit,
        "render_unit": build_spec(
            spec.candidate_sha, spec.run_id, spec.sample_kind, spec.config.name,
            "render", spec.trial,
        ).unit,
        "result_path": path_text(spec.output_dir / "result.mp4"),
        "result_sha256": frozen["artifact"]["sha256"],
        "result_size": frozen["artifact"]["size_bytes"],
        "result_identity_before": frozen["artifact_identity"],
        "result_identity_after": verified["artifact_identity"],
        "result_reverified_after_decode": True,
        "benchmark_evidence_sha256": frozen["benchmark_evidence_sha256"],
        "elapsed_seconds": outcome["elapsed_seconds"],
        "exit_code": 0,
        "minimum_mem_available_bytes": outcome["minimum_mem_available_bytes"],
        "host_memory_stop_threshold_bytes": MIN_RUNNING_MEM_AVAILABLE_BYTES,
        "host_memory_sampling_interval_seconds": 1,
        "ffmpeg_processes": 1,
        "generated_video_files": 0,
        "cos_uploads": 0,
        "production_requests": 0,
    }, code="decode_evidence_write_failed")
    return {"ok": True, "operation": spec.operation, "unit": spec.unit,
            "completed": True, "media_started": True,
            "minimum_mem_available_bytes": outcome["minimum_mem_available_bytes"],
            "host_memory_stop_threshold_bytes": MIN_RUNNING_MEM_AVAILABLE_BYTES,
            "host_memory_sampling_interval_seconds": 1}


def finalize_resource_evidence(spec, uid, gid, guard, verified, result,
                               run_source_before, run_source_after,
                               operation_error=None):
    before = verified.get("pressure")
    require(isinstance(before, dict) and
            before.get("resources_sha256") == verified["proof"]["resources_sha256"],
            "cgroup_pressure_evidence_invalid")
    after = None
    pressure_error = None
    try:
        after = guard.capture_pressure(
            guard.LinuxFiles(), guard.LinuxProcess(), spec.unit, spec.config.cpu_cores,
            profile=guard.MEDIA_16_GIB_PROFILE,
        )
        guard.verify_pressure_transition(before, after)
    except guard.GuardFailure as exc:
        pressure_error = str(exc)
    except (OSError, KeyError, ValueError, IndexError, TypeError):
        pressure_error = "cgroup_pressure_evidence_invalid"
    minimum_mem_available = (
        result.get("minimum_mem_available_bytes") if isinstance(result, dict)
        else getattr(operation_error, "minimum_mem_available_bytes", None)
    )
    operation_succeeded = (
        operation_error is None and isinstance(result, dict) and
        result.get("ok") is True and type(minimum_mem_available) is int and
        minimum_mem_available >= MIN_RUNNING_MEM_AVAILABLE_BYTES
    )
    first = before.get("pressure") if isinstance(before, dict) else None
    last = after.get("pressure") if isinstance(after, dict) else None
    verified_pressure = pressure_error is None
    value = {
        "version": 1,
        "ok": verified_pressure and operation_succeeded,
        "candidate_sha": spec.candidate_sha,
        "run_id": spec.run_id,
        "operation": spec.operation,
        "sample_kind": spec.sample_kind,
        "configuration": spec.config.name,
        "trial": spec.trial,
        "unit": spec.unit,
        "resources_sha256": before.get("resources_sha256"),
        "before": first,
        "after": last,
        "failcnt_unchanged": bool(verified_pressure),
        "oom_unchanged": bool(verified_pressure),
        "under_oom_clear": bool(verified_pressure),
        "swap_zero": bool(verified_pressure),
        "operation_succeeded": operation_succeeded,
        "minimum_mem_available_bytes": minimum_mem_available,
        "host_memory_stop_threshold_bytes": MIN_RUNNING_MEM_AVAILABLE_BYTES,
        "host_memory_sampling_interval_seconds": 1,
        "long_source_before": run_source_before,
        "long_source_after": run_source_after,
        "cos_uploads": 0,
        "production_requests": 0,
    }
    if pressure_error is not None:
        value["pressure_error_code"] = pressure_error
    if operation_error is not None:
        value["operation_error_code"] = (
            str(operation_error) if isinstance(operation_error, LaunchFailure)
            else "media_operation_failed"
        )
    write_exclusive_json(
        spec.resource_evidence_path, value, code="resource_completion_evidence_write_failed"
    )
    if pressure_error is not None:
        raise LaunchFailure(pressure_error)
    if operation_error is not None:
        raise operation_error
    if not operation_succeeded:
        return result
    resource_fp = fingerprint_regular(
        spec.resource_evidence_path, uid, gid,
        "resource_completion_evidence_invalid", mode=0o400,
    )
    write_exclusive_json(spec.completion_evidence_path, {
        "version": 1,
        "ok": True,
        "candidate_sha": spec.candidate_sha,
        "run_id": spec.run_id,
        "operation": spec.operation,
        "sample_kind": spec.sample_kind,
        "configuration": spec.config.name,
        "trial": spec.trial,
        "unit": spec.unit,
        "resource_evidence": path_text(spec.resource_evidence_path),
        "resource_evidence_sha256": resource_fp["sha256"],
        "resource_evidence_size": resource_fp["size_bytes"],
        "cos_uploads": 0,
        "production_requests": 0,
    }, code="action_completion_evidence_write_failed")
    validate_action_completion(spec, uid, gid)
    result = dict(result)
    result["resource_pressure_verified"] = True
    return result


def internal_verified_stage(spec, proof_fd, lock_fd=None):
    require_linux()
    ensure_python_stage("verified")
    uid, gid = target_identity()
    require(os.geteuid() == uid and os.getegid() == gid and not os.getgroups(),
            "dropped_identity_not_retained")
    verify_candidate(spec)
    validate_fixed_inputs(spec)
    read_host_memory()
    candidate = verify_candidate(spec)
    guard = load_candidate_module(
        "_drama_media_resource_guard_verified", spec.candidate_root /
        "scripts/check_drama_media_resource_guard.py",
        expected_identity=candidate["critical"]["scripts/check_drama_media_resource_guard.py"],
        candidate_root=spec.candidate_root, expected_tree=candidate["tracked"],
    )
    if spec.operation == "guard-only":
        require(lock_fd is None, "invalid_internal_arguments")
        try:
            guard.run_probe(
                spec.unit, spec.config.cpu_cores, proof_fd,
                profile=guard.MEDIA_16_GIB_PROFILE,
            )
        except guard.GuardFailure as exc:
            raise LaunchFailure(str(exc)) from None
        except (OSError, KeyError, ValueError, IndexError, TypeError):
            raise LaunchFailure("media_cgroup_proof_failed") from None
        result = {
            "ok": True, "operation": "guard-only", "unit": spec.unit,
            "completed": True, "media_started": False, "media_acceptance": False,
            "guard_profile": guard.MEDIA_16_GIB_PROFILE.name,
            "memory_bytes": MEMORY_BYTES, "tasks_max": TASKS_MAX,
            "allocated_bytes": guard.PROBE_BYTES,
            "observed_seconds": guard.PROBE_SECONDS,
            "ffmpeg_processes": 0, "ffprobe_processes": 0,
        }
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    require(type(lock_fd) is int and lock_fd >= 3, "invalid_internal_arguments")
    verify_private_run_root(spec, uid, gid)
    validate_existing_action_inputs(spec, uid, gid)
    lock_identity = verify_inherited_media_lock(lock_fd)
    try:
        verified = guard.verify_inherited_guard(
            spec.unit, spec.config.cpu_cores, proof_fd,
            profile=guard.MEDIA_16_GIB_PROFILE,
        )
    except guard.GuardFailure as exc:
        raise LaunchFailure(str(exc)) from None
    except (OSError, KeyError, ValueError, IndexError, TypeError):
        raise LaunchFailure("media_cgroup_proof_failed") from None
    require(verified["proof"]["pid"] == os.getpid() and
            verified["proof"]["profile"] == guard.MEDIA_16_GIB_PROFILE.name,
            "invalid_guard_proof")
    frozen_run_source = ensure_run_source_frozen(spec, uid, gid)
    if spec.operation == "render" and spec.sample_kind == "short":
        prepared = validate_prepared_short(spec, uid, gid)
        require_prepared_source_binding(spec, prepared, uid, gid)
    write_exclusive_json(spec.launcher_guard_path, {
        "version": 1,
        "ok": True,
        "phase": "verified_before_benchmark",
        "candidate_sha": spec.candidate_sha,
        "run_id": spec.run_id,
        "sample_kind": spec.sample_kind,
        "configuration": spec.config.name,
        "trial": spec.trial,
        "operation": spec.operation,
        "unit": spec.unit,
        "guard_profile": verified["proof"]["profile"],
        "resources_sha256": verified["proof"]["resources_sha256"],
        "lock_device": lock_identity[0],
        "lock_inode": lock_identity[1],
        "operation_timeout_seconds": RENDER_TIMEOUT_SECONDS,
        "cos_uploads": 0,
        "production_requests": 0,
    })
    candidate = verify_candidate(spec)
    benchmark = load_candidate_module(
        "_drama_media_acceptance_benchmark", spec.candidate_root /
        "scripts/benchmark_drama_synthesis_media.py",
        expected_identity=candidate["critical"]["scripts/benchmark_drama_synthesis_media.py"],
        candidate_root=spec.candidate_root, expected_tree=candidate["tracked"],
    )
    require(getattr(benchmark, "MEDIA_ACCEPTANCE_LOCK_PATH", None) == LOCK_PATH,
            "benchmark_lock_protocol_mismatch")
    result = None
    operation_error = None
    try:
        if spec.operation == "prepare-short":
            result = run_prepare_short(spec, uid, gid, lock_fd, benchmark)
            prepared = validate_prepared_short(spec, uid, gid)
            require_prepared_source_binding(spec, prepared, uid, gid)
        elif spec.operation == "decode":
            result = run_decode(spec, uid, gid, lock_fd, benchmark)
        else:
            result = run_render(spec, uid, gid, lock_fd, benchmark)
    except BaseException as exc:
        operation_error = exc
    ending_run_source = None
    try:
        ending_run_source = ensure_run_source_frozen(spec, uid, gid)
        require(ending_run_source == frozen_run_source, "fixed_long_source_changed")
    except BaseException as exc:
        if operation_error is None:
            operation_error = exc
    try:
        ending_candidate = verify_candidate(spec)
        require(ending_candidate == candidate, "candidate_changed_during_media_action")
    except BaseException as exc:
        if operation_error is None:
            operation_error = exc
    result = finalize_resource_evidence(
        spec, uid, gid, guard, verified, result, frozen_run_source,
        ending_run_source, operation_error=operation_error
    )
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result.get("ok") is True else 1


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    execution = value.add_mutually_exclusive_group()
    execution.add_argument("--apply", action="store_true",
                           help="Submit the selected fixed guarded unit; omission only previews")
    execution.add_argument("--preflight", action="store_true",
                           help="Run fixed read-only/no-media host checks without submitting a unit")
    operation = value.add_mutually_exclusive_group()
    operation.add_argument("--prepare-short", action="store_true",
                           help="Select the fixed 120-second stream-copy preparation")
    operation.add_argument("--decode", action="store_true",
                           help="Select fixed full decode of the matching result")
    operation.add_argument("--guard-only", action="store_true",
                           help="Select the 16 GiB guard plus fixed 8 MiB/3 second non-media probe")
    value.add_argument("--candidate-sha", required=True)
    value.add_argument("--run-id", required=True)
    value.add_argument("--sample-kind", choices=("short", "long"), required=True)
    value.add_argument("--config", choices=tuple(item.name for item in CONFIGS), required=True)
    value.add_argument("--trial", choices=("r1", "r2"), required=True)
    value.add_argument("--internal-stage", choices=("guard", "verified"),
                       help=argparse.SUPPRESS)
    value.add_argument("--guard-proof-fd", type=int, help=argparse.SUPPRESS)
    value.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    spec = None
    try:
        operation = ("prepare-short" if args.prepare_short else
                     "decode" if args.decode else
                     "guard-only" if args.guard_only else "render")
        spec = build_spec(
            args.candidate_sha, args.run_id, args.sample_kind, args.config,
            operation, args.trial,
        )
        if args.internal_stage == "guard":
            require(not args.apply and not args.preflight and
                    args.guard_proof_fd is None and args.lock_fd is None,
                    "invalid_internal_arguments")
            internal_guard_stage(spec)
            return 0
        if args.internal_stage == "verified":
            require(not args.apply and not args.preflight and
                    args.guard_proof_fd is not None and
                    ((spec.operation == "guard-only" and args.lock_fd is None) or
                     (spec.operation != "guard-only" and args.lock_fd is not None)),
                    "invalid_internal_arguments")
            return internal_verified_stage(spec, args.guard_proof_fd, args.lock_fd)
        require(args.guard_proof_fd is None and args.lock_fd is None,
                "invalid_internal_arguments")
        value = submit(spec) if args.apply else (
            run_public_preflight(spec) if args.preflight else preview(spec)
        )
        print(json.dumps(value, sort_keys=True), flush=True)
        return 0
    except SubmissionUncertain as exc:
        reason = str(exc)
        print(json.dumps({
            "ok": False,
            "error_code": reason,
            "unit": spec.unit if spec is not None else None,
            "candidate_sha": spec.candidate_sha if spec is not None else None,
            "run_id": spec.run_id if spec is not None else None,
            "operation": spec.operation if spec is not None else None,
            "sample_kind": spec.sample_kind if spec is not None else None,
            "configuration": spec.config.name if spec is not None else None,
            "trial": spec.trial if spec is not None else None,
            "media_started": (False if spec is not None and
                              spec.operation == "guard-only" else None),
            "completion_unknown": True,
            "replay_forbidden": True,
        }, sort_keys=True), flush=True)
        return 78
    except (LaunchFailure, OSError, OverflowError, ValueError, KeyError, TypeError) as exc:
        reason = str(exc) if isinstance(exc, LaunchFailure) else "media_acceptance_launcher_failed"
        media_started = ((False if spec.operation == "guard-only" else None)
                         if spec is not None and args.internal_stage in ("guard", "verified")
                         else False)
        print(json.dumps({"ok": False, "error_code": reason, "media_started": media_started,
                          "completion_unknown": (spec is not None and
                                                 args.internal_stage in ("guard", "verified") and
                                                 spec.operation != "guard-only")},
                         sort_keys=True), flush=True)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
