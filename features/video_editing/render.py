"""Deterministic, shell-free FFmpeg commands for the video editor."""
from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess

from features.drama_synthesis.async_runtime import process_launch, record_process, clear_process
from features.drama_synthesis.gpu import run_render_with_progress
from .recipe import FPS, FADE_FRAMES, error, output_frames


def _seconds(frames):
    return "%.9f" % (frames / FPS)


def encoder_args(encoder="h264_nvenc"):
    if encoder == "h264_nvenc":
        return ["-c:v", encoder, "-preset", "p4", "-rc", "vbr", "-cq", "21", "-b:v", "0"]
    if encoder == "libx264":
        return ["-c:v", encoder, "-preset", "veryfast", "-crf", "20"]
    raise error("video_editing_encoder_invalid", "剪辑编码器配置无效", 503)


def output_args(encoder):
    return [*encoder_args(encoder), "-maxrate", "6M", "-bufsize", "12M", "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-fps_mode", "cfr", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            "-video_track_timescale", "15360", "-movflags", "+faststart"]


def build_normalize_command(source, clip, output, *, has_audio, ffmpeg="ffmpeg", encoder="h264_nvenc"):
    count = clip["end_frame"] - clip["start_frame"]
    duration = _seconds(count)
    fit = ("scale=720:1280:force_original_aspect_ratio=decrease:force_divisible_by=2,"
           "pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black") if clip["fit"] == "contain" else (
           "scale=720:1280:force_original_aspect_ratio=increase:force_divisible_by=2,crop=720:1280")
    video = ("[0:v:0]setpts=PTS-STARTPTS,scale=trunc(iw*sar/2)*2:ih,setsar=1,%s,setsar=1,"
             "fps=30,tpad=stop_mode=clone:stop_duration=0.1,trim=end_frame=%d,"
             "settb=1/30,setpts=N,format=yuv420p[v]" % (fit, count))
    if has_audio and not clip["mute"]:
        audio = ("[0:a:0]asetpts=PTS-STARTPTS,aresample=48000:async=1:first_pts=0,"
                 "aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=duration=%s,"
                 "asetpts=PTS-STARTPTS[a]" % duration)
    else:
        audio = "anullsrc=r=48000:cl=stereo,atrim=duration=%s,asetpts=PTS-STARTPTS[a]" % duration
    return [ffmpeg, "-hide_banner", "-nostdin", "-y", "-ss", _seconds(clip["start_frame"]),
            "-i", str(source), "-filter_complex_threads", "1", "-filter_complex", video + ";" + audio,
            "-map", "[v]", "-map", "[a]", "-t", duration, *output_args(encoder), str(output)]


def build_assemble_command(paths, recipe, output, *, ffmpeg="ffmpeg", encoder="h264_nvenc"):
    if len(paths) != len(recipe["clips"]) or not paths:
        raise error()
    command = [ffmpeg, "-hide_banner", "-nostdin", "-y"]
    filters = []
    for index, path in enumerate(paths):
        command += ["-i", str(path)]
        filters += ["[%d:v:0]settb=1/30,setpts=PTS-STARTPTS[v%d]" % (index, index),
                    "[%d:a:0]aresample=48000,asetpts=PTS-STARTPTS[a%d]" % (index, index)]
    video, audio = "v0", "a0"
    length = recipe["clips"][0]["end_frame"] - recipe["clips"][0]["start_frame"]
    for index in range(1, len(paths)):
        new_video, new_audio = "joinedv%d" % index, "joineda%d" % index
        if recipe["clips"][index - 1]["transition"] == "fade":
            filters += ["[%s][v%d]xfade=transition=fade:duration=0.3:offset=%s[%s]" %
                        (video, index, _seconds(length - FADE_FRAMES), new_video),
                        "[%s][a%d]acrossfade=d=0.3:c1=tri:c2=tri[%s]" % (audio, index, new_audio)]
            length -= FADE_FRAMES
        else:
            filters += ["[%s][%s][v%d][a%d]concat=n=2:v=1:a=1[%s][%s]" %
                        (video, audio, index, index, new_video, new_audio)]
        # concat chooses AVTB whereas xfade needs matching 1/30 time bases.
        filters += ["[%s]settb=1/30,setpts=PTS-STARTPTS[tv%d]" % (new_video, index)]
        video, audio = "tv%d" % index, new_audio
        length += recipe["clips"][index]["end_frame"] - recipe["clips"][index]["start_frame"]
    filters += ["[%s]trim=end_frame=%d,setpts=N/(30*TB),format=yuv420p[outv]" % (video, length),
                "[%s]apad,atrim=duration=%s,asetpts=PTS-STARTPTS[outa]" % (audio, _seconds(length))]
    command += ["-filter_complex_threads", "1", "-filter_complex", ";".join(filters),
                "-map", "[outv]", "-map", "[outa]", "-t", _seconds(length), *output_args(encoder), str(output)]
    return command


def assembly_layers(recipe, max_inputs=8):
    """Bound simultaneous decoders without re-encoding the growing prefix N times."""
    if type(max_inputs) is not int or not 2 <= max_inputs <= 16:
        raise error()
    nodes = [{"name": "clip-%04d" % i, "first": i, "last": i,
              "frames": c["end_frame"] - c["start_frame"]} for i, c in enumerate(recipe["clips"])]
    layers, level = [], 0
    while len(nodes) > 1:
        output, work = [], []
        for offset in range(0, len(nodes), max_inputs):
            group = nodes[offset:offset + max_inputs]
            if len(group) == 1:
                output.append(group[0]); continue
            clips = [{"start_frame": 0, "end_frame": node["frames"],
                      "transition": recipe["clips"][node["last"]]["transition"] if i < len(group) - 1 else "cut"}
                     for i, node in enumerate(group)]
            count = sum(c["end_frame"] - (FADE_FRAMES if c["transition"] == "fade" else 0) for c in clips)
            node = {"name": "join-%02d-%04d" % (level, offset), "first": group[0]["first"],
                    "last": group[-1]["last"], "frames": count}
            work.append({**node, "inputs": [item["name"] for item in group], "recipe": {"clips": clips}})
            output.append(node)
        layers.append(work); nodes = output; level += 1
    return layers, nodes[0]["name"]


def probe_media(path, *, ffprobe="ffprobe"):
    command = [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    proc = None
    with process_launch():
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            record_process(proc.pid)
            stdout, _ = proc.communicate(timeout=120)
            if proc.returncode:
                raise error("video_editing_probe_failed", "无法读取视频信息")
        except subprocess.TimeoutExpired:
            proc.kill(); proc.communicate()
            raise error("video_editing_probe_failed", "读取视频信息超时") from None
        finally:
            if proc is not None and proc.poll() is not None:
                clear_process(proc.pid)
    try:
        data = json.loads(stdout)
        video = next(s for s in data["streams"] if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic"))
        audio = next((s for s in data["streams"] if s.get("codec_type") == "audio"), None)
        duration = float(video.get("duration") or data["format"]["duration"])
        width, height = int(video["width"]), int(video["height"])
        ratio = video.get("avg_frame_rate", "0/1").split("/")
        fps = float(ratio[0]) / float(ratio[1])
        if not math.isfinite(duration) or not 0 < duration <= 2147483 or not math.isfinite(fps) or not 0 < fps <= 240 or not 0 < width <= 16384 or not 0 < height <= 16384:
            raise ValueError()
        rotation = int(video.get("tags", {}).get("rotate", 0))
        for side in video.get("side_data_list", []):
            rotation = int(side.get("rotation", rotation))
        if rotation % 180:
            width, height = height, width
        return {"duration_ms": int(round(duration * 1000)), "width": width, "height": height,
                "fps": round(fps, 6), "has_audio": audio is not None,
                "video_codec": str(video.get("codec_name", "")),
                "audio_codec": str((audio or {}).get("codec_name", "")),
                "format_name": str(data["format"].get("format_name", "")),
                "container_brand": str(data["format"].get("tags", {}).get("major_brand", "")).strip(),
                "pixel_format": str(video.get("pix_fmt", "")),
                "sample_aspect_ratio": str(video.get("sample_aspect_ratio", "1:1")),
                "audio_duration_ms": int(round(float((audio or {}).get("duration") or duration) * 1000)) if audio else 0}
    except (ValueError, KeyError, TypeError, StopIteration, ZeroDivisionError):
        raise error("video_editing_probe_failed", "视频没有可处理的画面或时长") from None


def run_ffmpeg(command, duration_seconds, *, progress_callback=None):
    budget = min(86400, max(300, int(duration_seconds * 8 + 120)))
    return run_render_with_progress(command, timeout=budget, duration_seconds=max(duration_seconds, 0.04),
                                    stall_timeout=900, progress_callback=progress_callback)


def validate_output(info, expected_frames):
    expected_ms = expected_frames * 1000 / FPS
    if (info.get("width") != 720 or info.get("height") != 1280 or abs(info.get("fps", 0) - FPS) > 0.001
            or info.get("video_codec") != "h264" or info.get("audio_codec") != "aac"
            or info.get("pixel_format", "yuv420p") != "yuv420p" or info.get("sample_aspect_ratio", "1:1") != "1:1"
            or not info.get("has_audio") or abs(info["duration_ms"] - expected_ms) > 70
            or abs(info.get("audio_duration_ms", 0) - expected_ms) > 100):
        raise error("video_editing_output_invalid", "成片规格或音画时长校验失败", 502)
    return info
