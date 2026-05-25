"""Route boundary for drama synthesis APIs."""

import re
from urllib.parse import parse_qs


_DEPS = {}


def configure_drama_synthesis_routes(**deps):
    _DEPS.update(deps)


def _dep(name):
    value = _DEPS.get(name)
    if value is None:
        raise RuntimeError("drama synthesis route dependency is not configured: %s" % name)
    return value


def parse_job_route(path):
    match = re.match(r"^/api/drama-material/jobs/([0-9a-f]{32})(?:/(retry))?$", path)
    if match:
        return match.group(1), match.group(2) or ""
    return "", ""


def try_handle_drama_synthesis_get(handler, parsed):
    json_response = _dep("json_response")
    api_error_payload = _dep("api_error_payload")

    if parsed.path == "/api/drama-material/products":
        if not handler._require_any_module(("drama_synthesis", "cover_synthesis")):
            return True
        try:
            json_response(handler, 200, {"items": _dep("list_products")()})
        except Exception as exc:
            json_response(handler, 500, {"error": str(exc)})
        return True

    if parsed.path == "/api/drama-material/jobs":
        if not handler._require_module("drama_synthesis"):
            return True
        try:
            params = parse_qs(parsed.query)
            payload = _dep("fetch_job_rows")(
                job_id=(params.get("job_id") or [""])[0].strip() or None,
                app_id=(params.get("app_id") or [""])[0].strip() or None,
                content_id=(params.get("content_id") or [""])[0].strip() or None,
                status=(params.get("status") or [""])[0].strip() or None,
                query=(params.get("q") or [""])[0].strip() or None,
                date_from=(params.get("date_from") or [""])[0].strip() or None,
                date_to=(params.get("date_to") or [""])[0].strip() or None,
                page=int((params.get("page") or ["1"])[0]),
                page_size=int((params.get("page_size") or ["20"])[0]),
            )
            json_response(handler, 200, payload)
        except Exception as exc:
            json_response(handler, 400, api_error_payload(exc))
        return True

    job_id, action = parse_job_route(parsed.path)
    if job_id and not action:
        if not handler._require_module("drama_synthesis"):
            return True
        job = _dep("fetch_job_row")(job_id)
        if not job:
            json_response(handler, 404, {"error": "not_found"})
            return True
        json_response(handler, 200, job)
        return True

    return False


def try_handle_drama_synthesis_post(handler, parsed):
    json_response = _dep("json_response")
    api_error_payload = _dep("api_error_payload")
    append_audit_log = _dep("append_audit_log")

    if parsed.path == "/api/drama-material/jobs":
        if not handler._require_module("drama_synthesis"):
            return True
        try:
            payload = _dep("submit_job")(handler._read_json(), handler._session())
            append_audit_log(handler._session(), "create_job", "job", payload.get("job_id", ""), payload)
            json_response(handler, 202, payload)
        except Exception as exc:
            json_response(handler, 400, api_error_payload(exc))
        return True

    job_id, action = parse_job_route(parsed.path)
    if job_id and action == "retry":
        if not handler._require_module("drama_synthesis"):
            return True
        try:
            payload = _dep("retry_job")(job_id)
            append_audit_log(handler._session(), "retry_job", "job", job_id, payload)
            json_response(handler, 202, payload)
        except Exception as exc:
            json_response(handler, 400, api_error_payload(exc))
        return True

    return False


def try_handle_drama_synthesis_delete(handler, parsed):
    job_id, action = parse_job_route(parsed.path)
    if not (job_id and not action):
        return False

    json_response = _dep("json_response")
    if not handler._require_module("drama_synthesis"):
        return True
    if _dep("delete_job")(job_id):
        _dep("append_audit_log")(handler._session(), "delete_job", "job", job_id, {})
        json_response(handler, 200, {"message": "deleted", "job_id": job_id})
    else:
        json_response(handler, 404, {"error": "not_found"})
    return True
