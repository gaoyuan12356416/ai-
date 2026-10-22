"""Bounded private-COS/media transport and independently verified output receipts."""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit

import requests

from features.drama_synthesis.async_runtime import emit_progress
from features.drama_synthesis.cos_upload import resume_upload, SHA_HEADER, SIZE_HEADER, BINDING_HEADER
from features.drama_synthesis.local_checkpoint import atomic_write_record, file_fingerprint, read_record
from features.drama_synthesis.media_pipeline import download_episode
from .recipe import canonical_json, error, source_locator


def header_map(value):
    return {str(k).lower(): v for k, v in value.items()}


def verified_source_download(source, path, *, client, bucket, allowed_hosts, region="ap-hongkong"):
    identity = source["identity"]
    url = source["url"]
    hosts = set(allowed_hosts)
    if identity.get("bucket"):
        if identity["bucket"] != bucket:
            raise error("video_editing_source_forbidden", "素材存储桶不在允许范围内", 403)
        if not identity["key"].startswith("video-editing/") and source["kind"] == "asset":
            raise error("video_editing_source_forbidden", "素材存储路径不在允许范围内", 403)
        # Short lived transport credentials are generated on GPU, never stored in
        # the recipe/download checkpoint/public DTO or passed back to the CPU.
        url = client.get_presigned_url(Method="GET", Bucket=bucket, Key=identity["key"], Expired=3600)
        hosts.add("%s.cos.%s.myqcloud.com" % (bucket, region))
    source_locator(url)
    if urlsplit(url).hostname.lower() not in hosts:
        raise error("video_editing_source_forbidden", "视频来源域名不在允许范围内", 403)
    expected_etag = str(identity.get("etag", "")).strip('"')
    expected_size = identity["size_bytes"]

    class VerifiedSession(requests.Session):
        def get(self, target, **kwargs):
            source_locator(target)
            if urlsplit(target).hostname.lower() not in hosts:
                raise error("video_editing_source_forbidden", "视频来源域名不在允许范围内", 403)
            kwargs["allow_redirects"] = False
            headers = dict(kwargs.get("headers") or {})
            if expected_etag:
                headers["If-Match"] = '"' + expected_etag + '"'
            kwargs["headers"] = headers
            response = super().get(target, **kwargs)
            if response.status_code in (200, 206):
                got = response.headers.get("ETag", "").strip('"')
                total = response.headers.get("Content-Length")
                if response.status_code == 206:
                    match = re.fullmatch(r"bytes \d+-\d+/(\d+)", response.headers.get("Content-Range", ""))
                    total = match[1] if match else None
                if (expected_etag and got != expected_etag) or total != str(expected_size):
                    response.close()
                    raise error("video_editing_source_changed", "素材版本已变化，请重新选择素材", 409)
            return response

    frozen = {"identity": identity}
    if not identity.get("bucket"):
        frozen["locator"] = source_locator(source["url"])
    result = download_episode(url, path, identity=canonical_json(frozen), session_factory=VerifiedSession,
        progress_callback=lambda done, total: emit_progress("downloading", download_bytes=done, total_bytes=total))
    if result["size_bytes"] != expected_size or (expected_etag and result.get("etag", "").strip('"') != expected_etag):
        raise error("video_editing_source_changed", "素材版本已变化，请重新选择素材", 409)
    if identity.get("sha256") and result["sha256"] != identity["sha256"]:
        raise error("video_editing_source_changed", "素材内容指纹不一致，已停止制作", 409)
    return result


def upload_private(client, *, bucket, key, path, checkpoint, metadata=None, content_type="video/mp4"):
    emit_progress("uploading")
    class MetadataClient:
        def __getattr__(self, name):
            return getattr(client, name)

        def create_multipart_upload(self, **kwargs):
            metadata = dict(kwargs.get("Metadata") or {})
            metadata["x-cos-meta-sha256"] = metadata[SHA_HEADER]
            metadata["x-cos-meta-size"] = metadata[SIZE_HEADER]
            kwargs["Metadata"] = metadata
            return client.create_multipart_upload(**kwargs)

    receipt = resume_upload(MetadataClient(), bucket=bucket, key=key, path=path, checkpoint_path=checkpoint,
        content_type=content_type, acl="private",
        progress_callback=lambda done, total: emit_progress("uploading", uploaded_bytes=done, upload_bytes_total=total))
    return {**receipt, **(metadata or {})}


def verify_receipt(client, artifact, *, source=False):
    try:
        headers = header_map(client.head_object(Bucket=artifact["bucket"], Key=artifact["key"]))
        if int(headers["content-length"]) != artifact["size_bytes"]:
            raise ValueError()
        if artifact.get("etag") and headers.get("etag", "").strip('"') != artifact["etag"].strip('"'):
            raise ValueError()
        if not source and (headers.get(SHA_HEADER) != artifact["sha256"]
            or headers.get(SIZE_HEADER) != str(artifact["size_bytes"])
            or headers.get(BINDING_HEADER) != artifact["binding"]):
            raise ValueError()
        return True
    except Exception:
        raise error("gpu_result_cache_unverified", "已有成片暂时无法校验，已停止重制", 503) from None


def checked_workdir(root, job_id):
    root = Path(root).absolute()
    target = root / job_id
    if not re.fullmatch(r"ve_[0-9a-f]{32}", job_id) or target.is_symlink() or root.is_symlink():
        raise error("video_editing_path_invalid", "剪辑临时目录不安全", 503)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.resolve().parent != root.resolve():
        raise error("video_editing_path_invalid", "剪辑临时目录不安全", 503)
    target.mkdir(mode=0o700, exist_ok=True)
    return target


def remove_workdir(root, job_id):
    root = Path(root).absolute()
    target = root / job_id
    if (not re.fullmatch(r"ve_[0-9a-f]{32}", job_id) or root.is_symlink() or target.is_symlink()
            or target.resolve().parent != root.resolve()):
        raise error("video_editing_path_invalid", "剪辑临时目录不安全", 503)
    if target.exists():
        shutil.rmtree(target)
        if target.exists():
            raise error("video_editing_cleanup_failed", "剪辑临时文件清理失败", 503)
