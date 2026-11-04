"""Bounded X rendering qualification; no upload, credentials or task mutation."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fixture", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--adapter", type=Path)
    p.add_argument("--seconds", type=float, default=2)
    p.add_argument("--max-bytes", type=int, default=512*1024**2)
    args = p.parse_args()
    if not .5 <= args.seconds <= 3:
        raise ValueError("bounded_qualification_sample_required")
    os.environ["RANDOM_GPU_NATIVE"] = "1"
    from features.x_posts import media_repair as repair
    if args.adapter:
        # Evaluation-only import overlay for the immutable deployed baseline.
        spec = importlib.util.spec_from_file_location("features.x_posts.random_template_gpu", args.adapter)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
    else:
        from features.x_posts import random_template_gpu as gpu
    from features.fb_gpu.random_overlay import load_asset_set, selected_asset_paths
    from features.fb_gpu.render_process import run_render
    probe = lambda path: json.loads(subprocess.check_output([
        "/usr/bin/ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], timeout=30))
    frozen = json.loads((args.fixture/"recipe.json").read_text())
    source = args.fixture/"source.mp4"
    raw = probe(source)
    info = dict(duration=args.seconds, format=raw["format"],
                video=next(s for s in raw["streams"] if s["codec_type"] == "video"),
                has_audio=any(s["codec_type"] == "audio" for s in raw["streams"]))
    assets = selected_asset_paths(frozen["recipe"], load_asset_set(
        Path(frozen["asset_root"]), frozen["recipe"]["asset_set_sha256"]))
    config = SimpleNamespace(profile=gpu.PROFILE, ffmpeg_bin="/usr/bin/ffmpeg",
                             max_output_bytes=args.max_bytes)
    command = gpu.random_command(config, source, args.output, info, frozen["recipe"], assets)
    spec = json.loads(command[command.index("--spec")+1])
    assert spec["audio_bitrate"] == 128000
    try:
        run_render(command, timeout=180, stall_seconds=60, poll_seconds=.2)
    except subprocess.CalledProcessError as exc:
        print(exc.stderr, file=sys.stderr)
        raise
    out = probe(args.output)
    accepted = repair.validate_output(out, args.output, (720, 1280), args.max_bytes,
                                      expected_duration=args.seconds)
    assert abs(accepted["duration"]-args.seconds) <= .05, accepted
    subprocess.run(["/usr/bin/ffmpeg", "-v", "error", "-xerror", "-threads", "1",
                    "-i", str(args.output), "-f", "null", "-"], check=True, timeout=60)
    result = json.loads(args.output.with_suffix(args.output.suffix+".native.json").read_text())
    result.update(accepted=accepted, full_decode_passed=True, size=args.output.stat().st_size,
                  budget=repair.output_rate_control(args.max_bytes, args.seconds))
    args.output.with_suffix(".acceptance.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
