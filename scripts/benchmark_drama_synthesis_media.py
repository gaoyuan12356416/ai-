#!/usr/bin/env python3
"""Explicit local media benchmarks; never submit production jobs or upload COS.

Render each fixed source/recipe into a NEW absolute output directory. The
operator supplies CPUQuota through an outer systemd-run scope; this script does
not alter service configuration. Download probes read an operator-provided URL
file, discard sampled bodies and cap application reads at 256 MiB per run.

The reviewed media launcher may call benchmark_render() with its already-held
fixed lock fd. The ordinary CLI never accepts or supplies that descriptor and
therefore does not claim the launcher's 16 GiB cgroup protection.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.drama_synthesis import gpu
from features.drama_synthesis.local_checkpoint import atomic_write_record


MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
MEDIA_ACCEPTANCE_LOCK_PATH = Path(
    "/data/drama-synthesis-gpu/acceptance/control/media-benchmark.lock"
)
# Fixed acceptance limits, never CLI/environment overrides. These sampled
# checks supplement the outer cgroup CPU/memory/memsw/pids hard limits.
MIN_HOST_MEM_AVAILABLE_BYTES = 8 * 1024 ** 3
MIN_LAUNCHER_START_MEM_AVAILABLE_BYTES = 24 * 1024 ** 3
STOP_RENDER_RSS_BYTES = 14 * 1024 ** 3
MAX_RENDER_THREADS = 120
RENDER_GLOBAL_CAP_SECONDS = 24 * 60 * 60


class BenchmarkGuardError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def validate_inherited_media_lock_fd(lock_fd):
    """Validate the launcher's fixed flock without accepting a path override."""
    if sys.platform != "linux" or type(lock_fd) is not int or lock_fd < 3:
        raise BenchmarkGuardError("benchmark_media_lock_invalid")
    try:
        import fcntl
        descriptor = os.fstat(lock_fd)
        control = os.stat(MEDIA_ACCEPTANCE_LOCK_PATH.parent, follow_symlinks=False)
        path_stat = os.stat(MEDIA_ACCEPTANCE_LOCK_PATH, follow_symlinks=False)
        target_stat = os.stat(MEDIA_ACCEPTANCE_LOCK_PATH)
        link = os.readlink("/proc/self/fd/%d" % lock_fd)
        if (not stat.S_ISDIR(control.st_mode) or control.st_uid != 0 or
                stat.S_IMODE(control.st_mode) & 0o022 != 0 or
                not os.get_inheritable(lock_fd) or not stat.S_ISREG(descriptor.st_mode) or
                not stat.S_ISREG(path_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode) or
                descriptor.st_nlink != 1 or path_stat.st_nlink != 1 or
                (descriptor.st_dev, descriptor.st_ino) != (path_stat.st_dev, path_stat.st_ino) or
                (descriptor.st_dev, descriptor.st_ino) != (target_stat.st_dev, target_stat.st_ino) or
                link != str(MEDIA_ACCEPTANCE_LOCK_PATH)):
            raise BenchmarkGuardError("benchmark_media_lock_invalid")
        independent = os.open(
            MEDIA_ACCEPTANCE_LOCK_PATH,
            os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            try:
                fcntl.flock(independent, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(independent, fcntl.LOCK_UN)
                raise BenchmarkGuardError("benchmark_media_lock_not_held")
        finally:
            os.close(independent)
    except BenchmarkGuardError:
        raise
    except (ImportError, OSError, OverflowError, ValueError):
        raise BenchmarkGuardError("benchmark_media_lock_invalid") from None
    return descriptor.st_dev, descriptor.st_ino


def verify_child_media_lock_fd(pid, lock_fd, expected_identity):
    """Immediately prove only this newly spawned renderer inherited the lock fd."""
    if (type(pid) is not int or pid <= 1 or type(lock_fd) is not int or lock_fd < 3 or
            not isinstance(expected_identity, tuple) or len(expected_identity) != 2):
        raise BenchmarkGuardError("benchmark_media_lock_inheritance_failed")
    try:
        child = os.stat("/proc/%d/fd/%d" % (pid, lock_fd))
    except OSError:
        raise BenchmarkGuardError("benchmark_media_lock_inheritance_failed") from None
    if (not stat.S_ISREG(child.st_mode) or
            (child.st_dev, child.st_ino) != expected_identity):
        raise BenchmarkGuardError("benchmark_media_lock_inheritance_failed")


def launch_renderer_process(command, *, inherited_lock_fd=None, popen=None, **kwargs):
    """Launch one renderer; optional fd is an internal, prevalidated protocol."""
    popen = popen or subprocess.Popen
    expected_identity = None
    if inherited_lock_fd is not None:
        expected_identity = validate_inherited_media_lock_fd(inherited_lock_fd)
        if "pass_fds" in kwargs or kwargs.get("close_fds") is False:
            raise BenchmarkGuardError("benchmark_media_lock_invalid")
        kwargs["pass_fds"] = (inherited_lock_fd,)
    proc = popen(command, **kwargs)
    if expected_identity is None:
        return proc
    try:
        verify_child_media_lock_fd(proc.pid, inherited_lock_fd, expected_identity)
    except BaseException:
        cleanup_failed = False
        try:
            if proc.poll() is None:
                proc.kill()
        except BaseException:
            cleanup_failed = True
        try:
            proc.wait(timeout=30)
        except BaseException:
            cleanup_failed = True
        try:
            if proc.poll() is None:
                cleanup_failed = True
        except BaseException:
            cleanup_failed = True
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except BaseException:
            cleanup_failed = True
        if cleanup_failed:
            raise BenchmarkGuardError("benchmark_renderer_cleanup_failed") from None
        raise
    return proc


def host_memory_sample(proc_meminfo="/proc/meminfo"):
    """Read actual host availability; never estimate it from free/cache bytes."""
    try:
        with Path(proc_meminfo).open(encoding="ascii") as stream:
            text = stream.read(65537)
    except (OSError, UnicodeError):
        raise BenchmarkGuardError("benchmark_host_memory_unavailable") from None
    values = {}
    for line in text.splitlines():
        key = line.partition(":")[0]
        if key in {"MemAvailable", "MemTotal"}:
            match = re.fullmatch(r"(MemAvailable|MemTotal):\s+([0-9]+) kB\s*", line)
            if key in values or match is None:
                raise BenchmarkGuardError("benchmark_host_memory_invalid")
            values[key] = int(match[2]) * 1024
    if (len(text) > 65536 or set(values) != {"MemAvailable", "MemTotal"} or
            not 0 <= values["MemAvailable"] <= values["MemTotal"] or values["MemTotal"] <= 0):
        raise BenchmarkGuardError("benchmark_host_memory_invalid")
    return {"mem_available_bytes": values["MemAvailable"], "mem_total_bytes": values["MemTotal"]}


def stable_source_fingerprint(path):
    """Hash one local source while binding the digest to its regular-file inode."""
    path = Path(path)
    try:
        direct = os.lstat(path)
        target = os.stat(path)
        if (stat.S_ISLNK(direct.st_mode) or not stat.S_ISREG(direct.st_mode) or
                not stat.S_ISREG(target.st_mode) or
                (direct.st_dev, direct.st_ino) != (target.st_dev, target.st_ino) or
                direct.st_nlink != target.st_nlink or target.st_nlink < 1 or
                target.st_size <= 0):
            raise BenchmarkGuardError("benchmark_source_fingerprint_failed")
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) |
            getattr(os, "O_NOFOLLOW", 0)
        )
        digest, size = hashlib.sha256(), 0
        with os.fdopen(descriptor, "rb") as stream:
            identity = (target.st_dev, target.st_ino, target.st_size,
                        target.st_mtime_ns, target.st_nlink)
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size,
                    opened.st_mtime_ns, opened.st_nlink) != identity:
                raise BenchmarkGuardError("benchmark_source_fingerprint_failed")
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
            final = os.fstat(stream.fileno())
            if (size != target.st_size or
                    (final.st_dev, final.st_ino, final.st_size,
                     final.st_mtime_ns, final.st_nlink) != identity):
                raise BenchmarkGuardError("benchmark_source_fingerprint_failed")
    except BenchmarkGuardError:
        raise
    except (OSError, ValueError):
        raise BenchmarkGuardError("benchmark_source_fingerprint_failed") from None
    return {
        "sha256": digest.hexdigest(), "size_bytes": size,
        "device": target.st_dev, "inode": target.st_ino,
        "mtime_ns": target.st_mtime_ns, "nlink": target.st_nlink,
    }


def fresh_directory(path):
    path = Path(path)
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise ValueError("output_directory_must_be_new_and_absolute")
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    return path


def check_sample_duration(kind, duration):
    if not math.isfinite(duration) or not (
        0.5 <= duration <= 300 if kind == "short" else 5400 <= duration <= 7200
    ):
        raise ValueError("source_duration_outside_short_or_90_minute_policy")


def process_sample(pid):
    root = Path("/proc") / str(pid)
    try:
        status = dict(line.split(":", 1) for line in (root / "status").read_text().splitlines() if ":" in line)
        stat = (root / "stat").read_text()
        fields = stat[stat.rfind(")") + 2:].split()
        return {"rss_bytes": int(status["VmRSS"].split()[0]) * 1024,
                "threads": int(status["Threads"].strip()),
                "cpu_seconds": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")}
    except (OSError, KeyError, IndexError, ValueError):
        return None


def cgroup_limits(*, proc_cgroup="/proc/self/cgroup", proc_mountinfo="/proc/self/mountinfo",
                  filesystem_root=None):
    """Read current cgroup limits, not ancestor/effective limits; never write.

    Proc paths and an optional filesystem root are injectable for offline tests.
    Missing/invalid limits stay explicitly unverified, including absent v1 memsw.
    """
    result = {"host_cpu_count": os.cpu_count(), "cgroup_version": None,
              "limit_read_status": "unavailable", "cpu_quota_read": False,
              "ancestor_limits_checked": False, "controllers": {}, "read_errors": {}}

    def absolute_path(value):
        value = re.sub(r"\\(040|011|012|134)", lambda match: chr(int(match[1], 8)), value)
        if not value.startswith("/") or ".." in value.split("/") or "\x00" in value:
            raise ValueError("invalid_cgroup_path")
        return PurePosixPath(value)

    def file_path(value):
        if filesystem_root is None:
            return Path(str(value))
        root = Path(filesystem_root).resolve()
        path = root.joinpath(*value.parts[1:]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("invalid_cgroup_path")
        return path

    proc_text = {}
    for name, path in (("membership", proc_cgroup), ("mountinfo", proc_mountinfo)):
        try:
            proc_text[name] = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            result["read_errors"][name] = "unreadable"
    if len(proc_text) != 2:
        return result

    memberships, mounts = {}, []
    for line in proc_text["membership"].splitlines():
        try:
            hierarchy, controllers, path = line.split(":", 2)
            path = absolute_path(path)
            if hierarchy == "0" and not controllers:
                memberships["unified"] = path
            for controller in controllers.split(","):
                if controller in ("cpu", "memory", "pids"):
                    memberships[controller] = path
        except ValueError:
            result["read_errors"]["membership"] = "invalid"
    for line in proc_text["mountinfo"].splitlines():
        try:
            left, right = line.split(" - ", 1)
            fields, details = left.split(), right.split()
            if details[0] in ("cgroup", "cgroup2"):
                mounts.append((details[0], absolute_path(fields[3]),
                               absolute_path(fields[4]), set(details[2].split(","))))
        except (ValueError, IndexError):
            result["read_errors"]["mountinfo"] = "invalid"

    specifications = {
        (1, "cpu"): {"cpu.cfs_quota_us": r"(?:-1|[1-9][0-9]*)", "cpu.cfs_period_us": r"[1-9][0-9]*"},
        (1, "memory"): {"memory.limit_in_bytes": r"[0-9]+", "memory.memsw.limit_in_bytes": r"[0-9]+"},
        (1, "pids"): {"pids.max": r"(?:max|[0-9]+)"},
        (2, "cpu"): {"cpu.max": r"(?:max|[1-9][0-9]*) [1-9][0-9]*"},
        (2, "memory"): {"memory.max": r"(?:max|[0-9]+)", "memory.swap.max": r"(?:max|[0-9]+)"},
        (2, "pids"): {"pids.max": r"(?:max|[0-9]+)"},
    }
    versions, count = set(), 0
    for controller in ("cpu", "memory", "pids"):
        version = 1 if controller in memberships else 2
        membership = memberships.get(controller if version == 1 else "unified")
        if membership is None:
            result["read_errors"][controller] = "controller_unavailable"
            continue
        versions.add(version)
        candidates = []
        for kind, mount_root, mount_point, options in mounts:
            if kind != ("cgroup" if version == 1 else "cgroup2") or (version == 1 and controller not in options):
                continue
            try:
                relative = membership.relative_to(mount_root)
            except ValueError:
                continue
            candidates.append((len(mount_root.parts), mount_point / relative))
        if not candidates:
            result["read_errors"][controller] = "matching_mount_unavailable"
            continue
        directory = max(candidates, key=lambda item: item[0])[1]
        result["controllers"][controller] = {"version": version, "directory": str(directory)}
        for name, pattern in specifications[(version, controller)].items():
            try:
                with file_path(directory / name).open(encoding="ascii") as stream:
                    raw = stream.read(257)
                value = raw.strip()
                if len(raw) > 256 or not re.fullmatch(pattern, value):
                    raise ValueError("invalid_limit")
                result[name] = value
                count += 1
            except (OSError, UnicodeError):
                result["read_errors"][name] = "unreadable"
            except ValueError:
                result["read_errors"][name] = "invalid"
    result["cgroup_version"] = next(iter(versions)) if len(versions) == 1 else ("hybrid" if versions else None)
    if "cpu.max" in result:
        quota, period = result["cpu.max"].split()
        result["cpu_quota_read"] = True
        result["cpu_quota_cores"] = None if quota == "max" else int(quota) / int(period)
    elif "cpu.cfs_quota_us" in result and "cpu.cfs_period_us" in result:
        quota, period = int(result["cpu.cfs_quota_us"]), int(result["cpu.cfs_period_us"])
        result["cpu_quota_read"] = True
        result["cpu_quota_cores"] = None if quota == -1 else quota / period
    result["limit_read_status"] = ("partial" if result["read_errors"] else "complete") if count else "unavailable"
    return result


def read_json_file(path, maximum=131072):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise ValueError("input_file_invalid")
    return json.loads(path.read_text(encoding="utf-8"))


def benchmark_render(args, *, inherited_lock_fd=None):
    if inherited_lock_fd is not None:
        validate_inherited_media_lock_fd(inherited_lock_fd)
    if not Path("/proc/self/stat").is_file():
        raise ValueError("render_sampling_requires_linux_proc")
    source, recipe_path = Path(args.source), Path(args.recipe)
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        raise ValueError("source_must_be_a_local_absolute_regular_file")
    render_timeout = gpu.render_timeout_seconds(args.timeout)
    recipe = read_json_file(recipe_path)
    source_preflight_started = time.monotonic()
    source_full_fp = stable_source_fingerprint(source)
    source_preflight_elapsed = round(time.monotonic() - source_preflight_started, 3)
    source_fp = {key: source_full_fp[key] for key in ("sha256", "size_bytes")}
    source_identity = {key: source_full_fp[key] for key in
                       ("device", "inode", "mtime_ns", "nlink")}
    info = gpu._probe(args.ffprobe, source)
    check_sample_duration(args.sample_kind, info["duration"])
    render_planned_timeout = gpu.render_budget_seconds(info["duration"], render_timeout)
    output = fresh_directory(args.output_dir)
    evidence = {"version": 1, "kind": "render", "ok": False, "sample_kind": args.sample_kind,
                "filter_threads": args.filter_threads, "source": source_fp,
                "duration_seconds": info["duration"], "recipe_sha256": recipe.get("recipe_sha256"),
                "asset_manifest_sha256": args.asset_manifest_sha256, "limits": cgroup_limits(),
                "render_timeout_seconds": render_timeout,
                "render_planned_timeout_seconds": render_planned_timeout,
                "render_global_cap_seconds": RENDER_GLOBAL_CAP_SECONDS,
                "rendered_processes": 0, "sample_count": 0, "peak_rss_bytes": 0, "peak_threads": 0,
                "minimum_mem_available_bytes": None,
                "sampling_interval_seconds": 1, "peak_values_are_sampled": True,
                "full_decode_verified": False, "visual_review_required": True,
                "cos_uploads": 0, "production_requests": 0,
                "acceptance_launcher_lock_inherited": inherited_lock_fd is not None}
    evidence.update(source_identity=source_identity,
                    source_preflight_elapsed_seconds=source_preflight_elapsed,
                    source_unchanged=False)
    guard = {"triggered": False, "thresholds": {
        "host_mem_available_below_bytes": MIN_HOST_MEM_AVAILABLE_BYTES,
        "renderer_rss_at_or_above_bytes": STOP_RENDER_RSS_BYTES,
        "renderer_threads_above": MAX_RENDER_THREADS,
    }, "outer_cgroup_hard_limits_required": True}
    if inherited_lock_fd is not None:
        guard["thresholds"]["launcher_start_mem_available_below_bytes"] = (
            MIN_LAUNCHER_START_MEM_AVAILABLE_BYTES
        )
    evidence["resource_guard"] = guard
    atomic_write_record(output / "evidence.json", evidence)
    started = time.monotonic()
    progress = {}
    sample_log = None
    rendered_at = None
    previous_sample = None
    last_metrics = {}
    completed_result = None

    def trip(codes, metrics, phase, *, raise_error=True):
        if isinstance(codes, str):
            codes = [codes]
        if not guard["triggered"]:
            guard.update(triggered=True, reason_codes=codes, phase=phase,
                         observed_at_utc=datetime.now(timezone.utc).isoformat(),
                         elapsed_seconds=round(time.monotonic() - started, 3), metrics=dict(metrics))
        evidence["ok"] = False
        evidence["error_code"] = guard["reason_codes"][0]
        if raise_error:
            raise BenchmarkGuardError(evidence["error_code"])

    def resources(proc=None, phase="sampling"):
        nonlocal last_metrics
        metrics = {"renderer_pid": proc.pid if proc is not None else None}
        try:
            metrics.update(host_memory_sample())
            current_available = metrics["mem_available_bytes"]
            previous_available = evidence["minimum_mem_available_bytes"]
            evidence["minimum_mem_available_bytes"] = (
                current_available if previous_available is None
                else min(previous_available, current_available)
            )
            if proc is not None and proc.poll() is None:
                value = process_sample(proc.pid)
                if value is None and proc.poll() is not None:
                    value = None  # A finished child no longer has /proc data.
                elif (not isinstance(value, dict) or type(value.get("rss_bytes")) is not int or
                      value["rss_bytes"] < 0 or type(value.get("threads")) is not int or value["threads"] < 1 or
                      type(value.get("cpu_seconds")) not in (int, float) or
                      not math.isfinite(value["cpu_seconds"]) or value["cpu_seconds"] < 0):
                    raise BenchmarkGuardError("benchmark_process_sample_invalid")
                if value is not None:
                    metrics.update(value)
        except BenchmarkGuardError as exc:
            trip(exc.code, metrics, phase)
        except Exception:
            trip("benchmark_resource_sampling_failed", metrics, phase)
        last_metrics = metrics
        if "rss_bytes" in metrics:
            evidence["sample_count"] += 1
            evidence["peak_rss_bytes"] = max(evidence["peak_rss_bytes"], metrics["rss_bytes"])
            evidence["peak_threads"] = max(evidence["peak_threads"], metrics["threads"])
        codes = []
        if metrics["mem_available_bytes"] < MIN_HOST_MEM_AVAILABLE_BYTES:
            codes.append("benchmark_host_memory_low")
        if metrics.get("rss_bytes", 0) >= STOP_RENDER_RSS_BYTES:
            codes.append("benchmark_renderer_rss_limit")
        if metrics.get("threads", 0) > MAX_RENDER_THREADS:
            codes.append("benchmark_renderer_thread_limit")
        if codes:
            trip(codes, metrics, phase)
        return metrics

    def on_progress(metrics):
        progress.update(metrics)

    def sample_process(proc):
        nonlocal previous_sample
        metrics = resources(proc)
        now = time.monotonic()
        row = {**progress, **metrics, "elapsed_seconds": round(now - rendered_at, 3),
               "observed_at_utc": datetime.now(timezone.utc).isoformat()}
        if "rss_bytes" in metrics:
            if previous_sample and now > previous_sample[0]:
                row["cpu_percent"] = round(max(0, metrics["cpu_seconds"] - previous_sample[1]) /
                                           (now - previous_sample[0]) * 100, 2)
            previous_sample = (now, metrics["cpu_seconds"])
        try:
            sample_log.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
            sample_log.flush()
        except Exception:
            trip("benchmark_sample_log_failed", metrics, "sampling")

    class ObservedProcess:
        """Sample at the renderer's existing one-second wait, without a thread.

        Any exception reaches run_render_with_progress's own kill/wait finally.
        Cleanup waits must not sample/raise again and prevent child reaping.
        """
        def __init__(self, proc):
            self.proc, self.stopping = proc, False

        def __getattr__(self, name):
            return getattr(self.proc, name)

        def wait(self, timeout=None):
            if self.stopping or guard["triggered"]:
                return self.proc.wait(timeout=timeout)
            try:
                sample_process(self.proc)
                code = self.proc.wait(timeout=timeout)
                # A simultaneous successful exit does not override protection.
                sample_process(self.proc)
                return code
            except (BenchmarkGuardError, subprocess.TimeoutExpired):
                raise
            except Exception:
                trip("benchmark_resource_sampling_failed", last_metrics, "sampling")

        def kill(self):
            self.stopping = True
            return self.proc.kill()

    def start_process(command, **kwargs):
        nonlocal sample_log, rendered_at
        if evidence["rendered_processes"]:
            raise RuntimeError("benchmark_must_launch_exactly_one_renderer")
        metrics = resources(phase="before_launch")
        if (inherited_lock_fd is not None and
                metrics["mem_available_bytes"] < MIN_LAUNCHER_START_MEM_AVAILABLE_BYTES):
            trip("benchmark_launcher_start_memory_low", metrics, "before_launch")
        try:
            sample_log = (output / "process-samples.jsonl").open("x", encoding="utf-8")
        except Exception:
            trip("benchmark_sample_log_failed", metrics, "before_launch")
        proc = launch_renderer_process(
            command, inherited_lock_fd=inherited_lock_fd, **kwargs
        )
        try:
            evidence["rendered_processes"] += 1
            rendered_at = time.monotonic()
            return ObservedProcess(proc)
        except BaseException:
            # Factory failures happen before the runner owns the Popen.
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)
            if proc.stdout is not None:
                proc.stdout.close()
            raise

    def runner(command, **kwargs):
        runner_timeout = kwargs.get("timeout")
        if type(runner_timeout) is not int or runner_timeout != render_planned_timeout:
            raise BenchmarkGuardError("benchmark_render_timeout_contract_mismatch")
        gpu.run_render_with_progress(
            command, timeout=runner_timeout, configured_timeout=render_timeout,
            absolute_timeout=RENDER_GLOBAL_CAP_SECONDS, duration_seconds=info["duration"],
            popen=start_process, progress_callback=on_progress,
        )
        evidence["renderer_elapsed_seconds"] = round(time.monotonic() - rendered_at, 3)

    previous_threads = os.environ.get("DRAMA_GPU_FILTER_THREADS")
    os.environ["DRAMA_GPU_FILTER_THREADS"] = str(args.filter_threads)
    try:
        result = gpu.render_random_output(source=source, output=output / "result.mp4", recipe=recipe,
                                         asset_root=args.asset_root, manifest_sha256=args.asset_manifest_sha256,
                                         ffmpeg=args.ffmpeg, ffprobe=args.ffprobe, timeout=render_timeout, runner=runner)
        if evidence["rendered_processes"] != 1 or not evidence["sample_count"]:
            raise RuntimeError("benchmark_did_not_observe_a_fresh_renderer")
        completed_result = result
    except Exception as exc:
        code = getattr(exc, "code", "render_benchmark_failed")
        evidence["error_code"] = code if re.fullmatch(r"[a-z0-9_]{1,100}", str(code)) else "render_benchmark_failed"
    finally:
        if sample_log is not None:
            try:
                sample_log.close()
            except Exception:
                trip("benchmark_sample_log_failed", last_metrics, "closing_log", raise_error=False)
        if previous_threads is None:
            os.environ.pop("DRAMA_GPU_FILTER_THREADS", None)
        else:
            os.environ["DRAMA_GPU_FILTER_THREADS"] = previous_threads
        source_recheck_started = time.monotonic()
        try:
            source_final_full = stable_source_fingerprint(source)
            evidence["source_final"] = {
                key: source_final_full[key] for key in ("sha256", "size_bytes")
            }
            evidence["source_final_identity"] = {
                key: source_final_full[key] for key in
                ("device", "inode", "mtime_ns", "nlink")
            }
            evidence["source_unchanged"] = source_final_full == source_full_fp
            if not evidence["source_unchanged"]:
                trip("benchmark_source_changed", last_metrics, "source_recheck",
                     raise_error=False)
        except Exception:
            evidence["source_unchanged"] = False
            trip("benchmark_source_recheck_failed", last_metrics, "source_recheck",
                 raise_error=False)
        evidence["source_recheck_elapsed_seconds"] = round(
            time.monotonic() - source_recheck_started, 3
        )
        evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if guard["triggered"]:
            evidence["error_code"] = guard["reason_codes"][0]
        elif completed_result is not None:
            evidence.update(ok=True, result=completed_result)
            evidence["realtime_multiple"] = round(info["duration"] / max(0.001, evidence["renderer_elapsed_seconds"]), 3)
        try:
            atomic_write_record(output / "evidence.json", evidence)
        except Exception:
            trip("benchmark_evidence_write_failed", last_metrics, "writing_evidence", raise_error=False)
            # Preserve a safe diagnostic when the evidence filesystem fails.
            print(json.dumps({"ok": False, "resource_guard": guard}), file=sys.stderr)
            raise BenchmarkGuardError("benchmark_evidence_write_failed") from None
    return evidence


def download_definition(urls, bytes_per_source):
    if (not isinstance(urls, list) or not 1 <= len(urls) <= 8 or
            not all(isinstance(url, str) for url in urls) or len(set(urls)) != len(urls)):
        raise ValueError("download_sources_must_be_one_to_eight_distinct_urls")
    for url in urls:
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (parsed is None or parsed.scheme not in {"http", "https"} or not parsed.hostname or
                parsed.username or parsed.password or parsed.fragment or len(url) > 16384):
            raise ValueError("download_source_invalid")
    if type(bytes_per_source) is not int or not 1 <= bytes_per_source <= 32 * 1024 * 1024 or len(urls) * bytes_per_source > MAX_DOWNLOAD_BYTES:
        raise ValueError("download_budget_exceeds_256_mib")
    value = {"source_ids": [hashlib.sha256(url.encode()).hexdigest() for url in urls],
             "comparison_resource_ids": [comparison_resource_id(url) for url in urls], "bytes_per_source": bytes_per_source}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), value


def comparison_resource_id(url):
    """Comparison label only: NEVER used as downloader cache/resume identity."""
    parsed = urlsplit(url)
    if (parsed.hostname in {"img.tianmai.cn", "accelerate.tianmai.cn"} and
            re.fullmatch(r"/resource/[^/]+/[^/]+", parsed.path)):
        identity = "tianmai-resource:" + parsed.path + "?" + parsed.query
    else:
        identity = url
    return hashlib.sha256(identity.encode()).hexdigest()


def compare_download_evidence(baseline, candidate):
    if (baseline.get("kind") != "download" or baseline.get("ok") is not True or
            baseline.get("workers") != candidate["workers"] or
            baseline.get("definition", {}).get("bytes_per_source") != candidate["definition"]["bytes_per_source"] or
            baseline.get("definition", {}).get("comparison_resource_ids") != candidate["definition"]["comparison_resource_ids"]):
        raise ValueError("comparison_requires_same_worker_count_and_resource_sample_set")
    baseline_speed = float(baseline.get("bytes_per_second", 0))
    if not math.isfinite(baseline_speed) or baseline_speed <= 0:
        raise ValueError("comparison_baseline_speed_invalid")
    fields = ("resource_id", "size_bytes", "total_source_bytes", "sample_sha256")
    signature = lambda value: [tuple(row.get(key) for key in fields) for row in value.get("sources", [])]
    return {"content_equal_for_sample": signature(baseline) == signature(candidate),
            "comparison_scope": "sampled-prefix-and-total-length-only-not-full-object-proof",
            "throughput_ratio": round(candidate["bytes_per_second"] / baseline_speed, 3)}


def benchmark_download(args):
    import requests

    urls = read_json_file(args.url_file)
    definition_sha, definition = download_definition(urls, args.bytes_per_source)
    if len(urls) < args.workers:
        raise ValueError("not_enough_distinct_sources_for_worker_comparison")
    baseline = None
    if args.workers == 8:
        if not args.four_worker_evidence:
            raise ValueError("eight_workers_requires_successful_four_worker_evidence")
        baseline = read_json_file(args.four_worker_evidence)
        if (baseline.get("kind") != "download" or baseline.get("ok") is not True or baseline.get("workers") != 4 or
                baseline.get("definition_sha256") != definition_sha):
            raise ValueError("four_worker_evidence_does_not_match_fixed_sample_set")
    output = fresh_directory(args.output_dir)
    evidence = {"version": 1, "kind": "download", "ok": False, "workers": args.workers,
                "definition_sha256": definition_sha, "definition": definition,
                "maximum_bytes": len(urls) * args.bytes_per_source, "downloaded_bytes": 0,
                "cos_uploads": 0, "production_requests": 0, "sources": []}
    started = time.monotonic()
    lock = threading.Lock()
    stop = threading.Event()

    def sample(item):
        index, url = item
        begin = time.monotonic()
        session = requests.Session()
        session.trust_env = False
        response = None
        try:
            if stop.is_set():
                raise RuntimeError("sample_cancelled")
            response = session.get(url, headers={"Range": "bytes=0-%d" % (args.bytes_per_source - 1), "Accept-Encoding": "identity"},
                                   stream=True, allow_redirects=False, timeout=(10, 30))
            header_seconds = time.monotonic() - begin
            length = int(response.headers.get("Content-Length", "0"))
            if not 0 < length <= args.bytes_per_source or response.headers.get("Content-Encoding", "identity") != "identity":
                raise RuntimeError("source_range_not_bounded")
            if response.status_code == 206:
                match = re.fullmatch(r"bytes 0-([0-9]+)/([0-9]+)", response.headers.get("Content-Range", ""))
                if not match or int(match[1]) + 1 != length or int(match[2]) < length:
                    raise RuntimeError("source_range_invalid")
                total_source_bytes = int(match[2])
            elif response.status_code != 200:
                raise RuntimeError("source_http_failed")
            else:
                total_source_bytes = length
            digest, received = hashlib.sha256(), 0
            while received < length:
                if stop.is_set():
                    raise RuntimeError("sample_cancelled")
                chunk = response.raw.read(min(65536, length - received), decode_content=False)
                if not chunk:
                    raise RuntimeError("sample_truncated")
                received += len(chunk)
                digest.update(chunk)
                with lock:
                    evidence["downloaded_bytes"] += len(chunk)
                    if evidence["downloaded_bytes"] > evidence["maximum_bytes"]:
                        raise RuntimeError("download_budget_exceeded")
            return {"source_id": definition["source_ids"][index], "resource_id": definition["comparison_resource_ids"][index],
                    "source_host": urlsplit(url).hostname, "size_bytes": received, "total_source_bytes": total_source_bytes,
                    "sample_sha256": digest.hexdigest(), "header_seconds": round(header_seconds, 3),
                    "elapsed_seconds": round(time.monotonic() - begin, 3)}
        except Exception:
            stop.set()
            raise RuntimeError("bounded_download_sample_failed") from None
        finally:
            if response is not None:
                response.close()
            session.close()

    atomic_write_record(output / "evidence.json", evidence)
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            evidence["sources"] = list(pool.map(sample, enumerate(urls)))
        if baseline:
            signature = lambda rows: [(row["source_id"], row["size_bytes"], row["sample_sha256"]) for row in rows]
            if signature(baseline.get("sources", [])) != signature(evidence["sources"]):
                raise ValueError("source_sample_changed_since_four_worker_baseline")
        evidence["ok"] = True
    except Exception:
        evidence["error_code"] = "bounded_download_benchmark_failed"
    finally:
        evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
        evidence["bytes_per_second"] = round(evidence["downloaded_bytes"] / max(0.001, evidence["elapsed_seconds"]), 1)
        if evidence["ok"] and args.compare_evidence:
            try:
                evidence["comparison"] = compare_download_evidence(read_json_file(args.compare_evidence), evidence)
                if not evidence["comparison"]["content_equal_for_sample"]:
                    evidence["ok"] = False
                    evidence["error_code"] = "download_sample_content_mismatch"
            except Exception:
                evidence["ok"] = False
                evidence["error_code"] = "download_comparison_invalid"
        atomic_write_record(output / "evidence.json", evidence)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Explicitly authorize local benchmark outputs or bounded sample downloads")
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render")
    render.add_argument("--source", required=True)
    render.add_argument("--recipe", required=True)
    render.add_argument("--asset-root", required=True)
    render.add_argument("--asset-manifest-sha256", required=True)
    render.add_argument("--output-dir", required=True)
    render.add_argument("--sample-kind", choices=("short", "long"), required=True)
    render.add_argument("--filter-threads", type=int, choices=(2, 4), required=True)
    render.add_argument("--ffmpeg", default="/usr/bin/ffmpeg")
    render.add_argument("--ffprobe", default="/usr/bin/ffprobe")
    render.add_argument("--timeout", type=int, default=None, metavar="SECONDS",
                        help="60..86400; defaults to DRAMA_GPU_RENDER_TIMEOUT or 43200")
    download = commands.add_parser("download")
    download.add_argument("--url-file", required=True, help="Private JSON array; URL values are never printed or saved to evidence")
    download.add_argument("--output-dir", required=True)
    download.add_argument("--workers", type=int, choices=(1, 2, 4, 8), required=True)
    download.add_argument("--bytes-per-source", type=int, default=16 * 1024 * 1024)
    download.add_argument("--four-worker-evidence")
    download.add_argument("--compare-evidence", help="Compare same-worker bounded samples, including img/accelerate same-resource paths")
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required; no benchmark was run")
    if args.command == "render" and args.timeout is not None and not 60 <= args.timeout <= gpu.MAX_RENDER_TIMEOUT_SECONDS:
        parser.error("render --timeout must be in 60..86400 seconds")
    try:
        evidence = benchmark_render(args) if args.command == "render" else benchmark_download(args)
        print(json.dumps({"ok": evidence["ok"], "kind": evidence["kind"], "elapsed_seconds": evidence["elapsed_seconds"]}))
        return 0 if evidence["ok"] else 1
    except Exception:
        print(json.dumps({"ok": False, "error_code": "benchmark_preflight_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
