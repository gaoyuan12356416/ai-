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
import os
import re
import secrets
import shutil
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
_ENV_PREFIX = "X_POST_DEFERRED_DRAMA_REPAIR_"


def _queue_identity(queue):
    return tuple(str(queue.get(field, "") or "") for field in _QUEUE_IDENTITY_FIELDS)


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
