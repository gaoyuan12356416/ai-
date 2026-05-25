"""Route boundary for cover/screenshot synthesis APIs."""

import re
from urllib.parse import parse_qs


_DEPS = {}


def configure_cover_synthesis_routes(**deps):
    _DEPS.update(deps)


def _dep(name):
    value = _DEPS.get(name)
    if value is None:
        raise RuntimeError("cover synthesis route dependency is not configured: %s" % name)
    return value


def parse_screenshot_job_route(path):
    match = re.match(r"^/api/drama-screenshot-material/jobs/([0-9a-f]{32})(?:/(retry))?$", path)
    if match:
        return match.group(1), match.group(2) or ""
    return "", ""


def try_handle_cover_synthesis_get(handler, parsed):
    json_response = _dep("json_response")
    api_error_payload = _dep("api_error_payload")

    screenshot_job_id, screenshot_action = parse_screenshot_job_route(parsed.path)
    if screenshot_job_id and not screenshot_action:
        if not handler._require_module("cover_synthesis"):
            return True
        try:
            payload = _dep("fetch_screenshot_job_row")(screenshot_job_id)
            if not payload:
                json_response(handler, 404, {"error": "not_found"})
                return True
            json_response(handler, 200, payload)
        except Exception as exc:
            json_response(handler, 500, {"error": str(exc)})
        return True

    if parsed.path == "/api/drama-screenshot-material/jobs":
        if not handler._require_module("cover_synthesis"):
            return True
        try:
            params = parse_qs(parsed.query)
            payload = _dep("fetch_screenshot_job_rows")(
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

    return False


def try_handle_cover_synthesis_post(handler, parsed):
    json_response = _dep("json_response")
    api_error_payload = _dep("api_error_payload")
    append_audit_log = _dep("append_audit_log")

    screenshot_job_id, screenshot_action = parse_screenshot_job_route(parsed.path)
    if screenshot_job_id and screenshot_action == "retry":
        if not handler._require_module("cover_synthesis"):
            return True
        try:
            payload = _dep("retry_screenshot_job")(screenshot_job_id)
            append_audit_log(handler._session(), "retry_screenshot_job", "screenshot_job", screenshot_job_id, payload)
            json_response(handler, 202, payload)
        except Exception as exc:
            json_response(handler, 400, api_error_payload(exc))
        return True

    if parsed.path == "/api/drama-screenshot-material/jobs/delete-batch":
        if not handler._require_cookie_module("cover_synthesis"):
            return True
        try:
            payload = handler._read_json()
            result = _dep("delete_screenshot_jobs")(payload.get("job_ids", []))
            append_audit_log(
                handler._session(),
                "delete_screenshot_job_batch",
                "screenshot_job",
                "",
                {
                    "requested_count": result.get("requested_count", 0),
                    "deleted_count": result.get("deleted_count", 0),
                    "missing_count": result.get("missing_count", 0),
                },
            )
            json_response(handler, 200, result)
        except Exception as exc:
            json_response(handler, 400, api_error_payload(exc))
        return True

    if parsed.path == "/api/drama-screenshot-material/jobs/batch":
        if not handler._require_module("cover_synthesis"):
            return True
        try:
            payload = _dep("submit_screenshot_job_batch")(handler._read_json(), handler._session())
            append_audit_log(
                handler._session(),
                "create_screenshot_job_batch",
                "screenshot_job",
                "",
                {
                    "app_id": payload.get("app_id", ""),
                    "count": payload.get("count", 0),
                    "accepted_count": payload.get("accepted_count", 0),
                    "duplicate_count": payload.get("duplicate_count", 0),
                    "failed_count": payload.get("failed_count", 0),
                },
            )
            json_response(handler, 202, payload)
        except Exception as exc:
            json_response(handler, 400, api_error_payload(exc))
        return True

    if parsed.path == "/api/drama-screenshot-material/jobs":
        if not handler._require_module("cover_synthesis"):
            return True
        try:
            payload = _dep("submit_screenshot_job")(handler._read_json(), handler._session())
            append_audit_log(handler._session(), "create_screenshot_job", "screenshot_job", payload.get("job_id", ""), payload)
            json_response(handler, 202, payload)
        except Exception as exc:
            json_response(handler, 400, api_error_payload(exc))
        return True

    return False


def try_handle_cover_synthesis_delete(handler, parsed):
    screenshot_job_id, screenshot_action = parse_screenshot_job_route(parsed.path)
    if not (screenshot_job_id and not screenshot_action):
        return False

    json_response = _dep("json_response")
    if not handler._require_cookie_module("cover_synthesis"):
        return True
    result = _dep("delete_screenshot_job")(screenshot_job_id)
    if result:
        _dep("append_audit_log")(handler._session(), "delete_screenshot_job", "screenshot_job", screenshot_job_id, {})
        json_response(handler, 200, {"message": "deleted", "job_id": screenshot_job_id})
    else:
        json_response(handler, 404, {"error": "not_found"})
    return True
