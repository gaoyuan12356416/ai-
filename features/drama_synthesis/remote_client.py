"""Reconnect to one immutable GPU execution instead of retrying media work."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import math
import re
import threading
from typing import Mapping
from urllib.parse import quote, urlsplit

import requests

from .async_runtime import ERROR_MESSAGES as GPU_ERROR_MESSAGES, render_fingerprint
from .core import DramaSynthesisError


REQUEST_TIMEOUT = (3, 15)
POLL_SECONDS = 10
REMOTE_STATUSES = frozenset(("queued", "running", "completed", "failed", "recovery_required"))
SAFE_ERRORS = {
    **GPU_ERROR_MESSAGES,
    "gpu_job_input_conflict": "任务输入与已有制作记录不一致，已停止重复制作",
    "gpu_job_not_found": "制作记录暂不可用，需核对原任务后恢复",
    "gpu_result_cache_unverified": "已有成片暂时无法校验，已停止重制，请稍后重试",
    "gpu_job_interrupted": "制作节点重启，任务需从已验证的检查点恢复",
    "gpu_job_recovery_required": "制作执行状态需要核对，未自动重制",
    "gpu_render_failed": "媒体制作失败，请查看任务阶段后申请恢复",
    "gpu_queue_full": "制作节点队列已满，等待可用资源",
}
RECONCILIATION_ERRORS = frozenset((
    "gpu_result_cache_unverified", "gpu_runtime_unverified", "gpu_process_state_unknown",
    "gpu_previous_process_running", "gpu_job_resume_unavailable",
))


class RemoteJobError(DramaSynthesisError):
    pass


class RemoteJobConflict(RemoteJobError):
    def __init__(self):
        super().__init__("gpu_job_input_conflict", SAFE_ERRORS["gpu_job_input_conflict"], 409)


class RemoteJobFailed(RemoteJobError):
    pass


class RemoteRecoveryRequired(RemoteJobError):
    def __init__(self, code="gpu_job_recovery_required"):
        super().__init__(code, SAFE_ERRORS.get(code, SAFE_ERRORS["gpu_job_recovery_required"]), 409)


class RemotePollingInterrupted(Exception):
    """The CPU observer stopped; the accepted GPU execution must remain active."""

    def __init__(self):
        super().__init__("后台观察已暂停，原制作任务将由后续执行继续跟踪")


def _safe_error(value, default="gpu_render_failed"):
    code = value.get("code") if isinstance(value, Mapping) else default
    if not isinstance(code, str) or code not in SAFE_ERRORS:
        code = default
    return {"code": code, "message": SAFE_ERRORS.get(code, SAFE_ERRORS.get(default, SAFE_ERRORS["gpu_render_failed"]))}


def _request(session, method, url, token, payload=None):
    kwargs = {
        "headers": {"Authorization": "Bearer " + token, "Accept": "application/json"},
        "timeout": REQUEST_TIMEOUT, "allow_redirects": False,
    }
    if payload is not None:
        kwargs["json"] = payload
    response = session.request(method, url, **kwargs)
    try:
        status = int(response.status_code)
        try:
            data = response.json()
        except (ValueError, TypeError):
            data = None
        return status, data
    finally:
        response.close()


def _snapshot(data, job_id, fingerprint, last_generation):
    if not isinstance(data, Mapping):
        raise RemoteRecoveryRequired()
    if data.get("job_id") != job_id or data.get("fingerprint") != fingerprint:
        raise RemoteJobConflict()
    generation = data.get("generation")
    if type(generation) is not int or generation < 1 or generation < last_generation:
        raise RemoteRecoveryRequired()
    if data.get("status") not in REMOTE_STATUSES:
        raise RemoteRecoveryRequired()
    metrics = data.get("metrics", data.get("progress", {}))
    if not isinstance(metrics, Mapping):
        raise RemoteRecoveryRequired()
    safe_metrics = {}
    for key, value in metrics.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            safe_metrics[key] = value
    stage = data.get("stage")
    if not isinstance(stage, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", stage):
        raise RemoteRecoveryRequired()
    value = {
        "job_id": job_id, "fingerprint": fingerprint, "generation": generation,
        "status": data["status"], "stage": stage, "metrics": safe_metrics,
        "connection_state": "connected", "error_code": "",
    }
    for field in ("created_at", "started_at", "heartbeat_at", "last_progress_at", "completed_at"):
        value[field] = str(data.get(field) or "")[:64]
    if data["status"] == "completed":
        result = data.get("result")
        if not isinstance(result, Mapping) or result.get("job_id") != job_id:
            raise RemoteRecoveryRequired()
        fields = (
            "job_id", "output_video_url", "output_video_no_bgm_url", "output_random_template_url",
            "random_template_output_sha256", "random_template_output_profile", "random_template_recipe_sha256",
        )
        value["result"] = {field: result[field] for field in fields if field in result}
    if data["status"] in {"failed", "recovery_required"}:
        default = "gpu_job_recovery_required" if data["status"] == "recovery_required" else "gpu_render_failed"
        value["error"] = _safe_error(data.get("error"), default)
    return value


def _stalled(snapshot, seconds):
    if snapshot.get("status") != "running" or not snapshot.get("last_progress_at"):
        return False
    try:
        value = datetime.fromisoformat(snapshot["last_progress_at"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - value).total_seconds() >= seconds
    except (ValueError, TypeError):
        return False


def wait_for_gpu_job(base_url, token, payload, *, on_status=None, stop_event=None, session=None,
                     poll_seconds=POLL_SECONDS, stall_seconds=900, known_remote=False,
                     previous_status=None, explicit_resume=False, expected_generation=None):
    """Wait for the same GPU job, with no total wall-time rendering deadline.

    A missing record may be submitted only before this client has proof of an
    accepted execution.  Known records that disappear require reconciliation.
    An explicit operator resume is limited to one frozen expected generation.
    """
    parsed = urlsplit(str(base_url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid GPU worker base URL")
    if not isinstance(token, str) or not token:
        raise ValueError("GPU worker token is required")
    frozen = copy.deepcopy(dict(payload))
    job_id = frozen.get("job_id")
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", job_id):
        raise ValueError("invalid job_id")
    fingerprint = render_fingerprint(frozen)
    stop = stop_event if stop_event is not None else threading.Event()
    collection_url = str(base_url).rstrip("/") + "/api/gpu-video/jobs"
    status_url = collection_url + "/" + quote(job_id, safe="")
    last = {
        "job_id": job_id, "fingerprint": fingerprint, "generation": 0,
        "status": "queued", "stage": "connecting", "metrics": {},
        "started_at": "", "heartbeat_at": "", "last_progress_at": "",
    }
    if previous_status is not None:
        if not isinstance(previous_status, Mapping) or previous_status.get("job_id") != job_id or previous_status.get("fingerprint") != fingerprint:
            raise RemoteJobConflict()
        if int(previous_status.get("generation") or 0) > 0:
            last = copy.deepcopy(dict(previous_status))
            known_remote = True
    if expected_generation is not None and (type(expected_generation) is not int or expected_generation < 1):
        raise ValueError("expected_generation must be a positive integer")
    if expected_generation is not None:
        explicit_resume = True
    resume_generation = expected_generation
    owned_session = session is None
    client = session if session is not None else requests.Session()

    def emit(snapshot):
        if on_status is not None:
            on_status(copy.deepcopy(snapshot))

    def disconnected():
        snapshot = copy.deepcopy(last)
        snapshot.update(connection_state="reconnecting", error_code="remote_connection_unavailable")
        snapshot["stalled"] = _stalled(snapshot, stall_seconds)
        emit(snapshot)

    def pause():
        if stop.wait(max(0.0, float(poll_seconds))):
            raise RemotePollingInterrupted()

    try:
        while True:
            if stop.is_set():
                raise RemotePollingInterrupted()
            try:
                http_status, data = _request(client, "GET", status_url, token)
            except requests.RequestException:
                disconnected()
                pause()
                continue
            if http_status == 404:
                if not isinstance(data, Mapping) or data.get("code") != "gpu_job_not_found" or known_remote:
                    raise RemoteRecoveryRequired("gpu_job_not_found")
                if stop.is_set():
                    raise RemotePollingInterrupted()
                try:
                    http_status, data = _request(client, "POST", collection_url, token, frozen)
                except requests.RequestException:
                    disconnected()
                    pause()
                    continue
            if http_status in {401, 403}:
                raise RemoteJobError("gpu_job_authorization_failed", "制作节点授权不可用，请联系管理员", 502)
            if http_status == 409:
                code = data.get("code") if isinstance(data, Mapping) else ""
                if code == "gpu_job_input_conflict":
                    raise RemoteJobConflict()
                raise RemoteRecoveryRequired(code if code in SAFE_ERRORS else "gpu_job_recovery_required")
            if isinstance(data, Mapping) and data.get("code") in RECONCILIATION_ERRORS:
                raise RemoteRecoveryRequired(data["code"])
            if http_status == 429 or http_status >= 500:
                disconnected()
                pause()
                continue
            if http_status not in {200, 202}:
                raise RemoteJobError("gpu_job_request_rejected", "制作节点拒绝了任务请求，请核对配置", 502)
            last = _snapshot(data, job_id, fingerprint, int(last.get("generation") or 0))
            known_remote = True
            last["stalled"] = _stalled(last, stall_seconds)
            emit(last)
            if last["status"] == "completed":
                return copy.deepcopy(last["result"])
            if last["status"] in {"failed", "recovery_required"}:
                if explicit_resume and (resume_generation is None or last["generation"] == resume_generation):
                    if stop.is_set():
                        raise RemotePollingInterrupted()
                    if resume_generation is None:
                        resume_generation = last["generation"]
                    resume_payload = {**frozen, "expected_generation": resume_generation}
                    try:
                        status, resume_data = _request(client, "POST", status_url + "/resume", token, resume_payload)
                    except requests.RequestException:
                        disconnected()
                        pause()
                        continue
                    if isinstance(resume_data, Mapping) and resume_data.get("code") in RECONCILIATION_ERRORS:
                        raise RemoteRecoveryRequired(resume_data["code"])
                    if status == 429 or status >= 500:
                        disconnected()
                        pause()
                        continue
                    if status in {401, 403}:
                        raise RemoteJobError("gpu_job_authorization_failed", "制作节点授权不可用，请联系管理员", 502)
                    if status == 409:
                        if isinstance(resume_data, Mapping) and resume_data.get("code") == "gpu_job_input_conflict":
                            raise RemoteJobConflict()
                        code = resume_data.get("code") if isinstance(resume_data, Mapping) else ""
                        raise RemoteRecoveryRequired(code if code in SAFE_ERRORS else "gpu_job_recovery_required")
                    if status not in {200, 202}:
                        raise RemoteRecoveryRequired()
                    resumed = _snapshot(resume_data, job_id, fingerprint, last["generation"])
                    last = resumed
                    emit(last)
                    if last["status"] == "completed":
                        return copy.deepcopy(last["result"])
                    if last["generation"] == resume_generation and last["status"] in {"failed", "recovery_required"}:
                        raise RemoteRecoveryRequired()
                    pause()
                    continue
                error = last.get("error") or _safe_error(None)
                if last["status"] == "recovery_required":
                    raise RemoteRecoveryRequired(error["code"])
                raise RemoteJobFailed(error["code"], error["message"], 502)
            pause()
    finally:
        if owned_session:
            client.close()
