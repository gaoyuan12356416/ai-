#!/usr/bin/env python3
"""Offline clean-reference/V2 visual comparison on an immutable GPU release."""

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
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.drama_synthesis.core import freeze_random_recipe
from features.drama_synthesis.gpu import build_drama_random_command, catalog_from_assets
from features.drama_synthesis.gpu_compositor import BACKEND, render_chunked_random_output
from features.fb_gpu.random_overlay import load_asset_set, selected_asset_paths, validate_recipe
from scripts.benchmark_drama_gpu_compositor_v2 import (
    PRODUCTION_BENCHMARK_ROOT, PRODUCTION_RELEASES_ROOT, probe, safe_new_output_root,
    sha256_file, write_record,
)


def create_clip(ffmpeg: str, source: Path, output: Path, start: int, seconds: int) -> None:
    subprocess.run([
        ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", str(start), "-i", str(source), "-t", str(seconds),
        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
        "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(output),
    ], check=True, capture_output=True, text=True, timeout=900)


def decode_ok(ffmpeg: str, path: Path) -> bool:
    result = subprocess.run([
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-",
    ], check=False, capture_output=True, text=True, timeout=1800)
    return result.returncode == 0


def render_clean_reference(
    ffmpeg: str,
    source: Path,
    output: Path,
    source_info: dict,
    recipe: dict,
    asset_root: str,
    manifest_sha256: str,
) -> None:
    """Render the intended CPU geometry with rotw/roth fed the real angle."""
    assets = load_asset_set(Path(asset_root), manifest_sha256)
    fb_recipe = {
        "asset_set_sha256": recipe.get("asset_set_sha256"),
        "assets": recipe.get("assets"),
        "rotation_millidegrees": int(recipe.get("rotation_millidegrees") or 0),
        "scale_bp": int(recipe.get("scale_bp") or 0),
        "tint_opacity_bp": int(recipe.get("tint_opacity_bp") or 0),
        "version": 1,
    }
    validate_recipe(fb_recipe, assets)
    command = build_drama_random_command(
        SimpleNamespace(ffmpeg=ffmpeg), source, output, source_info, fb_recipe,
        selected_asset_paths(fb_recipe, assets),
    )
    graph_index = command.index("-filter_complex") + 1
    graph = command[graph_index]
    malformed = "ow=rotw(iw):oh=roth(ih)"
    if graph.count(malformed) != 1:
        raise RuntimeError("clean_reference_graph_contract_invalid")
    degrees = fb_recipe["rotation_millidegrees"] / 1000.0
    angle = "%.6f*PI/180" % degrees
    command[graph_index] = graph.replace(
        malformed, "ow=rotw(%s):oh=roth(%s)" % (angle, angle), 1
    )
    subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=1800
    )


def ssim_score(ffmpeg: str, reference: Path, candidate: Path) -> float:
    result = subprocess.run([
        ffmpeg, "-nostdin", "-hide_banner", "-i", str(reference), "-i", str(candidate),
        "-filter_complex", "[0:v][1:v]ssim", "-an", "-f", "null", "-",
    ], check=False, capture_output=True, text=True, timeout=1800)
    match = re.search(r"All:([0-9]+(?:\.[0-9]+)?)", result.stderr or "")
    if result.returncode != 0 or not match:
        raise RuntimeError("visual_ssim_failed")
    score = float(match.group(1))
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise RuntimeError("visual_ssim_invalid")
    return score


def extract_comparisons(
    ffmpeg: str, reference: Path, candidate: Path, output_root: Path, duration: float,
) -> list[dict]:
    records = []
    for index, timestamp in enumerate((1.0, duration / 2.0, max(1.0, duration - 1.0)), start=1):
        output = output_root / ("comparison-%02d.png" % index)
        frame_end = min(duration, timestamp + (1.0 / 30.0))
        subprocess.run([
            ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(reference), "-i", str(candidate),
            "-filter_complex",
            "[0:v]trim=start=%.6f:end=%.6f,setpts=PTS-STARTPTS[left];"
            "[1:v]trim=start=%.6f:end=%.6f,setpts=PTS-STARTPTS[right];"
            "[left][right]hstack=inputs=2[view]"
            % (timestamp, frame_end, timestamp, frame_end),
            "-map", "[view]", "-frames:v", "1", str(output),
        ], check=True, capture_output=True, text=True, timeout=180)
        records.append({
            "timestamp_seconds": round(timestamp, 6), "file": output.name,
            "sha256": sha256_file(output), "size_bytes": output.stat().st_size,
        })
    return records


def extract_candidate_frames(
    ffmpeg: str, candidate: Path, output_root: Path, duration: float,
) -> list[dict]:
    records = []
    for index, timestamp in enumerate((1.0, duration / 2.0, max(1.0, duration - 1.0)), start=1):
        output = output_root / ("candidate-frame-%02d.png" % index)
        frame_end = min(duration, timestamp + (1.0 / 30.0))
        subprocess.run([
            ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(candidate), "-filter_complex",
            "[0:v]trim=start=%.6f:end=%.6f,setpts=PTS-STARTPTS[view]"
            % (timestamp, frame_end),
            "-map", "[view]", "-frames:v", "1", str(output),
        ], check=True, capture_output=True, text=True, timeout=180)
        records.append({
            "timestamp_seconds": round(timestamp, 6), "file": output.name,
            "sha256": sha256_file(output), "size_bytes": output.stat().st_size,
        })
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--allowed-root", default=str(PRODUCTION_BENCHMARK_ROOT))
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--start-seconds", type=int, default=0)
    parser.add_argument("--clip-seconds", type=int, default=30)
    parser.add_argument("--minimum-ssim", type=float, default=0.90)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.candidate_sha):
        raise SystemExit("candidate_sha_invalid")
    if os.environ.get("DRAMA_GPU_RELEASE_SHA") != args.candidate_sha:
        raise SystemExit("candidate_release_mismatch")
    expected_release = PRODUCTION_RELEASES_ROOT / args.candidate_sha
    try:
        if expected_release.is_symlink() or ROOT.resolve() != expected_release.resolve(strict=True):
            raise ValueError()
    except (OSError, ValueError, RuntimeError):
        raise SystemExit("candidate_checkout_mismatch") from None
    if os.environ.get("DRAMA_GPU_COMPOSITOR_BACKEND") != BACKEND:
        raise SystemExit("candidate_backend_mismatch")
    if not 0 <= args.start_seconds <= 7200 or not 10 <= args.clip_seconds <= 300:
        raise SystemExit("clip_range_invalid")
    if not math.isfinite(args.minimum_ssim) or not 0.5 <= args.minimum_ssim <= 1:
        raise SystemExit("minimum_ssim_invalid")
    source = Path(args.source)
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        raise SystemExit("source_invalid")

    output_root = safe_new_output_root(args.output_root, Path(args.allowed_root))
    ffmpeg = os.environ["DRAMA_RANDOM_OVERLAY_FFMPEG"]
    ffprobe = os.environ["DRAMA_RANDOM_OVERLAY_FFPROBE"]
    asset_root = os.environ["DRAMA_RANDOM_OVERLAY_ROOT"]
    manifest = os.environ["DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256"]
    clip = output_root / "source.mp4"
    create_clip(ffmpeg, source, clip, args.start_seconds, args.clip_seconds)
    source_info = probe(ffprobe, clip)
    if abs(source_info["duration"] - args.clip_seconds) > 0.15:
        raise SystemExit("comparison_clip_duration_mismatch")
    clip_sha256 = sha256_file(clip)
    catalog = catalog_from_assets(asset_root, manifest)
    recipe = freeze_random_recipe(
        job_id=clip_sha256[:32],
        content_id=clip_sha256[:24],
        request={"mode": "auto", "source": "concat_video"}, catalog=catalog,
    )

    reference = output_root / "clean-reference.mp4"
    candidate = output_root / "candidate.mp4"
    render_clean_reference(
        ffmpeg, clip, reference, source_info, recipe, asset_root, manifest,
    )
    os.environ["DRAMA_GPU_COMPOSITOR_CACHE_ROOT"] = str(output_root / "candidate-cache")
    result = render_chunked_random_output(
        source=clip, output=candidate, recipe=recipe, asset_root=asset_root,
        manifest_sha256=manifest, ffmpeg=ffmpeg, ffprobe=ffprobe,
    )
    score = ssim_score(ffmpeg, reference, candidate)
    comparisons = extract_comparisons(
        ffmpeg, reference, candidate, output_root, source_info["duration"]
    )
    candidate_frames = extract_candidate_frames(
        ffmpeg, candidate, output_root, source_info["duration"]
    )
    decoded = {
        "clean_reference": decode_ok(ffmpeg, reference),
        "candidate": decode_ok(ffmpeg, candidate),
    }
    passed = bool(all(decoded.values()) and score >= args.minimum_ssim)
    report = {
        "ok": passed, "mode": "offline-clean-reference-no-upload",
        "reference_profile": "correct-angle-centered-v1", "candidate_sha": args.candidate_sha,
        "source_sha256": clip_sha256, "recipe_sha256": recipe["recipe_sha256"],
        "asset_manifest_sha256": manifest, "start_seconds": args.start_seconds,
        "clip_duration_seconds": source_info["duration"], "minimum_ssim": args.minimum_ssim,
        "ssim_all": round(score, 6), "decode_ok": decoded,
        "clean_reference_sha256": sha256_file(reference),
        "candidate_sha256": result["output_sha256"], "comparison_frames": comparisons,
        "candidate_frames": candidate_frames,
    }
    write_record(output_root / "report.json", report)
    print(json.dumps(report, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
