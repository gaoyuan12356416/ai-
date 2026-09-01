#!/usr/bin/env python3
"""Offline T4 benchmark for the production random-overlay Composition Spec.

The script creates a private source clip and renderer cache under a new
benchmark directory.  It never submits a business job or uploads an object.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

try:
    import resource
except ImportError:  # pragma: no cover - benchmark production is Linux-only.
    resource = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.drama_synthesis.core import freeze_random_recipe
from features.drama_synthesis.gpu import catalog_from_assets
from features.drama_synthesis.composition import RENDERER_PROFILE
from features.drama_synthesis.gpu_compositor import (
    BACKEND, KERNEL_TEMPLATE, _video_contract, compositor_lanes,
    render_chunked_random_output, runtime_identity,
)


PRODUCTION_BENCHMARK_ROOT = Path("/data/drama-synthesis-gpu/work/benchmarks")
PRODUCTION_RELEASES_ROOT = Path("/data/drama-synthesis-gpu/releases")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_new_output_root(value: str, allowed_root: Path) -> Path:
    root = Path(value)
    allowed = Path(allowed_root)
    if not root.is_absolute() or not allowed.is_absolute() or allowed.is_symlink() or not allowed.is_dir():
        raise ValueError("benchmark_root_invalid")
    try:
        root.resolve(strict=False).relative_to(allowed.resolve(strict=True))
    except (ValueError, OSError, RuntimeError):
        raise ValueError("benchmark_root_outside_allowed_path") from None
    if root == allowed or root.exists() or root.is_symlink():
        raise ValueError("benchmark_output_must_be_new")
    root.mkdir(mode=0o700, parents=False)
    return root


def safe_output_root(value: str, allowed_root: Path, *, resume: bool) -> Path:
    if not resume:
        return safe_new_output_root(value, allowed_root)
    root = Path(value)
    allowed = Path(allowed_root)
    try:
        valid = (
            root.is_absolute() and allowed.is_absolute() and not root.is_symlink()
            and root.is_dir() and not allowed.is_symlink() and allowed.is_dir()
            and root.resolve(strict=True) != allowed.resolve(strict=True)
        )
        root.resolve(strict=True).relative_to(allowed.resolve(strict=True))
    except (ValueError, OSError, RuntimeError):
        valid = False
    if not valid:
        raise ValueError("benchmark_resume_root_invalid")
    return root


def file_record(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("benchmark_artifact_invalid")
    return {"sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def write_record(path: Path, value: dict) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with temporary.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def probe(ffprobe: str, path: Path) -> dict:
    result = subprocess.run(
        [
            ffprobe, "-v", "error", "-show_data_hash", "sha256",
            "-show_streams", "-show_format", "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True, timeout=180,
    )
    packet_result = subprocess.run(
        [
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-read_intervals", "0%+#1", "-show_packets",
            "-show_entries", "packet=flags", "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True, timeout=180,
    )
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    video = next(row for row in streams if row.get("codec_type") == "video")
    audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
    duration = float(video.get("duration") or (payload.get("format") or {}).get("duration"))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("benchmark_probe_invalid")
    packets = json.loads(packet_result.stdout).get("packets") or []
    return {
        "duration": duration, "has_audio": audio is not None, "video": video, "audio": audio,
        "first_packet_keyframe": bool(packets and "K" in str(packets[0].get("flags") or "")),
    }


def create_clip(ffmpeg: str, source: Path, output: Path, seconds: int) -> None:
    subprocess.run([
        ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-t", str(seconds), "-map", "0:v:0", "-map", "0:a:0?",
        "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(output),
    ], check=True, capture_output=True, text=True, timeout=900)


def swap_used_bytes() -> int:
    values = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, separator, remainder = line.partition(":")
        if separator and key in {"SwapTotal", "SwapFree"}:
            values[key] = int(remainder.strip().split()[0]) * 1024
    return max(0, values.get("SwapTotal", 0) - values.get("SwapFree", 0))


class NvidiaSampler:
    def __init__(self, swap_baseline: int):
        self.stop = threading.Event()
        self.max_memory_mib = 0
        self.max_utilization_percent = 0
        self.max_swap_used_bytes = swap_baseline
        self.memory_samples = []
        self.utilization_samples = []
        self.samples = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.wait(0.5):
            try:
                result = subprocess.run([
                    "nvidia-smi", "--query-gpu=memory.used,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ], check=True, capture_output=True, text=True, timeout=5)
                memory, utilization = (int(part.strip()) for part in result.stdout.splitlines()[0].split(","))
                self.max_memory_mib = max(self.max_memory_mib, memory)
                self.max_utilization_percent = max(self.max_utilization_percent, utilization)
                self.max_swap_used_bytes = max(self.max_swap_used_bytes, swap_used_bytes())
                self.memory_samples.append(memory)
                self.utilization_samples.append(utilization)
                self.samples += 1
            except Exception:
                continue

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.stop.set()
        self.thread.join(timeout=5)


def percentile(values, ratio: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * ratio) - 1))
    return int(ordered[index])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--clip-seconds", type=int, default=300)
    parser.add_argument("--allowed-root", default=str(PRODUCTION_BENCHMARK_ROOT))
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--minimum-realtime", type=float, default=1.5)
    parser.add_argument("--max-gpu-memory-mib", type=int, default=15000)
    parser.add_argument("--max-child-rss-kib", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--confirmed-stopped-recovery", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if not 30 <= args.clip_seconds <= 7200:
        raise SystemExit("clip_seconds_out_of_range")
    if not re.fullmatch(r"[0-9a-f]{40}", args.candidate_sha):
        raise SystemExit("candidate_sha_invalid")
    if os.environ.get("DRAMA_GPU_RELEASE_SHA") != args.candidate_sha:
        raise SystemExit("candidate_release_mismatch")
    expected_release = PRODUCTION_RELEASES_ROOT / args.candidate_sha
    try:
        release_matches = (
            not expected_release.is_symlink()
            and expected_release.is_dir()
            and ROOT.resolve() == expected_release.resolve()
        )
    except (OSError, RuntimeError):
        release_matches = False
    if not release_matches:
        raise SystemExit("candidate_checkout_mismatch")
    if os.environ.get("DRAMA_GPU_COMPOSITOR_BACKEND") != BACKEND:
        raise SystemExit("candidate_backend_mismatch")
    if not math.isfinite(args.minimum_realtime) or not 1 <= args.minimum_realtime <= 20:
        raise SystemExit("minimum_realtime_invalid")
    if not 1024 <= args.max_gpu_memory_mib <= 16384:
        raise SystemExit("max_gpu_memory_invalid")
    if not 1024 * 1024 <= args.max_child_rss_kib <= 32 * 1024 * 1024:
        raise SystemExit("max_child_rss_invalid")
    if args.confirmed_stopped_recovery and not args.resume:
        raise SystemExit("confirmed_stopped_recovery_requires_resume")
    source = Path(args.source)
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        raise SystemExit("source_invalid")
    output_root = safe_output_root(
        args.output_root, Path(args.allowed_root), resume=args.resume
    )
    try:
        import fcntl
        benchmark_lock = os.open(str(output_root / ".benchmark.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(benchmark_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        raise SystemExit("benchmark_lock_unavailable") from None
    ffmpeg = os.environ.get("DRAMA_RANDOM_OVERLAY_FFMPEG", "/usr/bin/ffmpeg")
    ffprobe = os.environ.get("DRAMA_RANDOM_OVERLAY_FFPROBE", "/usr/bin/ffprobe")
    asset_root = os.environ.get("DRAMA_RANDOM_OVERLAY_ROOT", "")
    manifest = os.environ.get("DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256", "")
    clip = output_root / "source.mp4"
    intent_path = output_root / "intent.json"
    expected_intent = {
        "version": 1, "candidate_sha": args.candidate_sha,
        "source_path": str(source.resolve()), "clip_seconds": args.clip_seconds,
        "asset_manifest_sha256": manifest, "renderer_backend": BACKEND,
        "renderer_profile": RENDERER_PROFILE, "runtime_identity": runtime_identity(),
    }
    if args.resume:
        try:
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            clip_record = file_record(clip)
            if (
                not isinstance(intent, dict)
                or set(intent) != set(expected_intent) | {"clip"}
                or any(intent.get(key) != value for key, value in expected_intent.items())
                or intent.get("clip") != clip_record
            ):
                raise ValueError()
        except Exception:
            raise SystemExit("benchmark_resume_identity_mismatch") from None
    else:
        create_clip(ffmpeg, source, clip, args.clip_seconds)
        clip_record = file_record(clip)
        write_record(intent_path, {**expected_intent, "clip": clip_record})
    source_info = probe(ffprobe, clip)
    if abs(source_info["duration"] - args.clip_seconds) > 0.15:
        raise SystemExit("benchmark_clip_duration_mismatch")
    clip_sha256 = clip_record["sha256"]
    catalog = catalog_from_assets(asset_root, manifest)
    recipe = freeze_random_recipe(
        job_id=clip_sha256[:32],
        content_id=clip_sha256[:24],
        request={"mode": "auto", "source": "concat_video"},
        catalog=catalog,
    )
    os.environ["DRAMA_GPU_COMPOSITOR_CACHE_ROOT"] = str(output_root / "cache")
    output = output_root / "rendered.mp4"
    swap_before = swap_used_bytes()
    started = time.monotonic()
    with NvidiaSampler(swap_before) as gpu:
        result = render_chunked_random_output(
            source=clip, output=output, recipe=recipe, asset_root=asset_root,
            manifest_sha256=manifest, ffmpeg=ffmpeg, ffprobe=ffprobe,
            confirmed_stopped_recovery=args.confirmed_stopped_recovery,
        )
    elapsed = time.monotonic() - started
    output_info = probe(ffprobe, output)
    swap_after = swap_used_bytes()
    multiplier = source_info["duration"] / elapsed
    contract = {
        "codec": output_info["video"].get("codec_name"),
        "profile": output_info["video"].get("profile"),
        "width": output_info["video"].get("width"),
        "height": output_info["video"].get("height"),
        "pixel_format": output_info["video"].get("pix_fmt"),
        "frame_rate": output_info["video"].get("avg_frame_rate"),
        "has_audio": output_info["has_audio"],
    }
    contract_ok = (
        contract == {
            "codec": "h264", "profile": "High", "width": 720, "height": 1280,
            "pixel_format": "yuv420p", "frame_rate": "30/1", "has_audio": True,
        }
        and abs(source_info["duration"] - output_info["duration"]) <= 0.15
        and _video_contract(
            output_info, round(source_info["duration"] * 30) / 30,
            expected_frames=round(source_info["duration"] * 30), audio=True,
        )
        and result["output_sha256"] == sha256_file(output)
    )
    child_max_rss_kib = (
        resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss if resource is not None else 0
    )
    passed = bool(
        contract_ok
        and multiplier >= args.minimum_realtime
        and gpu.samples >= 2
        and gpu.max_swap_used_bytes <= swap_before
        and gpu.max_memory_mib <= args.max_gpu_memory_mib
        and child_max_rss_kib <= args.max_child_rss_kib
    )
    report = {
        "ok": passed,
        "mode": "offline-no-upload",
        "candidate_sha": args.candidate_sha,
        "renderer_backend": BACKEND,
        "renderer_profile": RENDERER_PROFILE,
        "runtime_identity": runtime_identity(),
        "kernel_template_sha256": sha256_file(KERNEL_TEMPLATE),
        "asset_manifest_sha256": manifest,
        "compositor_lanes": compositor_lanes(),
        "minimum_realtime": args.minimum_realtime,
        "max_gpu_memory_limit_mib": args.max_gpu_memory_mib,
        "max_child_rss_limit_kib": args.max_child_rss_kib,
        "confirmed_stopped_recovery": args.confirmed_stopped_recovery,
        "clip_duration_seconds": round(source_info["duration"], 6),
        "output_duration_seconds": round(output_info["duration"], 6),
        "elapsed_seconds": round(elapsed, 3),
        "realtime_multiplier": round(multiplier, 3),
        "max_gpu_memory_mib": gpu.max_memory_mib,
        "max_gpu_utilization_percent": gpu.max_utilization_percent,
        "p50_gpu_memory_mib": percentile(gpu.memory_samples, 0.50),
        "p95_gpu_memory_mib": percentile(gpu.memory_samples, 0.95),
        "p50_gpu_utilization_percent": percentile(gpu.utilization_samples, 0.50),
        "p95_gpu_utilization_percent": percentile(gpu.utilization_samples, 0.95),
        "gpu_samples": gpu.samples,
        "child_max_rss_kib": child_max_rss_kib,
        "swap_used_before_bytes": swap_before,
        "swap_used_after_bytes": swap_after,
        "swap_delta_bytes": swap_after - swap_before,
        "swap_peak_used_bytes": gpu.max_swap_used_bytes,
        "output_sha256": result["output_sha256"],
        "output_size": result["output_size"],
        "output_contract": contract,
        "output_contract_ok": contract_ok,
    }
    (output_root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
