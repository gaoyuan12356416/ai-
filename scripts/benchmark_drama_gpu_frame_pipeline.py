#!/usr/bin/env python3
"""Fixed-input, private A/B test of baseline/cache/native GPU chunks."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from features.drama_synthesis.asset_cache import selected_entries
from features.drama_synthesis.composition import compile_random_overlay_spec
from features.drama_synthesis.core import freeze_random_recipe
from features.drama_synthesis.gpu import catalog_from_assets
from features.drama_synthesis.gpu_compositor import (
    _probe, _validate_recipe_and_assets, _video_contract, build_opencl_chunk_command,
    compile_opencl_kernel,
)
from features.fb_gpu.random_overlay import selected_asset_paths
from scripts.benchmark_drama_gpu_compositor_v2 import sha256_file, NvidiaSampler, swap_used_bytes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--mode", choices=("baseline", "cache", "native"), required=True)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--start", type=int, default=0)
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 300 or not 0 <= args.start <= 7200:
        raise SystemExit("benchmark_timeline_invalid")
    output_root = Path(args.output_root)
    allowed = Path("/data/drama-synthesis-gpu/work/benchmarks")
    if (not output_root.is_absolute() or output_root.is_symlink() or output_root.exists()
            or not output_root.resolve().is_relative_to(allowed.resolve()) or output_root == allowed):
        raise SystemExit("benchmark_root_invalid")
    sha = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    if ROOT != Path("/data/drama-synthesis-gpu/releases") / sha:
        raise SystemExit("benchmark_checkout_not_immutable")
    source = Path(args.source)
    if not source.is_file() or source.is_symlink():
        raise SystemExit("benchmark_source_invalid")
    output_root.mkdir(mode=0o700)
    ffmpeg, ffprobe = (os.environ["DRAMA_RANDOM_OVERLAY_" + key] for key in ("FFMPEG", "FFPROBE"))
    asset_root = Path(os.environ["DRAMA_RANDOM_OVERLAY_ROOT"])
    manifest = os.environ["DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256"]
    digest = sha256_file(source)
    recipe = freeze_random_recipe(job_id=digest[:32], content_id=digest[:24],
        request={"mode": "auto", "source": "concat_video"}, catalog=catalog_from_assets(asset_root, manifest))
    _, asset_set, fb_recipe = _validate_recipe_and_assets(recipe, asset_root, manifest)
    assets = selected_asset_paths(fb_recipe, asset_set)
    info = _probe(ffprobe, source)
    if args.start + args.seconds > info["duration"] + .1:
        raise SystemExit("benchmark_source_too_short")
    spec = compile_random_overlay_spec(recipe, info)
    chunk = {"index": 0, "start_frame": args.start * 30, "start_seconds": args.start,
             "frame_count": args.seconds * 30, "duration_seconds": args.seconds}
    durations = {key: _probe(ffprobe, value)["duration"] for key, value in assets.items()
                 if fb_recipe["assets"][key]["media_type"] == "video/webm"}
    cached = selected_entries(fb_recipe) if args.mode != "baseline" else {}
    kernel = compile_opencl_kernel(spec)
    kernel_path = output_root / "kernel.cl"
    kernel_path.write_text(kernel["source"], encoding="utf-8")
    output = output_root / "rendered.mp4"
    if args.mode == "native":
        plan = {"spec": spec, "source": str(source), "output": str(output), "ffmpeg": ffmpeg,
                "chunk": chunk, "assets": fb_recipe["assets"], "asset_durations": durations,
                "asset_cache_root": os.environ["DRAMA_GPU_ASSET_CACHE_ROOT"]}
        plan_path = output_root / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        command = [sys.executable, "-m", "features.drama_synthesis.native_gpu", "--plan", str(plan_path)]
    else:
        command = build_opencl_chunk_command(ffmpeg=ffmpeg, source=source, output=output,
            spec=spec, assets=assets, asset_media_types={k:v["media_type"] for k,v in fb_recipe["assets"].items()},
            asset_durations=durations, chunk=chunk, kernel_path=kernel_path, asset_cache_entries=cached)
    env = {**os.environ, "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    with (output_root/"child.log").open("wb") as log, NvidiaSampler(swap_used_bytes()) as sampler:
        child = subprocess.run(command, env=env, stdout=log, stderr=log, timeout=900)
    elapsed = time.monotonic() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_seconds = after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime
    contract = _video_contract(_probe(ffprobe, output), args.seconds,
                              expected_frames=args.seconds*30, audio=False) if child.returncode == 0 else False
    report = {"mode": args.mode, "candidate_sha": sha, "source_sha256": digest,
              "recipe_sha256": recipe["recipe_sha256"], "seconds": args.seconds, "start": args.start,
              "elapsed_seconds": round(elapsed, 3), "cpu_seconds": round(cpu_seconds, 3),
              "average_cpu_cores": round(cpu_seconds/elapsed, 3), "exit_code": child.returncode,
              "realtime": round(args.seconds/elapsed, 3), "output_contract": contract,
              "max_gpu_memory_mib": sampler.max_memory_mib,
              "p50_gpu_utilization": sorted(sampler.utilization_samples)[len(sampler.utilization_samples)//2]
                  if sampler.utilization_samples else None,
              "output_sha256": sha256_file(output) if contract else None,
              "output_bytes": output.stat().st_size if output.is_file() else 0}
    (output_root/"report.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report))
    return 0 if contract else 1


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    raise SystemExit(main())
