"""Handoff legacy executor RPCs after authentication, before any native claim."""
import os
import re


def handoff(channel, path):
    if os.environ.get("PIPELINE_" + channel.upper() + "_DISPATCH_ENABLED") != "1":
        return None
    paths = {
        "fb": {"/internal/fb-auto-post/prepare-next", "/internal/fb-auto-post/execute-next",
               "/internal/fb-video-publish/prepare-next", "/internal/fb-video-publish/execute-next"},
        "tt": {"/internal/tt-auto-post/execute-next", "/internal/tt-posts/claim",
               "/internal/tt-posts/preparations/claim", "/internal/tt-posts/direct-tests/claim",
               "/internal/tt-posts/direct-tests/preparations/claim"},
    }
    matched = path in paths.get(channel, set())
    if channel == "x":
        matched = bool(re.fullmatch(r"/internal/posts/(?:auto-template/)?queue/[0-9]+/publish", path))
    elif channel == "tt":
        matched = matched or bool(re.fullmatch(r"/internal/tt-posts/(?:preparations|direct-tests/preparations)/[1-9][0-9]*/process", path))
        matched = matched or bool(re.fullmatch(r"/internal/tt-posts/(?:queue|direct-tests)/[1-9][0-9]*/publish", path))
    if not matched:
        return None
    # Existing runners understand empty claim shapes. Keep original task rows
    # available to the collector; no claim, rendering or platform write occurs.
    return {"ok": True, "status": "dispatch_queued", "dispatch_queued": True,
            "claimed": False, "no_claim": True, "item": None, "items": []}
