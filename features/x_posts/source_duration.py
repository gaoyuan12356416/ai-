"""Recover absent source durations from bounded, verified video downloads."""

from __future__ import annotations

import math
import os
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

from features.x_posts import service
from features.x_posts.publish_media_repair import _probe_raw_video_duration


_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
_PROBE_SLOTS = threading.BoundedSemaphore(2)
_CACHE_TTL_SECONDS = 900
_CACHE_LIMIT = 256


def probe_source_duration(url):
    """Read actual video parameters, retaining every final publish-time gate.

    Only successful measurements are cached briefly, by exact URL and allowlist.
    Media bytes are temporary and never retained in the source database.
    """
    allowed = tuple(item.strip() for item in (
        os.environ.get("X_POST_SOURCE_DURATION_ALLOWED_HOSTS")
        or os.environ.get("X_POST_DAILY_MEDIA_ALLOWED_HOSTS")
        or os.environ.get("X_POST_MEDIA_ALLOWED_HOSTS")
        or ""
    ).split(",") if item.strip())
    if not allowed:
        raise service.XPostError("media_allowlist_not_configured", "素材域名允许列表未配置", 503)
    key = (str(url), allowed)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
            _CACHE.move_to_end(key)
            return cached[1]
    if not _PROBE_SLOTS.acquire(timeout=5):
        raise service.XPostError("media_probe_failed", "视频时长读取繁忙，请稍后重试", 503)
    try:
        service.preflight_post_storage(service.DEFAULT_STORAGE_ROOT + "/s2l")
        work_root = Path(service.DEFAULT_STORAGE_ROOT) / "media-work"
        with tempfile.TemporaryDirectory(prefix="source-duration-", dir=str(work_root)) as root:
            path = Path(root) / "source.mp4"
            media = service.download_media(
                url, path, allowed, max_bytes=service.DEFAULT_MAX_MEDIA_BYTES, timeout=30,
            )
            if not str(media.get("media_type", "")).startswith("video/"):
                raise service.XPostError("invalid_media_type", "素材文件不包含视频", 422)
            duration = _probe_raw_video_duration(path, timeout=30)
            if not math.isfinite(duration) or duration <= 0:
                raise service.XPostError("invalid_media_duration", "视频文件未提供有效时长", 422)
        with _CACHE_LOCK:
            _CACHE[key] = (time.monotonic(), duration)
            _CACHE.move_to_end(key)
            while len(_CACHE) > _CACHE_LIMIT:
                _CACHE.popitem(last=False)
        return duration
    finally:
        _PROBE_SLOTS.release()
