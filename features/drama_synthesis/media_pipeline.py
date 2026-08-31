"""Drama-only verified downloads and a bounded download/normalization pipeline.

No shared downloader or encoding recipe is changed. A nonempty legacy source
without a completed download record is not trusted; it is replaced only after
a fresh, length-checked transfer succeeds. Source URLs never enter records or
operator errors. Only strong ETags authorize a byte-range continuation.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from urllib.parse import urlsplit

import requests

from .core import DramaSynthesisError
from .local_checkpoint import (
    atomic_write_record, checkpoint_error, file_fingerprint, load_completed,
    read_record, save_completed,
)


NORMALIZATION_PROFILE = (
    "concat-ep0-even-display-scale-pad-black-explicit-colorspace-bt709-limited-"
    "progressive-or-bwdif-explicit-parity-h264-high41-cfr25-aac-lc-128k-48k-"
    "stereo-apad-silence-v5"
)
_NORMALIZATION_COLOR_SPACES = frozenset({"bt709", "fcc", "bt470bg", "smpte170m", "smpte240m"})
_NORMALIZATION_COLOR_TRANSFERS = frozenset({
    "bt709", "gamma22", "gamma28", "smpte170m", "smpte240m", "iec61966-2-1",
})
_NORMALIZATION_COLOR_PRIMARIES = frozenset({
    "bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "film", "smpte431", "smpte432",
})
_NORMALIZATION_COLOR_RANGES = frozenset({"tv", "pc"})
_NORMALIZATION_PROGRESSIVE_FIELD_ORDERS = frozenset({"progressive"})
_NORMALIZATION_INTERLACED_FIELD_ORDERS = frozenset({"tt", "bb", "tb", "bt"})
CONCAT_STREAM_PROBE_FIELDS = (
    "codec_type", "codec_name", "profile", "level", "pix_fmt",
    "codec_tag_string", "codec_tag", "is_avc", "nal_length_size", "width", "height", "coded_width", "coded_height",
    "sample_aspect_ratio", "display_aspect_ratio", "field_order", "color_range", "color_space",
    "color_transfer", "color_primaries", "chroma_location", "r_frame_rate", "avg_frame_rate",
    "time_base", "sample_fmt", "sample_rate", "channels", "channel_layout", "bits_per_sample",
    "extradata", "extradata_size", "duration",
)
CONCAT_STREAM_SHOW_ENTRIES = "stream=" + ",".join(CONCAT_STREAM_PROBE_FIELDS) + ":format=duration"
CONCAT_STREAM_PROBE_ARGS = ("-show_data", "-show_entries", CONCAT_STREAM_SHOW_ENTRIES)
_LOCKS_GUARD = threading.Lock()
_DOWNLOAD_LOCKS = {}


def download_worker_count(value=None):
    raw = os.environ.get("DRAMA_GPU_DOWNLOAD_WORKERS", "4") if value is None else value
    if isinstance(raw, bool) or not re.fullmatch(r"[1-8]", str(raw)):
        raise DramaSynthesisError("drama_download_configuration_invalid", "下载并发配置无效", 503)
    return int(raw)


def _download_error(code="drama_episode_download_failed", status=502):
    messages = {
        "drama_episode_source_changed": "视频源版本发生变化，已停止续传",
        "drama_episode_download_invalid": "视频下载完整性校验失败",
        "drama_episode_download_cancelled": "制作已停止，下载检查点已保留",
        "drama_episode_download_route_invalid": "视频下载线路配置与冻结任务不一致",
    }
    return DramaSynthesisError(code, messages.get(code, "视频下载失败，已保留可校验的下载进度"), status)


def freeze_episode_download_route(source_url, policy=None):
    """Freeze a new task's route; never probe a network or rewrite signed URLs.

    The default remains the original URL. Only the exact public Tianmai MP4
    path shape is eligible for the explicit international option. Existing
    tasks without this field continue to use their exact original URL.
    """
    policy = os.environ.get("DRAMA_GPU_TIANMAI_CDN", "original") if policy is None else policy
    if policy not in ("original", "international") or not isinstance(policy, str):
        raise DramaSynthesisError("drama_download_configuration_invalid", "下载线路配置无效", 503)
    if not isinstance(source_url, str) or not source_url or len(source_url) > 16384:
        raise _download_error("drama_episode_download_route_invalid", 400)
    route = {"version": 1, "source_url": source_url, "primary_url": source_url, "fallback_url": ""}
    if policy == "international" and "?" not in source_url and "#" not in source_url:
        try:
            parsed = urlsplit(source_url)
        except ValueError:
            return route
        if (parsed.scheme in ("http", "https") and parsed.netloc == "img.tianmai.cn" and
                re.fullmatch(r"/resource/[A-Za-z0-9_-]+/[A-Za-z0-9][A-Za-z0-9_.-]*\.mp4", parsed.path) and
                source_url == parsed.geturl()):
            route["primary_url"] = parsed._replace(netloc="accelerate.tianmai.cn").geturl()
            route["fallback_url"] = source_url
    return route


def validate_episode_download_route(source_url, route):
    """Validate the frozen relation without consulting current environment."""
    fields = {"version", "source_url", "primary_url", "fallback_url"}
    if (not isinstance(route, dict) or set(route) != fields or type(route["version"]) is not int or
            route["version"] != 1 or any(not isinstance(route[key], str) for key in fields - {"version"}) or
            route["source_url"] != source_url):
        raise _download_error("drama_episode_download_route_invalid", 400)
    if route not in (freeze_episode_download_route(source_url, "original"),
                     freeze_episode_download_route(source_url, "international")):
        raise _download_error("drama_episode_download_route_invalid", 400)
    return dict(route)


def _header(response, name):
    return next((str(value) for key, value in response.headers.items() if str(key).lower() == name.lower()), "")


def _strong_etag(response):
    value = _header(response, "ETag")
    return value if re.fullmatch(r'"[\x21\x23-\x7e]{1,200}"', value) else ""


def _positive_length(value):
    if not re.fullmatch(r"[0-9]{1,18}", str(value)) or int(value) <= 0:
        raise _download_error("drama_episode_download_invalid")
    return int(value)


def _partial_state(part, record, identity):
    value = read_record(record)
    if value is None:
        return None
    fields = {"version", "source_identity", "etag", "expected_size", "partial_size", "partial_sha256"}
    if (set(value) != fields or type(value["version"]) is not int or value["version"] != 1 or
            type(value["expected_size"]) is not int or value["expected_size"] <= 0 or
            type(value["partial_size"]) is not int or not 0 <= value["partial_size"] <= value["expected_size"] or
            not re.fullmatch(r"[0-9a-f]{64}", str(value["partial_sha256"])) or
            not isinstance(value["etag"], str)):
        raise checkpoint_error()
    if value["source_identity"] != identity:
        raise checkpoint_error(conflict=True)
    if part.is_symlink():
        raise checkpoint_error()
    if not part.exists():
        # Crash after atomic source rename but before completed-record write.
        # The unmarked source is deliberately re-downloaded, never adopted.
        return None
    if not part.is_file() or part.stat().st_size < value["partial_size"]:
        raise checkpoint_error()
    digest = hashlib.sha256()
    remaining = value["partial_size"]
    with part.open("rb") as handle:
        while remaining:
            chunk = handle.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise checkpoint_error()
            digest.update(chunk)
            remaining -= len(chunk)
    if digest.hexdigest() != value["partial_sha256"]:
        raise checkpoint_error()
    if part.stat().st_size != value["partial_size"]:
        # Discard only the uncommitted tail after validating the durable prefix.
        with part.open("r+b") as handle:
            handle.truncate(value["partial_size"])
    value["digest"] = digest
    return value


def download_episode(url, path, progress_callback=None, *, identity=None,
                     session_factory=requests.Session, max_attempts=3,
                     connect_timeout=10, read_timeout=180, sleep=time.sleep,
                     stop_event=None, validate=None, transfer_callback=None):
    """Return transport-verified path/size/SHA metadata; never accept a half file.

    ``identity`` may be an already frozen opaque source identity. By default it
    is the SHA256 of the exact input URL, including its query, not a guessed
    resource identity obtained by stripping potentially meaningful parameters.
    ``validate`` is an optional local media validator called before promotion.
    """
    parsed = urlsplit(str(url))
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.username or parsed.password or parsed.fragment or len(str(url)) > 16384):
        raise _download_error("drama_episode_download_invalid", 400)
    if type(max_attempts) is not int or not 1 <= max_attempts <= 5 or not 0 < connect_timeout <= 60 or not 0 < read_timeout <= 300:
        raise _download_error("drama_episode_download_invalid", 400)
    source_identity = hashlib.sha256(str(identity if identity is not None else url).encode("utf-8")).hexdigest()
    target = Path(path)
    part = target.with_name(target.name + ".part")
    partial_record = target.with_name(target.name + ".part.json")
    complete_record = target.with_name(target.name + ".download.json")
    for item in (target, part, partial_record, complete_record):
        if item.is_symlink():
            raise checkpoint_error()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache_identity = {"kind": "episode_download", "source_identity": source_identity}
    with _LOCKS_GUARD:
        lock = _DOWNLOAD_LOCKS.setdefault(str(target.absolute()), threading.Lock())
    with lock:
        cached = load_completed(complete_record, target, cache_identity)
        if cached is not None:
            if (not re.fullmatch(r"[0-9a-f]{64}", str(cached.get("sha256"))) or
                    type(cached.get("size_bytes")) is not int or cached["size_bytes"] <= 0 or
                    cached.get("source_identity") != source_identity):
                raise checkpoint_error()
            if progress_callback:
                progress_callback(cached["size_bytes"], cached["size_bytes"])
            return {**cached, "path": str(target), "reused": True}

        session = session_factory()
        session.trust_env = False
        session.max_redirects = 3
        force_full = False
        try:
            for attempt in range(max_attempts):
                failure_code = "drama_episode_download_failed"
                if stop_event is not None and stop_event.is_set():
                    raise _download_error("drama_episode_download_cancelled", 409)
                state = None if force_full else _partial_state(part, partial_record, source_identity)
                etag = str((state or {}).get("etag") or "")
                resumable = bool(state and re.fullmatch(r'"[\x21\x23-\x7e]{1,200}"', etag))
                offset = state["partial_size"] if resumable else 0
                headers = {"Accept-Encoding": "identity"}
                if offset:
                    headers.update(Range="bytes=%d-" % offset, **{"If-Range": etag})
                response = None
                retry = False
                try:
                    response = session.get(str(url), headers=headers, stream=True,
                                           timeout=(connect_timeout, read_timeout), allow_redirects=True)
                    status = response.status_code
                    if status == 429 or 500 <= status <= 599:
                        retry = True
                    elif status == 416:
                        match = re.fullmatch(r"bytes \*/([0-9]+)", _header(response, "Content-Range"))
                        if (offset and match and int(match[1]) == offset == state["expected_size"] and
                                _strong_etag(response) == etag):
                            return _finish_download(target, part, partial_record, complete_record,
                                                    cache_identity, state["digest"].hexdigest(), offset,
                                                    etag, progress_callback, validate)
                        # A 416 never proves completeness without exact length
                        # and the unchanged strong validator. Retry a full GET.
                        force_full = True
                        retry = True
                    elif status not in (200, 206):
                        raise _download_error()
                    else:
                        encoding = _header(response, "Content-Encoding").lower()
                        if encoding not in ("", "identity"):
                            raise _download_error("drama_episode_download_invalid")
                        response_etag = _strong_etag(response)
                        length = _positive_length(_header(response, "Content-Length"))
                        if offset and response_etag != etag:
                            raise _download_error("drama_episode_source_changed", 409)
                        if status == 206:
                            match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", _header(response, "Content-Range"))
                            if (not offset or not match or int(match[1]) != offset or
                                    int(match[3]) != state["expected_size"] or int(match[2]) + 1 != int(match[3]) or
                                    length != int(match[2]) - int(match[1]) + 1):
                                raise _download_error("drama_episode_download_invalid")
                            expected, digest, mode = state["expected_size"], state["digest"], "ab"
                        else:
                            # A server may ignore Range. Never append its 200 body.
                            expected, digest, mode, offset = length, hashlib.sha256(), "wb", 0
                            force_full = False
                        etag = response_etag
                        done = offset
                        last_saved, last_saved_at = done, time.monotonic()

                        def save_partial(handle):
                            handle.flush()
                            os.fsync(handle.fileno())
                            atomic_write_record(partial_record, {
                                "version": 1, "source_identity": source_identity, "etag": etag,
                                "expected_size": expected, "partial_size": done,
                                "partial_sha256": digest.hexdigest(),
                            })

                        if progress_callback:
                            progress_callback(done, expected)
                        with part.open(mode) as handle:
                            try:
                                for chunk in response.iter_content(chunk_size=256 * 1024):
                                    if stop_event is not None and stop_event.is_set():
                                        raise _download_error("drama_episode_download_cancelled", 409)
                                    if not chunk:
                                        continue
                                    if transfer_callback:
                                        transfer_callback(len(chunk))
                                    if done + len(chunk) > expected:
                                        raise _download_error("drama_episode_download_invalid")
                                    handle.write(chunk)
                                    digest.update(chunk)
                                    done += len(chunk)
                                    if progress_callback:
                                        progress_callback(done, expected)
                                    if done - last_saved >= 16 * 1024 * 1024 or time.monotonic() - last_saved_at >= 10:
                                        save_partial(handle)
                                        last_saved, last_saved_at = done, time.monotonic()
                            finally:
                                save_partial(handle)
                        if done != expected:
                            retry = True
                            failure_code = "drama_episode_download_invalid"
                        else:
                            return _finish_download(target, part, partial_record, complete_record,
                                                    cache_identity, digest.hexdigest(), done, etag,
                                                    progress_callback, validate)
                except requests.RequestException:
                    retry = True
                except OSError:
                    raise _download_error("drama_episode_download_invalid") from None
                finally:
                    if response is not None:
                        response.close()
                if retry and attempt + 1 < max_attempts:
                    delay = min(2 ** attempt, 8)
                    if stop_event is not None:
                        if stop_event.wait(delay):
                            raise _download_error("drama_episode_download_cancelled", 409)
                    else:
                        sleep(delay)
            raise _download_error(failure_code) from None
        finally:
            session.close()


def _finish_download(target, part, partial_record, complete_record, identity,
                     sha256, size, etag, callback, validate):
    if part.is_symlink() or not part.is_file() or part.stat().st_size != size or size <= 0:
        raise _download_error("drama_episode_download_invalid")
    if validate is not None:
        try:
            if validate(str(part)) is False:
                raise ValueError("invalid media")
        except Exception:
            raise _download_error("drama_episode_download_invalid") from None
    result = {"path": str(target), "sha256": sha256, "size_bytes": size,
              "source_identity": identity["source_identity"], "etag": etag}
    os.replace(part, target)
    save_completed(complete_record, target, identity, result,
                   fingerprint={"sha256": sha256, "size_bytes": size})
    partial_record.unlink(missing_ok=True)
    if callback:
        callback(size, size)
    return {**result, "reused": False}


def download_episode_with_route(source_url, path, route=None, progress_callback=None, *,
                                downloader=download_episode, **download_options):
    """Use a frozen CDN route, with isolated origin files and sticky fallback.

    Fallback is allowed only after transport/HTTP failure. An integrity,
    source-version or checkpoint conflict is never bypassed by another origin.
    Both origins retain their own exact-URL identity; no byte range crosses
    hosts. A completed canonical record survives an upload/render retry.
    """
    if route is not None:
        route = validate_episode_download_route(source_url, route)
    if route is None or not route["fallback_url"]:
        previous_route = Path(path).with_name(Path(path).name + ".route.json")
        if previous_route.exists() or previous_route.is_symlink():
            raise checkpoint_error(conflict=True)
        return downloader(source_url, path, progress_callback, **download_options)
    if download_options.get("identity") is not None:
        raise _download_error("drama_episode_download_route_invalid", 400)
    route_sha = hashlib.sha256(json.dumps(route, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    source_identity = hashlib.sha256(source_url.encode()).hexdigest()
    identity = {"kind": "episode_routed_download", "route_sha256": route_sha}
    target = Path(path)
    record = target.with_name(target.name + ".download.json")
    state_path = target.with_name(target.name + ".route.json")
    origins = target.parent / ("." + target.name + ".download-origins")
    for item in (target, record, state_path, origins):
        if item.is_symlink():
            raise checkpoint_error()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _LOCKS_GUARD:
        lock = _DOWNLOAD_LOCKS.setdefault(str(target.absolute()), threading.Lock())
    with lock:
        state = read_record(state_path)
        if state is not None:
            if (set(state) != {"version", "route_sha256", "active_origin"} or
                    type(state["version"]) is not int or state["version"] != 1 or
                    state["active_origin"] not in ("primary", "fallback")):
                raise checkpoint_error()
            if state["route_sha256"] != route_sha:
                raise checkpoint_error(conflict=True)
        cached = load_completed(record, target, identity)
        if cached is not None:
            role = cached.get("origin")
            if (role not in ("primary", "fallback") or cached.get("route_sha256") != route_sha or
                    cached.get("source_identity") != source_identity or
                    cached.get("origin_identity") != hashlib.sha256(route[role + "_url"].encode()).hexdigest() or
                    not re.fullmatch(r"[0-9a-f]{64}", str(cached.get("sha256"))) or
                    type(cached.get("size_bytes")) is not int or cached["size_bytes"] <= 0 or
                    (state is not None and role != state["active_origin"])):
                raise checkpoint_error()
            if progress_callback:
                progress_callback(cached["size_bytes"], cached["size_bytes"])
            return {**cached, "path": str(target), "reused": True}
        if state is None:
            state = {"version": 1, "route_sha256": route_sha, "active_origin": "primary"}
            atomic_write_record(state_path, state)
        origins.mkdir(mode=0o700, exist_ok=True)
        role = state["active_origin"]
        while True:
            origin_path = origins / (role + ".mp4")
            try:
                value = downloader(route[role + "_url"], origin_path, progress_callback, **download_options)
                break
            except DramaSynthesisError as exc:
                if role != "primary" or exc.code != "drama_episode_download_failed":
                    raise
                stop = download_options.get("stop_event")
                if stop is not None and stop.is_set():
                    raise _download_error("drama_episode_download_cancelled", 409) from None
                role = "fallback"
                state["active_origin"] = role
                atomic_write_record(state_path, state)
                if progress_callback:
                    progress_callback(0, 0)
        fingerprint = file_fingerprint(origin_path)
        origin_identity = hashlib.sha256(route[role + "_url"].encode()).hexdigest()
        if (value.get("sha256") != fingerprint["sha256"] or value.get("size_bytes") != fingerprint["size_bytes"] or
                value.get("source_identity") != origin_identity):
            raise checkpoint_error()
        result = {**fingerprint, "path": str(target), "source_identity": source_identity,
                  "origin_identity": origin_identity, "origin": role, "route_sha256": route_sha,
                  "etag": value.get("etag", "")}
        # Keep the origin artifact and its completed record intact. If a crash
        # happens between canonical rename and marker write, replay promotes the
        # already verified origin without another download. Hard links add no
        # second large allocation on the normal same-filesystem path.
        fd, temporary_name = tempfile.mkstemp(prefix=".download-promote-", dir=str(target.parent))
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            temporary.unlink()
            try:
                os.link(origin_path, temporary)
            except OSError:
                with origin_path.open("rb") as source, temporary.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                if file_fingerprint(temporary) != fingerprint:
                    raise checkpoint_error()
            os.replace(temporary, target)
            save_completed(record, target, identity, result, fingerprint=fingerprint)
        finally:
            temporary.unlink(missing_ok=True)
        return {**result, "reused": bool(value.get("reused", False))}


def _signature_text(stream, field):
    value = stream.get(field)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value == "N/A":
        return None
    return value


def _signature_integer(stream, field, minimum=0):
    value = stream.get(field)
    if value is None or isinstance(value, bool) or not re.fullmatch(r"-?[0-9]+", str(value)):
        return None
    value = int(value)
    return value if value >= minimum else None


def _signature_ratio(stream, field, separator="/"):
    value = _signature_text(stream, field)
    if value is None:
        return None
    parts = value.split(separator)
    if len(parts) != 2 or not all(re.fullmatch(r"[0-9]+", part) for part in parts):
        return None
    if int(parts[0]) <= 0 or int(parts[1]) <= 0:
        return None
    return value


def _extradata_signature(stream):
    value = _signature_text(stream, "extradata")
    size = _signature_integer(stream, "extradata_size", minimum=1)
    if value is None or size is None:
        return None
    # ffprobe formats -show_data as a deterministic hex dump. Normalize only
    # platform newlines; byte/config changes must change this digest.
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return size, hashlib.sha256(value.encode("utf-8")).hexdigest()


def concat_signature(info):
    """Return a fail-closed MP4 stream-copy signature, or None if incomplete."""
    if not isinstance(info, dict):
        return None
    streams = info.get("streams")
    if not isinstance(streams, list):
        return None
    videos = [row for row in streams if isinstance(row, dict) and row.get("codec_type") == "video"]
    audios = [row for row in streams if isinstance(row, dict) and row.get("codec_type") == "audio"]
    if len(videos) != 1 or len(audios) != 1:
        return None
    video, audio = videos[0], audios[0]
    video_values = (
        _signature_text(video, "codec_name"), _signature_text(video, "profile"),
        _signature_integer(video, "level"), _signature_text(video, "pix_fmt"),
        _signature_text(video, "codec_tag_string"), _signature_text(video, "codec_tag"),
        _signature_text(video, "is_avc"), _signature_integer(video, "nal_length_size", minimum=1),
        _signature_integer(video, "width", minimum=1), _signature_integer(video, "height", minimum=1),
        _signature_integer(video, "coded_width", minimum=1),
        _signature_integer(video, "coded_height", minimum=1),
        _signature_ratio(video, "sample_aspect_ratio", separator=":"),
        _signature_ratio(video, "display_aspect_ratio", separator=":"),
        _signature_text(video, "field_order"), _signature_text(video, "color_range"),
        _signature_text(video, "color_space"), _signature_text(video, "color_transfer"),
        _signature_text(video, "color_primaries"), _signature_text(video, "chroma_location"),
        _signature_ratio(video, "r_frame_rate"), _signature_ratio(video, "avg_frame_rate"),
        _signature_ratio(video, "time_base"), _extradata_signature(video),
    )
    audio_values = (
        _signature_text(audio, "codec_name"), _signature_text(audio, "profile"),
        _signature_text(audio, "sample_fmt"), _signature_integer(audio, "sample_rate", minimum=1),
        _signature_integer(audio, "channels", minimum=1), _signature_text(audio, "channel_layout"),
        _signature_text(audio, "codec_tag_string"), _signature_text(audio, "codec_tag"),
        _signature_integer(audio, "bits_per_sample"), _signature_ratio(audio, "time_base"),
        _extradata_signature(audio),
    )
    if any(value is None for value in video_values + audio_values):
        return None
    return ("mp4-stream-copy-v2", ("video",) + video_values, ("audio",) + audio_values)


def _normalization_error():
    return DramaSynthesisError(
        "drama_concat_normalization_invalid",
        "转码后的剧集片段仍不兼容，已停止拼接",
        502,
    )


def _normalization_source_error():
    return DramaSynthesisError(
        "drama_concat_normalization_source_unsupported",
        "源视频缺少可验证的色彩或流信息，已停止转码",
        422,
    )


def concat_signatures_are_compatible(signatures):
    signatures = list(signatures)
    return bool(signatures) and all(signature is not None for signature in signatures) and len(set(signatures)) == 1


def validate_normalized_concat_signatures(signatures):
    """Require every normalized target to have one identical complete signature."""
    signatures = list(signatures)
    if not concat_signatures_are_compatible(signatures):
        raise _normalization_error()
    return signatures[0]


def _one_stream(info, codec_type, *, required=True):
    if not isinstance(info, dict) or not isinstance(info.get("streams"), list):
        raise _normalization_source_error()
    streams = [row for row in info["streams"]
               if isinstance(row, dict) and row.get("codec_type") == codec_type]
    if len(streams) > 1 or (required and len(streams) != 1):
        raise _normalization_source_error()
    return streams[0] if streams else None


def freeze_concat_normalization_plan(reference_info, source_info, segment_index,
                                     normalization_profile=NORMALIZATION_PROFILE):
    """Freeze deterministic geometry, pixels, scan conversion and audio policy."""
    if (isinstance(segment_index, bool) or type(segment_index) is not int or segment_index < -1 or
            not isinstance(normalization_profile, str) or len(normalization_profile) > 512 or
            not re.fullmatch(r"[a-z0-9.-]+", normalization_profile)):
        raise _normalization_error()
    reference_video = _one_stream(reference_info, "video")
    source_video = _one_stream(source_info, "video")
    source_audio = _one_stream(source_info, "audio", required=False)
    reference_width = _signature_integer(reference_video, "width", minimum=2)
    reference_height = _signature_integer(reference_video, "height", minimum=2)
    source_width = _signature_integer(source_video, "width", minimum=2)
    source_height = _signature_integer(source_video, "height", minimum=2)
    source_sar = _signature_ratio(source_video, "sample_aspect_ratio", separator=":")
    if None in (reference_width, reference_height, source_width, source_height, source_sar):
        raise _normalization_source_error()
    colors = {
        "space": _signature_text(source_video, "color_space"),
        "transfer": _signature_text(source_video, "color_transfer"),
        "primaries": _signature_text(source_video, "color_primaries"),
        "range": _signature_text(source_video, "color_range"),
    }
    field_order = _signature_text(source_video, "field_order")
    if (colors["space"] not in _NORMALIZATION_COLOR_SPACES or
            colors["transfer"] not in _NORMALIZATION_COLOR_TRANSFERS or
            colors["primaries"] not in _NORMALIZATION_COLOR_PRIMARIES or
            colors["range"] not in _NORMALIZATION_COLOR_RANGES or
            field_order not in _NORMALIZATION_PROGRESSIVE_FIELD_ORDERS | _NORMALIZATION_INTERLACED_FIELD_ORDERS):
        raise _normalization_source_error()
    target_width = reference_width + reference_width % 2
    target_height = reference_height + reference_height % 2
    divisor = math.gcd(target_width, target_height)
    sar_numerator, sar_denominator = (int(value) for value in source_sar.split(":"))
    plan = {
        "version": 3,
        "profile": normalization_profile,
        "segment_index": segment_index,
        "geometry_policy": "episode0-even-display-aspect-scale-pad-black-v1",
        "color_policy": "ffmpeg-colorspace-explicit-input-to-bt709-limited-v1",
        "scan_policy": "preserve-progressive-or-bwdif-explicit-display-parity-v2",
        "audio_policy": "preserve-first-resample-apad-or-video-bounded-silence-v2",
        "target": {
            "width": target_width,
            "height": target_height,
            "display_aspect_ratio": "%d:%d" % (target_width // divisor, target_height // divisor),
            "sample_aspect_ratio": "1:1",
            "frame_rate": "25/1",
            "time_base": "1/12800",
            "pix_fmt": "yuv420p",
            "color_space": "bt709",
            "color_transfer": "bt709",
            "color_primaries": "bt709",
            "color_range": "tv",
            "field_order": "progressive",
            "chroma_location": "left",
            "video_codec": "h264",
            "video_profile": "High",
            "video_level": 41,
            "video_tag": "avc1",
            "video_tag_hex": "0x31637661",
        },
        "source": {
            "width": source_width,
            "height": source_height,
            "sample_aspect_ratio": source_sar,
            "sar_numerator": sar_numerator,
            "sar_denominator": sar_denominator,
            "color_space": colors["space"],
            "color_transfer": colors["transfer"],
            "color_primaries": colors["primaries"],
            "color_range": colors["range"],
            "field_order": field_order,
            "scan_mode": "progressive" if field_order == "progressive" else "interlaced",
            "deinterlace_parity": (
                "none" if field_order == "progressive" else
                "tff" if field_order in {"tt", "bt"} else "bff"
            ),
        },
        "audio": {
            "mode": "resample" if source_audio is not None else "silence",
            "codec": "aac",
            "profile": "LC",
            "sample_fmt": "fltp",
            "sample_rate": 48000,
            "channels": 2,
            "channel_layout": "stereo",
            "bit_rate": 128000,
            "tag": "mp4a",
            "tag_hex": "0x6134706d",
        },
    }
    return validate_concat_normalization_plan(plan)


def validate_concat_normalization_plan(plan):
    """Validate the closed plan before it can influence an FFmpeg command."""
    if not isinstance(plan, dict) or set(plan) != {
        "version", "profile", "segment_index", "geometry_policy", "color_policy", "scan_policy",
        "audio_policy",
        "target", "source", "audio",
    }:
        raise _normalization_error()
    if (type(plan["version"]) is not int or plan["version"] != 3 or
            isinstance(plan["segment_index"], bool) or type(plan["segment_index"]) is not int or
            plan["segment_index"] < -1 or not isinstance(plan["profile"], str) or
            not re.fullmatch(r"[a-z0-9.-]{1,512}", plan["profile"]) or
            plan["geometry_policy"] != "episode0-even-display-aspect-scale-pad-black-v1" or
            plan["color_policy"] != "ffmpeg-colorspace-explicit-input-to-bt709-limited-v1" or
            plan["scan_policy"] != "preserve-progressive-or-bwdif-explicit-display-parity-v2" or
            plan["audio_policy"] != "preserve-first-resample-apad-or-video-bounded-silence-v2"):
        raise _normalization_error()
    target, source, audio = plan.get("target"), plan.get("source"), plan.get("audio")
    if (not isinstance(target, dict) or set(target) != {
            "width", "height", "display_aspect_ratio", "sample_aspect_ratio", "frame_rate", "time_base",
            "pix_fmt", "color_space", "color_transfer", "color_primaries", "color_range", "field_order",
            "chroma_location", "video_codec", "video_profile", "video_level", "video_tag",
            "video_tag_hex",
    } or not isinstance(source, dict) or set(source) != {
            "width", "height", "sample_aspect_ratio", "sar_numerator", "sar_denominator", "color_space",
            "color_transfer", "color_primaries", "color_range", "field_order", "scan_mode",
            "deinterlace_parity",
    } or not isinstance(audio, dict) or set(audio) != {
            "mode", "codec", "profile", "sample_fmt", "sample_rate", "channels", "channel_layout",
            "bit_rate", "tag", "tag_hex",
    }):
        raise _normalization_error()
    integers = (target["width"], target["height"], target["video_level"], source["width"], source["height"],
                source["sar_numerator"], source["sar_denominator"], audio["sample_rate"], audio["channels"],
                audio["bit_rate"])
    if any(type(value) is not int or value <= 0 for value in integers):
        raise _normalization_error()
    divisor = math.gcd(target["width"], target["height"])
    if (target["width"] % 2 or target["height"] % 2 or
            target != {
                "width": target["width"], "height": target["height"],
                "display_aspect_ratio": "%d:%d" % (target["width"] // divisor, target["height"] // divisor),
                "sample_aspect_ratio": "1:1", "frame_rate": "25/1", "time_base": "1/12800",
                "pix_fmt": "yuv420p", "color_space": "bt709", "color_transfer": "bt709",
                "color_primaries": "bt709", "color_range": "tv", "field_order": "progressive",
                "chroma_location": "left", "video_codec": "h264", "video_profile": "High",
                "video_level": 41, "video_tag": "avc1", "video_tag_hex": "0x31637661",
            } or source["sample_aspect_ratio"] != "%d:%d" % (
                source["sar_numerator"], source["sar_denominator"]
            ) or source["color_space"] not in _NORMALIZATION_COLOR_SPACES or
            source["color_transfer"] not in _NORMALIZATION_COLOR_TRANSFERS or
            source["color_primaries"] not in _NORMALIZATION_COLOR_PRIMARIES or
            source["color_range"] not in _NORMALIZATION_COLOR_RANGES or
            source["field_order"] not in _NORMALIZATION_PROGRESSIVE_FIELD_ORDERS | _NORMALIZATION_INTERLACED_FIELD_ORDERS or
            source["scan_mode"] != ("progressive" if source["field_order"] == "progressive" else "interlaced") or
            source["deinterlace_parity"] != (
                "none" if source["field_order"] == "progressive" else
                "tff" if source["field_order"] in {"tt", "bt"} else "bff"
            ) or
            audio != {
                "mode": audio["mode"], "codec": "aac", "profile": "LC", "sample_fmt": "fltp",
                "sample_rate": 48000, "channels": 2, "channel_layout": "stereo", "bit_rate": 128000,
                "tag": "mp4a", "tag_hex": "0x6134706d",
            } or audio["mode"] not in ("resample", "silence")):
        raise _normalization_error()
    return json.loads(json.dumps(plan, sort_keys=True, separators=(",", ":")))


def validate_normalized_concat_info(info, plan):
    """Require a normalized output to match its frozen target, not merely its peers."""
    plan = validate_concat_normalization_plan(plan)
    signature = concat_signature(info)
    if signature is None:
        raise _normalization_error()
    video = _one_stream(info, "video")
    audio = _one_stream(info, "audio")
    target = plan["target"]
    video_actual = {
        "codec_name": _signature_text(video, "codec_name"),
        "profile": _signature_text(video, "profile"),
        "level": _signature_integer(video, "level"),
        "pix_fmt": _signature_text(video, "pix_fmt"),
        "codec_tag_string": _signature_text(video, "codec_tag_string"),
        "codec_tag": _signature_text(video, "codec_tag"),
        "is_avc": _signature_text(video, "is_avc"),
        "nal_length_size": _signature_integer(video, "nal_length_size"),
        "width": _signature_integer(video, "width"),
        "height": _signature_integer(video, "height"),
        "coded_width": _signature_integer(video, "coded_width"),
        "coded_height": _signature_integer(video, "coded_height"),
        "sample_aspect_ratio": _signature_text(video, "sample_aspect_ratio"),
        "display_aspect_ratio": _signature_text(video, "display_aspect_ratio"),
        "field_order": _signature_text(video, "field_order"),
        "color_range": _signature_text(video, "color_range"),
        "color_space": _signature_text(video, "color_space"),
        "color_transfer": _signature_text(video, "color_transfer"),
        "color_primaries": _signature_text(video, "color_primaries"),
        "chroma_location": _signature_text(video, "chroma_location"),
        "r_frame_rate": _signature_text(video, "r_frame_rate"),
        "avg_frame_rate": _signature_text(video, "avg_frame_rate"),
        "time_base": _signature_text(video, "time_base"),
    }
    video_expected = {
        "codec_name": target["video_codec"], "profile": target["video_profile"],
        "level": target["video_level"], "pix_fmt": target["pix_fmt"], "codec_tag_string": target["video_tag"],
        "codec_tag": target["video_tag_hex"],
        "is_avc": "true", "nal_length_size": 4, "width": target["width"], "height": target["height"],
        "coded_width": target["width"], "coded_height": target["height"],
        "sample_aspect_ratio": target["sample_aspect_ratio"],
        "display_aspect_ratio": target["display_aspect_ratio"], "field_order": target["field_order"],
        "color_range": target["color_range"], "color_space": target["color_space"],
        "color_transfer": target["color_transfer"], "color_primaries": target["color_primaries"],
        "chroma_location": target["chroma_location"], "r_frame_rate": target["frame_rate"],
        "avg_frame_rate": target["frame_rate"], "time_base": target["time_base"],
    }
    audio_actual = {
        "codec_name": _signature_text(audio, "codec_name"), "profile": _signature_text(audio, "profile"),
        "sample_fmt": _signature_text(audio, "sample_fmt"),
        "sample_rate": _signature_integer(audio, "sample_rate"),
        "channels": _signature_integer(audio, "channels"),
        "channel_layout": _signature_text(audio, "channel_layout"),
        "codec_tag_string": _signature_text(audio, "codec_tag_string"),
        "codec_tag": _signature_text(audio, "codec_tag"),
        "bits_per_sample": _signature_integer(audio, "bits_per_sample"),
        "time_base": _signature_text(audio, "time_base"),
    }
    audio_expected = {
        "codec_name": "aac", "profile": "LC", "sample_fmt": "fltp", "sample_rate": 48000,
        "channels": 2, "channel_layout": "stereo", "codec_tag_string": "mp4a",
        "codec_tag": plan["audio"]["tag_hex"], "bits_per_sample": 0,
        "time_base": "1/48000",
    }
    if video_actual != video_expected or audio_actual != audio_expected:
        raise _normalization_error()
    return signature


def _probe_info_sha256(info):
    try:
        payload = json.dumps(
            info, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise checkpoint_error() from None
    return hashlib.sha256(payload).hexdigest()


def probe_media_source_with_anchor(source, probe, *, expected_fingerprint=None):
    """Bind one full probe result to unchanged source bytes before and after it."""
    source = Path(source)
    before = file_fingerprint(source)
    if expected_fingerprint is not None:
        expected_fingerprint = dict(expected_fingerprint)
        if (set(expected_fingerprint) != {"sha256", "size_bytes"} or
                type(expected_fingerprint["size_bytes"]) is not int or
                expected_fingerprint["size_bytes"] <= 0 or
                not re.fullmatch(r"[0-9a-f]{64}", str(expected_fingerprint["sha256"])) or
                before != expected_fingerprint):
            raise checkpoint_error(conflict=True)
    info = probe(str(source))
    after = file_fingerprint(source)
    if before != after:
        raise checkpoint_error(conflict=True)
    return info, {**before, "probe_sha256": _probe_info_sha256(info)}


def verify_media_source_anchor(source, source_info, anchor):
    """Recheck both the probe digest and current bytes against a frozen anchor."""
    if (not isinstance(anchor, dict) or set(anchor) != {"sha256", "size_bytes", "probe_sha256"} or
            type(anchor["size_bytes"]) is not int or anchor["size_bytes"] <= 0 or
            not re.fullmatch(r"[0-9a-f]{64}", str(anchor["sha256"])) or
            not re.fullmatch(r"[0-9a-f]{64}", str(anchor["probe_sha256"])) or
            anchor["probe_sha256"] != _probe_info_sha256(source_info) or
            file_fingerprint(source) != {"sha256": anchor["sha256"], "size_bytes": anchor["size_bytes"]}):
        raise checkpoint_error(conflict=True)
    return dict(anchor)


def prepare_normalized_concat_segment(source, target, *, source_info, source_anchor,
                                      reference_info, reference_source, reference_anchor, segment_index,
                                      normalize, probe,
                                      normalization_profile=NORMALIZATION_PROFILE):
    """Create/replay one identity-bound normalized segment and re-probe it."""
    source, target, reference_source = Path(source), Path(target), Path(reference_source)
    source_anchor = verify_media_source_anchor(source, source_info, source_anchor)
    reference_anchor = verify_media_source_anchor(reference_source, reference_info, reference_anchor)
    plan = freeze_concat_normalization_plan(
        reference_info, source_info, segment_index, normalization_profile=normalization_profile,
    )
    plan_sha256 = hashlib.sha256(json.dumps(
        plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    identity = {
        "kind": "normalized_segment",
        "source": source_anchor,
        "reference_source": reference_anchor,
        "segment_index": segment_index,
        "normalization_profile": normalization_profile,
        "normalization_plan_sha256": plan_sha256,
        "normalization_plan": plan,
    }
    marker = target.with_name(target.name + ".normalized.json")
    value = load_completed(marker, target, identity)
    if value is None:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        normalize(str(source), str(target), plan)
        fingerprint = file_fingerprint(target)
        signature = validate_normalized_concat_info(probe(str(target)), plan)
        verify_media_source_anchor(source, source_info, source_anchor)
        verify_media_source_anchor(reference_source, reference_info, reference_anchor)
        result = {**fingerprint, "normalization_plan_sha256": plan_sha256}
        save_completed(marker, target, identity, result, fingerprint=fingerprint)
    else:
        if value.get("normalization_plan_sha256") != plan_sha256:
            raise checkpoint_error()
        signature = validate_normalized_concat_info(probe(str(target)), plan)
        verify_media_source_anchor(source, source_info, source_anchor)
        verify_media_source_anchor(reference_source, reference_info, reference_anchor)
    return str(target), signature, plan


def download_and_prepare_segments(items, *, output_dir, probe, normalize,
                                  intro_factory=None, download_workers=None,
                                  downloader=download_episode, progress_callback=None,
                                  normalization_profile=NORMALIZATION_PROFILE):
    """Download at most N episodes and normalize at most one segment at a time.

    items have episode_url/source_path and optional episode_number. The intro
    callback receives the first source path once it has downloaded and returns
    the optional intro path. Results always retain intro + original input order.
    No normalization starts until an actual incompatibility has been observed.
    """
    from .async_runtime import capture_context, emit_progress, use_context

    rows = list(items)
    if not rows:
        return []
    if len({str(Path(row["source_path"]).absolute()) for row in rows}) != len(rows):
        raise _download_error("drama_episode_download_invalid", 400)
    workers = min(download_worker_count(download_workers), len(rows))
    context = capture_context()
    stop = threading.Event()
    progress_lock = threading.Lock()
    downloaded = [0] * len(rows)
    totals = [0] * len(rows)
    transferred = [0] * len(rows)
    completed = set()
    normalized_count = 0
    normalizing = False
    last_reported = 0.0
    last_rate_at = time.monotonic()
    last_rate_bytes = 0
    bytes_per_second = 0.0

    def report(force=False):
        nonlocal last_reported, last_rate_at, last_rate_bytes, bytes_per_second
        now = time.monotonic()
        if not force and now - last_reported < 1:
            return
        last_reported = now
        if now - last_rate_at >= 1:
            bytes_per_second = (sum(transferred) - last_rate_bytes) / (now - last_rate_at)
            last_rate_at, last_rate_bytes = now, sum(transferred)
        stage = "normalizing" if normalizing else "downloading"
        # Until every source has supplied its length, the sum of known headers
        # is not the whole-job denominator. Episode counts remain meaningful.
        metrics = {"downloaded_bytes": sum(downloaded), "total_bytes": sum(totals) if all(totals) else 0,
                   "completed_episodes": len(completed), "total_episodes": len(rows),
                   "normalized_episodes": normalized_count,
                   "total_segments": len(rows) + (1 if intro else 0),
                   "bytes_per_second": round(bytes_per_second, 1)}
        emit_progress(stage, **metrics)
        if progress_callback:
            progress_callback(stage, **metrics)

    def download_one(index):
        with use_context(context):
            def on_bytes(done, total):
                with progress_lock:
                    downloaded[index], totals[index] = done, total
                    report()
            def on_transfer(size):
                with progress_lock:
                    transferred[index] += size
            row = rows[index]
            return download_episode_with_route(row["episode_url"], row["source_path"],
                                               row.get("download_route"), on_bytes, downloader=downloader,
                                               stop_event=stop, transfer_callback=on_transfer)

    def normalize_one(index, source, offset):
        with use_context(context):
            target = Path(output_dir) / ("%03d.mp4" % (index + offset))
            target_path, signature, _ = prepare_normalized_concat_segment(
                source, target, source_info=probe_infos[index], source_anchor=source_anchors[index],
                reference_info=probe_infos[0], reference_source=sources[0],
                reference_anchor=source_anchors[0], segment_index=index,
                normalize=normalize, probe=probe,
                normalization_profile=normalization_profile,
            )
            return target_path, signature

    sources, metadata, probe_infos, source_anchors, signatures = {}, {}, {}, {}, {}
    normalized, normalized_signatures = {}, {}
    pending_normalization = deque()
    scheduled_normalization = set()
    intro_done = intro_factory is None
    intro = None
    incompatible = False
    next_download = 0
    downloads = {}
    normal_future = None
    normal_index = None

    with ThreadPoolExecutor(max_workers=workers) as download_pool, ThreadPoolExecutor(max_workers=1) as normalize_pool:
        try:
            while (next_download < len(rows) or downloads or normal_future is not None or pending_normalization or
                   (incompatible and len(normalized) < len(sources))):
                while next_download < len(rows) and len(downloads) < workers and (
                    not incompatible or len(pending_normalization) < workers * 2
                ):
                    downloads[download_pool.submit(download_one, next_download)] = next_download
                    next_download += 1
                if incompatible and intro_done and 0 in probe_infos:
                    for index in sorted(sources):
                        if index not in scheduled_normalization:
                            pending_normalization.append(index)
                            scheduled_normalization.add(index)
                    if normal_future is None and pending_normalization:
                        normal_index = pending_normalization.popleft()
                        normalizing = True
                        normal_future = normalize_pool.submit(
                            normalize_one, normal_index, sources[normal_index], 1 if intro else 0,
                        )
                waiting = set(downloads)
                if normal_future is not None:
                    waiting.add(normal_future)
                if not waiting:
                    continue
                done, _ = wait(waiting, return_when=FIRST_COMPLETED)
                if normal_future in done:
                    normalized[normal_index], normalized_signatures[normal_index] = normal_future.result()
                    normal_future = None
                    with progress_lock:
                        normalized_count += 1
                        report(force=True)
                for future in done:
                    if future not in downloads:
                        continue
                    index = downloads.pop(future)
                    value = future.result()
                    sources[index] = str(rows[index]["source_path"])
                    expected = {"sha256": value.get("sha256"), "size_bytes": value.get("size_bytes")}
                    probe_infos[index], source_anchors[index] = probe_media_source_with_anchor(
                        sources[index], probe, expected_fingerprint=expected,
                    )
                    metadata[index] = value
                    signatures[index] = concat_signature(probe_infos[index])
                    with progress_lock:
                        completed.add(index)
                        report(force=True)
                    if index == 0 and not intro_done:
                        intro = intro_factory(sources[0])
                        intro_done = True
                        if intro:
                            sources[-1] = str(intro)
                            probe_infos[-1], source_anchors[-1] = probe_media_source_with_anchor(intro, probe)
                            metadata[-1] = {
                                "sha256": source_anchors[-1]["sha256"],
                                "size_bytes": source_anchors[-1]["size_bytes"],
                            }
                            signatures[-1] = concat_signature(probe_infos[-1])
                    expected_segments = len(rows) + (1 if intro else 0)
                    if expected_segments > 1 and (
                        None in signatures.values() or len(set(signatures.values())) > 1
                    ):
                        incompatible = True
            if incompatible:
                ordered = sorted(sources)
                if set(normalized_signatures) != set(ordered):
                    raise _normalization_error()
                validate_normalized_concat_signatures(normalized_signatures[index] for index in ordered)
                for index in ordered:
                    verify_media_source_anchor(sources[index], probe_infos[index], source_anchors[index])
                return [normalized[index] for index in ordered]
            for index in sorted(sources):
                verify_media_source_anchor(sources[index], probe_infos[index], source_anchors[index])
            return [sources[index] for index in sorted(sources)]
        except BaseException:
            stop.set()
            for future in downloads:
                future.cancel()
            if normal_future is not None:
                normal_future.cancel()
            raise


__all__ = ["download_episode", "download_episode_with_route", "freeze_episode_download_route",
           "validate_episode_download_route", "download_and_prepare_segments", "download_worker_count",
           "CONCAT_STREAM_PROBE_ARGS", "CONCAT_STREAM_PROBE_FIELDS", "CONCAT_STREAM_SHOW_ENTRIES",
           "NORMALIZATION_PROFILE", "concat_signature", "concat_signatures_are_compatible",
           "freeze_concat_normalization_plan", "validate_concat_normalization_plan",
           "validate_normalized_concat_info", "validate_normalized_concat_signatures",
           "probe_media_source_with_anchor", "verify_media_source_anchor",
           "prepare_normalized_concat_segment"]
