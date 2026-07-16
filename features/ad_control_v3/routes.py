"""HTTP adapter for the isolated ad-control V3 service.

This module owns transport concerns only: route matching, existing monolith
authentication hooks, same-origin JSON enforcement and safe response mapping.
It does not import ``app`` and it is imported by the monolith only after the
``/api/ad-control/v3`` prefix has matched.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote

from .errors import AdControlV3Error
from .page_renderer import load_asset, render_page


PREFIX = "/api/ad-control/v3"
MODULE_KEY = "ad_control_center"
PAGE_NAMES = frozenset({"rule-groups", "execution-logs"})
ASSET_TYPES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}
MAX_JSON_BODY_BYTES = 2 * 1024 * 1024
QUICK_NAV_STYLE_CSP_HASH = "sha256-hwxbDTADufampcgI9oc75ltbbfB38tCWOve6LIq/j68="
UI_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; "
    f"style-src 'self' '{QUICK_NAV_STYLE_CSP_HASH}'; "
    "img-src 'self' data: https:; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'self'; form-action 'self'"
)


def get_service():
    """Resolve the environment-backed service only after an authenticated API hit."""

    from .service import get_service as service_factory

    return service_factory()


def _send_bytes(
    handler: Any,
    status: int,
    body: bytes,
    content_type: str,
    *,
    no_store: bool = True,
    extra_headers: Optional[Mapping[str, str]] = None,
) -> None:
    handler.send_response(int(status))
    handler.send_header("Content-Type", content_type)
    if no_store:
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Pragma", "no-cache")
    handler.send_header("X-Content-Type-Options", "nosniff")
    for name, value in (extra_headers or {}).items():
        handler.send_header(str(name), str(value))
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_json(handler: Any, status: int, payload: Any, *, allow: str = "") -> None:
    if payload is None:
        payload = {"ok": True}
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    headers = {"Allow": allow} if allow else None
    _send_bytes(
        handler,
        status,
        body,
        "application/json; charset=utf-8",
        extra_headers=headers,
    )


def _error(handler: Any, error: AdControlV3Error) -> None:
    status = int(error.status or 400)
    if status < 400 or status > 599:
        status = 400
    _send_json(handler, status, error.to_dict())


def _not_found(handler: Any) -> None:
    _error(handler, AdControlV3Error("not_found", "resource not found", status=404))


def _method_not_allowed(handler: Any, allow: Sequence[str]) -> None:
    value = ", ".join(allow)
    payload = AdControlV3Error(
        "method_not_allowed",
        "method not allowed",
        status=405,
        details={"allow": list(allow)},
    ).to_dict()
    _send_json(handler, 405, payload, allow=value)


def _segments(path: str) -> Tuple[str, ...]:
    if path == PREFIX:
        return ()
    if not path.startswith(PREFIX + "/"):
        return ("__outside_prefix__",)
    result = []
    for raw in path[len(PREFIX) + 1 :].strip("/").split("/"):
        if not raw:
            continue
        value = unquote(raw)
        if not value or "/" in value or "\\" in value or value in {".", ".."}:
            return ("__invalid_path__",)
        result.append(value)
    return tuple(result)


def _query(parsed: Any) -> Dict[str, Any]:
    values = parse_qs(str(getattr(parsed, "query", "") or ""), keep_blank_values=False)
    result: Dict[str, Any] = {}
    for key, items in values.items():
        if not items:
            continue
        result[str(key)] = items if len(items) > 1 else items[0]
    return result


def _positive_integer(value: Any, field: str, default: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise AdControlV3Error(
            "validation_error",
            "%s must be an integer" % field,
            details={"field": field},
        )
    if parsed < 1 or parsed > maximum:
        raise AdControlV3Error(
            "validation_error",
            "%s is out of range" % field,
            details={"field": field, "minimum": 1, "maximum": maximum},
        )
    return parsed


def _pagination(query: Dict[str, Any]) -> Tuple[int, int, Dict[str, Any]]:
    filters = dict(query)
    page = _positive_integer(filters.pop("page", None), "page", 1, 1_000_000)
    page_size = _positive_integer(filters.pop("page_size", None), "page_size", 20, 100)
    return page, page_size, filters


def _actor(handler: Any) -> Dict[str, Any]:
    session = dict(handler._session() or {})
    if "is_admin" not in session:
        session["is_admin"] = str(session.get("role") or "").strip().lower() in {
            "admin",
            "administrator",
            "superadmin",
            "super_admin",
        }
    return session


def _read_payload(handler: Any) -> Dict[str, Any]:
    raw_length = str(handler.headers.get("Content-Length", "") or "").strip()
    if raw_length:
        try:
            content_length = int(raw_length)
        except ValueError:
            raise AdControlV3Error("invalid_request", "Content-Length is invalid")
        if content_length < 0:
            raise AdControlV3Error("invalid_request", "Content-Length is invalid")
        if content_length > MAX_JSON_BODY_BYTES:
            raise AdControlV3Error(
                "request_too_large",
                "JSON body is too large",
                status=413,
                details={"max_bytes": MAX_JSON_BODY_BYTES},
            )
    value = handler._read_json()
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AdControlV3Error("validation_error", "JSON body must be an object")
    return value


def _render_ui(handler: Any, method: str, segments: Tuple[str, ...]) -> None:
    if not handler._require_cookie_module(MODULE_KEY):
        return
    if method != "GET":
        _method_not_allowed(handler, ("GET",))
        return
    if len(segments) != 2 or segments[1] not in PAGE_NAMES:
        _not_found(handler)
        return
    html = render_page(
        segments[1],
        {
            "apiBase": PREFIX,
            "page": segments[1],
        },
    ).encode("utf-8")
    _send_bytes(
        handler,
        200,
        html,
        "text/html; charset=utf-8",
        extra_headers={
            "Content-Security-Policy": UI_CONTENT_SECURITY_POLICY,
            "Referrer-Policy": "same-origin",
        },
    )


def _render_asset(handler: Any, method: str, segments: Tuple[str, ...]) -> None:
    if not handler._require_cookie_module(MODULE_KEY):
        return
    if method != "GET":
        _method_not_allowed(handler, ("GET",))
        return
    if len(segments) != 2 or segments[1] not in ASSET_TYPES:
        _not_found(handler)
        return
    _send_bytes(
        handler,
        200,
        load_asset(segments[1]),
        ASSET_TYPES[segments[1]],
    )


def _dispatch_api(handler: Any, method: str, segments: Tuple[str, ...], parsed: Any) -> None:
    if not handler._require_module(MODULE_KEY):
        return
    if method in {"POST", "PUT", "DELETE"} and not handler._require_same_origin_json():
        return

    service_instance = None

    def service():
        nonlocal service_instance
        if service_instance is None:
            service_instance = get_service()
        return service_instance

    actor = _actor(handler)
    query = _query(parsed)

    if segments == ("meta",):
        if method != "GET":
            _method_not_allowed(handler, ("GET",))
            return
        _send_json(handler, 200, service().meta(actor))
        return

    if segments == ("rule-groups",):
        if method == "GET":
            page, page_size, filters = _pagination(query)
            _send_json(handler, 200, service().list_rule_groups(actor, filters, page, page_size))
            return
        if method == "POST":
            payload = _read_payload(handler)
            _send_json(handler, 201, service().create_rule_group(actor, payload))
            return
        _method_not_allowed(handler, ("GET", "POST"))
        return

    if len(segments) == 2 and segments[0] == "rule-groups":
        group_id = segments[1]
        if method == "GET":
            _send_json(handler, 200, service().get_rule_group(actor, group_id))
            return
        if method == "PUT":
            payload = _read_payload(handler)
            expected_version: Optional[int] = None
            raw_version = handler.headers.get("If-Match", "") or payload.get("version")
            if raw_version not in (None, ""):
                raw_version = str(raw_version).strip().strip('"')
                expected_version = _positive_integer(raw_version, "version", 1, 2_147_483_647)
            _send_json(
                handler,
                200,
                service().update_rule_group(actor, group_id, payload, expected_version),
            )
            return
        if method == "DELETE":
            _read_payload(handler)
            _send_json(handler, 200, service().delete_rule_group(actor, group_id))
            return
        _method_not_allowed(handler, ("GET", "PUT", "DELETE"))
        return

    if len(segments) == 3 and segments[0] == "rule-groups":
        group_id, action = segments[1], segments[2]
        if method != "POST":
            _method_not_allowed(handler, ("POST",))
            return
        payload = _read_payload(handler)
        if action == "duplicate":
            _send_json(handler, 201, service().duplicate_rule_group(actor, group_id, payload))
            return
        if action == "preview":
            _send_json(handler, 200, service().preview(actor, group_id, payload))
            return
        if action == "enabled":
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                raise AdControlV3Error(
                    "validation_error",
                    "enabled must be boolean",
                    details={"field": "enabled"},
                )
            _send_json(
                handler,
                200,
                service().set_enabled(
                    actor,
                    group_id,
                    enabled,
                    str(payload.get("confirm") or ""),
                ),
            )
            return
        if action == "emergency-stop":
            _send_json(handler, 200, service().emergency_stop(actor, group_id))
            return
        _not_found(handler)
        return

    if segments == ("scope-estimate",):
        if method != "POST":
            _method_not_allowed(handler, ("POST",))
            return
        payload = _read_payload(handler)
        _send_json(handler, 200, service().scope_estimate(actor, payload))
        return

    if segments == ("executions",):
        if method != "GET":
            _method_not_allowed(handler, ("GET",))
            return
        page, page_size, filters = _pagination(query)
        _send_json(handler, 200, service().list_executions(actor, filters, page, page_size))
        return

    if len(segments) == 2 and segments[0] == "executions":
        if method != "GET":
            _method_not_allowed(handler, ("GET",))
            return
        _send_json(handler, 200, service().get_execution(actor, segments[1]))
        return

    _not_found(handler)


def dispatch(handler: Any, method: str, parsed: Any) -> None:
    """Dispatch one already-prefix-matched request and always terminate it."""

    method = str(method or "").upper()
    segments = _segments(str(getattr(parsed, "path", "") or ""))
    try:
        if segments and segments[0] == "ui":
            _render_ui(handler, method, segments)
            return
        if segments and segments[0] == "assets":
            _render_asset(handler, method, segments)
            return
        _dispatch_api(handler, method, segments, parsed)
    except AdControlV3Error as exc:
        _error(handler, exc)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _error(handler, AdControlV3Error("invalid_json", "request JSON is invalid", status=400))
    except (TypeError, ValueError):
        logging.warning("ad-control V3 request rejected", exc_info=True)
        _error(handler, AdControlV3Error("invalid_request", "request is invalid", status=400))
    except Exception:
        logging.exception("ad-control V3 request failed")
        _error(handler, AdControlV3Error("internal_error", "internal server error", status=500))


__all__ = ["PREFIX", "dispatch", "get_service"]
