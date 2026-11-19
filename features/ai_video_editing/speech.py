"""Natural speech, measured timing and bounded fitting; no robotic fallback."""

from array import array
import base64
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import wave

from .smart_common import SmartError, require

VOICES = {
    "en": "en-US-GuyNeural",
    "zh": "zh-CN-YunxiNeural",
    "es": "es-ES-AlvaroNeural",
    "fr": "fr-FR-HenriNeural",
    "de": "de-DE-ConradNeural",
    "it": "it-IT-DiegoNeural",
    "pt": "pt-BR-AntonioNeural",
    "ja": "ja-JP-KeitaNeural",
    "ko": "ko-KR-InJoonNeural",
    "id": "id-ID-ArdiNeural",
    "vi": "vi-VN-NamMinhNeural",
    "ru": "ru-RU-DmitryNeural",
    "tr": "tr-TR-AhmetNeural",
    "ar": "ar-SA-HamedNeural",
    "hi": "hi-IN-MadhurNeural",
}


def executable():
    path = os.getenv(
        "AI_VIDEO_EDITING_TTS_PYTHON",
        "/mnt/data-disk/ai-video-editing-deploy/speech-venv/bin/python",
    )
    return path if Path(path).is_file() else ""


def caption_groups(text, words):
    """Retain source punctuation while grouping actual word boundary timings."""
    aligned, cursor = [], 0
    for word in words:
        position = text.casefold().find(word["text"].casefold(), cursor)
        require(position >= 0, "speech_alignment", "配音文字对齐失败，请重新生成", 502)
        aligned.append({**word, "left": cursor, "right": position + len(word["text"])})
        cursor = position + len(word["text"])
    require(aligned, "speech_alignment", "配音缺少时间点，请重新生成", 502)
    for i, word in enumerate(aligned):
        # Include punctuation immediately after a word, not as an orphan next cue.
        right = word["right"]
        while right < len(text) and (
            text[right].isspace() or text[right] in ',.!?;:，。！？；：”’"』」'
        ):
            right += 1
        word["right"] = right
        if i + 1 < len(aligned):
            aligned[i + 1]["left"] = max(aligned[i + 1]["left"], right)
    limit = 22 if re.search(r"[\u3040-\u9fff\uac00-\ud7af]", text) else 54
    groups, first = [], 0
    for i, word in enumerate(aligned):
        fragment = text[aligned[first]["left"] : word["right"]].strip()
        final = i == len(aligned) - 1
        next_length = (
            0
            if final
            else len(text[aligned[first]["left"] : aligned[i + 1]["right"]].strip())
        )
        punctuation = bool(re.search(r'[.!?。！？][”’"』」]*$', fragment))
        if (
            final
            or next_length > limit
            or word["end"] - aligned[first]["start"] >= 3000
            or punctuation
        ):
            groups.append(
                {"start": aligned[first]["start"], "end": word["end"], "text": fragment}
            )
            first = i + 1
    joined = []
    for cue in groups:
        if (joined and len(cue["text"]) <= 12 and
                not re.search(r'[.!?。！？][”’"』」]*$', joined[-1]["text"]) and
                len(joined[-1]["text"] + cue["text"]) <= 76):
            joined[-1]["text"] += ("" if limit == 22 else " ") + cue["text"]
            joined[-1]["end"] = cue["end"]
        else:
            joined.append(cue)
    return joined


def render_voice(text, language, folder, deadline):
    from .smart_media import command

    exe = executable()
    require(exe, "speech_unavailable", "自然配音服务尚未配置", 503)
    target = folder / "voice.mp3"
    try:
        run = subprocess.run(
            [exe, str(Path(__file__).with_name("neural_voice.py")), str(target)],
            input=json.dumps({"text": text, "voice": VOICES[language]}).encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=max(1, min(60, deadline - time.monotonic())),
        )
        require(
            run.returncode == 0 and target.is_file() and target.stat().st_size > 100,
            "speech_failed",
            "自然配音暂未完成，请稍后重试；不会降级为机械语音",
            503,
        )
        words = json.loads(run.stdout)
        raw = command(
            [
                "-i",
                str(target),
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-ar",
                "24000",
                "-ac",
                "1",
                "-f",
                "s16le",
                "pipe:1",
            ],
            timeout=25,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        raise SmartError("speech_failed", "自然配音暂未完成，请稍后重试", 503) from None
    require(raw, "speech_empty", "自然配音返回空音轨", 502)
    return raw, caption_groups(text, words)


def synthesize(payload):
    from .language_tools import _validated_cues, SPEECH_SLOTS
    from .smart_media import command

    require(isinstance(payload, dict), "speech_input", "请输入配音参数")
    language = payload.get("language")
    require(language in VOICES, "speech_language", "请选择有效配音语种")
    timed = "cues" in payload
    cues = (
        _validated_cues(payload["cues"])
        if timed
        else [{"start": 0, "end": 0, "text": payload.get("text", "")}]
    )
    require(
        all(isinstance(c["text"], str) and c["text"].strip() for c in cues)
        and sum(len(c["text"]) for c in cues) <= 6000
        and len(cues) <= 90,
        "speech_limit",
        "配音支持 90 段和 6000 字以内",
    )
    require(
        not timed or max(c["end"] for c in cues) <= 600000,
        "speech_limit",
        "配音最长 10 分钟",
    )
    require(
        SPEECH_SLOTS.acquire(blocking=False),
        "speech_busy",
        "配音服务忙，请稍后重试",
        429,
    )
    try:
        rate, deadline = 24000, time.monotonic() + 240
        length = max(c["end"] for c in cues) * rate // 1000 if timed else 0
        track, captions = array("h", [0]) * length, []
        with tempfile.TemporaryDirectory(prefix="ai-editor-neural-") as temp:
            for cue in cues:
                raw, words = render_voice(cue["text"], language, Path(temp), deadline)
                duration = len(raw) / 2 / rate
                span = (cue["end"] - cue["start"]) / 1000 if timed else duration
                scale = max(1.0, duration / span)
                require(
                    scale <= 1.12,
                    "speech_too_long",
                    "解说稿超出自然语速可读时长，请缩短脚本或增加时长",
                    422,
                )
                if scale > 1.001:
                    source = Path(temp) / "natural.raw"
                    source.write_bytes(raw)
                    raw = command(
                        [
                            "-f",
                            "s16le",
                            "-ar",
                            str(rate),
                            "-ac",
                            "1",
                            "-i",
                            str(source),
                            "-af",
                            "atempo=%.7f" % scale,
                            "-ar",
                            str(rate),
                            "-ac",
                            "1",
                            "-f",
                            "s16le",
                            "pipe:1",
                        ],
                        timeout=20,
                    )
                samples = array("h")
                samples.frombytes(raw)
                start = cue["start"] * rate // 1000
                if not timed:
                    track = samples
                else:
                    count = min(len(samples), len(track) - start, round(span * rate))
                    track[start : start + count] = samples[:count]
                for word in words:
                    a = cue["start"] + round(word["start"] / scale)
                    b = cue["start"] + round(word["end"] / scale)
                    if timed:
                        b = min(b, cue["end"])
                    if b > a:
                        captions.append(dict(word, start=a, end=b))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(track.tobytes())
        return {
            "content_type": "audio/wav",
            "language": language,
            "duration_ms": round(len(track) / rate * 1000),
            "audio_base64": base64.b64encode(buf.getvalue()).decode(),
            "cues": captions,
            "engine": "neural",
        }
    finally:
        SPEECH_SLOTS.release()
