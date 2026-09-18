"""Build the twenty approved PNG / alpha-WebM layers and append-only manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from PIL import Image
from random_subtemplate_artwork import GROUPS, border, atmosphere, corner, tint

BASE_SHA = "028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f"
SIZE = (720, 1280)
FPS, FRAMES = 30, 120


def write_video(path, layer, index, ffmpeg):
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
               "-pix_fmt", "rgba", "-s", "720x1280", "-r", str(FPS), "-i", "pipe:0",
               "-an", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-lossless", "1",
               "-deadline", "good", "-cpu-used", "4", "-threads", "2", "-row-mt", "1",
               "-auto-alt-ref", "0", "-metadata:s:v:0", "alpha_mode=1", "-y", str(path)]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for n in range(FRAMES):
            frame = layer(index, n / FRAMES).resize(SIZE, Image.Resampling.LANCZOS)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        error = proc.stderr.read()
        if proc.wait():
            raise RuntimeError(error.decode("utf-8", "replace"))
    except BaseException:
        proc.kill()
        proc.wait()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "assets/random-subtemplates/20260918")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    output = args.output
    raw = (output / "base-manifest.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != BASE_SHA:
        raise RuntimeError("original_manifest_changed")
    manifest = json.loads(raw)
    layers = output / "layers"
    layers.mkdir(parents=True, exist_ok=True)
    additions = []
    for category, info in GROUPS.items():
        static = category in {"border", "tint"}
        stem = "opacity-video" if category == "opacity_video" else category
        extension = "png" if static else "webm"
        for index, name in enumerate(info["names"]):
            filename = f'{stem}-{info["next"] + index:02}.{extension}'
            path = layers / filename
            if static:
                layer = (border if category == "border" else tint)(index)
                layer.resize(SIZE, Image.Resampling.LANCZOS).save(path)
            else:
                write_video(path, atmosphere if category == "opacity_video" else corner, index, args.ffmpeg)
            row = {"media_type": "image/png" if static else "video/webm", "name": filename,
                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}
            assert not any(old["name"] == filename for old in manifest["categories"][category])
            manifest["categories"][category].append(row)
            additions.append({"id": f'{info["prefix"]}{index+1:02}', "name": name,
                              "category": category, "file": filename, "sha256": row["sha256"]})
            print(json.dumps({"file": filename, "bytes": row["size"]}), flush=True)
    # Category row schemas and original rows remain unchanged. Human-readable
    # style labels are separate so CPU/GPU strict catalog parsers stay compatible.
    encoded = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    (output / "manifest.json").write_bytes(encoded)
    report = {"base_manifest_sha256": BASE_SHA, "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
              "dimensions": list(SIZE), "video_fps": FPS, "video_frames": FRAMES,
              "counts": {k: len(v) for k, v in manifest["categories"].items()}, "additions": additions}
    (output / "additions.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "additions"}), flush=True)


if __name__ == "__main__":
    main()
