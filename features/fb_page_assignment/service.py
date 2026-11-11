"""Request orchestration; the public response deliberately stays minimal."""
from __future__ import annotations

import threading

from .runtime import log
from .selector import assign
from .sources import SourceError, load_profiles


class InvalidRequest(Exception):
    pass


def validate(payload):
    if not isinstance(payload, dict):
        raise InvalidRequest("body_must_be_object")
    external = payload.get("external_task_id")
    ids = payload.get("drama_ids")
    if (not isinstance(external, str) or not external.strip() or len(external) > 128
            or any(ord(c) < 32 for c in external)):
        raise InvalidRequest("external_task_id_required_1_to_128_characters")
    if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
        raise InvalidRequest("drama_ids_required_1_to_100_items")
    if any(not isinstance(cid, str) or not cid.strip() or len(cid) > 128
           or cid != cid.strip() or any(ord(c) < 32 for c in cid) for cid in ids):
        raise InvalidRequest("drama_id_must_be_nonblank_string_at_most_128_characters")
    try:
        for value in [external, *ids]:
            value.encode("utf-8")
    except UnicodeError:
        raise InvalidRequest("request_identifier_encoding_invalid") from None
    return external, ids


def failure_row(code, message, drama_id=None):
    return dict(drama_id=drama_id, page_id=None, status="failed", selection_stage="error",
                error_code=code, error_message=message, diagnostics={})


class Service:
    def __init__(self, sources, store, writer, notifier, *, profile_loader=load_profiles, profile_root=None):
        self.sources, self.store, self.writer, self.notifier = sources, store, writer, notifier
        self.profile_loader = profile_loader
        self.profile_root = profile_root or "/mnt/data-disk/fb-page-portraits"
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.worker = None

    def start(self):
        self.store.recover_inflight_alerts()
        self.worker = threading.Thread(target=self._run, name="fb-page-assignment-outbox", daemon=True)
        self.worker.start()

    def finish(self, task_id, external, rows, code, results, reasons):
        response = dict(task_id=task_id, results=results)
        try:
            self.store.save(task_id, external, rows, code, response, sorted(set(reasons)))
        except Exception as exc:
            log("local_persistence_failed", task_id=task_id, external_task_id=external,
                error_type=type(exc).__name__)
            # No durable outbox is available. Attempt this explicit emergency
            # notice once; never report an unpersisted allocation as successful.
            task = dict(task_id=task_id, external_task_id=external, rows=rows,
                        alert_reasons=["local_persistence_failed"])
            try:
                message_id = self.notifier.send(task)
                log("emergency_alert_sent", task_id=task_id, message_id=message_id)
            except Exception as notify_exc:
                log("emergency_alert_failed", task_id=task_id, error_type=type(notify_exc).__name__)
            return 503, dict(task_id=task_id, results=[])
        # A failed write stays frozen in the outbox and creates a failure alert.
        try:
            self.store.sync_task(task_id, self.writer)
        except Exception as exc:
            # Results are already frozen. Never rebuild this task with a
            # different error payload when synchronization coordination fails.
            log("sync_coordination_failed", task_id=task_id, error_type=type(exc).__name__)
            try:
                self.store.defer_sync(task_id, exc)
            except Exception as defer_exc:
                log("sync_deferral_failed", task_id=task_id, error_type=type(defer_exc).__name__)
                try:
                    self.notifier.send(dict(task_id=task_id, external_task_id=external, rows=rows,
                                            alert_reasons=["log_sync_pending", "sync_deferral_failed"]))
                except Exception as notify_exc:
                    log("emergency_alert_failed", task_id=task_id, error_type=type(notify_exc).__name__)
        self.wake.set()
        log("assignment_completed", task_id=task_id, external_task_id=external,
            http_status=code, results=results, alert_reasons=sorted(set(reasons)))
        return code, response

    def request_error(self, task_id, code, payload=None):
        external = payload.get("external_task_id") if isinstance(payload, dict) else None
        if not isinstance(external, str) or len(external) > 128:
            external = ""
        try:
            external.encode("utf-8")
        except UnicodeError:
            external = ""
        return self.finish(task_id, external, [failure_row(code, "请求参数无效，请按接口格式提交")],
                           400, [], [code])

    def handle(self, task_id, payload):
        try:
            external, ids = validate(payload)
        except InvalidRequest as exc:
            return self.request_error(task_id, str(exc), payload)
        try:
            data = self.sources.load(ids)
        except Exception as exc:
            error = exc.code if isinstance(exc, SourceError) else "source_internal_error"
            rows = [failure_row(error, "剧目或Page实时数据查询失败", cid) for cid in ids]
            return self.finish(task_id, external, rows, 503,
                               [dict(drama_id=cid, page_id=None) for cid in ids], [error])
        profiles, metadata, warnings = self.profile_loader([p["page_id"] for p in data["pages"]], self.profile_root)
        rows = assign(ids, data["dramas"], data["pages"], profiles)
        reasons = list(data.get("warnings", [])) + list(warnings)
        for row in rows:
            row["diagnostics"].update(profile_snapshot=metadata,
                                      source_read_at=data.get("source_read_at"),
                                      source_warnings=data.get("warnings", []), profile_warnings=warnings,
                                      group_id=62)
            if row.get("status") in {"failed", "degraded"}:
                reasons.append(row.get("error_code") or row["status"])
        results = [dict(drama_id=row["drama_id"], page_id=row["page_id"]) for row in rows]
        return self.finish(task_id, external, rows, 200, results, reasons)

    def tick(self):
        self._deliver_alerts()
        for task in self.store.pending_sync(limit=5):
            self.store.sync_task(task["task_id"], self.writer)
        self._deliver_alerts()

    def _deliver_alerts(self):
        from .notify import DeliveryError
        for task in self.store.pending_alerts(limit=20):
            claimed = self.store.claim_alert(task["task_id"])
            if not claimed:
                continue
            try:
                receipt = self.notifier.send(claimed)
                self.store.mark_alert(task["task_id"], "sent", message_id=receipt)
                log("alert_sent", task_id=task["task_id"], external_task_id=task.get("external_task_id"),
                    message_id=receipt)
            except DeliveryError as exc:
                self.store.mark_alert(task["task_id"], "unknown" if exc.uncertain else "failed", error_code=exc.code)
                log("alert_failed", task_id=task["task_id"], error_code=exc.code, uncertain=exc.uncertain)
            except Exception as exc:
                self.store.mark_alert(task["task_id"], "unknown", error_code="notification_internal_error")
                log("alert_internal_error", task_id=task["task_id"], error_type=type(exc).__name__)

    def _run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                log("outbox_iteration_failed", error_type=type(exc).__name__)
            self.wake.wait(2)
            self.wake.clear()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.worker:
            self.worker.join(timeout=20)
