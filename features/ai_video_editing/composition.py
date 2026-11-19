"""Validated, portable finishing instructions; media stays in the editor store."""

import base64
import hashlib
import json
import re
from pathlib import Path

from .runtime import check, ident, owner, packed

EFFECTS = {
    "basic",
    "mirror",
    "zoom",
    "tilt",
    "border",
    "vignette",
    "ghost",
    "frame_drop",
    "picture_in_picture",
}


def normalize(value, duration):
    check(isinstance(value, dict), "composition_invalid", "字幕和画面配置无效")
    watermark = value.get("watermark", "")
    check(
        isinstance(watermark, str) and len(watermark) <= 30,
        "watermark_invalid",
        "水印最多 30 字",
    )
    effects = value.get("effects", [])
    check(
        isinstance(effects, list) and all(e in EFFECTS for e in effects),
        "effects_invalid",
        "画面处理选项无效",
    )
    style = value.get("style", {})
    check(isinstance(style, dict), "subtitle_style", "字幕样式无效")
    size, height, outline = (
        style.get("size", 36),
        style.get("height", 84),
        style.get("outline", 2),
    )
    check(
        type(size) is int
        and 18 <= size <= 72
        and type(height) is int
        and 10 <= height <= 95
        and type(outline) is int
        and 0 <= outline <= 5,
        "subtitle_style",
        "请检查字幕字号、位置和描边",
    )
    color = style.get("color", "#ffffff")
    check(
        isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color),
        "subtitle_style",
        "字幕颜色无效",
    )
    align = style.get("align", "center")
    check(align in ("left", "center", "right"), "subtitle_style", "字幕对齐无效")
    cues = value.get("cues", [])
    check(
        isinstance(cues, list) and len(cues) <= 1000, "subtitle_limit", "字幕段数量过多"
    )
    clean = []
    for cue in cues:
        a, b, text = cue.get("start"), cue.get("end"), cue.get("text")
        check(
            type(a) is int
            and type(b) is int
            and 0 <= a < b <= duration + 100
            and isinstance(text, str)
            and 0 < len(text) <= 1200,
            "subtitle_invalid",
            "字幕超出成片或内容无效",
        )
        clean.append({"start": a, "end": min(b, duration), "text": text})
    speech = value.get("speech_id", "")
    check(
        isinstance(speech, str)
        and (not speech or re.fullmatch(r"ag_[a-f0-9]{32}", speech)),
        "speech_invalid",
        "音轨编号无效",
    )
    mix = value.get("original_volume", 1)
    check(type(mix) in (float, int) and 0 <= mix <= 1, "audio_invalid", "原声比例无效")
    from .subtitle_protection import settings as protection_settings

    return {
        "subtitle_protection": protection_settings(value.get("subtitle_protection")),
        "watermark": watermark,
        "effects": list(dict.fromkeys(effects)),
        "cues": clean,
        "style": {
            "size": size,
            "height": height,
            "color": color,
            "outline": outline,
            "align": align,
        },
        "speech_id": speech,
        "original_volume": mix,
    }


def setup(rt):
    with rt.db() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS studio_audio(id TEXT PRIMARY KEY,owner TEXT NOT NULL,sha256 TEXT NOT NULL)"
        )
    (rt.root / "audio").mkdir(exist_ok=True)


def store_audio(rt, actor, encoded):
    setup(rt)
    raw = base64.b64decode(encoded, validate=True)
    check(
        raw[:4] == b"RIFF" and len(raw) <= 32 * 1024**2,
        "speech_invalid",
        "配音文件无效",
    )
    aid = ident("ag")
    (rt.root / "audio" / (aid + ".wav")).write_bytes(raw)
    with rt.db(True) as db:
        db.execute(
            "INSERT INTO studio_audio VALUES(?,?,?)",
            (aid, owner(actor), hashlib.sha256(raw).hexdigest()),
        )
    return aid


def audio(rt, aid, key):
    with rt.db() as db:
        row = db.execute(
            "SELECT * FROM studio_audio WHERE id=? AND owner=?", (aid, key)
        ).fetchone()
    check(row, "audio_missing", "音轨不存在或没有权限", 404)
    return rt.root / "audio" / (aid + ".wav"), row["sha256"]


def ass_text(text):
    # User text cannot inject ASS overrides or a new dialogue line.
    return (
        text.replace("\\", "＼")
        .replace("{", "｛")
        .replace("}", "｝")
        .replace("\r", "")
        .replace("\n", r"\N")
    )


def ass_time(ms):
    cs = max(0, int(ms) // 10)
    return "%d:%02d:%02d.%02d" % (
        cs // 360000,
        cs // 6000 % 60,
        cs // 100 % 60,
        cs % 100,
    )


def write_ass(composition, path, duration):
    s = composition["style"]
    rgb = s["color"][1:]
    color = "&H00" + rgb[4:6] + rgb[2:4] + rgb[:2]
    align = {"left": 1, "center": 2, "right": 3}[s["align"]]
    head = "[Script Info]\nScriptType: v4.00+\nPlayResX: 720\nPlayResY: 1280\nWrapStyle: 0\n\n[V4+ Styles]\n"
    head += "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    head += (
        "Style: Default,Noto Sans CJK SC,%s,%s,&H000000FF,&H00111111,&H80000000,0,0,0,0,100,100,0,0,1,%s,1,%s,30,30,%s,1\n"
        % (
            s["size"],
            color,
            s["outline"],
            align,
            round(1280 * (100 - s["height"]) / 100),
        )
    )
    head += "Style: Watermark,Noto Sans CJK SC,26,&H90FFFFFF,&H000000FF,&H80111111,&H80000000,0,0,0,0,100,100,0,0,1,1,0,7,24,24,24,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    rows = [
        "Dialogue: 0,%s,%s,Default,,0,0,0,,%s"
        % (ass_time(c["start"]), ass_time(c["end"]), ass_text(c["text"]))
        for c in composition["cues"]
    ]
    if composition["watermark"]:
        rows.append(
            "Dialogue: 1,0:00:00.00,%s,Watermark,,0,0,0,,%s"
            % (ass_time(duration), ass_text(composition["watermark"]))
        )
    Path(path).write_text(head + "\n".join(rows) + "\n", encoding="utf-8")
