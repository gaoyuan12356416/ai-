"""Narrow write broker for automatic-post four-character code routes.

The automatic publisher remains read-only against the legacy TT Post ledger.
This separately sandboxed loopback service may mutate only the shared
``tt_post_code_route`` tables so old and new publishing systems allocate from
one globally unique four-character namespace.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit

from features.tt_posts.code_routes import allocate_code_route
from features.tt_posts.links import TTPostLinkError, validate_w2a_url

from .validation import valid_internal_bearer


UTC = timezone.utc
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18832
MAX_BODY_BYTES = 64 * 1024
AUTO_QUEUE_ID_NAMESPACE = 7_000_000_000_000_000_000
AUTO_QUEUE_ID_SLOTS = 999_999_999_999_999_999
ROUTE_STATES = frozenset(
    {"reserved", "publishing", "reconciling", "unknown", "published", "failed"}
)
_CODE_RE = re.compile(r"^[A-Z0-9]{4}$")
_CONTENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ROUTE_QUERY_FIELDS = {
    "c",
    "af_adset",
    "af_adset_id",
    "af_ad",
    "af_ad_id",
    "af_channel",
    "af_c_id",
    "af_dp",
}
_ROUTE_COLUMNS = {
    "code",
    "queue_id",
    "content_id",
    "c",
    "af_adset",
    "af_adset_id",
    "af_ad",
    "af_ad_id",
    "af_channel",
    "af_c_id",
    "long_url",
    "state",
    "created_at",
    "published_at",
    "updated_at",
}


class AutoCodeBrokerError(RuntimeError):
    """Stable, non-secret error returned by the route broker."""

    def __init__(self, code: str, message: str, status: int = 400):
        self.code = str(code or "tt_auto_code_route_error")[:96]
        self.status = int(status)
        super().__init__(str(message or "自动发布四位码路由失败")[:500])


def synthetic_queue_id(task_id: Any) -> int:
    if isinstance(task_id, bool):
        raise AutoCodeBrokerError("tt_auto_code_task_id_invalid", "自动发布任务ID无效")
    try:
        normalized = int(task_id)
    except (TypeError, ValueError, OverflowError):
        normalized = 0
    if normalized <= 0 or normalized > AUTO_QUEUE_ID_SLOTS:
        raise AutoCodeBrokerError("tt_auto_code_task_id_invalid", "自动发布任务ID无效")
    return AUTO_QUEUE_ID_NAMESPACE + normalized


def _utc_iso(value: Any, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise AutoCodeBrokerError("tt_auto_code_time_invalid", "%s无效" % label)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _route_values(
    task_id: Any,
    content_id: Any,
    long_url: Any,
    created_at: Any,
) -> Tuple[int, Dict[str, str]]:
    normalized_task_id = synthetic_queue_id(task_id)
    clean_content_id = str(content_id or "").strip()
    if not _CONTENT_ID_RE.fullmatch(clean_content_id):
        raise AutoCodeBrokerError(
            "tt_auto_code_content_id_invalid", "自动发布剧ID无效"
        )
    try:
        target = validate_w2a_url(long_url)
    except (TTPostLinkError, TypeError, ValueError):
        raise AutoCodeBrokerError(
            "tt_auto_code_route_invalid", "自动发布四位码目标地址无效"
        ) from None
    pairs = parse_qsl(urlsplit(target).query, keep_blank_values=True)
    if (
        len(pairs) != len(_ROUTE_QUERY_FIELDS)
        or {key for key, _value in pairs} != _ROUTE_QUERY_FIELDS
        or len({key for key, _value in pairs}) != len(pairs)
    ):
        raise AutoCodeBrokerError(
            "tt_auto_code_route_invalid", "自动发布四位码归因参数无效"
        )
    query = dict(pairs)
    if (
        query.get("af_channel") != "TT"
        or query.get("af_dp") != clean_content_id
        or query.get("af_c_id") != str(int(task_id))
        or any(not str(query.get(name) or "") for name in _ROUTE_QUERY_FIELDS)
    ):
        raise AutoCodeBrokerError(
            "tt_auto_code_route_invalid", "自动发布四位码归因身份不一致", 409
        )
    timestamp = _utc_iso(created_at, "自动发布任务冻结时间")
    return normalized_task_id, {
        "content_id": clean_content_id,
        "c": query["c"],
        "af_adset": query["af_adset"],
        "af_adset_id": query["af_adset_id"],
        "af_ad": query["af_ad"],
        "af_ad_id": query["af_ad_id"],
        "af_channel": "TT",
        "af_c_id": query["af_c_id"],
        "long_url": target,
        "state": "reserved",
        "created_at": timestamp,
        "published_at": "",
        "updated_at": timestamp,
    }


class LegacyCodeRouteStore:
    """Write only the shared route namespace, never legacy queue rows."""

    def __init__(self, db_path: Any, *, timeout_seconds: float = 5.0):
        path = Path(str(db_path or "").strip()).expanduser()
        if not path.is_absolute():
            raise AutoCodeBrokerError(
                "tt_auto_code_db_path_invalid", "四位码数据库路径无效", 500
            )
        self.db_path = str(path.resolve(strict=False))
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 30.0))

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                self.db_path,
                timeout=self.timeout_seconds,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=%d" % int(self.timeout_seconds * 1000))
            return conn
        except sqlite3.Error:
            raise AutoCodeBrokerError(
                "tt_auto_code_db_unavailable", "四位码数据库暂不可用", 503
            ) from None

    def validate_schema(self) -> None:
        try:
            with contextlib.closing(self._connect()) as conn:
                route_columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(tt_post_code_route)")
                }
                audit_columns = {
                    str(row[1])
                    for row in conn.execute(
                        "PRAGMA table_info(tt_post_code_recycle_audit)"
                    )
                }
                queue_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tt_post_queue'"
                ).fetchone()
        except sqlite3.Error:
            raise AutoCodeBrokerError(
                "tt_auto_code_db_unavailable", "四位码数据库暂不可用", 503
            ) from None
        if (
            not _ROUTE_COLUMNS.issubset(route_columns)
            or not {"code", "old_queue_id", "new_queue_id"}.issubset(audit_columns)
            or queue_table is None
        ):
            raise AutoCodeBrokerError(
                "tt_auto_code_schema_invalid", "四位码数据库结构不完整", 500
            )

    @staticmethod
    def _same_route(existing: Mapping[str, Any], requested: Mapping[str, str]) -> bool:
        immutable = {
            "content_id",
            "c",
            "af_adset",
            "af_adset_id",
            "af_ad",
            "af_ad_id",
            "af_channel",
            "af_c_id",
            "long_url",
            "created_at",
        }
        return all(
            secrets.compare_digest(
                str(existing.get(name) or "").encode("utf-8"),
                requested[name].encode("utf-8"),
            )
            for name in immutable
        )

    def freeze(
        self,
        task_id: Any,
        *,
        content_id: Any,
        long_url: Any,
        created_at: Any,
    ) -> Dict[str, Any]:
        queue_id, route = _route_values(task_id, content_id, long_url, created_at)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            queue_collision = conn.execute(
                "SELECT 1 FROM tt_post_queue WHERE id=?", (queue_id,)
            ).fetchone()
            if queue_collision is not None:
                raise AutoCodeBrokerError(
                    "tt_auto_code_namespace_conflict", "自动发布四位码命名空间冲突", 500
                )
            existing_row = conn.execute(
                "SELECT * FROM tt_post_code_route WHERE queue_id=?", (queue_id,)
            ).fetchone()
            if existing_row is not None:
                existing = dict(existing_row)
                if not self._same_route(existing, route):
                    raise AutoCodeBrokerError(
                        "tt_auto_code_route_conflict", "自动发布任务已绑定不同四位码路由", 409
                    )
                result = existing
            else:
                result = allocate_code_route(conn, queue_id, route)
            conn.execute("COMMIT")
        except AutoCodeBrokerError:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        except (sqlite3.Error, RuntimeError, ValueError):
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise AutoCodeBrokerError(
                "tt_auto_code_freeze_failed", "自动发布四位码冻结失败", 503
            ) from None
        finally:
            conn.close()
        code = str(result.get("code") or "")
        if not _CODE_RE.fullmatch(code):
            raise AutoCodeBrokerError(
                "tt_auto_code_route_invalid", "自动发布四位码记录无效", 500
            )
        return {"task_id": int(task_id), "code": code, "state": str(result["state"])}

    def set_state(
        self,
        task_id: Any,
        *,
        state: Any,
        updated_at: Any,
    ) -> Dict[str, Any]:
        queue_id = synthetic_queue_id(task_id)
        target = str(state or "").strip()
        if target not in ROUTE_STATES:
            raise AutoCodeBrokerError(
                "tt_auto_code_state_invalid", "自动发布四位码状态无效"
            )
        timestamp = _utc_iso(updated_at, "自动发布四位码更新时间")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tt_post_code_route WHERE queue_id=?", (queue_id,)
            ).fetchone()
            if row is None:
                raise AutoCodeBrokerError(
                    "tt_auto_code_route_not_found", "自动发布四位码路由不存在", 404
                )
            current = str(row["state"] or "")
            if current == "published" and target != "published":
                raise AutoCodeBrokerError(
                    "tt_auto_code_state_conflict", "已发布四位码路由不能回退", 409
                )
            published_at = str(row["published_at"] or "")
            if target == "published" and not published_at:
                published_at = timestamp
            conn.execute(
                "UPDATE tt_post_code_route SET state=?,published_at=?,updated_at=? WHERE queue_id=?",
                (target, published_at, timestamp, queue_id),
            )
            result = conn.execute(
                "SELECT code,state,published_at FROM tt_post_code_route WHERE queue_id=?",
                (queue_id,),
            ).fetchone()
            conn.execute("COMMIT")
        except AutoCodeBrokerError:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise AutoCodeBrokerError(
                "tt_auto_code_state_update_failed", "自动发布四位码状态更新失败", 503
            ) from None
        finally:
            conn.close()
        assert result is not None
        return {
            "task_id": int(task_id),
            "code": str(result["code"]),
            "state": str(result["state"]),
            "published_at": str(result["published_at"] or ""),
        }


class AutoCodeBrokerHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True

    def __init__(
        self,
        address: Tuple[str, int],
        store: LegacyCodeRouteStore,
        internal_token: str,
    ):
        host, port = address
        if host != DEFAULT_HOST or int(port) != DEFAULT_PORT:
            raise AutoCodeBrokerError(
                "tt_auto_code_listen_invalid", "四位码服务只能监听127.0.0.1:18832", 500
            )
        if not valid_internal_bearer(internal_token):
            raise AutoCodeBrokerError(
                "tt_auto_code_bearer_invalid", "四位码服务内部凭据无效", 500
            )
        self.route_store = store
        self.internal_token = str(internal_token)
        super().__init__(address, AutoCodeBrokerHandler)


class AutoCodeBrokerHandler(BaseHTTPRequestHandler):
    server_version = "TTAutoCodeBroker/1"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(
            dict(payload), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = str(self.headers.get("X-Internal-Token") or "")
        expected = self.server.internal_token
        return bool(supplied) and secrets.compare_digest(supplied, expected)

    def _body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or "-1")
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            raise AutoCodeBrokerError("invalid_request", "请求体无效", 413)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, ValueError):
            raise AutoCodeBrokerError("invalid_request", "请求体不是有效JSON") from None
        if not isinstance(payload, dict):
            raise AutoCodeBrokerError("invalid_request", "请求体必须是对象")
        return payload

    def _dispatch(self) -> Mapping[str, Any]:
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            raise AutoCodeBrokerError("invalid_request", "请求路径无效")
        if self.command == "GET" and parsed.path == "/health":
            self.server.route_store.validate_schema()
            return {"ok": True}
        if not self._authorized():
            raise PermissionError
        if self.command == "POST" and parsed.path == "/internal/tt-auto-code-routes/freeze":
            payload = self._body()
            if set(payload) != {"task_id", "content_id", "long_url", "created_at"}:
                raise AutoCodeBrokerError("invalid_request", "四位码冻结参数无效")
            return {
                "ok": True,
                "route": self.server.route_store.freeze(
                    payload["task_id"],
                    content_id=payload["content_id"],
                    long_url=payload["long_url"],
                    created_at=payload["created_at"],
                ),
            }
        state_match = re.fullmatch(
            r"/internal/tt-auto-code-routes/([1-9][0-9]*)/state", parsed.path
        )
        if self.command == "POST" and state_match:
            payload = self._body()
            if set(payload) != {"state", "updated_at"}:
                raise AutoCodeBrokerError("invalid_request", "四位码状态参数无效")
            return {
                "ok": True,
                "route": self.server.route_store.set_state(
                    state_match.group(1),
                    state=payload["state"],
                    updated_at=payload["updated_at"],
                ),
            }
        raise AutoCodeBrokerError("not_found", "接口不存在", 404)

    def _handle(self) -> None:
        try:
            self._json(200, self._dispatch())
        except PermissionError:
            self._json(403, {"ok": False, "error": "forbidden", "message": "无权限"})
        except AutoCodeBrokerError as exc:
            self._json(
                exc.status,
                {"ok": False, "error": exc.code, "message": str(exc)},
            )
        except Exception:
            self._json(
                500,
                {
                    "ok": False,
                    "error": "tt_auto_code_internal_error",
                    "message": "自动发布四位码服务异常",
                },
            )

    do_GET = _handle
    do_POST = _handle


def build_server_from_env(
    environ: Optional[Mapping[str, str]] = None,
) -> AutoCodeBrokerHTTPServer:
    source = os.environ if environ is None else environ
    token = str(source.get("TT_AUTO_POST_INTERNAL_TOKEN", "") or "")
    root = Path(
        str(
            source.get(
                "TT_AUTO_POST_LEGACY_STATE_ROOT",
                "/mnt/data-disk/tt-post-publisher",
            )
        ).strip()
    ).expanduser()
    legacy_path = Path(
        str(source.get("TT_AUTO_POST_LEGACY_DB_PATH", "") or "").strip()
    ).expanduser()
    route_path = Path(
        str(source.get("TT_AUTO_CODE_ROUTE_DB_PATH", "") or "").strip()
    ).expanduser()
    if not root.is_absolute() or not legacy_path.is_absolute() or not route_path.is_absolute():
        raise AutoCodeBrokerError(
            "tt_auto_code_db_path_invalid", "四位码数据库路径无效", 500
        )
    try:
        resolved_root = root.resolve(strict=False)
        resolved_legacy = legacy_path.resolve(strict=False)
        resolved_route = route_path.resolve(strict=False)
        resolved_route.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        raise AutoCodeBrokerError(
            "tt_auto_code_db_path_invalid", "四位码数据库路径无效", 500
        ) from None
    if resolved_route != resolved_legacy:
        raise AutoCodeBrokerError(
            "tt_auto_code_db_path_mismatch", "四位码数据库必须显式绑定旧路由账本", 500
        )
    try:
        port = int(source.get("TT_AUTO_CODE_ROUTE_SERVICE_PORT", str(DEFAULT_PORT)))
    except (TypeError, ValueError, OverflowError):
        port = 0
    host = str(source.get("TT_AUTO_CODE_ROUTE_SERVICE_HOST", DEFAULT_HOST) or "")
    store = LegacyCodeRouteStore(resolved_route)
    store.validate_schema()
    return AutoCodeBrokerHTTPServer((host, port), store, token)


def serve(environ: Optional[Mapping[str, str]] = None) -> None:
    server = build_server_from_env(environ)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


__all__ = [
    "AUTO_QUEUE_ID_NAMESPACE",
    "AutoCodeBrokerError",
    "AutoCodeBrokerHTTPServer",
    "LegacyCodeRouteStore",
    "build_server_from_env",
    "serve",
    "synthetic_queue_id",
]
