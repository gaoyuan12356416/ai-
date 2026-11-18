"""Read captions or sample pixels only from a caller-resolved private media file."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from difflib import SequenceMatcher
from pathlib import Path

from .smart_common import SmartError, require
from .smart_media import command, local_input

OCR_LANGS = {"en":"eng","es":"spa","zh":"chi_sim","fr":"fra","de":"deu","it":"ita","pt":"por",
             "ja":"jpn","ko":"kor","id":"ind","vi":"vie","ru":"rus","tr":"tur","ar":"ara","hi":"hin"}


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
    require(seconds > 0, "language_media_failed", "媒体时长无效", 422)
    return seconds, info.get("streams", [])


def _parse_srt(content):
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


def _hard_captions(path, seconds, source_language, temp):
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
    command([*local_input(path), "-an", "-vf", vf, "-frames:v", "72",
             "-y", str(temp/"ocr-%04d.pgm")],timeout=50)
    cues=[];deadline=time.monotonic()+110
    for i, frame in enumerate(sorted(temp.glob("ocr-*.pgm"))):
        require(time.monotonic()<deadline, "transcript_timeout", "画面文字识别超过时限，可剪出较短片段或导入字幕", 503)
        run = _process([exe,str(frame),"stdout","-l","+".join(langs),"--psm","6","tsv"],timeout=6)
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
        start,end=round(i*step*1000), min(round(seconds*1000),round((i+1)*step*1000))
        if end<=start: continue
        if cues and start==cues[-1]["end"] and SequenceMatcher(None,cues[-1]["text"].lower(),text.lower()).ratio()>=.86:
            cues[-1]["end"]=end
            if len(text)>len(cues[-1]["text"]): cues[-1]["text"]=text
        else:
            cues.append({"start":start,"end":end,"text":text})
    require(cues, "transcript_unavailable", "未读到可确认的字幕。可导入 SRT/VTT 或手工填写对白", 422)
    return {"cues":cues, "source":"ocr", "note":"从画面字幕区域抽样识别，文字与起止时间请校对。"}


def transcribe(path, source_language="auto"):
    seconds, streams = probe(path)
    require(seconds <= 600, "transcript_duration", "单次识别支持 10 分钟以内的视频，请先剪出所需片段", 422)
    visible = [s for s in streams if s["codec_type"]=="subtitle"]
    known = {source_language,OCR_LANGS.get(source_language,""),{"fr":"fre","de":"ger","zh":"chi","ko":"kor"}.get(source_language,"")}
    if source_language != "auto":
        tagged=[s for s in visible if s.get("tags",{}).get("language") in known]
        visible = tagged or [s for s in visible if s.get("tags",{}).get("language","und")=="und"]
    if visible:
        argv=[os.environ.get("AI_VIDEO_EDITING_FFMPEG","ffmpeg"), "-v", "error", *local_input(path),
              "-map","0:"+str(visible[0]["index"]),"-c:s","srt","-f","srt","pipe:1"]
        p = _process(argv,timeout=30)
        cues=_parse_srt(p.stdout.decode("utf-8","replace")) if p.returncode==0 else []
        if cues: return {"cues":cues,"source":"embedded","note":"已读取视频内嵌字幕轨。"}
    exe=shutil.which(os.environ.get("AI_VIDEO_EDITING_WHISPER_BIN","whisper-cli"))
    model=os.environ.get("AI_VIDEO_EDITING_WHISPER_MODEL","")
    with tempfile.TemporaryDirectory(prefix="ai-video-captions-") as folder:
        root=Path(folder)
        if exe and model and Path(model).is_file() and any(s["codec_type"]=="audio" for s in streams):
            audio=root/"audio.wav"
            command([*local_input(path),"-vn","-ac","1","-ar","16000","-c:a","pcm_s16le","-y",str(audio)],timeout=45)
            p=_process([exe,"-m",model,"-f",str(audio),"-l",source_language,"-osrt","-of",str(root/"audio"),"-t","2"],timeout=130)
            result=root/"audio.srt"
            if p.returncode==0 and result.exists():
                cues=_parse_srt(result.read_text(encoding="utf-8",errors="replace"))
                if cues:return {"cues":cues,"source":"asr","note":"已根据原声音轨生成字幕草稿，请校对对白。"}
        return _hard_captions(path,seconds,source_language,root)
