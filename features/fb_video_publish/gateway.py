"""Cookie, navigation, CSRF and trusted identity boundary in the main API."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
from contextlib import closing
from urllib.parse import urlsplit

from .client import GatewayError, MAX_BYTES, query_params, request, route

MAPPING_LIMIT = threading.BoundedSemaphore(2)
UNTRUSTED_IDENTITY = {
    "actor", "_actor", "creator", "tenant_key", "user_id", "open_id", "role",
    "is_admin", "email", "business_owner_id", "sub_user_id", "owner_user_id",
    "attribution", "attribution_context",
}


def attribution_identity(app, actor):
    """Use current tenant account email and the unique ACTIVE replica mapping."""
    with closing(sqlite3.connect("file:" + str(app["JOB_DB_PATH"]) + "?mode=ro", uri=True, timeout=5)) as conn:
        users = conn.execute(
            "SELECT email FROM drama_admin_user WHERE tenant_key=? AND user_id=? LIMIT 2",
            (actor["tenant_key"], actor["user_id"]),
        ).fetchall()
    if len(users) != 1 or not str(users[0][0] or "").strip():
        raise GatewayError("attribution_email_missing", "请先完善发布人的唯一邮箱信息", 409)
    email = str(users[0][0]).strip().lower()
    schema = str(app["ADMIN_MAPPING_MYSQL_DATABASE"])
    if (str(app["ADMIN_MAPPING_MYSQL_HOST"]) != "101.32.56.53"
            or str(app["ADMIN_MAPPING_MYSQL_PORT"]) != "63350"
            or not re.fullmatch(r"[A-Za-z0-9_]+", schema)):
        raise GatewayError("mapping_unavailable", "用户映射只读数据库配置无效", 503)
    if not MAPPING_LIMIT.acquire(timeout=2):
        raise GatewayError("mapping_busy", "用户映射查询繁忙，请稍后重试", 503)
    try:
        sql = ("SELECT DISTINCT CAST(sub_user_id AS CHAR) FROM `" + schema + "`.admin_user_group "
               "WHERE status=0 AND sub_user_id IS NOT NULL "
               "AND BINARY LOWER(TRIM(email))=BINARY CONVERT(0x" + email.encode("utf-8").hex()
               + " USING utf8mb4) LIMIT 2")
        env = os.environ.copy()
        env.pop("SQL_GATE_BYPASS", None)
        env["MYSQL_PWD"] = str(app["ADMIN_MAPPING_MYSQL_PASSWORD"])
        command = ["/usr/bin/mysql", "-h", "101.32.56.53", "-P", "63350", "-u",
                   str(app["ADMIN_MAPPING_MYSQL_USER"]), "-N", "-B",
                   "--default-character-set=utf8mb4", "--connect-timeout=3", "-e",
                   "SET SESSION MAX_EXECUTION_TIME=8000; START TRANSACTION READ ONLY; "
                   "SELECT @@read_only; " + sql + "; COMMIT;"]
        result = subprocess.run(command, env=env, capture_output=True, text=True,
                                timeout=25, check=True, encoding="utf-8")
        rows = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not rows or rows.pop(0) != "1":
            raise GatewayError("mapping_unavailable", "用户映射必须使用只读副本", 503)
        if len(rows) != 1 or not re.fullmatch(r"[1-9][0-9]{0,30}", rows[0]):
            raise GatewayError("attribution_mapping_invalid", "发布人邮箱未唯一对应有效的负责人编号", 409)
        return rows[0]
    except GatewayError:
        raise
    except Exception:
        raise GatewayError("mapping_unavailable", "用户邮箱映射查询失败，请稍后重试", 503) from None
    finally:
        MAPPING_LIMIT.release()


def read_json(handler):
    lengths = handler.headers.get_all("Content-Length", [])
    if (handler.headers.get("Transfer-Encoding") or len(lengths) != 1
            or not re.fullmatch(r"[0-9]{1,9}", lengths[0])):
        raise GatewayError("valid_content_length_required", "请求长度无效", 411)
    length = int(lengths[0])
    if length > MAX_BYTES:
        raise GatewayError("request_too_large", "请求不能超过 4 MiB", 413)
    if length < 2:
        raise GatewayError("invalid_json", "请求必须为 JSON 对象", 400)
    source = str(handler.headers.get("Origin") or handler.headers.get("Referer") or "")
    try:
        parsed = urlsplit(source)
        valid_source = parsed.scheme in ("https", "http") and bool(parsed.netloc) and not parsed.username
    except ValueError:
        valid_source = False
    if not valid_source or handler.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        raise GatewayError("same_origin_required", "请从本站页面发起操作", 403)
    if not handler._require_same_origin_json():
        handler.close_connection = True
        return None
    try:
        raw = handler.rfile.read(length)
        if len(raw) != length:
            raise ValueError()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise GatewayError("invalid_json", "请求必须为 JSON 对象", 400) from None
    for key in UNTRUSTED_IDENTITY:
        payload.pop(key, None)
    return payload


def dispatch(handler, parsed, app):
    send = lambda status, data: app["json_response"](handler, status, data, no_store=True)
    try:
        nav_keys, needs_mapping = route(handler.command, parsed.path)
        session = app["load_session"](handler._cookies().get(app["SESSION_COOKIE_NAME"], ""))
        if (not session or session.get("auth_type") == "api_token"
                or not str(session.get("tenant_key") or "").strip()
                or not str(session.get("user_id") or "").strip()):
            raise GatewayError("cookie_auth_required", "请先使用飞书登录", 401)
        if not app["has_module_permission"](session, "fb_video_publish"):
            raise GatewayError("permission_denied", "尚未开通 Facebook 视频批量发布权限", 403)
        try:
            nav = app["load_navigation_config"]()
            allowed = any(app["navigation_item_access"](session, key, nav).get("allowed") for key in nav_keys)
        except (OSError, TypeError, ValueError):
            raise GatewayError("navigation_config_unavailable", "导航配置暂不可用", 503) from None
        if not allowed:
            raise GatewayError("navigation_item_unavailable", "当前入口不可用", 403)
        actor = {key: str(session.get(key) or "") for key in ("tenant_key", "user_id")}
        actor.update(user_name=str(session.get("name") or "")[:200],
                     is_admin=session.get("role") == "admin", business_owner_id="", sub_user_id="")
        payload = read_json(handler) if handler.command == "POST" else None
        if handler.command == "POST" and payload is None:
            return
        if handler.command == "POST" and re.fullmatch(r"/api/fb-video-publish/pages/[1-9][0-9]{0,40}/pool/enable", parsed.path):
            from .identity_broker import resolve_identity
            actor = resolve_identity(app, actor["tenant_key"], actor["user_id"])
        elif needs_mapping and not (
            actor["is_admin"] and handler.command == "GET"
            and (
                parsed.path in {"/api/fb-video-publish/pages", "/api/fb-video-publish/pages/available"}
                or re.fullmatch(r"/api/fb-video-publish/pages/[1-9][0-9]{0,30}/pool", parsed.path)
            )
        ):
            # Admin Page/listed-pool reads have no owner filter or attribution fields.
            # All writes and ordinary-user reads still resolve the current mapping.
            actor["business_owner_id"] = actor["sub_user_id"] = attribution_identity(app, actor)
        result = request(handler.command, parsed.path, actor=actor, payload=payload, query=query_params(parsed.query))
        if handler.command == "GET" and parsed.path == "/api/fb-video-publish/pages":
            from .identity_broker import enrich_creator_names
            result = enrich_creator_names(app, actor["tenant_key"], result)
        send(200, result)
    except GatewayError as exc:
        handler.close_connection = True
        data = {"ok": False, "error": exc.code, "code": exc.code, "message": str(exc)}
        if exc.details is not None:
            data["details"] = exc.details
        if exc.accepted is False:
            data["accepted"] = False
        send(exc.status, data)
    except Exception:
        handler.close_connection = True
        send(503, {"ok": False, "error": "service_unavailable", "message": "服务暂不可用，请稍后重试"})
