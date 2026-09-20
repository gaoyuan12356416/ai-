"""Render a synthetic FB CPU graph locally; no network, upload, or publication.

Only the NVENC encoder options are replaced with libx264 for portable QA.
The source, template inputs, complete filter graph, audio mapping and duration
remain the actual build_command result.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import zlib

from features.fb_gpu.prepare_worker import build_command
from scripts.test_fb_gpu_prepare_worker import config


def png(path, color):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 720, 1280, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress((b"\x00" + bytes(color) * 720) * 1280)) + chunk(b"IEND", b""))


def run(command):
    result = subprocess.run([str(value) for value in command], capture_output=True, check=False, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[-6000:])
    return result.stdout


def verify():
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg and ffprobe are required")
    base = [ffmpeg, "-y", "-v", "error", "-nostdin"]
    receipts = []
    with tempfile.TemporaryDirectory(prefix="fb-source-overlay-qa-") as temporary:
        root = Path(temporary)
        transparent, white = root / "transparent.png", root / "white.png"
        png(transparent, (0, 0, 0, 0)); png(white, (255, 255, 255, 255))
        assets = {"border": transparent, "tint": transparent}
        for name, image in (("opacity_video", transparent), ("corners", white)):
            assets[name] = root / (name + ".webm")
            run([*base, "-loop", "1", "-framerate", "30", "-i", image, "-t", "1",
                 "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-lossless", "1", assets[name]])
        source_audio = root / "source-audio.mp4"
        # A wide source exercises cover/crop. Narrow blue lines land at x=60
        # and x=660 only after centered 150% zoom. Their top/bottom samples also
        # reject accidentally applying the main layer's 1.25-degree rotation.
        pattern = ",drawbox=x=128:y=0:w=4:h=320:c=blue:t=fill,drawbox=x=228:y=0:w=4:h=320:c=blue:t=fill"
        run([*base, "-f", "lavfi", "-i", "color=c=red:s=360x320:r=30:d=0.5" + pattern,
             "-f", "lavfi", "-i", "color=c=lime:s=360x320:r=30:d=0.5" + pattern,
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
             "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
             "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv420p", "-c:a", "aac", source_audio])
        source_silent = root / "source-silent.mp4"
        run([*base, "-i", source_audio, "-an", "-c:v", "copy", source_silent])
        cfg = replace(config(root), ffmpeg=ffmpeg, ffprobe=ffprobe)
        legacy = {"rotation_millidegrees": 1250, "scale_bp": 9900, "tint_opacity_bp": 500}
        for has_audio, source in ((True, source_audio), (False, source_silent)):
            audio_hashes = []
            for enabled in (False, True):
                recipe = dict(legacy)
                if enabled:
                    recipe["source_overlay"] = {"version": 1, "opacity_bp": 500, "scale_bp": 15000}
                output = root / f"output-{has_audio}-{enabled}.mp4"
                command = build_command(cfg, source, output, {"has_audio": has_audio, "duration": 1}, recipe, assets)
                assert "-shortest" not in command, command
                assert command[-3:] == ["-t", "1.000000", str(output)], command
                command[command.index("h264_nvenc")] = "libx264"
                command[command.index("p5")] = "ultrafast"
                for key in ("-rc", "-cq"):
                    position = command.index(key)
                    del command[position:position + 2]
                command[-1:-1] = ["-crf", "15"]
                run(command)
                probe = json.loads(run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", output]))
                video = [item for item in probe["streams"] if item["codec_type"] == "video"]
                audio = [item for item in probe["streams"] if item["codec_type"] == "audio"]
                assert len(video) == 1 and len(audio) == 1, probe
                assert (video[0]["width"], video[0]["height"]) == (720, 1280), probe
                assert int(video[0]["nb_frames"]) == 30, probe
                assert abs(float(probe["format"]["duration"]) - 1) <= 0.05, probe
                pixels = []
                for timestamp in ("0.25", "0.75"):
                    raw = run([*base, "-ss", timestamp, "-i", output, "-frames:v", "1", "-vf",
                               "crop=2:2:360:640,format=rgb24", "-f", "rawvideo", "-"])
                    pixels.append(tuple(raw[:3]))
                spatial = []
                for x, y in ((60, 64), (60, 1216), (660, 64), (660, 1216)):
                    raw = run([*base, "-ss", "0.25", "-i", output, "-frames:v", "1", "-vf",
                               f"crop=2:2:{x}:{y},format=rgb24", "-f", "rawvideo", "-"])
                    spatial.append(tuple(raw[:3]))
                if enabled:
                    red, green = pixels
                    assert red[0] > red[1] + 6 and red[0] > red[2] + 6, pixels
                    assert green[1] > green[0] + 6 and green[1] > green[2] + 6, pixels
                    assert min(*red, *green) >= 235, pixels
                    assert all(pixel[2] > pixel[0] + 6 and pixel[2] > pixel[1] + 6 for pixel in spatial), spatial
                else:
                    assert all(min(pixel) >= 250 for pixel in pixels), pixels
                    assert all(min(pixel) >= 250 for pixel in spatial), spatial
                pcm = run([*base, "-i", output, "-map", "0:a:0", "-f", "s16le", "-"])
                if not has_audio:
                    assert not any(pcm), "silent-source output contains sound"
                audio_hashes.append(hashlib.sha256(pcm).hexdigest())
                receipts.append({"has_source_audio": has_audio, "source_overlay": enabled,
                                 "video_frames": 30, "audio_streams": 1,
                                 "duration": probe["format"]["duration"], "quarter_pixels_rgb": pixels,
                                 "cover_zoom_unrotated_pixels_rgb": spatial})
            assert audio_hashes[0] == audio_hashes[1], "source overlay changed encoded audio"
    return {"ok": True, "backend": "CPU legacy graph with libx264 QA encoder", "renders": receipts,
            "audio_pcm_unchanged": True, "no_external_actions": True}


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
