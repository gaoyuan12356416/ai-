"""Frozen previews and durable, explicitly resumed deletion runs."""
import copy
import os
import re
import threading
import uuid
from .core import (AssetError, PHASES, STATUSES, TERMINAL_SUCCESS, account_id, actor_key,
                   meta_id, normalize_input, normalize_phases, now, summary)
from .graph import GraphError
from .store import StoreError, _process_start


def public_product(product):
    return {k: product.get(k, "") for k in ("id", "name", "kind", "parent_id", "parent_name")}


def public_object(obj):
    keys = ("key", "kind", "object_id", "status", "product_ids", "content_ids",
            "series_codes", "languages", "account_ids", "reason", "result", "updated_at", "video_account_results")
    return dict({key: obj.get(key, "") for key in keys},
                video_direct_eligible=obj.get("kind") == "video" and obj.get("status") == "blocked")


class Service:
    def __init__(self, store, source, graph_factory, spawn=None, authorize=None):
        self.store, self.source, self.graph_factory = store, source, graph_factory
        self.spawn = spawn or self._spawn
        self.authorize = authorize or (lambda session: session)
        self.store.recover_interrupted()
        self.store.recover_rechecks()
        for job in self.store.list_jobs(limit=500):
            owner = job.get("preview_owner") or {}
            if job["status"] == "previewing" and owner.get("pid"):
                alive, identity = _process_start(owner["pid"])
                if alive is False or alive is True and identity != owner.get("identity"):
                    self.store.update_job(job["job_id"], status="failed", error={"code": "preview_interrupted", "message": "预览进程已中断，请重新预览；未执行删除"})
        self.preview_slots = threading.BoundedSemaphore(2)

    @staticmethod
    def _spawn(fn, *args):
        thread = threading.Thread(target=fn, args=args, daemon=True, name="meta-asset-delete")
        thread.start()

    def products(self, session):
        return {"items": [public_product(p) for p in self.source.list_products(session)]}

    def _allowed(self, session, job):
        actor = actor_key(session)
        if session.get("role") != "admin" and job["actor"] != actor:
            raise AssetError("job_forbidden", "无权查看或执行此任务", 403)
        selected = self.source.selected_products(session, [p["id"] for p in job["products"]])
        if {(p["id"], p["parent_id"]) for p in selected} != {(p["id"], p["parent_id"]) for p in job["products"]}:
            raise AssetError("product_mapping_changed", "产品与主产品的关联已变化，请重新预览", 409)
        return selected

    @staticmethod
    def _counts(job):
        if "objects" in job:
            job["summary"], job["phase_results"] = summary(job["objects"])
        else:
            totals = dict.fromkeys(STATUSES, 0)
            totals.update(dict.fromkeys(PHASES, 0))
            totals["total"] = 0
            stages = {}
            for kind in PHASES:
                counts = dict.fromkeys(STATUSES, 0)
                counts.update(job.get("object_counts", {}).get(kind, {}))
                counts["total"] = sum(counts.values())
                stages[kind] = counts
                totals[kind] = counts["total"]
                totals["total"] += counts["total"]
                for status in STATUSES:
                    totals[status] += counts[status]
            job["summary"], job["phase_results"] = totals, stages
        job["video_direct_eligible_count"] = job["phase_results"].get("video", {}).get("blocked", 0)
        return job

    @classmethod
    def _public(cls, job):
        data = cls._counts(copy.deepcopy(job))
        for key in ("actor", "object_counts", "preview_owner", "user_ids"):
            data.pop(key, None)
        data["products"] = [public_product(p) for p in data.get("products", [])]
        if "objects" in data:
            data["objects"] = [public_object(o) for o in data["objects"]]
        data["runs"] = [{k: r[k] for k in ("run_id", "request_id", "phases", "status", "created_at", "updated_at", "summary") if k in r} for r in data.get("runs", [])]
        return data

    def list_jobs(self, session):
        permitted = {p["id"] for p in self.source.list_products(session)}
        rows = self.store.list_jobs(None if session.get("role") == "admin" else actor_key(session), limit=100)
        return {"items": [self._public(j) for j in rows if all(p["id"] in permitted for p in j["products"])]}

    def detail(self, session, job_id, page=1, page_size=50, kind="", status=""):
        job = self.store.get_job(job_id)
        self._allowed(session, job)
        if kind and kind not in PHASES or status and status not in STATUSES:
            raise AssetError("invalid_filter", "明细筛选条件无效")
        page, page_size = max(1, int(page)), max(1, min(100, int(page_size)))
        data = self._public(job)
        # Historical blocked Video rows retain their audit state until claimed,
        # but operator filters must match the page's current pending count.
        objects = [o for o in data["objects"] if (not kind or o["kind"] == kind) and
                   (not status or ("pending" if o["video_direct_eligible"] else o["status"]) == status)]
        data.update(total=len(objects), page=page, page_size=page_size, objects=objects[(page-1)*page_size:page*page_size])
        return data

    def preview(self, session, payload):
        input_type, ids, product_ids = normalize_input(payload)
        products = self.source.selected_products(session, product_ids)
        if not self.preview_slots.acquire(blocking=False):
            raise AssetError("preview_busy", "已有两个预览正在查询，请稍后重试", 429)
        try:
            job = self.store.create_job(dict(job_id=uuid.uuid4().hex, preview_id=uuid.uuid4().hex,
                actor=actor_key(session), actor_name=str(session.get("name") or ""), input_type=input_type,
                ids=ids, products=products, status="previewing", preview_started_at=now(), objects=[],
                preview_owner={"pid": os.getpid(), "identity": _process_start(os.getpid())[1]}))
            self.spawn(self._build_preview, job["job_id"])
        except Exception:
            self.preview_slots.release()
            raise
        return self._public(job)

    @staticmethod
    def _block(obj, code, message):
        obj.update(status="blocked", reason=message, result={"code": code, "message": message, "checked_at": now()})

    @staticmethod
    def _error_result(exc):
        result = {"code": exc.code, "message": exc.message, "checked_at": now()}
        if getattr(exc, "detail", None):
            result["detail"] = copy.deepcopy(exc.detail)
            for key in ("credential_user_id", "account_diagnostic"):
                if key in exc.detail:
                    result[key] = copy.deepcopy(exc.detail[key])
        return result

    @staticmethod
    def _reference_step(kind, done, total):
        if kind == "video":
            return "视频引用索引：已读取 %s 条源记录%s" % (format(done, ","), "，完整读取结束" if total else "，正在完整读取")
        return "核验 Creative 共享引用：%d / %d" % (done, total)

    def _build_preview(self, job_id):
        try:
            job = self.store.get_job(job_id)
            dramas, blockers = self.source.resolve_dramas(job["input_type"], job["ids"], job["products"])
            self.store.update_job(job_id, preview_step="查找精确关联的 Meta 广告", dramas=dramas, blockers=blockers)
            ads, issues = self.source.resolve_ads(dramas, job["products"])
            blockers.extend(issues)
            if len(ads) > 10000:
                raise AssetError("too_many_ads", "命中超过 10,000 个 Ad，请缩小范围")
            objects = {}
            defaults = {p["id"]: p.get("default_user") for p in job["products"]}

            def add(kind, oid, ad):
                try:
                    oid = meta_id(oid)
                except AssetError:
                    blockers.append(dict(code="invalid_meta_id", message="广告记录包含无效的 %s ID" % kind, product_id=ad["product_id"], input_id=",".join(ad["content_ids"])))
                    return None
                key = kind + ":" + oid
                obj = objects.setdefault(key, dict(key=key, kind=kind, object_id=oid, status="pending", reason="", result={},
                    product_ids=[], content_ids=[], series_codes=[], languages=[], account_ids=[], user_ids=[], ad_ids=[], creative_ids=[], video_ids=[]))
                values = dict(product_ids=[ad["product_id"]], content_ids=ad["content_ids"],
                    series_codes=[d["series_code"] for d in ad["dramas"]], languages=[d["language"] for d in ad["dramas"]],
                    account_ids=[ad["account_id"]], user_ids=[ad.get("user_id"), defaults.get(ad["product_id"])],
                    ad_ids=[ad["ad_id"]], creative_ids=[ad.get("creative_id")], video_ids=ad.get("video_ids", []))
                for name, entries in values.items():
                    obj[name] = sorted(set(obj[name]) | {str(x) for x in entries if x and str(x) not in ("0", "NULL")})
                if kind == "video":
                    evidence = dict(account_id=ad["account_id"], source_row_id=str(ad.get("row_id") or ""),
                        ad_id=ad["ad_id"], product_id=ad["product_id"], user_id=str(ad.get("user_id") or ""))
                    sources = obj.setdefault("video_account_sources", [])
                    if evidence not in sources:
                        sources.append(evidence)
                if kind != "video" and ad.get("reason"):
                    self._block(obj, "source_ownership_conflict", ad["reason"])
                return obj

            for ad in ads:
                add("ad", ad["ad_id"], ad)
                if ad.get("creative_id"):
                    add("creative", ad["creative_id"], ad)
                for vid in ad.get("video_ids", []):
                    add("video", vid, ad)
            graph = self.graph_factory()
            allowed_ads = {o["object_id"] for o in objects.values() if o["kind"] == "ad" and o["status"] != "blocked"}
            self.store.update_job(job_id, preview_step="核验 Meta 对象及广告账户关系")
            # Verify ads first so changed creatives cannot authorize deleting the
            # old source record's assets. Freeze any discovered video IDs here.
            for kind in ("ad", "creative"):
                for obj in list(objects.values()):
                    if obj["kind"] != kind or obj["status"] == "blocked":
                        continue
                    try:
                        state, proof = graph.inspect(obj, allowed_ads, check_references=False)
                        obj.update(status=state, result=proof)
                        if kind == "ad" and proof.get("creative_id") and not obj["creative_ids"]:
                            for ad in ads:
                                if ad["ad_id"] == obj["object_id"]:
                                    ad["creative_id"] = proof["creative_id"]
                                    obj["creative_ids"] = [proof["creative_id"]]
                                    add("creative", proof["creative_id"], ad)
                        if kind == "creative":
                            obj["account_verified"] = True
                            obj["verified_video_ids"] = proof.get("video_ids", [])
                            obj["video_ids"] = sorted(set(obj["video_ids"]) | set(proof.get("video_ids", [])))
                            for ad in ads:
                                if str(ad.get("creative_id")) == obj["object_id"]:
                                    for vid in proof.get("video_ids", []):
                                        add("video", vid, ad)
                    except AssetError as exc:
                        self._block(obj, exc.code, exc.message)
                        obj["result"] = self._error_result(exc)
            # Creative retains ownership checks. Matched Video IDs are frozen
            # for direct deletion, independently of Meta read availability.
            bad_ads = {o["object_id"] for o in objects.values() if o["kind"] == "ad" and o["status"] == "blocked"}
            for obj in objects.values():
                if obj["kind"] == "creative" and set(obj["ad_ids"]) & bad_ads:
                    self._block(obj, "ad_ownership_unverified", "关联广告归属或当前 Creative 无法确认，请查看对应 Ad 阻止原因")
            allowed_ads -= bad_ads
            assets = [o for o in objects.values() if o["kind"] == "creative" and o["status"] == "pending"]
            self.store.update_job(job_id, preview_step="核验所选范围之外的共享引用")
            for asset_kind in ("creative",):
                kind_assets = [o for o in assets if o["kind"] == asset_kind]
                try:
                    refs = self.source.shared_references(kind_assets, progress=lambda kind, done, total:
                        self.store.update_job(job_id, preview_step=self._reference_step(kind, done, total)))
                    for ref in refs:
                        if ref["key"] in objects and str(ref["ad_id"]) not in allowed_ads:
                            self._block(objects[ref["key"]], "shared_outside_scope", "源记录显示范围外 Ad %s 引用该素材" % ref["ad_id"])
                except AssetError as exc:
                    for obj in kind_assets:
                        self._block(obj, exc.code, exc.message)
            for obj in assets:
                if obj["status"] != "pending":
                    continue
                try:
                    state, proof = graph.inspect(obj, allowed_ads)
                    obj.update(status=state, result=proof)
                except AssetError as exc:
                    self._block(obj, exc.code, exc.message)
                    obj["result"] = self._error_result(exc)
            ordered = sorted(objects.values(), key=lambda o: (PHASES.index(o["kind"]), o["object_id"]))
            self.store.update_job(job_id, status="ready", objects=ordered, dramas=dramas, blockers=blockers,
                                  preview_step="预览完成", preview_completed_at=now())
        except Exception as exc:
            error = {"code": exc.code, "message": exc.message} if isinstance(exc, AssetError) else {"code": "preview_failed", "message": "预览未完成，请重新预览；未执行删除"}
            try:
                self.store.update_job(job_id, status="failed", error=error, preview_step="预览失败")
            except Exception:
                pass  # no Graph write has occurred
        finally:
            self.preview_slots.release()

    def execute(self, session, job_id, payload):
        phases = normalize_phases(payload.get("phases"))
        request_id = str(payload.get("request_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", request_id):
            raise AssetError("invalid_request_id", "执行请求标识无效，请刷新页面后重试")
        if any(k in payload for k in ("objects", "object_ids", "ad_ids", "creative_ids", "video_ids")):
            raise AssetError("frozen_preview_only", "执行仅接受预览清单，不能追加对象 ID")
        job = self.store.get_job(job_id)
        self._allowed(session, job)
        run = self.store.claim_run(job_id, str(payload.get("preview_id") or ""), actor_key(session), phases, request_id)
        if not run["duplicate"]:
            try:
                self.spawn(self._run, job_id, run["run_id"], phases, copy.deepcopy(session))
            except Exception:
                self.store.finish_run(run["run_id"], "interrupted", {"message": "后台执行线程未启动，请人工恢复"})
                raise AssetError("worker_unavailable", "后台执行未启动，进度已保留，请人工恢复", 503) from None
        return {k: run[k] for k in ("job_id", "run_id", "status", "duplicate")}

    def _run(self, job_id, run_id, phases, session):
        try:
            job = self.store.get_job(job_id)
            graph = self.graph_factory()
            allowed_ads = {o["object_id"] for o in job["objects"] if o["kind"] == "ad" and o["status"] != "blocked"}
            deleted_creatives = {o["object_id"] for o in job["objects"] if o["kind"] == "creative" and o["status"] in TERMINAL_SUCCESS}
            for phase in phases:
                fresh_session = self.authorize(session)
                self._allowed(fresh_session, job)
                self.store.update_job(job_id, current_phase=phase)
                refs_error, outside_keys = None, set()
                if phase == "creative":
                    try:
                        phase_objects = [o for o in job["objects"] if o["kind"] == phase and o["status"] in ("pending", "failed")]
                        refs = self.source.shared_references(phase_objects, progress=lambda kind, done, total:
                            self.store.update_job(job_id, execution_step=self._reference_step(kind, done, total)))
                        outside_keys = {r["key"] for r in refs if str(r["ad_id"]) not in allowed_ads}
                    except AssetError as exc:
                        refs_error = exc
                for obj in job["objects"]:
                    eligible = obj["status"] in ("pending", "failed") or phase == "video" and obj["status"] == "blocked"
                    if obj["kind"] != phase or not eligible:
                        continue
                    self._allowed(self.authorize(session), job)
                    # Claim first: the lock covers the final read check and the
                    # mutation, so a concurrent task cannot change the same node.
                    claim = self.store.claim_object(job_id, obj["key"], run_id)
                    if not claim["claimed"]:
                        if phase == "creative" and claim["status"] in TERMINAL_SUCCESS:
                            deleted_creatives.add(obj["object_id"])
                        continue
                    try:
                        if phase == "video":
                            if callable(getattr(graph, "prepare_video_account_delete", None)):
                                state, result = self._run_video_accounts(graph, obj, job, run_id, session)
                            elif callable(getattr(graph, "prepare_video_delete", None)):
                                prepared = graph.prepare_video_delete(obj)
                                self.store.record_object_credential(job_id, obj["key"], run_id, prepared[1])
                                self._allowed(self.authorize(session), job)
                                state, result = graph.delete(obj, prepared=prepared)
                            else:
                                state, result = graph.delete(obj)
                        else:
                            if refs_error:
                                raise refs_error
                            if obj["key"] in outside_keys:
                                raise GraphError("shared_outside_scope", "执行前发现范围外广告引用该素材，已阻止")
                            state, result = graph.inspect(obj, allowed_ads, deleted_creatives)
                            if state == "pending":
                                state, result = graph.delete(obj)
                    except StoreError:
                        # Failure to record the selected identity stops new
                        # writes, including the very first request of this item.
                        raise
                    except AssetError as exc:
                        if phase == "video" and callable(getattr(graph, "prepare_video_account_delete", None)):
                            # Account attempts already retain their own outcomes.
                            # Stop the run with the original authorization/input
                            # error instead of overwriting its account progress.
                            raise
                        state, result = "failed" if phase == "video" else "blocked", self._error_result(exc)
                    except Exception:
                        if phase == "video" and callable(getattr(graph, "prepare_video_account_delete", None)):
                            raise
                        # Unexpected adapter failure may occur after a request.
                        state, result = "unknown", {"code": "unexpected_outcome", "message": "对象处理未正常返回，需要核实", "checked_at": now()}
                    # If persistence fails, propagate and stop the entire run;
                    # never start another deletion without a writable ledger.
                    self.store.finish_object(job_id, obj["key"], run_id, state, result)
                    if phase == "creative" and state in TERMINAL_SUCCESS:
                        deleted_creatives.add(obj["object_id"])
            final = self.store.get_job(job_id)
            totals, stages = summary(final["objects"])
            self.store.finish_run(run_id, "completed", {"summary": totals, "phase_results": stages})
        except Exception as exc:
            error = {"code": exc.code, "message": exc.message} if isinstance(exc, AssetError) else {"code": "ledger_or_worker_interrupted", "message": "台账或执行中断，已停止新增删除请求，请人工恢复"}
            try:
                self.store.finish_run(run_id, "interrupted", error)
            except Exception:
                pass  # restart recovery fences the durable active claim

    def _run_video_accounts(self, graph, obj, job, run_id, session):
        """Each account has its own durable attempt and success receipt."""
        job_id, key, video_id = job["job_id"], obj["key"], obj["object_id"]
        accounts = sorted({account_id(value) for value in obj.get("account_ids", [])})
        if not accounts:
            return "failed", dict(delete_mode="ad_account_video", code="missing_ad_account",
                message="冻结记录缺少广告账户，无法构造账户视频删除请求", account_results=[], checked_at=now())
        self.store.begin_video_accounts(job_id, key, run_id)
        for aid in accounts:
            self._allowed(self.authorize(session), job)
            prepared, error = None, None
            context = dict(delete_mode="ad_account_video", delete_account_id=aid,
                delete_endpoint="act_" + aid + "/advideos")
            try:
                prepared = graph.prepare_video_account_delete(obj, aid)
                context = prepared[1]
            except AssetError as exc:
                error = self._error_result(exc)
            except Exception:
                error = {"code": "credential_unavailable", "message": "投放凭证读取未完成，尚未发送删除请求"}
            claim = self.store.claim_video_account(job_id, key, run_id, aid, context)
            if not claim["claimed"]:
                continue
            if error is not None:
                state, result = "failed", dict(context, **error)
            else:
                try:
                    self._allowed(self.authorize(session), job)
                    state, result = graph.delete_video_account(obj, aid, prepared=prepared)
                except AssetError as exc:
                    state, result = "failed", dict(context, **self._error_result(exc))
                except Exception:
                    state, result = "unknown", dict(context, code="unexpected_outcome",
                        message="账户视频请求未正常返回，需要核实后再处理")
            result = dict(result, delete_mode="ad_account_video", delete_scope="ad_account_video",
                account_id=aid, video_id=video_id, checked_at=now())
            self.store.finish_video_account(job_id, key, run_id, aid, claim["account_attempt_id"], state, result)
        results = self.store.video_account_results(job_id, key)
        states = {item["status"] for item in results}
        state = "unknown" if states & {"unknown", "in_progress"} else "deleted" if results and states.issubset(TERMINAL_SUCCESS) else "failed"
        return state, dict(delete_mode="ad_account_video", delete_scope="ad_account_video", video_id=video_id,
            account_results=results, success=state == "deleted", checked_at=now(),
            message="所列广告账户的视频素材已删除" if state == "deleted" else "部分账户尚未完成，请查看逐账户结果")

    def reconcile(self, session, job_id, payload):
        job = self.store.get_job(job_id)
        self._allowed(session, job)
        if str(payload.get("preview_id") or "") != job["preview_id"]:
            raise AssetError("preview_mismatch", "预览标识不匹配", 409)
        if job["status"] == "running" or job.get("recheck", {}).get("status") == "running":
            raise AssetError("job_running", "请等待当前执行结束后核实", 409)
        # A bounded batch keeps the HTTP request short; subsequent clicks only
        # read unresolved objects. No DELETE is issued by this route.
        graph = self.graph_factory()
        checked = 0
        for obj in sorted(job["objects"], key=lambda o: str((o.get("result") or {}).get("checked_at", ""))):
            if obj["status"] != "unknown":
                continue
            account_results = obj.get("video_account_results") or []
            if obj["kind"] == "video" and (account_results or obj.get("result", {}).get("delete_mode") == "ad_account_video"):
                unresolved = [item for item in account_results if item["status"] == "unknown"]
                if not unresolved:
                    continue
                target = min(unresolved, key=lambda item: str(item.get("result", {}).get("checked_at", "")))
                state, result = graph.reconcile_video_account(obj, target["account_id"])
                self.store.reconcile_video_account(job_id, obj["key"], target["account_id"], state, result)
            else:
                state, result = graph.reconcile(obj)
                self.store.reconcile_object(job_id, obj["key"], state, result)
            checked += 1
            if checked >= 1:
                break
        return {"job_id": job_id, "checked": checked, "read_only": True}

    def recheck(self, session, job_id, payload):
        if set(payload) - {"preview_id", "request_id"}:
            raise AssetError("frozen_preview_only", "重新核验仅使用原任务固定清单，不能追加或修改对象")
        request_id = str(payload.get("request_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", request_id):
            raise AssetError("invalid_request_id", "核验请求标识无效")
        job = self.store.get_job(job_id)
        self._allowed(session, job)
        result = self.store.claim_recheck(job_id, str(payload.get("preview_id") or ""), actor_key(session), request_id,
                                         sum(o["status"] == "blocked" and o["kind"] != "video" for o in job["objects"]))
        if not result["duplicate"]:
            try:
                self.spawn(self._recheck, job_id, result["operation_id"], copy.deepcopy(session))
            except Exception:
                self.store.update_recheck(result["operation_id"], status="interrupted", step="核验线程未启动，请重新核验")
                raise AssetError("worker_unavailable", "后台核验未启动，未执行删除", 503) from None
        return result

    def _recheck(self, job_id, operation_id, session):
        # Ownership/lineage conflicts still require a new preview. Read/transient
        # reference blockers alone are eligible for clearing on this frozen list.
        retryable = {"reference_check_incomplete", "shared_outside_scope", "source_unavailable",
                     "read_unavailable", "video_owner_unverified", "video_index_incomplete",
                     "video_index_expired", "video_index_malformed", "video_index_unavailable",
                     "video_index_building", "video_index_disk_full", "video_index_too_large", "video_reference_unverified"}
        checked, released = 0, 0
        try:
            job = self.store.get_job(job_id)
            self._allowed(self.authorize(session), job)
            graph = self.graph_factory()
            blocked = [o for o in job["objects"] if o["status"] == "blocked" and o["kind"] != "video"]
            allowed_ads = {o["object_id"] for o in job["objects"] if o["kind"] == "ad" and o["status"] != "blocked"}
            deleted_creatives = {o["object_id"] for o in job["objects"] if o["kind"] == "creative" and o["status"] in TERMINAL_SUCCESS}
            for phase in PHASES:
                objects = [o for o in blocked if o["kind"] == phase]
                if not objects:
                    continue
                candidates = [o for o in objects if o.get("result", {}).get("code") in retryable]
                ref_error, refs = None, []
                if phase != "ad" and candidates:
                    try:
                        refs = self.source.shared_references(candidates, progress=lambda kind, done, total:
                            self.store.update_recheck(operation_id, step=self._reference_step(kind, done, total)))
                    except AssetError as exc:
                        ref_error = exc
                outside = {r["key"] for r in refs if str(r["ad_id"]) not in allowed_ads}
                for obj in objects:
                    self._allowed(self.authorize(session), job)
                    if obj in candidates:
                        try:
                            if ref_error:
                                raise ref_error
                            if obj["key"] in outside:
                                raise GraphError("shared_outside_scope", "仍有范围外广告引用该素材，保持阻止")
                            state, result = graph.inspect(obj, allowed_ads, deleted_creatives)
                        except AssetError as exc:
                            state, result = "blocked", self._error_result(exc)
                        self.store.recheck_object(operation_id, obj["key"], state, result)
                        released += state != "blocked"
                    checked += 1
                    self.store.update_recheck(operation_id, checked=checked, released=released,
                        step="已核验 %d / %d 个阻止项；%d 个通过，尚未执行删除" % (checked, len(blocked), released))
            self.store.update_recheck(operation_id, status="completed", checked=checked, released=released,
                step="核验完成：%d 个通过，%d 个仍阻止。执行需另行确认" % (released, len(blocked)-released))
        except Exception as exc:
            error = self._error_result(exc) if isinstance(exc, AssetError) else dict(code="recheck_interrupted", message="核验或台账写入中断，保持未核验项原状态")
            try:
                self.store.update_recheck(operation_id, status="interrupted", error=error, step=error["message"])
            except Exception:
                pass  # process recovery only marks interrupted; never resumes writes
