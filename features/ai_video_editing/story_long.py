"""Keep consecutive original scenes for a 10–20 minute studio edit."""

import base64
from collections import defaultdict
import math
from pathlib import Path
import zlib

from .render_worker import frame
from .runtime import Fault
from .smart_media import command, local_input
from .story_receipts import cached, remember

VERSION = "continuous-story-1"
MIN_MS, MAX_MS = 600_000, 1_200_000
W, H, RATE = 80, 112, 15
SIZE = W * H


def clock(ms):
    seconds = max(0, ms // 1000)
    return f"{seconds // 60} 分 {seconds % 60:02} 秒"


def shortfall(ms):
    return f"可用剧情最多 {clock(ms)}，距 10 分钟至少还差 {clock(MIN_MS - ms)}。请补选同剧的后续连续集。"


def output_ms(segments):
    frames = sum(frame(s["end_ms"]) - frame(s["start_ms"]) for s in segments)
    return round(frames * 1000 / 30)


def edge_images(path, start, duration):
    return command(
        [
            "-ss",
            f"{start / 1000:.3f}",
            *local_input(path),
            "-t",
            f"{duration / 1000:.3f}",
            "-an",
            "-vf",
            f"fps={RATE}:start_time=0:round=up,scale={W}:{H},format=gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
    )


def profile(rt, actor, source):
    asset = source["asset"]
    key = "cards-1:" + asset["sha256"]
    saved = cached(rt, actor, "episode_edges", key)
    duration = source["duration_ms"]
    start = max(0, duration - 5500)
    if saved is None:
        if asset.get("_cos"):
            from .cloud_media import materialize

            materialize(rt, asset)
        path = Path(asset["path"])
        if not path.is_file():
            raise Fault("media_missing", "无法检查原片，请核对素材后重试", 409)
        data = [edge_images(path, 0, min(duration, 1400)), edge_images(path, start, duration - start)]
        if not all(len(v) >= 2 * SIZE and len(v) % SIZE == 0 for v in data):
            return None
        saved = [base64.b64encode(zlib.compress(v, 2)).decode("ascii") for v in data]
        remember(rt, actor, "episode_edges", key, saved)

    def frames(value):
        raw = zlib.decompress(base64.b64decode(value))
        return [raw[i : i + SIZE] for i in range(0, len(raw), SIZE)]

    return frames(saved[0]), frames(saved[1]), start


def distance(a, b):
    return sum(abs(x - y) for x, y in zip(a[::13], b[::13])) / len(a[::13])


def strokes(pic):
    # Bright letter strokes with a darker neighbour; compare shapes across different episode backgrounds.
    return {
        i
        for y in range(H // 6, H * 4 // 5)
        for i in range(y * W + 4, (y + 1) * W - 4)
        if pic[i] > 145 and pic[i] - min(pic[i - 2], pic[i + 2], pic[i - 2 * W], pic[i + 2 * W]) > 42
    }


def title_template(profiles):
    if len(profiles) < 3:
        return set()
    counts, other = defaultdict(int), defaultdict(int)
    for head, tail, _ in profiles:
        if len(tail) < 15:
            return set()
        for i in strokes(tail[-5]):
            counts[i] += 1
        # Exclude persistent watermarks, letterbox boundaries and burnt-in subtitle styles.
        for i in strokes(head[-1]) | strokes(tail[1]):
            other[i] += 1
    n = len(profiles)
    template = {i for i, hits in counts.items() if hits >= math.ceil(n * 0.8) and other[i] < n * 0.4}
    return template if len(template) >= 25 else set()


def poster_signature(picture):
    # Block means retain the portrait's layout across grading, compression and differently lettered masters.
    grid = [
        sum(sum(picture[row * W + x : row * W + x + 8]) for row in range(y, y + 7)) / 56
        for y in range(0, 70, 7)
        for x in range(0, W, 8)
    ]
    mean = sum(grid) / len(grid)
    centered = [value - mean for value in grid]
    norm = math.sqrt(sum(value * value for value in centered))
    return [value / norm for value in centered] if norm > 50 else []


def bounds(p, peers, template, duration):
    head, tail, tail_start = p
    a, b = 0, duration
    # Distinct episodes must actually share the same opening artwork and move away from it.
    # A different master can move the show logo and the episode number on the poster.
    portrait = poster_signature(head[0])
    matches = [
        h
        for h, _, _ in peers
        if sum(a * b for a, b in zip(portrait, poster_signature(h[0]))) >= 0.975
        and distance(head[-1], h[-1]) > 12
    ]
    # A common black frame or a fade from white alone does not identify a poster.
    textured = len(strokes(head[0])) >= 40
    if textured and matches and len(matches) >= min(2, len(peers)) and distance(head[0], head[-1]) > 15:
        exit_at = next((i for i, f in enumerate(head) if distance(f, head[0]) > 12), None)
        if exit_at:
            a = math.ceil(exit_at * 1000 / RATE) + 34
    if template and len(tail) >= 6:
        scores = [len(template & strokes(f)) / len(template) for f in tail]
        if min(scores[-6:-2]) >= 0.66 and min(scores[:5]) < 0.32:
            # Find the contiguous appearance of this title at the end. Drop its fade-in as well.
            i = len(scores) - 5
            while i > 0 and scores[i - 1] > 0.30:
                i -= 1
            # The glyph is already visible before bright strokes reach this threshold.
            # Use the measured rise of this particular title to cover the translucent start.
            solid = next((j for j in range(i, len(scores)) if scores[j] >= 0.62), i)
            lead = 2 + min(11, solid - i + 2)
            b = tail_start + max(0, math.floor((i - lead) * 1000 / RATE))
    return (a, b) if b - a >= 350 else (0, duration)


def continuous(rt, actor, story):
    spans = {s["source_id"]: (0, s["duration_ms"]) for s in story}
    families = defaultdict(list)
    for s in story:
        if s["identity"] is not None and s["episode"]:
            families[s["identity"]].append(s)
    notes = []
    for family in families.values():
        if len({s["episode"] for s in family}) < 2 or len({s["asset"]["sha256"] for s in family}) < 2:
            continue
        profiles = {}
        for s in family:
            try:
                p = profile(rt, actor, s)
            except (OSError, ValueError):
                p = None
            if p:
                profiles[s["source_id"]] = p
            else:
                notes.append(s["title"] + " 的片头片尾未能核实，已保留原画面。")
        template = title_template(list(profiles.values()))
        for s in family:
            sid = s["source_id"]
            if sid in profiles:
                peers = [p for k, p in profiles.items() if k != sid]
                spans[sid] = bounds(profiles[sid], peers, template, s["duration_ms"])
    if len(story) > 1:
        identities = {s["identity"] for s in story}
        if len(identities) != 1 or None in identities:
            notes.append("素材来自不同剧目或集号未能核实，已沿用所选顺序，请试听跨集衔接。")
        elif all(s["episode"] for s in story):
            for left, right in zip(story, story[1:]):
                if right["episode"] != left["episode"] + 1:
                    notes.append(f"第 {left['episode']} 集后接第 {right['episode']} 集，请核对这个转场。")
    result = []
    for s in story:
        a, b = spans[s["source_id"]]
        evidence = sorted(
            (c for c in s["candidates"] if a <= c.get("anchor_ms", -1) < b), key=lambda c: c["anchor_ms"]
        )
        descriptions = [
            c.get("reason", "") for c in evidence if c.get("engine") == "multimodal" and c.get("reason")
        ]
        reason = (
            " ".join(dict.fromkeys(descriptions[:1] + descriptions[-1:]))
            or "连续保留这一集的人物行动、对话和反应。"
        )
        result.append(
            dict(
                source_id=s["source_id"],
                candidate_id="continuous",
                episode=s["episode"],
                start_ms=a,
                end_ms=b,
                role="连续剧情",
                reason=reason,
                transcript="",
            )
        )
    if result:
        result[0]["role"] = "故事开场"
        if len(result) > 1:
            result[-1]["role"] = "本段收尾"
    return result, notes


def edit(rt, actor, story, clip, script, original=None):
    from . import story_scenes
    from .smart_model import model_config

    raw = sum(s["duration_ms"] for s in story)
    if raw < MIN_MS:
        return [], [], [shortfall(raw)], "请补齐连续剧情素材"
    spans, notes = original if original is not None else continuous(rt, actor, story)
    length = output_ms(spans)
    cap = (clip["seconds"] or MAX_MS // 1000) * 1000
    alternative = bool(clip["opening_take"] + script or clip["ending_take"])
    if length < MIN_MS:
        return spans, notes, [shortfall(length)], "去除分集片头片尾后的可用剧情"
    if length > cap or alternative:
        routes = story_scenes.routes(rt, actor, story, clip) if any(model_config()[:2]) else None
        if routes:
            segments, extra, conflicts, question = story_scenes.cut(routes, story, clip, script)
            # Scene proposals can include pictures from the episode poster; intersect with measured boundaries.
            valid = {s["source_id"]: (s["start_ms"], s["end_ms"]) for s in spans}
            trimmed = []
            for s in segments:
                a, b = valid[s["source_id"]]
                left, right = max(a, s["start_ms"]), min(b, s["end_ms"])
                if frame(left) < frame(right):
                    trimmed.append(dict(s, start_ms=left, end_ms=right))
            if (
                not conflicts
                and MIN_MS <= output_ms(trimmed) <= cap
                and {s["source_id"] for s in trimmed} == set(valid)
            ):
                return trimmed, notes + extra, [], question
        if length > cap:
            return (
                spans,
                notes,
                [
                    f"现有连续场次共 {clock(length)}，暂未找到能保留各集因果且落在 10 分钟至 {clock(cap)} 内的剪法。请调整选集或时长上限。"
                ],
                "需要调整连续剧情的范围",
            )
        notes = notes + ["没有找到能保留前因后果并满足时长的替代首尾，沿用原片连续场次。"]
    return spans, notes, [], "按所选场次连续讲述"
