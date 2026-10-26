"""Bounded manual worker using existing prepare-only GPU and Graph transports."""
from __future__ import annotations

import hashlib
import json
import random
import re
import threading
import time

import requests

from features.fb_auto_posts.publisher import GraphResult
from .core import DomainError
from .links import publish_link, short_url, validate_long_url


def public_link_readback(url, expected_sha):
    """Read only our immutable wrapper, never follow the W2A redirect."""
    session = requests.Session()
    session.trust_env = False
    try:
        with session.get(url, timeout=(5, 15), allow_redirects=False, stream=True) as response:
            body = bytearray()
            if response.status_code != 200:
                raise DomainError("short_link_public_unavailable", "短链公网读回失败，尚未发布", 503)
            for chunk in response.iter_content(16384):
                body.extend(chunk)
                if len(body) > 1024 * 1024:
                    raise DomainError("short_link_public_mismatch", "短链公网内容过大，尚未发布", 503)
            if hashlib.sha256(body).hexdigest() != expected_sha:
                raise DomainError("short_link_public_mismatch", "短链公网内容与冻结文件不一致", 503)
    except requests.RequestException:
        raise DomainError("short_link_public_unavailable", "短链公网读回暂不可用", 503) from None
    finally:
        session.close()


class Worker:
    def __init__(self, store, pages, gpu, graph, *, sources=None, live_enabled=False, prepare_enabled=False,
                 short_link_root="", short_link_owner="", link_writer=publish_link,
                 readback=public_link_readback, rng=None, rate_limit=None,
                 group_sources=None, group_identity_resolver=None):
        self.store, self.pages, self.gpu, self.graph, self.sources = store, pages, gpu, graph, sources
        self.live_enabled, self.prepare_enabled = bool(live_enabled), bool(prepare_enabled)
        self.short_link_root, self.short_link_owner = short_link_root, short_link_owner
        self.link_writer, self.readback, self.rng = link_writer, readback, rng or random.SystemRandom()
        self.rate_limit = rate_limit or (lambda: None)
        self.group_sources, self.group_identity_resolver = group_sources, group_identity_resolver
        self._prepare_lock = threading.Lock()

    def _group_preflight(self, task, snap):
        from .core import actor_checked
        from .group_pool import POLICY_VERSION
        policy = snap.get("group_pool")
        if (not isinstance(policy, dict) or policy.get("policy_version") != POLICY_VERSION
                or not isinstance(policy.get("group_ids"), list) or not policy["group_ids"]
                or "manual_policy" in snap or "pool" in snap
                or self.group_sources is None or self.group_identity_resolver is None):
            raise DomainError("group_pool_policy_invalid", "组素材池冻结政策或复核服务无效，未调用平台", 409)
        actor = actor_checked(self.group_identity_resolver(task["tenant_key"], task["owner_user_id"]))
        frozen_actor = snap.get("actor", {})
        if (actor["tenant_key"] != task["tenant_key"] or actor["user_id"] != task["owner_user_id"]
                or any(actor.get(k) != frozen_actor.get(k) for k in ("tenant_key", "user_id", "sub_user_id", "business_owner_id"))):
            raise DomainError("group_pool_identity_changed", "计划发布人身份已改变，未调用平台", 403)
        scope = self.group_sources.resolve_scope(actor, policy["group_ids"])
        page = scope["pages"].get(task["page_id"])
        frozen_page = snap["page"]
        if (not page or page.get("language") != frozen_page.get("language")
                or sorted(page.get("group_ids", [])) != sorted(frozen_page.get("group_ids", []))):
            raise DomainError("group_pool_membership_changed", "Page组成员、语言或授权已改变，未调用平台", 409)
        fresh = self.group_sources.get_materials([task["material_id"]], need_desc=False)[task["material_id"]]
        if any(str(fresh.get(k, "")) != str(snap["material"].get(k, "")) for k in ("material_id", "content_id", "language", "media_url")):
            raise DomainError("material_identity_changed", "素材来源身份已变化，未调用平台", 409)
        if fresh["language"] != page["language"]:
            raise DomainError("language_mismatch", "Page与素材当前语言不匹配，未调用平台", 409)

    def _task_credentials(self, task, snapshot=None):
        """Re-read only the credential scope frozen for this publication."""
        snap = json.loads(task["snapshot_json"]) if snapshot is None else snapshot
        mode = snap.get("credential_mode", "page_pool")
        if mode == "page_pool":
            return self.pages.eligible_credentials(task["page_id"])
        if mode != "operator":
            raise DomainError("credential_policy_invalid", "任务的Token策略无效，未调用平台", 409)
        owner = snap.get("credential_owner_id")
        actor = snap.get("actor")
        if (not isinstance(owner, str) or not re.fullmatch(r"[1-9][0-9]{0,30}", owner)
                or not isinstance(actor, dict) or owner != actor.get("sub_user_id")):
            raise DomainError("operator_identity_invalid", "个人Token的冻结操作人身份无效，未调用平台", 409)
        return self.pages.eligible_credentials(task["page_id"], owner_user_id=owner)

    def prepare_next(self, worker_id="fb-video-prepare"):
        if not self.live_enabled or not self.prepare_enabled:
            return {"ok": True, "status": "live_gate_closed"}
        if not self._prepare_lock.acquire(blocking=False):
            return {"ok": True, "status": "no_pending"}
        try:
            task = self.store.claim("prepare", worker_id)
            if task is None:
                return {"ok": True, "status": "no_pending"}
            try:
                snap = json.loads(task["snapshot_json"])
                result = self.gpu.prepare(job_id=task["gpu_job_id"], content_id=task["content_id"],
                                          source_url=snap["material"]["media_url"], video_template="random_overlay")
                if not isinstance(result.get("random_overlay_recipe"), dict) or not result["random_overlay_recipe"]:
                    raise DomainError("prepared_recipe_missing", "GPU没有返回可冻结的随机加工配方", 409)
                return self.store.finish_stage(task, "prepare", result)
            except Exception as exc:
                safe = exc if hasattr(exc, "code") else DomainError("gpu_prepare_unavailable", "视频加工暂不可用", 503)
                permanent = {"fb_auto_gpu_request_invalid", "fb_auto_prepared_identity_mismatch",
                             "fb_auto_prepared_response_invalid", "invalid_request", "invalid_configuration",
                             "fb_gpu_job_conflict", "fb_gpu_output_contract_invalid", "fb_gpu_cos_identity_conflict"}
                retry = safe.code not in permanent and getattr(safe, "status", 500) >= 500
                return self.store.finish_stage(task, "prepare", error=safe, retry=retry)
        finally:
            self._prepare_lock.release()

    def link_next(self, worker_id="fb-video-link"):
        if not self.live_enabled:
            return {"ok": True, "status": "live_gate_closed"}
        task = self.store.claim("link", worker_id)
        if task is None:
            return {"ok": True, "status": "no_pending"}
        try:
            if short_url(task["ledger_id"]) != task["short_url"]:
                raise DomainError("short_link_identity_mismatch", "短链冻结身份不一致", 409)
            validate_long_url(task["long_url"], task["ledger_id"])
            result = self.link_writer(self.short_link_root, task["ledger_id"], task["long_url"], owner_user=self.short_link_owner)
            if result["short_url"] != task["short_url"] or result["long_url"] != task["long_url"]:
                raise DomainError("short_link_identity_mismatch", "短链写入身份不一致", 409)
            self.readback(task["short_url"], result["sha256"])
            return self.store.finish_stage(task, "link", result)
        except Exception as exc:
            safe = exc if hasattr(exc, "code") else DomainError("short_link_unavailable", "短链写入暂不可用", 503)
            return self.store.finish_stage(task, "link", error=safe, retry=getattr(safe, "status", 500) >= 500)

    def execute_next(self, worker_id="fb-video-execute"):
        if not self.live_enabled:
            return {"ok": True, "status": "live_gate_closed"}
        task = self.store.claim("execute", worker_id)
        if task is None:
            return {"ok": True, "status": "no_pending"}
        try:
            snap, prepared = json.loads(task["snapshot_json"]), json.loads(task["prepared_json"])
            if task.get("source_kind") == "group_pool":
                self._group_preflight(task, snap)
            elif self.sources is not None:
                manual = task.get("source_kind") == "manual" and "pool" not in snap
                self.sources.get_pages(snap["actor"], [task["page_id"]], manual=manual)
                # Manual and Page-pool tasks both waive the blacklist. Keep
                # pool release/language/metadata checks on the strict path.
                enforce_blacklist = task.get("source_kind") not in {"manual", "pool"}
                fresh = self.sources.get_materials([task["material_id"]], need_desc=False,
                                                   enforce_blacklist=enforce_blacklist, manual=manual)[task["material_id"]]
                if any(str(fresh[k]) != str(snap["material"][k]) for k in ("material_id", "content_id", "language", "media_url")):
                    raise DomainError("material_identity_changed", "素材来源身份已变化，未向平台发布", 409)
            media_url = str(prepared.get("media_url") or "")
            if task["processing_mode"] == "random_overlay":
                if (not media_url or media_url == snap["material"]["media_url"] or not prepared.get("sha256")
                        or not prepared.get("profile") or not prepared.get("random_overlay_recipe")):
                    raise DomainError("prepared_media_invalid", "任务缺少独立加工成片", 409)
            elif media_url != snap["material"]["media_url"]:
                raise DomainError("original_media_invalid", "原片身份与冻结来源不一致", 409)
            if short_url(task["ledger_id"]) != task["short_url"]:
                raise DomainError("short_link_identity_mismatch", "短链身份不一致", 409)
            validate_long_url(task["long_url"], task["ledger_id"])
            title = snap.get("title", "")
            if not isinstance(title, str):
                raise DomainError("title_invalid", "任务的视频标题无效，未调用平台", 409)
            credentials = self._task_credentials(task, snap)
            self.rng.shuffle(credentials)
        except DomainError as exc:
            if task.get("source_kind") == "group_pool":
                return self.store.record_outcome(task, GraphResult("definite_failure", error_code=exc.code, message=str(exc)))
            if exc.code in {"credential_policy_invalid", "operator_identity_invalid", "title_invalid"}:
                return self.store.record_outcome(task, GraphResult("definite_failure", error_code=exc.code,
                                                                 message=str(exc)))
            return self.store.record_outcome(task, GraphResult("definite_failure", error_code="publish_preflight_failed",
                                                             message="发布前Page、素材或成片复核失败，未调用平台"))
        except Exception as exc:
            if task.get("source_kind") == "group_pool" and hasattr(exc, "code"):
                return self.store.record_outcome(task, GraphResult("definite_failure", error_code=exc.code, message=str(exc)))
            return self.store.record_outcome(task, GraphResult("definite_failure", error_code="publish_preflight_failed",
                                                             message="发布前Page、素材或成片复核失败，未调用平台"))
        if not credentials:
            personal = snap.get("credential_mode") == "operator"
            return self.store.record_outcome(task, GraphResult("definite_failure",
                    error_code="operator_authorization_unavailable" if personal else "page_authorization_unavailable",
                    message="当前操作人没有该Page的有效Token，未使用其他用户授权" if personal else "Page已无有效授权，未调用平台"))
        for index, credential in enumerate(credentials):
            self.rate_limit()
            attempt_id = self.store.begin_attempt(task, credential.credential_id, credential.fb_user_id)
            if attempt_id is None:
                return {"ok": True, "status": "stale_claim"}
            try:
                outcome = self.graph.publish_video(task["page_id"], credential, media_url, task["caption"],
                                                   **({"title": title} if title else {}))
            except Exception:
                outcome = GraphResult("unknown", error_code="graph_outcome_unknown", message="平台请求结果无法确认，已停止重试")
            if str(outcome.post_id or "").strip():
                # A receipt always stops further writes, even if an adapter also
                # reports an error. Reconciliation owns the next step.
                outcome = GraphResult("success", post_id=outcome.post_id, trace_id=outcome.trace_id)
            elif outcome.kind == "success":
                outcome = GraphResult("unknown", error_code="graph_receipt_missing", message="平台未返回有效发布标识，已停止重试")
            retry_credential = outcome.kind in {"credential_failure", "definite_failure"}
            final = not retry_credential or index == len(credentials) - 1
            if retry_credential and final:
                scope = "当前操作人在该Page的" if snap.get("credential_mode") == "operator" else "同Page全部"
                outcome = GraphResult(outcome.kind, error_code=outcome.error_code,
                                      message=f"已尝试{scope}{len(credentials)}个可用授权，均被Meta明确拒绝发布",
                                      trace_id=outcome.trace_id)
            result = self.store.record_outcome(task, outcome, credential_id=credential.credential_id,
                                               fb_user_id=credential.fb_user_id, final=final, attempt_id=attempt_id)
            if final or result.get("status") == "stale_claim":
                return result
        return {"ok": True, "status": "no_pending"}

    def reconcile_next(self, worker_id="fb-video-reconcile"):
        if not self.live_enabled:
            return {"ok": True, "status": "live_gate_closed"}
        task = self.store.claim("reconcile", worker_id)
        if task is None:
            return {"ok": True, "status": "no_pending"}
        try:
            credentials = self._task_credentials(task)
        except Exception:
            credentials = []
        if not credentials:
            return self.store.record_outcome(task, GraphResult("unknown", error_code="reconcile_credentials_unavailable",
                                                               message="暂无可用授权查询平台状态，保留已提交占用"), reconcile=True)
        self.rng.shuffle(credentials)
        credentials = credentials[:8]
        for index, credential in enumerate(credentials):
            self.rate_limit()
            if not self.store.claim_is_current(task, status="submitted"):
                return {"ok": True, "status": "stale_claim"}
            try:
                result = self.graph.reconcile_video(task["graph_post_id"], credential)
            except Exception:
                result = GraphResult("unknown", error_code="reconcile_unavailable", message="平台处理状态暂不可查询")
            final = result.kind != "credential_failure" or index == len(credentials) - 1
            if result.kind == "credential_failure" and final:
                result = GraphResult("hold", post_id=task["graph_post_id"], error_code="reconcile_all_credentials_rejected",
                                     message="所有授权均被平台拒绝对账，请人工确认")
            elif result.kind == "definite_failure" and result.error_code != "fb_graph_video_processing_failed":
                result = GraphResult("hold", post_id=task["graph_post_id"], error_code=result.error_code,
                                     message="平台对象无法明确对账，请人工确认")
            response = self.store.record_outcome(task, result, credential_id=credential.credential_id,
                                                 fb_user_id=credential.fb_user_id, final=final, reconcile=True)
            if final or response.get("status") == "stale_claim":
                return response

    def dispatch(self, action, payload):
        if not isinstance(payload, dict) or set(payload) - {"worker_id"}:
            raise DomainError("invalid_request", "worker参数无效")
        worker_id = str(payload.get("worker_id", "fb-video-worker"))
        if not 1 <= len(worker_id) <= 120:
            raise DomainError("invalid_request", "worker标识无效")
        if action == "tick":
            return self.store.tick()
        method = {"prepare-next": self.prepare_next, "link-next": self.link_next,
                  "execute-next": self.execute_next, "reconcile-next": self.reconcile_next}.get(action)
        if method is None:
            raise DomainError("not_found", "接口不存在", 404)
        return method(worker_id)
