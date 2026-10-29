#!/usr/bin/env python3
"""Isolated local-media canary; no API, database, queue, COS or live ledger writes.

Run with the existing HK runtime Python and an exact release checkout. Use a
fresh --work-dir on the data disk, after the deployment's idle check. It uses
the production command builder and runner directly with no AsyncRuntime
context. Default encoder is NVENC; libx264 exists only for local smoke tests.
Evidence remains in --work-dir for inspection; this script deletes nothing.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True


def command(args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45).stdout


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_frame(path):
    pixels = bytearray()
    for y in range(1280):
        pixels.extend(bytes((220, 35, 30) if y < 960 else (35, 70, 160)) * 360)
        pixels.extend(bytes((25, 130, 210) if y < 960 else (100, 160, 25)) * 360)
    glyphs = {
        "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
        "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
        "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
        "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
        "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
        "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
        "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    }
    for offset, letter in enumerate("READ123"):
        for row, line in enumerate(glyphs[letter]):
            for col, bit in enumerate(line):
                if bit != "1":
                    continue
                for dy in range(10):
                    start = ((1040 + row * 10 + dy) * 720 + 100 + offset * 70 + col * 10) * 3
                    pixels[start:start + 30] = bytes((255, 255, 255)) * 10
    path.write_bytes(b"P6\n720 1280\n255\n" + pixels)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument("--encoder", choices=("h264_nvenc", "libx264"), default="h264_nvenc")
    args = parser.parse_args()
    release, work = Path(args.release).absolute(), Path(args.work_dir).absolute()
    if not release.is_dir() or release.is_symlink() or release.resolve() != release:
        raise RuntimeError("release_must_be_real_checkout")
    head = command(["git", "-C", str(release), "rev-parse", "HEAD"]).decode().strip()
    if head != args.expected_sha:
        raise RuntimeError("release_commit_mismatch")
    if work.exists() or not work.parent.is_dir() or work.parent.resolve() != work.parent:
        raise RuntimeError("work_dir_must_be_fresh_with_real_existing_parent")
    if work == release or release in work.parents or work in release.parents or ".runtime" in work.parts:
        raise RuntimeError("work_dir_must_not_overlap_release_or_business_runtime")
    if not work.name.startswith("mirror-canary-"):
        raise RuntimeError("work_dir_name_must_start_with_mirror_canary")
    work.mkdir(mode=0o700)
    os.environ["TMPDIR"] = str(work)
    sys.path.insert(0, str(release))
    from features.drama_synthesis.async_runtime import capture_context
    from features.video_editing.recipe import LEGACY_PROFILE, PROFILE, validate_recipe, recipe_fingerprint
    from features.video_editing.render import (
        build_normalize_command, build_assemble_command, encoder_args, probe_media, run_ffmpeg, validate_output,
    )
    if capture_context() is not None:
        raise RuntimeError("unexpected_business_runtime_context")
    if (LEGACY_PROFILE, PROFILE) != ("video-editing-720-v1", "video-editing-720-v2"):
        raise RuntimeError("compact_profiles_missing")
    frame, source = work / "readable.ppm", work / "source.mp4"
    make_frame(frame)
    source_command = [args.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loop", "1", "-framerate", "30",
        "-i", str(frame), "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.6",
        "-t", "0.6", *encoder_args(args.encoder), "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)]
    run_ffmpeg(source_command, .6, diagnostic_path=work / "source.failure.json")
    metadata = probe_media(source, ffprobe=args.ffprobe)
    media_source = {"source_id": "canary", "kind": "episode", "url": "https://canary.invalid/source.mp4",
        "identity": {"size_bytes": source.stat().st_size, "sha256": sha(source)}, **metadata}
    rendered, recipes = {}, {}
    for name, version, effect in (
        ("plain", 1, {}),
        ("protected", 2, {"mirror": True}),
        ("full", 2, {"mirror": True, "subtitle_band": None}),
        ("top", 2, {"mirror": True, "subtitle_band": {"top": 0, "bottom": 320}}),
    ):
        recipe = validate_recipe({"version": version, "profile": PROFILE, "sources": [media_source],
            "clips": [{"id": name, "source_id": "canary", "start_ms": 0, "end_ms": 600, **effect}]})
        target = work / (name + ".mp4")
        cmd = build_normalize_command(source, recipe["clips"][0], target, has_audio=True,
            ffmpeg=args.ffmpeg, encoder=args.encoder, profile=PROFILE, final=True)
        if cmd[cmd.index("-maxrate") + 1] != "3M":
            raise RuntimeError("compact_final_arguments_missing")
        run_ffmpeg(cmd, .6, diagnostic_path=work / (name + ".failure.json"))
        validate_output(probe_media(target, ffprobe=args.ffprobe), 18)
        rendered[name], recipes[name] = target, recipe

    def decode(path, flip=False):
        cmd = [args.ffmpeg, "-v", "error", "-i", str(path)]
        if flip:
            cmd += ["-vf", "hflip"]
        return command([*cmd, "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])

    def region(data, top, bottom):
        return data[top * 720 * 3:bottom * 720 * 3]

    def error(left, right):
        if len(left) != len(right) or not left:
            raise RuntimeError("decoded_frame_size_mismatch")
        return sum(abs(a - b) for a, b in zip(left, right)) / len(left)

    plain, flipped = decode(rendered["plain"]), decode(rendered["plain"], True)
    protected, full, top = (decode(rendered[key]) for key in ("protected", "full", "top"))
    checks = {
        "outside_band_vs_mirrored": error(region(protected, 4, 956), region(flipped, 4, 956)),
        "subtitle_band_vs_original": error(region(protected, 964, 1276), region(plain, 964, 1276)),
        "whole_frame_vs_mirrored": error(full, flipped),
        "custom_top_band_vs_original": error(region(top, 4, 316), region(plain, 4, 316)),
        "outside_custom_band_vs_mirrored": error(region(top, 324, 1276), region(flipped, 324, 1276)),
    }
    incorrect = error(region(protected, 964, 1276), region(flipped, 964, 1276))
    if any(value >= 6 for value in checks.values()) or incorrect <= 25:
        raise RuntimeError("mirror_or_subtitle_pixel_assertion_failed")
    joined = work / "fade.mp4"
    assembly = {"clips": [{"start_frame": 0, "end_frame": 18, "transition": "fade"},
                          {"start_frame": 0, "end_frame": 18, "transition": "cut"}]}
    cmd = build_assemble_command([rendered["protected"], rendered["top"]], assembly, joined,
        ffmpeg=args.ffmpeg, encoder=args.encoder, profile=PROFILE, final=True)
    run_ffmpeg(cmd, .9, diagnostic_path=work / "fade.failure.json")
    validate_output(probe_media(joined, ffprobe=args.ffprobe), 27)
    command([args.ffmpeg, "-v", "error", "-i", str(joined), "-f", "null", "-"])
    command([args.ffmpeg, "-v", "error", "-i", str(rendered["protected"]), "-frames:v", "1", str(work / "protected.png")])
    result = {"ok": True, "release": head, "encoder": args.encoder, "python": sys.executable,
        "ffmpeg": command([args.ffmpeg, "-version"]).decode().splitlines()[0],
        "profile": PROFILE, "files": {name: sha(release / "features/video_editing" / name)
            for name in ("recipe.py", "render.py", "gpu_integration.py")},
        "mean_absolute_pixel_errors": checks, "subtitle_vs_wrong_mirrored_error": incorrect,
        "recipe_sha256": {name: recipe_fingerprint(value) for name, value in recipes.items()},
        "artifacts": {path.name: {"sha256": sha(path), "bytes": path.stat().st_size}
            for path in (*rendered.values(), joined, work / "protected.png")},
        "business_runtime_context": False, "api_or_cos_or_database_calls": False}
    (work / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "encoder": args.encoder, "result": str(work / "result.json")}, sort_keys=True))


if __name__ == "__main__":
    main()
