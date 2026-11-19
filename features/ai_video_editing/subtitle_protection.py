"""Keep the original subtitle band readable while mirroring the rest of a frame."""

import math
from pathlib import Path
import shutil
import subprocess
import tempfile

from .runtime import check


def settings(value=None):
    value = {} if value is None else value
    check(isinstance(value, dict), "subtitle_protection", "字幕保护配置无效")
    enabled, mode = value.get("enabled", True), value.get("mode", "auto")
    top, bottom = value.get("top", 55), value.get("bottom", 92)
    check(
        type(enabled) is bool
        and mode in ("auto", "manual")
        and type(top) is int
        and type(bottom) is int
        and 0 <= top < bottom <= 100,
        "subtitle_protection",
        "请检查字幕保护区域的上下边界",
    )
    return {"enabled": enabled, "mode": mode, "top": top, "bottom": bottom}


def resolve(rt, actor, clips, value):
    result = settings(value)
    if not result["enabled"] or result["mode"] == "manual":
        return result
    exe = shutil.which("tesseract")
    if not exe:
        return result
    candidates = []
    for c in clips:
        for ratio in (0.25, 0.65):
            candidates.append(
                (
                    c["source_id"],
                    (c["start_ms"] + (c["end_ms"] - c["start_ms"]) * ratio) / 1000,
                )
            )
    candidates = candidates[:8]
    bounds = []
    with tempfile.TemporaryDirectory(prefix="ai-editor-subtitle-") as temp:
        for sid, seconds in candidates:
            frame = Path(temp) / "frame.png"
            source = rt.asset(sid, actor)
            try:
                run = subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-y",
                        "-ss",
                        str(seconds),
                        "-i",
                        source["path"],
                        "-vf",
                        "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,crop=720:640:0:576,format=gray,lut=y='if(gte(val,205),0,255)'",
                        "-frames:v",
                        "1",
                        str(frame),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
                if run.returncode:
                    continue
                ocr = subprocess.run(
                    [exe, str(frame), "stdout", "-l", "eng", "--psm", "11", "tsv"],
                    capture_output=True,
                    timeout=8,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            for line in ocr.stdout.decode("utf8", "replace").splitlines()[1:]:
                columns = line.split("\t", 11)
                try:
                    if (
                        len(columns) == 12
                        and columns[0] == "5"
                        and float(columns[10]) >= 60
                        and len(columns[11].strip()) >= 2
                    ):
                        y, height = int(columns[7]) + 576, int(columns[9])
                        if 10 <= height <= 90:
                            bounds.append((y, y + height))
                except ValueError:
                    continue
    if len(bounds) >= 3:
        result["top"] = max(0, math.floor(min(a for a, b in bounds) / 12.8) - 2)
        result["bottom"] = min(100, math.ceil(max(b for a, b in bounds) / 12.8) + 2)
    return result
