"""Loopback client used by the AI backend to call the X OAuth sidecar."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


SAFE_ERROR_CODES = {
    "invalid_request",
    "x_account_disabled",
    "x_account_not_publishable",
    "x_account_publish_not_approved",
    "x_account_owned_by_other",
    "x_account_not_found",
    "x_admin_required",
    "x_accounts_unavailable",
    "x_disconnect_failed",
    "x_disconnect_pending",
    "x_identity_mismatch",
    "x_internal_auth_failed",
    "x_oauth_not_configured",
    "x_post_account_day_already_reserved",
    "x_post_daily_candidate_shortage",
    "x_post_daily_run_exists",
    "x_post_idempotency_conflict",
    "x_post_material_already_used",
    "x_post_pool_item_not_found",
    "x_post_pool_item_occupied",
    "x_post_pool_item_published",
    "x_post_pool_item_unavailable",
    "x_post_pool_material_already_exists",
    "x_post_pool_material_already_used",
    "x_post_pool_required",
    "x_post_queue_not_found",
    "x_post_rate_limited",
    "x_post_retry_requires_review",
    "x_post_run_not_found",
    "x_post_schedule_collision",
    "x_post_schedule_config_changed",
    "x_post_schedule_not_found",
    "x_post_schedule_run_exists",
    "x_post_schedule_run_not_found",
    "x_post_schedule_version_conflict",
    "x_post_drama_already_used",
    "x_post_drama_episode_already_used",
    "x_post_drama_pool_item_exists",
    "x_post_drama_pool_item_not_found",
    "x_post_drama_pool_item_occupied",
    "x_post_drama_pool_item_unavailable",
    "x_post_drama_sequence_conflict",
    "x_post_storage_conflict",
    "x_post_unknown_outcome",
    "x_posts_unavailable",
    "x_publish_unknown",
    "x_token_missing",
    "x_token_revoked",
    "x_upstream_error",
}


class XAccountsClientError(RuntimeError):
    def __init__(self, code="x_accounts_unavailable", message="X账号服务暂不可用", status_code=503):
        safe_code = str(code or "x_accounts_unavailable")
        if safe_code not in SAFE_ERROR_CODES:
            safe_code = "x_accounts_unavailable"
        super().__init__(str(message or "X账号服务暂不可用"))
        self.code = safe_code
        self.status_code = int(status_code or 503)


_BASE_URL = "http://127.0.0.1:8810"
_INTERNAL_TOKEN = ""
_TIMEOUT = 30


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirect(),
)


def configure_x_accounts_client(base_url, internal_token, timeout=30):
    global _BASE_URL, _INTERNAL_TOKEN, _TIMEOUT
    parsed = urllib.parse.urlsplit(str(base_url or "").rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Invalid X sidecar base URL")
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("X sidecar base URL must use loopback")
    _BASE_URL = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    _INTERNAL_TOKEN = str(internal_token or "")
    try:
        parsed_timeout = int(timeout)
    except (TypeError, ValueError):
        parsed_timeout = 30
    _TIMEOUT = max(1, min(parsed_timeout, 120))


def _request(path, method="GET", payload=None):
    if not _INTERNAL_TOKEN:
        raise XAccountsClientError("x_accounts_unavailable", "X账号服务内部鉴权未配置", 503)
    body = None
    headers = {
        "Authorization": "Bearer " + _INTERNAL_TOKEN,
        "Accept": "application/json",
    }
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(_BASE_URL + path, data=body, method=method, headers=headers)
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=_TIMEOUT) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise XAccountsClientError("x_accounts_unavailable", "X账号服务响应过大", 502)
            return json.loads(raw.decode("utf-8")) if raw else {}
    except XAccountsClientError:
        raise
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(64 * 1024)
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            data = {}
        finally:
            exc.close()
        code = str(data.get("error", "x_accounts_unavailable") or "x_accounts_unavailable")
        if code not in SAFE_ERROR_CODES:
            code = "x_accounts_unavailable"
        message = str(data.get("message", "") or "X账号服务请求失败")
        status = exc.code if exc.code in {400, 403, 404, 409, 429, 502, 503} else 503
        raise XAccountsClientError(code, message, status) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        raise XAccountsClientError("x_accounts_unavailable", "X账号服务暂不可用", 503) from None


def get_x_accounts_config():
    return _request("/internal/config")


def normalize_actor(actor):
    actor = actor or {}
    return {
        "user_id": str(actor.get("user_id", "") or "")[:255],
        "tenant_key": str(actor.get("tenant_key", "") or "")[:255],
        "name": str(actor.get("name", "") or "")[:255],
        "email": str(actor.get("email", "") or "")[:255],
        "role": str(actor.get("role", "user") or "user")[:32],
    }


def normalize_scope(scope):
    value = str(scope or "mine").strip().lower()
    if value not in {"mine", "all"}:
        raise XAccountsClientError("invalid_request", "X账号查询范围无效", 400)
    return value


def query_x_accounts(actor, scope="mine"):
    return _request(
        "/internal/accounts/query",
        method="POST",
        payload={"actor": normalize_actor(actor), "scope": normalize_scope(scope)},
    )


def start_x_authorization(actor):
    return _request("/internal/authorize", method="POST", payload={"actor": normalize_actor(actor)})


def verify_x_account(
    account_id,
    actor,
    scope="mine",
    *,
    only_refresh_required=False,
    preserve_transient_status=False,
):
    account_id = str(account_id or "")
    if not account_id.isdigit():
        raise XAccountsClientError("invalid_request", "X账号记录ID无效", 400)
    payload = {"actor": normalize_actor(actor), "scope": normalize_scope(scope)}
    if only_refresh_required:
        payload["only_refresh_required"] = True
    if preserve_transient_status:
        payload["preserve_transient_status"] = True
    return _request(
        "/internal/accounts/%s/verify" % account_id,
        method="POST",
        payload=payload,
    )


def set_x_account_publish_approval(account_id, approved, actor):
    account_id = str(account_id or "")
    if not account_id.isdigit():
        raise XAccountsClientError("invalid_request", "X账号记录ID无效", 400)
    if not isinstance(approved, bool):
        raise XAccountsClientError("invalid_request", "approved必须是布尔值", 400)
    return _request(
        "/internal/accounts/%s/publish-approval" % account_id,
        method="POST",
        payload={
            "actor": normalize_actor(actor),
            "scope": "all",
            "approved": approved,
        },
    )


def logout_x_account(account_id, actor):
    account_id = str(account_id or "")
    if not account_id.isdigit():
        raise XAccountsClientError("invalid_request", "X账号记录ID无效", 400)
    return _request(
        "/internal/accounts/%s/logout" % account_id,
        method="POST",
        payload={"actor": normalize_actor(actor)},
    )


def _post_query_payload(params):
    if not isinstance(params, dict):
        raise XAccountsClientError("invalid_request", "X发布日志查询参数必须是对象", 400)
    result = {
        "actor": normalize_actor(params.get("actor", {})),
        "scope": normalize_scope(params.get("scope", "all")),
    }
    for field in (
        "page", "page_size", "run_date", "source_date", "account_id", "status",
        "material_id", "unknown_outcome",
    ):
        if field in params and params[field] not in (None, ""):
            result[field] = params[field]
    return result


def query_x_post_logs(params):
    """Return the sidecar's redacted, paginated X Post queue/log view."""
    return _request(
        "/internal/posts/logs/query",
        method="POST",
        payload=_post_query_payload(params),
    )


def query_x_post_runs(params):
    """Return the sidecar's redacted, paginated daily-run view."""
    return _request(
        "/internal/posts/runs/query",
        method="POST",
        payload=_post_query_payload(params),
    )


def query_x_post_material_pool(params, navigation_item=""):
    params = params if isinstance(params, dict) else {}
    payload = _post_query_payload(params)
    if "availability" in params and params["availability"] not in (None, ""):
        payload["availability"] = params["availability"]
    if navigation_item:
        payload["navigation_item"] = str(navigation_item)
    return _request(
        "/internal/posts/material-pool/query",
        method="POST",
        payload=payload,
    )


def add_x_post_material_pool(
    material_ids,
    actor,
    validation_checks=None,
    navigation_item="",
):
    if not isinstance(material_ids, (list, tuple)):
        raise XAccountsClientError("invalid_request", "素材ID列表必须是数组", 400)
    payload = {
        "actor": normalize_actor(actor),
        "scope": "all",
        "material_ids": list(material_ids),
    }
    if validation_checks is not None:
        if not isinstance(validation_checks, (list, tuple)):
            raise XAccountsClientError(
                "invalid_request", "素材校验结果必须是数组", 400
            )
        payload["validation_checks"] = list(validation_checks)
    if navigation_item:
        payload["navigation_item"] = str(navigation_item)
    return _request(
        "/internal/posts/material-pool/add",
        method="POST",
        payload=payload,
    )


def delete_x_post_material_pool(pool_item_id, actor, navigation_item=""):
    pool_item_id = str(pool_item_id or "")
    if not pool_item_id.isdigit() or int(pool_item_id) <= 0:
        raise XAccountsClientError("invalid_request", "素材池记录ID无效", 400)
    payload = {"actor": normalize_actor(actor), "scope": "all"}
    if navigation_item:
        payload["navigation_item"] = str(navigation_item)
    return _request(
        "/internal/posts/material-pool/%s/delete" % pool_item_id,
        method="POST",
        payload=payload,
    )


def query_x_post_account_options(actor, source_type, navigation_item):
    source_type = str(source_type or "").strip().lower()
    if source_type not in {"material", "drama"}:
        raise XAccountsClientError(
            "invalid_request",
            "X发布来源无效",
            400,
        )
    prefix = "material-pool" if source_type == "material" else "drama-pool"
    return _request(
        "/internal/posts/%s/account-options" % prefix,
        method="POST",
        payload={
            "actor": normalize_actor(actor),
            "scope": "all",
            "navigation_item": str(navigation_item or ""),
        },
    )


def query_x_post_schedule(actor, source_type, navigation_item):
    source_type = str(source_type or "").strip().lower()
    if source_type not in {"material", "drama"}:
        raise XAccountsClientError(
            "invalid_request",
            "X发布来源无效",
            400,
        )
    prefix = "material-pool" if source_type == "material" else "drama-pool"
    return _request(
        "/internal/posts/%s/schedule/query" % prefix,
        method="POST",
        payload={
            "actor": normalize_actor(actor),
            "scope": "all",
            "navigation_item": str(navigation_item or ""),
        },
    )


def save_x_post_schedule(
    settings,
    actor,
    source_type,
    navigation_item,
):
    if not isinstance(settings, dict):
        raise XAccountsClientError(
            "invalid_request",
            "自动发布设置必须是对象",
            400,
        )
    source_type = str(source_type or "").strip().lower()
    if source_type not in {"material", "drama"}:
        raise XAccountsClientError(
            "invalid_request",
            "X发布来源无效",
            400,
        )
    prefix = "material-pool" if source_type == "material" else "drama-pool"
    return _request(
        "/internal/posts/%s/schedule/save" % prefix,
        method="POST",
        payload={
            "actor": normalize_actor(actor),
            "scope": "all",
            "navigation_item": str(navigation_item or ""),
            "settings": dict(settings),
        },
    )


def query_x_post_drama_pool(params, navigation_item=""):
    params = params if isinstance(params, dict) else {}
    payload = _post_query_payload(params)
    if "drama_id" in params and params["drama_id"] not in (None, ""):
        payload["drama_id"] = params["drama_id"]
    if navigation_item:
        payload["navigation_item"] = str(navigation_item)
    return _request(
        "/internal/posts/drama-pool/query",
        method="POST",
        payload=payload,
    )


def add_x_post_drama_pool(
    drama_ids,
    validation_checks,
    actor,
    navigation_item="",
):
    if not isinstance(drama_ids, (list, tuple)):
        raise XAccountsClientError(
            "invalid_request",
            "短剧ID列表必须是数组",
            400,
        )
    if not isinstance(validation_checks, (list, tuple)):
        raise XAccountsClientError(
            "invalid_request",
            "短剧校验结果必须是数组",
            400,
        )
    payload = {
        "actor": normalize_actor(actor),
        "scope": "all",
        "drama_ids": list(drama_ids),
        "validation_checks": list(validation_checks),
    }
    if navigation_item:
        payload["navigation_item"] = str(navigation_item)
    return _request(
        "/internal/posts/drama-pool/add",
        method="POST",
        payload=payload,
    )


def query_x_post_drama_pool_episodes(
    pool_item_id,
    params,
    actor,
    navigation_item="",
):
    pool_item_id = str(pool_item_id or "")
    if not pool_item_id.isdigit() or int(pool_item_id) <= 0:
        raise XAccountsClientError(
            "invalid_request",
            "短剧池记录ID无效",
            400,
        )
    params = params if isinstance(params, dict) else {}
    payload = {
        "actor": normalize_actor(actor),
        "scope": "all",
    }
    for field in ("page", "page_size"):
        if field in params and params[field] not in (None, ""):
            payload[field] = params[field]
    if navigation_item:
        payload["navigation_item"] = str(navigation_item)
    return _request(
        "/internal/posts/drama-pool/%s/episodes" % pool_item_id,
        method="POST",
        payload=payload,
    )


def delete_x_post_drama_pool(
    pool_item_id,
    actor,
    navigation_item="",
):
    pool_item_id = str(pool_item_id or "")
    if not pool_item_id.isdigit() or int(pool_item_id) <= 0:
        raise XAccountsClientError(
            "invalid_request",
            "短剧池记录ID无效",
            400,
        )
    payload = {"actor": normalize_actor(actor), "scope": "all"}
    if navigation_item:
        payload["navigation_item"] = str(navigation_item)
    return _request(
        "/internal/posts/drama-pool/%s/delete" % pool_item_id,
        method="POST",
        payload=payload,
    )


def batch_delete_x_post_drama_pool(
    pool_item_ids,
    actor,
    navigation_item="",
):
    if (
        not isinstance(pool_item_ids, list)
        or not pool_item_ids
        or len(pool_item_ids) > 100
    ):
        raise XAccountsClientError(
            "invalid_request",
            "pool_item_ids必须是包含1到100项的数组",
            400,
        )
    normalized = []
    seen = set()
    for raw in pool_item_ids:
        if isinstance(raw, bool):
            raise XAccountsClientError(
                "invalid_request",
                "短剧池记录ID无效",
                400,
            )
        value = str(raw or "")
        if not value.isdigit() or int(value) <= 0:
            raise XAccountsClientError(
                "invalid_request",
                "短剧池记录ID无效",
                400,
            )
        pool_item_id = int(value)
        if pool_item_id in seen:
            raise XAccountsClientError(
                "invalid_request",
                "pool_item_ids不能重复",
                400,
            )
        seen.add(pool_item_id)
        normalized.append(pool_item_id)
    payload = {
        "actor": normalize_actor(actor),
        "scope": "all",
        "pool_item_ids": normalized,
    }
    if navigation_item:
        payload["navigation_item"] = str(navigation_item)
    return _request(
        "/internal/posts/drama-pool/batch-delete",
        method="POST",
        payload=payload,
    )


def record_x_post_material_pool_checks(checks):
    if not isinstance(checks, (list, tuple)) or not checks:
        raise XAccountsClientError(
            "invalid_request", "素材校验结果必须是非空数组", 400
        )
    return _request(
        "/internal/posts/material-pool/check",
        method="POST",
        payload={"checks": list(checks)},
    )


def create_x_post_daily_plan(payload):
    if not isinstance(payload, dict):
        raise XAccountsClientError("invalid_request", "X每日发布计划必须是对象", 400)
    return _request("/internal/posts/daily-plan", method="POST", payload=payload)


def publish_x_post_queue(queue_id):
    queue_id = str(queue_id or "")
    if not queue_id.isdigit() or int(queue_id) <= 0:
        raise XAccountsClientError("invalid_request", "X发布队列ID无效", 400)
    return _request(
        "/internal/posts/queue/%s/publish" % queue_id,
        method="POST",
        payload={},
    )


def query_x_post_material_keys(material_keys):
    if not isinstance(material_keys, (list, tuple)):
        raise XAccountsClientError("invalid_request", "素材ID列表必须是数组", 400)
    return _request(
        "/internal/posts/material-keys/query",
        method="POST",
        payload={"material_keys": list(material_keys)},
    )


def record_x_post_run_failure(payload):
    if not isinstance(payload, dict):
        raise XAccountsClientError("invalid_request", "X每日发布失败记录必须是对象", 400)
    allowed = {
        "run_date",
        "source_date",
        "error_code",
        "error_message",
        "expected_count",
    }
    body = {key: payload.get(key) for key in allowed if key in payload}
    return _request(
        "/internal/posts/runs/record-failure",
        method="POST",
        payload=body,
    )
