#!/usr/bin/env python3
"""Convert the approved TT template export into transparent GPU assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


CATEGORY_RULES = (
    ("opacity_video", "视频-透明度5%", ".gif", 5, "video/webm"),
    ("corners", "四角会动", ".gif", 3, "video/webm"),
    ("light", "光效", ".gif", 2, "video/webm"),
    ("border", "边框", ".png", 3, "image/png"),
    ("tint", "透明底", ".png", 7, "image/png"),
)


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def run(command: list[str]) -> None:
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "command failed (%s): %s"
            % (completed.returncode, completed.stderr[-4000:])
        )


def discover(source_root: Path) -> dict[str, list[Path]]:
    files = [item for item in source_root.iterdir() if item.is_file()]
    result = {}
    claimed = set()
    for category, prefix, suffix, expected_count, _ in CATEGORY_RULES:
        matched = sorted(
            (
                item
                for item in files
                if item.name.startswith(prefix)
                and item.suffix.lower() == suffix
            ),
            key=lambda item: item.name,
        )
        if len(matched) != expected_count:
            raise RuntimeError(
                "%s requires exactly %s files, found %s"
                % (category, expected_count, len(matched))
            )
        result[category] = matched
        claimed.update(matched)
    if len(claimed) != 20:
        raise RuntimeError("the approved source set must contain exactly 20 assets")
    return result


def png_command(ffmpeg: str, source: Path, output: Path, category: str) -> list[str]:
    filters = ["scale=720:1280:flags=lanczos", "format=rgba"]
    if category == "tint":
        # Runtime opacity is absolute 10%-20%, so remove embedded export alpha.
        filters.append("lut=a=255")
    return [
        ffmpeg,
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-vf",
        ",".join(filters),
        "-frames:v",
        "1",
        str(output),
    ]


def webm_command(ffmpeg: str, source: Path, output: Path, category: str) -> list[str]:
    if category == "light":
        alpha_filter = "format=rgba,colorkey=0x000000:0.02:0.10"
    elif category == "opacity_video":
        # The exports are approximately 5%-opacity RGB over black. Remove the
        # black matte, restore RGB, and cap the resulting true alpha near 5%.
        alpha_filter = (
            "format=rgba,colorkey=0x000000:0.005:0.02,"
            "lutrgb=r='min(val*20,255)':g='min(val*20,255)':"
            "b='min(val*20,255)':a='min(val,13)'"
        )
    else:
        alpha_filter = "format=rgba"
    return [
        ffmpeg,
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-an",
        "-vf",
        "%s,scale=720:1280:flags=lanczos,fps=30,format=yuva420p"
        % alpha_filter,
        "-c:v",
        "libvpx-vp9",
        "-pix_fmt",
        "yuva420p",
        "-auto-alt-ref",
        "0",
        "-row-mt",
        "1",
        "-deadline",
        "realtime",
        "-cpu-used",
        "8",
        "-crf",
        "20",
        "-b:v",
        "0",
        str(output),
    ]


def probe(ffprobe: str, path: Path, media_type: str) -> None:
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:stream_tags=alpha_mode",
            "-of",
            "json",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("ffprobe failed for %s" % path.name)
    payload = json.loads(completed.stdout)
    streams = payload.get("streams") or []
    if len(streams) != 1 or (
        int(streams[0].get("width") or 0),
        int(streams[0].get("height") or 0),
    ) != (720, 1280):
        raise RuntimeError("asset dimensions are invalid: %s" % path.name)
    if media_type == "video/webm" and str(
        (streams[0].get("tags") or {}).get("alpha_mode") or ""
    ) != "1":
        raise RuntimeError("WebM alpha flag is missing: %s" % path.name)


def build(source_root: Path, output_root: Path, ffmpeg: str, ffprobe: str) -> str:
    if not source_root.is_dir():
        raise RuntimeError("source directory is missing")
    if output_root.exists():
        raise RuntimeError("output directory already exists")
    sources = discover(source_root)
    staging = output_root.with_name(output_root.name + ".building-%s" % os.getpid())
    if staging.exists():
        raise RuntimeError("staging directory already exists")
    staging.mkdir(parents=True, mode=0o700)
    try:
        categories = {}
        source_contract = {}
        for category, _, _, _, media_type in CATEGORY_RULES:
            rows = []
            for index, source in enumerate(sources[category], start=1):
                extension = ".png" if media_type == "image/png" else ".webm"
                name = "%s-%02d%s" % (category.replace("_", "-"), index, extension)
                output = staging / name
                command = (
                    png_command(ffmpeg, source, output, category)
                    if media_type == "image/png"
                    else webm_command(ffmpeg, source, output, category)
                )
                run(command)
                probe(ffprobe, output, media_type)
                output_sha, output_size = sha256_file(output)
                source_sha, source_size = sha256_file(source)
                rows.append(
                    {
                        "media_type": media_type,
                        "name": name,
                        "sha256": output_sha,
                        "size": output_size,
                    }
                )
                source_contract[source.name] = {
                    "category": category,
                    "sha256": source_sha,
                    "size": source_size,
                }
            categories[category] = rows
        manifest = {
            "categories": categories,
            "conversion": {
                "canvas": "720x1280",
                "light_black_key": "0.02/0.10",
                "opacity_video_alpha_cap": "13/255",
                "tint_runtime_opacity_bp": [1000, 2000],
            },
            "sources": source_contract,
            "version": 1,
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        manifest_sha, _ = sha256_file(manifest_path)
        staging.rename(output_root)
        return manifest_sha
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()
    try:
        manifest_sha = build(
            args.source.resolve(),
            args.output.resolve(),
            args.ffmpeg,
            args.ffprobe,
        )
    except Exception as exc:
        print("asset build failed: %s" % exc, file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "manifest_sha256": manifest_sha,
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
