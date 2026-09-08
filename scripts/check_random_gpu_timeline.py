"""GPU regression: frame identities, mixed frame rates, resolution change, A/V offset.

Only creates local synthetic media. No worker API, upload or publishing calls.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.random_gpu.compositor import BACKEND


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--worker", choices=("tt", "fb"), required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    args = parser.parse_args(); root = Path(args.output); root.mkdir(parents=True, exist_ok=True)
    if args.worker == "tt":
        from features.tt_gpu.worker import build_random_overlay_command
        def build(cfg, source, output, info, recipe, assets):
            return build_random_overlay_command(cfg, source, output, info, info["duration"], recipe, assets)
    else:
        from features.fb_gpu.prepare_worker import build_command as build
    def run(arguments):
        return subprocess.run([args.ffmpeg, "-nostdin", "-y", "-v", "error", "-filter_threads", "2", *arguments],
                              check=True, capture_output=True, timeout=90)
    def generate(name, size, rate, seconds, offset=0, transport=False):
        graph = "nullsrc=s=%s:r=%s:d=%s,geq=lum='16+mod((N+%d)*37,200)':cb=128:cr=128" % (size, rate, seconds, offset)
        codec = ["-c:v", "libx264", "-threads", "2", "-g", "1", "-crf", "0", "-f", "mpegts"] if transport else ["-c:v", "ffv1"]
        run(["-f", "lavfi", "-i", graph, *codec, str(root / name)])
    generate("portrait.nut", "320x568", 30, 2)
    generate("landscape.nut", "320x180", 25, 2)
    generate("part0.ts", "320x180", 30, 1, transport=True)
    generate("part1.ts", "320x568", 30, 1, offset=30, transport=True)
    (root / "parts.txt").write_text("file 'part0.ts'\nfile 'part1.ts'\n")
    run(["-f", "concat", "-safe", "0", "-i", str(root / "parts.txt"), "-c", "copy", str(root / "change.nut")])
    run(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2.034",
         "-itsoffset", "0.033333333", "-i", str(root / "portrait.nut"), "-map", "1:v", "-map", "0:a",
         "-c:v", "copy", "-c:a", "pcm_s16le", str(root / "offset.nut")])
    run(["-f", "lavfi", "-i", "color=black@0:s=16x16:r=30,format=rgba", "-frames:v", "1", str(root / "clear.png")])
    run(["-f", "lavfi", "-i", "color=black@0:s=16x16:r=30:d=1,format=yuva420p", "-c:v", "libvpx-vp9",
         "-auto-alt-ref", "0", str(root / "clear.webm")])
    assets = {key: root / ("clear.webm" if key in ("opacity_video", "corners") else "clear.png")
              for key in ("border", "opacity_video", "corners", "tint")}
    if os.environ.get('RANDOM_GPU_ASSET_CACHE_ROOT'):
        # The acceptance fixtures have their own cache; never mix test assets
        # into the production cache or invoke the online worker.
        from scripts.build_random_gpu_asset_cache import build as build_cache
        cache = root / 'fixture-cache'; cache.mkdir(exist_ok=True)
        for source in set(assets.values()):
            build_cache(args.ffmpeg, source, cache, 16 * 1024**3)
        os.environ['RANDOM_GPU_ASSET_CACHE_ROOT'] = str(cache)
    recipe = {"rotation_millidegrees": 0, "scale_bp": 10000, "tint_opacity_bp": 100}
    cfg = SimpleNamespace(ffmpeg=args.ffmpeg, ffmpeg_bin=args.ffmpeg, video_encoder="hevc_nvenc", compositor_backend=BACKEND)
    def gray(path, duration):
        return list(run(["-reinit_filter", "0", "-threads", "2", "-i", str(path), "-map", "0:v:0", "-an",
                         "-vf", "fps=30:start_time=0,scale=1:1:eval=frame,format=gray", "-t", str(duration), "-f", "rawvideo", "-"]).stdout)
    results = []
    for name, duration, audio in (("portrait", 2, False), ("landscape", 2, False), ("change", 2, False), ("offset", 61 / 30, True)):
        source = root / (name + ".nut"); output = root / (name + ".mp4")
        command = build(cfg, source, output, {"has_audio": audio, "duration": duration}, recipe, assets)
        subprocess.run(command, check=True, capture_output=True, timeout=90)
        expected, actual = gray(source, duration), gray(output, duration)
        mismatches = sum(abs(a - b) > 5 for a, b in zip(expected, actual))
        row = {"case": name, "expected_frames": len(expected), "output_frames": len(actual),
               "frame_identity_mismatches": mismatches, "expected_values": expected, "actual_values": actual}
        probe = json.loads(subprocess.check_output([args.ffprobe, "-v", "error", "-show_streams", "-of", "json", str(output)]))
        row["streams"] = [{k: s.get(k) for k in ("codec_type", "start_time", "duration", "nb_frames")} for s in probe["streams"]]
        row["pass"] = len(actual) == len(expected) == round(duration * 30) and mismatches == 0
        results.append(row)
        (root / "timeline.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in row.items() if k not in ("expected_values", "actual_values", "streams")}), flush=True)
        if not row["pass"]: raise AssertionError("frame identity or frame count changed")


if __name__ == "__main__": main()
