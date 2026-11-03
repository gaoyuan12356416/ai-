"""Legacy TT readback using the owning runtime's frozen publication routes.

Channel imports stay lazy: this package is deployed to every sidecar. The
existing publisher source is retained without replacing its deployed features.
"""
from datetime import timedelta
import re
from collections.abc import Mapping


def _existing_receipt(executor, task, token):
    from features.tt_auto_posts.publisher import AutoPostExecutionError, _public_tiktok_url, _stable_gpu_job_id

    if not task.publish_id:
        raise AutoPostExecutionError("tt_auto_reconcile_publish_id_missing", "原任务没有可核对发布ID", 409)
    route = getattr(executor, "_video_route_for_task", None)
    gpu = route(task).gpu_client if route is not None else executor.gpu_client
    with executor.account_source.publish_credentials(task.account_id) as credentials:
        result = gpu.reconcile(job_id=_stable_gpu_job_id(task), source_account_id=task.account_id,
                               access_token=credentials.reveal_access_token())
    if not isinstance(result, Mapping):
        raise AutoPostExecutionError("tt_auto_reconcile_response_invalid", "GPU核对响应无效", 502)
    returned_id = str(result.get("publish_id") or "").strip()
    if not returned_id:
        raise AutoPostExecutionError("tt_auto_reconcile_publish_id_missing", "GPU账本暂无可核对publish_id", 503)
    if task.publish_id != returned_id:
        raise AutoPostExecutionError("tt_auto_publish_id_conflict", "CPU与GPU publish_id不一致", 500)
    remote = str(result.get("state") or result.get("remote_status") or "").lower()
    updates = {"publish_id": returned_id, "claim_worker": "", "claim_token": "", "lease_expires_at_utc": ""}
    if remote in {"published", "publish_complete"}:
        target, event = "published", "task_reconciled_published"
        updates.update(publish_url=_public_tiktok_url(result.get("publish_url")), unknown_outcome=False,
                       claim_phase="", error_code="", error_message="")
    elif remote in {"failed", "publish_failed"}:
        target, event = "failed", "task_reconciled_failed"
        updates.update(unknown_outcome=False, claim_phase="", error_code="tt_auto_remote_publish_failed",
                       error_message="TikTok远端发布失败")
    else:
        uncertain = bool(task.unknown_outcome or task.status == "unknown")
        target, event = ("unknown" if uncertain else "reconciling"), "task_reconcile_pending"
        updates.update(unknown_outcome=uncertain, claim_phase="reconcile",
                       error_code="", error_message="",
                       next_attempt_at_utc=executor._now() + timedelta(minutes=5))
    saved = executor.store.transition_task(task.id, target, expected_statuses={task.status},
                                         claim_token=token, updates=updates, event_type=event)
    sync = getattr(executor, "_sync_code_state", None)
    if target == "published" and sync is not None:
        sync(saved, "published", best_effort=True)
    return saved


def reconcile_next(executor, worker_id, *, after_task_id=0):
    """Query one existing legacy publish ID; never enter publishing code."""
    from features.tt_auto_posts.publisher import AutoPostExecutionError

    worker = str(worker_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:@-]{1,128}", worker):
        raise AutoPostExecutionError("tt_auto_worker_id_invalid", "执行器身份无效", 400)
    if isinstance(after_task_id, bool) or not isinstance(after_task_id, int) or after_task_id < 0:
        raise AutoPostExecutionError("tt_auto_reconcile_cursor_invalid", "核对游标无效", 400)
    if not executor._execute_slots.acquire(blocking=False):
        return {"ok": True, "claimed": False, "busy": True}
    try:
        claim = executor.store.claim_next_executable_task(worker_id=worker, lease_seconds=executor.lease_seconds,
            now=executor._now(), allowed_phases={"reconcile"}, legacy_reconcile_only=True, after_task_id=after_task_id)
        if claim is None:
            return {"ok": True, "claimed": False}
        task, token = claim.task, claim.reveal_claim_token()
        try:
            task = _existing_receipt(executor, task, token)
        except Exception as exc:
            task = executor._retry_or_fail(executor.store.get_task(task.id), "reconcile", exc, token)
        executor._update_run(task.run_id)
        return {"ok": True, "claimed": True, "task": {
            "id": task.id, "status": task.status, "unknown_outcome": task.unknown_outcome,
            "publish_id": task.publish_id, "error_code": task.error_code,
        }}
    finally:
        executor._execute_slots.release()
