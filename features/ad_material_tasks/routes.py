"""Route boundary for ad material task APIs."""

import re
from urllib.parse import parse_qs


_DEPS = {}


def configure_ad_material_task_routes(**deps):
    _DEPS.update(deps)


def _dep(name):
    value = _DEPS.get(name)
    if value is None:
        raise RuntimeError("ad material route dependency is not configured: %s" % name)
    return value


def parse_ad_material_task_route(path):
    match = re.match(r"^/api/ad-material/tasks/([0-9a-f]{32})(?:/([a-z-]+))?$", path)
    if match:
        return match.group(1), match.group(2) or ""
    match = re.match(r"^/api/ad-material/tasks/([0-9a-f]{32})/assets/([^/]+)/review$", path)
    if match:
        return match.group(1), "asset-review:%s" % match.group(2)
    return "", ""


def try_handle_ad_material_get(handler, parsed):
    json_response = _dep("json_response")
    api_error_payload = _dep("api_error_payload")

    if parsed.path == "/api/ad-material/competitor-sources":
        if not handler._require_module("ad_material_tasks"):
            return True
        try:
            include_disabled = (parse_qs(parsed.query).get("include_disabled") or [""])[0] in (
                "1",
                "true",
                "yes",
            )
            payload = {"items": _dep("list_ad_material_competitor_sources")(include_disabled=include_disabled)}
            json_response(handler, 200, payload)
        except Exception as exc:
            json_response(handler, 400, api_error_payload(exc))
        return True

    if parsed.path == "/api/ad-material/products":
        if not handler._require_module("ad_material_tasks"):
            return True
        try:
            product_params = parse_qs(parsed.query)
            product_query = (product_params.get("q") or [""])[0]
            product_limit = (product_params.get("limit") or ["80"])[0]
            payload = _dep("list_ad_material_products")(
                handler._session(),
                query=product_query,
                limit=product_limit,
                with_total=True,
            )
            json_response(handler, 200, payload)
        except Exception as exc:
            json_response(handler, 400, api_error_payload(exc))
        return True

    if parsed.path == "/api/ad-material/tasks":
        if not handler._require_module("ad_material_tasks"):
            return True
        try:
            json_response(handler, 200, _dep("list_ad_material_tasks")(handler._session(), parse_qs(parsed.query)))
        except Exception as exc:
            code = 403 if isinstance(exc, PermissionError) else 400
            json_response(handler, code, api_error_payload(exc))
        return True

    ad_task_id, ad_action = parse_ad_material_task_route(parsed.path)
    if ad_task_id and not ad_action:
        if not handler._require_module("ad_material_tasks"):
            return True
        try:
            task = _dep("fetch_ad_material_task")(ad_task_id)
            _dep("ensure_ad_material_access")(handler._session(), task)
            json_response(handler, 200, task)
        except Exception as exc:
            code = 403 if isinstance(exc, PermissionError) else 400
            json_response(handler, code, api_error_payload(exc))
        return True

    return False


def try_handle_ad_material_post(handler, parsed):
    json_response = _dep("json_response")
    api_error_payload = _dep("api_error_payload")
    append_audit_log = _dep("append_audit_log")

    if parsed.path == "/api/ad-material/tasks":
        if not handler._require_module("ad_material_tasks"):
            return True
        try:
            payload = _dep("create_ad_material_task")(handler._read_json(), handler._session())
            append_audit_log(
                handler._session(),
                "create_ad_material_task",
                "ad_material_task",
                payload.get("task_id", ""),
                payload,
            )
            json_response(handler, 201, payload)
        except Exception as exc:
            code = 403 if isinstance(exc, PermissionError) else 400
            json_response(handler, code, api_error_payload(exc))
        return True

    ad_task_id, ad_action = parse_ad_material_task_route(parsed.path)
    if ad_task_id:
        if not handler._require_module("ad_material_tasks"):
            return True
        try:
            body = handler._read_json()
            if ad_action == "":
                payload = _dep("update_ad_material_task")(ad_task_id, body, handler._session())
                audit_action = "update_ad_material_task"
            elif ad_action == "copy":
                payload = _dep("copy_ad_material_task")(ad_task_id, handler._session())
                audit_action = "copy_ad_material_task"
            elif ad_action == "publish":
                payload = _dep("publish_ad_material_task")(ad_task_id, handler._session())
                audit_action = "publish_ad_material_task"
            elif ad_action == "demand-review":
                payload = _dep("review_ad_material_demand")(ad_task_id, body, handler._session())
                audit_action = "review_ad_material_demand"
            elif ad_action == "export-pdf":
                payload = _dep("export_ad_material_demand_pdf")(ad_task_id, handler._session())
                audit_action = "export_ad_material_demand_pdf"
            elif ad_action == "complete-upload":
                payload = _dep("complete_ad_material_upload")(ad_task_id, handler._session())
                audit_action = "complete_ad_material_upload"
            elif ad_action.startswith("asset-review:"):
                asset_id = ad_action.split(":", 1)[1]
                payload = _dep("review_ad_material_asset")(ad_task_id, asset_id, body, handler._session())
                audit_action = "review_ad_material_asset"
            else:
                json_response(handler, 404, {"error": "not_found"})
                return True
            audit_detail = {"status": payload.get("status", "")}
            if ad_action == "demand-review":
                audit_detail["result"] = str(body.get("result") or "").strip()
                reason = str(body.get("reason") or "").strip()
                if reason:
                    audit_detail["reason_excerpt"] = reason[:500]
            append_audit_log(
                handler._session(),
                audit_action,
                "ad_material_task",
                ad_task_id,
                audit_detail,
            )
            json_response(handler, 202 if audit_action.startswith(("publish", "review")) else 200, payload)
        except Exception as exc:
            code = 403 if isinstance(exc, PermissionError) else 400
            json_response(handler, code, api_error_payload(exc))
        return True

    return False


def try_handle_ad_material_delete(handler, parsed):
    ad_task_id, ad_action = parse_ad_material_task_route(parsed.path)
    if not (ad_task_id and not ad_action):
        return False

    json_response = _dep("json_response")
    api_error_payload = _dep("api_error_payload")
    if not handler._require_module("ad_material_tasks"):
        return True
    try:
        result = _dep("delete_ad_material_task")(ad_task_id, handler._session())
        _dep("append_audit_log")(handler._session(), "delete_ad_material_task", "ad_material_task", ad_task_id, {})
        json_response(handler, 200, result)
    except Exception as exc:
        code = 403 if isinstance(exc, PermissionError) else 400
        json_response(handler, code, api_error_payload(exc))
    return True
