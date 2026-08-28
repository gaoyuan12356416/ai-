"""HK-GPU random-template rendering adapter.

It reuses the verified FB random-overlay asset manifest and FFmpeg graph, while
keeping a drama-specific immutable profile and result identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Mapping, Optional, Union

from features.fb_gpu.prepare_worker import build_command
from features.fb_gpu.random_overlay import (
    load_asset_set,
    selected_asset_paths,
    validate_recipe,
)

from .core import DramaSynthesisError, RECIPE_CATEGORIES, RECIPE_PROFILE
from .local_checkpoint import (
    atomic_write_record, checkpoint_error, file_fingerprint, load_completed, read_record, save_completed,
)


DEFAULT_RENDER_TIMEOUT_SECONDS = 43200
MAX_RENDER_TIMEOUT_SECONDS = 86400


def render_timeout_seconds(value=None):
    """The renderer's deadline is independent of HTTP and result polling limits."""
    raw = os.environ.get("DRAMA_GPU_RENDER_TIMEOUT", str(DEFAULT_RENDER_TIMEOUT_SECONDS)) if value is None else value
    if (isinstance(raw, bool) or not re.fullmatch(r"[0-9]{1,6}", str(raw)) or
            not 60 <= int(raw) <= MAX_RENDER_TIMEOUT_SECONDS):
        raise DramaSynthesisError("drama_random_timeout_configuration_invalid", "随机模板制作时限配置无效", 503)
    return int(raw)


def catalog_from_assets(asset_root: Union[str, os.PathLike], manifest_sha256: str) -> Dict[str, Any]:
    assets = load_asset_set(Path(asset_root), str(manifest_sha256 or "").lower())
    categories = {}
    for category in RECIPE_CATEGORIES:
        categories[category] = [
            {key: row[key] for key in ("name", "sha256", "media_type", "size")}
            for row in assets["categories"][category]
        ]
    return {"version": 1, "profile": RECIPE_PROFILE, "manifest_sha256": assets["manifest_sha256"], "categories": categories}


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _probe(ffprobe: str, path: Path) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        payload = json.loads(proc.stdout)
    except Exception:
        raise DramaSynthesisError("drama_random_probe_failed", "随机模板视频校验失败", 502) from None
    streams = payload.get("streams") if isinstance(payload, Mapping) else []
    video = next((row for row in streams if row.get("codec_type") == "video"), {})
    audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
    try:
        duration = float((payload.get("format") or {}).get("duration") or video.get("duration") or 0)
    except (TypeError, ValueError, OverflowError):
        duration = 0
    if not math.isfinite(duration) or duration <= 0:
        raise DramaSynthesisError("drama_random_probe_failed", "随机模板视频时长无效", 502)
    return {"duration": duration, "has_audio": audio is not None, "video": video, "audio": audio}


def build_drama_random_command(config, source, output, info, recipe, assets):
    command = list(build_command(config, source, output, info, recipe, assets))
    prefix = "[0:v]setpts=PTS-STARTPTS,"
    if command.count("-filter_complex") != 1 or "-copyts" in command:
        raise DramaSynthesisError("drama_random_graph_contract_invalid", "随机模板处理配置不兼容", 503)
    graph_index = command.index("-filter_complex") + 1
    if graph_index >= len(command) or not command[graph_index].startswith(prefix) or command[graph_index].count(prefix) != 1:
        raise DramaSynthesisError("drama_random_graph_contract_invalid", "随机模板处理配置不兼容", 503)
    # A 16:9 intro followed by portrait episodes reinitializes the filter graph.
    # Demuxer timestamps are already rebased by FFmpeg's default input handling;
    # resetting them again at every reinit silently drops earlier seconds.
    command[graph_index] = command[graph_index].replace(prefix, "[0:v]setpts=PTS,", 1)
    # Default to the dedicated worker's two-core budget. The inherited FB graph's
    # automatic per-filter thread pools exceed this service's TasksMax=128.
    # This is a drama-only invocation override, not a change to the FB worker.
    threads = os.environ.get("DRAMA_GPU_FILTER_THREADS", "2")
    if not re.fullmatch(r"[1-4]", threads):
        raise DramaSynthesisError("drama_random_thread_configuration_invalid", "随机模板线程配置无效", 503)
    return [command[0], "-filter_complex_threads", threads, *command[1:]]


def ffmpeg_progress_metrics(values, duration_seconds):
    metrics = {"duration_seconds": float(duration_seconds)}
    for field in ("out_time_us", "out_time_ms"):
        try:
            seconds = float(values.get(field, "")) / 1000000
            if math.isfinite(seconds) and seconds >= 0:
                metrics["out_time_seconds"] = seconds
                break
        except (ValueError, TypeError, OverflowError):
            pass
    if "out_time_seconds" not in metrics:
        match = re.fullmatch(r"([0-9]+):([0-9]{2}):([0-9]{2}(?:\.[0-9]+)?)", str(values.get("out_time", "")))
        if match:
            metrics["out_time_seconds"] = int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])
    for key in ("frame", "fps", "speed"):
        try:
            value = float(str(values.get(key, "")).removesuffix("x"))
            if math.isfinite(value) and value >= 0:
                metrics[key] = int(value) if key == "frame" else value
        except (ValueError, TypeError, OverflowError):
            pass
    return metrics


def run_render_with_progress(command, *, timeout, duration_seconds, popen=None, progress_callback=None):
    """Track the real child and stream bounded FFmpeg telemetry without stderr logs."""
    from .async_runtime import clear_process, emit_progress, process_launch, record_process

    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
            not 0 < timeout <= MAX_RENDER_TIMEOUT_SECONDS):
        raise DramaSynthesisError("drama_random_timeout_configuration_invalid", "随机模板制作时限配置无效", 503)
    popen = popen or subprocess.Popen
    tracked_command = [command[0], "-progress", "pipe:1", "-nostats", *command[1:]]
    updates = queue.Queue(maxsize=8)
    proc = None
    reader = None

    def emit(metrics):
        emit_progress("rendering_random", **metrics)
        if progress_callback:
            progress_callback(dict(metrics))

    def read_progress():
        values = {}
        try:
            for line in proc.stdout:
                key, separator, value = line.strip().partition("=")
                if separator and key in {"out_time_us", "out_time_ms", "out_time", "frame", "fps", "speed", "progress"}:
                    values[key] = value[:128]
                if key == "progress":
                    if updates.full():
                        try:
                            updates.get_nowait()
                        except queue.Empty:
                            pass
                    try:
                        updates.put_nowait(values)
                    except queue.Full:
                        pass
                    values = {}
        except (OSError, ValueError):
            return

    def report_updates():
        latest = None
        while True:
            try:
                latest = updates.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            emit(ffmpeg_progress_metrics(latest, duration_seconds))

    with tempfile.TemporaryFile() as errors:
        try:
            with process_launch():
                proc = popen(tracked_command, stdout=subprocess.PIPE, stderr=errors, text=True,
                             encoding="utf-8", errors="replace", start_new_session=os.name == "posix")
                record_process(proc.pid)
            reader = threading.Thread(target=read_progress, daemon=True)
            reader.start()
            deadline = time.monotonic() + timeout
            emit({"out_time_seconds": 0, "duration_seconds": duration_seconds})
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("random render deadline exceeded")
                try:
                    code = proc.wait(timeout=min(1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    report_updates()
            reader.join(timeout=5)
            report_updates()
            if code != 0:
                raise RuntimeError("random render failed")
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=30)
                # Do not clear a process merely because kill was requested.
                if proc.poll() is not None:
                    clear_process(proc.pid)
                if proc.stdout is not None:
                    proc.stdout.close()
                if reader is not None:
                    reader.join(timeout=2)


def random_output_duration_matches(source_seconds, output_seconds, video_seconds):
    try:
        values = [float(value) for value in (source_seconds, output_seconds, video_seconds)]
    except (ValueError, TypeError, OverflowError):
        return False
    # Fixed tolerance covers 30fps/AAC rounding, never a whole missing intro.
    return all(math.isfinite(value) and value > 0 for value in values) and all(
        abs(values[0] - value) <= 0.15 for value in values[1:]
    )


def _validate_render_result(result, source_duration, recipe_sha):
    if (not isinstance(result, dict) or
            set(result) != {"output_sha256", "output_size", "duration_seconds", "profile", "recipe_sha256"} or
            not re.fullmatch(r"[0-9a-f]{64}", str(result.get("output_sha256"))) or
            type(result.get("output_size")) is not int or result["output_size"] <= 0 or
            result.get("profile") != RECIPE_PROFILE or result.get("recipe_sha256") != recipe_sha or
            not random_output_duration_matches(source_duration, result.get("duration_seconds"), result.get("duration_seconds"))):
        raise checkpoint_error()


def _commit_prepared_render(prepared_path, checkpoint_path, output_path, identity, source_duration, recipe_sha,
                            completed_result=None):
    """Recover only a fingerprinted, validated render from our durable prepare.

    A crash can leave either the named temporary artifact or the renamed output.
    The prepare is retained until the completed marker is durable; neither state
    starts another encoder. Unknown output files are never adopted or replaced.
    """
    prepared = read_record(prepared_path)
    if prepared is None:
        return None
    if (set(prepared) != {"version", "identity", "artifact", "result", "temporary_name"} or
            type(prepared["version"]) is not int or prepared["version"] != 1 or
            not isinstance(prepared["temporary_name"], str) or
            not re.fullmatch(r"\.random-render-[A-Za-z0-9_-]{1,80}\.mp4", prepared["temporary_name"])):
        raise checkpoint_error()
    if prepared["identity"] != identity:
        raise checkpoint_error(conflict=True)
    result, artifact = prepared["result"], prepared["artifact"]
    _validate_render_result(result, source_duration, recipe_sha)
    if completed_result is not None and result != completed_result:
        raise checkpoint_error(conflict=True)
    if (not isinstance(artifact, dict) or set(artifact) != {"sha256", "size_bytes"} or
            type(artifact["size_bytes"]) is not int or
            artifact != {"sha256": result["output_sha256"], "size_bytes": result["output_size"]}):
        raise checkpoint_error()
    temporary_path = output_path.parent / prepared["temporary_name"]
    if temporary_path == output_path or output_path.is_symlink() or temporary_path.is_symlink():
        raise checkpoint_error()
    if output_path.exists():
        if file_fingerprint(output_path) != artifact:
            raise checkpoint_error()
        if temporary_path.exists() and file_fingerprint(temporary_path) != artifact:
            raise checkpoint_error()
    elif file_fingerprint(temporary_path) != artifact:
        raise checkpoint_error()
    try:
        if not output_path.exists():
            os.replace(temporary_path, output_path)
        if completed_result is None:
            save_completed(checkpoint_path, output_path, identity, result, fingerprint=artifact)
        # save_completed fsyncs the directory on POSIX, committing both the
        # artifact rename and the completed marker before prepare is removed.
        temporary_path.unlink(missing_ok=True)
        prepared_path.unlink()
    except OSError:
        raise checkpoint_error() from None
    return result


def render_random_output(
    *,
    source: Union[str, os.PathLike],
    output: Union[str, os.PathLike],
    recipe: Mapping[str, Any],
    asset_root: Union[str, os.PathLike],
    manifest_sha256: str,
    ffmpeg: str = "/usr/bin/ffmpeg",
    ffprobe: str = "/usr/bin/ffprobe",
    timeout: Optional[int] = None,
    runner=None,
) -> Dict[str, Any]:
    source_path, output_path = Path(source), Path(output)
    if not source_path.is_file() or source_path.is_symlink():
        raise DramaSynthesisError("drama_random_source_missing", "随机模板源视频不存在", 502)
    if recipe.get("profile") != RECIPE_PROFILE or int(recipe.get("version") or 0) != 1:
        raise DramaSynthesisError("drama_recipe_profile_mismatch", "随机模板配方版本不一致", 409)
    supplied_sha = str(recipe.get("recipe_sha256") or "")
    unsigned = {key: value for key, value in recipe.items() if key != "recipe_sha256"}
    actual_sha = hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", supplied_sha) or supplied_sha != actual_sha:
        raise DramaSynthesisError("drama_recipe_hash_invalid", "随机模板配方指纹无效", 409)
    assets = load_asset_set(Path(asset_root), str(manifest_sha256 or "").lower())
    fb_recipe = {
        "asset_set_sha256": recipe.get("asset_set_sha256"),
        "assets": recipe.get("assets"),
        "rotation_millidegrees": int(recipe.get("rotation_millidegrees") or 0),
        "scale_bp": int(recipe.get("scale_bp") or 0),
        "tint_opacity_bp": int(recipe.get("tint_opacity_bp") or 0),
        "version": 1,
    }
    try:
        validate_recipe(fb_recipe, assets)
    except Exception:
        raise DramaSynthesisError("drama_recipe_asset_mismatch", "随机模板配方与GPU素材不一致", 409) from None
    source_info = _probe(ffprobe, source_path)
    identity = {
        "kind": "random_template_render", "source": file_fingerprint(source_path),
        "recipe_sha256": supplied_sha, "profile": RECIPE_PROFILE,
        "asset_set_sha256": str(manifest_sha256).lower(),
    }
    checkpoint_path = output_path.with_name(output_path.name + ".render.json")
    prepared_path = output_path.with_name(output_path.name + ".render.prepared.json")
    cached = load_completed(checkpoint_path, output_path, identity)
    if cached is not None:
        _validate_render_result(cached, source_info["duration"], supplied_sha)
        _commit_prepared_render(prepared_path, checkpoint_path, output_path, identity,
                                source_info["duration"], supplied_sha, completed_result=cached)
        return cached
    prepared = _commit_prepared_render(prepared_path, checkpoint_path, output_path, identity,
                                       source_info["duration"], supplied_sha)
    if prepared is not None:
        return prepared
    if output_path.exists() or output_path.is_symlink():
        # A legacy/unmarked final could be a valid render from an interrupted
        # old process. Without its identity evidence, do not overwrite it.
        raise checkpoint_error()
    render_timeout = render_timeout_seconds(timeout)
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".random-render-", suffix=".mp4", dir=str(output_path.parent))
    os.close(fd)
    temporary_path = Path(temporary)
    config = SimpleNamespace(ffmpeg=ffmpeg)
    started_record = {"version": 1, "identity": identity, "artifact": None,
                      "result": None, "temporary_name": temporary_path.name}
    verified_render = False
    try:
        # A start guard is intentionally not a completed render. If the process
        # crashes before its validated prepare is durable, replay fails closed
        # and retains the named artifact instead of silently rendering again.
        try:
            atomic_write_record(prepared_path, started_record)
        except OSError:
            raise checkpoint_error() from None
        command = build_drama_random_command(config, source_path, temporary_path, source_info, fb_recipe, selected_asset_paths(fb_recipe, assets))
        try:
            if runner is None:
                run_render_with_progress(command, timeout=render_timeout, duration_seconds=source_info["duration"])
            else:
                runner(command, check=True, capture_output=True, text=True, timeout=render_timeout)
        except DramaSynthesisError:
            raise
        except (TimeoutError, subprocess.TimeoutExpired):
            raise DramaSynthesisError("drama_random_render_timeout", "随机模板视频制作超时", 504) from None
        except Exception:
            raise DramaSynthesisError("drama_random_render_failed", "随机模板视频制作失败", 502) from None
        result_info = _probe(ffprobe, temporary_path)
        video = result_info["video"]
        if not random_output_duration_matches(source_info["duration"], result_info["duration"], video.get("duration")):
            raise DramaSynthesisError("drama_random_duration_mismatch", "随机模板成片时长与源视频不一致，已阻止上传", 502)
        if (
            video.get("codec_name") != "h264"
            or str(video.get("profile") or "").lower() != "high"
            or int(video.get("width") or 0) != 720
            or int(video.get("height") or 0) != 1280
        ):
            raise DramaSynthesisError("drama_random_output_contract_invalid", "随机模板成片规格不符合要求", 502)
        fingerprint = file_fingerprint(temporary_path)
        result = {
            "output_sha256": fingerprint["sha256"], "output_size": fingerprint["size_bytes"],
            "duration_seconds": result_info["duration"], "profile": RECIPE_PROFILE,
            "recipe_sha256": supplied_sha,
        }
        verified_render = True
        try:
            # Windows fsync requires a writable handle; no bytes are modified.
            with temporary_path.open("r+b") as rendered:
                os.fsync(rendered.fileno())
            atomic_write_record(prepared_path, {
                "version": 1, "identity": identity, "artifact": fingerprint,
                "result": result, "temporary_name": temporary_path.name,
            })
        except OSError:
            raise checkpoint_error() from None
        return _commit_prepared_render(prepared_path, checkpoint_path, output_path, identity,
                                       source_info["duration"], supplied_sha)
    finally:
        # A synchronously failed renderer or rejected media contract has no
        # valid output to preserve. Clear only our exact, uncompleted guard.
        if not verified_render and read_record(prepared_path) == started_record:
            prepared_path.unlink(missing_ok=True)
        # A durable prepare may still refer to the temporary artifact after a
        # failed rename/checkpoint write. Preserve that recovery evidence.
        if not prepared_path.exists() and not prepared_path.is_symlink():
            temporary_path.unlink(missing_ok=True)


__all__ = ["catalog_from_assets", "render_random_output", "render_timeout_seconds"]
