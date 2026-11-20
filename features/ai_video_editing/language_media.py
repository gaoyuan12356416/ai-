"""Read captions or sample pixels only from a caller-resolved private media file."""
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from array import array
import sys
from difflib import SequenceMatcher
from pathlib import Path

from .smart_common import SmartError, require
from .smart_media import command, local_input

OCR_LANGS = {"en":"eng","es":"spa","zh":"chi_sim","fr":"fra","de":"deu","it":"ita","pt":"por",
             "ja":"jpn","ko":"kor","id":"ind","vi":"vie","ru":"rus","tr":"tur","ar":"ara","hi":"hin"}

# Each decode/recognizer invocation has its own bounded media and time budget.
# OCR retains one sample per second instead of thinning an entire long movie.
CHUNK_MS = 60000


def selected_ranges(ranges, duration_ms):
    if ranges is None:
        return [(0, duration_ms)]
    require(isinstance(ranges, (list, tuple)) and ranges, "transcript_ranges", "请选择有效的识别区间", 422)
    clean = []
    for row in ranges:
        require(isinstance(row, (list, tuple)) and len(row) == 2, "transcript_ranges", "识别区间无效", 422)
        a, b = row
        require(type(a) is int and type(b) is int and 0 <= a < b <= duration_ms + 100,
                "transcript_ranges", "识别区间超出源视频", 422)
        b = min(b, duration_ms)
        require(a < b, "transcript_ranges", "识别区间超出源视频", 422)
        clean.append((a, b))
    merged = []
    for a, b in sorted(clean):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def _window_input(path, start, end):
    # Input seeking avoids decoding the unselected prefix. -t bounds the input,
    # including OCR filters which otherwise can decode beyond their frame cap.
    return ["-ss", f"{start/1000:.3f}", "-t", f"{(end-start)/1000:.3f}", *local_input(path)]


def _append_cues(out, cues, start, end, offset=0):
    for cue in sorted(cues, key=lambda c: (c["start"], c["end"])):
        a, b = max(start, offset + cue["start"]), min(end, offset + cue["end"])
        if b <= a:
            continue
        if out and out[-1]["text"] == cue["text"] and a <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], b)
        else:
            out.append({"start": a, "end": b, "text": cue["text"]})


def _audio_samples(path):
    with wave.open(str(path), "rb") as wav:
        require(wav.getnchannels() == 1 and wav.getsampwidth() == 2 and wav.getframerate() == 16000,
                "language_media_failed", "识别音频格式无效", 422)
        samples = array("h", wav.readframes(wav.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    require(samples, "language_media_failed", "当前分段未取得有效音频", 422)
    return samples


def _has_sound(samples, start=0, end=None):
    # Only reject effectively digital silence, not quiet speech. Whisper can
    # otherwise invent dialogue in a silent chunk or emit [BLANK_AUDIO].
    return any(abs(v) > 3 for v in samples[start*16:end*16 if end is not None else None])


def _process(argv, *, timeout, input_bytes=None):
    try:
        return subprocess.run(argv, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              check=False, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        raise SmartError("language_media_failed", "媒体读取超时或对应工具不可用", 503) from None


def probe(path):
    command_name = os.environ.get("AI_VIDEO_EDITING_FFPROBE", "ffprobe")
    p = _process([command_name, "-v", "error", "-protocol_whitelist", "file,pipe",
                  "-show_entries", "format=duration:stream=index,codec_type,codec_name:stream_tags=language", "-of", "json", str(path)], timeout=20)
    require(p.returncode == 0, "language_media_failed", "无法读取当前视频", 422)
    try:
        info = json.loads(p.stdout)
        seconds = float(info["format"]["duration"])
    except (ValueError, TypeError, KeyError):
        raise SmartError("language_media_failed", "无法读取当前视频时长", 422) from None
    require(math.isfinite(seconds) and seconds > 0, "language_media_failed", "媒体时长无效", 422)
    return seconds, info.get("streams", [])


def _parse_srt(content, duration_ms):
    out = []
    for block in re.split(r"\n\s*\n", content.lstrip("\ufeff").replace("\r", "").strip()):
        lines = block.splitlines()
        row = next((i for i, s in enumerate(lines) if "-->" in s), None)
        if row is None:
            continue
        matches = re.findall(r"(\d{2,}):(\d{2}):(\d{2})[,\.](\d{3})", lines[row])
        if len(matches) != 2:
            continue
        times = [((int(h)*60+int(m))*60+int(s))*1000+int(ms) for h,m,s,ms in matches]
        times[1] = min(times[1], duration_ms)
        text = re.sub(r"</?(?:b|i|u|font)(?:\s[^>]*)?>", "", "\n".join(lines[row+1:]), flags=re.I).strip()
        if text and times[1] > times[0]:
            out.append({"start": times[0], "end": times[1], "text": text})
    return out


def frame_sheet(path, target):
    seconds, _ = probe(path)
    # Use FFmpeg's scale and tile so no machine-local image library is needed.
    frames = min(12, max(2, round(seconds)))
    step = max(.12, seconds/frames)
    vf = (f"fps=1/{step:.3f}:start_time=0:round=up,scale=300:480:force_original_aspect_ratio=decrease,"
          f"pad=300:480:(ow-iw)/2:(oh-ih)/2,tile=4x{(frames+3)//4}")
    rgb = command([*local_input(path), "-an", "-vf", vf, "-frames:v", "1", "-vcodec", "mjpeg",
                   "-f", "image2pipe", "pipe:1"], timeout=60)
    require(rgb, "language_media_failed", "未取得有效画面", 422)
    sheet = Path(target)/"scenes.jpg"
    sheet.write_bytes(rgb)
    return sheet, seconds


def _hard_captions(path, start, end, source_language, temp):
    seconds = (end-start)/1000
    exe = shutil.which("tesseract")
    require(exe, "transcript_unavailable", "没有可用字幕轨、语音识别模型或文字识别引擎，请导入字幕", 422)
    installed = _process([exe, "--list-langs"], timeout=5).stdout.decode(errors="replace").splitlines()[1:]
    langs = [OCR_LANGS[source_language]] if source_language in OCR_LANGS else [v for v in ("eng","spa") if v in installed]
    require(langs and all(v in installed for v in langs), "ocr_language_unavailable", "当前服务器没有该源语种的画面文字识别包，请导入字幕", 422)
    # Sample the normal dialogue band; do not mix title bugs and platform watermarks into dialogue.
    step = max(1.0, seconds/72)
    vf = (f"fps=1/{step:.3f}:start_time=0:round=up,scale=640:-2,"
          "crop=iw:trunc(ih*0.32/2)*2:0:trunc(ih*0.55/2)*2,format=gray,"
          "lut=y='if(gte(val,214),0,255)'")
    command([*_window_input(path, start, end), "-an", "-vf", vf, "-frames:v", "72",
             "-y", str(temp/"ocr-%04d.pgm")],timeout=50)
    cues=[];deadline=time.monotonic()+110
    for i, frame in enumerate(sorted(temp.glob("ocr-*.pgm"))):
        require(time.monotonic()<deadline, "transcript_timeout", "当前分段画面文字识别超过时限，请稍后重试或导入字幕", 503)
        run = _process([exe,str(frame),"stdout","-l","+".join(langs),"--psm","6","tsv"],timeout=6)
        require(run.returncode == 0, "language_media_failed", "当前分段画面文字识别失败", 503)
        words=[]
        for row in run.stdout.decode("utf-8","replace").splitlines()[1:]:
            fields = row.split("\t",11)
            if len(fields)!=12 or fields[0]!="5": continue
            try: confidence=float(fields[10])
            except ValueError: continue
            if confidence>=55 and fields[11].strip() and any(c.isalnum() for c in fields[11]):
                words.append((fields[11].strip(), confidence))
        text=" ".join(x[0] for x in words)
        if not text or sum(x[1] for x in words)/len(words)<75: continue
        a,b=round(i*step*1000), min(round(seconds*1000),round((i+1)*step*1000))
        if b<=a: continue
        if cues and a==cues[-1]["end"] and SequenceMatcher(None,cues[-1]["text"].lower(),text.lower()).ratio()>=.86:
            cues[-1]["end"]=b
            if len(text)>len(cues[-1]["text"]): cues[-1]["text"]=text
        else:
            cues.append({"start":a,"end":b,"text":text})
    require(list(temp.glob("ocr-*.pgm")), "language_media_failed", "当前分段未取得有效画面", 422)
    return cues


def transcribe(path, source_language="auto", *, ranges=None, temp_dir=None, progress=None):
    """Return source-absolute cues for the union of selected millisecond ranges.

    An empty successful chunk is silence/blank captions, not an engine failure.
    Any failed chunk aborts the result; callers never receive partial success.
    """
    seconds, streams = probe(path)
    duration = round(seconds*1000)
    ranges = selected_ranges(ranges, duration)
    chunks = [(a, min(b, a+CHUNK_MS)) for start, b in ranges for a in range(start, b, CHUNK_MS)]
    visible = [s for s in streams if s["codec_type"]=="subtitle"]
    known = {source_language,OCR_LANGS.get(source_language,""),{"fr":"fre","de":"ger","zh":"chi","ko":"kor"}.get(source_language,"")}
    if source_language != "auto":
        tagged=[s for s in visible if s.get("tags",{}).get("language") in known]
        visible = tagged or [s for s in visible if s.get("tags",{}).get("language","und")=="und"]
    exe=shutil.which(os.environ.get("AI_VIDEO_EDITING_WHISPER_BIN","whisper-cli"))
    model=os.environ.get("AI_VIDEO_EDITING_WHISPER_MODEL","")
    asr = exe and model and Path(model).is_file() and any(s["codec_type"]=="audio" for s in streams)
    engine = "embedded" if visible else "asr" if asr else "ocr"
    temp_dir = Path(temp_dir or os.environ.get("AI_VIDEO_EDITING_ROOT", "/mnt/data-disk/ai-video-editing"))/"temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    cues = []
    for index, (a, b) in enumerate(chunks):
        try:
            with tempfile.TemporaryDirectory(prefix="captions-", dir=temp_dir) as folder:
                root = Path(folder)
                if engine == "embedded":
                    # Subtitle demuxers may seek to the prior cue and reset its PTS.
                    # copyts keeps absolute PTS, including a cue straddling the seek.
                    argv = [os.environ.get("AI_VIDEO_EDITING_FFMPEG", "ffmpeg"), "-v", "error",
                            "-copyts", "-ss", f"{a/1000:.3f}", *local_input(path), "-to", f"{b/1000:.3f}",
                            "-map", "0:"+str(visible[0]["index"]), "-c:s", "srt", "-f", "srt", "pipe:1"]
                    p = _process(argv, timeout=30)
                    require(p.returncode == 0, "language_media_failed", "当前内嵌字幕轨无法读取，请更换字幕轨或导入字幕", 422)
                    part = _parse_srt(p.stdout.decode("utf-8", "replace"), duration)
                    _append_cues(cues, part, a, b)
                elif engine == "asr":
                    audio = root/"audio.wav"
                    command([*_window_input(path, a, b), "-vn", "-ac", "1", "-ar", "16000",
                             "-c:a", "pcm_s16le", "-y", str(audio)], timeout=45)
                    samples = _audio_samples(audio)
                    part = []
                    if _has_sound(samples):
                        p = _process([exe, "-m", model, "-f", str(audio), "-l", source_language, "-osrt",
                                      "-of", str(root/"audio"), "-t", "4"], timeout=120)
                        result = root/"audio.srt"
                        require(p.returncode == 0 and result.is_file(), "language_media_failed", "当前分段语音识别失败", 503)
                        part = [c for c in _parse_srt(result.read_text(encoding="utf-8", errors="replace"), b-a)
                                if c["text"].strip() != "[BLANK_AUDIO]" and _has_sound(samples, c["start"], c["end"])]
                    _append_cues(cues, part, a, b, a)
                else:
                    _append_cues(cues, _hard_captions(path, a, b, source_language, root), a, b, a)
        except SmartError as exc:
            raise SmartError(exc.code, f"源视频 {a/1000:.3f}–{b/1000:.3f} 秒识别失败：{exc.message}", exc.status) from None
        if progress:
            progress(index+1, len(chunks))
    notes = {"embedded": "已读取视频内嵌字幕轨。", "asr": "已按分段原声生成字幕草稿，请校对对白。",
             "ocr": "从画面字幕区域逐段抽样识别，文字与起止时间请校对。"}
    return {"cues": cues, "source": engine, "note": notes[engine], "ranges": ranges, "chunks": len(chunks)}
