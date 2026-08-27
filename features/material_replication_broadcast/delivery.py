"""Batch-only HTTP/worker orchestration using the existing Feishu adapters.

No upstream scheduler, material-production operation, or legacy-queue write is
performed here. The adapters are injected so tests never need live services.
"""

import json
import logging
import threading
import time

from features.material_status_broadcast import service as legacy
from . import service


ENDPOINT = "/api/integrations/v1/material-replication-events"
RETRY_DELAYS = (1, 5, 30, 120, 600)


def token_eligible(value):
    return (
        isinstance(value, str) and len(value) >= 32 and value.isascii()
        and not any(char.isspace() for char in value)
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite JSON number")


class ReplicationRuntime:
    def __init__(
        self, db_path, tokens, fallback_chat_id, resolve_editor, resolve_open_id,
        send_text, dependencies_ready=lambda: True, audit=None, store=None,
    ):
        self.db_path = db_path
        self.tokens = tuple(tokens)
        self.fallback_chat_id = str(fallback_chat_id or "").strip()
        self.resolve_editor = resolve_editor
        self.resolve_open_id = resolve_open_id
        self.send_text = send_text
        self.dependencies_ready = dependencies_ready
        self.audit = audit
        self._store = store
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._last_send = 0.0
        self.stop_event = threading.Event()
        self.thread = None

    @property
    def store(self):
        with self._lock:
            if self._store is None:
                self._store = service.ReplicationOutbox(self.db_path)
            return self._store

    def configured(self):
        return bool(
            any(token_eligible(token) for token in self.tokens)
            and self.fallback_chat_id
            and len(self.fallback_chat_id) <= 128
            and not any(char.isspace() for char in self.fallback_chat_id)
            and self.dependencies_ready()
        )

    def authorized(self, header):
        matched = False
        for token in self.tokens:
            valid = token_eligible(token) and legacy.validate_bearer_authorization(header, token)
            matched = bool(valid) or matched
        return matched

    def start(self):
        with self._lock:
            if not self.configured():
                return None
            if self.thread is not None and self.thread.is_alive():
                return self.thread
            self.store
            self.stop_event.clear()
            self.thread = threading.Thread(
                target=self._loop, name="material-replication-broadcast-worker", daemon=True,
            )
            self.thread.start()
            return self.thread

    def ready(self):
        try:
            thread = self.start()
            return bool(thread and thread.is_alive())
        except Exception as exc:
            logging.error("replication worker unavailable type=%s", type(exc).__name__)
            return False

    def stop(self, timeout=5):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)

    def _record(self, row):
        if self.audit is not None and row:
            try:
                self.audit(row)
            except Exception as exc:
                logging.error("replication audit unavailable type=%s", type(exc).__name__)
        return row

    def _fallback(self, row, code, message):
        return self.store.prepare_fallback(
            row["id"], row["lease_id"], self.fallback_chat_id, code, message,
        )

    def _failure(self, row, code, message, retryable=True):
        # Any earlier uncertain send remains uncertain, even if a later attempt
        # receives an explicit rejection. It must never change destination.
        uncertain = bool(row.get("uncertain"))
        remaining = row["attempt_count"] < row["max_attempts"]
        if (retryable or uncertain) and remaining:
            delay = RETRY_DELAYS[min(row["attempt_count"] - 1, len(RETRY_DELAYS) - 1)]
            return self.store.retry(row["id"], row["lease_id"], code, message, delay)
        if uncertain:
            return self.store.unknown(row["id"], row["lease_id"], code, message)
        if row["phase"] == "private":
            return self._fallback(row, code, message)
        return self.store.dead_letter(row["id"], row["lease_id"], code, message)

    def process(self, row):
        if not row.get("receive_id"):
            try:
                editor = self.resolve_editor(row["payload"]["editor_username"])
                if not editor.get("matched"):
                    return self._record(self._fallback(
                        row, editor.get("code") or "optimizer_not_found",
                        editor.get("message") or "未匹配到剪辑师账号",
                    ))
                person = self.resolve_open_id(editor["email"])
                if not person.get("matched"):
                    return self._record(self._fallback(
                        row, person.get("code") or "feishu_user_not_found",
                        person.get("message") or "未匹配到剪辑师飞书用户",
                    ))
                row = self.store.freeze_target(row["id"], row["lease_id"], person["open_id"])
            except service.ReplicationError:
                raise
            except Exception as exc:
                return self._record(self._failure(
                    row, getattr(exc, "code", "mapping_unavailable"), str(exc),
                    retryable=getattr(exc, "retryable", True),
                ))

        # Prevent this new worker alone from flooding one recipient when the
        # upstream sends several consecutive batches. Platform 429 is retried.
        with self._send_lock:
            delay = max(0.0, 0.25 - (time.monotonic() - self._last_send))
            if delay:
                self.stop_event.wait(delay)
            row = self.store.begin_send(row["id"], row["lease_id"])
            if row["status"] != "processing":
                return self._record(row)
            previous_uncertain = bool(row.get("previous_uncertain"))
            self._last_send = time.monotonic()
            try:
                sent = self.send_text(
                    "open_id" if row["phase"] == "private" else "chat_id",
                    row["receive_id"], row["message_text"], row["message_uuid"],
                )
            except Exception as exc:
                # Legacy errors now carry observational certainty metadata;
                # unknown exceptions are conservative. Legacy handling itself
                # remains unchanged.
                uncertain = getattr(exc, "uncertain", True)
                if not uncertain:
                    row = self.store.clear_known_failure(
                        row["id"], row["lease_id"], previous_uncertain,
                    )
                return self._record(self._failure(
                    row, getattr(exc, "code", "message_send_unknown"), str(exc),
                    retryable=getattr(exc, "retryable", True),
                ))
        message_id = sent.get("message_id") if isinstance(sent, dict) else None
        if not isinstance(message_id, str) or not message_id.strip():
            return self._record(self._failure(
                row, "message_send_invalid_response", "飞书未返回可确认的消息编号",
            ))
        return self._record(self.store.delivered(row["id"], row["lease_id"], message_id))

    def _loop(self):
        while not self.stop_event.is_set():
            row = None
            try:
                row = self.store.claim_next(lease_seconds=300)
                if row is None:
                    self.stop_event.wait(1)
                    continue
                self.process(row)
            except Exception as exc:
                logging.error(
                    "replication worker error batch=%s type=%s",
                    row["id"] if row else "none", type(exc).__name__,
                )
                # If the post-send database write failed, the persisted send
                # marker is the authority. Never assume the network failed.
                if row:
                    try:
                        current = self.store.get(row["id"])
                        if current["status"] == "processing" and current["lease_id"] == row["lease_id"]:
                            self._record(self._failure(
                                current, "internal_error", "播报处理异常，等待安全恢复",
                            ))
                    except Exception:
                        pass  # Lease recovery owns any unavailable/expired row.
                self.stop_event.wait(1)


def handle_request(handler, runtime, json_response):
    def error(status, code, message):
        handler.close_connection = True
        json_response(handler, status, {"code": code, "message": message}, no_store=True)

    if not runtime.configured():
        error(503, "service_unavailable", "自动复刻播报接口暂未配置")
        return
    if not runtime.authorized(handler.headers.get("Authorization", "")):
        error(401, "invalid_token", "Bearer Token 缺失或错误")
        return
    if str(handler.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower() != "application/json":
        error(415, "unsupported_media_type", "Content-Type 必须为 application/json")
        return
    if handler.headers.get("Transfer-Encoding") or handler.headers.get("Content-Encoding", "identity").lower() != "identity":
        error(400, "invalid_request", "不支持分块或压缩请求体")
        return
    lengths = handler.headers.get_all("Content-Length", [])
    if (len(lengths) != 1 or len(lengths[0]) > 10
            or not lengths[0].isascii() or not lengths[0].isdigit()):
        error(400, "invalid_request", "需要唯一有效的 Content-Length")
        return
    length = int(lengths[0])
    if length > service.MAX_REQUEST_BYTES:
        error(413, "payload_too_large", "请求体超过 32 KiB")
        return
    try:
        key = legacy.validate_idempotency_key(handler.headers.get("Idempotency-Key"))
        connection = getattr(handler, "connection", None)
        old_timeout = connection.gettimeout() if connection is not None else None
        try:
            if connection is not None:
                connection.settimeout(10)
            raw = handler.rfile.read(length)
        finally:
            if connection is not None:
                connection.settimeout(old_timeout)
        if len(raw) != length:
            error(400, "invalid_request", "请求体长度不完整")
            return
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
        payload = service.normalize_payload(payload)
    except service.ReplicationError as exc:
        error(exc.status, exc.code, str(exc))
        return
    except (UnicodeError, ValueError, RecursionError):
        error(400, "invalid_json", "请求体不是有效且字段唯一的 UTF-8 JSON")
        return
    except (TimeoutError, OSError):
        error(400, "invalid_request", "请求体接收超时或中断")
        return
    if not runtime.ready():
        error(503, "service_unavailable", "自动复刻播报投递服务暂不可用")
        return
    try:
        row = runtime.store.enqueue(
            key, payload,
            source_ip=legacy.extract_audit_source_ip(
                handler.client_address[0], handler.headers.get("X-Real-IP", ""),
            ),
        )
    except service.ReplicationError as exc:
        error(exc.status, exc.code, str(exc))
        return
    except Exception as exc:
        logging.error("replication enqueue unavailable type=%s", type(exc).__name__)
        error(503, "service_unavailable", "批次暂时无法可靠落库")
        return
    created = bool(row.get("created"))
    json_response(handler, 202, {
        "code": "accepted" if created else "duplicate_accepted",
        "message": "批次已接收，等待投递" if created else "批次已接收",
        "batch_id": service.format_batch_id(row["id"]),
        "duplicate": not created,
        "item_count": len(row["payload"]["items"]),
        "delivery_status": row["status"],
        "delivery_kind": row.get("delivery_kind", ""),
        "received_at": row["created_at"],
    }, no_store=True)
