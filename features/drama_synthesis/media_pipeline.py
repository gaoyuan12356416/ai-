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


NORMALIZATION_PROFILE = "concat-cfr25-yuv420p-sar1-aac128k-48k-stereo-v1"
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


def concat_signature(info):
    """The exact compatibility test used by app.concat_segments_need_normalization."""
    streams = (info or {}).get("streams") or []
    video = next((row for row in streams if row.get("codec_type") == "video"), None)
    audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
    if not video or not audio:
        return None
    return (video.get("codec_name") or "", int(video.get("width") or 0), int(video.get("height") or 0),
            video.get("avg_frame_rate") or video.get("r_frame_rate") or "", video.get("time_base") or "",
            audio.get("codec_name") or "", audio.get("sample_rate") or "", int(audio.get("channels") or 0),
            audio.get("time_base") or "")


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

    def normalize_one(index, source, metadata, offset):
        with use_context(context):
            source_fp = {"sha256": metadata["sha256"], "size_bytes": metadata["size_bytes"]}
            identity = {"kind": "normalized_segment", "profile": normalization_profile, "source": source_fp}
            target = Path(output_dir) / ("%03d.mp4" % (index + offset))
            marker = target.with_name(target.name + ".normalized.json")
            value = load_completed(marker, target, identity)
            if value is None:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                normalize(str(source), str(target))
                fingerprint = file_fingerprint(target)
                save_completed(marker, target, identity, fingerprint, fingerprint=fingerprint)
            return str(target)

    sources, metadata, signatures, normalized = {}, {}, {}, {}
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
                if incompatible and intro_done:
                    for index in sorted(sources):
                        if index not in scheduled_normalization:
                            pending_normalization.append(index)
                            scheduled_normalization.add(index)
                    if normal_future is None and pending_normalization:
                        normal_index = pending_normalization.popleft()
                        normalizing = True
                        normal_future = normalize_pool.submit(normalize_one, normal_index, sources[normal_index],
                                                              metadata[normal_index], 1 if intro else 0)
                waiting = set(downloads)
                if normal_future is not None:
                    waiting.add(normal_future)
                if not waiting:
                    continue
                done, _ = wait(waiting, return_when=FIRST_COMPLETED)
                if normal_future in done:
                    normalized[normal_index] = normal_future.result()
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
                    actual = file_fingerprint(sources[index])
                    if value.get("sha256") != actual["sha256"] or value.get("size_bytes") != actual["size_bytes"]:
                        raise checkpoint_error()
                    metadata[index] = value
                    signatures[index] = concat_signature(probe(sources[index]))
                    with progress_lock:
                        completed.add(index)
                        report(force=True)
                    if index == 0 and not intro_done:
                        intro = intro_factory(sources[0])
                        intro_done = True
                        if intro:
                            sources[-1] = str(intro)
                            metadata[-1] = file_fingerprint(intro)
                            signatures[-1] = concat_signature(probe(str(intro)))
                    expected_segments = len(rows) + (1 if intro else 0)
                    if expected_segments > 1 and (
                        None in signatures.values() or len(set(signatures.values())) > 1
                    ):
                        incompatible = True
            if incompatible:
                return [normalized[index] for index in sorted(sources)]
            return [sources[index] for index in sorted(sources)]
        except BaseException:
            stop.set()
            for future in downloads:
                future.cancel()
            if normal_future is not None:
                normal_future.cancel()
            raise


__all__ = ["download_episode", "download_episode_with_route", "freeze_episode_download_route",
           "validate_episode_download_route", "download_and_prepare_segments", "download_worker_count", "concat_signature"]
