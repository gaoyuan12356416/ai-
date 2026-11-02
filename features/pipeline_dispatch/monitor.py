"""Cookie-authenticated monitor gateway; publisher databases stay private."""
from __future__ import annotations

import http.client
import json
import logging
import os
import re
from urllib.parse import parse_qs, urlsplit

from .monitor_contract import CHANNELS, CHANNEL_MODULES, STAGES, STATUSES, WINDOWS

PREFIX = "/api/pipeline-monitor/"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def ensure_navigation(config):
    """Make the authenticated home available in saved navigation as well."""
    if not isinstance(config, list):
        return config
    import copy
    result = copy.deepcopy(config)
    for group in result:
        if isinstance(group, dict) and any(isinstance(item, dict) and item.get("key") == "pipelineMonitor"
                                          for item in group.get("items", [])):
            return result
    result.insert(0, {"key": "pipeline_overview", "label": "运行监测", "order": 1,
                      "items": [{"key": "pipelineMonitor", "label": "制作与发布监测", "order": 1,
                                 "kind": "page", "href": "/pipeline-monitor.html", "enabled": True,
                                 "description": "任务进度、服务器容量与制作发布效率"}]})
    return result


class MonitorError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def coordinator_request(method, path, payload, *, environ=None):
    env = os.environ if environ is None else environ
    target = urlsplit(env.get("PIPELINE_DISPATCH_INTERNAL_URL", "http://127.0.0.1:18846"))
    try:
        valid = (target.scheme == "http" and target.hostname in ("127.0.0.1", "::1", "localhost")
                 and target.port == 18846 and target.path in ("", "/")
                 and not target.username and not target.password and not target.query and not target.fragment)
    except ValueError:
        valid = False
    token = str(env.get("PIPELINE_DISPATCH_INTERNAL_TOKEN", "") or "")
    if not valid or not token or any(char in token for char in "\r\n"):
        raise MonitorError("monitor_unavailable", "监测服务尚未配置或暂不可用", 503)
    if (method, path) not in {
        ("POST", "/internal/pipeline-monitor/query"),
        ("PUT", "/internal/pipeline-monitor/capacity"),
        ("POST", "/internal/pipeline-monitor/enroll"),
    }:
        raise MonitorError("invalid_request", "监测请求无效")
    connection = http.client.HTTPConnection(target.hostname, 18846, timeout=8)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        connection.request(method, path, body=body, headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json; charset=utf-8", "Accept": "application/json",
            "Connection": "close",
        })
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise MonitorError("monitor_unavailable", "监测响应过大，请缩小查询范围", 503)
        result = json.loads(data.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("invalid response")
        if not 200 <= response.status < 300:
            if response.status == 409:
                raise MonitorError("capacity_version_conflict", "容量已被更新，请刷新后再保存", 409)
            if response.status == 400:
                raise MonitorError("invalid_request", "容量或查询参数超出允许范围", 400)
            raise MonitorError("monitor_unavailable", "监测服务暂不可用，保留上次数据", 503)
        return result
    except MonitorError:
        raise
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        raise MonitorError("monitor_unavailable", "监测服务暂不可用，保留上次数据", 503) from None
    finally:
        connection.close()


def trusted_actor(session, app):
    """Never forward a caller-supplied actor, role or system capability."""
    admin = session.get("role") == "admin"
    permission = app.get("has_module_permission")
    modules = sorted({module for values in CHANNEL_MODULES.values() for module in values
                      if admin or (callable(permission) and permission(session, module))})
    actor = {"tenant_key": str(session.get("tenant_key") or ""),
            "user_id": str(session.get("user_id") or ""), "is_admin": admin,
            "allowed_channels": [c for c in CHANNELS if any(m in modules for m in CHANNEL_MODULES[c])],
            "allowed_modules": modules}
    # FB Page pools use the existing business-owner mapping; they do not use
    # the Feishu account ID stored by manual Facebook tasks.
    resolver = app.get("fb_auto_post_actor_scope")
    if not admin and "fb_page_posts" in modules and callable(resolver):
        try:
            actor["fb_owner_user_id"] = str(resolver(session).get("owner_user_id") or "")
        except Exception:
            actor["fb_owner_user_id"] = ""
    return actor


def filters_from_query(query):
    parsed = parse_qs(query, keep_blank_values=True, max_num_fields=12)
    allowed = {"window", "channel", "stage", "status", "host_id", "cursor", "limit"}
    if set(parsed) - allowed or any(len(values) != 1 for values in parsed.values()):
        raise MonitorError("invalid_request", "查询参数无效")
    result = {key: values[0] for key, values in parsed.items() if values[0] != ""}
    result.setdefault("window", "today")
    for key, choices in (("window", WINDOWS), ("channel", CHANNELS), ("stage", STAGES),
                         ("status", STATUSES)):
        if key in result and result[key] not in choices:
            raise MonitorError("invalid_request", "查询参数无效")
    if "host_id" in result and not valid_host_id(result["host_id"]):
        raise MonitorError("invalid_request", "服务器参数无效")
    try:
        limit = int(result.get("limit", "50"))
    except (ValueError, TypeError):
        raise MonitorError("invalid_request", "分页参数无效") from None
    if not 1 <= limit <= 100 or len(result.get("cursor", "")) > 512:
        raise MonitorError("invalid_request", "分页参数无效")
    result["limit"] = limit
    return result


def valid_host_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", value))


def capacity_payload(raw, actor):
    allowed = {"expected_version", "host_id", "stage", "max_workers", "global_publish_limit", "channel_limits"}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise MonitorError("invalid_request", "容量参数无效")
    if type(raw.get("expected_version")) is not int or raw["expected_version"] < 0:
        raise MonitorError("invalid_request", "请先刷新当前容量版本")
    result = dict(raw)
    if any(k in raw for k in ("host_id", "stage", "max_workers")):
        if not valid_host_id(raw.get("host_id")) or raw.get("stage") not in STAGES:
            raise MonitorError("invalid_request", "服务器或执行阶段无效")
        if type(raw.get("max_workers")) is not int or not 0 <= raw["max_workers"] <= 64:
            raise MonitorError("invalid_request", "进程数无效")
    if "global_publish_limit" in raw:
        if type(raw["global_publish_limit"]) is not int or not 1 <= raw["global_publish_limit"] <= 64:
            raise MonitorError("invalid_request", "发布总并发无效")
    if "channel_limits" in raw:
        limits = raw["channel_limits"]
        if not isinstance(limits, dict) or not limits or set(limits) - set(CHANNELS):
            raise MonitorError("invalid_request", "渠道并发无效")
        if any(type(v) is not int or not 1 <= v <= 64 for v in limits.values()):
            raise MonitorError("invalid_request", "渠道并发无效")
    if set(raw) == {"expected_version"}:
        raise MonitorError("invalid_request", "请填写需要更新的容量")
    result["actor"] = actor
    return result


def enrollment_payload(raw, actor):
    if not isinstance(raw, dict) or set(raw) - {"label"}:
        raise MonitorError("invalid_request", "服务器登记参数无效")
    label = raw.get("label")
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 120:
        raise MonitorError("invalid_request", "请填写服务器名称（最多120字）")
    return {"actor": actor, "label": label.strip()}


def dispatch(handler, parsed, app):
    reply = lambda status, value: app["json_response"](handler, status, value, no_store=True)
    if not handler._require_auth():
        return
    session = handler._session() or {}
    if not session:
        return reply(401, {"error": "authentication_required", "message": "请先登录"})
    if session.get("auth_type") == "api_token":
        return reply(403, {"error": "cookie_auth_required", "message": "请通过后台登录查看监测"})
    actor = trusted_actor(session, app)
    if not actor["is_admin"] and (not actor["user_id"] or not actor["tenant_key"]):
        return reply(403, {"error": "permission_denied", "message": "登录身份不完整"})
    view = parsed.path.removeprefix(PREFIX)
    try:
        if handler.command == "GET" and view in ("summary", "workers", "tasks"):
            filters = filters_from_query(parsed.query)
            if filters.get("channel") and filters["channel"] not in actor["allowed_channels"]:
                return reply(403, {"error": "permission_denied", "message": "没有此渠道的访问权限"})
            result = coordinator_request("POST", "/internal/pipeline-monitor/query",
                                         {"view": view, "actor": actor, "filters": filters})
            return reply(200, result)
        if handler.command == "PUT" and view == "capacity":
            if not actor["is_admin"]:
                return reply(403, {"error": "admin_required", "message": "仅管理员可以调整容量"})
            if not handler._require_same_origin_json():
                return
            if parsed.query:
                raise MonitorError("invalid_request", "容量接口不接受查询参数")
            payload = capacity_payload(handler._read_json(), actor)
            result = coordinator_request("PUT", "/internal/pipeline-monitor/capacity", payload)
            audit = app.get("append_audit_log")
            if callable(audit):
                try:
                    audit(session, "update_pipeline_capacity", "pipeline_dispatch", "capacity",
                          {k: v for k, v in payload.items() if k != "actor"})
                except Exception:
                    logging.exception("pipeline capacity audit append failed after accepted update")
            return reply(200, result)
        if handler.command == "POST" and view == "nodes/enroll":
            if not actor["is_admin"]:
                return reply(403, {"error": "admin_required", "message": "仅管理员可以接入服务器"})
            if not handler._require_same_origin_json():
                return
            if parsed.query:
                raise MonitorError("invalid_request", "服务器登记接口不接受查询参数")
            payload = enrollment_payload(handler._read_json(), actor)
            result = coordinator_request("POST", "/internal/pipeline-monitor/enroll", payload)
            # The enrollment secret only travels in this no-store response.
            return reply(200, result)
        return reply(404, {"error": "not_found"})
    except MonitorError as exc:
        return reply(exc.status, {"error": exc.code, "message": str(exc)})
    except (ValueError, TypeError):
        return reply(400, {"error": "invalid_request", "message": "请求参数无效"})
