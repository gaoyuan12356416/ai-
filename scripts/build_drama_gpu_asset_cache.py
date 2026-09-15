#!/usr/bin/env python3
"""Build bounded, verified RGBA assets offline; no worker/business APIs."""
from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.drama_synthesis.asset_cache import (
    VERSION, MAX_ITEM_BYTES, MAX_TOTAL_BYTES, MIN_FREE_BYTES, cache_root, verified_entry,
)
from features.fb_gpu.random_overlay import load_asset_set, sha256_file


def frame_hashes(ffmpeg, source, *, original=False):
    options = (["-c:v", "libvpx-vp9"] if source.suffix == ".webm" else ["-framerate", "30"]) if original else []
    frames = ["-frames:v", "1"] if source.suffix == ".png" else []
    command = [ffmpeg, "-nostdin", "-v", "error", "-filter_threads", "2", "-threads", "2",
               *options, "-i", str(source), "-map", "0:v:0", "-an", "-vf", "format=rgba",
               "-fps_mode", "passthrough", *frames, "-enc_time_base", "demux", "-f", "framemd5", "-"]
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=600)
    lines = result.stdout.splitlines()
    tb = Fraction(next(line.split(":", 1)[1].strip() for line in lines if line.startswith("#tb 0:")))
    rows = [line.split(",") for line in lines if line and not line.startswith("#")]
    return [(Fraction(row[2].strip())*tb, Fraction(row[3].strip())*tb,
             row[4].strip(), row[5].strip()) for row in rows]


def remux_pixels(seed, output, expected):
    """Preserve both PTS and packet duration without FFmpeg CLI rate coercion."""
    import av
    denominator = math.lcm(*(value.denominator for row in expected for value in row[:2]))
    time_base = Fraction(1, denominator)
    written, count = 0, 0
    with av.open(str(seed)) as source, av.open(str(output), "w", "nut") as target:
        source_stream = source.streams.video[0]
        if (source_stream.codec_context.name != "rawvideo" or source_stream.codec_context.format.name != "rgba"
                or (source_stream.width, source_stream.height) != (720, 1280)):
            raise RuntimeError("drama_asset_seed_format_invalid")
        stream = target.add_stream("rawvideo", rate=1/expected[0][1])
        stream.width, stream.height, stream.pix_fmt = 720, 1280, "rgba"
        stream.time_base = stream.codec_context.time_base = time_base
        for packet in source.demux(source_stream):
            if not packet.size:
                continue
            if count >= len(expected) or packet.size != int(expected[count][2]):
                raise RuntimeError("drama_asset_seed_frame_count_invalid")
            if hashlib.md5(packet).hexdigest() != expected[count][3]:
                raise RuntimeError("drama_asset_seed_pixels_invalid")
            written += packet.size + 1024  # Conservative per-packet header reserve.
            if written + 65536 > MAX_ITEM_BYTES:
                raise RuntimeError("drama_asset_item_capacity_exceeded")
            timestamp, duration = expected[count][:2]
            packet.pts = packet.dts = int(timestamp/time_base)
            packet.duration = int(duration/time_base)
            packet.time_base = time_base
            packet.stream = stream
            target.mux(packet)
            count += 1
        if count != len(expected):
            raise RuntimeError("drama_asset_seed_frame_count_invalid")


def build(ffmpeg, item, root, *, verify_only=False, seed_root=None):
    source, key = Path(item["path"]), item["sha256"]
    target, receipt = root/(key+".nut"), root/(key+".json")
    if target.exists() or receipt.exists() or target.is_symlink() or receipt.is_symlink():
        entry = verified_entry(root, key)
        return {"source": source.name, "reused": True, "size": entry["size"], "frames": entry["frames"]}
    if verify_only:
        raise RuntimeError("drama_asset_cache_missing")
    partial = root/(key+".partial")
    if partial.exists() or partial.is_symlink():
        raise RuntimeError("drama_asset_partial_requires_inspection")
    used = sum(path.stat().st_size for path in root.iterdir() if path.is_file())
    if used + MAX_ITEM_BYTES > MAX_TOTAL_BYTES or shutil.disk_usage(root).free < MIN_FREE_BYTES + MAX_ITEM_BYTES:
        raise RuntimeError("drama_asset_cache_capacity_exceeded")
    options = ["-c:v", "libvpx-vp9"] if source.suffix == ".webm" else ["-framerate", "30"]
    frames = ["-frames:v", "1"] if source.suffix == ".png" else []
    started = time.monotonic()
    expected = frame_hashes(ffmpeg, source, original=True)
    durations = {row[1] for row in expected}
    if len(durations) != 1 or not expected or expected[0][1] <= 0:
        raise RuntimeError("drama_asset_packet_duration_unsupported")
    # NUT reconstructs packet duration from the rate hint. Matroska's 30 fps
    # assets actually carry 33 ms durations, not 1/30 s; retaining this hint is
    # necessary for stream_loop to restart at the exact original timestamp.
    seed = seed_root/(key+".nut") if seed_root is not None else None
    owns_seed = seed is None
    if seed is None:
        seed = root/(key+".decoded.nut")
        if seed.exists() or seed.is_symlink():
            raise RuntimeError("drama_asset_partial_requires_inspection")
        if used + 2*MAX_ITEM_BYTES > MAX_TOTAL_BYTES or shutil.disk_usage(root).free < MIN_FREE_BYTES+2*MAX_ITEM_BYTES:
            raise RuntimeError("drama_asset_cache_capacity_exceeded")
        subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-filter_threads", "2", "-threads", "2",
            *options, "-i", str(source), "-map", "0:v:0", "-an", "-vf", "format=rgba",
            "-fps_mode", "passthrough", *frames, "-enc_time_base", "demux", "-c:v", "rawvideo", "-threads", "2",
            "-fs", str(MAX_ITEM_BYTES), "-f", "nut", str(seed)],
            check=True, capture_output=True, timeout=600)
    if seed.is_symlink() or not seed.is_file() or seed.stat().st_size > MAX_ITEM_BYTES:
        raise RuntimeError("drama_asset_seed_invalid")
    remux_pixels(seed, partial, expected)
    actual = frame_hashes(ffmpeg, partial)
    if not expected or expected != actual or sha256_file(source)[0] != key:
        raise RuntimeError("drama_asset_pixel_or_timestamp_mismatch")
    if not 0 < partial.stat().st_size <= MAX_ITEM_BYTES:
        raise RuntimeError("drama_asset_item_capacity_exceeded")
    with partial.open("rb") as handle:
        os.fsync(handle.fileno())
    os.rename(partial, target)
    if owns_seed:
        seed.unlink()
    target.chmod(0o444)
    stat = target.stat()
    record = {"version": VERSION, "source_sha256": key, "sha256": sha256_file(target)[0],
              "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "frames": len(actual),
              "rgba_frames_verified": True, "build_seconds": time.monotonic()-started}
    record["timing_basis"] = "demux"
    temporary = receipt.with_suffix(".json.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(record, indent=2)+"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.rename(temporary, receipt)
    receipt.chmod(0o444)
    descriptor = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {"source": source.name, "reused": False, "size": stat.st_size, "frames": len(actual)}


def main(argv=None):
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--assets", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--seed-root", help="Read-only RGBA seed; every pixel is reverified against the source")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if (not root.is_absolute() or not root.resolve().is_relative_to(Path("/data"))
            or root == Path("/data") or root.is_symlink() or os.geteuid() != 0):
        raise SystemExit("drama_asset_cache_builder_root_invalid")
    if not args.verify_only:
        root.mkdir(mode=0o755, parents=True, exist_ok=True)
    root = cache_root(root)
    assets = load_asset_set(Path(args.assets), args.manifest_sha256)
    seed_root = cache_root(args.seed_root) if args.seed_root else None
    # Verification is read-only, including the lock file of the shared cache.
    lock = None
    try:
        if not args.verify_only:
            lock = (root/".build.lock").open("a")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for category in ("border", "opacity_video", "corners", "tint"):
            for item in assets["categories"][category]:
                print(json.dumps(build(args.ffmpeg, item, root, verify_only=args.verify_only, seed_root=seed_root)), flush=True)
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    main()
