"""Prepare queue-frozen random media outside the X credential lock."""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from . import service
from .random_template_store import PreparationStore, canonical
from .publish_media_repair import _queue_identity, _verified_local_media

PROFILE = "x-random-overlay-h264-720x1280-v1"
PREPARE_PATH = "/internal/x-post-random-template"


def queue_identity(queue):
    return _queue_identity(queue) + tuple(str(queue.get(key, "")) for key in (
        "random_template_enabled", "preflight_sha256", "preflight_size", "preflight_duration",
        "schedule_run_id", "manual_run_id", "route_version", "account_drama_language"))


@dataclass(frozen=True)
class PreparedRandomTemplateMedia:
    identity: tuple
    work_dir: Path
    media: object

    def for_queue(self, queue, max_media_bytes):
        if not queue.get("random_template_enabled") or queue_identity(queue) != self.identity:
            raise service.XPostError("x_random_identity_changed", "随机成片与冻结队列不一致", 409)
        return _verified_local_media(self.work_dir, dict(self.media), max_media_bytes)


class RandomTemplateClient:
    def __init__(self, *, connection_factory=http.client.HTTPConnection, environ=None):
        env = os.environ if environ is None else environ
        endpoint = urlsplit(env.get("X_POST_DEFERRED_DRAMA_REPAIR_URL", ""))
        self.token = env.get("X_POST_DEFERRED_DRAMA_REPAIR_TOKEN", "")
        self.connection_factory = connection_factory
        if (endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.port != 18820
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                or endpoint.path != "/internal/x-post-media-repair"
                or not self.token or any(ord(c) < 32 for c in self.token)):
            raise service.XPostError("x_random_configuration_missing", "随机模板制作服务配置不可用", 503)

    def prepare(self, request):
        conn = self.connection_factory("127.0.0.1", 18820, timeout=7800)
        try:
            conn.request("POST", PREPARE_PATH, canonical(request).encode("utf-8"),
                         {"Authorization": "Bearer " + self.token, "Content-Type": "application/json", "Connection": "close"})
            response = conn.getresponse()
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("oversize response")
            if response.status != 200:
                raise service.XPostError("x_random_preparation_failed", "随机模板制作失败，未尝试发布", 502)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("invalid response")
            return value
        except service.XPostError:
            raise
        except Exception:
            raise service.XPostError("x_random_service_unavailable", "随机模板制作服务暂不可用，未尝试发布", 503) from None
        finally:
            conn.close()


def validate_receipt(request, result):
    for key in ("job_key", "queue_id", "profile", "source_sha256", "source_size", "asset_manifest_sha256"):
        if result.get(key) != request[key]:
            raise service.XPostError("x_random_receipt_invalid", "随机成片身份校验失败", 502)
    try:
        size = result["output_size"]
        duration = float(result["probe"]["duration"])
        parsed = urlsplit(result["output_url"])
        recipe = result["recipe"]
        valid = (type(size) is int and 0 < size <= service.DEFAULT_MAX_MEDIA_BYTES
                 and re.fullmatch(r"[a-f0-9]{64}", result["output_sha256"])
                 and parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
                 and math.isfinite(duration) and abs(duration - request["source_duration"]) <= 0.05
                 and duration <= request["max_duration_seconds"]
                 and (duration > 140) == (request["source_duration"] > 140)
                 and recipe["asset_set_sha256"] == request["asset_manifest_sha256"])
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise service.XPostError("x_random_receipt_invalid", "随机成片指纹或时长校验失败", 502)


@contextlib.contextmanager
def prepare_random_template(*, queue, log, account, db_path, public_root, allowed_media_hosts,
                            prepared_media=None, max_media_bytes=service.DEFAULT_MAX_MEDIA_BYTES,
                            timeout=30, storage_guard=None, durable_storage=None,
                            auto_template=False, client=None, environ=None, downloader=None, prober=None):
    if not queue.get("random_template_enabled"):
        yield prepared_media
        return
    if (log.get("status") != "reserved" or log.get("unknown_outcome")
            or int(log.get("attempt_count") or 0) != 0 or int(log.get("queue_id") or 0) != int(queue["id"])):
        raise service.XPostError("x_post_retry_requires_review", "已有发布尝试，禁止重新制作", 409)
    source_account = queue["relay_account_id"] if queue.get("delivery_mode") == service.PREMIUM_RELAY_REPOST_MODE else queue["account_id"]
    if int(account.get("id") or 0) != int(source_account):
        raise service.XPostError("x_post_account_mismatch", "随机制作账号与冻结来源不一致", 409)
    env = os.environ if environ is None else environ
    downloader, prober = downloader or service.download_media, prober or service.probe_media
    if callable(storage_guard):
        storage_guard()
    if durable_storage is not None:
        layout = service._validate_post_storage_layout(public_root,
                    mount_root=durable_storage.get("mount_root", service.DEFAULT_STORAGE_MOUNT_ROOT),
                    storage_root=durable_storage.get("storage_root", service.DEFAULT_STORAGE_ROOT))
        work_root = layout["media_work"]
    else:
        work_root = Path(public_root).resolve().parent / "media-work"
        work_root.mkdir(parents=True, exist_ok=True)
    receipt_store = PreparationStore(db_path)
    with tempfile.TemporaryDirectory(prefix="random-%s-" % queue["id"], dir=work_root) as temp:
        work_dir = Path(temp)
        source = (prepared_media.for_queue(queue, max_media_bytes) if prepared_media is not None else
                  downloader(queue["material_url"], work_dir / "source.bin", allowed_media_hosts,
                             max_bytes=max_media_bytes, timeout=timeout))
        if queue.get("media_validation_mode") != service.MEDIA_VALIDATION_DEFERRED and (
                source["sha256"] != queue.get("preflight_sha256") or source["size"] != queue.get("preflight_size")):
            raise service.XPostError("media_preflight_changed", "随机制作前源文件指纹已变化", 409)
        if source.get("media_kind") == "image":
            yield prepared_media
            return
        limit = 14400 if service._account_has_premium_video_entitlement(account) else 140
        probe = prober(source["path"], max_bytes=max_media_bytes, timeout=timeout, max_duration_seconds=limit)
        duration = float(probe["duration"])
        existing = receipt_store.get(int(queue["id"]))
        manifest = existing["request"]["asset_manifest_sha256"] if existing else env.get("X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256", "")
        if not re.fullmatch(r"[a-f0-9]{64}", manifest):
            raise service.XPostError("x_random_configuration_missing", "随机模板素材目录配置不可用", 503)
        job = "x-random-v1-" + hashlib.sha256(canonical({"queue": queue_identity(queue), "sha256": source["sha256"]}).encode()).hexdigest()
        request = {"job_key": job, "queue_id": int(queue["id"]), "content_id": str(queue["content_id"]),
                   "source_url": str(source.get("source_url") or queue["material_url"]),
                   "source_sha256": source["sha256"], "source_size": source["size"], "source_duration": duration,
                   "profile": PROFILE, "asset_manifest_sha256": manifest,
                   "max_duration_seconds": existing["request"]["max_duration_seconds"] if existing else limit}
        try:
            record = receipt_store.freeze(int(queue["id"]), request)
            result = record["result"] if record["status"] == "ready" else (client or RandomTemplateClient(environ=env)).prepare(request)
            validate_receipt(request, result)
            # The caller owns any deferred source. Release only this context's
            # own source before downloading the result to bound disk use.
            if prepared_media is None:
                Path(source["path"]).unlink()
            media = downloader(result["output_url"], work_dir / "prepared.mp4", allowed_media_hosts,
                               max_bytes=max_media_bytes, timeout=timeout)
            if (media.get("media_kind") != "video" or media["sha256"] != result["output_sha256"]
                    or media["size"] != result["output_size"]):
                raise service.XPostError("x_random_fingerprint_mismatch", "随机成片文件指纹不一致", 502)
            final = prober(media["path"], max_bytes=max_media_bytes, timeout=timeout, max_duration_seconds=limit)
            if (abs(float(final["duration"]) - duration) > 0.05
                    or (float(final["duration"]) > 140) != (duration > 140)
                    or (final["width"], final["height"]) != (720, 1280)):
                raise service.XPostError("x_random_output_invalid", "随机成片参数不符合发布要求", 502)
            receipt_store.complete(int(queue["id"]), result)
        except service.XPostError as exc:
            receipt_store.fail(int(queue["id"]), exc.code)
            raise
        except Exception:
            receipt_store.fail(int(queue["id"]), "x_random_preparation_failed")
            raise service.XPostError("x_random_preparation_failed", "随机模板制作或回执校验失败，未尝试发布", 503) from None
        yield PreparedRandomTemplateMedia(queue_identity(queue), work_dir, MappingProxyType(dict(media)))
