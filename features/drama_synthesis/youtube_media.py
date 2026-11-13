"""Credential-free YouTube media data plane for the Hong Kong worker."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping
from urllib.parse import urlsplit

from .youtube import YouTubeHTTPClient, YouTubeHTTPError


UPLOAD_SESSION_HOSTS = frozenset({"www.googleapis.com", "youtube.googleapis.com", "upload.youtube.com"})


class YouTubeMediaExecutorService:
    def __init__(self, root: str, allowed_source_hosts: Iterable[str], *, ffprobe: str = "/usr/bin/ffprobe", timeout: int = 7200):
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError("YouTube media root must be absolute")
        self.allowed_source_hosts = tuple(str(item).strip().lower() for item in allowed_source_hosts if str(item).strip())
        if not self.allowed_source_hosts:
            raise ValueError("YouTube source allowlist is required")
        self.ffprobe = str(ffprobe)
        self.client = YouTubeHTTPClient(timeout=max(60, int(timeout)))

    @staticmethod
    def _task_id(payload: Mapping[str, Any]) -> int:
        value = payload.get("task_id")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise YouTubeHTTPError("youtube_media_task_invalid", "YouTube媒体任务无效", status=400)
        return value

    def _source(self, task_id: int) -> Path:
        return self.root / ("task-%d" % task_id) / "source.mp4"

    @staticmethod
    def _fingerprint(source: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest(), source.stat().st_size

    def prepare(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        task_id = self._task_id(payload)
        source_url = str(payload.get("source_url") or "")
        source = self._source(task_id)
        if not source.is_file():
            source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.client.download(source_url, source, allowed_hosts=self.allowed_source_hosts)
        sha256, size = self._fingerprint(source)
        try:
            probe = subprocess.run(
                [self.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(source)],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60,
            )
            duration_ms = int(float(json.loads(probe.stdout)["format"]["duration"]) * 1000)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            raise YouTubeHTTPError("youtube_source_probe_failed", "视频素材校验失败") from None
        if duration_ms <= 0:
            raise YouTubeHTTPError("youtube_source_probe_failed", "视频素材校验失败")
        return {"ok": True, "sha256": sha256, "size": size, "duration_ms": duration_ms}

    def upload(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        task_id = self._task_id(payload)
        source = self._source(task_id)
        if not source.is_file():
            raise YouTubeHTTPError("youtube_media_source_missing", "香港节点视频缓存不存在", status=409)
        session_uri = str(payload.get("session_uri") or "")
        parsed = urlsplit(session_uri)
        if parsed.scheme != "https" or parsed.hostname not in UPLOAD_SESSION_HOSTS or parsed.username or parsed.password or parsed.fragment:
            raise YouTubeHTTPError("youtube_upload_session_denied", "YouTube上传会话地址无效", status=400)
        expected_sha = str(payload.get("sha256") or "")
        expected_size = payload.get("size")
        sha256, size = self._fingerprint(source)
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha) or isinstance(expected_size, bool) or not isinstance(expected_size, int):
            raise YouTubeHTTPError("youtube_media_fingerprint_invalid", "YouTube媒体指纹无效", status=400)
        if expected_sha != sha256 or expected_size != size:
            raise YouTubeHTTPError("youtube_media_source_changed", "香港节点视频缓存已变化", status=409, unknown=True)
        offset = payload.get("offset", 0)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset > size:
            raise YouTubeHTTPError("youtube_upload_offset_invalid", "YouTube上传偏移无效", status=400)
        return {"ok": True, **self.client.upload(session_uri, source, offset)}

    def cleanup(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        task_id = self._task_id(payload)
        shutil.rmtree(self.root / ("task-%d" % task_id), ignore_errors=True)
        return {"ok": True}
