"""Offline same-source benchmark; never calls job APIs, COS or social APIs."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import resource
import subprocess
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.random_gpu.compositor import BACKEND, LEGACY, preflight


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("worker", "source", "output", "assets", "manifest", "ffmpeg", "ffprobe"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--variants", default="legacy,cpu_reference,gpu")
    args = parser.parse_args()
    if args.worker not in ("tt", "fb"): parser.error("worker must be tt or fb")
    if args.worker == "tt":
        from features.tt_gpu import worker as worker
        from features.tt_gpu import random_overlay as random
        profile = worker.RANDOM_OVERLAY_PROFILE
    else:
        from features.fb_gpu import prepare_worker as worker
        from features.fb_gpu import random_overlay as random
        profile = worker.PROFILE
    root = Path(args.output); root.mkdir(parents=True, exist_ok=True)
    source = Path(args.source)
    def probe(path):
        data = json.loads(subprocess.check_output([args.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]))
        return data
    data = probe(source)
    duration = min(args.seconds, float(data["format"]["duration"]))
    info = {"duration": duration, "has_audio": any(s["codec_type"] == "audio" for s in data["streams"])}
    asset_set = random.load_asset_set(Path(args.assets), args.manifest)
    recipe = random.derive_recipe(job_id="offline-gpu-20260907", content_id="fixed-input", profile=profile,
                                  source_url_sha256="a" * 64, asset_set=asset_set)
    paths = random.selected_asset_paths(recipe, asset_set)
    cfg = SimpleNamespace(ffmpeg=args.ffmpeg, ffmpeg_bin=args.ffmpeg, video_encoder="hevc_nvenc",
                          compositor_backend=LEGACY)
    report = {"worker": args.worker, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "duration": duration, "recipe": recipe, "runs": []}
    preflight(args.ffmpeg, "hevc_nvenc" if args.worker == "tt" else "h264_nvenc", root)
    for variant in args.variants.split(","):
        if variant not in ("legacy", "cpu_reference", "gpu"): parser.error("invalid variant")
        output = root / (variant + ".mp4")
        if output.exists(): raise RuntimeError("refusing to overwrite benchmark output")
        cfg.compositor_backend = BACKEND if variant == "gpu" else LEGACY
        if args.worker == "tt":
            command = worker.build_random_overlay_command(cfg, source, output, info, duration, recipe, paths)
        else:
            command = worker.build_command(cfg, source, output, info, recipe, paths)
        if variant == "cpu_reference":
            # Correct only the legacy rotation extent; keep contain geometry.
            a = "%.6f*PI/180" % (recipe["rotation_millidegrees"] / 1000)
            index = command.index("-filter_complex") + 1
            command[index] = command[index].replace("ow=rotw(iw):oh=roth(ih)", "ow=rotw(%s):oh=roth(%s)" % (a, a))
        (root / (variant + ".command.json")).write_text(json.dumps(command), encoding="utf-8")
        before = resource.getrusage(resource.RUSAGE_CHILDREN); started = time.monotonic()
        with (root / (variant + ".log")).open("w") as log:
            subprocess.run(command, check=True, stdout=log, stderr=log, timeout=1200)
        wall = time.monotonic() - started; after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
        output_probe = probe(output)
        subprocess.run([args.ffmpeg, "-nostdin", "-v", "error", "-threads", "2", "-i", str(output),
                        "-f", "null", "-"], check=True, capture_output=True, timeout=240)
        row = {"variant": variant, "wall_seconds": wall, "cpu_seconds": cpu, "bytes": output.stat().st_size,
               "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "probe": output_probe, "decode": "pass"}
        report["runs"].append(row)
        (root / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in row.items() if k != "probe"}), flush=True)


if __name__ == "__main__": main()
