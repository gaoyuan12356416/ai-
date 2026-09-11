"""Approved-cover YouTube lane, sharing IDs/media transport with synthesis.

Google contracts: thumbnails/set (2 MB JPEG/PNG), videos/update (part=status
replaces mutable status fields), videos#processingDetails.processingStatus.
Only this lane uploads private and makes the same video public after approval,
thumbnail success and processing success. No Google token enters the ledger.
"""
from __future__ import annotations

import hashlib
import io
import time
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode

import requests
from PIL import Image

from features.drama_synthesis.core import DramaSynthesisError, REVIEWED_SCOPES, REVIEWED_WORKFLOW
from features.drama_synthesis.youtube import (
    VIDEO_ID_RE, VIDEOS_URL, YouTubeHTTPClient, YouTubeHTTPError, YouTubePublishEngine,
)

THUMBNAILS_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
MAX_COVER_BYTES = 2 * 1024 * 1024


def reviewed_scope_eligible(scopes: Iterable[str]) -> bool:
    return bool(REVIEWED_SCOPES.intersection(scopes))


def approved_cover_bytes(task: Mapping[str, Any], root: Path) -> tuple[bytes, str]:
    """Read once; both digest verification and API upload use these exact bytes."""
    try:
        original = Path(str(task.get("approved_cover_path") or ""))
        path = original.resolve(strict=True)
        if not original.is_absolute() or original.is_symlink() or not path.is_relative_to(root.resolve(strict=True)) or not path.is_file():
            raise ValueError("outside private root")
        with path.open("rb") as handle:
            content = handle.read(MAX_COVER_BYTES + 1)
        if not 0 < len(content) <= MAX_COVER_BYTES or hashlib.sha256(content).hexdigest() != task.get("approved_cover_sha256"):
            raise ValueError("approved digest changed")
        with Image.open(io.BytesIO(content)) as image:
            mime = {"PNG": "image/png", "JPEG": "image/jpeg"}.get(image.format)
            width, height = image.size
            # The preparation service enforces 16:9 for AI output. Local
            # images retain their approved composition; do not silently crop.
            if not mime or width < 320 or height < 180 or width > 8192 or height > 8192:
                raise ValueError("invalid cover")
            image.verify()
        return content, mime
    except (OSError, ValueError, Image.DecompressionBombError):
        raise YouTubeHTTPError("youtube_approved_cover_invalid", "已审核封面不存在、内容已变化或图片格式无效", status=409) from None


class ReviewedYouTubeHTTPClient(YouTubeHTTPClient):
    allowed_upload_privacy = frozenset({"private"})

    @staticmethod
    def _safe_error_detail(response):
        # Never persist Google's free-form message, request body or credentials.
        explanations = {'forbidden':'YouTube 拒绝此操作，请核对频道权限',
            'videoNotFound':'YouTube 当前未找到此视频', 'quotaExceeded':'YouTube API 配额已用尽',
            'rateLimitExceeded':'YouTube 请求频率受限', 'invalidImage':'YouTube 无法识别封面图片',
            'mediaBodyRequired':'请求缺少图片内容', 'authError':'频道授权无效',
            'insufficientPermissions':'频道授权权限不足', 'uploadTooLarge':'图片超过平台大小限制'}
        try:
            payload = response.json()
            errors = payload.get('error',{}).get('errors',[])
            reasons = [x.get('reason') for x in errors if isinstance(x,Mapping)]
        except (ValueError,AttributeError,TypeError):
            reasons = []
        known = list(dict.fromkeys(reason for reason in reasons if isinstance(reason,str) and reason in explanations))
        suffix = '；'.join(explanations[reason]+' ('+reason+')' for reason in known)
        return 'HTTP '+str(response.status_code)+('；'+suffix if suffix else '')

    def begin_resumable(self, *args: Any, **kwargs: Any) -> str:
        try:
            return super().begin_resumable(*args, **kwargs)
        except YouTubeHTTPError as exc:
            if exc.code == "youtube_resumable_create_failed" and not 400 <= exc.status < 500:
                # A success/malformed response without Location may have
                # created a session. It is not a definite request rejection.
                raise YouTubeHTTPError("youtube_resumable_create_unknown", "上传会话身份未确认，禁止创建替代会话", status=exc.status, unknown=True) from None
            raise

    def read_reviewed_video_state(self, token: str, video_id: str, *, expected_channel_id: str) -> dict[str, Any]:
        if not VIDEO_ID_RE.fullmatch(str(video_id)):
            raise YouTubeHTTPError("youtube_video_identity_invalid", "YouTube视频身份无效")
        session = self.session_factory()
        session.trust_env = False
        try:
            response = session.get(VIDEOS_URL + "?" + urlencode({"part": "snippet,status,processingDetails", "id": video_id}),
                headers={"Authorization": "Bearer " + token}, timeout=self.timeout, allow_redirects=False)
        except requests.RequestException:
            raise YouTubeHTTPError("youtube_processing_check_failed", "YouTube视频状态查询暂时失败", retryable=True) from None
        finally:
            session.close()
        if response.status_code >= 500:
            raise YouTubeHTTPError("youtube_processing_check_failed", "YouTube视频状态查询暂时失败", retryable=True)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        items = payload.get("items") if isinstance(payload, Mapping) else None
        if response.status_code == 200 and items == []:
            raise YouTubeHTTPError("youtube_video_reconcile_unknown", "YouTube 当前未返回此视频，无法确认是否仍可访问；已保留原视频 ID，禁止重新上传", unknown=True)
        if response.status_code != 200 or not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], Mapping):
            raise YouTubeHTTPError("youtube_video_reconcile_unknown", "YouTube视频状态无法确认（"+self._safe_error_detail(response)+"）", unknown=True)
        item = items[0]
        snippet, status, processing = item.get("snippet"), item.get("status"), item.get("processingDetails")
        if (item.get("id") != video_id or not isinstance(snippet, Mapping) or snippet.get("channelId") != expected_channel_id
                or not isinstance(status, Mapping) or not isinstance(processing, Mapping)):
            raise YouTubeHTTPError("youtube_video_identity_conflict", "YouTube视频或频道身份无法确认", unknown=True)
        upload, visibility = str(status.get("uploadStatus") or ""), str(status.get("privacyStatus") or "")
        processing_status = str(processing.get("processingStatus") or "")
        state = "unknown"
        if upload in {"failed", "rejected", "deleted"} or processing_status in {"failed", "terminated"}:
            state = "failed"
        elif upload == "processed" and processing_status == "succeeded":
            state = "succeeded"
        elif upload in {"uploaded", "processing", "processed"} and processing_status == "processing":
            state = "processing"
        return {"state": state, "visibility": visibility, "processing_status": processing_status,
                "preserved_status": dict(status)}

    def set_thumbnail(self, token: str, *, video_id: str, content: bytes, mime_type: str) -> None:
        if not VIDEO_ID_RE.fullmatch(str(video_id)) or mime_type not in {"image/jpeg", "image/png"} or not 0 < len(content) <= MAX_COVER_BYTES:
            raise YouTubeHTTPError("youtube_thumbnail_invalid", "YouTube封面无效", status=400)
        session = self.session_factory()
        session.trust_env = False
        try:
            response = session.post(THUMBNAILS_URL + "?" + urlencode({"videoId": video_id, "uploadType": "media"}),
                data=content, headers={"Authorization": "Bearer " + token, "Content-Type": mime_type},
                timeout=self.timeout, allow_redirects=False)
        except requests.RequestException:
            # Replacing this same video's thumbnail with identical frozen bytes
            # is idempotent. A retry never creates another video or comment.
            raise YouTubeHTTPError("youtube_thumbnail_set_failed", "封面设置响应未确认，可重试此封面") from None
        finally:
            session.close()
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code != 200 or not isinstance(payload, Mapping) or not payload.get("items"):
            raise YouTubeHTTPError("youtube_thumbnail_set_failed", "YouTube封面设置失败（"+self._safe_error_detail(response)+"）", status=response.status_code)

    def make_video_public(self, token: str, *, video_id: str, preserved_status: Mapping[str, Any]) -> None:
        if not VIDEO_ID_RE.fullmatch(str(video_id)):
            raise YouTubeHTTPError("youtube_video_identity_invalid", "YouTube视频身份无效")
        # videos.update removes unspecified mutable fields in the requested
        # part. Preserve owner-read writable flags, excluding read-only status
        # and publishAt (a private-only scheduling property).
        status = {key: preserved_status[key] for key in ("embeddable", "license", "publicStatsViewable",
                  "selfDeclaredMadeForKids", "containsSyntheticMedia") if key in preserved_status}
        status["privacyStatus"] = "public"
        session = self.session_factory()
        session.trust_env = False
        try:
            response = session.put(VIDEOS_URL + "?part=status", json={"id": video_id, "status": status},
                headers={"Authorization": "Bearer " + token}, timeout=self.timeout, allow_redirects=False)
        except requests.RequestException:
            raise YouTubeHTTPError("youtube_public_update_unknown", "视频公开设置结果未知，需核验原视频", unknown=True) from None
        finally:
            session.close()
        if response.status_code >= 500:
            raise YouTubeHTTPError("youtube_public_update_unknown", "视频公开设置结果未知，需核验原视频", unknown=True)
        if response.status_code != 200:
            raise YouTubeHTTPError("youtube_public_update_failed", "YouTube视频公开设置失败（"+self._safe_error_detail(response)+"）", status=response.status_code)


class ReviewedYouTubePublishEngine(YouTubePublishEngine):
    def __init__(self, *args: Any, approved_cover_root: str | Path, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.approved_cover_root = Path(approved_cover_root)
        if not self.approved_cover_root.is_absolute():
            raise ValueError("Approved cover root must be absolute")

    def _initial_privacy(self, task: Mapping[str, Any]) -> str:
        if task.get("workflow") != REVIEWED_WORKFLOW:
            raise YouTubeHTTPError("youtube_workflow_invalid", "发布流程不匹配")
        return "private"

    def _before_begin_upload(self, task: Mapping[str, Any], worker_id: str, *, size: int) -> Mapping[str, Any]:
        return self.store.mark_reviewed_upload_intent(int(task["id"]), worker_id=worker_id,
                                                     lease_generation=int(task["lease_generation"]), source_size=size)

    def _source_fingerprint_frozen(self, task: Mapping[str, Any]) -> bool:
        return bool(task.get("source_sha256") and int(task.get("source_size") or 0))

    def _reconcile_unknown_upload(self, session_uri: str, size: int) -> dict[str, Any]:
        try:
            return self.client.query_upload(session_uri, size)
        except YouTubeHTTPError:
            raise YouTubeHTTPError("youtube_upload_reconcile_unknown", "上传结果未知且原会话核验失败，禁止创建替代视频", unknown=True) from None

    def _phase(self, task: Mapping[str, Any], worker_id: str, phase: str, **states: str) -> dict[str, Any]:
        self._renew(task, worker_id)
        return self.store.advance_reviewed_youtube(int(task["id"]), phase, worker_id=worker_id,
                                                   lease_generation=int(task["lease_generation"]), **states)

    def _read(self, task: Mapping[str, Any], token: str, worker_id: str) -> dict[str, Any]:
        self._renew(task, worker_id)
        return self.client.read_reviewed_video_state(token, task["video_id"], expected_channel_id=task["channel_id"])

    def _confirm_public(self, task: Mapping[str, Any], token: str, worker_id: str) -> None:
        # A successful update can precede visibility propagation. Only reread
        # this frozen video; never repeat a write or release the comment fence.
        state = {}
        for delay in (0, 2, 5, 10):
            if delay:
                self._renew(task, worker_id)
                time.sleep(delay)
            try:
                state = self._read(task, token, worker_id)
            except YouTubeHTTPError as exc:
                if not exc.retryable:
                    raise  # Missing video/identity conflict remains unknown.
                state = {}
                continue
            if state["visibility"] == "public" and state["state"] == "succeeded":
                return
            if state["state"] == "failed":
                raise YouTubeHTTPError("youtube_processing_failed", "YouTube视频处理失败，保留原视频记录")
            if state["visibility"] not in {"private", "public"}:
                raise YouTubeHTTPError("youtube_reviewed_privacy_mismatch", "视频隐私状态异常，已阻断公开", unknown=True)
        detail = "仍为私享" if state.get("visibility") == "private" else "公开及处理状态未确认"
        raise YouTubeHTTPError("youtube_public_readback_unknown",
            "已提交公开设置，多次核验后" + detail + "；保留原视频，暂停首评，可核验原视频后重试", unknown=True)

    def run_once(self, worker_id: str) -> dict[str, Any]:
        task = self.store.claim_reviewed_youtube(str(worker_id), self._lease_expiry())
        if task is None:
            return {"ok": True, "status": "no_pending", "claimed": False}
        task_id, generation = int(task["id"]), int(task["lease_generation"])
        stage = str(task.get("reviewed_phase") or "upload")
        try:
            if task.get("workflow") != REVIEWED_WORKFLOW:
                raise YouTubeHTTPError("youtube_workflow_invalid", "发布流程不匹配")
            try:
                credential = self.credentials.credential(app_id=task["app_id"], channel_local_id=task["channel_local_id"],
                    account_id=task["youtube_account_id"], expected_channel_id=task["channel_id"])
            except DramaSynthesisError as exc:
                # Revoked/missing credentials are a known preflight rejection,
                # even if this task uploaded its private video on an earlier tick.
                raise YouTubeHTTPError(exc.code, str(exc), status=exc.status) from None
            if not reviewed_scope_eligible(credential.scopes):
                raise YouTubeHTTPError("youtube_thumbnail_scope_missing", "频道授权缺少封面及公开设置权限", status=409)
            if task["comment_text"] and not credential.capabilities["comment_eligible"]:
                raise YouTubeHTTPError("youtube_comment_scope_missing", "频道授权缺少评论权限", status=409)
            if not task.get("video_id"):
                # Fail before any video insert if approval bytes were changed.
                approved_cover_bytes(task, self.approved_cover_root)
            task = self._renew(task, worker_id)
            token = self.client.refresh_access_token(credential)
            task = self._renew(task, worker_id)
            self.client.verify_channel_identity(token, task["channel_id"])
            if not task.get("video_id"):
                stage = "upload"
                task = self._phase(task, worker_id, stage)
                task = self._publish_video(task, token, worker_id)
                return {"ok": task["status"] not in {"failed", "unknown"}, "status": task["status"], "task_id": task_id, "claimed": True}
            if task["video_state"] != "published":
                state = self._read(task, token, worker_id)
                if state["visibility"] not in {"private", "public"}:
                    raise YouTubeHTTPError("youtube_reviewed_privacy_mismatch", "视频隐私状态异常，已阻断公开", unknown=True)
                if state["visibility"] == "public" and task["public_status"] not in {"running", "unknown", "succeeded", "failed"}:
                    raise YouTubeHTTPError("youtube_reviewed_early_public", "视频在封面处理完成前已公开，需人工核验", unknown=True)
                if task["thumbnail_status"] != "succeeded":
                    stage = "thumbnail"
                    task = self._phase(task, worker_id, stage, thumbnail_status="running")
                    content, mime = approved_cover_bytes(task, self.approved_cover_root)
                    self.client.set_thumbnail(token, video_id=task["video_id"], content=content, mime_type=mime)
                    task = self._phase(task, worker_id, stage, thumbnail_status="succeeded")
                stage = "processing"
                task = self._phase(task, worker_id, stage)
                state = self._read(task, token, worker_id)
                if state["visibility"] == "public" and task["public_status"] not in {"running", "unknown", "succeeded", "failed"}:
                    raise YouTubeHTTPError("youtube_reviewed_early_public", "视频在公开步骤前被更改，需人工核验", unknown=True)
                if state["state"] == "failed":
                    raise YouTubeHTTPError("youtube_processing_failed", "YouTube视频处理失败，保留原视频记录")
                if state["state"] == "processing":
                    task = self._phase(task, worker_id, stage, processing_status="running")
                    task = self.store.video_processing(task_id, worker_id=worker_id, lease_generation=generation)
                    return {"ok": True, "status": "processing", "task_id": task_id, "claimed": True}
                if state["state"] != "succeeded":
                    raise YouTubeHTTPError("youtube_processing_unconfirmed", "YouTube视频处理完成状态尚未确认", retryable=True)
                task = self._phase(task, worker_id, stage, processing_status="succeeded")
                stage = "public"
                task = self._phase(task, worker_id, stage, public_status="running")
                if state["visibility"] == "private":
                    self._renew(task, worker_id)
                    try:
                        self.client.make_video_public(token, video_id=task["video_id"], preserved_status=state["preserved_status"])
                    except YouTubeHTTPError as exc:
                        if not exc.unknown:
                            raise
                        # An uncertain update gets the same bounded read-only
                        # reconciliation as HTTP 200; no replacement PUT.
                # An API write response alone is never a publication receipt.
                self._confirm_public(task, token, worker_id)
                task = self._phase(task, worker_id, "comment" if task["comment_text"] else "complete", public_status="succeeded")
                task = self.store.video_published(task_id, task["video_id"], worker_id=worker_id, lease_generation=generation)
            if task["comment_status"] in {"queued", "retry", "publishing"}:
                stage = "comment"
                task = self._phase(task, worker_id, stage)
                state = self._read(task, token, worker_id)
                if state["visibility"] != "public" or state["state"] != "succeeded":
                    raise YouTubeHTTPError("youtube_comment_preflight_unknown", "原视频公开状态无法确认，禁止首评", unknown=True)
                self.client.verify_channel_identity(token, task["channel_id"])
                self._renew(task, worker_id)
                self.store.mark_comment_attempt(task_id, worker_id=worker_id, lease_generation=generation)
                comment_id = self.client.publish_comment(token, video_id=task["video_id"], comment_text=task["comment_text"], channel_id=task["channel_id"])
                task = self.store.comment_published(task_id, comment_id, worker_id=worker_id, lease_generation=generation)
            self._cleanup_terminal(task_id)
            return {"ok": True, "status": task["status"], "task_id": task_id, "claimed": True}
        except DramaSynthesisError as exc:
            if exc.code == "youtube_stale_claim":
                return {"ok": False, "status": "stale_claim", "task_id": task_id, "claimed": True}
            return self._failure(task_id, worker_id, generation, stage, exc.code, str(exc), internal=True)
        except YouTubeHTTPError as exc:
            return self._failure(task_id, worker_id, generation, stage, exc.code, str(exc), unknown=exc.unknown, retryable=exc.retryable)
        except Exception:
            return self._failure(task_id, worker_id, generation, stage, "youtube_worker_internal_error", "YouTube发布任务发生内部错误", internal=True)

    def _failure(self, task_id: int, worker_id: str, generation: int, stage: str, code: str, message: str,
                 *, unknown: bool = False, retryable: bool = False, internal: bool = False) -> dict[str, Any]:
        current = self.store.youtube_task(task_id) or {}
        if internal:
            unknown = bool(current.get("resumable_session_uri") or current.get("video_attempt_count") or current.get("comment_attempt_count"))
            if stage == "thumbnail" and current.get("video_id"):
                unknown = False  # Same approved bytes replace the same thumbnail safely.
        try:
            updates = {stage + "_status": "unknown" if unknown else "failed"} if stage in {"thumbnail", "processing", "public"} else {}
            self.store.advance_reviewed_youtube(task_id, stage, worker_id=worker_id, lease_generation=generation, **updates)
            failed = self.store.fail_youtube(task_id, worker_id=worker_id, lease_generation=generation,
                phase="comment" if current.get("video_state") == "published" else "video", code=code, message=message,
                unknown=unknown, retryable=retryable)
        except DramaSynthesisError as exc:
            if exc.code != "youtube_stale_claim":
                raise
            return {"ok": False, "status": "stale_claim", "task_id": task_id, "claimed": True}
        # Keep immutable source and approved image through failures/retries.
        return {"ok": False, "status": failed["status"], "task_id": task_id, "claimed": True}

__all__ = ["ReviewedYouTubeHTTPClient", "ReviewedYouTubePublishEngine", "reviewed_scope_eligible", "approved_cover_bytes"]
