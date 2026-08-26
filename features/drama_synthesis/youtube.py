"""Server-only YouTube channel discovery and resumable publishing.

The worker deliberately separates confirmed video and comment state.  Any
ambiguous network outcome is terminal ``unknown`` until an operator reconciles
it; the code never creates a replacement video or comment automatically.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Sequence, Union
from urllib.parse import quote, urlencode, urlsplit

import requests

from .core import (
    COMMENT_SCOPE,
    DramaSynthesisError,
    DramaSynthesisStore,
    normalize_channel_scopes,
    scope_capabilities,
)


TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
COMMENTS_URL = "https://www.googleapis.com/youtube/v3/commentThreads"
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,32}")


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
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
        value["eligible"] = bool(self.channel_status == 1 and value["refreshable"] and value["upload_eligible"])
        return value


class YouTubeCredentialRepository:
    """Read token JSON into process memory and return safe DTOs only."""

    def __init__(self, query_runner: Callable[[str], Sequence[Sequence[Any]]], *, schema: str = "kunlunads_dev"):
        if not callable(query_runner) or not re.fullmatch(r"[A-Za-z0-9_]+", schema):
            raise ValueError("invalid YouTube credential repository")
        self.query_runner = query_runner
        self.schema = schema

    def _query(self, where: str) -> list[YouTubeCredential]:
        sql = f"""
            SELECT CAST(ch.id AS CHAR),COALESCE(ch.channel_id,''),COALESCE(ch.channel_name,''),
                   CAST(COALESCE(ch.channel_status,0) AS CHAR),CAST(a.id AS CHAR),
                   COALESCE(a.account_token,''),COALESCE(a.account_credentials,'')
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
            local_id, channel_id, channel_name, channel_status, account_id, token_text, credential_text = row[:7]
            identity = (str(local_id), str(account_id))
            if identity in seen:
                continue
            token = _json_object(token_text)
            client = _client_config(_json_object(credential_text))
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
            if not caps["eligible"] or row.channel_local_id in seen_channels:
                continue
            items.append(
                {
                    "channel_local_id": row.channel_local_id,
                    "channel_id": row.channel_id,
                    "channel_name": row.channel_name,
                    "youtube_account_id": row.account_id,
                    "upload_eligible": True,
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

    def download(self, url: str, target: Path, *, allowed_hosts: Iterable[str], max_bytes: int = 16 * 1024 * 1024 * 1024) -> int:
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
        except (OSError, requests.RequestException):
            temporary.unlink(missing_ok=True)
            raise YouTubeHTTPError("youtube_source_download_failed", "视频下载失败", retryable=True) from None
        finally:
            session.close()
        if total <= 0:
            target.unlink(missing_ok=True)
            raise YouTubeHTTPError("youtube_source_empty", "视频文件为空", status=400)
        return total

    def begin_resumable(self, token: str, *, title: str, description: str, size: int) -> str:
        params = urlencode({"uploadType": "resumable", "part": "snippet,status"})
        body = {"snippet": {"title": title, "description": description}, "status": {"privacyStatus": "public"}}
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
            return {"state": "published", "video_id": video_id, "next_byte": int(size)}
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

    def publish_comment(self, token: str, *, video_id: str, comment_text: str) -> str:
        body = {
            "snippet": {
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
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        comment_id = str(payload.get("id") or "")
        if response.status_code not in (200, 201) or not comment_id:
            raise YouTubeHTTPError("youtube_comment_failed", "YouTube评论发布失败", status=response.status_code)
        return comment_id


class YouTubePublishEngine:
    def __init__(
        self,
        store: DramaSynthesisStore,
        credentials: YouTubeCredentialRepository,
        client: YouTubeHTTPClient,
        *,
        work_root: Union[str, os.PathLike],
        allowed_source_hosts: Iterable[str],
    ):
        root = Path(work_root)
        if not root.is_absolute():
            raise ValueError("YouTube work root must be absolute")
        self.store = store
        self.credentials = credentials
        self.client = client
        self.work_root = root
        self.allowed_source_hosts = tuple(dict.fromkeys(str(item).strip().lower() for item in allowed_source_hosts if str(item).strip()))
        if not self.allowed_source_hosts:
            raise ValueError("YouTube source allowlist is required")

    def run_once(self, worker_id: str) -> Dict[str, Any]:
        lease = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        task = self.store.claim_youtube(worker_id, lease)
        if task is None:
            return {"ok": True, "status": "no_pending", "claimed": False}
        task_id = int(task["id"])
        try:
            credential = self.credentials.credential(
                app_id=task["app_id"],
                channel_local_id=task["channel_local_id"],
                account_id=task["youtube_account_id"],
                expected_channel_id=task["channel_id"],
            )
            if task["comment_text"] and not credential.capabilities["comment_eligible"]:
                raise YouTubeHTTPError("youtube_comment_scope_missing", "频道授权缺少评论权限", status=409)
            token = self.client.refresh_access_token(credential)
            if task["video_state"] != "published":
                task = self._publish_video(task, token)
                if task["video_state"] != "published":
                    return {"ok": False, "status": task["status"], "task_id": task_id}
            if task["comment_state"] == "queued" or task["status"] == "publishing_comment":
                self.store.mark_comment_attempt(task_id)
                comment_id = self.client.publish_comment(token, video_id=task["video_id"], comment_text=task["comment_text"])
                task = self.store.comment_published(task_id, comment_id)
            return {"ok": True, "status": task["status"], "task_id": task_id, "claimed": True}
        except YouTubeHTTPError as exc:
            phase = "comment" if task.get("video_state") == "published" else "video"
            failed = self.store.fail_youtube(task_id, phase=phase, code=exc.code, message=str(exc), unknown=exc.unknown, retryable=exc.retryable)
            return {"ok": False, "status": failed["status"], "task_id": task_id, "claimed": True}
        except DramaSynthesisError as exc:
            phase = "comment" if task.get("video_state") == "published" else "video"
            failed = self.store.fail_youtube(task_id, phase=phase, code=exc.code, message=str(exc), unknown=False, retryable=False)
            return {"ok": False, "status": failed["status"], "task_id": task_id, "claimed": True}
        except Exception:
            # A programming/runtime failure after an upload or comment attempt
            # cannot be converted into a replacement publish automatically.
            phase = "comment" if task.get("video_state") == "published" else "video"
            external_attempted = bool(
                task.get("resumable_session_uri")
                or int(task.get("video_attempt_count") or 0)
                or (phase == "comment" and int(task.get("comment_attempt_count") or 0))
            )
            failed = self.store.fail_youtube(
                task_id,
                phase=phase,
                code="youtube_worker_internal_error",
                message="YouTube发布任务发生内部错误",
                unknown=external_attempted,
                retryable=not external_attempted,
            )
            return {"ok": False, "status": failed["status"], "task_id": task_id, "claimed": True}

    def _publish_video(self, task: Mapping[str, Any], token: str) -> Dict[str, Any]:
        task_id = int(task["id"])
        root = self.work_root / ("task-%d" % task_id)
        source = root / "source.mp4"
        if not source.is_file():
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.client.download(task["source_url"], source, allowed_hosts=self.allowed_source_hosts)
        size = source.stat().st_size
        session_uri = str(task.get("resumable_session_uri") or "")
        offset = int(task.get("next_byte") or 0)
        if session_uri:
            status = self.client.query_upload(session_uri, size)
            if status["state"] == "published":
                return self.store.video_published(task_id, status["video_id"])
            if status["state"] == "expired":
                return self.store.fail_youtube(task_id, phase="video", code="youtube_resumable_session_expired_unknown", message="YouTube上传会话已过期，无法证明未发布", unknown=True)
            offset = int(status.get("next_byte") or 0)
            self.store.set_upload_offset(task_id, offset)
        else:
            session_uri = self.client.begin_resumable(token, title=task["title"], description=task["description"], size=size)
            self.store.set_upload_session(task_id, session_uri, size)
        result = self.client.upload(session_uri, source, offset)
        if result["state"] == "published":
            return self.store.video_published(task_id, result["video_id"])
        if result["state"] == "resume":
            self.store.set_upload_offset(task_id, int(result.get("next_byte") or 0))
            raise YouTubeHTTPError("youtube_upload_incomplete", "YouTube视频上传尚未完成", retryable=True)
        raise YouTubeHTTPError("youtube_resumable_session_expired_unknown", "YouTube上传会话已过期，无法证明未发布", unknown=True)


__all__ = [
    "YouTubeCredential",
    "YouTubeCredentialRepository",
    "YouTubeHTTPClient",
    "YouTubeHTTPError",
    "YouTubePublishEngine",
]
