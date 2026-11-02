"""Authenticated private adapters hosted by the existing application process."""
import json
import os
import secrets


def dispatch_youtube(handler, path, app):
    respond = app["json_response"]
    expected = os.environ.get("PIPELINE_YOUTUBE_CANONICAL_TOKEN", "")
    supplied = handler.headers.get("Authorization", "").removeprefix("Bearer ")
    if os.environ.get("PIPELINE_YOUTUBE_CANONICAL_BRIDGE_ENABLED") != "1" or len(expected) < 32 or not secrets.compare_digest(supplied, expected):
        return respond(handler, 403, {"code": "pipeline_youtube_bridge_disabled_or_unauthorized"}, no_store=True)
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if not 0 < length <= 8 * 1024 * 1024:
            return respond(handler, 413, {"code": "request_size_invalid"}, no_store=True)
        payload = json.loads(handler.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("invalid request")
        from .youtube_native import handle_youtube_dispatch
        result = handle_youtube_dispatch(app["get_youtube_auto_service"](), path.rsplit("/", 1)[-1], payload)
        return respond(handler, 200, result, no_store=True)
    except Exception as exc:
        return respond(handler, 409, {"code": getattr(exc, "code", "youtube_dispatch_failed")}, no_store=True)
