#!/usr/bin/env python3
"""Explicit local media benchmarks; never submit production jobs or upload COS.

Render each fixed source/recipe into a NEW absolute output directory. The
operator supplies CPUQuota through an outer systemd-run scope; this script does
not alter service configuration. Download probes read an operator-provided URL
file, discard sampled bodies and cap application reads at 256 MiB per run.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.drama_synthesis import gpu
from features.drama_synthesis.local_checkpoint import atomic_write_record, file_fingerprint


MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024


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


def benchmark_render(args):
    if not Path("/proc/self/stat").is_file():
        raise ValueError("render_sampling_requires_linux_proc")
    source, recipe_path = Path(args.source), Path(args.recipe)
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        raise ValueError("source_must_be_a_local_absolute_regular_file")
    render_timeout = gpu.render_timeout_seconds(args.timeout)
    recipe = read_json_file(recipe_path)
    info = gpu._probe(args.ffprobe, source)
    check_sample_duration(args.sample_kind, info["duration"])
    source_fp = file_fingerprint(source)
    output = fresh_directory(args.output_dir)
    evidence = {"version": 1, "kind": "render", "ok": False, "sample_kind": args.sample_kind,
                "filter_threads": args.filter_threads, "source": source_fp,
                "duration_seconds": info["duration"], "recipe_sha256": recipe.get("recipe_sha256"),
                "asset_manifest_sha256": args.asset_manifest_sha256, "limits": cgroup_limits(),
                "render_timeout_seconds": render_timeout,
                "rendered_processes": 0, "sample_count": 0, "peak_rss_bytes": 0, "peak_threads": 0,
                "sampling_interval_seconds": 1, "peak_values_are_sampled": True,
                "full_decode_verified": False, "visual_review_required": True,
                "cos_uploads": 0, "production_requests": 0}
    atomic_write_record(output / "evidence.json", evidence)
    started = time.monotonic()
    progress = {}
    progress_lock = threading.Lock()
    stop = threading.Event()
    sampler = None
    rendered_at = None

    def on_progress(metrics):
        with progress_lock:
            progress.update(metrics)

    def start_process(command, **kwargs):
        nonlocal sampler, rendered_at
        if evidence["rendered_processes"]:
            raise RuntimeError("benchmark_must_launch_exactly_one_renderer")
        proc = subprocess.Popen(command, **kwargs)
        evidence["rendered_processes"] += 1
        rendered_at = time.monotonic()

        def sample():
            previous = None
            with (output / "process-samples.jsonl").open("x", encoding="utf-8") as log:
                while not stop.is_set():
                    value = process_sample(proc.pid)
                    now = time.monotonic()
                    if value is not None:
                        row = {**value, "elapsed_seconds": round(now - rendered_at, 3)}
                        if previous and now > previous[0]:
                            row["cpu_percent"] = round(max(0, value["cpu_seconds"] - previous[1]) / (now - previous[0]) * 100, 2)
                        previous = (now, value["cpu_seconds"])
                        with progress_lock:
                            row.update(progress)
                        evidence["sample_count"] += 1
                        evidence["peak_rss_bytes"] = max(evidence["peak_rss_bytes"], value["rss_bytes"])
                        evidence["peak_threads"] = max(evidence["peak_threads"], value["threads"])
                        log.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
                        log.flush()
                    if proc.poll() is not None:
                        break
                    stop.wait(1)

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        return proc

    def runner(command, **kwargs):
        gpu.run_render_with_progress(command, timeout=kwargs["timeout"], duration_seconds=info["duration"],
                                     popen=start_process, progress_callback=on_progress)
        evidence["renderer_elapsed_seconds"] = round(time.monotonic() - rendered_at, 3)

    previous_threads = os.environ.get("DRAMA_GPU_FILTER_THREADS")
    os.environ["DRAMA_GPU_FILTER_THREADS"] = str(args.filter_threads)
    try:
        result = gpu.render_random_output(source=source, output=output / "result.mp4", recipe=recipe,
                                         asset_root=args.asset_root, manifest_sha256=args.asset_manifest_sha256,
                                         ffmpeg=args.ffmpeg, ffprobe=args.ffprobe, timeout=render_timeout, runner=runner)
        if evidence["rendered_processes"] != 1 or not evidence["sample_count"]:
            raise RuntimeError("benchmark_did_not_observe_a_fresh_renderer")
        evidence.update(ok=True, result=result)
        evidence["realtime_multiple"] = round(info["duration"] / evidence["renderer_elapsed_seconds"], 3)
    except Exception as exc:
        code = getattr(exc, "code", "render_benchmark_failed")
        evidence["error_code"] = code if re.fullmatch(r"[a-z0-9_]{1,100}", str(code)) else "render_benchmark_failed"
    finally:
        stop.set()
        if sampler is not None:
            sampler.join(timeout=5)
        if previous_threads is None:
            os.environ.pop("DRAMA_GPU_FILTER_THREADS", None)
        else:
            os.environ["DRAMA_GPU_FILTER_THREADS"] = previous_threads
        evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
        atomic_write_record(output / "evidence.json", evidence)
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
