"""Source-relative edit boundaries measured only around shortlisted evidence."""

import array
import hashlib
import os
from pathlib import Path
import re
import shutil
import sys

from .runtime import Fault, packed
from .smart_media import command, local_input

VERSION = "local-edges-2"
STEP = 50
WIDTH, HEIGHT = 64, 96


def metrics(path, start, end, has_audio):
    args = ["-ss", "%.3f" % (start / 1000), *local_input(path), "-t", "%.3f" % ((end - start) / 1000)]
    # A small grayscale image preserves shot changes and high-contrast subtitle glyphs.
    raw = command(
        [*args, "-an", "-vf", f"fps=20,scale={WIDTH}:{HEIGHT},format=gray", "-f", "rawvideo", "pipe:1"]
    )
    size = WIDTH * HEIGHT
    cuts, captions, signatures = [], [], []
    previous = previous_band = None
    for pos in range(0, len(raw) - size + 1, size):
        frame = raw[pos : pos + size]
        timestamp = start + pos // size * STEP
        grid = frame[::7]
        band = frame[size * 68 // 100 : size * 93 // 100]
        mask = bytes(1 if a > 170 and a - b > 36 else 0 for a, b in zip(band[1:], band))
        if previous is not None:
            delta = sum(abs(a - b) for a, b in zip(grid, previous)) / len(grid)
            changed = sum(a ^ b for a, b in zip(mask, previous_band))
            if delta >= 28:
                cuts.append(timestamp)
            elif changed >= max(8, 0.54 * max(sum(mask), sum(previous_band))):
                captions.append(timestamp)
        if (pos // size) % 5 == 0:
            # Small spatial signature is used only alongside matching speech/text.
            sample = [frame[y * WIDTH + x] for y in range(8, 88, 10) for x in range(4, 64, 8)]
            mean = sum(sample) / len(sample)
            signatures.append([timestamp, format(sum((v > mean) << i for i, v in enumerate(sample)), "016x")])
        previous, previous_band = grid, mask
    quiet = []
    if has_audio:
        pcm = array.array("h")
        pcm.frombytes(command([*args, "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"]))
        if sys.byteorder != "little":
            pcm.byteswap()
        levels = [
            (start + i // 8, sum(v * v for v in chunk) / len(chunk))
            for i in range(0, len(pcm), 400)
            if (chunk := pcm[i : i + 400])
        ]
        if levels:
            threshold = min(1200**2, max(80**2, sorted(v for _, v in levels)[len(levels) // 5]))
            quiet = [t for t, level in levels if level <= threshold]
    return {"cuts": cuts, "captions": captions, "quiet": quiet, "signatures": signatures, "cues": []}


def boundaries(rt, actor, asset, event):
    start = max(0, event["start_ms"] - 2600)
    end = min(asset["duration_ms"], event["end_ms"] + 2600)
    key = hashlib.sha256(
        packed([VERSION, asset["sha256"], asset["has_audio"], start, end]).encode()
    ).hexdigest()
    from .story_receipts import cached, remember

    found = cached(rt, actor, "edges", key)
    if found is not None:
        return found
    if asset.get("_cos"):
        from .cloud_media import materialize

        materialize(rt, asset)
    path = Path(asset["path"])
    try:
        data = metrics(path, start, end, asset["has_audio"])
    except (OSError, ValueError, RuntimeError):
        # A missing original cannot be rendered. If only optional decoding failed,
        # keep the verified analysis window and disclose its lower precision.
        if not path.is_file():
            raise Fault("media_missing", "原片暂不可读取，请核对素材后重试", 409)
        return {
            "cuts": [],
            "captions": [],
            "quiet": [],
            "signatures": [],
            "cues": [],
            "speech_note": "局部剪点检测未完成，已采用原解析区间，请试听。",
        }

    # Timed captions/ASR can establish a better sentence edge than a volume trough.
    # For burned-in-only sources the visual subtitle-band transitions remain available.
    tracks_key = asset["sha256"]
    embedded = cached(rt, actor, "caption_track", tracks_key)
    if embedded is None:
        from .language_media import probe

        try:
            _, streams = probe(path)
            embedded = any(s.get("codec_type") == "subtitle" for s in streams)
        except (ValueError, OSError, Fault):
            embedded = False
        remember(rt, actor, "caption_track", tracks_key, embedded)
    endpoints_have_pause = all(
        any(abs(p - edge) < 350 for p in data["quiet"]) for edge in (event["start_ms"], event["end_ms"])
    )
    if embedded or asset["has_audio"] and (not event.get("transcript") or not endpoints_have_pause):
        whisper = os.environ.get("AI_VIDEO_EDITING_WHISPER_MODEL", "")
        if embedded or (
            whisper
            and Path(whisper).is_file()
            and shutil.which(os.environ.get("AI_VIDEO_EDITING_WHISPER_BIN", "whisper-cli"))
        ):
            from .language_media import transcribe

            try:
                data["cues"] = transcribe(path, ranges=[(start, end)], temp_dir=rt.root)["cues"]
            except (OSError, ValueError, RuntimeError, Fault) as exc:
                data["speech_note"] = getattr(exc, "message", "局部台词识别不可用")
    remember(rt, actor, "edges", key, data)
    return data


def snap(event, data, duration):
    begin, end = event["start_ms"], event["end_ms"]
    anchor = min(end - 1, max(begin, event["anchor_ms"]))
    evidence = event.get("reason", "") + event.get("category", "")

    def edge(t, lower, upper, is_end):
        choices = []

        def add(points, weight):
            for point in points:
                if lower <= point <= upper:
                    choices.append((abs(point - t) / weight, point))

        add(data["cuts"], 3.2)
        # Constrain glyph flicker to stable subtitle transitions close to a quiet point.
        add((p for p in data["captions"] if any(abs(p - q) <= 100 for q in data["quiet"])), 1.3)
        add(data["quiet"], 0.6)
        add((cue["end" if is_end else "start"] for cue in data["cues"]), 4)
        # Prefer a complete neighboring shot when near an evidence-window edge.
        near = [p for p in data["cuts"] if lower <= p <= upper and abs(p - t) <= 1250]
        if data["cues"]:
            for cue in data["cues"]:
                if cue["start"] < t < cue["end"]:
                    bound = cue["end" if is_end else "start"]
                    if lower <= bound <= upper:
                        return bound
            near = [p for p in near if not any(c["start"] + 50 < p < c["end"] - 50 for c in data["cues"])]
        if near:
            return min(near, key=lambda p: abs(p - t))
        return min(choices)[1] if choices else t

    a = edge(begin, max(0, begin - 2100), min(anchor - 100, begin + 1000), False)
    b = edge(end, max(anchor + 100, end - 1100), min(duration, end + 2100), True)
    cuts = data["cuts"]
    if begin < 4000 and re.search(r"封面|片头|海报|集数文字|集数卡|第.{1,4}集.{0,4}(?:字样|字卡)", evidence):
        interior = [p for p in cuts if 100 <= p < b - 400]
        if not interior:
            return None
        a = max(a, min(interior))
    if duration - end < 4500 and re.search(r"片尾|片名|剧名|标题卡|剧终", evidence):
        interior = [p for p in cuts if a + 400 < p < duration - 120]
        if not interior:
            return None
        b = min(b, max(interior))
    if b <= a or b - a < min(500, duration):
        return None
    signature = (
        [
            min(data["signatures"], key=lambda x: abs(x[0] - (a + (b - a) * fraction)))[1]
            for fraction in (0.25, 0.5, 0.75)
        ]
        if data["signatures"]
        else []
    )
    return a, b, signature
