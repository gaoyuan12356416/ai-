"""Frozen previews and durable, explicitly resumed deletion runs."""
import copy
import os
import re
import threading
import uuid
from .core import (AssetError, PHASES, STATUSES, TERMINAL_SUCCESS, actor_key,
                   meta_id, normalize_input, normalize_phases, now, summary)
from .graph import GraphError
from .store import StoreError, _process_start


def public_product(product):
    return {k: product.get(k, "") for k in ("id", "name", "kind", "parent_id", "parent_name")}


def public_object(obj):
    keys = ("key", "kind", "object_id", "status", "product_ids", "content_ids",
            "series_codes", "languages", "account_ids", "reason", "result", "updated_at")
    return {key: obj.get(key, "") for key in keys}


class Service:
    def __init__(self, store, source, graph_factory, spawn=None, authorize=None):
        self.store, self.source, self.graph_factory = store, source, graph_factory
        self.spawn = spawn or self._spawn
        self.authorize = authorize or (lambda session: session)
        self.store.recover_interrupted()
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
        objects = [o for o in data["objects"] if (not kind or o["kind"] == kind) and (not status or o["status"] == status)]
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
                if ad.get("reason"):
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
            # Assets backed by any unverified Ad remain blocked. An independent
            # Ad can still be deleted when a creative/video cannot be verified.
            bad_ads = {o["object_id"] for o in objects.values() if o["kind"] == "ad" and o["status"] == "blocked"}
            for obj in objects.values():
                if obj["kind"] != "ad" and set(obj["ad_ids"]) & bad_ads:
                    self._block(obj, "ad_ownership_unverified", "关联广告归属或当前 Creative 无法确认，请查看对应 Ad 阻止原因")
                if obj["kind"] == "video":
                    verified = any(c["kind"] == "creative" and c["status"] != "blocked" and
                        obj["object_id"] in c.get("verified_video_ids", []) and set(c["ad_ids"]) & set(obj["ad_ids"])
                        for c in objects.values())
                    if not verified:
                        self._block(obj, "video_relation_unverified", "无法从本次范围内已核验的 Creative 确认该视频关系")
            allowed_ads -= bad_ads
            assets = [o for o in objects.values() if o["kind"] != "ad" and o["status"] == "pending"]
            self.store.update_job(job_id, preview_step="核验所选范围之外的共享引用")
            for asset_kind in ("creative", "video"):
                kind_assets = [o for o in assets if o["kind"] == asset_kind]
                try:
                    refs = self.source.shared_references(kind_assets, progress=lambda kind, done, total:
                        self.store.update_job(job_id, preview_step="核验 %s 共享引用：%d / %d 段" % (kind, done, total)))
                    for ref in refs:
                        if ref["key"] in objects and str(ref["ad_id"]) not in allowed_ads:
                            self._block(objects[ref["key"]], "shared_outside_scope", "源记录显示范围外 Ad %s 引用该素材" % ref["ad_id"])
                except AssetError as exc:
                    for obj in kind_assets:
                        self._block(obj, "reference_check_incomplete", exc.message)
            for obj in assets:
                if obj["status"] != "pending":
                    continue
                try:
                    state, proof = graph.inspect(obj, allowed_ads)
                    obj.update(status=state, result=proof)
                except AssetError as exc:
                    self._block(obj, exc.code, exc.message)
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
                if phase != "ad":
                    try:
                        phase_objects = [o for o in job["objects"] if o["kind"] == phase and o["status"] in ("pending", "failed")]
                        refs = self.source.shared_references(phase_objects, progress=lambda kind, done, total:
                            self.store.update_job(job_id, execution_step="核验 %s 共享引用：%d / %d 段" % (kind, done, total)))
                        outside_keys = {r["key"] for r in refs if str(r["ad_id"]) not in allowed_ads}
                    except AssetError as exc:
                        refs_error = exc
                for obj in job["objects"]:
                    if obj["kind"] != phase or obj["status"] not in ("pending", "failed"):
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
                        if refs_error:
                            raise refs_error
                        if obj["key"] in outside_keys:
                            raise GraphError("shared_outside_scope", "执行前发现范围外广告引用该素材，已阻止")
                        state, result = graph.inspect(obj, allowed_ads, deleted_creatives)
                        if state == "pending":
                            state, result = graph.delete(obj)
                    except AssetError as exc:
                        state, result = "blocked", {"code": exc.code, "message": exc.message, "checked_at": now()}
                    except Exception:
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

    def reconcile(self, session, job_id, payload):
        job = self.store.get_job(job_id)
        self._allowed(session, job)
        if str(payload.get("preview_id") or "") != job["preview_id"]:
            raise AssetError("preview_mismatch", "预览标识不匹配", 409)
        if job["status"] == "running":
            raise AssetError("job_running", "请等待当前执行结束后核实", 409)
        # A bounded batch keeps the HTTP request short; subsequent clicks only
        # read unresolved objects. No DELETE is issued by this route.
        graph = self.graph_factory()
        checked = 0
        for obj in sorted(job["objects"], key=lambda o: str((o.get("result") or {}).get("checked_at", ""))):
            if obj["status"] != "unknown":
                continue
            state, result = graph.reconcile(obj)
            self.store.reconcile_object(job_id, obj["key"], state, result)
            checked += 1
            if checked >= 1:
                break
        return {"job_id": job_id, "checked": checked, "read_only": True}
