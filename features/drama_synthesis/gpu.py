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
import sys
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
RENDER_PLANNING_MINIMUM_SPEED = 0.10
RENDER_PLANNING_MARGIN_RATIO = 1.25
RENDER_FINALIZE_GRACE_SECONDS = 1800
DEFAULT_RENDER_STALL_SECONDS = 1800


def _timeout_configuration_error():
    return DramaSynthesisError("drama_random_timeout_configuration_invalid", "随机模板制作时限配置无效", 503)


def render_timeout_seconds(value=None):
    """The renderer's deadline is independent of HTTP and result polling limits."""
    raw = os.environ.get("DRAMA_GPU_RENDER_TIMEOUT", str(DEFAULT_RENDER_TIMEOUT_SECONDS)) if value is None else value
    if (isinstance(raw, bool) or not re.fullmatch(r"[0-9]{1,6}", str(raw)) or
            not 60 <= int(raw) <= MAX_RENDER_TIMEOUT_SECONDS):
        raise _timeout_configuration_error()
    return int(raw)


def _render_stall_seconds(value=None):
    raw = os.environ.get("DRAMA_GPU_RENDER_STALL_SECONDS", str(DEFAULT_RENDER_STALL_SECONDS)) if value is None else value
    if (isinstance(raw, bool) or not re.fullmatch(r"[0-9]{1,5}", str(raw)) or
            not 900 <= int(raw) <= 7200):
        raise _timeout_configuration_error()
    return int(raw)


def _calculated_render_budget(duration_seconds, configured_timeout):
    if (isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)) or
            not math.isfinite(float(duration_seconds)) or float(duration_seconds) <= 0):
        raise _timeout_configuration_error()
    calculated = math.ceil(
        float(duration_seconds) / RENDER_PLANNING_MINIMUM_SPEED * RENDER_PLANNING_MARGIN_RATIO
        + RENDER_FINALIZE_GRACE_SECONDS
    )
    return min(MAX_RENDER_TIMEOUT_SECONDS, max(
        DEFAULT_RENDER_TIMEOUT_SECONDS, int(math.ceil(configured_timeout)), calculated,
    ))


def render_budget_seconds(duration_seconds, configured_timeout=None):
    """Return the bounded absolute render budget for a probed media duration."""
    configured_timeout = render_timeout_seconds(configured_timeout)
    return _calculated_render_budget(duration_seconds, configured_timeout)


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


_STDERR_TAGS = {
    "decoder_error": ("error while decoding", "decode error", "invalid data found"),
    "device_error": ("no cuda-capable device", "device not found", "device is busy"),
    "disk_full": ("no space left on device", "disk quota exceeded"),
    "encoder_error": ("error initializing output stream", "cannot init encoder", "encoder setup failed"),
    "filter_error": ("error reinitializing filters", "failed to inject frame", "filtering failed"),
    "gpu_memory_exhausted": ("cuda_error_out_of_memory", "cuvid out of memory"),
    "memory_exhausted": ("cannot allocate memory", "out of memory"),
    "permission_denied": ("permission denied",),
    "process_killed": ("received signal", "immediate exit requested"),
    "write_error": ("error writing trailer", "error writing output", "broken pipe"),
}


def _new_stderr_evidence():
    return {
        "bytes": 0, "digest": hashlib.sha256(), "tags": set(), "carry": "",
        "complete": True, "encoding_transformed": False, "lock": threading.Lock(),
    }


def _observe_stderr(evidence, value):
    if value is None:
        return
    transformed = not isinstance(value, bytes)
    raw = value if isinstance(value, bytes) else str(value).encode("utf-8", errors="replace")
    with evidence["lock"]:
        evidence["encoding_transformed"] = evidence["encoding_transformed"] or transformed
        evidence["bytes"] += len(raw)
        evidence["digest"].update(raw)
        # Detection is deliberately allowlist-only. No stderr text, command,
        # path, URL or credential-like value survives this bounded chunk.
        lowered = (evidence["carry"] + raw.decode("utf-8", errors="replace").lower())[-8192:]
        for tag, needles in _STDERR_TAGS.items():
            if any(needle in lowered for needle in needles):
                evidence["tags"].add(tag)
        evidence["carry"] = lowered[-256:]


def _mark_stderr_incomplete(evidence):
    with evidence["lock"]:
        evidence["complete"] = False


def _stderr_evidence_record(evidence):
    with evidence["lock"]:
        return {
            "bytes": min(int(evidence["bytes"]), 2 ** 63 - 1),
            "sha256": evidence["digest"].hexdigest(),
            # The complete stream was observed incrementally, while no raw
            # bytes were retained. `truncated` describes observation only.
            "truncated": not evidence["complete"],
            "raw_stored": False,
            "encoding_transformed": bool(evidence["encoding_transformed"]),
            "tags": sorted(evidence["tags"]),
        }


def _bounded_progress_record(metrics):
    result = {}
    for key in ("out_time_seconds", "duration_seconds", "fps", "speed", "frame"):
        value = metrics.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if not math.isfinite(number) or number < 0:
            continue
        if key == "frame":
            result[key] = min(int(number), 10 ** 12)
        else:
            result[key] = round(min(number, 10 ** 12), 6)
    return result


def _safe_diagnostic_context(value):
    source = value if isinstance(value, Mapping) else {}
    result = {}
    for key in (
        "source_sha256", "recipe_sha256", "asset_set_sha256",
        "composition_sha256", "kernel_sha256",
    ):
        candidate = str(source.get(key) or "")
        if re.fullmatch(r"[0-9a-f]{64}", candidate):
            result[key] = candidate
    size = source.get("source_size_bytes")
    if type(size) is int and 0 < size <= 2 ** 63 - 1:
        result["source_size_bytes"] = size
    return result


def _render_failure_diagnostic_path(output_path):
    try:
        from .async_runtime import capture_context
        execution = capture_context()
    except Exception:
        raise checkpoint_error() from None
    if execution is not None:
        job_id = str(getattr(execution, "job_id", ""))
        generation = getattr(execution, "generation", None)
        runtime_root = getattr(getattr(execution, "runtime", None), "root", None)
        try:
            runtime_root = Path(runtime_root)
        except (TypeError, ValueError, OSError):
            raise checkpoint_error() from None
        if (not runtime_root.is_absolute() or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", job_id) or
                type(generation) is not int or not 1 <= generation <= 2 ** 31 - 1):
            raise checkpoint_error()
        return (runtime_root / "diagnostics" / job_id /
                ("generation-%08d.random-render.json" % generation))
    output_path = Path(output_path)
    return output_path.with_name("." + output_path.name + ".render.failure.json")


def _failure_process_record(returncode):
    if type(returncode) is not int or not -4096 <= returncode <= 4096:
        return {"returncode": None, "signal": None}
    return {"returncode": returncode, "signal": -returncode if returncode < 0 else None}


def _write_render_failure_diagnostic(path, *, reason, public_code, duration_seconds, elapsed_seconds,
                                     configured_floor, planned_timeout, final_deadline_offset,
                                     global_cap, stall_timeout, last_progress, returncode,
                                     stderr_evidence, context=None, progress_complete=True):
    if path is None:
        return
    safe_context = _safe_diagnostic_context(context)
    try:
        from .async_runtime import capture_context
        execution = capture_context()
    except Exception:
        raise checkpoint_error() from None
    if execution is not None:
        job_id = str(getattr(execution, "job_id", ""))
        generation = getattr(execution, "generation", None)
        required_context = {
            "source_sha256", "source_size_bytes", "recipe_sha256", "asset_set_sha256",
        }
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", job_id) or
                type(generation) is not int or not 1 <= generation <= 2 ** 31 - 1 or
                not required_context.issubset(safe_context)):
            raise checkpoint_error()
        safe_context.update(job_id=job_id, generation=generation)
    try:
        atomic_write_record(Path(path), {
            "version": 1,
            "kind": "drama_random_render_failure",
            "failed_at_epoch": int(time.time()),
            "failed_stage": "rendering_random",
            "reason": reason if reason in {
                "render_timeout", "progress_stall", "process_exit",
                "process_launch_failed", "render_runner_failed", "cleanup_unverified",
                "post_render_probe_failed", "duration_mismatch", "output_contract_invalid",
                "fingerprint_failed", "checkpoint_unverified",
            } else "render_runner_failed",
            "public_code": public_code if public_code in {
                "drama_random_render_timeout", "drama_random_render_failed",
                "drama_random_probe_failed", "drama_random_duration_mismatch",
                "drama_random_output_contract_invalid", "drama_media_checkpoint_unverified",
                "drama_media_checkpoint_conflict",
            } else "drama_random_render_failed",
            "duration_seconds": round(float(duration_seconds), 6),
            "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 6),
            "configured_floor_seconds": int(math.ceil(configured_floor)),
            "planned_timeout_seconds": int(math.ceil(planned_timeout)),
            "final_deadline_offset_seconds": int(math.ceil(final_deadline_offset)),
            "global_cap_seconds": int(math.ceil(global_cap)),
            "stall_timeout_seconds": int(math.ceil(stall_timeout)),
            "last_progress": _bounded_progress_record(last_progress),
            "progress_stream_complete": bool(progress_complete),
            "process": _failure_process_record(returncode),
            "stderr": _stderr_evidence_record(stderr_evidence),
            "context": safe_context,
        })
        # atomic_write_record uses mkstemp, so each replaced inode is private
        # (0600) even if an older sidecar had unsafe permissions.
    except Exception:
        # An unrecorded failure is recovery-required, not an ordinary timeout
        # that is safe to clean up and potentially submit again.
        raise checkpoint_error() from None


def run_render_with_progress(command, *, timeout, duration_seconds, absolute_timeout=None,
                             configured_timeout=None, diagnostic_path=None, diagnostic_context=None, stall_timeout=None,
                             popen=None, progress_callback=None, monotonic=None,
                             progress_offset_seconds=0.0, progress_total_seconds=None,
                             progress_frame_offset=0):
    """Track one child with monotonic extension, stall fencing and safe evidence."""
    from .async_runtime import clear_process, emit_progress, process_launch, record_process

    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
            not 0 < timeout <= MAX_RENDER_TIMEOUT_SECONDS):
        raise DramaSynthesisError("drama_random_timeout_configuration_invalid", "随机模板制作时限配置无效", 503)
    if absolute_timeout is None:
        absolute_timeout = MAX_RENDER_TIMEOUT_SECONDS
    if (isinstance(absolute_timeout, bool) or not isinstance(absolute_timeout, (int, float)) or
            not timeout <= absolute_timeout <= MAX_RENDER_TIMEOUT_SECONDS):
        raise _timeout_configuration_error()
    if configured_timeout is None:
        configured_timeout = timeout
    if (isinstance(configured_timeout, bool) or not isinstance(configured_timeout, (int, float)) or
            not 0 < configured_timeout <= timeout):
        raise _timeout_configuration_error()
    stall_timeout = _render_stall_seconds(stall_timeout)
    if (isinstance(progress_offset_seconds, bool) or
            not isinstance(progress_offset_seconds, (int, float)) or
            not math.isfinite(float(progress_offset_seconds)) or float(progress_offset_seconds) < 0):
        raise _timeout_configuration_error()
    if (isinstance(progress_frame_offset, bool) or not isinstance(progress_frame_offset, int) or
            progress_frame_offset < 0):
        raise _timeout_configuration_error()
    if progress_total_seconds is not None:
        if (isinstance(progress_total_seconds, bool) or
                not isinstance(progress_total_seconds, (int, float)) or
                not math.isfinite(float(progress_total_seconds)) or
                float(progress_total_seconds) + 0.000001 <
                float(progress_offset_seconds) + float(duration_seconds)):
            raise _timeout_configuration_error()
    monotonic = monotonic or time.monotonic
    popen = popen or subprocess.Popen
    tracked_command = [command[0], "-progress", "pipe:1", "-nostats", *command[1:]]
    updates = queue.Queue(maxsize=8)
    proc = None
    reader = None
    error_reader = None
    stderr_evidence = _new_stderr_evidence()
    failure_reason = None
    failure_public_code = "drama_random_render_failed"
    raised = None
    final_returncode = None
    progress_complete = True
    last_progress = {"duration_seconds": float(duration_seconds)}
    started_at = None
    last_advance_at = None
    maximum_out_time = 0.0
    maximum_frame = -1
    pending_deadline_plan = False
    deadline = float(timeout)
    absolute_deadline = float(absolute_timeout)
    final_deadline_offset = float(timeout)

    def emit(metrics):
        displayed = dict(metrics)
        if progress_total_seconds is not None:
            displayed["duration_seconds"] = float(progress_total_seconds)
            if "out_time_seconds" in displayed:
                displayed["out_time_seconds"] = min(
                    float(progress_total_seconds),
                    float(progress_offset_seconds) + float(displayed["out_time_seconds"]),
                )
            if "frame" in displayed:
                displayed["frame"] = progress_frame_offset + int(displayed["frame"])
        emit_progress("rendering_random", **displayed)
        if progress_callback:
            progress_callback(dict(displayed))

    def fold_progress(first, latest):
        folded = {"duration_seconds": float(duration_seconds)}
        for key in ("out_time_seconds", "frame"):
            available = [value for value in (first.get(key), latest.get(key)) if value is not None]
            if available:
                folded[key] = max(available)
        for key in ("fps", "speed"):
            if key in latest:
                folded[key] = latest[key]
            elif key in first:
                folded[key] = first[key]
        return folded

    def read_progress():
        nonlocal progress_complete
        values = {}
        try:
            for raw_line in proc.stdout:
                line = (raw_line.decode("utf-8", errors="replace")
                        if isinstance(raw_line, bytes) else str(raw_line))
                key, separator, value = line.strip().partition("=")
                if separator and key in {"out_time_us", "out_time_ms", "out_time", "frame", "fps", "speed", "progress"}:
                    values[key] = value[:128]
                if key == "progress":
                    packet = ffmpeg_progress_metrics(values, duration_seconds)
                    if updates.full():
                        try:
                            packet = fold_progress(updates.get_nowait(), packet)
                        except queue.Empty:
                            pass
                    try:
                        updates.put_nowait(packet)
                    except queue.Full:
                        pass
                    values = {}
        except Exception:
            progress_complete = False
            return

    def read_errors():
        stream = getattr(proc, "stderr", None)
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                _observe_stderr(stderr_evidence, chunk)
        except Exception:
            _mark_stderr_incomplete(stderr_evidence)
            return

    def extend_deadline(now):
        nonlocal deadline, final_deadline_offset, pending_deadline_plan
        if not pending_deadline_plan or started_at is None:
            return
        # Deadline planning belongs to the progress batch that advanced native
        # out_time.  Consume the signal before the eligibility checks so an
        # early sample cannot be reused by a later frame-only or empty batch.
        pending_deadline_plan = False
        elapsed = now - started_at
        if elapsed < 300 or maximum_out_time <= 0:
            return
        observed_speed = maximum_out_time / elapsed
        planning_speed = max(0.01, min(RENDER_PLANNING_MINIMUM_SPEED, observed_speed))
        remaining_media = max(0.0, float(duration_seconds) - maximum_out_time)
        candidate = (
            now + remaining_media / planning_speed * RENDER_PLANNING_MARGIN_RATIO
            + RENDER_FINALIZE_GRACE_SECONDS
        )
        # A valid progress packet may buy more time, but it can never retract
        # an already granted deadline or exceed the global 24 hour cap.
        deadline = max(deadline, min(absolute_deadline, candidate))
        final_deadline_offset = deadline - started_at

    def report_updates(now):
        nonlocal last_advance_at, maximum_out_time, maximum_frame, last_progress, pending_deadline_plan
        folded_out_time = maximum_out_time
        folded_frame = maximum_frame
        latest_fps = last_progress.get("fps")
        latest_speed = last_progress.get("speed")
        received = False
        while True:
            try:
                packet = updates.get_nowait()
            except queue.Empty:
                break
            received = True
            folded_out_time = max(folded_out_time, float(packet.get("out_time_seconds", 0)))
            folded_frame = max(folded_frame, int(packet.get("frame", -1)))
            if "fps" in packet:
                latest_fps = packet["fps"]
            if "speed" in packet:
                latest_speed = packet["speed"]
        if received:
            metrics = {"duration_seconds": float(duration_seconds)}
            if folded_out_time >= 0:
                metrics["out_time_seconds"] = folded_out_time
            if folded_frame >= 0:
                metrics["frame"] = folded_frame
            if latest_fps is not None:
                metrics["fps"] = latest_fps
            if latest_speed is not None:
                metrics["speed"] = latest_speed
            out_time_advanced = folded_out_time > maximum_out_time
            frame_advanced = folded_frame > maximum_frame
            advanced = out_time_advanced or frame_advanced
            maximum_out_time = folded_out_time
            maximum_frame = folded_frame
            last_progress = dict(metrics)
            emit(metrics)
            if advanced:
                last_advance_at = now
            if out_time_advanced:
                pending_deadline_plan = True
                extend_deadline(now)

    try:
        try:
            with process_launch():
                proc = popen(tracked_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=os.name == "posix")
                record_process(proc.pid)
            reader = threading.Thread(target=read_progress, daemon=True)
            error_reader = threading.Thread(target=read_errors, daemon=True)
            reader.start()
            error_reader.start()
            started_at = monotonic()
            last_advance_at = started_at
            deadline = started_at + timeout
            absolute_deadline = started_at + absolute_timeout
            final_deadline_offset = float(timeout)
            emit({"out_time_seconds": 0, "duration_seconds": duration_seconds})
            while True:
                now = monotonic()
                report_updates(now)
                extend_deadline(now)
                # A child that already exited wins over a coincident stall or
                # deadline boundary; classify its real return code first.
                try:
                    code = proc.poll()
                except Exception:
                    failure_reason = "cleanup_unverified"
                    failure_public_code = "drama_media_checkpoint_unverified"
                    raise checkpoint_error() from None
                if code is not None:
                    break
                # poll() may yield the GIL long enough for a pipe reader to
                # publish progress. Timeout/stall decisions use a fresh clock
                # sample and this second drain, which is the contract boundary.
                now = monotonic()
                report_updates(now)
                extend_deadline(now)
                if now - last_advance_at >= stall_timeout:
                    failure_reason = "progress_stall"
                    failure_public_code = "drama_random_render_timeout"
                    raise TimeoutError("random render progress stalled")
                remaining = deadline - now
                if remaining <= 0:
                    failure_reason = "render_timeout"
                    failure_public_code = "drama_random_render_timeout"
                    raise TimeoutError("random render deadline exceeded")
                try:
                    code = proc.wait(timeout=min(1, remaining, stall_timeout - (now - last_advance_at)))
                    break
                except subprocess.TimeoutExpired:
                    continue
            reader.join(timeout=5)
            report_updates(monotonic())
            if code != 0:
                failure_reason = "process_exit"
                raise RuntimeError("random render failed")
        except Exception as exc:
            raised = exc
            if failure_reason is None:
                failure_reason = "process_launch_failed" if proc is None else "render_runner_failed"
            raise
        finally:
            if proc is not None:
                cleanup_unverified = False
                try:
                    final_returncode = proc.poll()
                except Exception:
                    cleanup_unverified = True
                if final_returncode is None and not cleanup_unverified:
                    try:
                        proc.kill()
                    except Exception:
                        cleanup_unverified = True
                    if not cleanup_unverified:
                        try:
                            proc.wait(timeout=30)
                        except (OSError, subprocess.TimeoutExpired):
                            cleanup_unverified = True
                    try:
                        final_returncode = proc.poll()
                    except Exception:
                        cleanup_unverified = True
                if final_returncode is None:
                    cleanup_unverified = True
                if final_returncode is not None:
                    # Never close a pipe while its reader may own the buffered
                    # I/O lock. A descendant can inherit the write end after the
                    # direct child exits, so join and close must both be bounded.
                    streams = (
                        ("progress", reader, proc.stdout),
                        ("stderr", error_reader, getattr(proc, "stderr", None)),
                    )
                    for kind, thread, stream in streams:
                        join_failed = False
                        alive = False
                        if thread is not None:
                            try:
                                thread.join(timeout=5)
                            except Exception:
                                join_failed = True
                            probe_alive = getattr(thread, "is_alive", None)
                            if callable(probe_alive):
                                try:
                                    alive = bool(probe_alive())
                                except Exception:
                                    alive = True
                                    join_failed = True
                            if alive:
                                try:
                                    thread.join(timeout=2)
                                except Exception:
                                    join_failed = True
                                try:
                                    alive = bool(probe_alive())
                                except Exception:
                                    alive = True
                                    join_failed = True
                        if join_failed or alive:
                            cleanup_unverified = True
                            if kind == "progress":
                                progress_complete = False
                            else:
                                _mark_stderr_incomplete(stderr_evidence)
                        if stream is not None and not alive:
                            try:
                                stream.close()
                            except Exception:
                                cleanup_unverified = True
                else:
                    _mark_stderr_incomplete(stderr_evidence)
                if not cleanup_unverified:
                    try:
                        clear_process(proc.pid)
                    except Exception:
                        cleanup_unverified = True
                if cleanup_unverified:
                    # Keep the process identity, start guard and partial. A
                    # later recovery pass must prove the child is gone first.
                    recovery = checkpoint_error()
                    raised = recovery
                    failure_reason = "cleanup_unverified"
                    failure_public_code = recovery.code
                    raise recovery
    finally:
        if raised is not None and diagnostic_path is not None:
            try:
                finished_at = monotonic()
            except Exception:
                finished_at = started_at
            _write_render_failure_diagnostic(
                diagnostic_path,
                reason=failure_reason,
                public_code=failure_public_code,
                duration_seconds=duration_seconds,
                elapsed_seconds=0 if started_at is None or finished_at is None else finished_at - started_at,
                configured_floor=configured_timeout,
                planned_timeout=timeout,
                final_deadline_offset=final_deadline_offset,
                global_cap=absolute_timeout,
                stall_timeout=stall_timeout,
                last_progress=last_progress,
                returncode=final_returncode,
                stderr_evidence=stderr_evidence,
                context=diagnostic_context,
                progress_complete=progress_complete,
            )


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
    artifact_committed = output_path.exists()
    result_committed = completed_result is not None
    try:
        if not output_path.exists():
            os.replace(temporary_path, output_path)
        artifact_committed = True
        if completed_result is None:
            save_completed(checkpoint_path, output_path, identity, result, fingerprint=artifact)
        result_committed = True
        # save_completed fsyncs the directory on POSIX, committing both the
        # artifact rename and the completed marker before prepare is removed.
        temporary_path.unlink(missing_ok=True)
        prepared_path.unlink()
    except OSError:
        if artifact_committed and result_committed:
            # Cleanup debris cannot reverse an already durable result. Replay
            # revalidates the artifact and marker before retrying cleanup.
            return result
        raise checkpoint_error() from None
    return result


def _render_random_output_legacy(
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
    configured_timeout = render_timeout_seconds(timeout)
    render_timeout = render_budget_seconds(source_info["duration"], configured_timeout)
    stall_timeout = _render_stall_seconds()
    diagnostic_path = _render_failure_diagnostic_path(output_path)
    diagnostic_context = {
        "source_sha256": identity["source"]["sha256"],
        "source_size_bytes": identity["source"]["size_bytes"],
        "recipe_sha256": supplied_sha,
        "asset_set_sha256": identity["asset_set_sha256"],
    }
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".random-render-", suffix=".mp4", dir=str(output_path.parent))
    os.close(fd)
    temporary_path = Path(temporary)
    config = SimpleNamespace(ffmpeg=ffmpeg)
    started_record = {"version": 1, "identity": identity, "artifact": None,
                      "result": None, "temporary_name": temporary_path.name}
    verified_render = False
    render_committed = False
    preserve_failure_evidence = False
    render_started_at = None

    def record_post_render_failure(exc, reason):
        nonlocal preserve_failure_evidence
        code = getattr(exc, "code", "drama_random_render_failed")
        checkpoint_failure = code in {
            "drama_media_checkpoint_unverified", "drama_media_checkpoint_conflict",
        }
        if checkpoint_failure:
            preserve_failure_evidence = True
        try:
            _write_render_failure_diagnostic(
                diagnostic_path,
                reason=reason,
                public_code=code,
                duration_seconds=source_info["duration"],
                elapsed_seconds=(0 if render_started_at is None else
                                 max(0.0, time.monotonic() - render_started_at)),
                configured_floor=configured_timeout,
                planned_timeout=render_timeout,
                final_deadline_offset=render_timeout,
                global_cap=MAX_RENDER_TIMEOUT_SECONDS,
                stall_timeout=stall_timeout,
                last_progress={"duration_seconds": source_info["duration"]},
                returncode=0,
                stderr_evidence=_new_stderr_evidence(),
                context=diagnostic_context,
                progress_complete=False,
            )
        except DramaSynthesisError:
            preserve_failure_evidence = True
            raise

    try:
        # A start guard is intentionally not a completed render. If the process
        # crashes before its validated prepare is durable, replay fails closed
        # and retains the named artifact instead of silently rendering again.
        try:
            atomic_write_record(prepared_path, started_record)
        except OSError:
            raise checkpoint_error() from None
        command = build_drama_random_command(config, source_path, temporary_path, source_info, fb_recipe, selected_asset_paths(fb_recipe, assets))
        render_started_at = time.monotonic()
        try:
            if runner is None:
                run_render_with_progress(
                    command,
                    timeout=render_timeout,
                    absolute_timeout=MAX_RENDER_TIMEOUT_SECONDS,
                    configured_timeout=configured_timeout,
                    duration_seconds=source_info["duration"],
                    stall_timeout=stall_timeout,
                    diagnostic_path=diagnostic_path,
                    diagnostic_context=diagnostic_context,
                )
            else:
                runner_started = time.monotonic()
                try:
                    runner(command, check=True, capture_output=True, text=True, timeout=render_timeout)
                except Exception as exc:
                    evidence = _new_stderr_evidence()
                    _observe_stderr(evidence, getattr(exc, "stderr", None))
                    is_timeout = isinstance(exc, (TimeoutError, subprocess.TimeoutExpired))
                    returncode = getattr(exc, "returncode", None)
                    _write_render_failure_diagnostic(
                        diagnostic_path,
                        reason=("render_timeout" if is_timeout else
                                "process_exit" if type(returncode) is int else "render_runner_failed"),
                        public_code=("drama_random_render_timeout" if is_timeout else
                                     "drama_random_render_failed"),
                        duration_seconds=source_info["duration"],
                        elapsed_seconds=time.monotonic() - runner_started,
                        configured_floor=configured_timeout,
                        planned_timeout=render_timeout,
                        final_deadline_offset=render_timeout,
                        global_cap=MAX_RENDER_TIMEOUT_SECONDS,
                        stall_timeout=stall_timeout,
                        last_progress={"duration_seconds": source_info["duration"]},
                        returncode=returncode,
                        stderr_evidence=evidence,
                        context=diagnostic_context,
                    )
                    raise
        except DramaSynthesisError as exc:
            if exc.code in {"drama_media_checkpoint_unverified", "drama_media_checkpoint_conflict"}:
                preserve_failure_evidence = True
            raise
        except (TimeoutError, subprocess.TimeoutExpired):
            raise DramaSynthesisError("drama_random_render_timeout", "随机模板视频制作超时", 504) from None
        except Exception:
            raise DramaSynthesisError("drama_random_render_failed", "随机模板视频制作失败", 502) from None
        try:
            post_stage = "post_render_probe_failed"
            result_info = _probe(ffprobe, temporary_path)
            video = result_info["video"]
            post_stage = "duration_mismatch"
            if not random_output_duration_matches(source_info["duration"], result_info["duration"], video.get("duration")):
                raise DramaSynthesisError("drama_random_duration_mismatch", "随机模板成片时长与源视频不一致，已阻止上传", 502)
            post_stage = "output_contract_invalid"
            if (
                video.get("codec_name") != "h264"
                or str(video.get("profile") or "").lower() != "high"
                or int(video.get("width") or 0) != 720
                or int(video.get("height") or 0) != 1280
            ):
                raise DramaSynthesisError("drama_random_output_contract_invalid", "随机模板成片规格不符合要求", 502)
            post_stage = "fingerprint_failed"
            fingerprint = file_fingerprint(temporary_path)
            result = {
                "output_sha256": fingerprint["sha256"], "output_size": fingerprint["size_bytes"],
                "duration_seconds": result_info["duration"], "profile": RECIPE_PROFILE,
                "recipe_sha256": supplied_sha,
            }
            verified_render = True
            post_stage = "checkpoint_unverified"
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
            committed = _commit_prepared_render(
                prepared_path, checkpoint_path, output_path, identity,
                source_info["duration"], supplied_sha,
            )
            if committed is None:
                raise checkpoint_error()
            render_committed = True
            return committed
        except DramaSynthesisError as exc:
            record_post_render_failure(exc, post_stage)
            raise
        except Exception as exc:
            record_post_render_failure(exc, post_stage)
            raise DramaSynthesisError("drama_random_render_failed", "随机模板视频制作失败", 502) from None
    finally:
        active_error = sys.exc_info()[1]
        cleanup_unverified = False
        # Never let best-effort debris cleanup reverse a durable commit or
        # obscure an already structured render failure. Verified recovery
        # artifacts and explicit recovery-required failures are retained.
        if not render_committed and not verified_render and not preserve_failure_evidence:
            try:
                current_guard = read_record(prepared_path)
            except Exception:
                current_guard = None
                cleanup_unverified = True
            if not cleanup_unverified and current_guard == started_record:
                try:
                    # Remove unverified media first. If that fails, the exact
                    # start guard remains and blocks any later re-render.
                    temporary_path.unlink(missing_ok=True)
                except Exception:
                    cleanup_unverified = True
                if not cleanup_unverified:
                    try:
                        prepared_path.unlink(missing_ok=True)
                    except Exception:
                        cleanup_unverified = True
            elif not cleanup_unverified and current_guard is None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except Exception:
                    cleanup_unverified = True
        if cleanup_unverified and active_error is None and not render_committed:
            raise checkpoint_error() from None


def compositor_backend(value=None):
    raw = os.environ.get("DRAMA_GPU_COMPOSITOR_BACKEND", "legacy_cpu") if value is None else value
    backend = str(raw or "").strip().lower()
    if backend not in {"legacy_cpu", "opencl_fused_v2"}:
        raise DramaSynthesisError("drama_gpu_compositor_unavailable", "GPU合成引擎不可用", 503)
    return backend


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
    backend = compositor_backend()
    if backend == "opencl_fused_v2":
        from .gpu_compositor import render_chunked_random_output
        return render_chunked_random_output(
            source=source,
            output=output,
            recipe=recipe,
            asset_root=asset_root,
            manifest_sha256=manifest_sha256,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            timeout=timeout,
            runner=runner,
        )
    return _render_random_output_legacy(
        source=source,
        output=output,
        recipe=recipe,
        asset_root=asset_root,
        manifest_sha256=manifest_sha256,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        timeout=timeout,
        runner=runner,
    )


__all__ = [
    "catalog_from_assets", "compositor_backend", "render_budget_seconds", "render_random_output",
    "render_timeout_seconds",
]
