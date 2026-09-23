"""Recover absent source durations from bounded, verified video downloads."""

from __future__ import annotations

import math
import http.client
import json
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

from features.x_posts import service


_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
_PROBE_SLOTS = threading.BoundedSemaphore(2)
_CACHE_TTL_SECONDS = 900
_CACHE_LIMIT = 256
_MAX_METADATA_BYTES = 8 * 1024 * 1024


def _box_payloads(data):
    offset = 0
    while offset < len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        if size < header or offset + size > len(data):
            raise ValueError("invalid MP4 box")
        yield kind, data[offset + header:offset + size]
        offset += size


def _movie_duration(moov):
    boxes = list(_box_payloads(moov))
    # A container duration alone does not establish that this is a video.
    has_video = any(
        handler == b"hdlr" and len(payload) >= 12 and payload[8:12] == b"vide"
        for kind, trak in boxes if kind == b"trak"
        for child, mdia in _box_payloads(trak) if child == b"mdia"
        for handler, payload in _box_payloads(mdia)
    )
    if not has_video:
        raise ValueError("no video track")
    for kind, payload in boxes:
        if kind != b"mvhd" or not payload:
            continue
        if payload[0] == 0:
            scale, ticks = struct.unpack_from(">II", payload, 12)
            unknown = 0xffffffff
        elif payload[0] == 1:
            scale, ticks = struct.unpack_from(">IQ", payload, 20)
            unknown = 0xffffffffffffffff
        else:
            continue
        if scale > 0 and 0 < ticks < unknown:
            return ticks / scale
    raise ValueError("no finite movie duration")


def _mp4_metadata_duration(url, allowed):
    """Seek MP4 metadata with bounded HTTP ranges instead of fetching mdat."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or parsed.port not in (None, 443)
            or not service._allowed_host(parsed.hostname, allowed)):
        raise ValueError("URL is not allowlisted HTTPS")
    client = service.UrllibHttpClient()
    total_size = None
    validator = None

    def read_range(start, length):
        nonlocal total_size, validator
        headers = {"Range": "bytes=%d-%d" % (start, start + length - 1), "Accept-Encoding": "identity"}
        if validator:
            headers["If-Match"] = validator
        response = client.request("GET", url, headers=headers, timeout=10, stream=True, max_response_bytes=length)
        with response:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
            if response.status != 206 or not match:
                raise ValueError("range unavailable")
            first, last, total = map(int, match.groups())
            if (first != start or last != min(start + length, total) - 1
                    or not 0 < total <= service.DEFAULT_MAX_MEDIA_BYTES
                    or (total_size is not None and total != total_size)):
                raise ValueError("invalid range")
            etag = response.headers.get("etag", "")
            if not etag or etag.startswith("W/") or (validator and etag != validator):
                raise ValueError("unstable source")
            total_size, validator = total, etag
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > length:
                    raise ValueError("range too large")
            if len(body) != last - first + 1:
                raise ValueError("incomplete range")
            return bytes(body)

    offset = 0
    for _ in range(32):
        header_bytes = read_range(offset, 16)
        size, kind = struct.unpack_from(">I4s", header_bytes)
        header_size = 8
        if size == 1:
            size = struct.unpack_from(">Q", header_bytes, 8)[0]
            header_size = 16
        if size == 0:
            size = total_size - offset
        if size < header_size or offset + size > total_size:
            raise ValueError("invalid box size")
        if kind == b"moov":
            if size > _MAX_METADATA_BYTES:
                raise ValueError("metadata too large")
            return _movie_duration(read_range(offset + header_size, size - header_size))
        offset += size
        if offset >= total_size:
            break
    raise ValueError("MP4 metadata unavailable")


def _probe_raw_video_duration(path, timeout=30):
    """Probe a local fallback file without importing runtime-specific repair code."""
    binary = os.environ.get("X_POST_FFPROBE_BIN", "/usr/bin/ffprobe")
    if not Path(binary).is_absolute() or "\x00" in binary:
        raise service.XPostError("media_probe_failed", "ffprobe路径配置无效", 500)
    try:
        result = subprocess.run(
            [binary, "-v", "error", "-protocol_whitelist", "file,pipe", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, check=False,
        )
        if result.returncode != 0:
            raise ValueError("probe failed")
        payload = json.loads(result.stdout)
        videos = [s for s in payload.get("streams", []) if s.get("codec_type") == "video"]
        if not videos:
            raise ValueError("no video")
        for raw in [payload.get("format", {}).get("duration")] + [s.get("duration") for s in videos]:
            try:
                duration = float(raw)
            except (ValueError, TypeError):
                continue
            if math.isfinite(duration) and duration > 0:
                return duration
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        pass
    raise service.XPostError("media_probe_failed", "无法读取视频文件时长", 422)


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
        try:
            duration = _mp4_metadata_duration(url, allowed)
        except (ValueError, OSError, http.client.HTTPException, struct.error, service.XPostError):
            duration = None
        if duration is not None:
            with _CACHE_LOCK:
                _CACHE[key] = (time.monotonic(), duration)
                _CACHE.move_to_end(key)
                while len(_CACHE) > _CACHE_LIMIT:
                    _CACHE.popitem(last=False)
            return duration
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
