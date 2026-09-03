"""Server-only YouTube channel discovery and resumable publishing.

The worker deliberately separates confirmed video and comment state.  Any
ambiguous network outcome is terminal ``unknown`` until an operator reconciles
it; the code never creates a replacement video or comment automatically.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Union
from urllib.parse import quote, urlencode, urlsplit

import requests

from .core import (
    COMMENT_SCOPE,
    DramaSynthesisError,
    DramaSynthesisStore,
    is_youtube_canary,
    normalize_channel_scopes,
    scope_capabilities,
)


TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
COMMENTS_URL = "https://www.googleapis.com/youtube/v3/commentThreads"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,32}")


def _json_object_from_hex(value: Any) -> Dict[str, Any]:
    """Decode the exact HEX projection, not mysql batch-escaped JSON text.

    Invalid transport, UTF-8 or non-object JSON is ineligible. Never fall back
    to raw JSON or unescape scope/secret strings a second time.
    """
    if type(value) is not str or not value or len(value) % 2 or re.fullmatch(r"[0-9A-Fa-f]+", value) is None:
        return {}
    try:
        decoded = json.loads(bytes.fromhex(value).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _client_config(credentials: Mapping[str, Any]) -> Dict[str, Any]:
    value = credentials.get("web") or credentials.get("installed") or credentials
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class YouTubeCredential:
    account_id: str
    channel_local_id: str
    channel_id: str
    channel_name: str
    channel_status: int
    scopes: frozenset[str]
    refresh_token: str
    client_id: str
    client_secret: str

    @property
    def capabilities(self) -> Dict[str, bool]:
        value = scope_capabilities(self.scopes)
        value["refreshable"] = bool(self.refresh_token and self.client_id and self.client_secret)
        value["eligible"] = bool(
            self.channel_status == 1
            and value["refreshable"]
            and value["upload_eligible"]
            and value["identity_eligible"]
        )
        return value


class YouTubeCredentialRepository:
    """Read token JSON into process memory and return safe DTOs only."""

    def __init__(
        self,
        query_runner: Callable[[str], Sequence[Sequence[Any]]],
        *,
        schema: str = "kunlunads_dev",
        identity_probe: Optional[Callable[[YouTubeCredential], bool]] = None,
    ):
        if not callable(query_runner) or not re.fullmatch(r"[A-Za-z0-9_]+", schema):
            raise ValueError("invalid YouTube credential repository")
        self.query_runner = query_runner
        self.schema = schema
        self.identity_probe = identity_probe

    def _query(self, where: str) -> list[YouTubeCredential]:
        sql = f"""
            SELECT CAST(ch.id AS CHAR),COALESCE(ch.channel_id,''),COALESCE(ch.channel_name,''),
                   CAST(COALESCE(ch.channel_status,0) AS CHAR),CAST(a.id AS CHAR),
                   HEX(COALESCE(a.account_token,'')),HEX(COALESCE(a.account_credentials,''))
              FROM `{self.schema}`.ads_youtube_channels ch
              JOIN `{self.schema}`.ads_youtube_accounts a ON a.channel_id=ch.id
             WHERE {where}
             ORDER BY ch.id,a.id DESC
        """
        rows = self.query_runner(sql)
        result = []
        seen = set()
        for row in rows:
            if len(row) < 7:
                continue
            local_id, channel_id, channel_name, channel_status, account_id, token_hex, credential_hex = row[:7]
            identity = (str(local_id), str(account_id))
            if identity in seen:
                continue
            token = _json_object_from_hex(token_hex)
            client = _client_config(_json_object_from_hex(credential_hex))
            scopes = normalize_channel_scopes(token)
            try:
                status = int(channel_status or 0)
            except (TypeError, ValueError, OverflowError):
                status = 0
            result.append(
                YouTubeCredential(
                    account_id=str(account_id or ""),
                    channel_local_id=str(local_id or ""),
                    channel_id=str(channel_id or ""),
                    channel_name=str(channel_name or "")[:200],
                    channel_status=status,
                    scopes=scopes,
                    refresh_token=str(token.get("refresh_token") or ""),
                    client_id=str(client.get("client_id") or ""),
                    client_secret=str(client.get("client_secret") or ""),
                )
            )
            seen.add(identity)
        return result

    @staticmethod
    def _decimal_id(value: Any, label: str) -> str:
        text = str(value or "").strip()
        if not re.fullmatch(r"[1-9][0-9]{0,18}", text) or int(text) > 9_223_372_036_854_775_807:
            raise DramaSynthesisError("youtube_identifier_invalid", "YouTube%s标识无效" % label, 400)
        return text

    def list_for_app(self, app_id: str) -> list[Dict[str, Any]]:
        app_id = self._decimal_id(app_id, "产品")
        rows = self._query("CAST(ch.app_id AS UNSIGNED)=" + app_id)
        items = []
        seen_channels = set()
        for row in rows:
            caps = row.capabilities
            if not caps["eligible"] or row.channel_local_id in seen_channels or self.identity_probe is None:
                continue
            try:
                verified = bool(self.identity_probe(row))
            except Exception:
                verified = False
            if not verified:
                continue
            items.append(
                {
                    "channel_local_id": row.channel_local_id,
                    "channel_id": row.channel_id,
                    "channel_name": row.channel_name,
                    "youtube_account_id": row.account_id,
                    "upload_eligible": True,
                    "identity_eligible": True,
                    "comment_eligible": bool(caps["comment_eligible"]),
                }
            )
            seen_channels.add(row.channel_local_id)
        return items

    def credential(self, *, app_id: str, channel_local_id: str, account_id: str, expected_channel_id: str) -> YouTubeCredential:
        app_id = self._decimal_id(app_id, "产品")
        channel_local_id = self._decimal_id(channel_local_id, "频道")
        account_id = self._decimal_id(account_id, "账号")
        where = (
            "CAST(ch.app_id AS UNSIGNED)=" + app_id
            + " AND CAST(ch.id AS UNSIGNED)=" + channel_local_id
            + " AND CAST(a.id AS UNSIGNED)=" + account_id
        )
        rows = self._query(where)
        if len(rows) != 1:
            raise DramaSynthesisError("youtube_channel_not_eligible", "YouTube频道授权不可用", 409)
        credential = rows[0]
        if not secrets.compare_digest(credential.channel_id.encode(), str(expected_channel_id).encode()) or not credential.capabilities["eligible"]:
            raise DramaSynthesisError("youtube_channel_not_eligible", "YouTube频道授权不可用", 409)
        return credential


class YouTubeHTTPError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 0, unknown: bool = False, retryable: bool = False):
        self.code = code
        self.status = int(status)
        self.unknown = bool(unknown)
        self.retryable = bool(retryable)
        super().__init__(message)


class YouTubeHTTPClient:
    def __init__(self, *, session_factory=requests.Session, timeout: int = 120):
        self.session_factory = session_factory
        self.timeout = max(30, min(int(timeout), 600))

    def refresh_access_token(self, credential: YouTubeCredential) -> str:
        if not credential.capabilities["eligible"]:
            raise YouTubeHTTPError("youtube_channel_not_eligible", "YouTube频道授权不可用", status=409)
        session = self.session_factory()
        session.trust_env = False
        try:
            response = session.post(
                TOKEN_URL,
                data={
                    "client_id": credential.client_id,
                    "client_secret": credential.client_secret,
                    "refresh_token": credential.refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=60,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise YouTubeHTTPError("youtube_token_refresh_unavailable", "YouTube授权刷新暂时不可用", retryable=True) from None
        finally:
            session.close()
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        token = str(payload.get("access_token") or "")
        if response.status_code >= 500:
            raise YouTubeHTTPError("youtube_token_refresh_unavailable", "YouTube授权刷新暂时不可用", status=response.status_code, retryable=True)
        if response.status_code >= 400 or not token:
            raise YouTubeHTTPError("youtube_token_refresh_failed", "YouTube授权已失效，请重新授权", status=response.status_code)
        return token

    def verify_channel_identity(self, token: str, expected_channel_id: str) -> None:
        session = self.session_factory()
        session.trust_env = False
        try:
            response = session.get(
                CHANNELS_URL + "?" + urlencode({"part": "id", "mine": "true"}),
                headers={"Authorization": "Bearer " + token},
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise YouTubeHTTPError(
                "youtube_channel_identity_unavailable",
                "YouTube频道身份核验暂时不可用",
                retryable=True,
            ) from None
        finally:
            session.close()
        if response.status_code >= 500:
            raise YouTubeHTTPError(
                "youtube_channel_identity_unavailable",
                "YouTube频道身份核验暂时不可用",
                status=response.status_code,
                retryable=True,
            )
        if response.status_code in (401, 403):
            raise YouTubeHTTPError(
                "youtube_channel_identity_unauthorized",
                "YouTube频道身份核验未获授权",
                status=response.status_code,
            )
        if response.status_code != 200:
            raise YouTubeHTTPError(
                "youtube_channel_identity_failed",
                "YouTube频道身份核验失败",
                status=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        items = payload.get("items") if isinstance(payload, Mapping) else None
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], Mapping):
            raise YouTubeHTTPError("youtube_channel_identity_mismatch", "YouTube频道身份不匹配", status=409)
        actual_channel_id = str(items[0].get("id") or "")
        if not actual_channel_id or not secrets.compare_digest(actual_channel_id.encode(), str(expected_channel_id).encode()):
            raise YouTubeHTTPError("youtube_channel_identity_mismatch", "YouTube频道身份不匹配", status=409)

    def download(
        self,
        url: str,
        target: Path,
        *,
        allowed_hosts: Iterable[str],
        max_bytes: int = 16 * 1024 * 1024 * 1024,
        heartbeat: Optional[Callable[[], Any]] = None,
    ) -> int:
        parsed = urlsplit(str(url or ""))
        hosts = {str(item or "").strip().lower() for item in allowed_hosts if str(item or "").strip()}
        if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in hosts or parsed.username or parsed.password or parsed.fragment:
            raise YouTubeHTTPError("youtube_source_host_denied", "视频来源地址不在允许范围", status=400)
        session = self.session_factory()
        session.trust_env = False
        total = 0
        temporary = target.with_suffix(".download.tmp")
        temporary.unlink(missing_ok=True)
        try:
            response = session.get(url, stream=True, timeout=self.timeout, allow_redirects=False)
            if response.status_code != 200:
                raise YouTubeHTTPError("youtube_source_download_failed", "视频下载失败", status=response.status_code, retryable=response.status_code >= 500)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with temporary.open("xb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    if heartbeat is not None:
                        heartbeat()
                    total += len(chunk)
                    if total > max_bytes:
                        raise YouTubeHTTPError("youtube_source_too_large", "视频超过上传大小上限", status=413)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except YouTubeHTTPError:
            temporary.unlink(missing_ok=True)
            raise
        except DramaSynthesisError:
            temporary.unlink(missing_ok=True)
            raise
        except (OSError, requests.RequestException):
            temporary.unlink(missing_ok=True)
            raise YouTubeHTTPError("youtube_source_download_failed", "视频下载失败", retryable=True) from None
        finally:
            session.close()
        if total <= 0:
            target.unlink(missing_ok=True)
            raise YouTubeHTTPError("youtube_source_empty", "视频文件为空", status=400)
        return total

    def begin_resumable(self, token: str, *, title: str, description: str, size: int, privacy_status: str = "public") -> str:
        if privacy_status not in {"public", "unlisted"}:
            raise YouTubeHTTPError("youtube_privacy_invalid", "YouTube视频隐私设置无效", status=400)
        query = {"uploadType": "resumable", "part": "snippet,status"}
        if privacy_status == "unlisted":
            query["notifySubscribers"] = "false"
        params = urlencode(query)
        body = {"snippet": {"title": title, "description": description}, "status": {"privacyStatus": privacy_status}}
        session = self.session_factory()
        session.trust_env = False
        try:
            response = session.post(
                UPLOAD_URL + "?" + params,
                json=body,
                headers={
                    "Authorization": "Bearer " + token,
                    "X-Upload-Content-Length": str(int(size)),
                    "X-Upload-Content-Type": "video/mp4",
                },
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise YouTubeHTTPError("youtube_resumable_create_unknown", "创建YouTube上传会话结果未知", unknown=True) from None
        finally:
            session.close()
        if response.status_code >= 500:
            raise YouTubeHTTPError("youtube_resumable_create_unknown", "创建YouTube上传会话结果未知", status=response.status_code, unknown=True)
        location = str(response.headers.get("Location") or "")
        parsed = urlsplit(location)
        if response.status_code not in (200, 201) or parsed.scheme != "https" or not parsed.hostname:
            raise YouTubeHTTPError("youtube_resumable_create_failed", "创建YouTube上传会话失败", status=response.status_code)
        return location

    @staticmethod
    def _upload_response(response: requests.Response, size: int) -> Dict[str, Any]:
        if response.status_code in (200, 201):
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            video_id = str(payload.get("id") or "")
            if not VIDEO_ID_RE.fullmatch(video_id):
                raise YouTubeHTTPError("youtube_video_identity_invalid", "YouTube返回的视频ID无效", status=response.status_code, unknown=True)
            return {"state": "submitted", "video_id": video_id, "next_byte": int(size)}
        if response.status_code == 308:
            range_value = str(response.headers.get("Range") or "")
            match = re.fullmatch(r"bytes=0-([0-9]+)", range_value)
            next_byte = int(match.group(1)) + 1 if match else 0
            return {"state": "resume", "next_byte": next_byte}
        if response.status_code == 404:
            return {"state": "expired"}
        if response.status_code >= 500:
            raise YouTubeHTTPError("youtube_upload_unknown", "YouTube视频上传结果未知", status=response.status_code, unknown=True)
        raise YouTubeHTTPError("youtube_upload_failed", "YouTube视频上传失败", status=response.status_code)

    def query_upload(self, session_uri: str, size: int) -> Dict[str, Any]:
        session = self.session_factory()
        session.trust_env = False
        try:
            response = session.put(
                session_uri,
                data=b"",
                headers={"Content-Length": "0", "Content-Range": f"bytes */{int(size)}"},
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise YouTubeHTTPError("youtube_upload_query_unknown", "YouTube上传状态查询失败", unknown=True) from None
        finally:
            session.close()
        return self._upload_response(response, size)


    def upload(self, session_uri: str, source: Path, offset: int) -> Dict[str, Any]:
        size = source.stat().st_size
        offset = max(0, int(offset))
        if offset >= size:
            return self.query_upload(session_uri, size)
        session = self.session_factory()
        session.trust_env = False
        handle = source.open("rb")
        handle.seek(offset)
        try:
            response = session.put(
                session_uri,
                data=handle,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(size - offset),
                    "Content-Range": f"bytes {offset}-{size - 1}/{size}",
                },
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise YouTubeHTTPError("youtube_upload_unknown", "YouTube视频上传结果未知", unknown=True) from None
        finally:
            handle.close()
            session.close()
        return self._upload_response(response, size)

    def publish_comment(self, token: str, *, video_id: str, comment_text: str, channel_id: str) -> str:
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{20,30}", str(channel_id or "")):
            raise YouTubeHTTPError("youtube_comment_channel_invalid", "YouTube评论频道身份无效", status=400)
        body = {
            "snippet": {
                "channelId": channel_id,
                "videoId": video_id,
                "topLevelComment": {"snippet": {"textOriginal": comment_text}},
            }
        }
        session = self.session_factory()
        session.trust_env = False
        try:
            response = session.post(
                COMMENTS_URL + "?part=snippet",
                json=body,
                headers={"Authorization": "Bearer " + token},
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise YouTubeHTTPError("youtube_comment_unknown", "YouTube评论发布结果未知", unknown=True) from None
        finally:
            session.close()
        if response.status_code >= 500:
            raise YouTubeHTTPError("youtube_comment_unknown", "YouTube评论发布结果未知", status=response.status_code, unknown=True)
        if response.status_code not in (200, 201):
            if 200 <= response.status_code < 300:
                raise YouTubeHTTPError("youtube_comment_unknown", "YouTube评论发布结果未知", status=response.status_code, unknown=True)
            raise YouTubeHTTPError("youtube_comment_failed", "YouTube评论发布失败", status=response.status_code)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        snippet = payload.get("snippet") if isinstance(payload, Mapping) else None
        comment = snippet.get("topLevelComment") if isinstance(snippet, Mapping) else None
        comment_id = str(comment.get("id") or "") if isinstance(comment, Mapping) else ""
        if (not isinstance(snippet, Mapping) or snippet.get("channelId") != channel_id
                or snippet.get("videoId") != video_id
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", comment_id)):
            raise YouTubeHTTPError("youtube_comment_identity_unknown", "YouTube评论身份无法确认，禁止自动重发", unknown=True)
        return comment_id

    def video_status(self, token: str, video_id: str, *, expected_privacy_status: str = "public") -> Dict[str, str]:
        if expected_privacy_status not in {"public", "unlisted"}:
            raise YouTubeHTTPError("youtube_privacy_invalid", "YouTube视频隐私设置无效", status=400)
        canary = expected_privacy_status == "unlisted"
        session = self.session_factory()
        session.trust_env = False
        try:
            response = session.get(
                VIDEOS_URL + "?" + urlencode((("part", "status,processingDetails" if canary else "status"), ("id", video_id))),
                headers={"Authorization": "Bearer " + token}, timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise YouTubeHTTPError("youtube_processing_check_failed", "YouTube处理状态查询失败", retryable=True) from None
        finally:
            session.close()
        if response.status_code >= 500:
            raise YouTubeHTTPError("youtube_processing_check_failed", "YouTube处理状态查询失败", status=response.status_code, retryable=True)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        items = payload.get("items") if isinstance(payload, Mapping) else None
        if response.status_code != 200 or not isinstance(items, list) or len(items) != 1:
            raise YouTubeHTTPError("youtube_video_reconcile_unknown", "YouTube视频状态无法确认", status=response.status_code, unknown=True)
        status = items[0].get("status") if isinstance(items[0], Mapping) else None
        if not isinstance(status, Mapping):
            raise YouTubeHTTPError("youtube_video_reconcile_unknown", "YouTube视频状态无法确认", unknown=True)
        upload = str(status.get("uploadStatus") or "")
        visibility = str(status.get("privacyStatus") or "")
        if canary:
            if items[0].get("id") != video_id:
                raise YouTubeHTTPError("youtube_video_identity_conflict", "内部测试视频身份无法核验", unknown=True)
            if visibility != "unlisted":
                # Never promote a forced-private or otherwise changed canary.
                raise YouTubeHTTPError("youtube_canary_privacy_mismatch", "内部测试视频未保持不公开状态，已阻断后续操作", unknown=True)
            details = items[0].get("processingDetails")
            processing = str(details.get("processingStatus") or "") if isinstance(details, Mapping) else ""
            if upload in {"failed", "rejected", "deleted"} or processing in {"failed", "terminated"}:
                return {"state": "failed", "visibility": visibility, "processing_status": processing}
            if upload == "processed" and processing == "succeeded":
                return {"state": "published", "visibility": visibility, "processing_status": processing}
            if upload in {"uploaded", "processing", "processed"} and processing in {"processing", "succeeded"}:
                return {"state": "processing", "visibility": visibility, "processing_status": processing}
            return {"state": "unknown", "visibility": visibility, "processing_status": processing}
        if upload == "processed" and visibility == "public":
            return {"state": "published", "visibility": visibility}
        if upload in {"uploaded", "processing"}:
            return {"state": "processing", "visibility": visibility}
        if upload in {"failed", "rejected", "deleted"}:
            return {"state": "failed", "visibility": visibility}
        return {"state": "unknown", "visibility": visibility}


class YouTubeRemoteMediaExecutor:
    """Loopback client for the HK media data plane."""

    def __init__(self, base_url: str, token: str, *, timeout: int = 7200, session_factory=requests.Session):
        parsed = urlsplit(str(base_url or "").rstrip("/"))
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
            raise ValueError("YouTube media executor must use a loopback tunnel URL")
        if not str(token or ""):
            raise ValueError("YouTube media executor token is required")
        self.base_url = str(base_url).rstrip("/")
        self.token = str(token)
        self.timeout = max(60, int(timeout))
        self.session_factory = session_factory

    def _request(self, path: str, payload: Mapping[str, Any], heartbeat: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
        outcome: Dict[str, Any] = {}

        def execute() -> None:
            session = self.session_factory()
            session.trust_env = False
            try:
                outcome["response"] = session.post(
                    self.base_url + path, json=dict(payload),
                    headers={"Authorization": "Bearer " + self.token},
                    timeout=self.timeout, allow_redirects=False,
                )
            except requests.RequestException as exc:
                outcome["exception"] = exc
            finally:
                session.close()

        thread = threading.Thread(target=execute, name="youtube-hk-media", daemon=True)
        thread.start()
        while thread.is_alive():
            thread.join(20)
            if thread.is_alive() and heartbeat is not None:
                heartbeat()
        if "exception" in outcome or outcome.get("response") is None:
            raise YouTubeHTTPError("youtube_media_executor_unavailable", "香港YouTube媒体服务不可用", retryable=True)
        response = outcome["response"]
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not isinstance(body, Mapping) or body.get("ok") is not True:
            code = str(body.get("code") or "youtube_media_executor_failed") if isinstance(body, Mapping) else "youtube_media_executor_failed"
            message = str(body.get("error") or "香港YouTube媒体服务执行失败") if isinstance(body, Mapping) else "香港YouTube媒体服务执行失败"
            unknown = body.get("unknown") is True if isinstance(body, Mapping) else False
            retryable = body.get("retryable") is True if isinstance(body, Mapping) else response.status_code >= 500
            raise YouTubeHTTPError(code, message, status=response.status_code, unknown=unknown, retryable=retryable)
        return dict(body)

    def prepare(self, task_id: int, source_url: str, *, heartbeat: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
        body = self._request("/api/gpu-video/youtube-media/prepare", {"task_id": int(task_id), "source_url": source_url}, heartbeat)
        sha256, size, duration_ms = str(body.get("sha256") or ""), int(body.get("size") or 0), int(body.get("duration_ms") or 0)
        if not re.fullmatch(r"[0-9a-f]{64}", sha256) or size <= 0 or duration_ms <= 0:
            raise YouTubeHTTPError("youtube_media_executor_response_invalid", "香港YouTube媒体服务返回无效")
        return {"sha256": sha256, "size": size, "duration_ms": duration_ms}

    def upload(self, task_id: int, session_uri: str, offset: int, *, size: int, sha256: str, heartbeat: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
        body = self._request("/api/gpu-video/youtube-media/upload", {
            "task_id": int(task_id), "session_uri": session_uri, "offset": int(offset),
            "size": int(size), "sha256": str(sha256),
        }, heartbeat)
        state = str(body.get("state") or "")
        if state not in {"submitted", "resume", "expired"}:
            raise YouTubeHTTPError("youtube_media_executor_response_invalid", "香港YouTube媒体服务返回无效")
        result = {"state": state}
        if state == "submitted":
            result["video_id"] = str(body.get("video_id") or "")
            if not VIDEO_ID_RE.fullmatch(result["video_id"]):
                raise YouTubeHTTPError("youtube_media_executor_response_invalid", "香港YouTube媒体服务返回无效", unknown=True)
        if state == "resume": result["next_byte"] = int(body.get("next_byte") or 0)
        return result

    def cleanup(self, task_id: int) -> None:
        try:
            self._request("/api/gpu-video/youtube-media/cleanup", {"task_id": int(task_id)})
        except YouTubeHTTPError:
            return


class YouTubePublishEngine:
    def __init__(
        self,
        store: DramaSynthesisStore,
        credentials: YouTubeCredentialRepository,
        client: YouTubeHTTPClient,
        *,
        work_root: Union[str, os.PathLike],
        allowed_source_hosts: Iterable[str],
        ffprobe: str = "/usr/bin/ffprobe",
        media_executor: Optional[YouTubeRemoteMediaExecutor] = None,
    ):
        root = Path(work_root)
        if not root.is_absolute():
            raise ValueError("YouTube work root must be absolute")
        self.store = store
        self.credentials = credentials
        self.client = client
        self.work_root = root
        self.allowed_source_hosts = tuple(dict.fromkeys(str(item).strip().lower() for item in allowed_source_hosts if str(item).strip()))
        self.ffprobe = str(ffprobe)
        self.media_executor = media_executor
        self._canary_tick_token: Optional[tuple[int, str]] = None
        if not self.allowed_source_hosts:
            raise ValueError("YouTube source allowlist is required")

    def run_once(self, worker_id: str, *, canary_task_id: Optional[int] = None) -> Dict[str, Any]:
        worker_id = str(worker_id)
        self._canary_tick_token = None
        if canary_task_id is None:
            task = self.store.claim_youtube(worker_id, self._lease_expiry())
        else:
            task = self.store.claim_youtube_canary(worker_id, self._lease_expiry(), int(canary_task_id))
        if task is None:
            return {"ok": True, "status": "no_pending", "claimed": False}
        canary = canary_task_id is not None and is_youtube_canary(task)
        task_id = int(task["id"])
        lease_generation = int(task["lease_generation"])
        try:
            if canary_task_id is not None and not canary:
                raise YouTubeHTTPError("youtube_canary_identity_invalid", "内部测试任务身份不匹配", status=409)
            task = self._renew(task, worker_id)
            credential = self.credentials.credential(
                app_id=task["app_id"],
                channel_local_id=task["channel_local_id"],
                account_id=task["youtube_account_id"],
                expected_channel_id=task["channel_id"],
            )
            if task["comment_text"] and not credential.capabilities["comment_eligible"]:
                raise YouTubeHTTPError("youtube_comment_scope_missing", "频道授权缺少评论权限", status=409)
            task = self._renew(task, worker_id)
            token = self.client.refresh_access_token(credential)
            task = self._renew(task, worker_id)
            self.client.verify_channel_identity(token, task["channel_id"])
            if canary:
                self._canary_tick_token = (task_id, token)
            if canary and int(task.get("unknown_outcome") or 0) and not task.get("video_id"):
                # Unknown upload outcomes only query the original session.  A
                # 308/404 is not permission to send bytes or create a new one.
                task = self._renew(task, worker_id)
                state = self.client.query_upload(task["resumable_session_uri"], int(task["source_size"]))
                if state.get("state") == "submitted":
                    task = self.store.video_submitted(task_id, state["video_id"], worker_id=worker_id, lease_generation=lease_generation)
                    return {"ok": True, "status": task["status"], "task_id": task_id, "claimed": True}
                raise YouTubeHTTPError("youtube_canary_reconcile_inconclusive", "内部测试原上传会话尚无法确认，禁止重传", unknown=True)
            if task["video_state"] in {"submitted", "processing"} or (canary and task.get("video_id") and int(task.get("unknown_outcome") or 0)):
                task = self._renew(task, worker_id)
                state = self._video_status(task, token)
                if state["state"] == "published":
                    task = self.store.video_published(task_id, task["video_id"], worker_id=worker_id, lease_generation=lease_generation)
                elif state["state"] == "processing":
                    task = self.store.video_processing(task_id, worker_id=worker_id, lease_generation=lease_generation)
                    return {"ok": True, "status": "processing", "task_id": task_id, "claimed": True}
                elif state["state"] == "failed":
                    raise YouTubeHTTPError("youtube_processing_failed", "YouTube视频处理失败")
                else:
                    raise YouTubeHTTPError("youtube_video_reconcile_unknown", "YouTube视频状态无法确认", unknown=True)
            elif task["video_state"] != "published":
                task = self._publish_video(task, token, worker_id)
                return {"ok": True, "status": task["status"], "task_id": task_id, "claimed": True}
            if task["comment_status"] in {"queued", "retry", "publishing"}:
                if canary:
                    # Comment retries/restarts must also re-read privacy before
                    # any new external side effect on the confirmed video.
                    state = self._video_status(task, token)
                    if state.get("state") != "published":
                        raise YouTubeHTTPError("youtube_canary_comment_preflight_unknown", "内部测试视频处理或隐私状态无法确认，禁止评论", unknown=True)
                    if int(task.get("comment_attempt_count") or 0):
                        raise YouTubeHTTPError("youtube_canary_comment_already_attempted", "内部测试已尝试评论，禁止自动重发", unknown=True)
                task = self._renew(task, worker_id)
                self.client.verify_channel_identity(token, task["channel_id"])
                task = self._renew(task, worker_id)
                self.store.mark_comment_attempt(
                    task_id,
                    worker_id=worker_id,
                    lease_generation=lease_generation,
                )
                task = self._renew(task, worker_id)
                comment_id = self.client.publish_comment(token, video_id=task["video_id"], comment_text=task["comment_text"], channel_id=task["channel_id"])
                task = self.store.comment_published(
                    task_id,
                    comment_id,
                    worker_id=worker_id,
                    lease_generation=lease_generation,
                )
            self._cleanup_terminal(task_id)
            return {"ok": True, "status": task["status"], "task_id": task_id, "claimed": True}
        except YouTubeHTTPError as exc:
            phase = "comment" if task.get("video_state") == "published" else "video"
            failed = self._fail_claim(
                task_id,
                worker_id,
                lease_generation,
                phase=phase,
                code=exc.code,
                message=str(exc),
                unknown=exc.unknown,
                retryable=exc.retryable,
            )
            if failed is None:
                return {"ok": False, "status": "stale_claim", "task_id": task_id, "claimed": True}
            if failed["status"] in {"failed", "unknown"}:
                self._cleanup_terminal(task_id)
            return {"ok": False, "status": failed["status"], "task_id": task_id, "claimed": True}
        except DramaSynthesisError as exc:
            if exc.code == "youtube_stale_claim":
                return {"ok": False, "status": "stale_claim", "task_id": task_id, "claimed": True}
            phase = "comment" if task.get("video_state") == "published" else "video"
            persisted = self.store.youtube_task(task_id) if canary else task
            attempted = canary and bool(
                int((persisted or {}).get("comment_attempt_count" if phase == "comment" else "video_attempt_count") or 0)
            )
            failed = self._fail_claim(
                task_id,
                worker_id,
                lease_generation,
                phase=phase,
                code=exc.code,
                message=str(exc),
                unknown=attempted,
                retryable=False,
            )
            if failed is None:
                return {"ok": False, "status": "stale_claim", "task_id": task_id, "claimed": True}
            if failed["status"] in {"failed", "unknown"}:
                self._cleanup_terminal(task_id)
            return {"ok": False, "status": failed["status"], "task_id": task_id, "claimed": True}
        except Exception:
            # A programming/runtime failure after an upload or comment attempt
            # cannot be converted into a replacement publish automatically.
            phase = "comment" if task.get("video_state") == "published" else "video"
            if canary:
                # _publish_video may fail after persisting intent, before it
                # returns its local task object to this caller.
                task = self.store.youtube_task(task_id) or task
            external_attempted = bool(
                task.get("resumable_session_uri")
                or int(task.get("video_attempt_count") or 0)
                or (phase == "comment" and int(task.get("comment_attempt_count") or 0))
            )
            failed = self._fail_claim(
                task_id,
                worker_id,
                lease_generation,
                phase=phase,
                code="youtube_worker_internal_error",
                message="YouTube发布任务发生内部错误",
                unknown=external_attempted,
                retryable=not external_attempted,
            )
            if failed is None:
                return {"ok": False, "status": "stale_claim", "task_id": task_id, "claimed": True}
            if failed["status"] in {"failed", "unknown"}:
                self._cleanup_terminal(task_id)
            return {"ok": False, "status": failed["status"], "task_id": task_id, "claimed": True}

    def _video_status(self, task: Mapping[str, Any], token: str) -> Dict[str, str]:
        if not is_youtube_canary(task):
            return self.client.video_status(token, task["video_id"])
        state = self.client.video_status(token, task["video_id"], expected_privacy_status="unlisted")
        if state.get("visibility") != "unlisted":
            raise YouTubeHTTPError("youtube_canary_privacy_mismatch", "内部测试视频未保持不公开状态，已阻断后续操作", unknown=True)
        if state.get("state") == "published" and state.get("processing_status") != "succeeded":
            raise YouTubeHTTPError("youtube_canary_processing_unknown", "内部测试视频处理成功状态无法确认", unknown=True)
        return state

    def verify_canary_sync(self, task_id: int, *, token: Optional[str] = None) -> str:
        """Fresh readback before each outbox claim; reuse only this tick's OAuth."""
        task = self.store.youtube_canary_task()
        if (task is None or int(task["id"]) != int(task_id)
                or task["status"] != "published" or task["video_state"] != "published"
                or task["comment_status"] != "published" or int(task["unknown_outcome"])):
            raise YouTubeHTTPError("youtube_canary_sync_state_unconfirmed", "内部测试视频和评论尚未同时确认，禁止同步", unknown=True)
        if token is None:
            cached, self._canary_tick_token = self._canary_tick_token, None
            if cached is not None and cached[0] == int(task_id):
                token = cached[1]
            else:
                credential = self.credentials.credential(
                    app_id=task["app_id"], channel_local_id=task["channel_local_id"],
                    account_id=task["youtube_account_id"], expected_channel_id=task["channel_id"],
                )
                token = self.client.refresh_access_token(credential)
                self.client.verify_channel_identity(token, task["channel_id"])
        state = self._video_status(task, token)
        if state.get("state") != "published":
            raise YouTubeHTTPError("youtube_canary_sync_state_unconfirmed", "内部测试同步前处理成功状态无法确认", unknown=True)
        return token

    def _cleanup_terminal(self, task_id: int) -> None:
        if self.media_executor is not None:
            self.media_executor.cleanup(task_id)
        shutil.rmtree(self.work_root / ("task-%d" % int(task_id)), ignore_errors=True)

    @staticmethod
    def _lease_expiry() -> str:
        return (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _renew(self, task: Mapping[str, Any], worker_id: str) -> Dict[str, Any]:
        return self.store.renew_youtube_lease(
            int(task["id"]),
            worker_id,
            int(task["lease_generation"]),
            self._lease_expiry(),
        )

    def _fail_claim(
        self,
        task_id: int,
        worker_id: str,
        lease_generation: int,
        **failure: Any,
    ) -> Optional[Dict[str, Any]]:
        try:
            return self.store.fail_youtube(
                task_id,
                worker_id=worker_id,
                lease_generation=lease_generation,
                **failure,
            )
        except DramaSynthesisError as exc:
            if exc.code == "youtube_stale_claim":
                return None
            raise

    def _publish_video(self, task: Mapping[str, Any], token: str, worker_id: str) -> Dict[str, Any]:
        task_id = int(task["id"])
        lease_generation = int(task["lease_generation"])
        canary = is_youtube_canary(task)
        if canary and int(task.get("video_attempt_count") or 0) and not task.get("resumable_session_uri"):
            raise YouTubeHTTPError("youtube_canary_session_intent_unknown", "内部测试已有上传意图但缺少会话身份，禁止重建", unknown=True)
        frozen_sha256 = str(task.get("source_sha256") or "")
        frozen_size = int(task.get("source_size") or 0)
        root = self.work_root / ("task-%d" % task_id)
        source = root / "source.mp4"
        remote_source = None
        if self.media_executor is not None:
            task = self._renew(task, worker_id)
            if not task.get("source_sha256") or not int(task.get("source_size") or 0):
                task = self.store.advance_youtube(task_id, "downloading", worker_id=worker_id, lease_generation=lease_generation)
            remote_source = self.media_executor.prepare(
                task_id, task["source_url"], heartbeat=lambda: self._renew(task, worker_id),
            )
        elif not source.is_file():
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            task = self._renew(task, worker_id)
            task = self.store.advance_youtube(task_id, "downloading", worker_id=worker_id, lease_generation=lease_generation)
            self.client.download(
                task["source_url"],
                source,
                allowed_hosts=self.allowed_source_hosts,
                heartbeat=lambda: self._renew(task, worker_id),
            )
        if remote_source is not None:
            digest_hex = remote_source["sha256"]
            size = int(remote_source["size"])
            duration_ms = int(remote_source["duration_ms"])
        else:
            digest = hashlib.sha256()
            with source.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest_hex = digest.hexdigest()
            size = source.stat().st_size
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
        if task.get("resumable_session_uri") and (
            frozen_sha256 != digest_hex or frozen_size != size
        ):
            code = "youtube_canary_source_changed" if canary else "youtube_media_source_changed"
            raise YouTubeHTTPError(code, "素材与原上传会话指纹不一致，禁止继续上传", unknown=True)
        task = self.store.advance_youtube(task_id, "uploading", worker_id=worker_id, lease_generation=lease_generation, source_sha256=digest_hex, source_duration_ms=duration_ms)
        session_uri = str(task.get("resumable_session_uri") or "")
        offset = int(task.get("next_byte") or 0)
        if session_uri:
            task = self._renew(task, worker_id)
            status = self.client.query_upload(session_uri, size)
            if status["state"] == "submitted":
                return self.store.video_submitted(
                    task_id,
                    status["video_id"],
                    worker_id=worker_id,
                    lease_generation=lease_generation,
                )
            if status["state"] == "expired":
                return self.store.fail_youtube(
                    task_id,
                    worker_id=worker_id,
                    lease_generation=lease_generation,
                    phase="video",
                    code="youtube_resumable_session_expired_unknown",
                    message="YouTube上传会话已过期，无法证明未发布",
                    unknown=True,
                )
            offset = int(status.get("next_byte") or 0)
            self.store.set_upload_offset(
                task_id,
                offset,
                worker_id=worker_id,
                lease_generation=lease_generation,
            )
        else:
            task = self._renew(task, worker_id)
            self.client.verify_channel_identity(token, task["channel_id"])
            task = self._renew(task, worker_id)
            if canary:
                task = self.store.mark_canary_upload_intent(task_id, worker_id=worker_id, lease_generation=lease_generation)
                session_uri = self.client.begin_resumable(token, title=task["title"], description=task["description_rendered"], size=size, privacy_status="unlisted")
            else:
                session_uri = self.client.begin_resumable(token, title=task["title"], description=task["description_rendered"], size=size)
            self.store.set_upload_session(
                task_id,
                session_uri,
                size,
                worker_id=worker_id,
                lease_generation=lease_generation,
            )
            task = dict(task)
            task["resumable_session_uri"] = session_uri
            task["source_size"] = size
            task["video_attempt_count"] = 1 if canary else int(task.get("video_attempt_count") or 0) + 1
        task = self._renew(task, worker_id)
        try:
            if self.media_executor is None:
                result = self.client.upload(session_uri, source, offset)
            else:
                result = self.media_executor.upload(
                    task_id, session_uri, offset, size=size, sha256=digest_hex,
                    heartbeat=lambda: self._renew(task, worker_id),
                )
        except YouTubeHTTPError as exc:
            if not exc.unknown:
                raise
            task = self._renew(task, worker_id)
            result = self.client.query_upload(session_uri, size)
            if canary and result.get("state") != "submitted":
                raise YouTubeHTTPError("youtube_canary_reconcile_inconclusive", "内部测试原上传会话尚无法确认，禁止重传", unknown=True)
        if result["state"] == "submitted":
            return self.store.video_submitted(
                task_id,
                result["video_id"],
                worker_id=worker_id,
                lease_generation=lease_generation,
            )
        if result["state"] == "resume":
            self.store.set_upload_offset(
                task_id,
                int(result.get("next_byte") or 0),
                worker_id=worker_id,
                lease_generation=lease_generation,
            )
            raise YouTubeHTTPError("youtube_upload_incomplete", "YouTube视频上传尚未完成", retryable=True)
        raise YouTubeHTTPError("youtube_resumable_session_expired_unknown", "YouTube上传会话已过期，无法证明未发布", unknown=True)


__all__ = [
    "YouTubeCredential",
    "YouTubeCredentialRepository",
    "YouTubeHTTPClient",
    "YouTubeHTTPError",
    "YouTubePublishEngine",
    "YouTubeRemoteMediaExecutor",
]
