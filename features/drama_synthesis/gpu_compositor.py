"""Chunked fused-GPU renderer for drama random-overlay compositions."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from contextvars import copy_context
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Union

from features.fb_gpu.random_overlay import load_asset_set, selected_asset_paths, validate_recipe

from .composition import (
    CANVAS_FPS,
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    RENDERER_PROFILE,
    canonical_json,
    compile_random_overlay_spec,
    composition_sha256,
    plan_chunks,
)
from .core import DramaSynthesisError, RECIPE_PROFILE
from .local_checkpoint import (
    atomic_write_record,
    checkpoint_error,
    durable_ensure_directory,
    file_fingerprint,
    load_completed,
    read_record,
    save_completed,
)


BACKEND = "opencl_fused_v2"
DEFAULT_CHUNK_TIMEOUT_SECONDS = 1800
MIN_CHUNK_TIMEOUT_SECONDS = 300
MAX_CHUNK_TIMEOUT_SECONDS = 7200
DEFAULT_OPENCL_DEVICE = "0.0"
DEFAULT_COMPOSITOR_LANES = 1
DEFAULT_FILTER_THREADS = 2
DEFAULT_RUNTIME_IDENTITY = "ffmpeg-opencl-nvenc-runtime-v1"
KERNEL_TEMPLATE = Path(__file__).with_name("opencl") / "random_overlay_v2.cl"
HEX = re.compile(r"[0-9a-f]{64}\Z")
_THREAD_LOCKS: Dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
PROCESS_UNCERTAIN_CODES = frozenset({
    "drama_media_checkpoint_unverified",
    "drama_media_checkpoint_conflict",
    "gpu_process_state_unknown",
    "gpu_previous_process_running",
})


def compositor_error(code: str = "drama_gpu_compositor_unavailable", status: int = 503) -> DramaSynthesisError:
    messages = {
        "drama_gpu_compositor_unavailable": "GPU合成引擎不可用",
        "drama_render_chunk_failed": "随机模板视频分片制作失败",
        "drama_render_chunk_timeout": "随机模板视频分片制作超时",
        "drama_render_join_failed": "随机模板视频分片合并失败",
        "drama_render_audio_mux_failed": "随机模板视频音频封装失败",
    }
    return DramaSynthesisError(code, messages.get(code, "GPU合成制作失败"), status)


def chunk_timeout_seconds(value: Any = None) -> int:
    raw = os.environ.get("DRAMA_GPU_CHUNK_TIMEOUT", str(DEFAULT_CHUNK_TIMEOUT_SECONDS)) if value is None else value
    if isinstance(raw, bool) or not re.fullmatch(r"[0-9]{3,4}", str(raw)):
        raise compositor_error()
    number = int(raw)
    if not MIN_CHUNK_TIMEOUT_SECONDS <= number <= MAX_CHUNK_TIMEOUT_SECONDS:
        raise compositor_error()
    return number


def opencl_device(value: Any = None) -> str:
    raw = os.environ.get("DRAMA_GPU_OPENCL_DEVICE", DEFAULT_OPENCL_DEVICE) if value is None else value
    text = str(raw)
    if not re.fullmatch(r"[0-9]{1,2}\.[0-9]{1,2}", text):
        raise compositor_error()
    return text


def compositor_lanes(value: Any = None) -> int:
    raw = os.environ.get("DRAMA_GPU_COMPOSITOR_LANES", str(DEFAULT_COMPOSITOR_LANES)) if value is None else value
    if isinstance(raw, bool) or not re.fullmatch(r"[1-4]", str(raw)):
        raise compositor_error()
    return int(raw)


def compositor_filter_threads(value: Any = None) -> int:
    raw = os.environ.get("DRAMA_GPU_FILTER_THREADS", str(DEFAULT_FILTER_THREADS)) if value is None else value
    if isinstance(raw, bool) or not re.fullmatch(r"[1-4]", str(raw)):
        raise compositor_error()
    return int(raw)


def runtime_identity(value: Any = None) -> str:
    raw = os.environ.get("DRAMA_GPU_RUNTIME_IDENTITY", DEFAULT_RUNTIME_IDENTITY) if value is None else value
    text = str(raw)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{7,99}", text):
        raise compositor_error()
    return text


def _runtime_fingerprint(
    ffmpeg: str, *, nvidia_validator: Optional[Callable[[Path], bool]] = None,
) -> Dict[str, Any]:
    release_sha = str(os.environ.get("DRAMA_GPU_RELEASE_SHA", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", release_sha):
        raise compositor_error()
    try:
        binary = Path(ffmpeg).resolve(strict=True)
        if not binary.is_file():
            raise OSError
        binary_fingerprint = file_fingerprint(binary)
        nvidia_smi = Path(os.environ.get("DRAMA_NVIDIA_SMI", ""))
        nvidia_stat = nvidia_smi.stat()
        trusted = (
            nvidia_validator(nvidia_smi) if nvidia_validator is not None else bool(
                nvidia_smi.is_absolute()
                and not nvidia_smi.is_symlink()
                and nvidia_smi.is_file()
                and nvidia_smi.name == "nvidia-smi"
                and nvidia_stat.st_uid == 0
                and not nvidia_stat.st_mode & 0o022
                and os.access(nvidia_smi, os.X_OK)
            )
        )
        if not trusted:
            raise OSError
        gpu = subprocess.run(
            [
                str(nvidia_smi), "--query-gpu=uuid,driver_version",
                "--format=csv,noheader,nounits",
            ],
            check=True, capture_output=True, text=True, timeout=30,
        )
        gpu_identity = "\n".join(
            line.strip() for line in gpu.stdout.splitlines() if line.strip()
        )
        if not 8 <= len(gpu_identity) <= 4096:
            raise ValueError()
    except Exception:
        raise compositor_error() from None
    return {
        "version": 1,
        "release_sha": release_sha,
        "declared_identity": runtime_identity(),
        "ffmpeg": binary_fingerprint,
        "opencl_device": opencl_device(),
        "gpu_driver_identity_sha256": hashlib.sha256(gpu_identity.encode("utf-8")).hexdigest(),
    }


def cache_root(value: Any = None) -> Path:
    raw = os.environ.get(
        "DRAMA_GPU_COMPOSITOR_CACHE_ROOT", "/data/drama-synthesis-gpu/work/compositor-cache"
    ) if value is None else value
    path = Path(str(raw))
    if (
        not path.is_absolute()
        or path == Path(path.anchor)
        or path.name in {"", ".", ".."}
        or path.is_symlink()
    ):
        raise compositor_error()
    return path


def _canonical_recipe(recipe: Mapping[str, Any]) -> str:
    return canonical_json({key: value for key, value in recipe.items() if key != "recipe_sha256"})


def _probe(ffprobe: str, path: Path) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            [
                ffprobe, "-v", "error", "-show_data_hash", "sha256",
                "-show_streams", "-show_format", "-of", "json", str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        payload = json.loads(proc.stdout)
        packet_proc = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-read_intervals", "0%+#1", "-show_packets",
                "-show_entries", "packet=flags", "-of", "json", str(path),
            ],
            check=True, capture_output=True, text=True, timeout=180,
        )
        packets = json.loads(packet_proc.stdout).get("packets") or []
    except Exception:
        raise compositor_error("drama_render_chunk_failed", 502) from None
    streams = payload.get("streams") if isinstance(payload, Mapping) else []
    video = next((row for row in streams if row.get("codec_type") == "video"), {})
    audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
    try:
        # The composition follows the video timeline. Container duration can be
        # one muxer tick longer than the last video frame (especially after a
        # stream-copy clip), which would otherwise invent a non-existent frame.
        duration = float(video.get("duration") or (payload.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError, OverflowError):
        duration = 0
    if not math.isfinite(duration) or duration <= 0:
        raise compositor_error("drama_render_chunk_failed", 502)
    return {
        "duration": duration, "has_audio": audio is not None, "video": video, "audio": audio,
        "first_packet_keyframe": bool(packets and "K" in str(packets[0].get("flags") or "")),
    }


def _safe_kernel_template(path: Path = KERNEL_TEMPLATE) -> str:
    try:
        if path.is_symlink() or not path.is_file() or not 1024 <= path.stat().st_size <= 128 * 1024:
            raise OSError
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise compositor_error() from None
    if "__kernel void compose_random_overlay_v2" not in value or "SCENE_ROTATION_RADIANS" not in value:
        raise compositor_error()
    return value


def _clean_main_geometry(scale: float) -> Dict[str, int]:
    """Compile the centered main-layer extent for the clean visual profile.

    The source is first contain-fitted into a transparent 720x1280 plane, then
    the whole plane is scaled and rotated about the output center. Mapping the
    output pixels directly back into that plane avoids an intermediate rotate
    canvas and therefore cannot inherit the legacy ``rotw(iw)/roth(ih)``
    extent bug or its horizontal clipping bands.
    """
    if not math.isfinite(scale) or not 0.01 <= scale <= 10.0:
        raise compositor_error()
    main_width = int(CANVAS_WIDTH * scale / 2.0) * 2
    main_height = int(CANVAS_HEIGHT * scale / 2.0) * 2
    if main_width <= 0 or main_height <= 0:
        raise compositor_error()
    return {"main_width": main_width, "main_height": main_height}


def compile_opencl_kernel(spec: Mapping[str, Any], template: Optional[str] = None) -> Dict[str, str]:
    main = spec["layers"][1]
    tint = spec["layers"][2]
    rotation = main["transform"]["rotation_millidegrees"] / 1000.0 * math.pi / 180.0
    scale = main["transform"]["scale_bp"] / 10000.0
    opacity = tint["opacity_bp"] / 10000.0
    if not all(math.isfinite(value) for value in (rotation, scale, opacity)):
        raise compositor_error()
    geometry = _clean_main_geometry(scale)
    prefix = "\n".join((
        "#define SCENE_WIDTH %d" % CANVAS_WIDTH,
        "#define SCENE_HEIGHT %d" % CANVAS_HEIGHT,
        "#define SCENE_ROTATION_RADIANS %.9ff" % rotation,
        "#define SCENE_SCALE %.9ff" % scale,
        "#define SCENE_MAIN_WIDTH %d" % geometry["main_width"],
        "#define SCENE_MAIN_HEIGHT %d" % geometry["main_height"],
        "#define SCENE_TINT_OPACITY %.9ff" % opacity,
        "",
    ))
    body = _safe_kernel_template() if template is None else str(template)
    if "__kernel void compose_random_overlay_v2" not in body:
        raise compositor_error()
    text = prefix + body
    return {"source": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def _atomic_write_text(path: Path, value: str) -> None:
    durable_ensure_directory(path.parent)
    encoded = value.encode("utf-8")
    if path.exists() or path.is_symlink():
        try:
            if path.is_file() and not path.is_symlink() and path.read_bytes() == encoded:
                return
        except OSError:
            pass
        raise checkpoint_error(conflict=True)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, hashlib.sha256(encoded).hexdigest()[:16]))
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            descriptor = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except FileExistsError:
        if path.is_file() and not path.is_symlink() and path.read_bytes() == encoded:
            return
        try:
            if temporary.is_file() and not temporary.is_symlink() and temporary.read_bytes() == encoded:
                os.replace(temporary, path)
                if os.name == "posix":
                    descriptor = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                return
        except OSError:
            pass
        raise checkpoint_error(conflict=True) from None
    except OSError:
        temporary.unlink(missing_ok=True)
        raise checkpoint_error() from None


def _filter_escape(value: Union[str, os.PathLike]) -> str:
    # FFmpeg filter values use backslash escaping even when argv bypasses a shell.
    return str(value).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _input_arguments(
    path: Path, media_type: str, phase_seconds: float = 0.0, threads: int = DEFAULT_FILTER_THREADS
) -> list[str]:
    if media_type == "image/png":
        return [
            "-threads", str(threads), "-loop", "1", "-framerate", str(CANVAS_FPS),
            "-i", str(path),
        ]
    if media_type == "video/webm":
        return [
            "-threads", str(threads),
            "-stream_loop", "-1", "-ss", "%.6f" % max(0.0, phase_seconds),
            "-c:v", "libvpx-vp9", "-i", str(path),
        ]
    raise compositor_error()


def build_opencl_chunk_command(
    *,
    ffmpeg: str,
    source: Path,
    output: Path,
    spec: Mapping[str, Any],
    assets: Mapping[str, Path],
    asset_media_types: Mapping[str, str],
    asset_durations: Mapping[str, float],
    chunk: Mapping[str, Any],
    kernel_path: Path,
    device: Optional[str] = None,
) -> list[str]:
    device = opencl_device(device)
    threads = compositor_filter_threads()
    start = float(chunk["start_seconds"])
    command = [
        ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-filter_complex_threads", str(threads),
        "-init_hw_device", "opencl=ocl:%s" % device, "-filter_hw_device", "ocl",
        "-threads", str(threads), "-ss", "%.6f" % start, "-i", str(source),
    ]
    # program_opencl arguments are source, border, opacity, corners, tint.
    for category in ("border", "opacity_video", "corners", "tint"):
        duration = float(asset_durations.get(category) or 0)
        phase = (start % duration) if duration > 0 else 0.0
        command.extend(_input_arguments(
            Path(assets[category]), asset_media_types[category], phase, threads
        ))
    # The source may switch from a 16:9 intro to portrait episodes. FFmpeg
    # reinitializes the input filter chain at that boundary, so STARTPTS would
    # reset a second time and overlap the episode with the intro. The input
    # seek already rebases each chunk; preserve its demuxer timestamps across
    # reinitialization, matching the established drama renderer contract.
    graph_parts = [
        "[0:v]setpts=PTS,fps=%d,tpad=stop_mode=clone:stop_duration=1,"
        "format=rgba,hwupload[source]" % CANVAS_FPS
    ]
    for index, label in enumerate(("border", "opacity", "corners", "tint"), start=1):
        graph_parts.append(
            "[%d:v]fps=%d,setpts=PTS-STARTPTS,format=rgba,hwupload[%s]"
            % (index, CANVAS_FPS, label)
        )
    graph_parts.append(
        "[source][border][opacity][corners][tint]"
        "program_opencl=inputs=5:size=%dx%d:source='%s':kernel=compose_random_overlay_v2:"
        "shortest=1:eof_action=endall,hwdownload,format=rgba,"
        "fps=%d,setpts=N/(%d*TB)[v]"
        % (CANVAS_WIDTH, CANVAS_HEIGHT, _filter_escape(kernel_path), CANVAS_FPS, CANVAS_FPS)
    )
    command.extend([
        "-filter_complex", ";".join(graph_parts), "-map", "[v]", "-an",
        "-c:v", "h264_nvenc", "-profile:v", "high", "-preset", "p3",
        "-tune", "hq", "-rc", "vbr", "-cq", "21", "-b:v", "0",
        "-rgb_mode", "yuv420", "-pix_fmt", "yuv420p", "-r", str(CANVAS_FPS),
        "-fps_mode", "cfr", "-g", "60",
        "-keyint_min", "60", "-forced-idr", "1", "-no-scenecut", "1", "-bf", "0",
        "-color_range", "tv", "-colorspace", "bt709", "-color_trc", "bt709",
        "-color_primaries", "bt709", "-chroma_sample_location", "left",
        "-bsf:v", "h264_metadata=video_full_range_flag=0:colour_primaries=1:"
        "transfer_characteristics=1:matrix_coefficients=1",
        "-frames:v", str(int(chunk["frame_count"])), "-movflags", "+faststart", str(output),
    ])
    return command


def build_join_command(ffmpeg: str, concat_file: Path, output: Path) -> list[str]:
    return [
        ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-map", "0:v:0", "-c:v", "copy", "-an", "-movflags", "+faststart", str(output),
    ]


def build_audio_mux_command(
    ffmpeg: str, video: Path, source: Path, output: Path, *, has_audio: bool, duration_seconds: float
) -> list[str]:
    command = [ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(video)]
    if has_audio:
        command.extend(["-i", str(source)])
    else:
        command.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"])
    command.extend([
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
        "-profile:a", "aac_low", "-ar", "48000", "-ac", "2", "-b:a", "192k",
        "-af", "aresample=48000:async=1:first_pts=0,apad", "-shortest",
        "-t", "%.6f" % duration_seconds, "-movflags", "+faststart", str(output),
    ])
    return command


def _rate_is_30(value: Any) -> bool:
    try:
        return Fraction(str(value)) == CANVAS_FPS
    except (ValueError, ZeroDivisionError):
        return False


def _video_signature(info: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    video = info.get("video") if isinstance(info, Mapping) else None
    if not isinstance(video, Mapping):
        return None
    keys = (
        "codec_name", "profile", "level", "pix_fmt", "r_frame_rate", "avg_frame_rate",
        "time_base", "codec_tag_string", "extradata_size", "extradata_hash", "is_avc",
        "nal_length_size", "color_range", "color_space",
        "color_transfer", "color_primaries", "chroma_location", "field_order", "has_b_frames",
    )
    signature = {key: video.get(key) for key in keys}
    try:
        if (
            signature["codec_name"] != "h264"
            or str(signature["profile"] or "").lower() != "high"
            or int(signature["level"] or 0) <= 0
            or signature["pix_fmt"] != "yuv420p"
            or not _rate_is_30(signature["r_frame_rate"])
            or not _rate_is_30(signature["avg_frame_rate"])
            or not re.fullmatch(r"1/[1-9][0-9]*", str(signature["time_base"] or ""))
            or signature["codec_tag_string"] != "avc1"
            or int(signature["extradata_size"] or 0) <= 0
            or not re.fullmatch(r"SHA256:[0-9a-f]{64}", str(signature["extradata_hash"] or ""))
            or signature["is_avc"] != "true"
            or str(signature["nal_length_size"] or "") != "4"
            or signature["color_range"] != "tv"
            or signature["color_space"] != "bt709"
            or signature["color_transfer"] != "bt709"
            or signature["color_primaries"] != "bt709"
            or signature["chroma_location"] != "left"
            or signature["field_order"] != "progressive"
            or int(signature["has_b_frames"] or 0) != 0
        ):
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    return signature


def _video_contract(
    info: Mapping[str, Any], duration: float, *, expected_frames: int,
    audio: Optional[bool] = None,
) -> bool:
    video = info.get("video") if isinstance(info, Mapping) else None
    if not isinstance(video, Mapping) or _video_signature(info) is None:
        return False
    try:
        valid = (
            int(video.get("width") or 0) == CANVAS_WIDTH
            and int(video.get("height") or 0) == CANVAS_HEIGHT
            and int(video.get("nb_frames") or 0) == expected_frames
            and abs(float(info.get("duration") or 0) - float(duration)) <= 0.15
            and info.get("first_packet_keyframe") is True
        )
    except (TypeError, ValueError, OverflowError):
        return False
    if audio is not None:
        valid = valid and bool(info.get("has_audio")) is audio
    if valid and audio:
        stream = info.get("audio")
        try:
            valid = bool(
                isinstance(stream, Mapping)
                and stream.get("codec_name") == "aac"
                and str(stream.get("profile") or "").upper() == "LC"
                and int(stream.get("sample_rate") or 0) == 48000
                and int(stream.get("channels") or 0) == 2
                and stream.get("channel_layout") == "stereo"
                and stream.get("codec_tag_string") == "mp4a"
            )
        except (TypeError, ValueError, OverflowError):
            valid = False
    return valid


def _concat_line(path: Path) -> str:
    return "file '%s'" % str(path.resolve()).replace("'", "'\\''")


def _run_command(command: list[str], timeout: int, runner: Optional[Callable[..., Any]]) -> None:
    try:
        if runner is None:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
        else:
            runner(command, check=True, capture_output=True, text=True, timeout=timeout)
    except (TimeoutError, subprocess.TimeoutExpired):
        raise
    except Exception:
        raise


def _run_tracked_command(
    command: list[str], timeout: int, duration_seconds: float,
    runner: Optional[Callable[..., Any]], *, diagnostic_path: Optional[Path] = None,
    diagnostic_context: Optional[Mapping[str, Any]] = None,
) -> None:
    if runner is not None:
        _run_command(command, timeout, runner)
        return
    from .gpu import run_render_with_progress
    run_render_with_progress(
        command,
        timeout=timeout,
        absolute_timeout=timeout,
        configured_timeout=timeout,
        duration_seconds=duration_seconds,
        stall_timeout=min(900, timeout),
        diagnostic_path=diagnostic_path,
        diagnostic_context=diagnostic_context,
    )


def _preserve_process_evidence(exc: BaseException) -> bool:
    return getattr(exc, "code", "") in PROCESS_UNCERTAIN_CODES


def _confirmed_stopped_recovery(explicit: bool) -> bool:
    if type(explicit) is not bool:
        raise compositor_error()
    if explicit:
        return True
    try:
        from .async_runtime import capture_context
        context = capture_context()
        generation = getattr(context, "generation", None)
    except Exception:
        return False
    # AsyncRuntime only advances a generation after checking that every
    # recorded child is stopped and clearing the old child/launch ledger.
    return type(generation) is int and generation > 1


def _copy_durable(source: Path, destination: Path) -> None:
    durable_ensure_directory(destination.parent, mode=0o755)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".compositor-copy-", suffix=".mp4", dir=str(destination.parent))
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.unlink()
        try:
            # Cache and public job outputs normally share one data filesystem.
            # A hard link avoids a second multi-gigabyte copy while preserving
            # independent names and unlink lifetimes for immutable artifacts.
            os.link(source, temporary)
        except OSError:
            shutil.copyfile(source, temporary)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        if os.name == "posix":
            directory = os.open(str(destination.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise checkpoint_error() from None


def _recover_prepared_artifact(
    artifact_path: Path,
    marker_path: Path,
    prepare_path: Path,
    temporary_path: Path,
    identity: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    completed = load_completed(marker_path, artifact_path, identity)
    if completed is not None:
        for debris in (prepare_path, temporary_path):
            try:
                debris.unlink(missing_ok=True)
            except OSError:
                pass
        return completed
    prepared = read_record(prepare_path)
    if prepared is None:
        if artifact_path.exists() or artifact_path.is_symlink():
            raise checkpoint_error(conflict=True)
        return None
    if (
        set(prepared) != {"version", "identity", "artifact", "result", "temporary_name"}
        or type(prepared.get("version")) is not int
        or prepared.get("version") != 1
        or prepared.get("identity") != dict(identity)
        or prepared.get("temporary_name") != temporary_path.name
        or not isinstance(prepared.get("artifact"), Mapping)
        or not isinstance(prepared.get("result"), Mapping)
    ):
        raise checkpoint_error(conflict=prepared.get("identity") != dict(identity))
    fingerprint = dict(prepared["artifact"])
    candidate = artifact_path if artifact_path.exists() or artifact_path.is_symlink() else temporary_path
    if file_fingerprint(candidate) != fingerprint:
        raise checkpoint_error(conflict=True)
    if candidate == temporary_path:
        os.replace(temporary_path, artifact_path)
    save_completed(
        marker_path, artifact_path, identity, dict(prepared["result"]), fingerprint=fingerprint
    )
    try:
        prepare_path.unlink(missing_ok=True)
    except OSError:
        pass
    return dict(prepared["result"])


def _commit_prepared_artifact(
    temporary_path: Path,
    artifact_path: Path,
    marker_path: Path,
    prepare_path: Path,
    identity: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    fingerprint: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if artifact_path.exists() or artifact_path.is_symlink():
        raise checkpoint_error(conflict=True)
    if fingerprint is None:
        fingerprint = file_fingerprint(temporary_path)
    else:
        fingerprint = dict(fingerprint)
        try:
            valid_fingerprint = (
                set(fingerprint) == {"sha256", "size_bytes"}
                and bool(HEX.fullmatch(str(fingerprint.get("sha256") or "")))
                and type(fingerprint.get("size_bytes")) is int
                and fingerprint["size_bytes"] > 0
                and not temporary_path.is_symlink()
                and temporary_path.is_file()
                and temporary_path.stat().st_size == fingerprint["size_bytes"]
            )
        except OSError:
            valid_fingerprint = False
        if not valid_fingerprint:
            raise checkpoint_error()
    atomic_write_record(prepare_path, {
        "version": 1,
        "identity": dict(identity),
        "artifact": fingerprint,
        "result": dict(result),
        "temporary_name": temporary_path.name,
    })
    completed = _recover_prepared_artifact(
        artifact_path, marker_path, prepare_path, temporary_path, identity
    )
    if completed is None:
        raise checkpoint_error()
    return completed


def _public_result(record: Mapping[str, Any]) -> Dict[str, Any]:
    result = {
        "output_sha256": record.get("output_sha256"),
        "output_size": record.get("output_size"),
        "duration_seconds": record.get("duration_seconds"),
        "profile": record.get("profile"),
        "recipe_sha256": record.get("recipe_sha256"),
    }
    if (
        not HEX.fullmatch(str(result["output_sha256"] or ""))
        or isinstance(result["output_size"], bool)
        or not isinstance(result["output_size"], int)
        or result["output_size"] <= 0
        or result["profile"] != RECIPE_PROFILE
        or not HEX.fullmatch(str(result["recipe_sha256"] or ""))
    ):
        raise checkpoint_error()
    return result


def _discard_completed_intermediates(root: Path, chunks_root: Path) -> None:
    """Bound cache growth after the independently verified final is durable."""
    for path in (root / "joined.mp4", root / "concat.txt"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        shutil.rmtree(chunks_root)
    except OSError:
        pass


def _thread_lock(name: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(name, threading.Lock())


@contextmanager
def _composition_lock(root: Path):
    lock = _thread_lock(str(root))
    with lock:
        descriptor = None
        try:
            durable_ensure_directory(root)
            descriptor = os.open(str(root / ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
            if os.name == "posix":
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        except OSError:
            raise checkpoint_error() from None
        finally:
            if descriptor is not None:
                try:
                    if os.name == "posix":
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)


def _validate_recipe_and_assets(recipe: Mapping[str, Any], asset_root: Path, manifest_sha256: str):
    if (
        not isinstance(recipe, Mapping)
        or recipe.get("profile") != RECIPE_PROFILE
        or type(recipe.get("version")) is not int
        or recipe.get("version") != 1
    ):
        raise DramaSynthesisError("drama_recipe_profile_mismatch", "随机模板配方版本不一致", 409)
    supplied_sha = str(recipe.get("recipe_sha256") or "")
    actual_sha = hashlib.sha256(_canonical_recipe(recipe).encode("utf-8")).hexdigest()
    if not HEX.fullmatch(supplied_sha) or supplied_sha != actual_sha:
        raise DramaSynthesisError("drama_recipe_hash_invalid", "随机模板配方指纹无效", 409)
    try:
        assets = load_asset_set(asset_root, str(manifest_sha256 or "").lower())
    except Exception:
        raise DramaSynthesisError(
            "drama_random_assets_unavailable", "随机模板素材暂不可用", 503
        ) from None
    try:
        for key in ("rotation_millidegrees", "scale_bp", "tint_opacity_bp"):
            if type(recipe.get(key)) is not int:
                raise ValueError()
        fb_recipe = {
            "asset_set_sha256": recipe.get("asset_set_sha256"),
            "assets": recipe.get("assets"),
            "rotation_millidegrees": recipe.get("rotation_millidegrees"),
            "scale_bp": recipe.get("scale_bp"),
            "tint_opacity_bp": recipe.get("tint_opacity_bp"),
            "version": 1,
        }
        validate_recipe(fb_recipe, assets)
    except Exception:
        raise DramaSynthesisError("drama_recipe_asset_mismatch", "随机模板配方与GPU素材不一致", 409) from None
    return supplied_sha, assets, fb_recipe


def render_chunked_random_output(
    *,
    source: Union[str, os.PathLike],
    output: Union[str, os.PathLike],
    recipe: Mapping[str, Any],
    asset_root: Union[str, os.PathLike],
    manifest_sha256: str,
    ffmpeg: str = "/usr/bin/ffmpeg",
    ffprobe: str = "/usr/bin/ffprobe",
    timeout: Optional[int] = None,
    confirmed_stopped_recovery: bool = False,
    runtime_probe: Optional[Callable[[str], Mapping[str, Any]]] = None,
    runner: Optional[Callable[..., Any]] = None,
    probe: Optional[Callable[[str, Path], Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Render frame-exact chunks, then stream-copy join and mux one audio track."""
    source_path, output_path = Path(source), Path(output)
    may_clean_partial = _confirmed_stopped_recovery(confirmed_stopped_recovery)
    if not source_path.is_file() or source_path.is_symlink():
        raise DramaSynthesisError("drama_random_source_missing", "随机模板源视频不存在", 502)
    probe = probe or _probe
    supplied_sha, asset_set, fb_recipe = _validate_recipe_and_assets(
        recipe, Path(asset_root), manifest_sha256
    )
    source_info = dict(probe(ffprobe, source_path))
    spec = compile_random_overlay_spec(recipe, source_info)
    scene_sha = composition_sha256(spec)
    kernel = compile_opencl_kernel(spec)
    source_fingerprint = file_fingerprint(source_path)
    runtime_fingerprint = dict(
        _runtime_fingerprint(ffmpeg) if runtime_probe is None else runtime_probe(ffmpeg)
    )
    if set(runtime_fingerprint) != {
        "version", "release_sha", "declared_identity", "ffmpeg", "opencl_device",
        "gpu_driver_identity_sha256",
    }:
        raise compositor_error()
    runtime_ffmpeg = runtime_fingerprint.get("ffmpeg")
    if (
        runtime_fingerprint.get("version") != 1
        or not re.fullmatch(r"[0-9a-f]{40}", str(runtime_fingerprint.get("release_sha") or ""))
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{7,99}",
            str(runtime_fingerprint.get("declared_identity") or ""),
        )
        or not isinstance(runtime_ffmpeg, Mapping)
        or set(runtime_ffmpeg) != {"sha256", "size_bytes"}
        or not HEX.fullmatch(str(runtime_ffmpeg.get("sha256") or ""))
        or type(runtime_ffmpeg.get("size_bytes")) is not int
        or runtime_ffmpeg["size_bytes"] <= 0
        or not re.fullmatch(r"[0-9]{1,2}\.[0-9]{1,2}", str(runtime_fingerprint.get("opencl_device") or ""))
        or not HEX.fullmatch(str(runtime_fingerprint.get("gpu_driver_identity_sha256") or ""))
    ):
        raise compositor_error()
    chunks = plan_chunks(spec["timeline"]["total_frames"])
    chunk_plan_sha = hashlib.sha256(canonical_json({"chunks": chunks}).encode("utf-8")).hexdigest()
    content_identity = {
        "version": 2,
        "backend": BACKEND,
        "renderer_profile": RENDERER_PROFILE,
        "runtime": runtime_fingerprint,
        "recipe_sha256": supplied_sha,
        "source": source_fingerprint,
        "composition_sha256": scene_sha,
        "kernel_sha256": kernel["sha256"],
        "chunk_plan_sha256": chunk_plan_sha,
        "chunk_count": len(chunks),
    }
    content_sha = hashlib.sha256(canonical_json(content_identity).encode("utf-8")).hexdigest()
    root = cache_root() / content_sha[:2] / content_sha
    chunks_root = root / "chunks"
    kernel_path = root / "kernels" / (kernel["sha256"] + ".cl")
    final_cache = root / "final.mp4"
    final_marker = root / "final.json"
    output_marker = output_path.with_name(output_path.name + ".render.json")
    output_prepare = output_path.with_name(output_path.name + ".render.prepared.json")
    output_temporary = output_path.with_name(
        ".random-render-v2-%s.mp4" % content_sha[:16]
    )
    # Keep the public artifact marker byte-for-byte compatible with the legacy
    # renderer. A code rollback can therefore reuse a completed V2 artifact or
    # finish its prepared rename without rendering from zero.
    output_identity = {
        "kind": "random_template_render",
        "source": source_fingerprint,
        "recipe_sha256": supplied_sha,
        "profile": RECIPE_PROFILE,
        "asset_set_sha256": str(manifest_sha256).lower(),
    }
    cached_output = _recover_prepared_artifact(
        output_path, output_marker, output_prepare, output_temporary, output_identity
    )
    if cached_output is not None:
        if not _video_contract(
            probe(ffprobe, output_path), spec["timeline"]["duration_seconds"],
            expected_frames=spec["timeline"]["total_frames"], audio=True,
        ):
            raise checkpoint_error(conflict=True)
        return _public_result(cached_output)
    if output_path.exists() or output_path.is_symlink():
        raise checkpoint_error(conflict=True)
    selected = {key: Path(value) for key, value in selected_asset_paths(fb_recipe, asset_set).items()}
    media_types = {key: fb_recipe["assets"][key]["media_type"] for key in selected}
    asset_durations = {}
    for key, path in selected.items():
        if media_types[key] == "video/webm":
            asset_durations[key] = float(probe(ffprobe, path)["duration"])
    render_timeout = chunk_timeout_seconds(timeout)

    with _composition_lock(root):
        final_tmp = root / ".final.tmp.mp4"
        final_prepare = root / "final.prepare.json"
        completed_final = _recover_prepared_artifact(
            final_cache, final_marker, final_prepare, final_tmp, content_identity
        )
        if completed_final is None:
            _atomic_write_text(kernel_path, kernel["source"])
            if hashlib.sha256(kernel_path.read_bytes()).hexdigest() != kernel["sha256"]:
                raise checkpoint_error(conflict=True)
            durable_ensure_directory(chunks_root)
            chunk_paths = [chunks_root / ("chunk-%05d.mp4" % row["index"]) for row in chunks]
            chunk_infos: Dict[int, Mapping[str, Any]] = {}
            chunk_infos_lock = threading.Lock()

            def render_chunk(row: Mapping[str, Any]) -> Path:
                chunk_path = chunks_root / ("chunk-%05d.mp4" % row["index"])
                marker = chunks_root / ("chunk-%05d.json" % row["index"])
                prepare = chunks_root / ("chunk-%05d.prepare.json" % row["index"])
                identity = {**content_identity, "chunk": dict(row)}
                temporary = chunks_root / (".chunk-%05d.tmp.mp4" % row["index"])
                completed = _recover_prepared_artifact(
                    chunk_path, marker, prepare, temporary, identity
                )
                info = None
                if completed is None:
                    render_needed = not (temporary.exists() or temporary.is_symlink())
                    if not render_needed:
                        try:
                            if temporary.is_symlink() or not temporary.is_file():
                                raise ValueError()
                            info = dict(probe(ffprobe, temporary))
                            if not _video_contract(
                                info, row["duration_seconds"],
                                expected_frames=row["frame_count"], audio=False,
                            ):
                                raise ValueError()
                        except Exception:
                            if not may_clean_partial or temporary.is_symlink() or not temporary.is_file():
                                raise checkpoint_error() from None
                            temporary.unlink()
                            render_needed = True
                            info = None
                    if render_needed:
                        command = build_opencl_chunk_command(
                            ffmpeg=ffmpeg,
                            source=source_path,
                            output=temporary,
                            spec=spec,
                            assets=selected,
                            asset_media_types=media_types,
                            asset_durations=asset_durations,
                            chunk=row,
                            kernel_path=kernel_path,
                        )
                        try:
                            if runner is None:
                                from .gpu import run_render_with_progress
                                run_render_with_progress(
                                    command,
                                    timeout=render_timeout,
                                    absolute_timeout=render_timeout,
                                    configured_timeout=render_timeout,
                                    duration_seconds=row["duration_seconds"],
                                    stall_timeout=900,
                                    progress_offset_seconds=row["start_seconds"],
                                    progress_total_seconds=spec["timeline"]["duration_seconds"],
                                    progress_frame_offset=row["start_frame"],
                                    diagnostic_path=chunks_root / ("chunk-%05d.failure.json" % row["index"]),
                                    diagnostic_context={
                                        "composition_sha256": scene_sha,
                                        "kernel_sha256": kernel["sha256"],
                                        "recipe_sha256": supplied_sha,
                                        "asset_set_sha256": str(manifest_sha256).lower(),
                                        "source_sha256": source_fingerprint["sha256"],
                                        "source_size_bytes": source_fingerprint["size_bytes"],
                                        "chunk_index": row["index"],
                                    },
                                )
                            else:
                                _run_command(command, render_timeout, runner)
                        except (TimeoutError, subprocess.TimeoutExpired):
                            temporary.unlink(missing_ok=True)
                            raise compositor_error("drama_render_chunk_timeout", 504) from None
                        except DramaSynthesisError as exc:
                            if _preserve_process_evidence(exc):
                                raise
                            temporary.unlink(missing_ok=True)
                            if exc.code == "drama_random_render_timeout":
                                raise compositor_error("drama_render_chunk_timeout", 504) from None
                            raise compositor_error("drama_render_chunk_failed", 502) from None
                        except Exception:
                            temporary.unlink(missing_ok=True)
                            raise compositor_error("drama_render_chunk_failed", 502) from None
                        info = dict(probe(ffprobe, temporary))
                        if not _video_contract(
                            info, row["duration_seconds"], expected_frames=row["frame_count"], audio=False,
                        ):
                            temporary.unlink(missing_ok=True)
                            raise compositor_error("drama_render_chunk_failed", 502)
                    completed = {
                        "index": row["index"], "start_frame": row["start_frame"],
                        "frame_count": row["frame_count"], "duration_seconds": info["duration"],
                    }
                    completed = _commit_prepared_artifact(
                        temporary, chunk_path, marker, prepare, identity, completed
                    )
                if info is None:
                    info = dict(probe(ffprobe, chunk_path))
                    if not _video_contract(
                        info, row["duration_seconds"], expected_frames=row["frame_count"], audio=False,
                    ):
                        raise checkpoint_error(conflict=True)
                with chunk_infos_lock:
                    chunk_infos[int(row["index"])] = info
                return chunk_path

            lanes = compositor_lanes()
            if lanes == 1 or len(chunks) == 1:
                for row in chunks:
                    render_chunk(row)
            else:
                executor = ThreadPoolExecutor(max_workers=lanes, thread_name_prefix="drama-compositor")
                futures = []
                try:
                    for row in chunks:
                        context = copy_context()
                        futures.append(executor.submit(context.run, render_chunk, row))
                    for future in as_completed(futures):
                        future.result()
                except BaseException:
                    for future in futures:
                        future.cancel()
                    executor.shutdown(wait=True, cancel_futures=True)
                    raise
                else:
                    executor.shutdown(wait=True)
            signatures = [_video_signature(chunk_infos[index]) for index in range(len(chunks))]
            if any(signature is None for signature in signatures) or len({
                canonical_json(signature) for signature in signatures if signature is not None
            }) != 1:
                raise compositor_error("drama_render_join_failed", 502)
            concat_file = root / "concat.txt"
            _atomic_write_text(concat_file, "\n".join(_concat_line(path) for path in chunk_paths) + "\n")
            joined = root / "joined.mp4"
            joined_tmp = root / ".joined.tmp.mp4"

            def joined_contract(path: Path) -> bool:
                try:
                    if path.is_symlink() or not path.is_file():
                        return False
                    info = dict(probe(ffprobe, path))
                except Exception:
                    return False
                return bool(
                    _video_contract(
                        info, spec["timeline"]["duration_seconds"],
                        expected_frames=spec["timeline"]["total_frames"], audio=False,
                    )
                    and _video_signature(info) == signatures[0]
                )

            try:
                joined_candidate = joined if joined.exists() or joined.is_symlink() else joined_tmp
                recovered_join_candidate = joined_candidate.exists() or joined_candidate.is_symlink()
                if recovered_join_candidate and not joined_contract(joined_candidate):
                    if (
                        not may_clean_partial
                        or joined_candidate.is_symlink()
                        or not joined_candidate.is_file()
                    ):
                        raise checkpoint_error(conflict=joined_candidate == joined)
                    joined_candidate.unlink()
                    joined_candidate = joined_tmp
                    recovered_join_candidate = False
                if not recovered_join_candidate:
                    _run_tracked_command(
                        build_join_command(ffmpeg, concat_file, joined_tmp), render_timeout,
                        spec["timeline"]["duration_seconds"], runner,
                        diagnostic_path=root / "join.failure.json",
                        diagnostic_context={
                            "composition_sha256": scene_sha,
                            "kernel_sha256": kernel["sha256"],
                            "recipe_sha256": supplied_sha,
                            "asset_set_sha256": str(manifest_sha256).lower(),
                            "source_sha256": source_fingerprint["sha256"],
                            "source_size_bytes": source_fingerprint["size_bytes"],
                        },
                    )
                    joined_candidate = joined_tmp
                if not joined_contract(joined_candidate):
                    raise compositor_error("drama_render_join_failed", 502)
                if joined_candidate == joined_tmp:
                    os.replace(joined_tmp, joined)
            except DramaSynthesisError as exc:
                if _preserve_process_evidence(exc):
                    raise
                joined_tmp.unlink(missing_ok=True)
                raise compositor_error("drama_render_join_failed", 502) from None
            except Exception:
                joined_tmp.unlink(missing_ok=True)
                raise compositor_error("drama_render_join_failed", 502) from None
            try:
                recovered_final_tmp = final_tmp.exists() or final_tmp.is_symlink()
                final_info = None
                if recovered_final_tmp:
                    try:
                        if final_tmp.is_symlink() or not final_tmp.is_file():
                            raise ValueError()
                        final_info = dict(probe(ffprobe, final_tmp))
                        if not _video_contract(
                            final_info, spec["timeline"]["duration_seconds"],
                            expected_frames=spec["timeline"]["total_frames"], audio=True,
                        ):
                            raise ValueError()
                    except Exception:
                        if not may_clean_partial or final_tmp.is_symlink() or not final_tmp.is_file():
                            raise checkpoint_error() from None
                        final_tmp.unlink()
                        recovered_final_tmp = False
                        final_info = None
                if not recovered_final_tmp:
                    _run_tracked_command(
                        build_audio_mux_command(
                            ffmpeg, joined, source_path, final_tmp,
                            has_audio=bool(source_info.get("has_audio")),
                            duration_seconds=spec["timeline"]["duration_seconds"],
                        ),
                        render_timeout, spec["timeline"]["duration_seconds"], runner,
                        diagnostic_path=root / "audio-mux.failure.json",
                        diagnostic_context={
                            "composition_sha256": scene_sha,
                            "kernel_sha256": kernel["sha256"],
                            "recipe_sha256": supplied_sha,
                            "asset_set_sha256": str(manifest_sha256).lower(),
                            "source_sha256": source_fingerprint["sha256"],
                            "source_size_bytes": source_fingerprint["size_bytes"],
                        },
                    )
                    final_info = dict(probe(ffprobe, final_tmp))
                    if not _video_contract(
                        final_info, spec["timeline"]["duration_seconds"],
                        expected_frames=spec["timeline"]["total_frames"], audio=True,
                    ):
                        raise compositor_error("drama_render_audio_mux_failed", 502)
                fingerprint = file_fingerprint(final_tmp)
                completed_final = {
                    "output_sha256": fingerprint["sha256"],
                    "output_size": fingerprint["size_bytes"],
                    "duration_seconds": final_info["duration"],
                    "profile": RECIPE_PROFILE,
                    "recipe_sha256": supplied_sha,
                    "renderer_profile": RENDERER_PROFILE,
                    "composition_sha256": scene_sha,
                    "chunk_count": len(chunks),
                    "render_backend": BACKEND,
                }
                completed_final = _commit_prepared_artifact(
                    final_tmp, final_cache, final_marker, final_prepare,
                    content_identity, completed_final, fingerprint=fingerprint,
                )
            except DramaSynthesisError as exc:
                if _preserve_process_evidence(exc):
                    raise
                final_tmp.unlink(missing_ok=True)
                raise compositor_error("drama_render_audio_mux_failed", 502) from None
            except Exception:
                final_tmp.unlink(missing_ok=True)
                raise compositor_error("drama_render_audio_mux_failed", 502) from None
        _discard_completed_intermediates(root, chunks_root)
        if not _video_contract(
            probe(ffprobe, final_cache), spec["timeline"]["duration_seconds"],
            expected_frames=spec["timeline"]["total_frames"], audio=True,
        ):
            raise checkpoint_error(conflict=True)
        cached_output = _recover_prepared_artifact(
            output_path, output_marker, output_prepare, output_temporary, output_identity
        )
        if cached_output is not None:
            if not _video_contract(
                probe(ffprobe, output_path), spec["timeline"]["duration_seconds"],
                expected_frames=spec["timeline"]["total_frames"], audio=True,
            ):
                raise checkpoint_error(conflict=True)
            return _public_result(cached_output)
        recovered_output_temporary = output_temporary.exists() or output_temporary.is_symlink()
        output_fingerprint = None
        if recovered_output_temporary:
            try:
                if output_temporary.is_symlink() or not output_temporary.is_file():
                    raise ValueError()
                recovered_output_info = probe(ffprobe, output_temporary)
                if not _video_contract(
                    recovered_output_info, spec["timeline"]["duration_seconds"],
                    expected_frames=spec["timeline"]["total_frames"], audio=True,
                ):
                    raise ValueError()
                output_fingerprint = file_fingerprint(output_temporary)
                if output_fingerprint["sha256"] != completed_final["output_sha256"]:
                    raise ValueError()
            except Exception:
                if (
                    not may_clean_partial
                    or output_temporary.is_symlink()
                    or not output_temporary.is_file()
                ):
                    raise checkpoint_error() from None
                output_temporary.unlink()
                recovered_output_temporary = False
                output_fingerprint = None
        if not recovered_output_temporary:
            _copy_durable(final_cache, output_temporary)
            output_fingerprint = file_fingerprint(output_temporary)
        if output_fingerprint is None:
            raise checkpoint_error()
        if output_fingerprint["sha256"] != completed_final["output_sha256"]:
            raise checkpoint_error(conflict=True)
        completed_final = _commit_prepared_artifact(
            output_temporary, output_path, output_marker, output_prepare,
            output_identity, _public_result(completed_final), fingerprint=output_fingerprint,
        )
        return _public_result(completed_final)


__all__ = [
    "BACKEND", "DEFAULT_CHUNK_TIMEOUT_SECONDS", "build_audio_mux_command", "build_join_command",
    "build_opencl_chunk_command", "cache_root", "chunk_timeout_seconds", "compile_opencl_kernel",
    "compositor_filter_threads", "compositor_lanes",
    "opencl_device", "render_chunked_random_output", "runtime_identity",
]
