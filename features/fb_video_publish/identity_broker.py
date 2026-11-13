"""Current account verification in the main API; never persist a system actor."""
from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
from contextlib import closing

from .client import GatewayError

IDENTITY_PATH = "/internal/fb-video-publish/identity"
ACTOR_KEYS = {"tenant_key", "user_id", "user_name", "is_admin", "business_owner_id", "sub_user_id"}


def identity_keys(tenant_key, user_id):
    for value in (tenant_key, user_id):
        if (not isinstance(value, str) or not 1 <= len(value) <= 200
                or value != value.strip() or any(ord(ch) < 32 for ch in value)):
            raise GatewayError("invalid_identity", "账号标识无效", 400)
    return tenant_key, user_id


def resolve_identity(app, tenant_key, user_id, *, navigation_key="fbPageManagement"):
    """Re-read active tenant membership, permissions, navigation and attribution."""
    tenant_key, user_id = identity_keys(tenant_key, user_id)
    if navigation_key not in {"fbPageManagement", "fbPostMaterialPool"}:
        raise GatewayError("invalid_identity", "发布入口无效", 400)
    try:
        with closing(sqlite3.connect("file:" + str(app["JOB_DB_PATH"]) + "?mode=ro", uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT tenant_key,user_id,name,role,status,permissions_json FROM drama_admin_user "
                "WHERE tenant_key=? AND user_id=? LIMIT 2", (tenant_key, user_id),
            ).fetchall()
        if len(rows) != 1 or rows[0]["status"] != "active" or rows[0]["role"] not in {"user", "admin"}:
            raise GatewayError("identity_inactive", "原发布人账号已失效，请由有效账号重新启用计划", 403)
        account = dict(rows[0])
        account["permissions"] = app["normalize_user_permissions"](account.pop("permissions_json"), account["role"])
        if not app["has_module_permission"](account, "fb_video_publish"):
            raise GatewayError("identity_permission_denied", "原发布人已失去 Facebook 视频发布权限", 403)
        nav = app["load_navigation_config"]()
        if not app["navigation_item_access"](account, navigation_key, nav).get("allowed"):
            raise GatewayError("identity_permission_denied", "原发布人的发布入口已不可用", 403)
        actor = {"tenant_key": tenant_key, "user_id": user_id, "user_name": str(account["name"] or "")[:200],
                 "is_admin": account["role"] == "admin", "business_owner_id": "", "sub_user_id": ""}
        # Import here to avoid the public gateway/broker import cycle.
        from .gateway import attribution_identity
        try:
            actor["business_owner_id"] = actor["sub_user_id"] = attribution_identity(app, actor)
        except GatewayError as exc:
            if exc.status < 500:
                raise GatewayError("identity_attribution_invalid", "原发布人邮箱归因映射已失效，请修正后重新启用", 403) from None
            raise
        return actor
    except GatewayError:
        raise
    except Exception:
        raise GatewayError("identity_unavailable", "发布人身份验证暂不可用", 503) from None


def enrich_creator_names(app, tenant_key, result):
    """Resolve only missing historical snapshots, never substitute an updater."""
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list):
        return result
    missing = sorted({str(item.get("created_by") or "") for item in items if isinstance(item, dict)
                      and not str(item.get("created_by_name") or "").strip() and item.get("created_by")})
    names = {}
    try:
        if missing:
            with closing(sqlite3.connect("file:" + str(app["JOB_DB_PATH"]) + "?mode=ro", uri=True, timeout=5)) as conn:
                # SQLite's portable binding limit also bounds each name lookup.
                for start in range(0, len(missing), 200):
                    ids = missing[start:start + 200]
                    rows = conn.execute(
                        "SELECT user_id,name FROM drama_admin_user WHERE tenant_key=? AND user_id IN ("
                        + ",".join("?" for _ in ids) + ")", [tenant_key, *ids]).fetchall()
                    names.update({str(uid): str(name).strip()[:200] for uid, name in rows if str(name or "").strip()})
    except (sqlite3.Error, KeyError):
        # Names are display enrichment; durable creator IDs remain authoritative.
        names = {}
    return {**result, "items": [
        {**item, "created_by_name": (item.get("created_by_name") if str(item.get("created_by_name") or "").strip() else "")
         or names.get(str(item.get("created_by") or ""))
         or str(item.get("created_by") or "")} if isinstance(item, dict) else item for item in items]}


def dispatch(handler, app):
    respond = lambda status, payload: app["json_response"](handler, status, payload, no_store=True)
    expected = str(os.environ.get("FB_AUTO_POST_INTERNAL_TOKEN", ""))
    headers = handler.headers
    # Empty forwarding headers are also rejected: this endpoint has no proxy use.
    forwarded = any(key.lower() == "forwarded" or key.lower().startswith("x-forwarded-")
                    or key.lower() == "x-real-ip" for key in headers.keys())
    if (handler.command != "POST" or str(handler.client_address[0]) not in {"127.0.0.1", "::1"}
            or forwarded or not re.fullmatch(r"[A-Za-z0-9._~-]{32,512}", expected)
            or len(headers.get_all("Authorization", [])) != 1
            or not hmac.compare_digest(str(headers.get("Authorization", "")), "Bearer " + expected)):
        handler.close_connection = True
        respond(403, {"ok": False, "code": "forbidden", "message": "Internal route", "accepted": False})
        return
    try:
        lengths = headers.get_all("Content-Length", [])
        if (headers.get("Transfer-Encoding") is not None or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,5}", lengths[0]) or not 2 <= int(lengths[0]) <= 4096
                or headers.get("Content-Type", "").split(";", 1)[0] != "application/json"):
            raise GatewayError("invalid_request", "内部身份请求无效", 400)
        raw = handler.rfile.read(int(lengths[0]))
        if len(raw) != int(lengths[0]):
            raise ValueError()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) not in ({"tenant_key", "user_id"}, {"tenant_key", "user_id", "navigation_key"}):
            raise ValueError()
        actor = resolve_identity(app, payload["tenant_key"], payload["user_id"], navigation_key=payload.get("navigation_key", "fbPageManagement"))
        respond(200, {"ok": True, "actor": actor})
    except GatewayError as exc:
        handler.close_connection = True
        respond(exc.status, {"ok": False, "code": exc.code, "message": str(exc), "accepted": False})
    except (ValueError, UnicodeError, TypeError):
        handler.close_connection = True
        respond(400, {"ok": False, "code": "invalid_request", "message": "内部身份请求无效", "accepted": False})
    except Exception:
        respond(503, {"ok": False, "code": "identity_unavailable", "message": "发布人身份验证暂不可用"})
