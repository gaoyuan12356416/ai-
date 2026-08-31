"""Durable, media-only GPU execution and safe reattachment.

The private JSON ledger lives outside removable job directories.  HTTP callers
only see an allowlisted projection; source URLs and process identities remain
private.  A lost HTTP response never creates another execution.  OS locks fence
worker processes and launch markers fence a crash between Popen and PID capture.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import unicodedata
from urllib.parse import urlsplit
import uuid

from .core import DramaSynthesisError
from . import gpu_cache
from .local_checkpoint import durable_ensure_directory


JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
STAGES = frozenset({
    "queued", "starting", "rendering", "downloading", "normalizing",
    "waiting_cover", "rendering_intro", "concatenating", "removing_bgm",
    "rendering_random", "uploading", "verifying", "completed", "failed",
    "recovery_required",
})
METRICS = frozenset({
    "bytes_done", "bytes_total", "episodes_done", "episodes_total",
    "download_bytes", "total_bytes", "download_bps", "completed_episodes",
    "total_episodes", "uploaded_bytes", "upload_bytes_total", "upload_bps",
    "out_time_seconds", "duration_seconds", "fps", "speed", "frame",
    "completed_steps", "total_steps", "percent", "download_workers",
    "downloaded_bytes", "bytes_per_second", "normalized_episodes", "total_segments",
})
ADVANCEMENT_METRICS = METRICS - {
    "bytes_total", "episodes_total", "total_bytes", "total_episodes",
    "upload_bytes_total", "duration_seconds", "total_steps", "fps", "speed",
    "download_bps", "upload_bps", "download_workers",
    "bytes_per_second", "total_segments",
}
TERMINAL = frozenset({"completed", "failed"})
ERROR_MESSAGES = {
    "gpu_job_input_conflict": "任务制作参数与已保存的执行不一致，已停止重复制作",
    "gpu_job_not_found": "未找到制作记录",
    "gpu_queue_full": "制作等待队列已满，请稍后重试",
    "gpu_render_busy": "制作节点忙，请稍后重试",
    "gpu_job_running": "该任务已在制作中，请继续查询原任务",
    "gpu_runtime_unavailable": "制作节点正在恢复，请稍后查询",
    "gpu_runtime_unverified": "制作执行记录无法校验，已停止启动新制作",
    "gpu_result_cache_unverified": "已有成片暂时无法校验，已停止重制，请稍后重试",
    "gpu_previous_process_running": "原制作进程仍在运行，已停止重复制作",
    "gpu_process_state_unknown": "原制作进程状态尚不能确认，需核查后恢复",
    "gpu_job_resume_unavailable": "当前任务没有可安全恢复的检查点，请人工核查",
    "gpu_generation_conflict": "恢复请求的执行代次已过期，请重新查询任务",
    "gpu_render_failed": "制作失败，已保留现有素材和检查点",
    "invalid_job_id": "制作任务编号无效",
    "invalid_content_id": "剧集编号无效",
    "invalid_request": "制作参数无效",
    "drama_recipe_missing": "随机模板配方不存在",
    "drama_recipe_hash_invalid": "随机模板配方指纹无效",
    "drama_recipe_conflict": "该任务已有不同的随机模板成片，已停止重制",
    "drama_random_template_source_invalid": "随机模板源视频无效",
    "drama_random_assets_unavailable": "随机模板素材暂不可用",
    "drama_episode_source_changed": "视频源版本发生变化，已停止续传",
    "drama_episode_download_invalid": "视频下载完整性校验失败",
    "drama_episode_download_cancelled": "制作已停止，下载检查点已保留",
    "drama_episode_download_failed": "视频下载失败，已保留可校验的下载进度",
    "drama_episode_download_route_invalid": "视频下载线路配置与冻结任务不一致",
    "drama_download_configuration_invalid": "下载并发配置无效",
    "drama_concat_normalization_invalid": "转码后的剧集片段仍不兼容，已停止拼接",
    "drama_media_checkpoint_unverified": "本地制作记录暂时无法校验，已停止重制",
    "drama_media_checkpoint_conflict": "本地制作记录与当前任务不一致，已停止重制",
    "drama_random_probe_failed": "随机模板视频校验失败",
    "drama_random_graph_contract_invalid": "随机模板处理配置不兼容",
    "drama_random_thread_configuration_invalid": "随机模板线程配置无效",
    "drama_random_timeout_configuration_invalid": "随机模板制作时限配置无效",
    "drama_random_source_missing": "随机模板源视频不存在",
    "drama_recipe_profile_mismatch": "随机模板配方版本不一致",
    "drama_recipe_asset_mismatch": "随机模板配方与制作节点素材不一致",
    "drama_random_render_failed": "随机模板视频制作失败",
    "drama_random_render_timeout": "随机模板渲染超过制作时限，已保留素材，请核查执行记录",
    "drama_random_duration_mismatch": "随机模板成片时长与源视频不一致，已阻止上传",
    "drama_random_output_contract_invalid": "随机模板成片规格不符合要求",
    "drama_upload_checkpoint_unverified": "上传记录暂时无法校验，已保留成片和分片",
    "drama_upload_checkpoint_conflict": "上传记录与当前成片或目标不一致，已停止上传",
    "drama_upload_source_changed": "待上传成片发生变化，已停止上传并保留分片",
    "drama_upload_recovery_required": "上传结果尚不能确认，已保留成片和分片，请核查后恢复",
    "drama_upload_failed": "上传连接异常，已保留成片和已传分片，可恢复续传",
    "drama_upload_object_conflict": "目标位置已有无法确认归属的文件，已停止覆盖",
    "drama_upload_busy": "该成片正在上传，请继续查询原任务",
    "drama_upload_configuration_invalid": "上传参数无效",
    "drama_upload_bucket_state_unverified": "上传目标的版本控制状态未通过安全校验，已暂停上传",
}

# These failures mean that durable media or upload state exists but cannot be
# proved safe to reuse yet.  They must never be presented as an ordinary render
# failure because an operator retry could otherwise suggest starting over.
RECOVERY_BLOCKING_CODES = {
    "gpu_result_cache_unverified",
    "drama_media_checkpoint_unverified",
    "drama_media_checkpoint_conflict",
    "drama_upload_checkpoint_unverified",
    "drama_upload_checkpoint_conflict",
    "drama_upload_source_changed",
    "drama_upload_recovery_required",
    "drama_upload_failed",
    "drama_upload_object_conflict",
    "drama_upload_busy",
    "drama_upload_bucket_state_unverified",
}


def runtime_error(code, status=503):
    return DramaSynthesisError(code, ERROR_MESSAGES.get(code, "制作失败，请人工核查"), status)


def safe_error(exc):
    code = getattr(exc, "code", "gpu_render_failed")
    if code not in ERROR_MESSAGES:
        code = "gpu_render_failed"
    return {"code": code, "message": ERROR_MESSAGES[code]}


def valid_job_id(value):
    return isinstance(value, str) and bool(JOB_ID.fullmatch(value))


def _integer(value, default=0):
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise runtime_error("invalid_request", 400)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise runtime_error("invalid_request", 400) from None
    if str(result) != str(value) or result < 0:
        raise runtime_error("invalid_request", 400)
    return result


def _canonical_download_route(source_url, route):
    """Freeze the explicit route without importing the media executor.

    Host/path derivation is checked again by the downloader.  Here we reject
    malformed route objects instead of silently dropping identity fields.
    """
    fields = ("version", "source_url", "primary_url", "fallback_url")
    if (not isinstance(route, dict) or set(route) != set(fields)
            or type(route.get("version")) is not int or route["version"] != 1
            or route.get("source_url") != source_url):
        raise runtime_error("invalid_request", 400)
    for name in ("source_url", "primary_url", "fallback_url"):
        value = route[name]
        if not isinstance(value, str):
            raise runtime_error("invalid_request", 400)
        if name == "fallback_url" and value == "":
            continue
        if (not value or len(value) > 16384
                or any(char.isspace() or unicodedata.category(char) in {"Cc", "Cs"} for char in value)):
            raise runtime_error("invalid_request", 400)
        try:
            parts = urlsplit(value)
            if (parts.scheme not in {"https", "http"} or not parts.hostname
                    or parts.username or parts.password or parts.fragment):
                raise ValueError()
            _ = parts.port  # Reject malformed and out-of-range explicit ports.
        except ValueError:
            raise runtime_error("invalid_request", 400) from None
    return {name: route[name] for name in fields}


def canonical_render_payload(payload):
    """One CPU/GPU identity; late cover delivery and transport hints are not identity.

    The list order is significant, just as in the existing concatenator.  The
    presence of an intro is frozen.  A waiting job keeps its original wait flag
    during reconnection, so late callback delivery does not change identity.
    An initially fixed cover URL and an explicit episode download route do.
    Validation of required episode/output fields belongs to async submit so
    the legacy sync handler retains its existing error handling.
    """
    if not isinstance(payload, dict) or not valid_job_id(payload.get("job_id")):
        raise runtime_error("invalid_job_id", 400)
    content_id = payload.get("content_id")
    if content_id is None:
        content_id = ""
    if isinstance(content_id, int) and not isinstance(content_id, bool):
        content_id = str(content_id)
    if (not isinstance(content_id, str) or len(content_id.encode("utf-8", errors="replace")) > 200
            or any(char in "/\\" or unicodedata.category(char) in {"Cc", "Cs"} for char in content_id)):
        raise runtime_error("invalid_content_id", 400)
    episodes = payload.get("episodes") or []
    outputs = payload.get("outputs") or {}
    if not isinstance(episodes, list) or not isinstance(outputs, dict):
        raise runtime_error("invalid_request", 400)
    normalized = []
    for item in episodes:
        if not isinstance(item, dict) or not isinstance(item.get("episode_url"), str):
            raise runtime_error("invalid_request", 400)
        episode = {
            "episode_number": _integer(item.get("episode_number")),
            "episode_url": item["episode_url"].strip(),
        }
        if "download_route" in item:
            episode["download_route"] = _canonical_download_route(episode["episode_url"], item["download_route"])
        normalized.append(episode)
    selected = {key: bool(outputs.get(key, False)) for key in gpu_cache.OUTPUT_FIELDS}
    cover_url = payload.get("cover_16x9_url") or payload.get("cover_url") or ""
    if not isinstance(cover_url, str):
        raise runtime_error("invalid_request", 400)
    cover_url = cover_url.strip()
    await_cover = bool(payload.get("await_cover_16x9") or payload.get("wait_for_cover"))
    result = {
        "job_id": payload["job_id"], "content_id": content_id,
        "episode_start": _integer(payload.get("episode_start")),
        "episode_end": _integer(payload.get("episode_end")),
        "episodes": normalized, "outputs": selected,
        "include_cover": bool(cover_url or await_cover),
    }
    if cover_url and not await_cover:
        result["cover_16x9_url"] = cover_url
    if selected["random_template_video"]:
        recipe = payload.get("random_template_recipe")
        if not isinstance(recipe, dict):
            raise runtime_error("drama_recipe_missing", 409)
        gpu_cache.verify_cached_recipe({"random_template_recipe_sha256": recipe.get("recipe_sha256")}, recipe)
        if recipe.get("source") not in {"concat_video", "no_bgm_video"}:
            raise runtime_error("drama_random_template_source_invalid", 409)
        result["random_template_recipe"] = deepcopy(recipe)
    return result


def render_fingerprint(payload):
    try:
        # Reject non-JSON transport extras before they can poison a durable
        # write (Python's decoder otherwise accepts NaN and Infinity).
        json.dumps(payload, allow_nan=False)
        raw = json.dumps(canonical_render_payload(payload), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, UnicodeError):
        raise runtime_error("invalid_request", 400) from None


def validate_render_payload(payload):
    canonical = canonical_render_payload(payload)
    if not isinstance(payload.get("episodes"), list) or not isinstance(payload.get("outputs"), dict):
        raise runtime_error("invalid_request", 400)
    for flag in ("await_cover_16x9", "wait_for_cover"):
        if flag in payload and type(payload[flag]) is not bool:
            raise runtime_error("invalid_request", 400)
    episodes = canonical["episodes"]
    if not episodes or len(episodes) > 1000 or not any(canonical["outputs"].values()):
        raise runtime_error("invalid_request", 400)
    numbers = [item["episode_number"] for item in episodes]
    if any(number < 1 for number in numbers) or len(set(numbers)) != len(numbers):
        raise runtime_error("invalid_request", 400)
    if any(type(payload.get("outputs", {}).get(key, False)) is not bool for key in gpu_cache.OUTPUT_FIELDS):
        raise runtime_error("invalid_request", 400)
    for item in episodes:
        try:
            url = urlsplit(item["episode_url"])
            if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password or url.fragment:
                raise ValueError()
        except ValueError:
            raise runtime_error("invalid_request", 400) from None
    return canonical


def _boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return ""


def process_identity(pid):
    """PID reuse resistant identity. Unknown platforms deliberately fail closed."""
    try:
        pid = int(pid)
        if pid <= 0:
            return None
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
            if not handle:
                return None
            try:
                values = [wintypes.FILETIME() for _ in range(4)]
                if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
                    return None
                ticks = (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
                waited = kernel.WaitForSingleObject(handle, 0)
                if waited not in (0, 0x102):
                    return None
                return {"pid": pid, "start_ticks": str(ticks), "boot_id": "windows-creation-time",
                        "state": "Z" if waited == 0 else "R"}
            finally:
                kernel.CloseHandle(handle)
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2:].split()
        boot = _boot_id()
        if not boot:
            return None
        return {"pid": pid, "start_ticks": fields[19], "boot_id": boot,
                "state": fields[0], "pgrp": int(fields[2])}
    except (OSError, ValueError, IndexError):
        return None


def process_state(identity):
    """Return stopped only with positive absence/PID-reuse/reboot evidence."""
    if not isinstance(identity, dict) or not identity.get("start_ticks") or not identity.get("boot_id"):
        return "unknown"
    try:
        pid = int(identity["pid"])
    except (KeyError, TypeError, ValueError):
        return "unknown"
    current = process_identity(pid)
    if current is not None:
        if (current["start_ticks"], current["boot_id"]) != (identity["start_ticks"], identity["boot_id"]):
            return "stopped"
        if current.get("state") not in {"Z", "X"}:
            return "alive"
    elif os.name != "nt":
        if _boot_id() and _boot_id() != identity["boot_id"]:
            return "stopped"
        if Path(f"/proc/{pid}").exists():
            return "unknown"
        if not Path("/proc/self/stat").exists():
            return "unknown"
    else:
        # OpenProcess can also fail due to permissions; absence alone is not
        # proof on Windows. Production is Linux; tests can inject a probe.
        return "unknown"
    if identity.get("pgrp") == pid and os.name != "nt":
        # A session-owning parent can exit before its ffmpeg grandchildren.
        try:
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                raw = (entry / "stat")
                try:
                    value = raw.read_text(encoding="ascii")
                except FileNotFoundError:
                    continue
                fields = value[value.rfind(")") + 2:].split()
                if int(fields[2]) == pid and fields[0] not in {"Z", "X"}:
                    return "alive"
        except (OSError, ValueError, IndexError):
            return "unknown"
    return "stopped"


class _FileLock:
    def __init__(self, path):
        self.path, self.fd = Path(path), None

    def acquire(self):
        if self.path.is_symlink():
            raise runtime_error("gpu_runtime_unverified")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)


@dataclass(frozen=True)
class _ExecutionContext:
    runtime: object
    job_id: str
    generation: int
    owner: str


_CONTEXT = ContextVar("drama_gpu_execution", default=None)
_LAUNCH = ContextVar("drama_gpu_process_launch", default=None)


def capture_context():
    return _CONTEXT.get()


@contextmanager
def use_context(context):
    token = _CONTEXT.set(context)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def emit_progress(stage, **metrics):
    context = capture_context()
    if context is not None:
        context.runtime.emit(context, stage, **metrics)


@contextmanager
def process_launch():
    """Bracket Popen then record_process; an unresolved launch blocks recovery."""
    context = capture_context()
    if context is None:
        yield
        return
    launch = uuid.uuid4().hex
    context.runtime._launch_event(context, launch, begin=True)
    token = _LAUNCH.set(launch)
    try:
        yield
    finally:
        # Only record_process can atomically replace the launch marker with
        # the actual PID identity. An exception here cannot prove no child.
        _LAUNCH.reset(token)


def record_process(pid):
    context = capture_context()
    if context is not None:
        context.runtime._process_event(context, pid, begin=True, launch=_LAUNCH.get())


def clear_process(pid):
    context = capture_context()
    if context is not None:
        context.runtime._process_event(context, pid, begin=False)


class AsyncRuntime:
    def __init__(self, root, execute, cached_result, *, sync_cached_result=None, can_resume=None,
                 fingerprint=render_fingerprint, render_slots=None, queue_limit=8,
                 clock=time.time, process_probe=process_state, autostart=True):
        if type(queue_limit) is not int or not 1 <= queue_limit <= 64:
            raise ValueError("queue_limit must be an integer in 1..64")
        self.root = Path(root).absolute() / ".runtime"
        try:
            # The first accepted job is only durable if every newly-created
            # ancestor (including .runtime/jobs) has also been fsynced into its
            # parent. Creating these directories with plain mkdir first would
            # hide that fact from the atomic record writer.
            for path in (self.root.parent, self.root, self.root / "jobs", self.root / "locks"):
                durable_ensure_directory(path)
        except Exception:
            raise runtime_error("gpu_runtime_unverified") from None
        self.execute, self.cached_result = execute, cached_result
        self.sync_cached_result = sync_cached_result or cached_result
        self.can_resume = can_resume
        self.fingerprint, self.clock, self.process_probe = fingerprint, clock, process_probe
        self.render_slots = render_slots or threading.BoundedSemaphore(1)
        self.queue_limit, self.instance = queue_limit, uuid.uuid4().hex
        self._mutex = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._accepting = True
        self._healthy = True
        self._records, self._submission_locks = {}, {}
        self._dispatcher = self._heartbeat = None
        self._owner = _FileLock(self.root / "owner.lock")
        if not self._owner.acquire():
            raise runtime_error("gpu_runtime_unavailable")
        try:
            self._load()
            self._reconcile()
        except BaseException:
            self._owner.release()
            raise
        if autostart:
            self.start()

    def _timestamp(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="milliseconds")

    def _path(self, job_id):
        if not valid_job_id(job_id):
            raise runtime_error("invalid_job_id", 400)
        return self.root / "jobs" / (job_id + ".json")

    def _load(self):
        for path in (self.root / "jobs").glob("*.json"):
            try:
                if path.is_symlink() or not valid_job_id(path.stem) or path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError()
                record = json.loads(path.read_text(encoding="utf-8"))
                if (record.get("version") != 1 or record.get("job_id") != path.stem
                        or record.get("status") not in TERMINAL | {"queued", "running", "recovery_required"}
                        or type(record.get("generation")) is not int or record["generation"] < 1
                        or self.fingerprint(record["_payload"]) != record["fingerprint"]):
                    raise ValueError()
                self._records[path.stem] = record
            except Exception:
                raise runtime_error("gpu_runtime_unverified") from None

    def _save(self, record):
        try:
            self._write_record(record)
        except Exception:
            # A disk/write failure must never leave an accepting in-memory-only
            # worker. Recovery requires rereading its durable launch markers.
            self._healthy = self._accepting = False
            raise runtime_error("gpu_runtime_unverified") from None

    def _write_record(self, record):
        path = self._path(record["job_id"])
        if path.is_symlink():
            raise runtime_error("gpu_runtime_unverified")
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()
        self._records[record["job_id"]] = record

    def _dto(self, record):
        self._ensure_healthy()
        keys = ("job_id", "fingerprint", "generation", "status", "stage", "progress",
                "created_at", "started_at", "heartbeat_at", "last_progress_at", "completed_at")
        result = {key: deepcopy(record.get(key)) for key in keys}
        result["progress"] = {key: value for key, value in (record.get("progress") or {}).items()
                              if key in METRICS and type(value) in (int, float)
                              and math.isfinite(value) and 0 <= value <= 1e18}
        if record["status"] == "completed":
            result["result"] = self._public_result(record["result"], record["job_id"])
        if record.get("error"):
            result["error"] = safe_error(runtime_error(record["error"].get("code", "gpu_render_failed")))
        return result

    def _new_record(self, payload, fingerprint):
        now = self._timestamp()
        return {
            "version": 1, "job_id": payload["job_id"], "fingerprint": fingerprint,
            "generation": 1, "status": "queued", "stage": "queued", "progress": {},
            "created_at": now, "started_at": None, "heartbeat_at": now,
            "last_progress_at": now, "completed_at": None, "error": None,
            "_payload": deepcopy(payload), "_children": {}, "_launches": {},
            "_owner": None, "_resource_blocked": False, "_cache_blocked": False,
        }

    def _matching(self, record, fingerprint):
        if record["fingerprint"] != fingerprint:
            raise runtime_error("gpu_job_input_conflict", 409)

    def _submission_lock(self, job_id):
        with self._mutex:
            return self._submission_locks.setdefault(job_id, threading.Lock())

    def _capacity(self):
        self._ensure_healthy()
        if not self._accepting:
            raise runtime_error("gpu_runtime_unavailable")
        if sum(row["status"] == "queued" for row in self._records.values()) >= self.queue_limit:
            raise runtime_error("gpu_queue_full")

    def _ensure_healthy(self):
        if not self._healthy:
            raise runtime_error("gpu_runtime_unverified")

    def _public_result(self, value, job_id):
        if not isinstance(value, dict) or value.get("job_id", job_id) != job_id:
            raise runtime_error("gpu_render_failed", 500)
        result = gpu_cache.public_result(value)
        # Preserve the legacy trivial success envelope without arbitrary fields.
        if type(value.get("ok")) is bool:
            result["ok"] = value["ok"]
        for key in ("random_template_output_sha256", "random_template_recipe_sha256"):
            if key in result and not re.fullmatch(r"[0-9a-f]{64}", str(result[key])):
                raise runtime_error("gpu_result_cache_unverified")
        if ("random_template_output_profile" in result
                and result["random_template_output_profile"] != gpu_cache.RECIPE_PROFILE):
            raise runtime_error("gpu_result_cache_unverified")
        for field in gpu_cache.ARTIFACT_FILENAMES:
            if result.get(field):
                try:
                    parsed = urlsplit(result[field])
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                        raise ValueError()
                except (TypeError, ValueError):
                    raise runtime_error("gpu_result_cache_unverified") from None
        return result

    def _complete(self, record, result):
        now = self._timestamp()
        record.update(status="completed", stage="completed", error=None,
                      result=self._public_result(result, record["job_id"]),
                      completed_at=record.get("completed_at") or now,
                      heartbeat_at=now, last_progress_at=now, _resource_blocked=False)
        self._save(record)

    def submit(self, payload):
        validate_render_payload(payload)
        fingerprint = self.fingerprint(payload)
        job_id = payload["job_id"]
        with self._submission_lock(job_id):
            with self._mutex:
                self._ensure_healthy()
                existing = self._records.get(job_id)
                if existing:
                    self._matching(existing, fingerprint)
                    return self._dto(existing)
                if not self._accepting:
                    raise runtime_error("gpu_runtime_unavailable")
            # Manifest verification never takes a render slot or global ledger
            # mutex. A completed job can be read while another job is rendering.
            cached = self.cached_result(payload)
            with self._mutex:
                record = self._new_record(payload, fingerprint)
                if cached is not None:
                    self._complete(record, cached)
                else:
                    self._capacity()
                    self._save(record)  # fsync before the caller can send 202
                self._wake.set()
                return self._dto(record)

    def get(self, job_id):
        self._path(job_id)
        with self._mutex:
            self._ensure_healthy()
            record = self._records.get(job_id)
            if record is None:
                raise runtime_error("gpu_job_not_found", 404)
            return self._dto(record)

    def resume(self, payload, expected_generation):
        validate_render_payload(payload)
        if type(expected_generation) is not int or expected_generation < 1:
            raise runtime_error("invalid_request", 400)
        fingerprint, job_id = self.fingerprint(payload), payload["job_id"]
        with self._submission_lock(job_id):
            with self._mutex:
                self._ensure_healthy()
                record = self._records.get(job_id)
                if record is None:
                    raise runtime_error("gpu_job_not_found", 404)
                self._matching(record, fingerprint)
                if record["generation"] == expected_generation + 1:
                    return self._dto(record)  # replay of a lost resume response
                if record["generation"] != expected_generation:
                    raise runtime_error("gpu_generation_conflict", 409)
                if record["status"] in {"queued", "running", "completed"}:
                    return self._dto(record)
                if self._process_risk(record) is not None:
                    raise runtime_error("gpu_process_state_unknown", 409)
            cached = self.cached_result(record["_payload"])
            with self._mutex:
                if cached is not None:
                    self._complete(record, cached)
                    return self._dto(record)
            if not self.can_resume or not self.can_resume(deepcopy(record["_payload"])):
                raise runtime_error("gpu_job_resume_unavailable", 409)
            with self._mutex:
                self._capacity()
                record.update(generation=record["generation"] + 1, status="queued", stage="queued",
                              progress={}, error=None, _owner=None, _children={}, _launches={},
                              _resource_blocked=False, _cache_blocked=False)
                self._save(record)
                self._wake.set()
                return self._dto(record)

    def run_sync(self, payload):
        """The compatibility route shares the durable identity and render slot."""
        fingerprint, job_id = self.fingerprint(payload), payload["job_id"]
        with self._submission_lock(job_id):
            with self._mutex:
                self._ensure_healthy()
                existing = self._records.get(job_id)
                if existing:
                    self._matching(existing, fingerprint)
                    if existing["status"] == "completed":
                        return deepcopy(existing["result"])
                    if existing["status"] == "failed":
                        raise runtime_error(existing["error"]["code"], 500)
                    raise runtime_error("gpu_job_running")
                if not self._accepting:
                    raise runtime_error("gpu_runtime_unavailable")
            cached = self.sync_cached_result(payload)
            with self._mutex:
                record = self._new_record(payload, fingerprint)
                if cached is not None:
                    self._complete(record, cached)
                    return deepcopy(record["result"])
                if self._resource_blocked() or not self.render_slots.acquire(blocking=False):
                    raise runtime_error("gpu_render_busy")
                lock = _FileLock(self.root / "locks" / (job_id + ".lock"))
                if not lock.acquire():
                    self.render_slots.release()
                    raise runtime_error("gpu_job_running")
                try:
                    self._begin(record)
                except BaseException:
                    lock.release()
                    self.render_slots.release()
                    raise
        try:
            return self._execute(record)
        finally:
            lock.release()
            self.render_slots.release()
            self._wake.set()

    def _begin(self, record):
        now = self._timestamp()
        record.update(status="running", stage="starting", _owner=self.instance,
                      started_at=record.get("started_at") or now, heartbeat_at=now,
                      last_progress_at=now, error=None)
        self._save(record)

    def _active(self, context):
        record = self._records.get(context.job_id)
        if (record and record["status"] == "running" and record["generation"] == context.generation
                and record.get("_owner") == context.owner):
            return record
        return None

    def emit(self, context, stage, **metrics):
        if stage not in STAGES:
            return
        clean = {key: value for key, value in metrics.items() if key in METRICS
                 and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e18}
        with self._mutex:
            record = self._active(context)
            if record is None:
                return
            prior = record["progress"] if record["stage"] == stage else {}
            advanced = record["stage"] != stage or any(
                key in ADVANCEMENT_METRICS and value > prior.get(key, -1) for key, value in clean.items()
            )
            if record["stage"] != stage:
                record["progress"] = {}
            record["stage"] = stage
            record["progress"].update(clean)
            record["heartbeat_at"] = self._timestamp()
            if advanced:
                record["last_progress_at"] = record["heartbeat_at"]
            self._save(record)

    def _launch_event(self, context, launch, *, begin):
        with self._mutex:
            record = self._active(context)
            if record is None:
                raise runtime_error("gpu_generation_conflict", 409)
            record["_launches"][launch] = {"boot_id": _boot_id()}
            self._save(record)

    def _process_event(self, context, pid, *, begin, launch=None):
        pid = int(pid)
        with self._mutex:
            record = self._active(context)
            if record is None:
                raise runtime_error("gpu_generation_conflict", 409)
            if begin:
                identity = process_identity(pid)
                if identity is None:
                    # Keep an unknown identity, so no subsequent restart can
                    # assume this process is gone merely because PID capture failed.
                    identity = {"pid": pid}
                record["_children"][str(pid)] = identity
                if launch is not None:
                    record["_launches"].pop(launch, None)
            else:
                identity = record["_children"].get(str(pid))
                if identity is not None and self.process_probe(identity) == "stopped":
                    record["_children"].pop(str(pid), None)
            self._save(record)

    def _process_risk(self, record):
        launches = record.get("_launches", {})
        current_boot = _boot_id()
        if any(not current_boot or not row.get("boot_id") or row["boot_id"] == current_boot for row in launches.values()):
            return "gpu_process_state_unknown"
        states = [self.process_probe(identity) for identity in record.get("_children", {}).values()]
        if "alive" in states:
            return "gpu_previous_process_running"
        if any(state != "stopped" for state in states):
            return "gpu_process_state_unknown"
        return None

    def _resource_blocked(self):
        return any(record.get("_resource_blocked") for record in self._records.values())

    def _execute(self, record):
        context = _ExecutionContext(self, record["job_id"], record["generation"], self.instance)
        try:
            with use_context(context):
                result = self.execute(deepcopy(record["_payload"]))
            with self._mutex:
                current = self._active(context)
                if current is None:
                    raise runtime_error("gpu_generation_conflict", 409)
                risk = self._process_risk(current)
                if risk is not None:
                    raise runtime_error(risk)
                self._complete(current, result)
            return result
        except Exception as exc:
            with self._mutex:
                current = self._active(context)
                if current is not None:
                    risk = self._process_risk(current)
                    cache_blocked = getattr(exc, "code", "") in RECOVERY_BLOCKING_CODES
                    current.update(status="recovery_required" if risk or cache_blocked else "failed",
                                   stage="recovery_required" if risk or cache_blocked else "failed",
                                   error=safe_error(runtime_error(risk) if risk else exc),
                                   _resource_blocked=bool(risk), _cache_blocked=cache_blocked,
                                   heartbeat_at=self._timestamp())
                    self._save(current)
            raise

    def _reconcile(self):
        # Called before opening HTTP intake. Never age out a running child or
        # delete the output cache. A stale heartbeat alone is no restart proof.
        for record in list(self._records.values()):
            if record["status"] not in {"running", "recovery_required"}:
                continue
            risk = self._process_risk(record)
            if risk:
                record.update(status="recovery_required", stage="recovery_required",
                              error=safe_error(runtime_error(risk)), _resource_blocked=True)
                self._save(record)
                continue
            try:
                cached = self.cached_result(record["_payload"])
            except Exception as exc:
                record.update(status="recovery_required", stage="recovery_required", error=safe_error(exc),
                              _resource_blocked=False, _cache_blocked=True)
                self._save(record)
                continue
            if cached is not None:
                self._complete(record, cached)
            elif record.get("_cache_blocked"):
                record.update(_resource_blocked=False)
                self._save(record)
            else:
                record.update(generation=record["generation"] + 1, status="queued", stage="queued",
                              progress={}, error=None, _owner=None, _children={}, _launches={},
                              _resource_blocked=False)
                self._save(record)

    def start(self):
        if self._dispatcher is not None:
            return
        self._dispatcher = threading.Thread(target=self._dispatch, name="drama-gpu-dispatch", daemon=True)
        self._heartbeat = threading.Thread(target=self._heartbeats, name="drama-gpu-heartbeat", daemon=True)
        self._dispatcher.start()
        self._heartbeat.start()

    def _dispatch(self):
        while not self._stop.is_set():
            self._wake.clear()
            lock = None
            with self._mutex:
                queued = sorted((row for row in self._records.values() if row["status"] == "queued"),
                                key=lambda row: (row["created_at"], row["job_id"]))
                if queued and self._healthy and not self._resource_blocked() and self.render_slots.acquire(blocking=False):
                    record = queued[0]
                    lock = _FileLock(self.root / "locks" / (record["job_id"] + ".lock"))
                    if not lock.acquire():
                        lock = None
                        self.render_slots.release()
                    else:
                        try:
                            self._begin(record)
                        except Exception:
                            lock.release()
                            self.render_slots.release()
                            self._accepting = False
                            return
            if lock is None:
                self._wake.wait(0.25)
                continue
            try:
                self._execute(record)
            except Exception:
                # The sanitized failure is already durable; no automatic rerun.
                pass
            finally:
                lock.release()
                self.render_slots.release()

    def _heartbeats(self):
        while not self._stop.wait(5):
            with self._mutex:
                try:
                    for record in list(self._records.values()):
                        if record["status"] == "running" and record.get("_owner") == self.instance:
                            record["heartbeat_at"] = self._timestamp()
                            self._save(record)
                except DramaSynthesisError:
                    return  # no unsafe details or implicit retries on disk failure

    def stop_intake(self):
        with self._mutex:
            self._accepting = False

    def close(self, timeout=30):
        self.stop_intake()
        self._stop.set()
        self._wake.set()
        if self._dispatcher is not None:
            self._dispatcher.join(timeout=timeout)
            if self._dispatcher.is_alive():
                return False  # keep the process owner lock until process exit
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=1)
        with self._mutex:
            if any(row["status"] == "running" and row.get("_owner") == self.instance for row in self._records.values()):
                return False  # a compatibility HTTP render is still draining
            self._owner.release()
        return True
