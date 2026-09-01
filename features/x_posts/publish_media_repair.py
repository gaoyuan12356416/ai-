"""Prepare deferred drama media before entering the OAuth publish lock.

No account token is accepted here. The caller must verify the source account
again after preparation and pass the resulting file to ``publish_canary``.
Only local temporary media and a safe repair audit event are written; frozen
queue URLs, identities, fingerprints and delivery modes are never changed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from features.x_posts import service


_QUEUE_IDENTITY_FIELDS = (
    "id", "source_type", "media_validation_mode", "material_id", "material_url",
    "content_id", "drama_pool_item_id", "episode_number", "account_id",
    "account_username", "delivery_mode", "relay_account_id", "relay_account_username",
)
_DURATION_PENDING_IDENTITY_FIELDS = (
    "id", "source_type", "material_id", "content_id", "drama_pool_item_id",
    "drama_pool_created_at", "episode_number", "episode_key",
    "drama_replay_generation", "account_id", "account_username",
    "account_drama_language", "account_drama_language_frozen",
    "schedule_run_id", "run_date", "source_date", "route_version",
)
_ENV_PREFIX = "X_POST_DEFERRED_DRAMA_REPAIR_"


def _queue_identity(queue):
    return tuple(str(queue.get(field, "") or "") for field in _QUEUE_IDENTITY_FIELDS)


def _duration_pending_identity(queue):
    return tuple(
        str(queue.get(field, "") or "")
        for field in _DURATION_PENDING_IDENTITY_FIELDS
    )


def _probe_raw_video_duration(
    path,
    *,
    max_bytes=service.DEFAULT_MAX_MEDIA_BYTES,
    timeout=30,
    runner=None,
):
    """Read the source duration without applying the 140-second route limit.

    The strict X media contract remains the responsibility of
    :func:`service.probe_media`.  This narrow inspection exists so a malformed
    long video is repaired with the Premium duration policy instead of being
    truncated to the standard-video limit merely because its target account is
    not Premium.
    """
    path = Path(path)
    try:
        file_size = path.stat().st_size
    except OSError:
        raise service.XPostError(
            "invalid_media", "短剧源文件不存在，尚未尝试发布", 400,
        ) from None
    max_bytes = min(
        service._positive_int(max_bytes, "素材大小上限"),
        service.DEFAULT_MAX_MEDIA_BYTES,
    )
    if file_size <= 0 or file_size > max_bytes:
        raise service.XPostError(
            "media_too_large", "短剧源文件为空或超过512MB限制", 413,
        )
    ffprobe_bin = str(
        os.environ.get("X_POST_FFPROBE_BIN", "/usr/bin/ffprobe")
        or "/usr/bin/ffprobe"
    ).strip()
    if (
        not ffprobe_bin
        or "\x00" in ffprobe_bin
        or not (Path(ffprobe_bin).is_absolute() or ffprobe_bin.startswith("/"))
    ):
        raise service.XPostError(
            "media_probe_failed", "ffprobe路径配置无效", 500,
        )
    run = runner or subprocess.run
    try:
        completed = run(
            [
                ffprobe_bin,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(1, min(int(timeout), 120)),
            check=False,
            close_fds=True,
            env={
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
            },
        )
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        raise service.XPostError(
            "media_probe_failed", "ffprobe执行失败: %s" % exc, 422,
        ) from None
    if int(getattr(completed, "returncode", 1)) != 0:
        raise service.XPostError(
            "media_probe_failed", "ffprobe未能解析短剧源文件", 422,
        )
    try:
        payload = json.loads(str(getattr(completed, "stdout", "") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise service.XPostError(
            "media_probe_failed", "ffprobe短剧源文件响应无效", 422,
        ) from None
    streams = payload.get("streams") if isinstance(payload, dict) else None
    streams = streams if isinstance(streams, list) else []
    videos = [
        item
        for item in streams
        if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    if not videos:
        raise service.XPostError(
            "invalid_media_type", "短剧源文件不包含视频流，尚未尝试发布", 422,
        )
    format_data = (
        payload.get("format")
        if isinstance(payload.get("format"), dict)
        else {}
    )
    duration_value = format_data.get("duration") or videos[0].get("duration")
    try:
        duration = float(duration_value)
    except (TypeError, ValueError, OverflowError):
        duration = 0.0
    if not math.isfinite(duration) or duration < 0.5:
        raise service.XPostError(
            "invalid_media_duration",
            "短剧源文件时长必须至少为0.5秒，尚未尝试发布",
            422,
        )
    return duration


def _queue_evidence_value(queue, primary, *aliases):
    if primary in queue:
        return queue.get(primary)
    for alias in aliases:
        if alias in queue:
            return queue.get(alias)
    return None


def _same_float(left, right, tolerance=0.000001):
    try:
        left = float(left)
        right = float(right)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(left) and math.isfinite(right) and abs(left - right) <= tolerance


def _verified_local_media(work_dir, media, max_media_bytes):
    media = dict(media)
    path = Path(media["path"])
    try:
        expected_size = int(media["size"])
        maximum = min(
            service._positive_int(max_media_bytes, "素材大小上限"),
            service.DEFAULT_MAX_MEDIA_BYTES,
        )
        if (
            path.is_symlink()
            or path.resolve(strict=True).parent != Path(work_dir).resolve(strict=True)
            or not path.is_file()
            or expected_size <= 0
            or expected_size > maximum
            or path.stat().st_size != expected_size
        ):
            raise OSError("invalid prepared file")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if not secrets.compare_digest(
            digest.hexdigest(), str(media["sha256"]).lower()
        ):
            raise OSError("invalid prepared fingerprint")
    except (KeyError, OSError, TypeError, ValueError):
        raise service.XPostError(
            "media_preflight_changed",
            "短剧媒体准备后发生变化，尚未尝试发布",
            409,
        ) from None
    return media


def _repair_error(exc):
    code = str(getattr(exc, "code", "") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", code):
        code = "x_post_media_repair_failed"
    messages = {
        "x_post_media_repair_unreachable": "短剧媒体修复服务暂不可用，尚未尝试发布",
        "x_post_media_repair_invalid_response": "短剧媒体修复回执不符合约定，尚未尝试发布",
        "x_post_media_repair_fingerprint_mismatch": "短剧修复文件指纹与回执不一致，尚未尝试发布",
        "x_post_media_repair_probe_mismatch": "短剧修复文件参数与回执不一致，尚未尝试发布",
    }
    # Do not persist a GPU response/error body: it can contain private URLs or
    # unexpected credentials. The bounded code is enough to correlate logs.
    return service.XPostError(
        code,
        messages.get(code, "短剧媒体修复失败（%s），尚未尝试发布" % code),
        503,
    )


def _repair_client_from_env(max_media_bytes):
    from scripts.x_post_daily_runner import DEFAULT_REPAIR_PROFILE, MediaRepairClient

    enabled = os.environ.get(_ENV_PREFIX + "ENABLED", "false").strip().lower()
    if enabled in {"", "false", "0", "no", "off"}:
        return None, ""

    def invalid_config():
        return service.XPostError(
            "x_post_media_repair_config_invalid",
            "短剧媒体修复配置缺失或无效，尚未尝试发布",
            503,
        )

    if enabled not in {"true", "1", "yes", "on"}:
        raise invalid_config()
    url = os.environ.get(_ENV_PREFIX + "URL", "").strip()
    token = os.environ.get(_ENV_PREFIX + "TOKEN", "").strip()
    profile = os.environ.get(_ENV_PREFIX + "PROFILE", DEFAULT_REPAIR_PROFILE).strip()
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
        timeout = int(os.environ.get(_ENV_PREFIX + "TIMEOUT", "900"))
    except (TypeError, ValueError, OverflowError):
        raise invalid_config() from None
    if (
        not url
        or any(character.isspace() or ord(character) < 32 for character in url)
        or parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in range(1, 65536)
        or parsed.path != "/internal/x-post-media-repair"
        or not token
        or any(ord(character) < 32 for character in token)
        or profile != DEFAULT_REPAIR_PROFILE
        or not 5 <= timeout <= 3600
        or any(
            secrets.compare_digest(token, os.environ.get(name, "").strip())
            for name in (
                "X_INTERNAL_TOKEN", "X_POST_AUTOMATION_INTERNAL_TOKEN",
                "X_POST_DAILY_INTERNAL_TOKEN", "X_POST_AUTO_INTERNAL_TOKEN",
            )
        )
    ):
        raise invalid_config()
    return MediaRepairClient(
        url, token, timeout=timeout, max_output_bytes=max_media_bytes,
    ), profile


@dataclass(frozen=True)
class PreparedDeferredDramaMedia:
    """Request-local media capability bound to one immutable deferred queue."""

    queue_identity: tuple
    work_dir: Path
    media: object

    def for_queue(self, queue, max_media_bytes):
        if self.queue_identity != _queue_identity(queue):
            raise service.XPostError(
                "media_preflight_changed", "已准备媒体与冻结短剧队列不一致", 409,
            )
        media = dict(self.media)
        path = Path(media["path"])
        try:
            if (
                path.is_symlink()
                or path.resolve(strict=True).parent != self.work_dir.resolve(strict=True)
                or not path.is_file()
            ):
                raise OSError("invalid prepared file")
            expected_size = int(media["size"])
            if (
                expected_size <= 0
                or expected_size > min(max_media_bytes, service.DEFAULT_MAX_MEDIA_BYTES)
                or path.stat().st_size != expected_size
            ):
                raise OSError("invalid prepared size")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if not secrets.compare_digest(digest.hexdigest(), str(media["sha256"])):
                raise OSError("invalid prepared fingerprint")
        except (OSError, TypeError, ValueError):
            raise service.XPostError(
                "media_preflight_changed", "短剧媒体准备后发生变化，尚未尝试发布", 409,
            ) from None
        return media


@contextlib.contextmanager
def prepare_deferred_drama_media(
    *, queue, log, account, public_root, allowed_media_hosts,
    timeout=30, max_media_bytes=service.DEFAULT_MAX_MEDIA_BYTES,
    storage_guard=None, durable_storage=None, http_client=None,
):
    """Download/probe once, optionally repair once, and retain the verified file.

    Preparation runs before credentials are acquired. A second local probe in
    ``publish_canary`` enforces the newly verified account's current entitlement.
    The original is never downloaded again by the CPU publish path.
    """
    if (
        queue.get("source_type") != "drama"
        or queue.get("media_validation_mode") != service.MEDIA_VALIDATION_DEFERRED
    ):
        yield None
        return
    if (
        log.get("status") != "reserved"
        or log.get("unknown_outcome")
        or int(log.get("attempt_count") or 0) != 0
        or int(log.get("queue_id") or 0) != int(queue["id"])
    ):
        raise service.XPostError(
            "x_post_retry_requires_review", "发布日志已执行，禁止重复准备或发布", 409,
        )
    relay = queue.get("delivery_mode") == service.PREMIUM_RELAY_REPOST_MODE
    expected_id = queue["relay_account_id"] if relay else queue["account_id"]
    expected_username = queue["relay_account_username"] if relay else queue["account_username"]
    if (
        int(account.get("id") or 0) != int(expected_id)
        or str(account.get("username") or "").lstrip("@") != expected_username
    ):
        raise service.XPostError("x_post_account_mismatch", "短剧媒体准备账号与冻结队列不一致", 409)

    work_dir = None
    try:
        try:
            max_media_bytes = min(
                service._positive_int(max_media_bytes, "素材大小上限"),
                service.DEFAULT_MAX_MEDIA_BYTES,
            )
            if callable(storage_guard):
                storage_guard()
            if durable_storage is not None:
                layout = service._validate_post_storage_layout(
                    public_root,
                    mount_root=durable_storage.get("mount_root", service.DEFAULT_STORAGE_MOUNT_ROOT),
                    storage_root=durable_storage.get("storage_root", service.DEFAULT_STORAGE_ROOT),
                )
                work_root = layout["media_work"]
                if (
                    work_root.resolve(strict=True).parent != layout["storage"]
                    or work_root.stat().st_dev != layout["storage"].stat().st_dev
                ):
                    raise service.XPostError("x_post_storage_unavailable", "X Post媒体工作目录无效", 503)
            else:
                work_root = Path(public_root).resolve().parent / "media-work"
                work_root.mkdir(parents=True, exist_ok=True)
            work_dir = Path(tempfile.mkdtemp(prefix="deferred-log-%s-" % log["id"], dir=str(work_root)))
            media = service.download_media(
                queue["material_url"], work_dir / "source.bin", allowed_media_hosts,
                max_bytes=max_media_bytes, timeout=timeout, http_client=http_client,
            )
            if media.get("media_kind") != "video":
                raise service.XPostError("invalid_media_type", "短剧剧集必须是视频，尚未尝试发布", 422)
            premium = service._account_has_premium_video_entitlement(account)
            duration_policy = "premium" if premium else "standard"
            duration_limit = service.PREMIUM_MAX_DURATION_SECONDS if premium else service.STANDARD_MAX_DURATION_SECONDS
            try:
                service.probe_media(
                    media["path"], max_bytes=max_media_bytes,
                    timeout=timeout, max_duration_seconds=duration_limit,
                )
            except service.XPostError as probe_error:
                from scripts.x_post_daily_runner import (
                    REPAIRABLE_MEDIA_CODES, _media_fingerprint,
                    _repair_job_key, _verify_repaired_download,
                )

                if probe_error.code not in REPAIRABLE_MEDIA_CODES:
                    raise
                repair_client, profile = _repair_client_from_env(max_media_bytes)
                if repair_client is None:
                    raise
                try:
                    source_sha256, source_size = _media_fingerprint(media)
                    repair_item = {
                        "material_id": queue["material_id"],
                        "pool_item_id": queue["drama_pool_item_id"],
                    }
                    job_key = _repair_job_key(repair_item, source_sha256, profile, duration_policy)
                    repaired = repair_client.repair({
                        **repair_item,
                        "job_key": job_key,
                        "source_url": queue["material_url"],
                        "source_sha256": source_sha256,
                        "source_size": source_size,
                        "trigger_code": probe_error.code,
                        "profile": profile,
                        "duration_policy": duration_policy,
                    })
                    # The existing worker binds policy into job_key. Older
                    # public responses omit policy; reject a conflicting echo.
                    if (
                        repaired.get("job_key") != job_key
                        or repaired.get("profile") != profile
                        or repaired.get("duration_policy", duration_policy) != duration_policy
                    ):
                        raise service.XPostError(
                            "x_post_media_repair_invalid_response",
                            "短剧媒体修复身份或策略不一致，尚未尝试发布", 502,
                        )
                    # Free the source before downloading the output so the
                    # existing one-file storage reserve remains sufficient.
                    Path(media["path"]).unlink()
                    repaired_media = service.download_media(
                        repaired["output_url"], work_dir / "repaired.bin", allowed_media_hosts,
                        max_bytes=max_media_bytes, timeout=timeout, http_client=http_client,
                    )
                    if repaired_media.get("media_kind") != "video":
                        raise service.XPostError("invalid_media_type", "短剧修复结果不是视频，尚未尝试发布", 422)
                    repaired_probe = service.probe_media(
                        repaired_media["path"], max_bytes=max_media_bytes,
                        timeout=timeout, max_duration_seconds=duration_limit,
                    )
                    final_sha256, final_size, final_probe = _verify_repaired_download(
                        repaired, repaired_media, repaired_probe,
                        max_duration_seconds=duration_limit,
                    )
                    media = repaired_media
                except service.XPostError:
                    raise
                except Exception as exc:
                    raise _repair_error(exc) from None
                print(json.dumps({
                    "event": "x_post_deferred_drama_media_repair_ready",
                    "queue_id": int(queue["id"]), "log_id": int(log["id"]),
                    "job_key": job_key, "profile": profile,
                    "duration_policy": duration_policy, "trigger_code": probe_error.code,
                    "source_sha256": source_sha256, "source_size": source_size,
                    "output_sha256": final_sha256, "output_size": final_size,
                    "duration": final_probe["duration"],
                    "width": final_probe["width"], "height": final_probe["height"],
                }, ensure_ascii=False, sort_keys=True), flush=True)
            prepared = PreparedDeferredDramaMedia(
                queue_identity=_queue_identity(queue), work_dir=work_dir,
                media=MappingProxyType(dict(media)),
            )
        except service.XPostError:
            raise
        except Exception:
            raise service.XPostError(
                "x_post_media_preparation_failed", "短剧媒体准备失败，尚未尝试发布", 503,
            ) from None
        yield prepared
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)


@dataclass(frozen=True)
class PreparedDurationPendingDramaMedia:
    """Request-local final media prepared before a drama route is resolved.

    ``evidence`` is safe to pass to the atomic resolver.  The local file itself
    remains an unforgeable request-local capability: it cannot be consumed by
    ``publish_canary`` until ``bind_resolved`` has checked the resolver's frozen
    queue values and delivery route.
    """

    pending_queue_identity: tuple
    work_dir: Path
    media: object
    evidence: object
    repair_audit: object
    # ``object`` keeps this module importable on the production Python 3.9
    # runtime while still allowing the immutable tuple-or-None capability.
    resolved_queue_identity: object = None

    @property
    def final_url(self):
        return str(self.evidence["material_url"])

    @property
    def final_sha256(self):
        return str(self.evidence["preflight_sha256"])

    @property
    def final_size(self):
        return int(self.evidence["preflight_size"])

    @property
    def final_duration(self):
        return float(self.evidence["preflight_duration"])

    @property
    def final_width(self):
        return int(self.evidence["preflight_width"])

    @property
    def final_height(self):
        return int(self.evidence["preflight_height"])

    def _validate_resolved_queue(self, queue):
        if (
            not isinstance(queue, dict)
            or self.pending_queue_identity != _duration_pending_identity(queue)
            or queue.get("source_type") != "drama"
            or queue.get("media_validation_mode")
            != service.MEDIA_VALIDATION_PREFLIGHT
            or str(queue.get("status", "") or "") != "queued"
            or str(queue.get("route_state", "") or "") != "resolved"
        ):
            raise service.XPostError(
                "media_preflight_changed",
                "已解析短剧队列与准备媒体身份不一致",
                409,
            )
        delivery_mode = str(queue.get("delivery_mode", "") or "")
        resolved_delivery_mode = str(
            queue.get("resolved_delivery_mode", "") or ""
        )
        relay_account_id = int(queue.get("relay_account_id") or 0)
        relay_username = str(queue.get("relay_account_username") or "")
        if delivery_mode == service.DIRECT_DELIVERY_MODE:
            valid_route = relay_account_id == 0 and not relay_username
        elif delivery_mode == service.PREMIUM_RELAY_REPOST_MODE:
            valid_route = (
                self.final_duration > service.STANDARD_MAX_DURATION_SECONDS
                and relay_account_id > 0
                and relay_account_id != int(queue.get("account_id") or 0)
                and bool(relay_username)
            )
        else:
            valid_route = False
        if resolved_delivery_mode != delivery_mode:
            valid_route = False
        if not valid_route:
            raise service.XPostError(
                "media_preflight_changed",
                "已解析短剧队列发布路线无效",
                409,
            )

        expected = self.evidence
        frozen_url = str(
            _queue_evidence_value(queue, "material_url", "final_material_url")
            or ""
        )
        frozen_sha256 = str(
            _queue_evidence_value(queue, "preflight_sha256", "final_sha256")
            or ""
        ).lower()
        try:
            frozen_size = int(
                _queue_evidence_value(queue, "preflight_size", "final_size")
            )
            frozen_width = int(
                _queue_evidence_value(queue, "preflight_width", "final_width")
            )
            frozen_height = int(
                _queue_evidence_value(queue, "preflight_height", "final_height")
            )
        except (TypeError, ValueError, OverflowError):
            frozen_size = frozen_width = frozen_height = 0
        if (
            frozen_url != expected["material_url"]
            or not secrets.compare_digest(
                frozen_sha256, str(expected["preflight_sha256"])
            )
            or frozen_size != int(expected["preflight_size"])
            or not _same_float(
                _queue_evidence_value(
                    queue, "preflight_duration", "final_duration"
                ),
                expected["preflight_duration"],
            )
            or frozen_width != int(expected["preflight_width"])
            or frozen_height != int(expected["preflight_height"])
        ):
            raise service.XPostError(
                "media_preflight_changed",
                "已解析短剧队列的最终媒体证据不一致",
                409,
            )
        for field in (
            "original_material_url",
            "media_repair_trigger_code",
            "media_repair_job_key",
            "media_repair_profile",
            "media_repair_source_sha256",
        ):
            if str(queue.get(field, "") or "") != str(expected[field]):
                raise service.XPostError(
                    "media_preflight_changed",
                    "已解析短剧队列的媒体修复证据不一致",
                    409,
                )

    def bind_resolved(self, queue):
        """Bind this capability once the resolver freezes direct/relay state."""
        self._validate_resolved_queue(queue)
        return PreparedDurationPendingDramaMedia(
            pending_queue_identity=self.pending_queue_identity,
            work_dir=self.work_dir,
            media=self.media,
            evidence=self.evidence,
            repair_audit=self.repair_audit,
            resolved_queue_identity=_queue_identity(queue),
        )

    def for_queue(self, queue, max_media_bytes):
        """Return the exact local final file for its resolved frozen queue."""
        if (
            self.resolved_queue_identity is None
            or self.resolved_queue_identity != _queue_identity(queue)
        ):
            raise service.XPostError(
                "media_preflight_changed",
                "已准备媒体尚未绑定当前短剧发布路线",
                409,
            )
        self._validate_resolved_queue(queue)
        media = _verified_local_media(
            self.work_dir, self.media, max_media_bytes
        )
        if (
            int(media.get("size") or 0) != self.final_size
            or not secrets.compare_digest(
                str(media.get("sha256", "") or "").lower(),
                self.final_sha256,
            )
            or media.get("media_kind") != "video"
        ):
            raise service.XPostError(
                "media_preflight_changed",
                "已准备短剧文件与最终冻结指纹不一致",
                409,
            )
        return media


def _duration_pending_work_root(public_root, durable_storage):
    if durable_storage is not None:
        layout = service._validate_post_storage_layout(
            public_root,
            mount_root=durable_storage.get(
                "mount_root", service.DEFAULT_STORAGE_MOUNT_ROOT
            ),
            storage_root=durable_storage.get(
                "storage_root", service.DEFAULT_STORAGE_ROOT
            ),
        )
        work_root = layout["media_work"]
        if (
            work_root.resolve(strict=True).parent != layout["storage"]
            or work_root.stat().st_dev != layout["storage"].stat().st_dev
        ):
            raise service.XPostError(
                "x_post_storage_unavailable",
                "X Post媒体工作目录无效",
                503,
            )
        return work_root
    work_root = Path(public_root).resolve().parent / "media-work"
    work_root.mkdir(parents=True, exist_ok=True)
    return work_root


@contextlib.contextmanager
def prepare_duration_pending_drama_media(
    *,
    queue,
    public_root,
    allowed_media_hosts,
    timeout=30,
    max_media_bytes=service.DEFAULT_MAX_MEDIA_BYTES,
    storage_guard=None,
    durable_storage=None,
    http_client=None,
):
    """Prepare one unresolved drama episode without a log or account token.

    The source bytes are downloaded exactly once.  Raw duration selects the GPU
    repair policy; the strict probe of the final bytes is the sole authority for
    subsequent direct-versus-relay routing.
    """
    if not isinstance(queue, dict) or queue.get("source_type") != "drama":
        raise service.XPostError(
            "invalid_request", "时长待解析媒体必须来自短剧队列", 400,
        )
    if (
        str(queue.get("status", "") or "") != "queued"
        or str(queue.get("route_state", "") or "") != "duration_pending"
        or int(queue.get("route_version") or 0)
        != service.DRAMA_DURATION_ROUTE_VERSION
        or str(queue.get("resolved_delivery_mode", "") or "")
        or queue.get("media_validation_mode")
        != service.MEDIA_VALIDATION_DEFERRED
        or str(queue.get("delivery_mode", "direct") or "direct")
        not in {service.DIRECT_DELIVERY_MODE, "duration_pending"}
        or int(queue.get("relay_account_id") or 0) != 0
        or str(queue.get("relay_account_username") or "")
        or str(queue.get("preflight_sha256") or "")
        or int(queue.get("preflight_size") or 0) != 0
        or float(queue.get("preflight_duration") or 0.0) != 0.0
        or int(queue.get("preflight_width") or 0) != 0
        or int(queue.get("preflight_height") or 0) != 0
        or any(
            str(queue.get(field, "") or "")
            for field in (
                "original_material_url",
                "media_repair_trigger_code",
                "media_repair_job_key",
                "media_repair_profile",
                "media_repair_source_sha256",
            )
        )
    ):
        raise service.XPostError(
            "x_post_drama_route_resolution_fenced",
            "短剧队列已包含发布路线或媒体执行证据",
            409,
        )
    queue_id = service._positive_int(queue.get("id"), "queue_id")
    source_url = str(queue.get("material_url", "") or "")
    if not source_url:
        raise service.XPostError(
            "invalid_media_url", "短剧源文件地址为空", 400,
        )

    work_dir = None
    try:
        try:
            max_media_bytes = min(
                service._positive_int(max_media_bytes, "素材大小上限"),
                service.DEFAULT_MAX_MEDIA_BYTES,
            )
            if callable(storage_guard):
                storage_guard()
            work_root = _duration_pending_work_root(
                public_root, durable_storage
            )
            work_dir = Path(
                tempfile.mkdtemp(
                    prefix="duration-pending-queue-%s-" % queue_id,
                    dir=str(work_root),
                )
            )
            media = service.download_media(
                source_url,
                work_dir / "source.bin",
                allowed_media_hosts,
                max_bytes=max_media_bytes,
                timeout=timeout,
                http_client=http_client,
            )
            if media.get("media_kind") != "video":
                raise service.XPostError(
                    "invalid_media_type",
                    "短剧剧集必须是视频，尚未尝试发布",
                    422,
                )
            raw_duration = _probe_raw_video_duration(
                media["path"],
                max_bytes=max_media_bytes,
                timeout=timeout,
            )
            duration_policy = (
                "premium"
                if raw_duration > service.STANDARD_MAX_DURATION_SECONDS
                else "standard"
            )
            duration_limit = (
                service.PREMIUM_MAX_DURATION_SECONDS
                if duration_policy == "premium"
                else service.STANDARD_MAX_DURATION_SECONDS
            )
            from scripts.x_post_daily_runner import (
                REPAIRABLE_MEDIA_CODES,
                _media_fingerprint,
                _repair_job_key,
                _verify_repaired_download,
            )

            source_sha256, source_size = _media_fingerprint(media)
            repair_applied = False
            repair_trigger = ""
            repair_job_key = ""
            repair_profile = ""
            final_url = source_url
            try:
                final_probe = service.probe_media(
                    media["path"],
                    max_bytes=max_media_bytes,
                    timeout=timeout,
                    max_duration_seconds=duration_limit,
                )
                final_sha256, final_size = source_sha256, source_size
            except service.XPostError as probe_error:
                repair_trigger = str(getattr(probe_error, "code", "") or "")
                if repair_trigger not in REPAIRABLE_MEDIA_CODES:
                    raise
                repair_client, repair_profile = _repair_client_from_env(
                    max_media_bytes
                )
                if repair_client is None:
                    raise
                repair_item = {
                    "material_id": queue["material_id"],
                    "pool_item_id": queue["drama_pool_item_id"],
                }
                try:
                    repair_job_key = _repair_job_key(
                        repair_item,
                        source_sha256,
                        repair_profile,
                        duration_policy,
                    )
                    repaired = repair_client.repair(
                        {
                            **repair_item,
                            "job_key": repair_job_key,
                            "source_url": source_url,
                            "source_sha256": source_sha256,
                            "source_size": source_size,
                            "trigger_code": repair_trigger,
                            "profile": repair_profile,
                            "duration_policy": duration_policy,
                        }
                    )
                    if (
                        repaired.get("job_key") != repair_job_key
                        or repaired.get("profile") != repair_profile
                        or repaired.get("duration_policy", duration_policy)
                        != duration_policy
                    ):
                        raise service.XPostError(
                            "x_post_media_repair_invalid_response",
                            "短剧修复身份或策略不一致，尚未尝试发布",
                            502,
                        )
                    final_url = str(repaired.get("output_url", "") or "")
                    Path(media["path"]).unlink()
                    repaired_media = service.download_media(
                        final_url,
                        work_dir / "repaired.bin",
                        allowed_media_hosts,
                        max_bytes=max_media_bytes,
                        timeout=timeout,
                        http_client=http_client,
                    )
                    if repaired_media.get("media_kind") != "video":
                        raise service.XPostError(
                            "invalid_media_type",
                            "短剧修复结果不是视频，尚未尝试发布",
                            422,
                        )
                    repaired_probe = service.probe_media(
                        repaired_media["path"],
                        max_bytes=max_media_bytes,
                        timeout=timeout,
                        max_duration_seconds=duration_limit,
                    )
                    final_sha256, final_size, final_probe = (
                        _verify_repaired_download(
                            repaired,
                            repaired_media,
                            repaired_probe,
                            max_duration_seconds=duration_limit,
                        )
                    )
                    media = repaired_media
                    repair_applied = True
                except service.XPostError:
                    raise
                except Exception as exc:
                    raise _repair_error(exc) from None
                print(
                    json.dumps(
                        {
                            "event": (
                                "x_post_duration_pending_drama_media_repair_ready"
                            ),
                            "queue_id": queue_id,
                            "job_key": repair_job_key,
                            "profile": repair_profile,
                            "duration_policy": duration_policy,
                            "trigger_code": repair_trigger,
                            "source_sha256": source_sha256,
                            "source_size": source_size,
                            "output_sha256": final_sha256,
                            "output_size": final_size,
                            "duration": final_probe["duration"],
                            "width": final_probe["width"],
                            "height": final_probe["height"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    flush=True,
                )

            final_duration = float(final_probe["duration"])
            final_width = int(final_probe["width"])
            final_height = int(final_probe["height"])
            evidence = MappingProxyType(
                {
                    "material_url": final_url,
                    "original_material_url": (
                        source_url if repair_applied else ""
                    ),
                    "media_repair_trigger_code": (
                        repair_trigger if repair_applied else ""
                    ),
                    "media_repair_job_key": (
                        repair_job_key if repair_applied else ""
                    ),
                    "media_repair_profile": (
                        repair_profile if repair_applied else ""
                    ),
                    "media_repair_source_sha256": (
                        source_sha256 if repair_applied else ""
                    ),
                    "media_validation_mode": service.MEDIA_VALIDATION_PREFLIGHT,
                    "preflight_sha256": final_sha256,
                    "preflight_size": final_size,
                    "preflight_duration": final_duration,
                    "preflight_width": final_width,
                    "preflight_height": final_height,
                }
            )
            repair_audit = MappingProxyType(
                {
                    "applied": repair_applied,
                    "duration_policy": duration_policy,
                    "raw_duration": raw_duration,
                    "source_url": source_url,
                    "source_sha256": source_sha256,
                    "source_size": source_size,
                    "trigger_code": repair_trigger if repair_applied else "",
                    "job_key": repair_job_key if repair_applied else "",
                    "profile": repair_profile if repair_applied else "",
                    "output_url": final_url,
                    "output_sha256": final_sha256,
                    "output_size": final_size,
                    "output_duration": final_duration,
                    "output_width": final_width,
                    "output_height": final_height,
                }
            )
            prepared = PreparedDurationPendingDramaMedia(
                pending_queue_identity=_duration_pending_identity(queue),
                work_dir=work_dir,
                media=MappingProxyType(dict(media)),
                evidence=evidence,
                repair_audit=repair_audit,
            )
        except service.XPostError:
            raise
        except Exception:
            raise service.XPostError(
                "x_post_media_preparation_failed",
                "短剧时长路由媒体准备失败，尚未尝试发布",
                503,
            ) from None
        yield prepared
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
