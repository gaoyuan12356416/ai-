#!/usr/bin/env python3
"""Bounded, opt-in COS upload acceptance; never renders or calls the business API.

Without --apply this script only prints a plan, without reading credentials or
opening media. Apply requires a clean, committed candidate checkout, a private
POSIX credential file, a new evidence directory and an unused acceptance prefix.
The operator must fetch the candidate from GitHub before invoking it remotely.

Both injected failures happen AFTER a real successful SDK response. They test
lost-response reconciliation, not a physical network outage. No create-response
loss is injected; no object or multipart upload is ever deleted or aborted.
"""
from __future__ import annotations

# Real acceptance must start in an interpreter that cannot import checkout or
# PYTHONPATH shadows before the candidate cleanliness gate runs.  Keep this
# bootstrap check ahead of every non-builtin import.
import sys

if "--apply" in sys.argv[1:] and not (
        sys.flags.isolated == 1
        and sys.flags.ignore_environment == 1
        and sys.flags.no_user_site == 1
        and sys.flags.no_site == 1
        and sys.flags.dont_write_bytecode == 1
        and getattr(sys, "pycache_prefix", None) is None):
    print('{"code":"runtime_unverified","cos_cleanup_performed":false,'
          '"instruction":"Use the fixed runtime with python -I -B -S; no COS request was made.",'
          '"status":"not_passed"}')
    raise SystemExit(1)

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib
from importlib import machinery as importlib_machinery
from importlib import metadata as importlib_metadata
from importlib import util as importlib_util
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sysconfig
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PREFIX = Path("/data/drama-synthesis-gpu/runtime")
RUNTIME_PYTHON = RUNTIME_PREFIX / "bin" / "python"
FFPROBE_PATH = RUNTIME_PREFIX / "bin" / "ffprobe"
GIT_PATH = Path("/usr/bin/git")

# Candidate and COS modules are deliberately absent at module import time.
# Real apply populates these only after the clean-checkout/runtime gates. Tests
# inject exact local fixtures without entering the real apply path.
cos_upload = None
DramaSynthesisError = None
atomic_write_record = None
file_fingerprint = None
read_record = None
_VERIFIED_SDK_RUNTIME = None


MIB = 1024 * 1024
MIN_BYTES = 16 * MIB
MAX_BYTES = 256 * MIB
MAX_NOTIFICATION_BYTES = 64 * 1024
LOCAL_TOOL_OUTPUT_MAX_BYTES = 4 * MIB
FFPROBE_OUTPUT_MAX_BYTES = 64 * 1024
ACCEPTANCE_DEADLINE_SECONDS = 3600
CLEANUP_DEADLINE_SECONDS = 30
OUTER_RUNTIME_MAX_SECONDS = 3660
SDK_VERSION = "1.9.44"
TRANSPORT_DISTRIBUTIONS = ("requests", "urllib3", "certifi")
TRANSPORT_MODULE_ROOTS = (
    "qcloud_cos", "requests", "urllib3", "certifi", "charset_normalizer",
    "chardet", "idna", "six", "crcmod", "xmltodict",
)
ENV_KEYS = {"COS_SECRET_ID", "COS_SECRET_KEY", "COS_BUCKET", "COS_REGION"}
API_NAMES = (
    "list_objects", "list_multipart_uploads", "get_bucket_versioning", "get_bucket_notification_v2", "head_object",
    "get_object_acl",
    "create_multipart_upload", "list_parts", "upload_part", "complete_multipart_upload", "get_object",
)
WRITE_NAMES = ("create_multipart_upload", "upload_part", "complete_multipart_upload")
SAFE_ERRORS = {
    "arguments_invalid", "posix_required", "candidate_unverified", "credential_file_unverified",
    "credentials_invalid", "source_unverified", "source_size_outside_acceptance_limit",
    "ffprobe_failed", "evidence_directory_not_fresh", "insufficient_disk_space", "sdk_unverified",
    "scope_violation", "prefix_not_empty_or_unverified", "part_loss_not_verified",
    "completion_loss_not_verified", "checkpoint_not_preserved", "replay_created_work",
    "api_counts_not_verified", "download_not_verified", "source_changed", "interrupted",
    "notification_configuration_not_empty_or_unverified", "anonymous_access_not_private_or_unverified",
    "object_acl_not_private_or_unverified", "acceptance_deadline_exceeded", "verification_failed",
    "runtime_unverified",
}


class VerificationError(Exception):
    def __init__(self, code):
        self.code = code if code in SAFE_ERRORS else "verification_failed"
        super().__init__(self.code)


class InjectedResponseLoss(Exception):
    """Deliberately discarded successful response; contains no SDK exception text."""


class AcceptanceDeadline:
    """Fixed whole-run deadline; only the monotonic clock is injectable for tests."""

    def __init__(self, clock=time.monotonic):
        require(callable(clock), "arguments_invalid")
        self.clock = clock
        self.started = float(clock())
        self.exceeded = False
        self._armed = False
        self._previous_handler = None
        self._cleanup_started = None

    def elapsed(self):
        try:
            return max(0.0, float(self.clock()) - self.started)
        except Exception:
            self.exceeded = True
            raise VerificationError("acceptance_deadline_exceeded") from None

    def check(self):
        if self.elapsed() >= ACCEPTANCE_DEADLINE_SECONDS:
            self.exceeded = True
            raise VerificationError("acceptance_deadline_exceeded")

    def request_timeout(self, requested=60):
        self.check()
        try:
            if isinstance(requested, tuple):
                requested = max(float(item) for item in requested)
            requested = float(requested)
        except (TypeError, ValueError):
            requested = 60.0
        require(math.isfinite(requested) and requested > 0, "acceptance_deadline_exceeded")
        remaining = ACCEPTANCE_DEADLINE_SECONDS - self.elapsed()
        if remaining <= 0:
            self.exceeded = True
            raise VerificationError("acceptance_deadline_exceeded")
        return min(requested, remaining)

    def arm(self):
        """Install a POSIX hard timer so blocking local I/O cannot outlive the deadline."""
        require(os.name == "posix" and not self._armed, "posix_required")
        self.check()
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        require(previous_timer == (0.0, 0.0), "acceptance_deadline_exceeded")
        self._previous_handler = signal.getsignal(signal.SIGALRM)

        def expired(_signum, _frame):
            self.exceeded = True
            raise VerificationError("acceptance_deadline_exceeded")

        signal.signal(signal.SIGALRM, expired)
        self._armed = True
        try:
            signal.setitimer(signal.ITIMER_REAL, self.request_timeout(ACCEPTANCE_DEADLINE_SECONDS))
        except BaseException:
            self.disarm()
            raise

    def begin_cleanup(self):
        """Bound post-deadline persistence/close to one shared 30-second window."""
        self.exceeded = True
        if self._cleanup_started is None:
            try:
                self._cleanup_started = float(self.clock())
            except Exception:
                raise VerificationError("acceptance_deadline_exceeded") from None
        self.prepare_bounded_action()

    def cleanup_remaining(self):
        require(self._cleanup_started is not None, "acceptance_deadline_exceeded")
        try:
            remaining = CLEANUP_DEADLINE_SECONDS - (float(self.clock()) - self._cleanup_started)
        except Exception:
            raise VerificationError("acceptance_deadline_exceeded") from None
        require(math.isfinite(remaining) and remaining > 0, "acceptance_deadline_exceeded")
        return min(float(CLEANUP_DEADLINE_SECONDS), remaining)

    def prepare_bounded_action(self):
        """Arm the applicable hard timer immediately before cleanup/report I/O."""
        if self._cleanup_started is None:
            self.check()
            return
        remaining = self.cleanup_remaining()
        if self._armed:
            signal.setitimer(signal.ITIMER_REAL, remaining)

    def finish_bounded_action(self):
        if self._cleanup_started is None:
            self.check()
        else:
            self.cleanup_remaining()

    def disarm(self):
        if not self._armed:
            return
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self._previous_handler)
        self._armed = False


def require(condition, code):
    if not condition:
        raise VerificationError(code)


def acceptance_prefix(candidate_sha, run_id):
    require(isinstance(candidate_sha, str) and re.fullmatch(r"[0-9a-f]{40}", candidate_sha), "arguments_invalid")
    require(isinstance(run_id, str) and re.fullmatch(r"cos-[a-z0-9][a-z0-9-]{7,63}", run_id), "arguments_invalid")
    return "drama-synthesis-acceptance/{}/{}/".format(candidate_sha, run_id)


def absolute_path(value, code):
    require(isinstance(value, (str, Path)) and bool(str(value)), code)
    path = Path(value)
    require(path.is_absolute() and ".." not in path.parts, code)
    return path


def no_symlinks(path, code):
    require(not any(item.is_symlink() for item in (path, *path.parents)), code)


def verify_isolated_runtime():
    """Require the fixed venv and Python's complete environment isolation."""
    require(
        os.name == "posix"
        and sys.flags.isolated == 1
        and sys.flags.ignore_environment == 1
        and sys.flags.no_user_site == 1
        and sys.flags.no_site == 1
        and sys.flags.dont_write_bytecode == 1
        and sys.dont_write_bytecode is True
        and getattr(sys, "pycache_prefix", None) is None,
        "runtime_unverified",
    )
    no_symlinks(RUNTIME_PREFIX, "runtime_unverified")
    require(Path(sys.executable).is_absolute() and Path(sys.executable) == RUNTIME_PYTHON,
            "runtime_unverified")
    _secure_regular_file(RUNTIME_PYTHON, "runtime_unverified", executable=True)


def clean_subprocess_environment():
    """A fixed environment for audited local tools; ambient variables are omitted."""
    return {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1"}


def clean_git_environment():
    """A fixed Git namespace: no ambient config, object replacement or optional locks."""
    return {
        "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat",
    }


def _terminate_process(process):
    """Best-effort bounded-process termination without exposing child output."""
    try:
        poll = getattr(process, "poll", None)
        if callable(poll) and poll() is not None:
            return
        if os.name == "posix" and isinstance(getattr(process, "pid", None), int):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError, AttributeError):
        try:
            process.kill()
        except (OSError, ProcessLookupError, AttributeError):
            pass


def _terminate_and_reap(process):
    """Kill a live process group and spend at most ten seconds reaping its leader."""
    for _attempt in range(2):
        try:
            _terminate_process(process)
        except BaseException:
            pass
        try:
            process.wait(timeout=5)
            return
        except BaseException:
            continue


def _join_and_close_process_streams(workers, streams):
    cleanup_end = time.monotonic() + 5.0
    for worker in workers:
        remaining = max(0.0, cleanup_end - time.monotonic())
        if worker.is_alive() and remaining > 0:
            worker.join(timeout=remaining)
    if not any(worker.is_alive() for worker in workers):
        for stream in streams:
            try:
                stream.close()
            except (OSError, ValueError, AttributeError):
                pass


def run_bounded_process(command, *, deadline, timeout, output_limit, cwd, env, code):
    """Run a fixed local tool while draining both pipes into hard-capped buffers."""
    require(isinstance(command, (list, tuple)) and command and all(
        isinstance(item, str) and item for item in command), code)
    require(type(output_limit) is int and 0 < output_limit <= LOCAL_TOOL_OUTPUT_MAX_BYTES, code)
    deadline.check()
    process = None
    streams = []
    workers = []
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()
    reader_failed = threading.Event()
    succeeded = False

    def drain(name, stream):
        try:
            while True:
                remaining = output_limit + 1 - len(buffers[name])
                if remaining <= 0:
                    overflow.set()
                    _terminate_process(process)
                    return
                chunk = stream.read(min(8192, remaining))
                if chunk == b"":
                    return
                if not isinstance(chunk, bytes):
                    reader_failed.set()
                    _terminate_process(process)
                    return
                buffers[name].extend(chunk)
                if len(buffers[name]) > output_limit:
                    overflow.set()
                    _terminate_process(process)
                    return
        except BaseException:
            reader_failed.set()
            _terminate_process(process)

    try:
        process = subprocess.Popen(
            list(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, close_fds=True, cwd=cwd, env=dict(env),
            start_new_session=True,
        )
        require(process.stdout is not None and process.stderr is not None, code)
        streams = [process.stdout, process.stderr]
        for name, stream in zip(("stdout", "stderr"), streams):
            worker = threading.Thread(target=drain, args=(name, stream), daemon=True)
            workers.append(worker)
            worker.start()
        try:
            process.wait(timeout=deadline.request_timeout(timeout))
        except subprocess.TimeoutExpired:
            raise VerificationError(code) from None
        deadline.check()
        for worker in workers:
            worker.join(timeout=deadline.request_timeout(5))
            require(not worker.is_alive(), code)
        require(not overflow.is_set() and not reader_failed.is_set(), code)
        require(len(buffers["stdout"]) <= output_limit
                and len(buffers["stderr"]) <= output_limit, code)
        result = SimpleNamespace(
            returncode=process.returncode, stdout=bytes(buffers["stdout"]),
            stderr=bytes(buffers["stderr"]),
        )
        succeeded = True
        return result
    except VerificationError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
        deadline.check()
        raise VerificationError(code) from None
    finally:
        if succeeded:
            _join_and_close_process_streams(workers, streams)
        else:
            try:
                if process is not None:
                    _terminate_and_reap(process)
            except BaseException:
                pass
            try:
                _join_and_close_process_streams(workers, streams)
            except BaseException:
                pass


def _secure_regular_file(path, code, *, executable=False):
    """Return a resolved, root-owned regular file with no group/other writes."""
    require(os.name == "posix" and isinstance(path, Path) and path.is_absolute(), code)
    try:
        link_info = path.lstat()
        require(stat.S_ISREG(link_info.st_mode) or stat.S_ISLNK(link_info.st_mode), code)
        require(link_info.st_uid == 0, code)
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except (OSError, RuntimeError):
        raise VerificationError(code) from None
    require(resolved.is_absolute() and stat.S_ISREG(info.st_mode) and info.st_uid == 0
            and stat.S_IMODE(info.st_mode) & 0o022 == 0, code)
    for start in {path.parent, resolved.parent}:
        current = start
        while True:
            try:
                directory = current.stat()
            except OSError:
                raise VerificationError(code) from None
            require(stat.S_ISDIR(directory.st_mode) and directory.st_uid == 0
                    and stat.S_IMODE(directory.st_mode) & 0o022 == 0, code)
            if current.parent == current:
                break
            current = current.parent
    if executable:
        require(info.st_mode & 0o111 != 0 and os.access(str(resolved), os.X_OK), code)
    return resolved


def _secure_directory(path, code):
    """Return a resolved, root-owned directory with no group/other writes."""
    require(os.name == "posix" and isinstance(path, Path) and path.is_absolute(), code)
    try:
        link_info = path.lstat()
        require(stat.S_ISDIR(link_info.st_mode) or stat.S_ISLNK(link_info.st_mode), code)
        require(link_info.st_uid == 0, code)
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise VerificationError(code) from None
    for start in {path, resolved}:
        current = start
        while True:
            try:
                info = current.stat()
            except OSError:
                raise VerificationError(code) from None
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
                    and stat.S_IMODE(info.st_mode) & 0o022 == 0, code)
            if current.parent == current:
                break
            current = current.parent
    return resolved


def _hash_file(path, deadline, code):
    digest = hashlib.sha256()
    try:
        deadline.check()
        with path.open("rb") as handle:
            while True:
                deadline.check()
                chunk = handle.read(MIB)
                deadline.check()
                if chunk == b"":
                    break
                require(isinstance(chunk, bytes), code)
                digest.update(chunk)
    except VerificationError:
        raise
    except OSError:
        raise VerificationError(code) from None
    return digest.hexdigest()


def verify_ffprobe_binary(deadline=None):
    """Verify only the fixed HK ffprobe (or its fixed, root-owned symlink target)."""
    deadline = deadline or AcceptanceDeadline()
    deadline.check()
    resolved = _secure_regular_file(FFPROBE_PATH, "ffprobe_failed", executable=True)
    digest = _hash_file(resolved, deadline, "ffprobe_failed")
    deadline.check()
    return resolved, {
        "path": str(FFPROBE_PATH), "realpath": str(resolved), "sha256": digest,
        "root_owned": True, "group_other_writable": False,
    }


def verify_git_binary(deadline=None):
    """Verify the fixed system Git without consulting PATH or the checkout."""
    deadline = deadline or AcceptanceDeadline()
    deadline.check()
    resolved = _secure_regular_file(GIT_PATH, "candidate_unverified", executable=True)
    deadline.check()
    return resolved


def _run_git(git, arguments, deadline, *, output_limit=LOCAL_TOOL_OUTPUT_MAX_BYTES):
    """Run one audited Git command with the fsmonitor and replacement namespace disabled."""
    require(isinstance(git, Path) and git.is_absolute(), "candidate_unverified")
    command = [
        str(git), "--no-pager", "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=/dev/null", "-c", "core.quotepath=true",
        "-C", str(ROOT), *arguments,
    ]
    return run_bounded_process(
        command, deadline=deadline, timeout=10, output_limit=output_limit,
        cwd=str(ROOT), env=clean_git_environment(), code="candidate_unverified",
    )


def _git_text(result, *, allowed_returncodes=(0,)):
    require(result.returncode in allowed_returncodes
            and isinstance(result.stdout, bytes) and isinstance(result.stderr, bytes)
            and result.stderr == b"",
            "candidate_unverified")
    try:
        return result.stdout.decode("utf-8", "strict").strip()
    except UnicodeDecodeError:
        raise VerificationError("candidate_unverified") from None


def _verify_git_version(git, deadline):
    version_text = _git_text(_run_git(git, ["--version"], deadline))
    version_match = re.fullmatch(r"git version (\d+)\.(\d+)\.(\d+)(?:[^\s]*)?", version_text)
    require(version_match is not None
            and tuple(int(item) for item in version_match.groups()) >= (2, 36, 0),
            "candidate_unverified")


CANDIDATE_MODULE_PATHS = {
    "features": ROOT / "features" / "__init__.py",
    "features.drama_synthesis": ROOT / "features" / "drama_synthesis" / "__init__.py",
    "features.drama_synthesis.core": ROOT / "features" / "drama_synthesis" / "core.py",
    "features.drama_synthesis.local_checkpoint": ROOT / "features" / "drama_synthesis" / "local_checkpoint.py",
    "features.drama_synthesis.gpu_cache": ROOT / "features" / "drama_synthesis" / "gpu_cache.py",
    "features.drama_synthesis.async_runtime": ROOT / "features" / "drama_synthesis" / "async_runtime.py",
    "features.drama_synthesis.cos_upload": ROOT / "features" / "drama_synthesis" / "cos_upload.py",
}


def validate_candidate_module_origins(modules):
    require(isinstance(modules, dict) and set(modules) == set(CANDIDATE_MODULE_PATHS),
            "candidate_unverified")
    for name, expected in CANDIDATE_MODULE_PATHS.items():
        module_file = getattr(modules[name], "__file__", None)
        try:
            actual = Path(module_file).resolve(strict=True)
            expected_resolved = expected.resolve(strict=True)
        except (TypeError, OSError, RuntimeError):
            raise VerificationError("candidate_unverified") from None
        loader = getattr(modules[name], "__loader__", None)
        try:
            loader_path = Path(loader.path).resolve(strict=True)
        except (AttributeError, TypeError, OSError, RuntimeError):
            raise VerificationError("candidate_unverified") from None
        require(actual == expected_resolved and loader_path == expected_resolved
                and isinstance(loader, importlib_machinery.SourceFileLoader), "candidate_unverified")


class VerifiedCandidateSourceLoader(importlib_machinery.SourceFileLoader):
    """Execute the already verified Git blob and never consult bytecode."""

    def __init__(self, fullname, path, source):
        super().__init__(fullname, str(path))
        self._verified_source = source

    def get_data(self, path):
        try:
            requested = Path(path).resolve(strict=False)
            expected = Path(self.path).resolve(strict=True)
        except (OSError, RuntimeError):
            raise OSError("candidate source unavailable") from None
        if requested != expected:
            raise OSError("candidate bytecode access forbidden")
        return self._verified_source

    def set_data(self, _path, _data, *_args, **_kwargs):
        raise OSError("candidate bytecode writes forbidden")


def _verified_candidate_blob(git, candidate_sha, path, deadline):
    try:
        deadline.check()
        relative = path.relative_to(ROOT).as_posix()
        no_symlinks(path, "candidate_unverified")
        info = path.stat()
        require(stat.S_ISREG(info.st_mode) and 0 <= info.st_size <= 4 * MIB,
                "candidate_unverified")
        result = _run_git(
            git, ["show", "{}:{}".format(candidate_sha, relative)], deadline,
            output_limit=4 * MIB,
        )
        deadline.check()
        disk_source = path.read_bytes()
        deadline.check()
    except VerificationError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise VerificationError("candidate_unverified") from None
    require(result.returncode == 0 and isinstance(result.stdout, bytes)
            and isinstance(result.stderr, bytes) and result.stderr == b""
            and len(result.stdout) <= 4 * MIB and disk_source == result.stdout,
            "candidate_unverified")
    return result.stdout


def _candidate_git_blobs(candidate_sha, deadline):
    require(isinstance(candidate_sha, str) and re.fullmatch(r"[0-9a-f]{40}", candidate_sha),
            "candidate_unverified")
    require(all(not name.upper().startswith("GIT_") for name in os.environ),
            "candidate_unverified")
    git = verify_git_binary(deadline)
    _verify_git_version(git, deadline)
    blobs = {}
    for name, path in CANDIDATE_MODULE_PATHS.items():
        blobs[name] = _verified_candidate_blob(git, candidate_sha, path, deadline)
    return blobs


def _load_candidate_source(name, path, source, *, package=False, execute=True):
    locations = [str(path.parent)] if package else None
    loader = VerifiedCandidateSourceLoader(name, path, source)
    spec = importlib_util.spec_from_file_location(
        name, str(path), loader=loader, submodule_search_locations=locations)
    require(spec is not None and spec.loader is loader, "candidate_unverified")
    module = importlib_util.module_from_spec(spec)
    sys.modules[name] = module
    if "." in name:
        parent_name, attribute = name.rsplit(".", 1)
        parent = sys.modules.get(parent_name)
        require(parent is not None, "candidate_unverified")
        setattr(parent, attribute, module)
    if execute:
        spec.loader.exec_module(module)
    return module


def load_verified_candidate_modules(candidate_sha, deadline):
    """Import tracked candidate modules only after the full cleanliness gate."""
    global cos_upload, DramaSynthesisError, atomic_write_record, file_fingerprint, read_record
    deadline.check()
    require(all(not (name == "features" or name.startswith("features.")) for name in sys.modules),
            "candidate_unverified")
    blobs = _candidate_git_blobs(candidate_sha, deadline)
    try:
        modules = {}
        modules["features"] = _load_candidate_source(
            "features", CANDIDATE_MODULE_PATHS["features"], blobs["features"], package=True)
        modules["features.drama_synthesis"] = _load_candidate_source(
            "features.drama_synthesis", CANDIDATE_MODULE_PATHS["features.drama_synthesis"],
            blobs["features.drama_synthesis"],
            package=True, execute=False)
        for name in (
                "features.drama_synthesis.core",
                "features.drama_synthesis.local_checkpoint",
                "features.drama_synthesis.gpu_cache",
                "features.drama_synthesis.async_runtime",
                "features.drama_synthesis.cos_upload",
        ):
            modules[name] = _load_candidate_source(name, CANDIDATE_MODULE_PATHS[name], blobs[name])
        # Execute the package initializer only after its exact core dependency
        # is already source-loaded, preventing any bytecode/path fallback.
        modules["features.drama_synthesis"].__spec__.loader.exec_module(
            modules["features.drama_synthesis"])
    except Exception:
        for name in reversed(tuple(CANDIDATE_MODULE_PATHS)):
            sys.modules.pop(name, None)
        raise VerificationError("candidate_unverified") from None
    validate_candidate_module_origins(modules)
    core = modules["features.drama_synthesis.core"]
    checkpoint = modules["features.drama_synthesis.local_checkpoint"]
    cos_upload = modules["features.drama_synthesis.cos_upload"]
    DramaSynthesisError = getattr(core, "DramaSynthesisError", None)
    atomic_write_record = getattr(checkpoint, "atomic_write_record", None)
    file_fingerprint = getattr(checkpoint, "file_fingerprint", None)
    read_record = getattr(checkpoint, "read_record", None)
    require(isinstance(DramaSynthesisError, type) and all(callable(item) for item in (
        atomic_write_record, file_fingerprint, read_record, getattr(cos_upload, "resume_upload", None),
    )), "candidate_unverified")
    deadline.check()


def require_candidate_modules():
    require(cos_upload is not None and isinstance(DramaSynthesisError, type)
            and all(callable(item) for item in (atomic_write_record, file_fingerprint, read_record)),
            "candidate_unverified")


def _verified_distribution_file(path, prefix, deadline):
    deadline.check()
    resolved = _secure_regular_file(path, "sdk_unverified")
    try:
        resolved.relative_to(prefix)
    except ValueError:
        raise VerificationError("sdk_unverified") from None
    deadline.check()
    return resolved


def _is_within(path, roots):
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _read_small_text(path, deadline, code, limit=65536):
    try:
        deadline.check()
        with path.open("rb") as handle:
            value = handle.read(limit + 1)
        deadline.check()
        require(len(value) <= limit and b"\x00" not in value, code)
        return value.decode("utf-8", "strict")
    except VerificationError:
        raise
    except (OSError, UnicodeDecodeError):
        raise VerificationError(code) from None


def _verified_dependency_roots(prefix, deadline):
    """Prove the venv has one explicit, immutable dependency namespace and no .pth code."""
    deadline.check()
    no_symlinks(RUNTIME_PREFIX, "sdk_unverified")
    prefix = _secure_directory(prefix, "sdk_unverified")
    try:
        expected = prefix / "lib" / "python{}.{}".format(
            sys.version_info[0], sys.version_info[1]) / "site-packages"
        no_symlinks(expected, "sdk_unverified")
        roots = {_secure_directory(expected, "sdk_unverified")}
    except (OSError, RuntimeError, TypeError):
        raise VerificationError("sdk_unverified") from None
    require(roots and all(_is_within(root, {prefix}) for root in roots), "sdk_unverified")
    for root in roots:
        try:
            pth_files = tuple(root.glob("*.pth"))
        except OSError:
            raise VerificationError("sdk_unverified") from None
        require(not pth_files, "sdk_unverified")

    config = _secure_regular_file(prefix / "pyvenv.cfg", "sdk_unverified")
    contents = _read_small_text(config, deadline, "sdk_unverified")
    values = re.findall(
        r"(?im)^\s*include-system-site-packages\s*=\s*([^\s#]+)\s*(?:#.*)?$", contents)
    require([item.lower() for item in values] == ["false"], "sdk_unverified")
    deadline.check()
    return frozenset(roots)


def _verified_dependency_tree(root, deadline):
    """Validate every possible dependency payload before any package import."""
    root = _secure_directory(root, "sdk_unverified")
    stack = [root]
    verified_files = 0
    while stack:
        deadline.check()
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    deadline.check()
                    path = Path(entry.path)
                    try:
                        require(not entry.is_symlink(), "sdk_unverified")
                        if entry.is_dir(follow_symlinks=False):
                            require(entry.name != "__pycache__", "sdk_unverified")
                            resolved = _secure_directory(path, "sdk_unverified")
                            require(_is_within(resolved, {root}), "sdk_unverified")
                            stack.append(resolved)
                        elif entry.is_file(follow_symlinks=False):
                            require(path.suffix.lower()
                                    not in (".pyc", ".pyo", ".pth", ".egg-link"),
                                    "sdk_unverified")
                            resolved = _secure_regular_file(path, "sdk_unverified")
                            require(_is_within(resolved, {root}), "sdk_unverified")
                            verified_files += 1
                        else:
                            raise VerificationError("sdk_unverified")
                    except VerificationError:
                        raise
                    except OSError:
                        raise VerificationError("sdk_unverified") from None
        except VerificationError:
            raise
        except OSError:
            raise VerificationError("sdk_unverified") from None
    require(verified_files > 0, "sdk_unverified")
    deadline.check()
    return verified_files


def _verified_runtime_import_paths(prefix, dependency_roots, deadline):
    """Reject checkout, user-site, system-site and arbitrary zip/path injection."""
    try:
        paths = sysconfig.get_paths()
        stdlib_roots = {
            Path(paths[name]).resolve(strict=True) for name in ("stdlib", "platstdlib")
        }
    except (KeyError, OSError, RuntimeError, TypeError):
        raise VerificationError("sdk_unverified") from None
    stdlib_roots = {_secure_directory(root, "sdk_unverified") for root in stdlib_roots}
    allowed_missing_zips = {
        root.parent / "python{}{}.zip".format(sys.version_info[0], sys.version_info[1])
        for root in stdlib_roots
    }
    prefix = prefix.resolve(strict=True)
    for entry in sys.path:
        deadline.check()
        require(isinstance(entry, str) and entry and Path(entry).is_absolute(), "sdk_unverified")
        candidate = Path(entry)
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            require(candidate in allowed_missing_zips, "sdk_unverified")
            _secure_directory(candidate.parent, "sdk_unverified")
            continue
        parts = {part.lower() for part in resolved.parts}
        dependency_path = _is_within(resolved, dependency_roots)
        if "site-packages" in parts or "dist-packages" in parts:
            require(dependency_path, "sdk_unverified")
        if resolved.is_dir():
            _secure_directory(resolved, "sdk_unverified")
            require(_is_within(resolved, stdlib_roots) and not dependency_path,
                    "sdk_unverified")
        else:
            _secure_regular_file(resolved, "sdk_unverified")
            require(resolved in allowed_missing_zips, "sdk_unverified")
    deadline.check()
    return frozenset(stdlib_roots)


def _verified_module_file(module, dependency_roots, deadline):
    module_file = getattr(module, "__file__", None)
    require(isinstance(module_file, str) and module_file, "sdk_unverified")
    resolved = _secure_regular_file(Path(module_file), "sdk_unverified")
    require(_is_within(resolved, dependency_roots), "sdk_unverified")
    deadline.check()
    return resolved


def _verified_module_spec(name, dependency_roots, deadline):
    try:
        spec = importlib_util.find_spec(name)
    except (ImportError, AttributeError, ValueError):
        raise VerificationError("sdk_unverified") from None
    origin_name = getattr(spec, "origin", None)
    require(spec is not None and isinstance(origin_name, str) and origin_name,
            "sdk_unverified")
    require(isinstance(getattr(spec, "loader", None), (
        importlib_machinery.SourceFileLoader, importlib_machinery.ExtensionFileLoader,
    )), "sdk_unverified")
    origin = _secure_regular_file(Path(origin_name), "sdk_unverified")
    require(_is_within(origin, dependency_roots), "sdk_unverified")
    cached = getattr(spec, "cached", None)
    if cached is not None:
        cached_path = Path(cached)
        require(cached_path.is_absolute() and not cached_path.exists()
                and _is_within(cached_path.resolve(strict=False), dependency_roots),
                "sdk_unverified")
    search_locations = getattr(spec, "submodule_search_locations", None)
    if search_locations is not None:
        locations = tuple(search_locations)
        require(locations, "sdk_unverified")
        for location in locations:
            resolved = _secure_directory(Path(location), "sdk_unverified")
            require(_is_within(resolved, dependency_roots), "sdk_unverified")
    deadline.check()
    return origin


def _verified_transport_distribution(name, dependency_roots, deadline):
    distribution = importlib_metadata.distribution(name)
    require(isinstance(distribution.version, str) and distribution.version
            and distribution.files, "sdk_unverified")
    installed = set()
    for entry in distribution.files:
        candidate = Path(distribution.locate_file(entry))
        require(candidate.is_file() or candidate.is_symlink(), "sdk_unverified")
        resolved = _secure_regular_file(candidate, "sdk_unverified")
        require(_is_within(resolved, dependency_roots), "sdk_unverified")
        installed.add(resolved)
        deadline.check()
    require(installed, "sdk_unverified")
    return distribution.version, installed


def _virtual_module_has_verified_parent(name, dependency_roots, stdlib_roots, deadline):
    current = name
    while "." in current:
        current = current.rsplit(".", 1)[0]
        parent = sys.modules.get(current)
        parent_file = getattr(parent, "__file__", None)
        if not parent_file:
            continue
        resolved = _secure_regular_file(Path(parent_file), "sdk_unverified")
        deadline.check()
        return _is_within(resolved, dependency_roots) or _is_within(resolved, stdlib_roots)
    return False


def _verify_new_import_origins(previous_modules, dependency_roots, stdlib_roots, deadline):
    verified_nonstdlib = set()
    for name in sorted(set(sys.modules) - set(previous_modules)):
        deadline.check()
        module = sys.modules.get(name)
        if module is None:
            continue
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            spec = getattr(module, "__spec__", None)
            require(getattr(spec, "origin", None) in ("built-in", "frozen")
                    or _virtual_module_has_verified_parent(
                        name, dependency_roots, stdlib_roots, deadline),
                    "sdk_unverified")
            continue
        resolved = _secure_regular_file(Path(module_file), "sdk_unverified")
        if _is_within(resolved, dependency_roots):
            verified_nonstdlib.add(resolved)
        else:
            require(_is_within(resolved, stdlib_roots), "sdk_unverified")
    require(verified_nonstdlib, "sdk_unverified")
    return verified_nonstdlib


def _verify_preimport_module_origins(stdlib_roots, deadline):
    """With -S, every preloaded file must be stdlib except this exact verifier."""
    verifier = Path(__file__).resolve(strict=True)
    for name, module in tuple(sys.modules.items()):
        deadline.check()
        if module is None:
            continue
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            spec = getattr(module, "__spec__", None)
            require(getattr(spec, "origin", None) in ("built-in", "frozen")
                    or _virtual_module_has_verified_parent(
                        name, frozenset(), stdlib_roots, deadline),
                    "sdk_unverified")
            continue
        resolved = Path(module_file).resolve(strict=True)
        if resolved == verifier and name in (__name__, "__main__"):
            continue
        resolved = _secure_regular_file(resolved, "sdk_unverified")
        require(_is_within(resolved, stdlib_roots), "sdk_unverified")


def load_verified_sdk_runtime(deadline):
    """Load SDK 1.9.44 only from the fixed, root-owned runtime installation."""
    global _VERIFIED_SDK_RUNTIME
    deadline.check()
    require(_VERIFIED_SDK_RUNTIME is None and all(
        not any(name == root or name.startswith(root + ".") for root in TRANSPORT_MODULE_ROOTS)
        for name in sys.modules
    ),
            "sdk_unverified")
    try:
        prefix = RUNTIME_PREFIX.resolve(strict=True)
        require(os.name == "posix" and sys.flags.no_site == 1
                and Path(sys.executable) == RUNTIME_PYTHON, "sdk_unverified")
        dependency_roots = _verified_dependency_roots(prefix, deadline)
        dependency_tree_files = sum(
            _verified_dependency_tree(root, deadline) for root in dependency_roots)
        stdlib_roots = _verified_runtime_import_paths(prefix, dependency_roots, deadline)
        _verify_preimport_module_origins(stdlib_roots, deadline)
        dependency_root = next(iter(dependency_roots))
        require(str(dependency_root) not in sys.path, "sdk_unverified")
        sys.path.append(str(dependency_root))
        importlib.invalidate_caches()
        previous_modules = frozenset(sys.modules)

        distribution = importlib_metadata.distribution("cos-python-sdk-v5")
        require(distribution.version == SDK_VERSION and distribution.files, "sdk_unverified")
        installed = []
        for entry in distribution.files:
            candidate = Path(distribution.locate_file(entry))
            require(candidate.is_file() or candidate.is_symlink(), "sdk_unverified")
            installed.append(_verified_distribution_file(candidate, prefix, deadline))
        required = {
            _verified_distribution_file(Path(distribution.locate_file(
                Path("qcloud_cos") / name)), prefix, deadline)
            for name in ("__init__.py", "cos_client.py", "cos_auth.py")
        }
        installed_set = set(installed)
        require(required.issubset(installed_set), "sdk_unverified")
        expected_package = _verified_distribution_file(Path(distribution.locate_file(
            Path("qcloud_cos") / "__init__.py")), prefix, deadline)
        require(_is_within(expected_package, dependency_roots), "sdk_unverified")
        package_origin = _verified_module_spec("qcloud_cos", dependency_roots, deadline)
        require(package_origin == expected_package, "sdk_unverified")

        transport_versions = {}
        transport_files = {}
        for name in TRANSPORT_DISTRIBUTIONS:
            version, files = _verified_transport_distribution(name, dependency_roots, deadline)
            transport_versions[name] = version
            transport_files[name] = files
            _verified_module_spec(name.replace("-", "_"), dependency_roots, deadline)

        package = importlib.import_module("qcloud_cos")
        auth_module = importlib.import_module("qcloud_cos.cos_auth")
        client_module = importlib.import_module("qcloud_cos.cos_client")
        requests = importlib.import_module("requests")
        urllib3 = importlib.import_module("urllib3")
        certifi = importlib.import_module("certifi")
        adapters_module = importlib.import_module("requests.adapters")
        HTTPAdapter = getattr(adapters_module, "HTTPAdapter", None)
        package_file = _verified_distribution_file(Path(package.__file__), prefix, deadline)
        auth_file = _verified_distribution_file(Path(auth_module.__file__), prefix, deadline)
        require(package_file == expected_package
                and auth_file == _verified_distribution_file(Path(distribution.locate_file(
                    Path("qcloud_cos") / "cos_auth.py")), prefix, deadline),
                "sdk_unverified")
        require(_verified_module_file(requests, dependency_roots, deadline)
                in transport_files["requests"]
                and _verified_module_file(urllib3, dependency_roots, deadline)
                in transport_files["urllib3"]
                and _verified_module_file(certifi, dependency_roots, deadline)
                in transport_files["certifi"]
                and _verified_module_file(adapters_module, dependency_roots, deadline)
                in transport_files["requests"]
                and isinstance(HTTPAdapter, type)
                and HTTPAdapter.__module__ == "requests.adapters"
                and getattr(adapters_module, "HTTPAdapter", None) is HTTPAdapter,
                "sdk_unverified")
        require(getattr(client_module, "requests", None) is requests
                and getattr(client_module, "Session", None) is requests.Session
                and getattr(client_module, "Request", None) is requests.Request
                and getattr(client_module, "ConnectionError", None) is requests.ConnectionError
                and getattr(client_module, "Timeout", None) is requests.Timeout
                and getattr(auth_module, "AuthBase", None)
                is importlib.import_module("requests.auth").AuthBase,
                "sdk_unverified")
        ca_bundle = _secure_regular_file(Path(certifi.where()), "sdk_unverified")
        require(_is_within(ca_bundle, dependency_roots)
                and ca_bundle in transport_files["certifi"]
                and callable(getattr(importlib.import_module("requests.certs"), "where", None))
                and Path(importlib.import_module("requests.certs").where()).resolve(strict=True) == ca_bundle,
                "sdk_unverified")
        for name, module in tuple(sys.modules.items()):
            if name == "qcloud_cos" or name.startswith("qcloud_cos."):
                module_file = getattr(module, "__file__", None)
                require(module_file is not None
                        and _verified_distribution_file(Path(module_file), prefix, deadline) in installed_set,
                        "sdk_unverified")
        verified_transport_files = _verify_new_import_origins(
            previous_modules, dependency_roots, stdlib_roots, deadline)
        _VERIFIED_SDK_RUNTIME = SimpleNamespace(
            CosConfig=package.CosConfig, CosS3Client=package.CosS3Client,
            CosS3Auth=auth_module.CosS3Auth, requests=requests, HTTPAdapter=HTTPAdapter,
            proof={"version": SDK_VERSION, "runtime_prefix": str(prefix),
                   "distribution_files_verified": len(installed_set), "root_owned": True,
                   "group_other_writable": False, "isolated_python": True,
                   "dependency_prefixes": sorted(str(item) for item in dependency_roots),
                   "dependency_tree_files_verified": dependency_tree_files,
                   "transport_versions": transport_versions,
                   "transport_files_verified": len(verified_transport_files), "pth_files": 0},
        )
    except VerificationError:
        raise
    except Exception:
        raise VerificationError("sdk_unverified") from None
    deadline.check()
    return _VERIFIED_SDK_RUNTIME


def sdk_runtime():
    require(_VERIFIED_SDK_RUNTIME is not None, "sdk_unverified")
    return _VERIFIED_SDK_RUNTIME


def verify_candidate(candidate_sha, deadline=None):
    deadline = deadline or AcceptanceDeadline()
    deadline.check()
    require(isinstance(candidate_sha, str) and re.fullmatch(r"[0-9a-f]{40}", candidate_sha),
            "candidate_unverified")
    require(all(not name.upper().startswith("GIT_") for name in os.environ),
            "candidate_unverified")
    tracked = [
        "scripts/verify_drama_cos_upload.py",
        "features/__init__.py",
        "features/drama_synthesis/__init__.py",
        "features/drama_synthesis/core.py",
        "features/drama_synthesis/local_checkpoint.py",
        "features/drama_synthesis/gpu_cache.py",
        "features/drama_synthesis/async_runtime.py",
        "features/drama_synthesis/cos_upload.py",
    ]
    git = verify_git_binary(deadline)
    try:
        _verify_git_version(git, deadline)

        # The command-line false value must be the only visible fsmonitor value.
        # This detects local, worktree and included config without ever invoking
        # a configured fsmonitor executable.
        configured = _git_text(_run_git(git, [
            "config", "--show-origin", "--show-scope", "--get-all", "core.fsmonitor",
        ], deadline))
        require(re.fullmatch(r"command\s+command line:\s+false", configured) is not None,
                "candidate_unverified")

        replacement_refs = _git_text(_run_git(
            git, ["for-each-ref", "--count=1", "--format=%(refname)", "refs/replace/"], deadline))
        require(replacement_refs == "", "candidate_unverified")

        object_format = _git_text(_run_git(
            git, ["rev-parse", "--show-object-format"], deadline))
        require(object_format == "sha1", "candidate_unverified")

        top_level = _git_text(_run_git(
            git, ["rev-parse", "--show-toplevel"], deadline))
        require(Path(top_level).resolve(strict=True) == ROOT.resolve(strict=True),
                "candidate_unverified")

        head_commit = _git_text(_run_git(
            git, ["rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"], deadline))
        candidate_commit = _git_text(_run_git(
            git, ["rev-parse", "--verify", "--end-of-options",
                  "{}^{{commit}}".format(candidate_sha)], deadline))
        require(re.fullmatch(r"[0-9a-f]{40}", head_commit) is not None
                and candidate_commit == head_commit == candidate_sha, "candidate_unverified")

        head_tree = _git_text(_run_git(
            git, ["rev-parse", "--verify", "--end-of-options", "HEAD^{tree}"], deadline))
        candidate_tree = _git_text(_run_git(
            git, ["rev-parse", "--verify", "--end-of-options",
                  "{}^{{tree}}".format(candidate_sha)], deadline))
        require(re.fullmatch(r"[0-9a-f]{40}", head_tree) is not None
                and candidate_tree == head_tree, "candidate_unverified")

        index_entries = _run_git(
            git, ["ls-files", "-v", "-z"], deadline,
            output_limit=LOCAL_TOOL_OUTPUT_MAX_BYTES)
        require(index_entries.returncode == 0 and index_entries.stderr == b""
                and isinstance(index_entries.stdout, bytes) and index_entries.stdout,
                "candidate_unverified")
        records = index_entries.stdout.split(b"\x00")
        require(records[-1] == b"" and all(record.startswith(b"H ") for record in records[:-1]),
                "candidate_unverified")

        dirty = _git_text(_run_git(git, [
            "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching",
            "--no-renames",
        ], deadline))
        require(dirty == "", "candidate_unverified")
        ignored = _git_text(_run_git(git, [
            "ls-files", "--others", "--ignored", "--exclude-standard",
        ], deadline))
        require(ignored == "", "candidate_unverified")

        for item in tracked:
            listed = _git_text(_run_git(
                git, ["ls-files", "--error-unmatch", "--", item], deadline))
            require(listed == item, "candidate_unverified")
            _verified_candidate_blob(git, candidate_sha, ROOT / Path(item), deadline)
    except VerificationError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        deadline.check()
        raise VerificationError("candidate_unverified") from None
    deadline.check()


def parse_credentials(text):
    """Select four literal dotenv values. No expansion, sourcing or env fallback."""
    require(isinstance(text, str) and len(text.encode("utf-8")) <= 65536 and "\x00" not in text,
            "credentials_invalid")
    selected = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        found = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=(.*)$", line)
        require(found is not None and found.group(1) in ENV_KEYS, "credentials_invalid")
        name, value = found.group(1), found.group(2).strip()
        require(name not in selected, "credentials_invalid")
        if value.startswith(("'", '"')):
            require(len(value) >= 2 and value[-1] == value[0], "credentials_invalid")
            value = value[1:-1]
        require(value and len(value) <= 2048 and not any(char.isspace() for char in value)
                and not any(char in value for char in "'\"`$\\#")
                and not any(ord(char) < 32 or ord(char) > 126 for char in value), "credentials_invalid")
        selected[name] = value
    require(set(selected) == ENV_KEYS, "credentials_invalid")
    require(re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}-[1-9][0-9]{4,19}", selected["COS_BUCKET"]),
            "credentials_invalid")
    require(re.fullmatch(r"[a-z]{2,5}-[a-z]+(?:-[0-9]+)?", selected["COS_REGION"]), "credentials_invalid")
    return selected


def load_credentials(path, deadline=None):
    deadline = deadline or AcceptanceDeadline()
    deadline.check()
    require(os.name == "posix", "posix_required")
    no_symlinks(path, "credential_file_unverified")
    try:
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) in {0o400, 0o600}
                    and info.st_uid == os.geteuid() and info.st_nlink == 1 and 0 < info.st_size <= 65536,
                    "credential_file_unverified")
            raw = handle.read(65537)
        deadline.check()
        value = parse_credentials(raw.decode("utf-8-sig"))
        deadline.check()
        return value
    except (OSError, UnicodeError):
        raise VerificationError("credential_file_unverified") from None


def deadline_file_fingerprint(path, deadline):
    require_candidate_modules()
    deadline.check()
    value = file_fingerprint(path)
    deadline.check()
    return value


def probe_source(path, deadline=None):
    deadline = deadline or AcceptanceDeadline()
    deadline.check()
    no_symlinks(path, "source_unverified")
    require(path.is_file() and path.suffix.lower() == ".mp4", "source_unverified")
    require(MIN_BYTES < path.stat().st_size <= MAX_BYTES, "source_size_outside_acceptance_limit")
    before = deadline_file_fingerprint(path, deadline)
    ffprobe, ffprobe_proof = verify_ffprobe_binary(deadline)
    try:
        result = run_bounded_process(
            [str(ffprobe), "-v", "error", "-protocol_whitelist", "file", "-show_entries",
             "format=format_name,duration,size:stream=codec_type,codec_name,width,height", "-of", "json", str(path)],
            deadline=deadline, timeout=60, output_limit=FFPROBE_OUTPUT_MAX_BYTES,
            cwd="/", env=clean_subprocess_environment(), code="ffprobe_failed",
        )
        deadline.check()
        require(result.returncode == 0 and len(result.stdout) <= FFPROBE_OUTPUT_MAX_BYTES
                and len(result.stderr) <= FFPROBE_OUTPUT_MAX_BYTES, "ffprobe_failed")
        probe = json.loads(result.stdout)
        media_format, streams = probe["format"], probe["streams"]
        duration = float(media_format["duration"])
        require("mp4" in media_format["format_name"].split(",") and math.isfinite(duration) and duration > 0
                and int(media_format["size"]) == before["size_bytes"]
                and isinstance(streams, list) and 0 < len(streams) <= 64, "ffprobe_failed")
        videos = [item for item in streams if item.get("codec_type") == "video"]
        require(videos and all(type(item.get("width")) is int and 0 < item["width"] <= 32768
                              and type(item.get("height")) is int and 0 < item["height"] <= 32768
                              and re.fullmatch(r"[a-zA-Z0-9_]{1,64}", str(item.get("codec_name", "")))
                              for item in videos), "ffprobe_failed")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        deadline.check()
        raise VerificationError("ffprobe_failed") from None
    require(deadline_file_fingerprint(path, deadline) == before, "source_changed")
    return before, {"binary": ffprobe_proof, "format": "mp4", "duration_seconds": duration,
                    "video_streams": videos,
                    "audio_streams": sum(item.get("codec_type") == "audio" for item in streams)}


def fresh_evidence_directory(path, deadline=None):
    deadline = deadline or AcceptanceDeadline()
    deadline.check()
    no_symlinks(path, "evidence_directory_not_fresh")
    require(not path.exists() and path.parent.is_dir(), "evidence_directory_not_fresh")
    try:
        path.mkdir(mode=0o700)
        if os.name == "posix":
            require(stat.S_IMODE(path.stat().st_mode) == 0o700, "evidence_directory_not_fresh")
        deadline.check()
    except OSError:
        raise VerificationError("evidence_directory_not_fresh") from None


class HttpAudit:
    """Counts actual requests without retaining URLs, signatures or headers."""

    def __init__(self, bucket, region, prefix):
        self.host = "{}.cos.{}.myqcloud.com".format(bucket, region)
        self.prefix = prefix
        self.key = prefix + "material.mp4"
        self.calls = Counter()
        self.statuses = Counter()

    def classify(self, method, url, kwargs):
        parsed = urlsplit(url)
        require(parsed.scheme == "https" and parsed.netloc == self.host and not parsed.query
                and not parsed.fragment and parsed.path in {"/", "/" + self.key}, "scope_violation")
        params = kwargs.get("params", {}) or {}
        require(isinstance(params, dict), "scope_violation")
        # SDK 1.9.44 formats string parameter values as bytes before requests.
        normalized = {}
        for name, value in params.items():
            require(isinstance(name, str), "scope_violation")
            try:
                if isinstance(value, bytes):
                    value = value.decode("ascii")
                elif type(value) is int:
                    value = str(value)
            except UnicodeError:
                raise VerificationError("scope_violation") from None
            require(isinstance(value, str) and len(value) <= 2048, "scope_violation")
            normalized[name] = value
        params = normalized
        method = method.upper()
        if parsed.path == "/":
            require(method == "GET", "scope_violation")
            if params == {"notification": "", "notify-type": "2"}:
                return "get_bucket_notification_v2"
            if params == {"versioning": ""}:
                return "get_bucket_versioning"
            if params == {
                    "prefix": self.prefix, "delimiter": "", "marker": "", "max-keys": "1",
                    "encoding-type": "url",
            }:
                return "list_objects"
            if params == {
                    "uploads": "", "prefix": self.prefix, "delimiter": "", "key-marker": "",
                    "upload-id-marker": "", "max-uploads": "1", "encoding-type": "url",
            }:
                return "list_multipart_uploads"
            raise VerificationError("scope_violation")
        if method == "HEAD":
            require(params == {}, "scope_violation")
            return "head_object"
        if method == "GET":
            if params == {"acl": ""}:
                return "get_object_acl"
            if params == {}:
                return "get_object"
            require(set(params) == {"uploadId", "part-number-marker", "max-parts", "encoding-type"}
                    and re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,2048}", params["uploadId"])
                    and re.fullmatch(r"[0-9]{1,5}", params["part-number-marker"])
                    and 0 <= int(params["part-number-marker"]) <= 10000
                    and params["max-parts"] == "1000" and params["encoding-type"] == "url",
                    "scope_violation")
            return "list_parts"
        if method == "POST":
            if params == {"uploads": ""}:
                return "create_multipart_upload"
            require(set(params) == {"uploadId"}
                    and re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,2048}", params["uploadId"]),
                    "scope_violation")
            return "complete_multipart_upload"
        require(method == "PUT" and set(params) == {"uploadId", "partNumber"}
                and re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,2048}", params["uploadId"])
                and re.fullmatch(r"[1-9][0-9]{0,4}", params["partNumber"])
                and 1 <= int(params["partNumber"]) <= 10000, "scope_violation")
        return "upload_part"


def build_real_client(credentials, prefix, deadline=None):
    deadline = deadline or AcceptanceDeadline()
    require(isinstance(deadline, AcceptanceDeadline), "arguments_invalid")
    runtime = sdk_runtime()
    audit = HttpAudit(credentials["COS_BUCKET"], credentials["COS_REGION"], prefix)

    class NoRetrySession(runtime.requests.Session):
        def request(self, method, url, **kwargs):
            deadline.check()
            require(kwargs.get("verify") is True and kwargs.get("allow_redirects") is False
                    and kwargs.get("proxies") is None and "cert" not in kwargs, "sdk_unverified")
            name = audit.classify(method, url, kwargs)
            audit.calls[name] += 1
            kwargs["allow_redirects"] = False
            kwargs["timeout"] = deadline.request_timeout(kwargs.get("timeout", 60))
            response = None
            try:
                response = super().request(method, url, **kwargs)
                status_code = getattr(response, "status_code", None)
                require(type(status_code) is int and 100 <= status_code <= 599
                        and not 300 <= status_code <= 399, "sdk_unverified")
                audit.statuses[name + ":" + str(status_code)] += 1
                deadline.check()
                return response
            except VerificationError as exc:
                close = getattr(response, "close", None)
                if callable(close):
                    retained, _failed = preserve_bounded_action(deadline, close, exc)
                    raise retained
                raise

    session = NoRetrySession()
    session.trust_env = False
    session.mount("https://", runtime.HTTPAdapter(max_retries=0))
    config = runtime.CosConfig(Region=credentials["COS_REGION"], SecretId=credentials["COS_SECRET_ID"],
                               SecretKey=credentials["COS_SECRET_KEY"], Scheme="https", Timeout=60,
                               KeepAlive=False, AllowRedirects=False, AutoSwitchDomainOnRetry=False, VerifySSL=True)
    return runtime.CosS3Client(config, retry=0, session=session), audit, session


def build_anonymous_head_gate(*, bucket, region, key, deadline=None):
    """Build a body-free, no-auth HEAD gate for the one derived acceptance key."""
    runtime = sdk_runtime()
    deadline = deadline or AcceptanceDeadline()
    require(isinstance(deadline, AcceptanceDeadline), "arguments_invalid")
    require(re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}-[1-9][0-9]{4,19}", bucket)
            and re.fullmatch(r"[a-z]{2,5}-[a-z]+(?:-[0-9]+)?", region)
            and re.fullmatch(
                r"drama-synthesis-acceptance/[0-9a-f]{40}/cos-[a-z0-9][a-z0-9-]{7,63}/material\.mp4",
                key,
            ), "scope_violation")
    url = "https://{}.cos.{}.myqcloud.com/{}".format(bucket, region, key)
    session = runtime.requests.Session()
    session.trust_env = False
    session.mount("https://", runtime.HTTPAdapter(max_retries=0))

    def check():
        deadline.check()
        require(session.trust_env is False and not session.proxies
                and getattr(session.get_adapter(url).max_retries, "total", None) == 0
                and session.auth is None and not session.cookies
                and not any(str(name).lower() == "authorization" for name in session.headers),
                "anonymous_access_not_private_or_unverified")
        response = None
        try:
            response = session.head(
                url, headers={"Accept-Encoding": "identity"}, timeout=deadline.request_timeout(10),
                allow_redirects=False, verify=True, stream=True,
            )
            deadline.check()
            status = getattr(response, "status_code", None)
            require(type(status) is int and 100 <= status <= 599
                    and getattr(response, "url", None) == url
                    and getattr(response, "history", None) == [],
                    "anonymous_access_not_private_or_unverified")
            return status
        except VerificationError:
            raise
        except Exception:
            raise VerificationError("anonymous_access_not_private_or_unverified") from None
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                active = sys.exc_info()[1]
                retained, _failed = preserve_bounded_action(deadline, close, active)
                if active is None and retained is not None:
                    raise retained

    return check, session


def verify_private_object_acl(response):
    """Require one owner-only FULL_CONTROL grant and retain no identity data."""
    require(isinstance(response, dict)
            and set(response) == {"Owner", "AccessControlList", "CannedACL"}
            and isinstance(response["Owner"], dict)
            and response["CannedACL"] == "private"
            and isinstance(response["AccessControlList"], dict)
            and set(response["AccessControlList"]) == {"Grant"},
            "object_acl_not_private_or_unverified")
    owner = response["Owner"]
    require(set(owner).issubset({"ID", "DisplayName"}) and "ID" in owner,
            "object_acl_not_private_or_unverified")
    owner_id = owner["ID"]
    require(isinstance(owner_id, str) and owner_id == owner_id.strip()
            and 0 < len(owner_id) <= 2048
            and all(32 < ord(char) < 127 for char in owner_id),
            "object_acl_not_private_or_unverified")
    if "DisplayName" in owner:
        display = owner["DisplayName"]
        require(isinstance(display, str) and len(display) <= 2048
                and all(ord(char) >= 32 and ord(char) != 127 for char in display),
                "object_acl_not_private_or_unverified")
    grants = response["AccessControlList"]["Grant"]
    if isinstance(grants, dict):
        grants = [grants]
    require(isinstance(grants, list) and len(grants) == 1,
            "object_acl_not_private_or_unverified")
    grant = grants[0]
    require(isinstance(grant, dict) and set(grant) == {"Grantee", "Permission"}
            and grant["Permission"] == "FULL_CONTROL"
            and isinstance(grant["Grantee"], dict), "object_acl_not_private_or_unverified")
    grantee = grant["Grantee"]
    require(set(grantee).issubset({"Type", "ID", "DisplayName"})
            and set(grantee) >= {"Type", "ID"}
            and grantee["Type"] in {"CanonicalUser", "RootAccount", "SubAccount"}
            and grantee["ID"] == owner_id,
            "object_acl_not_private_or_unverified")
    if "DisplayName" in grantee:
        display = grantee["DisplayName"]
        require(isinstance(display, str) and 0 < len(display) <= 2048
                and all(ord(char) >= 32 and ord(char) != 127 for char in display),
                "object_acl_not_private_or_unverified")
    return {"grant_count": 1, "public_grants": 0, "owner_only": True, "verified": True}


def verify_empty_notification_configuration(body):
    """Return only non-sensitive evidence for a strictly empty V2 configuration."""
    require(isinstance(body, bytes) and 0 < len(body) <= MAX_NOTIFICATION_BYTES,
            "notification_configuration_not_empty_or_unverified")
    upper = body.upper()
    require(re.search(br"<\s*!\s*(?:DOCTYPE|ENTITY)\b", upper) is None,
            "notification_configuration_not_empty_or_unverified")
    try:
        lexical = body.decode("utf-8-sig").strip()
        declaration = re.match(r"^<\?xml[ \t\r\n]+[^?<>]*\?>", lexical)
        if declaration is not None:
            lexical = lexical[declaration.end():].strip()
        require(re.fullmatch(
            r"<NotificationConfiguration(?:[ \t\r\n]*/[ \t\r\n]*>"
            r"|[ \t\r\n]*>[ \t\r\n]*</NotificationConfiguration[ \t\r\n]*>)",
            lexical,
        ) is not None, "notification_configuration_not_empty_or_unverified")
        parser = ElementTree.XMLParser(
            target=ElementTree.TreeBuilder(insert_comments=True, insert_pis=True)
        )
        root = ElementTree.fromstring(body, parser=parser)
    except (ElementTree.ParseError, LookupError, TypeError, ValueError):
        raise VerificationError("notification_configuration_not_empty_or_unverified") from None
    require(
        root.tag == "NotificationConfiguration"
        and root.attrib == {}
        and len(root) == 0
        and (root.text is None or root.text.strip() == "")
        and (root.tail is None or root.tail.strip() == ""),
        "notification_configuration_not_empty_or_unverified",
    )
    return {
        "configuration_sha256": hashlib.sha256(body).hexdigest(),
        "rule_count": 0,
        "verified": True,
    }


def get_bucket_notification_v2(client, *, bucket, deadline=None):
    """Issue the SDK 1.9.44 signed, read-only V2 notification request."""
    deadline = deadline or AcceptanceDeadline()
    require(isinstance(deadline, AcceptanceDeadline), "arguments_invalid")
    try:
        CosS3Auth = sdk_runtime().CosS3Auth
        config = client._conf
        region = config._region
        url = config.uri(bucket=bucket)
        session = client._session
        adapter_retries = session.get_adapter(url).max_retries
    except (AttributeError, TypeError):
        raise VerificationError("sdk_unverified") from None
    expected_url = "https://{}.cos.{}.myqcloud.com/".format(bucket, region)
    require(
        isinstance(region, str)
        and re.fullmatch(r"[a-z]{2,5}-[a-z]+(?:-[0-9]+)?", region)
        and url == expected_url
        and config._scheme == "https"
        and config._verify_ssl is True
        and config._allow_redirects is False
        and config._auto_switch_domain_on_retry is False
        and config._proxies is None
        and client._retry == 0
        and getattr(session, "trust_env", None) is False
        and getattr(adapter_retries, "total", None) == 0,
        "sdk_unverified",
    )
    params = {"notification": "", "notify-type": "2"}
    response = None
    try:
        response = client.send_request(
            method="GET", url=url, bucket=bucket,
            auth=CosS3Auth(config, params=params),
            headers={"Accept": "application/xml", "Accept-Encoding": "identity"},
            params=params, stream=True,
        )
        require(getattr(response, "status_code", None) == 200,
                "notification_configuration_not_empty_or_unverified")
        headers = {str(name).lower(): value for name, value in response.headers.items()}
        require(str(headers.get("content-encoding", "identity")).lower() == "identity",
                "notification_configuration_not_empty_or_unverified")
        content_length = headers.get("content-length")
        if content_length is not None:
            require(re.fullmatch(r"[0-9]+", str(content_length)) is not None
                    and 0 < int(content_length) <= MAX_NOTIFICATION_BYTES,
                    "notification_configuration_not_empty_or_unverified")
        raw = getattr(response, "raw", None)
        require(hasattr(raw, "read"), "notification_configuration_not_empty_or_unverified")
        deadline.check()
        body = raw.read(MAX_NOTIFICATION_BYTES + 1)
        deadline.check()
        require(isinstance(body, bytes) and len(body) <= MAX_NOTIFICATION_BYTES
                and (content_length is None or len(body) == int(content_length)),
                "notification_configuration_not_empty_or_unverified")
        return verify_empty_notification_configuration(body)
    except VerificationError:
        raise
    except Exception:
        if deadline.exceeded:
            raise VerificationError("acceptance_deadline_exceeded") from None
        raise VerificationError("notification_configuration_not_empty_or_unverified") from None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            active = sys.exc_info()[1]
            retained, _failed = preserve_bounded_action(deadline, close, active)
            if active is None and retained is not None:
                raise retained


class AuditedCos:
    """Expose only scoped APIs and discard exactly one response of each kind."""

    def __init__(self, client, *, bucket, prefix, checkpoint, evidence_dir, persist,
                 notification_reader, anonymous_head, deadline):
        self.client, self.bucket, self.prefix = client, bucket, prefix
        self.key = prefix + "material.mp4"
        self.checkpoint, self.evidence_dir, self.persist = checkpoint, evidence_dir, persist
        self.notification_reader = notification_reader
        self.anonymous_head = anonymous_head
        self.deadline = deadline
        self.calls, self.acknowledged, self.parts = Counter(), Counter(), Counter()
        self.upload_ids = set()
        self.injections = {"part_response_loss": 0, "complete_response_loss": 0}
        self.stage, self.listed_on_resume = "preflight", set()
        self.head_after_complete_loss = 0
        self.notification_checks = []
        self.notification_gate_failed = False
        self.anonymous_attempts, self.anonymous_statuses = 0, []
        self.anonymous_gate_failed = False
        self.object_acl_proof = None
        self.object_acl_gate_failed = False

    def snapshot(self):
        return {
            "stage": self.stage, "sdk_calls": dict(self.calls), "successful_sdk_responses": dict(self.acknowledged),
            "part_calls": {str(number): count for number, count in sorted(self.parts.items())},
            "upload_id_sha256": sorted(self.upload_ids), "injections": dict(self.injections),
            "listed_parts_on_resume": sorted(self.listed_on_resume),
            "head_responses_after_complete_loss": self.head_after_complete_loss,
            "notification_configuration": {
                "hash": [item["configuration_sha256"] for item in self.notification_checks],
                "count": len(self.notification_checks),
                "verified": len(self.notification_checks) == 2 and not self.notification_gate_failed,
            },
            "anonymous_head": {
                "count": self.anonymous_attempts,
                "status": list(self.anonymous_statuses),
                "verified": self.anonymous_attempts == 2
                and self.anonymous_statuses == [403, 403]
                and not self.anonymous_gate_failed,
            },
            "object_acl": dict(self.object_acl_proof) if self.object_acl_proof is not None else {
                "grant_count": None, "public_grants": None, "owner_only": False, "verified": False,
            },
        }

    def _call(self, name, kwargs):
        require_candidate_modules()
        self.deadline.check()
        require(name in API_NAMES and kwargs.get("Bucket") == self.bucket, "scope_violation")
        expected_keys = {
            "list_objects": {"Bucket", "Prefix", "MaxKeys"},
            "list_multipart_uploads": {"Bucket", "Prefix", "MaxUploads"},
            "get_bucket_versioning": {"Bucket"},
            "get_bucket_notification_v2": {"Bucket"},
            "head_object": {"Bucket", "Key"},
            "get_object_acl": {"Bucket", "Key"},
            "create_multipart_upload": {"Bucket", "Key", "ACL", "ContentType", "Metadata"},
            "list_parts": {"Bucket", "Key", "UploadId", "MaxParts", "PartNumberMarker"},
            "upload_part": {"Bucket", "Key", "UploadId", "PartNumber", "Body", "EnableMD5"},
            "complete_multipart_upload": {"Bucket", "Key", "UploadId", "MultipartUpload", "Metadata"},
            "get_object": {"Bucket", "Key"},
        }
        require(set(kwargs) == expected_keys[name], "scope_violation")
        if name == "list_objects":
            require(kwargs["Prefix"] == self.prefix and kwargs["MaxKeys"] == 1, "scope_violation")
        elif name == "list_multipart_uploads":
            require(kwargs["Prefix"] == self.prefix and kwargs["MaxUploads"] == 1, "scope_violation")
        elif name not in {"get_bucket_versioning", "get_bucket_notification_v2"}:
            require(kwargs["Key"] == self.key, "scope_violation")
        if name == "create_multipart_upload":
            metadata = kwargs["Metadata"]
            require(kwargs["ACL"] == "private" and kwargs["ContentType"] == "video/mp4"
                    and isinstance(metadata, dict)
                    and set(metadata) == {cos_upload.SHA_HEADER, cos_upload.SIZE_HEADER,
                                         cos_upload.BINDING_HEADER}
                    and re.fullmatch(r"[0-9a-f]{64}", str(metadata[cos_upload.SHA_HEADER]))
                    and re.fullmatch(r"[1-9][0-9]{0,9}", str(metadata[cos_upload.SIZE_HEADER]))
                    and re.fullmatch(r"[0-9a-f]{32}", str(metadata[cos_upload.BINDING_HEADER])),
                    "scope_violation")
        if name == "list_parts":
            require(kwargs["MaxParts"] == 1000 and type(kwargs["PartNumberMarker"]) is int
                    and 0 <= kwargs["PartNumberMarker"] <= 10000, "scope_violation")
        if name == "upload_part":
            require(type(kwargs["PartNumber"]) is int and 1 <= kwargs["PartNumber"] <= 10000
                    and isinstance(kwargs["Body"], bytes) and 0 < len(kwargs["Body"]) <= MIN_BYTES
                    and kwargs["EnableMD5"] is True, "scope_violation")
        if name == "complete_multipart_upload":
            require(kwargs["Metadata"] == {cos_upload.FORBID_OVERWRITE_HEADER: "true"}
                    and isinstance(kwargs["MultipartUpload"], dict)
                    and set(kwargs["MultipartUpload"]) == {"Part"}, "scope_violation")
        if "UploadId" in kwargs:
            require(isinstance(kwargs["UploadId"], str) and 0 < len(kwargs["UploadId"]) <= 2048,
                    "scope_violation")
            self.upload_ids.add(hashlib.sha256(kwargs["UploadId"].encode()).hexdigest())
        self.calls[name] += 1
        if name == "upload_part":
            self.parts[kwargs["PartNumber"]] += 1
        self.persist(self.snapshot())
        if name == "get_bucket_notification_v2":
            response = self.notification_reader(self.client, bucket=self.bucket, deadline=self.deadline)
        else:
            response = getattr(self.client, name)(**kwargs)
        self.deadline.check()
        self.acknowledged[name] += 1
        if name == "list_parts" and self.stage == "resume_after_part_loss":
            entries = response.get("Part", [])
            if isinstance(entries, dict):
                entries = [entries]
            self.listed_on_resume.update(int(item["PartNumber"]) for item in entries)
        if name == "head_object" and self.injections["complete_response_loss"]:
            self.head_after_complete_loss += 1
        self.persist(self.snapshot())
        return response

    def _notification_gate(self):
        try:
            proof = self._call("get_bucket_notification_v2", {"Bucket": self.bucket})
            require(isinstance(proof, dict)
                    and set(proof) == {"configuration_sha256", "rule_count", "verified"}
                    and re.fullmatch(r"[0-9a-f]{64}", str(proof["configuration_sha256"]))
                    and proof["rule_count"] == 0 and proof["verified"] is True,
                    "notification_configuration_not_empty_or_unverified")
            self.notification_checks.append(dict(proof))
            self.persist(self.snapshot())
        except Exception:
            self.notification_gate_failed = True
            self.persist(self.snapshot())
            raise VerificationError("notification_configuration_not_empty_or_unverified") from None

    def _anonymous_gate(self):
        self.deadline.check()
        self.anonymous_attempts += 1
        self.persist(self.snapshot())
        try:
            status = self.anonymous_head()
            self.deadline.check()
            require(type(status) is int and 100 <= status <= 599,
                    "anonymous_access_not_private_or_unverified")
            self.anonymous_statuses.append(status)
            self.persist(self.snapshot())
            require(status == 403, "anonymous_access_not_private_or_unverified")
        except Exception:
            self.anonymous_gate_failed = True
            self.persist(self.snapshot())
            raise VerificationError("anonymous_access_not_private_or_unverified") from None

    def _object_acl_gate(self):
        try:
            response = self._call("get_object_acl", {"Bucket": self.bucket, "Key": self.key})
            self.object_acl_proof = verify_private_object_acl(response)
            self.persist(self.snapshot())
        except Exception:
            self.object_acl_gate_failed = True
            self.persist(self.snapshot())
            raise VerificationError("object_acl_not_private_or_unverified") from None

    def __getattr__(self, name):
        if name not in API_NAMES:
            raise AttributeError("COS operation is not permitted by this verifier")
        return lambda **kwargs: self._call(name, kwargs)

    def _discard(self, kind, phase, upload_id):
        record = read_record(self.checkpoint)
        require(record is not None and record["phase"] == phase and record["upload_id"] == upload_id,
                "checkpoint_not_preserved")
        atomic_write_record(self.evidence_dir / ("checkpoint_after_" + kind + ".json"), record)
        self.injections[kind] += 1
        self.persist(self.snapshot())
        raise InjectedResponseLoss(kind)

    def upload_part(self, **kwargs):
        response = self._call("upload_part", kwargs)
        if not self.injections["part_response_loss"]:
            require(kwargs["PartNumber"] == 1 and self.parts[1] == 1
                    and isinstance(response, dict) and response.get("ETag"), "part_loss_not_verified")
            self._discard("part_response_loss", "uploading", kwargs["UploadId"])
        return response

    def create_multipart_upload(self, **kwargs):
        self._notification_gate()
        self._anonymous_gate()
        return self._call("create_multipart_upload", kwargs)

    def head_object(self, **kwargs):
        response = self._call("head_object", kwargs)
        if self.injections["complete_response_loss"] and self.object_acl_proof is None:
            self._anonymous_gate()
            self._object_acl_gate()
        return response

    def complete_multipart_upload(self, **kwargs):
        self._notification_gate()
        response = self._call("complete_multipart_upload", kwargs)
        if not self.injections["complete_response_loss"]:
            require(isinstance(response, dict) and response.get("ETag"), "completion_loss_not_verified")
            self._discard("complete_response_loss", "completing", kwargs["UploadId"])
        return response


def require_empty_prefix(client, bucket, prefix):
    for name, count_key, entries_key in (
        ("list_objects", "MaxKeys", "Contents"),
        ("list_multipart_uploads", "MaxUploads", "Upload"),
    ):
        response = getattr(client, name)(Bucket=bucket, Prefix=prefix, **{count_key: 1})
        require(isinstance(response, dict) and response.get("Prefix") == prefix
                and (response.get("IsTruncated") is False or response.get("IsTruncated") == "false")
                and response.get(entries_key, []) == [] and response.get("CommonPrefixes", []) == [],
                "prefix_not_empty_or_unverified")


def download_and_verify(client, *, bucket, key, artifact, result, record, destination, deadline):
    response = client.get_object(Bucket=bucket, Key=key)
    require(isinstance(response, dict), "download_not_verified")
    body = response.get("Body")
    stream = body.get_raw_stream() if hasattr(body, "get_raw_stream") else body
    try:
        headers = {str(name).lower(): value for name, value in response.items() if name != "Body"}
        require(headers.get("content-length") == str(artifact["size_bytes"])
                and "content-range" not in headers and headers.get("etag") == result["etag"]
                and headers.get(cos_upload.SHA_HEADER) == artifact["sha256"]
                and headers.get(cos_upload.SIZE_HEADER) == str(artifact["size_bytes"])
                and headers.get(cos_upload.BINDING_HEADER) == record["binding"]
                and hasattr(stream, "read"), "download_not_verified")
        partial = destination.with_suffix(".mp4.part")
        require(not destination.exists() and not destination.is_symlink(), "download_not_verified")
        descriptor = os.open(str(partial), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        digest, size = hashlib.sha256(), 0
        with os.fdopen(descriptor, "wb") as handle:
            while True:
                deadline.check()
                chunk = stream.read(min(MIB, artifact["size_bytes"] - size + 1))
                deadline.check()
                if chunk == b"":
                    break
                require(isinstance(chunk, bytes) and size + len(chunk) <= artifact["size_bytes"],
                        "download_not_verified")
                handle.write(chunk)
                size += len(chunk)
                digest.update(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        downloaded = {"sha256": digest.hexdigest(), "size_bytes": size}
        deadline.check()
        require(downloaded == artifact, "download_not_verified")
        os.rename(str(partial), str(destination))
        return downloaded
    finally:
        active = sys.exc_info()[1]
        for value in (body, stream):
            close = getattr(value, "close", None)
            if callable(close):
                active, _failed = preserve_bounded_action(deadline, close, active)
        if sys.exc_info()[1] is None and active is not None:
            raise active


def verify_upload(client, *, bucket, prefix, source, evidence_dir, artifact, persist,
                  notification_reader=get_bucket_notification_v2, anonymous_head, deadline=None):
    """Run the fixed sequence. A fake client may exercise it in unit tests only."""
    require_candidate_modules()
    require(MIN_BYTES < artifact["size_bytes"] <= MAX_BYTES
            and cos_upload.DEFAULT_PART_SIZE == MIN_BYTES, "source_size_outside_acceptance_limit")
    require(re.fullmatch(r"drama-synthesis-acceptance/[0-9a-f]{40}/cos-[a-z0-9][a-z0-9-]{7,63}/", prefix),
            "scope_violation")
    deadline = deadline or AcceptanceDeadline()
    require(isinstance(deadline, AcceptanceDeadline), "arguments_invalid")
    checkpoint = evidence_dir / "upload-checkpoint.json"
    require(not checkpoint.exists() and not checkpoint.is_symlink(), "checkpoint_not_preserved")
    audited = AuditedCos(client, bucket=bucket, prefix=prefix, checkpoint=checkpoint,
                         evidence_dir=evidence_dir, persist=persist,
                         notification_reader=notification_reader, anonymous_head=anonymous_head,
                         deadline=deadline)
    require_empty_prefix(audited, bucket, prefix)
    arguments = dict(bucket=bucket, key=audited.key, path=source, checkpoint_path=checkpoint,
                     content_type="video/mp4", acl="private")
    audited.stage = "first_part_response_loss"
    try:
        cos_upload.resume_upload(audited, **arguments)
    except DramaSynthesisError as exc:
        if deadline.exceeded:
            raise VerificationError("acceptance_deadline_exceeded") from None
        if audited.notification_gate_failed:
            raise VerificationError("notification_configuration_not_empty_or_unverified") from None
        if audited.anonymous_gate_failed:
            raise VerificationError("anonymous_access_not_private_or_unverified") from None
        if audited.object_acl_gate_failed:
            raise VerificationError("object_acl_not_private_or_unverified") from None
        require(exc.code == "drama_upload_failed" and audited.injections["part_response_loss"] == 1,
                "part_loss_not_verified")
    else:
        raise VerificationError("part_loss_not_verified")
    first = read_record(checkpoint)
    require(first is not None and first["phase"] == "uploading" and first["upload_id"]
            and first["part_size"] == MIN_BYTES and first["artifact"] == artifact
            and checkpoint.read_bytes() == (evidence_dir / "checkpoint_after_part_response_loss.json").read_bytes(),
            "checkpoint_not_preserved")

    audited.stage = "resume_after_part_loss"
    try:
        result = cos_upload.resume_upload(audited, **arguments)
    except DramaSynthesisError:
        if deadline.exceeded:
            raise VerificationError("acceptance_deadline_exceeded") from None
        if audited.notification_gate_failed:
            raise VerificationError("notification_configuration_not_empty_or_unverified") from None
        if audited.anonymous_gate_failed:
            raise VerificationError("anonymous_access_not_private_or_unverified") from None
        if audited.object_acl_gate_failed:
            raise VerificationError("object_acl_not_private_or_unverified") from None
        raise
    completed = read_record(checkpoint)
    require(completed is not None and completed["phase"] == "completed" and completed["result"] == result
            and completed["upload_id"] == first["upload_id"] and completed["binding"] == first["binding"]
            and completed["artifact"] == artifact, "checkpoint_not_preserved")
    require(audited.injections == {"part_response_loss": 1, "complete_response_loss": 1}
            and audited.head_after_complete_loss >= 1 and audited.listed_on_resume == {1}
            and len(audited.notification_checks) == 2
            and audited.anonymous_attempts == 2 and audited.anonymous_statuses == [403, 403]
            and audited.object_acl_proof is not None,
            "completion_loss_not_verified")
    part_count = math.ceil(artifact["size_bytes"] / MIN_BYTES)
    require(audited.calls["create_multipart_upload"] == 1 and audited.calls["complete_multipart_upload"] == 1
            and audited.calls["upload_part"] == part_count and len(audited.upload_ids) == 1
            and audited.parts == Counter({number: 1 for number in range(1, part_count + 1)})
            and audited.calls["get_object_acl"] == 1,
            "api_counts_not_verified")

    before = checkpoint.read_bytes(), checkpoint.stat().st_mtime_ns
    writes_before = {name: audited.calls[name] for name in WRITE_NAMES}
    audited.stage = "completed_replay"
    require(cos_upload.resume_upload(audited, **arguments) == result, "replay_created_work")
    require({name: audited.calls[name] for name in WRITE_NAMES} == writes_before
            and (checkpoint.read_bytes(), checkpoint.stat().st_mtime_ns) == before, "replay_created_work")
    audited.stage = "full_download_sha256"
    downloaded = download_and_verify(audited, bucket=bucket, key=audited.key, artifact=artifact,
                                     result=result, record=completed, destination=evidence_dir / "downloaded.mp4",
                                     deadline=deadline)
    require(deadline_file_fingerprint(source, deadline) == artifact, "source_changed")
    deadline.check()
    audited.stage = "verified"
    persist(audited.snapshot())
    return {"result": result, "downloaded": downloaded, "source_unchanged": True,
            "completed_replay_write_delta": {name: 0 for name in WRITE_NAMES},
            "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
            "audit": audited.snapshot()}


def parser():
    value = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    value.add_argument("--apply", action="store_true", help="perform the scoped real COS acceptance (POSIX only)")
    value.add_argument("--candidate-sha", help="exact 40-character GitHub candidate commit, equal to clean local HEAD")
    value.add_argument("--run-id", help="fresh ID: cos- followed by 8..64 lowercase letters, digits or hyphens")
    value.add_argument("--source", help="absolute path to a dedicated non-sensitive MP4, >16 MiB and <=256 MiB")
    value.add_argument("--env-file", help="absolute path to a dedicated, current-user-owned 0600/0400 COS env copy")
    value.add_argument("--evidence-dir", help="new absolute private directory; its parent must already exist")
    return value


def plan(args):
    prefix = None
    if args.candidate_sha is not None or args.run_id is not None:
        prefix = acceptance_prefix(args.candidate_sha, args.run_id)
    return {
        "status": "plan_only", "apply_required": True, "credentials_read": False, "network_requests": 0,
        "deadline_seconds": ACCEPTANCE_DEADLINE_SECONDS,
        "cleanup_deadline_seconds": CLEANUP_DEADLINE_SECONDS,
        "required_outer_runtime_max_seconds": OUTER_RUNTIME_MAX_SECONDS,
        "required_invocation": "/data/drama-synthesis-gpu/runtime/bin/python -I -B -S",
        "prefix": prefix or "drama-synthesis-acceptance/<candidate-sha>/<cos-run-id>/",
        "source_limit": "dedicated non-sensitive MP4: >16 MiB and <=256 MiB; fixed verified HK ffprobe only",
        "sequence": ["verify clean candidate and private local inputs", "verify empty object and multipart prefix",
                     "verify empty V2 bucket notification configuration immediately before Create and Complete",
                     "require unauthenticated HEAD 403 before Create and after authenticated completion HEAD",
                     "read back object ACL and require one owner-only FULL_CONTROL grant",
                     "discard first successful Part response once", "resume same UploadId using ListParts",
                     "discard successful Complete response once; verify HEAD", "replay completed checkpoint with zero new writes",
                     "download entire private object and compare SHA-256"],
        "never": ["render", "business API", "change bucket notifications or call ACL mutation APIs",
                  "create-response-loss injection",
                  "delete or abort", "overwrite existing object", "read production COS_PREFIX or ambient credentials"],
    }


def run_bounded_action(deadline, action):
    """Run one persistence/close action without letting a later check mask it."""
    error = None
    result = None
    try:
        deadline.prepare_bounded_action()
        result = action()
    except BaseException as exc:
        error = exc
    try:
        deadline.finish_bounded_action()
    except BaseException as exc:
        if error is None:
            error = exc
    if error is not None:
        raise error
    return result


def preserve_bounded_action(deadline, action, primary_error):
    """Return (first error, action_failed), preserving an existing safety error."""
    if deadline.exceeded and deadline._cleanup_started is None:
        try:
            deadline.begin_cleanup()
        except BaseException as exc:
            if primary_error is None:
                primary_error = exc
    try:
        run_bounded_action(deadline, action)
        return primary_error, False
    except BaseException as exc:
        if primary_error is None:
            primary_error = exc
        if deadline.exceeded and deadline._cleanup_started is None:
            try:
                deadline.begin_cleanup()
            except BaseException:
                pass
        return primary_error, True


def apply(args):
    deadline = AcceptanceDeadline()
    started_at = datetime.now(timezone.utc).isoformat()
    deadline.arm()
    primary_error = None
    result = None
    try:
        try:
            verify_isolated_runtime()
            require(_VERIFIED_SDK_RUNTIME is None and cos_upload is None and DramaSynthesisError is None,
                    "runtime_unverified")
            result = _apply_with_deadline(args, deadline, started_at)
        except BaseException as exc:
            primary_error = exc
        try:
            deadline.disarm()
        except BaseException as exc:
            if primary_error is None:
                primary_error = exc
    except BaseException as exc:
        if primary_error is None:
            primary_error = exc
    if primary_error is not None:
        raise primary_error
    return result


def _apply_with_deadline(args, deadline, started_at):
    deadline.check()
    prefix = acceptance_prefix(args.candidate_sha, args.run_id)
    source = absolute_path(args.source, "arguments_invalid")
    env_file = absolute_path(args.env_file, "arguments_invalid")
    evidence = absolute_path(args.evidence_dir, "arguments_invalid")
    deadline.check()
    verify_candidate(args.candidate_sha, deadline=deadline)
    load_verified_sdk_runtime(deadline)
    load_verified_candidate_modules(args.candidate_sha, deadline)
    verify_candidate(args.candidate_sha, deadline=deadline)
    artifact, probe = probe_source(source, deadline=deadline)
    credentials = load_credentials(env_file, deadline=deadline)
    # SDK logging may include signed request details. This standalone process
    # records only the explicitly selected evidence below, never SDK messages.
    logging.disable(logging.CRITICAL)
    client, http, session = build_real_client(credentials, prefix, deadline=deadline)
    anonymous_session = None
    report = None
    verified = None
    primary_error = None
    try:
        anonymous_head, anonymous_session = build_anonymous_head_gate(
            bucket=credentials["COS_BUCKET"], region=credentials["COS_REGION"], key=prefix + "material.mp4",
            deadline=deadline,
        )
        fresh_evidence_directory(evidence, deadline=deadline)
        deadline.check()
        free_space = shutil.disk_usage(evidence).free
        deadline.check()
        require(free_space >= artifact["size_bytes"] + 4 * MIB, "insufficient_disk_space")
        report = {
            "version": 1, "status": "running", "candidate_sha": args.candidate_sha, "run_id": args.run_id,
            "started_at": started_at, "prefix": prefix,
            "source": {"path": str(source), **artifact}, "ffprobe": probe,
            "sdk_version": SDK_VERSION, "sdk_retry": 0, "transport_retries": 0, "requested_acl": "private",
            "sdk_runtime": dict(sdk_runtime().proof),
            "deadline_seconds": ACCEPTANCE_DEADLINE_SECONDS,
            "cleanup_deadline_seconds": CLEANUP_DEADLINE_SECONDS, "elapsed_seconds": 0.0,
            "required_outer_runtime_max_seconds": OUTER_RUNTIME_MAX_SECONDS,
            "fault_model": "discard a successful real SDK response at the client boundary",
            "not_tested": ["creating unknown", "physical network outage", "rendering", "business API",
                           "notification delivery", "four-hour execution", "90-minute media acceptance"],
        }

        def write_report(audit):
            report["audit"] = audit
            report["http_calls"] = dict(http.calls)
            report["http_statuses"] = dict(http.statuses)
            report["elapsed_seconds"] = round(deadline.elapsed(), 3)
            atomic_write_record(evidence / "report.json", report)

        def persist(audit):
            deadline.check()
            write_report(audit)
            deadline.check()

        persist({})
        verified = verify_upload(client, bucket=credentials["COS_BUCKET"], prefix=prefix, source=source,
                                 evidence_dir=evidence, artifact=artifact, persist=persist,
                                 anonymous_head=anonymous_head, deadline=deadline)
        deadline.check()
        require(dict(http.calls) == verified["audit"]["sdk_calls"], "api_counts_not_verified")
        deadline.check()
    except BaseException as exc:
        primary_error = exc

    try:
        for close in (
                (anonymous_session.close if anonymous_session is not None else None), session.close):
            if close is not None:
                primary_error, _failed = preserve_bounded_action(deadline, close, primary_error)

        if report is not None:
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            if primary_error is None:
                report.update(verified, status="passed")
                report.pop("code", None)
            else:
                report.update(status="not_passed", code=safe_error(primary_error))
            audit = report.get("audit", {})
            primary_error, persist_failed = preserve_bounded_action(
                deadline, lambda: write_report(audit), primary_error)
            if persist_failed:
                report.update(status="not_passed", code=safe_error(primary_error))
                primary_error, _retry_failed = preserve_bounded_action(
                    deadline, lambda: write_report(audit), primary_error)
    except BaseException as exc:
        if primary_error is None:
            primary_error = exc
        if report is not None and callable(locals().get("write_report")):
            def retain_failure_report():
                report.update(status="not_passed", code=safe_error(primary_error))
                write_report(report.get("audit", {}))

            primary_error, _failed = preserve_bounded_action(
                deadline, retain_failure_report, primary_error)

    if primary_error is not None:
        raise primary_error
    return {"status": "passed", "evidence_dir": str(evidence), "prefix": prefix,
            "sha256": artifact["sha256"], "size_bytes": artifact["size_bytes"],
            "injections": report["audit"]["injections"], "http_calls": dict(http.calls)}


def safe_error(exc):
    if isinstance(exc, VerificationError):
        return exc.code
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted"
    # Do not print raw SDK, ffprobe, OS or server exceptions, including URLs.
    return "verification_failed"


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        output = apply(args) if args.apply else plan(args)
    except (Exception, KeyboardInterrupt) as exc:
        output = {"status": "not_passed", "code": safe_error(exc), "cos_cleanup_performed": False,
                  "instruction": "保留证据目录、检查点和COS对象/分片；不要删除状态或换前缀自动重试。"}
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
