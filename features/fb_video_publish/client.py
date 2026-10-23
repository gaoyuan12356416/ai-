"""Bounded, loopback-only client for the authenticated main API."""
from __future__ import annotations

import json
import os
import re
from typing import Mapping
from urllib.parse import parse_qs

import requests

from features.fb_auto_posts.client import contains_sensitive_key
from features.fb_auto_posts.validation import valid_internal_bearer

PREFIX = "/api/fb-video-publish"
MAX_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class GatewayError(RuntimeError):
    def __init__(self, code, message, status=503, details=None, accepted=False):
        self.code, self.status, self.details, self.accepted = code, status, details, accepted
        super().__init__(message)


def route(method, path):
    """Return authorized navigation keys and whether fresh mapping is needed."""
    suffix = path[len(PREFIX):] if path.startswith(PREFIX + "/") else ""
    if method == "GET":
        if re.fullmatch(r"/excel/(?:operations/[a-f0-9-]{32,36}|batches/[a-f0-9]{32})", suffix):
            return ("fbPageManagement",), False
        if suffix == "/pages/available":
            return ("fbPageManagement",), True
        if suffix == "/pages":
            return ("fbPageManagement", "fbVideoPublish"), True
        if suffix == "/materials":
            return ("fbVideoPublish", "fbPageManagement"), False
        if re.fullmatch(r"/pages/[1-9][0-9]{0,40}/pool(?:/operations/[a-fA-F0-9-]{32,36})?", suffix):
            return ("fbPageManagement",), True
        if suffix == "/languages":
            return ("fbPageManagement", "fbVideoPublish"), False
        if suffix == "/draft":
            return ("fbVideoPublish",), False
        if suffix == "/batches" or re.fullmatch(r"/batches/[a-f0-9]{32}", suffix):
            return ("fbVideoPublishRecords", "fbVideoPublish"), False
        if suffix == "/tasks" or re.fullmatch(r"/tasks/[a-f0-9]{32}", suffix):
            return ("fbVideoPublishRecords", "fbVideoPublish"), False
        if re.fullmatch(r"/operations/[a-fA-F0-9-]{32,36}", suffix):
            return ("fbVideoPublish", "fbVideoPublishRecords"), False
    if method == "POST":
        if suffix in ("/excel/preview", "/excel/batches"):
            return ("fbPageManagement",), True
        if re.fullmatch(r"/pages/[1-9][0-9]{0,40}/pool/(?:settings|enable|pause|items(?:/preview|/resolve)?|remove|move)", suffix):
            return ("fbPageManagement",), True
        if suffix == "/pages" or re.fullmatch(r"/pages/[1-9][0-9]{0,40}(?:/delete)?", suffix):
            return ("fbPageManagement",), True
        if suffix in ("/preflight", "/batches"):
            return ("fbVideoPublish",), True
        if suffix == "/draft":
            return ("fbVideoPublish",), False
        if re.fullmatch(r"/tasks/[a-f0-9]{32}/cancel", suffix):
            return ("fbVideoPublishRecords", "fbVideoPublish"), False
    raise GatewayError("not_found", "接口不存在", 404)


def query_params(raw):
    try:
        query = parse_qs(raw, keep_blank_values=True, max_num_fields=8)
    except ValueError:
        raise GatewayError("invalid_query", "查询参数无效", 400) from None
    if any(len(values) != 1 or len(values[0]) > (512 if key == "cursor" else 300) for key, values in query.items()):
        raise GatewayError("invalid_query", "查询参数无效", 400)
    return {key: values[0] for key, values in query.items()}


def request(method, path, *, actor, payload=None, query=None, environ=None):
    route(method, path)
    env = os.environ if environ is None else environ
    bearer = str(env.get("FB_AUTO_POST_INTERNAL_TOKEN") or "")
    base = str(env.get("FB_AUTO_POST_ADMIN_SERVICE_URL") or "http://127.0.0.1:18835").rstrip("/")
    if base != "http://127.0.0.1:18835" or not valid_internal_bearer(bearer):
        raise GatewayError("service_not_configured", "Facebook 发布服务尚未配置", 503)
    if not isinstance(actor, Mapping) or contains_sensitive_key(actor):
        raise GatewayError("invalid_actor", "操作人无效", 403)
    if payload is not None and (not isinstance(payload, Mapping) or contains_sensitive_key(payload)):
        raise GatewayError("invalid_request", "请求包含无效字段", 400)
    headers = {
        "Authorization": "Bearer " + bearer,
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "X-FB-Video-Actor": json.dumps(dict(actor), ensure_ascii=True, separators=(",", ":")),
    }
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.request(method, base + path, headers=headers, params=query or None,
                                 json=payload, timeout=(3, 90), allow_redirects=False, stream=True) as response:
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise GatewayError("response_too_large", "服务响应超过上限", 502, accepted=None)
                    chunks.append(chunk)
                data = json.loads(b"".join(chunks).decode("utf-8"))
                if not isinstance(data, dict) or contains_sensitive_key(data):
                    raise GatewayError("invalid_response", "服务响应无效", 502, accepted=None)
                if not 200 <= response.status_code < 300:
                    code = data.get("code") or data.get("error") or "service_error"
                    if not re.fullmatch(r"[a-z0-9_]{1,80}", str(code)):
                        code = "service_error"
                    message = str(data.get("message") or "请求失败")[:500]
                    raise GatewayError(code, message, response.status_code, data.get("details"),
                                       False if data.get("accepted") is False else None)
                return data
    except GatewayError:
        raise
    except (requests.RequestException, ValueError, UnicodeError):
        raise GatewayError("service_unavailable", "请求结果暂未确认，请保留当前操作并查询原操作编号", 503, accepted=None) from None
