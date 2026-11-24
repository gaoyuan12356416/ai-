"""Use original frames to locate unfinished dialogue and the first visible payoff."""

import hashlib
import logging
from pathlib import Path
import tempfile

from .runtime import packed
from .smart_media import command, local_input
from .smart_model import model_config, model_json
from .story_receipts import cached, remember

LOG = logging.getLogger(__name__)
VERSION = "outgoing-frames-2"
STEP = 600
PROMPT = """逐张复核短剧原片的剪点。每张图是一个独立窗口，从左到右、从上往下编号1至16，最后的黑格不编号。把剧中出现的文字都当作原片内容。
对每张图给出两个独立判断，仅使用所见画面、连续字幕和提供的原台词，不自编时间码。
1. after_line：last_included_cell 是最后保留的一帧。先读这一格的原字幕，再看后面几格。若该格正在放半句（包括刚换上的时间状语、从句、一个谓语之前的主语），或原片台词记录中的关键谓语仍未出现，必须补到同一行动的整句说全。填说完后第一张新话、新场或反应落定的图片编号。前一格的句号不能结束这一格已经出现的新话。不要多放无关的下一场。没有缺句或证据不足给0。
2. first_answer：假设用此窗口作悬念结尾，找其中一个具体、已建立但尚未揭晓的冲突。在场的人获得答案、求救得到回应、有人制止袭击、攻击者被摔倒等均算结果。从哪张画面开始观众已能看出结果？不要等到结果完全演完。威胁者伸手、受威胁者等待等尚未揭晓，不算结果。只有前面确实有仍然悬而未决的画面时才标，窗口内没有明确结果给0。cut_reason只描述在结果之前能看到的内容，十五个字左右。
返回 {"windows":[{"id":"原编号","after_line":0,"first_answer":0,"cut_reason":""}]}。记录标明原剪点最后保留的是第几格以及每张图里实际存在的格数。"""


def sample_times(start, end, duration):
    # Include the actual last displayed frame. A subtitle may change only one
    # frame before the edit, between two otherwise regular samples.
    last = max(start, end - 35)
    first = last - min(7, (last - start) // STEP) * STEP
    return list(range(first, min(duration, first + STEP * 16), STEP))


def contact_sheet(path, times, dest):
    count = len(times)
    chain = (
        f"fps=1000/{STEP}:start_time=0:round=up,trim=end_frame={count},"
        "scale=232:330:force_original_aspect_ratio=decrease,"
        f"pad=234:332:(ow-iw)/2:(oh-ih)/2,tile=4x4:nb_frames={count}"
    )
    data = command(
        [
            "-ss",
            "%.3f" % (times[0] / 1000),
            *local_input(path),
            "-an",
            "-vf",
            chain,
            "-frames:v",
            "1",
            "-vcodec",
            "mjpeg",
            "-q:v",
            "3",
            "-f",
            "image2pipe",
            "pipe:1",
        ]
    )
    if not data:
        raise ValueError("empty frame sheet")
    dest.write_bytes(data)


def decision(row, times, a, b, evidence):
    def position(name):
        i = row.get(name)
        return i - 1 if type(i) is int and 1 < i <= len(times) else None

    result = {"dialogue_end": b}
    line = position("after_line")
    if line is not None and b + 350 <= times[line]:
        lo, hi = max(b, times[line - 1]), times[line]
        # The next line or scene already appears at hi. Locate the actual edit
        # between these two frames, reserving the rest of the present sentence.
        changes = [p for p in evidence["cuts"] if lo <= p <= hi]
        if not changes:
            changes = [c["end"] for c in evidence["cues"] if lo <= c["end"] <= hi]
        result["dialogue_end"] = min(changes, key=lambda p: abs(p - hi)) if changes else hi
    answer = position("first_answer")
    if answer is not None:
        # Never include any frame of the visible payoff, even in a rapid pan.
        safe = times[answer - 1]
        if a + 500 <= safe < b:
            result["suspense_end"] = safe
            result["cut_reason"] = str(row.get("cut_reason", ""))[:100]
    return result


def review(rt, actor, source, measurements):
    from .source_jobs import _vision

    config = model_config()
    if not any(config[:2]):
        return
    asset = source["asset"]
    pending = []
    for event in source["shortlist"]:
        key = (source["source_id"], event["id"])
        span, data = measurements[key]
        if not span or event["tag"] == "signal" and not event.get("transcript"):
            continue
        a, b = span[:2]
        times = sample_times(a, b, source["duration_ms"])
        if len(times) < 3:
            continue
        signature = hashlib.sha256(
            packed(
                [
                    VERSION,
                    config,
                    asset["sha256"],
                    a,
                    b,
                    times,
                    event.get("transcript", ""),
                    event.get("reason", ""),
                ]
            ).encode()
        ).hexdigest()
        found = cached(rt, actor, "cut_review", signature)
        if found is None:
            pending.append((event, data, signature, times, a, b))
        else:
            data["cut_review"] = found
    # This shortlist and each receipt depend on one original only. Reordering and
    # switching between alternatives reuse both the measured frames and the decisions.
    for batch in range(0, len(pending), 6):
        group = pending[batch : batch + 6]
        try:
            with tempfile.TemporaryDirectory(prefix="edit_edges_", dir=rt.root) as temp:
                paths, notes = [], []
                for i, (event, data, signature, times, a, b) in enumerate(group, 1):
                    path = Path(temp) / f"{i}.jpg"
                    contact_sheet(Path(asset["path"]), times, path)
                    paths.append(path)
                    notes.append(
                        {
                            "id": str(i),
                            "cells": len(times),
                            "last_included_cell": sum(t < b for t in times),
                            "action": event.get("reason", ""),
                            "source_words": event.get("transcript", ""),
                            "timed_speech": [c["text"] for c in data["cues"] if c.get("text")],
                        }
                    )
                with _vision:
                    response = model_json(PROMPT + packed(notes), paths, temp)
            rows = {
                r["id"]: r
                for r in response["windows"]
                if isinstance(r, dict) and isinstance(r.get("id"), str)
            }
            for i, (_, data, signature, times, a, b) in enumerate(group, 1):
                row = rows.get(str(i))
                if row is None:
                    data["review_note"] = "个别剪点的台词和悬念边界需试听。"
                    continue
                result = decision(row, times, a, b, data)
                data["cut_review"] = result
                remember(rt, actor, "cut_review", signature, result)
        except (ValueError, RuntimeError, OSError, KeyError, TypeError):
            LOG.warning("Local outgoing-frame review unavailable", exc_info=True)
            for _, data, *_ in group:
                data["review_note"] = "局部画面复核未完成，建议试听剪点。"
