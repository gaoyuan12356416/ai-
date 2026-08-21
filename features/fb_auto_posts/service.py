"""Loopback admin API, scheduler and bounded executor for FB Page auto posts."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Mapping
from urllib.parse import parse_qs, urlsplit

from .client import FB_AUTO_ADMIN_PREFIX, contains_sensitive_key
from .core import ActorScope, FBAutoPostStore, StoreError
from .gpu import GPUPrepareClient, PrepareExecutor
from .publisher import AutoPostExecutor, RequestsGraphTransport
from .repositories import MaterialRepository, PagePoolRepository, ReadOnlyMySQL, RepositoryError
from .validation import ValidationError, valid_internal_bearer


class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400, conflicts: Any = None):
        self.code, self.status, self.conflicts = code, status, conflicts if isinstance(conflicts, list) else []
        super().__init__(message)


class Runtime:
    def __init__(self, store: FBAutoPostStore, pages: PagePoolRepository, materials: MaterialRepository, executor: AutoPostExecutor, preparer: PrepareExecutor, internal_token: str, *, metric_store: FBAutoPostStore | None = None, max_daily_jobs: int = 500, max_publishable_pages: int = 500, max_jobs_per_slot: int = 20, max_enabled_templates: int = 10, prepare_ahead_seconds: int = 14400, prebuild_days_ahead: int = 1, max_late_seconds: int = 600, prebuild_enabled: bool | None = None):
        self.store, self.pages, self.materials, self.executor, self.preparer, self.internal_token = store, pages, materials, executor, preparer, internal_token
        self.max_daily_jobs, self.max_publishable_pages = int(max_daily_jobs), int(max_publishable_pages)
        self.max_jobs_per_slot = int(max_jobs_per_slot)
        self.max_enabled_templates, self.prepare_ahead_seconds = int(max_enabled_templates), int(prepare_ahead_seconds)
        self.prebuild_days_ahead, self.max_late_seconds = int(prebuild_days_ahead), int(max_late_seconds)
        self.prebuild_enabled = bool(executor.live_enabled) if prebuild_enabled is None else prebuild_enabled is True
        self.store.max_late_seconds = self.max_late_seconds
        if hasattr(self.preparer, "live_enabled"):
            self.preparer.live_enabled = self.prebuild_enabled
        self.metric_store = metric_store or store
        self._tick_lock = threading.Lock()

    @staticmethod
    def actor(payload: Dict[str, Any]) -> ActorScope:
        return ActorScope.from_payload(payload.pop("_actor", None))

    @staticmethod
    def pagination(query: Mapping[str, list[str]]) -> tuple[int, int]:
        try: limit, offset = int((query.get("limit") or ["50"])[0]), int((query.get("offset") or ["0"])[0])
        except (ValueError, TypeError): raise ServiceError("invalid_request", "分页参数无效", 400) from None
        if not 1 <= limit <= 200 or not 0 <= offset <= 1_000_000: raise ServiceError("invalid_request", "分页参数无效", 400)
        return limit, offset

    def groups(self, actor: ActorScope) -> Dict[str, Any]:
        groups = self.pages.list_groups(is_admin=actor.is_admin, owner_user_id=actor.owner_user_id)
        items = [{**as_public(group), "group_label": "Post" if group.group_type == 0 else "AD", "missing_token_pages": group.total_pages - group.publishable_pages} for group in groups]
        return {"ok": True, "items": items, "summary": {"total_groups": len(items), "total_pages": sum(item["total_pages"] for item in items), "publishable_pages": sum(item["publishable_pages"] for item in items), "missing_token_pages": sum(item["total_pages"] - item["publishable_pages"] for item in items)}}

    def resolve_source(self, payload: Mapping[str, Any], actor: ActorScope) -> Dict[str, str]:
        group_ids = payload.get("group_ids")
        if not isinstance(group_ids, list):
            raise ServiceError("invalid_request", "Page池ID无效", 400)
        wanted = {str(item) for item in group_ids}
        groups = [group for group in self.pages.list_groups(is_admin=actor.is_admin, owner_user_id=actor.owner_user_id) if group.group_id in wanted]
        if len(groups) != len(wanted):
            raise ServiceError("fb_auto_page_group_not_found", "Page池不存在或不属于当前负责人", 404)
        sources = {(group.app_id, group.product) for group in groups}
        if len(sources) != 1:
            raise ServiceError("fb_auto_mixed_product_groups", "首版模板只能选择同一产品的Page池", 409)
        app_id, product = next(iter(sources))
        if not app_id or not product:
            raise ServiceError("fb_auto_group_product_missing", "Page池缺少产品映射", 409)
        if app_id != "1479" or product != "Dramawave":
            raise ServiceError("fb_auto_product_mapping_unsupported", "首发仅支持Dramawave Page池，未知产品禁止启用", 409)
        return {"app_id": "1479", "product": "Dramawave", "material_data_source": 6, "metric_product": "Dramawave", "metric_platform": 0}

    def validate_activation(self, template: Mapping[str, Any]) -> Dict[str, Any]:
        config = template["config"]
        legacy = self.pages.legacy_conflicts(config["group_ids"])
        if legacy: raise ServiceError("fb_auto_legacy_queue_conflict", "所选Page池仍被旧版自动发布队列占用", 409, legacy)
        exclusive = self.store.enabled_group_conflicts(int(template["id"]), config["group_ids"])
        if exclusive: raise ServiceError("fb_auto_group_template_conflict", "所选Page池已被其他新版启用模板独占", 409, exclusive)
        pages = self.pages.list_pages(config["group_ids"], is_admin=bool(template["scope_is_admin"]), owner_user_id=str(template["owner_user_id"]))
        page_ids = {page.page_id for page in pages}
        page_conflicts, other_slot_jobs = [], 0
        enabled_sources = self.store.enabled_template_sources(int(template["id"]))
        enabled_fingerprint = [(int(item["template_id"]),int(item["template_version"])) for item in enabled_sources]
        for other in enabled_sources:
            other_pages = self.pages.list_pages(other["config"]["group_ids"], is_admin=other["scope_is_admin"], owner_user_id=other["owner_user_id"])
            other_slot_jobs += sum(page.eligible_token_count > 0 for page in other_pages)
            overlap = sorted(page_ids.intersection(page.page_id for page in other_pages))
            if overlap:
                page_conflicts.append({"template_id": other["template_id"], "template_name": other["template_name"], "overlap_count": len(overlap), "page_ids": overlap[:20]})
        if page_conflicts:
            raise ServiceError("fb_auto_page_template_conflict", "所选Page与其他启用模板存在重叠", 409, page_conflicts[:20])
        publishable = sum(page.eligible_token_count > 0 for page in pages)
        schedule = config["schedule"]
        daily_count = len(schedule["times"]) if schedule["mode"] == "fixed" else int(schedule["daily_count"])
        daily_jobs = publishable * daily_count
        with self.store.connect() as conn:
            enabled_count = int(conn.execute("SELECT COUNT(*) FROM fb_auto_template WHERE status='enabled' AND id<>?", (int(template["id"]),)).fetchone()[0])
        global_slot_jobs = publishable + other_slot_jobs
        summary = {"total_pages": len(pages), "publishable_pages": publishable, "missing_token_pages": len(pages) - publishable, "daily_frequency": daily_count, "estimated_jobs_per_slot": publishable, "estimated_global_jobs_per_slot": global_slot_jobs, "estimated_daily_gpu_jobs": daily_jobs, "estimated_daily_graph_posts": daily_jobs, "capacity_limits": {"publishable_pages": self.max_publishable_pages, "jobs_per_slot": self.max_jobs_per_slot, "daily_jobs": self.max_daily_jobs, "enabled_templates": self.max_enabled_templates, "prepare_ahead_seconds": self.prepare_ahead_seconds, "prebuild_days_ahead": self.prebuild_days_ahead, "max_late_seconds": self.max_late_seconds}}
        summary["_enabled_fingerprint"] = enabled_fingerprint
        if not pages: raise ServiceError("fb_auto_page_pool_empty", "所选Page池没有有效Page", 409)
        if not publishable: raise ServiceError("fb_auto_page_pool_unpublishable", "所选Page池没有任何可发布Page", 409)
        if publishable > self.max_publishable_pages or global_slot_jobs > self.max_jobs_per_slot or daily_jobs > self.max_daily_jobs or enabled_count + 1 > self.max_enabled_templates:
            raise ServiceError("fb_auto_capacity_exceeded", f"容量门禁拒绝启用：可发布Page {publishable}/{self.max_publishable_pages}，全局最坏同槽GPU任务 {global_slot_jobs}/{self.max_jobs_per_slot}，每日GPU任务和Graph发布 {daily_jobs}/{self.max_daily_jobs}，启用模板 {enabled_count + 1}/{self.max_enabled_templates}", 409)
        if config.get("video_template") != "random_overlay":
            raise ServiceError("fb_auto_video_template_required", "视频制作模板必填，当前仅支持随机排重模板", 409)
        today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).date()
        metric_dates = [(today - timedelta(days=offset)).isoformat() for offset in range(int(config["metric_window_days"]), 0, -1)]
        self.metric_store.load_metric_window(product="Dramawave", platform=0, dates=metric_dates)
        return summary

    def tick(self) -> Dict[str, Any]:
        if not self._tick_lock.acquire(blocking=False):
            return {"ok": True, "status": "already_running", "stale_marked_unknown": 0, "runs": []}
        try:
            stale = self.store.mark_stale_running_unknown()
            if not self.prebuild_enabled:
                status = "live_gate_closed" if not self.executor.live_enabled else "prebuild_gate_closed"
                return {"ok": True, "status": status, "stale_marked_unknown": stale, "enqueued": 0, "skipped_today_templates": 0}
            result = self.store.enqueue_due_slots(live_enabled=True, prepare_ahead_seconds=self.prepare_ahead_seconds, prebuild_days_ahead=self.prebuild_days_ahead)
            result["stale_marked_unknown"] = stale
            return result
        finally:
            self._tick_lock.release()

    def plan_next(self, worker_id: str, lease_seconds: int = 1800) -> Dict[str, Any]:
        if not self.prebuild_enabled:
            status = "live_gate_closed" if not self.executor.live_enabled else "prebuild_gate_closed"
            return {"ok": True, "status": status, "claimed": False}
        due = self.store.claim_due_slot(worker_id, lease_seconds, max_late_seconds=self.max_late_seconds)
        if due is None:
            return {"ok": True, "status": "no_due_slot", "claimed": False}
        actor = ActorScope("scheduler", "自动调度", bool(due["scope_is_admin"]), str(due["owner_user_id"]))
        try:
            trigger_type = "manual" if str(due.get("trigger_type") or "auto") == "manual" else "auto"
            result = self.store.create_run(
                int(due["template_id"]),
                str(due["slot_key"]),
                trigger_type,
                actor,
                self.pages,
                self.materials,
                planned_publish_at_utc=str(due["planned_publish_at_utc"]),
                expected_template_version=int(due["template_version"]),
                expected_due_id=int(due["id"]),
                expected_due_lease_owner=str(due["lease_owner"]),
                expected_due_lease_expires_at_utc=str(due["lease_expires_at_utc"]),
                max_publishable_pages=self.max_publishable_pages,
                max_jobs_per_slot=self.max_jobs_per_slot,
                max_daily_jobs=self.max_daily_jobs,
            )
            self.store.complete_due_slot(int(due["id"]), run_id=int(result["run_id"]), expected_lease_owner=str(due["lease_owner"]), expected_lease_expires_at_utc=str(due["lease_expires_at_utc"]))
            return result
        except (StoreError, RepositoryError) as exc:
            if exc.code == "fb_auto_due_slot_template_changed":
                self.store.complete_due_slot(int(due["id"]), error_code=exc.code, expected_lease_owner=str(due["lease_owner"]), expected_lease_expires_at_utc=str(due["lease_expires_at_utc"]))
                return {"ok": False, "status": "failed", "due_slot_id": int(due["id"]), "error": exc.code}
            self.store.defer_due_slot(int(due["id"]), exc.code, expected_lease_owner=str(due["lease_owner"]), expected_lease_expires_at_utc=str(due["lease_expires_at_utc"]))
            return {"ok": False, "status": "deferred", "due_slot_id": int(due["id"]), "error": exc.code}

    def prepare_next(self, worker_id: str, lease_seconds: int = 10200) -> Dict[str, Any]:
        if not self.prebuild_enabled:
            return {"ok": True, "status": "prebuild_gate_closed", "claimed": False}
        return self.preparer.prepare_next(worker_id, lease_seconds)


def as_public(value: Any) -> Dict[str, Any]:
    return {key: item for key, item in vars(value).items() if key != "token"}


class Handler(BaseHTTPRequestHandler):
    runtime: Runtime
    server_version = "FBAutoPost/1"

    def log_message(self, _format: str, *_args: Any) -> None: pass

    def send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        safe = dict(payload)
        if contains_sensitive_key(safe):
            status, safe = 500, {"ok": False, "code": "fb_auto_unsafe_response", "message": "服务拒绝输出敏感字段"}
        raw = json.dumps(safe, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=UTF-8"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def read_json(self) -> Dict[str, Any]:
        try: length = int(self.headers.get("Content-Length", "0"))
        except ValueError: length = -1
        if not 0 <= length <= 256 * 1024: raise ServiceError("invalid_request", "请求体大小无效", 400)
        try: value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (UnicodeError, ValueError): raise ServiceError("invalid_request", "请求体不是有效JSON", 400) from None
        if not isinstance(value, dict) or contains_sensitive_key(value): raise ServiceError("invalid_request", "请求体包含无效字段", 400)
        return value

    def internal(self) -> bool:
        supplied = str(self.headers.get("Authorization") or "")
        return supplied == "Bearer " + self.runtime.internal_token

    def dispatch(self) -> None:
        parsed, method = urlsplit(self.path), self.command
        if method == "GET" and parsed.path == "/health":
            self.send_json(200, {"ok": True, "service": "fb-auto-post", "prebuild_enabled": self.runtime.prebuild_enabled, "live_enabled": self.runtime.executor.live_enabled}); return
        if parsed.path.startswith("/internal/"):
            if not self.internal(): raise ServiceError("forbidden", "内部请求未授权", 403)
            payload = self.read_json()
            if method == "POST" and parsed.path == "/internal/fb-auto-post/tick": result = self.runtime.tick()
            elif method == "POST" and parsed.path == "/internal/fb-auto-post/plan-next": result = self.runtime.plan_next(str(payload.get("worker_id") or "fb-auto-plan")[:120], int(payload.get("lease_seconds") or 1800))
            elif method == "POST" and parsed.path == "/internal/fb-auto-post/prepare-next": result = self.runtime.prepare_next(str(payload.get("worker_id") or "fb-auto-prepare")[:120], int(payload.get("lease_seconds") or 10200))
            elif method == "POST" and parsed.path == "/internal/fb-auto-post/execute-next": result = self.runtime.executor.execute_next(str(payload.get("worker_id") or "fb-auto-runner")[:120], int(payload.get("lease_seconds") or 1200))
            elif method == "POST" and parsed.path == "/internal/fb-auto-post/reconcile-next": result = self.runtime.executor.reconcile_next(str(payload.get("worker_id") or "fb-auto-reconcile")[:120], int(payload.get("lease_seconds") or 1200))
            else: raise ServiceError("not_found", "接口不存在", 404)
            self.send_json(200, result); return
        auth = str(self.headers.get("Authorization") or "")
        if auth != "Bearer " + self.runtime.internal_token: raise ServiceError("forbidden", "内部请求未授权", 403)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if method == "GET":
            actor_raw = self.headers.get("X-FB-Auto-Actor")
            try: actor = ActorScope.from_payload(json.loads(actor_raw or "{}"))
            except (ValueError, TypeError): raise ServiceError("invalid_request", "操作人范围无效", 400) from None
            if parsed.path == FB_AUTO_ADMIN_PREFIX + "/groups": result = self.runtime.groups(actor)
            elif parsed.path == FB_AUTO_ADMIN_PREFIX + "/templates":
                limit, offset = self.runtime.pagination(query); result = self.runtime.store.list_templates(actor, status=(query.get("status") or [""])[0], query=(query.get("q") or [""])[0], limit=limit, offset=offset)
            elif parsed.path == FB_AUTO_ADMIN_PREFIX + "/runs":
                limit, offset = self.runtime.pagination(query); result = self.runtime.store.list_runs(actor, limit=limit, offset=offset)
            else:
                match = re.fullmatch(re.escape(FB_AUTO_ADMIN_PREFIX) + r"/(templates|runs)/([1-9][0-9]*)", parsed.path)
                if not match: raise ServiceError("not_found", "接口不存在", 404)
                result = ({"ok": True, "template": self.runtime.store.get_template(int(match.group(2)), actor)} if match.group(1) == "templates" else self.runtime.store.get_run(int(match.group(2)), actor))
            self.send_json(200, result); return
        if method != "POST": raise ServiceError("not_found", "接口不存在", 404)
        payload = self.read_json(); actor = self.runtime.actor(payload)
        if parsed.path == FB_AUTO_ADMIN_PREFIX + "/templates":
            source = self.runtime.resolve_source(payload, actor)
            result = {"ok": True, "template": self.runtime.store.create_template(payload, actor, source)}
        else:
            match = re.fullmatch(re.escape(FB_AUTO_ADMIN_PREFIX) + r"/templates/([1-9][0-9]*)(?:/(enable|disable|run-now))?", parsed.path)
            if not match: raise ServiceError("not_found", "接口不存在", 404)
            template_id, action = int(match.group(1)), match.group(2)
            if not action:
                version = payload.pop("expected_version", None); source = self.runtime.resolve_source(payload, actor); result = {"ok": True, "template": self.runtime.store.update_template(template_id, payload, actor, version, source)}
            elif action in {"enable", "disable"}:
                if set(payload) != {"expected_version"}: raise ServiceError("invalid_request", "请求字段无效", 400)
                template = self.runtime.store.get_template(template_id, actor)
                summary = self.runtime.validate_activation(template) if action == "enable" else {}
                fingerprint = summary.pop("_enabled_fingerprint", None)
                result = {"ok": True, "template": self.runtime.store.set_template_status(template_id, action == "enable", actor, payload["expected_version"], expected_enabled_fingerprint=fingerprint), "page_summary": summary}
            else:
                if set(payload) != {"expected_version","operation_id"}: raise ServiceError("invalid_request", "手动执行请求字段无效", 400)
                if not self.runtime.executor.live_enabled: raise ServiceError("fb_auto_live_gate_closed", "FB自动发布总开关关闭，未创建运行或调用GPU/Meta", 409)
                if not self.runtime.prebuild_enabled: raise ServiceError("fb_auto_prebuild_gate_closed", "FB自动发布预制开关关闭，未创建运行或调用GPU", 409)
                result = self.runtime.store.enqueue_manual_due_slot(template_id, actor, expected_template_version=int(payload["expected_version"]), operation_id=str(payload["operation_id"]))
        self.send_json(202 if parsed.path.endswith("run-now") else 200, result)

    def do_GET(self) -> None:
        try: self.dispatch()
        except Exception as exc: self.handle_error(exc)
    def do_POST(self) -> None:
        try: self.dispatch()
        except Exception as exc: self.handle_error(exc)
    def handle_error(self, exc: Exception) -> None:
        known = isinstance(exc, (ServiceError, StoreError, RepositoryError, ValidationError))
        code, status, message = (getattr(exc, "code", "fb_auto_internal_error"), getattr(exc, "status", 500), str(exc)) if known else ("fb_auto_internal_error", 500, "FB自动发布服务内部错误")
        payload: Dict[str, Any] = {"ok": False, "code": code, "error": code, "message": message}
        conflicts = getattr(exc, "conflicts", [])
        if conflicts: payload["conflicts"] = conflicts
        self.send_json(status, payload)


def build_runtime(environ: Mapping[str, str] | None = None) -> Runtime:
    env = os.environ if environ is None else environ
    token = str(env.get("FB_AUTO_POST_INTERNAL_TOKEN", "") or "")
    if not valid_internal_bearer(token): raise ValueError("FB_AUTO_POST_INTERNAL_TOKEN is required")
    def connect():
        import pymysql
        return pymysql.connect(host=env.get("FB_AUTO_MYSQL_HOST", "127.0.0.1"), port=int(env.get("FB_AUTO_MYSQL_PORT", "63350")), user=env.get("FB_AUTO_MYSQL_USER", ""), password=env.get("FB_AUTO_MYSQL_PASSWORD", ""), database=env.get("FB_AUTO_MYSQL_DATABASE", "kunlunads_dev"), charset="utf8mb4", autocommit=True, connect_timeout=10, read_timeout=60, write_timeout=10)
    mysql = ReadOnlyMySQL(connect, str(env.get("FB_AUTO_MYSQL_DATABASE", "kunlunads_dev")), str(env.get("FB_AUTO_BLACKLIST_MYSQL_DATABASE", "ads_setting")))
    operational_path = Path(str(env.get("FB_AUTO_POST_DB_PATH", "/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3"))).resolve()
    metric_path = Path(str(env.get("FB_AUTO_METRIC_DB_PATH", "/mnt/data-disk/fb-auto-post-publisher/fb-auto-metric.sqlite3"))).resolve()
    if operational_path == metric_path or (operational_path.exists() and metric_path.exists() and os.path.samefile(operational_path, metric_path)):
        raise ValueError("FB_AUTO_METRIC_DB_PATH must be independent from FB_AUTO_POST_DB_PATH")
    store = FBAutoPostStore(operational_path)
    metric_store = FBAutoPostStore(metric_path)
    pages, materials = PagePoolRepository(mysql), MaterialRepository(mysql, metric_store)
    graph = RequestsGraphTransport(api_version=str(env.get("FB_GRAPH_API_VERSION", "v22.0")), timeout_seconds=int(env.get("FB_AUTO_GRAPH_TIMEOUT", "120")))
    live = str(env.get("FB_AUTO_POST_LIVE_ENABLED", "0")) == "1"
    prebuild_raw = env.get("FB_AUTO_PREBUILD_ENABLED")
    if prebuild_raw is not None and str(prebuild_raw) not in {"0", "1"}:
        raise ValueError("FB_AUTO_PREBUILD_ENABLED must be 0 or 1")
    prebuild_enabled = live if prebuild_raw is None else str(prebuild_raw) == "1"
    short_link_root = Path(str(env.get("FB_AUTO_POST_SHORT_LINK_ROOT", "/mnt/data-disk/fb-auto-post-public/s2l/fb"))).expanduser()
    if not short_link_root.is_absolute():
        raise ValueError("FB_AUTO_POST_SHORT_LINK_ROOT must be absolute")
    executor = AutoPostExecutor(
        store,
        pages,
        graph,
        live_enabled=live,
        min_request_interval_seconds=float(env.get("FB_AUTO_GRAPH_MIN_INTERVAL_SECONDS", "0.5")),
        short_link_root=str(short_link_root),
    )
    gpu = GPUPrepareClient(str(env.get("FB_AUTO_GPU_PREPARE_INTERNAL_TOKEN", "")), port=int(env.get("FB_AUTO_GPU_PREPARE_PORT", "18836")), timeout=int(env.get("FB_AUTO_GPU_PREPARE_TIMEOUT", "9000")))
    preparer = PrepareExecutor(store, gpu, live_enabled=prebuild_enabled)
    limits = {
        "max_daily_jobs": int(env.get("FB_AUTO_MAX_DAILY_JOBS", "500")),
        "max_publishable_pages": int(env.get("FB_AUTO_MAX_PUBLISHABLE_PAGES", "500")),
        "max_jobs_per_slot": int(env.get("FB_AUTO_MAX_JOBS_PER_SLOT", "20")),
        "max_enabled_templates": int(env.get("FB_AUTO_MAX_ENABLED_TEMPLATES", "10")),
        "prepare_ahead_seconds": int(env.get("FB_AUTO_PREPARE_AHEAD_SECONDS", "14400")),
        "prebuild_days_ahead": int(env.get("FB_AUTO_PREBUILD_DAYS_AHEAD", "1")),
        "max_late_seconds": int(env.get("FB_AUTO_MAX_LATE_SECONDS", "600")),
    }
    if not 1 <= limits["max_daily_jobs"] <= 100000 or not 1 <= limits["max_publishable_pages"] <= 10000 or not 1 <= limits["max_jobs_per_slot"] <= 1000 or not 1 <= limits["max_enabled_templates"] <= 1000 or not 3600 <= limits["prepare_ahead_seconds"] <= 86400 or not 0 <= limits["prebuild_days_ahead"] <= 7 or not 0 <= limits["max_late_seconds"] <= 86400:
        raise ValueError("FB auto capacity configuration invalid")
    return Runtime(store, pages, materials, executor, preparer, token, metric_store=metric_store, prebuild_enabled=prebuild_enabled, **limits)


def main() -> None:
    runtime = build_runtime(); Handler.runtime = runtime
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("FB_AUTO_POST_PORT", "18835"))), Handler)
    server.serve_forever()


if __name__ == "__main__": main()
