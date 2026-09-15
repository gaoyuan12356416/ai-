"""Local-only browser fixture for the Meta drama asset deletion UI.

Start: python tests/fixtures/meta_asset_ui_server.py
Page:  http://127.0.0.1:8766/fb-post-ad-delete.html

This fixture never imports production modules, opens a database, or makes
outbound requests. All mutations affect in-memory fake objects only.

Optional page query scenarios:
  ?scenario=guest             unauthenticated gate
  ?scenario=forbidden         missing module permission
  ?scenario=products_error    product API failure
  ?scenario=uncertain         first execute response is 503 after acceptance;
                             same request_id resolves to duplicate=true
Special preview input IDs:
  EMPTY, FAIL_PREVIEW, REJECT_PREVIEW

GET /__mock/requests returns request evidence for local QA.
POST /__mock/reset resets the fixture to its initial state.
"""
from __future__ import annotations

import argparse
import copy
import json
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"
API = "/api/fb-post-ad-delete"
PHASES = ("creative", "ad", "video")
STATUSES = ("pending", "in_progress", "deleted", "already_deleted", "failed", "blocked", "unknown")
PRODUCTS = [
    {"id": "3443", "name": "MoboReels", "kind": "App", "parent_id": "3443", "parent_name": "MoboReels"},
    {"id": "3543", "name": "MoboReels W2A", "kind": "W2A", "parent_id": "3443", "parent_name": "MoboReels"},
    {"id": "4401", "name": "DramaWave", "kind": "App", "parent_id": "4401", "parent_name": "DramaWave"},
    {"id": "4402", "name": "DramaWave W2A", "kind": "W2A", "parent_id": "4401", "parent_name": "DramaWave"},
]
LOCK = threading.RLock()
JOBS = {}
REQUESTS = []
RUNS = {}
UNCERTAIN_SEEN = set()


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def counts(objects):
    totals = dict.fromkeys(STATUSES, 0)
    totals.update(dict.fromkeys(PHASES, 0))
    totals["total"] = len(objects)
    phases = {kind: {**dict.fromkeys(STATUSES, 0), "total": 0} for kind in PHASES}
    for obj in objects:
        totals[obj["kind"]] += 1
        totals[obj["status"]] += 1
        phases[obj["kind"]][obj["status"]] += 1
        phases[obj["kind"]]["total"] += 1
    return totals, phases


def build_job(job_id="demo-ready", payload=None, status="ready"):
    payload = payload or {"input_type": "content_id", "ids": ["EN-DEMO-001"], "product_ids": ["3443", "3543"]}
    products = [copy.deepcopy(item) for item in PRODUCTS if item["id"] in payload["product_ids"]]
    dramas = []
    for product in products:
        for value in payload["ids"]:
            languages = ("EN", "ES") if payload["input_type"] == "series_code" else ("EN",)
            for language in languages:
                dramas.append({
                    "product_id": product["id"], "product_name": product["name"], "parent_id": product["parent_id"],
                    "content_id": value if payload["input_type"] == "content_id" else language + "-" + value,
                    "series_code": "DEMO-001" if payload["input_type"] == "content_id" else value,
                    "language": language, "name": "重逢之后：测试短剧", "matched_input": value,
                })
    objects = []
    statuses = ("pending", "failed", "blocked", "deleted", "already_deleted", "unknown", "pending", "pending")
    if "EMPTY" not in payload["ids"]:
        for index in range(64):
            kind, object_status = PHASES[index % 3], statuses[index % len(statuses)]
            reason = {
                "failed": "Meta 返回权限不足，未确认删除。",
                "blocked": "存在所选范围外的广告引用，已阻止删除。",
                "unknown": "删除请求响应超时，需要先读取 Meta 核实结果。",
                "deleted": "删除请求已成功。",
                "already_deleted": "Meta 已明确确认该对象处于删除状态。",
            }.get(object_status, "")
            objects.append({
                "key": kind + ":" + str(120000000000000 + index),
                "kind": kind, "object_id": str(120000000000000 + index), "status": object_status,
                "product_ids": payload["product_ids"], "content_ids": sorted({d["content_id"] for d in dramas}),
                "series_codes": sorted({d["series_code"] for d in dramas}), "languages": sorted({d["language"] for d in dramas}),
                "account_ids": ["10000000001" if index % 2 else "10000000002"],
                "reason": reason, "result": {"message": reason} if reason else {}, "updated_at": now(),
            })
    summary, phase_results = counts(objects)
    return {
        "job_id": job_id, "preview_id": "preview-" + job_id, "schema_version": 2, "read_only": False,
        "status": status, "input_type": payload["input_type"], "ids": payload["ids"], "products": products,
        "dramas": dramas, "blockers": [{
            "code": "ambiguous_history", "message": "一条历史记录无法确认剧集归属，已从执行范围排除。",
            "product_id": products[0]["id"] if products else "", "input_id": payload["ids"][0],
        }] if objects else [],
        "summary": summary, "phase_results": phase_results, "objects": objects,
        "created_at": now(), "updated_at": now(), "preview_step": "预览完成",
        "_ticks": 0, "_phases": [],
    }


def reset():
    with LOCK:
        JOBS.clear()
        JOBS["demo-ready"] = build_job()
        JOBS["demo-interrupted"] = build_job("demo-interrupted", status="interrupted")
        JOBS["demo-legacy"] = {
            "job_id": "demo-legacy", "schema_version": 1, "read_only": True, "status": "completed", "phase": "posts",
            "input_type": "series_code", "ids": ["OLD-DEMO"], "series_ids": ["OLD-DEMO"],
            "products": [copy.deepcopy(PRODUCTS[0])], "summary": {"ads": 12, "campaigns": 3, "post_objects": 8},
            "phase_results": {}, "dramas": [], "objects": [], "created_at": "2026-09-01T03:00:00Z",
            "updated_at": "2026-09-01T03:30:00Z", "legacy_logs": [
                {"ts": "2026-09-01T03:00:00Z", "level": "info", "message": "历史预览任务已建立。"},
                {"ts": "2026-09-01T03:30:00Z", "level": "warn", "message": "旧任务保留只读，本地测试记录。"},
            ],
        }
        REQUESTS.clear()
        RUNS.clear()
        UNCERTAIN_SEEN.clear()


def advance(job):
    if job.get("read_only"):
        return
    if job["status"] == "previewing":
        job["_ticks"] += 1
        job["preview_step"] = "核验 Meta 对象及广告账户关系" if job["_ticks"] == 1 else "核验所选范围之外的共享引用"
        if job["_ticks"] >= 2:
            if "FAIL_PREVIEW" in job["ids"]:
                job["status"] = "failed"
                job["error"] = {"code": "mock_preview_failure", "message": "测试：无法连接只读素材来源。"}
            else:
                job["status"] = "ready"
                job["preview_step"] = "预览完成"
            job["updated_at"] = now()
    elif job["status"] == "running":
        index = job["_ticks"]
        phases = job["_phases"]
        if index < len(phases):
            kind = phases[index]
            for obj in job["objects"]:
                if obj["kind"] == kind and obj["status"] in ("pending", "failed", "in_progress"):
                    if obj["object_id"].endswith("000"):
                        obj["status"] = "failed"
                        obj["reason"] = "测试：单对象删除失败，其余对象和后续阶段仍继续。"
                    else:
                        obj["status"] = "deleted"
                        obj["reason"] = "本机模拟删除成功。"
            job["_ticks"] += 1
        if job["_ticks"] >= len(phases):
            has_unresolved = any(obj["status"] in ("failed", "blocked", "unknown", "pending") for obj in job["objects"])
            job["status"] = "partial" if has_unresolved else "completed"
        else:
            next_phase = phases[job["_ticks"]]
            for obj in job["objects"]:
                if obj["kind"] == next_phase and obj["status"] in ("pending", "failed"):
                    obj["status"] = "in_progress"
                    break
        job["summary"], job["phase_results"] = counts(job["objects"])
        job["updated_at"] = now()


def public_job(job, query=None, summary_only=False):
    result = {key: copy.deepcopy(value) for key, value in job.items() if not key.startswith("_")}
    if summary_only:
        result.pop("objects", None)
        result.pop("legacy_logs", None)
        return result
    query = query or {}
    objects = result.get("objects", [])
    kind, status = query.get("kind", [""])[0], query.get("status", [""])[0]
    objects = [item for item in objects if (not kind or item["kind"] == kind) and (not status or item["status"] == status)]
    page = max(1, int(query.get("page", ["1"])[0]))
    page_size = max(1, min(100, int(query.get("page_size", ["50"])[0])))
    result.update(total=len(objects), page=page, page_size=page_size, objects=objects[(page-1)*page_size:page*page_size])
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "MetaAssetLocalFixture/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        return

    def scenario(self):
        return parse_qs(urlparse(self.headers.get("Referer", "")).query).get("scenario", [""])[0]

    def json_response(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def error(self, message, status=400):
        self.json_response({"error": "mock_error", "message": message}, status)

    def record(self, payload=None):
        REQUESTS.append({"method": self.command, "path": self.path, "body": payload, "time": now()})
        del REQUESTS[:-200]

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        with LOCK:
            self.record()
            if path == "/api/ui/topbar":
                if self.scenario() == "guest":
                    return self.json_response({"authenticated": False, "login_url": "/api/auth/feishu/login"})
                user = {"name": "本机测试运营员", "role": "admin", "is_admin": True, "user_id": "local-ui-only", "tenant_key": "local-fixture",
                        "permissions": {"fb_ad_asset_delete": True}}
                if self.scenario() == "forbidden":
                    user.update(role="user", is_admin=False, permissions={"fb_ad_asset_delete": False})
                return self.json_response({"authenticated": True, "user": user})
            if path == "/navigation.json":
                return self.json_response([
                    {"key": "workspace", "label": "工作台", "items": [{"key": "tasks", "label": "任务列表", "kind": "page", "href": "/#tasks", "enabled": True}]},
                    {"key": "facebook_ads", "label": "Meta 广告", "items": [{"key": "fbPostAdDelete", "label": "Meta 剧集广告删除", "kind": "page", "href": "/fb-post-ad-delete.html", "module": "fb_ad_asset_delete", "enabled": True}]},
                ])
            if path == API + "/products":
                if self.scenario() == "products_error":
                    return self.error("测试：产品数据源暂不可用。", 503)
                return self.json_response({"items": PRODUCTS})
            if path == API + "/jobs":
                return self.json_response({"items": [public_job(job, summary_only=True) for job in reversed(list(JOBS.values()))]})
            if path.startswith(API + "/jobs/"):
                job_id = path.rsplit("/", 1)[-1]
                if job_id not in JOBS:
                    return self.error("找不到本机测试任务。", 404)
                job = JOBS[job_id]
                advance(job)
                return self.json_response(public_job(job, query))
            if path == "/__mock/requests":
                return self.json_response({"items": REQUESTS})
        filename = path.lstrip("/") or "fb-post-ad-delete.html"
        candidate = (STATIC / filename).resolve()
        if candidate.parent != STATIC.resolve() or candidate.suffix not in (".html", ".js", ".css") or not candidate.is_file():
            return self.error("本机 fixture 仅提供当前静态文件。", 404)
        raw = candidate.read_bytes()
        mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[candidate.suffix]
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                return self.error("Mock request too large.", 413)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self.error("JSON 格式无效。")
        path = urlparse(self.path).path
        with LOCK:
            self.record(payload)
            if path == "/__mock/reset":
                reset()
                return self.json_response({"reset": True})
            if path == "/api/auth/logout":
                return self.json_response({"ok": True})
            if path == API + "/preview":
                if not payload.get("ids") or len(payload["ids"]) > 50:
                    return self.error("每次需要 1–50 个输入 ID。")
                if payload.get("input_type") not in ("content_id", "series_code"):
                    return self.error("输入类型无效。")
                product_ids = payload.get("product_ids", [])
                if not product_ids or len(product_ids) > 20 or any(value not in {p["id"] for p in PRODUCTS} for value in product_ids):
                    return self.error("所选产品不可用。")
                if "REJECT_PREVIEW" in payload["ids"]:
                    return self.error("测试：所输入的剧 ID 格式不符合要求。")
                job_id = "local-" + uuid.uuid4().hex[:10]
                job = build_job(job_id, payload, "previewing")
                JOBS[job_id] = job
                return self.json_response(public_job(job), 202)
            if path.startswith(API + "/jobs/"):
                suffix = path[len(API + "/jobs/"):]
                job_id, _, action = suffix.partition("/")
                job = JOBS.get(job_id)
                if not job:
                    return self.error("找不到本机测试任务。", 404)
                if job.get("read_only"):
                    return self.error("历史任务仅支持只读。", 409)
                if payload.get("preview_id") != job["preview_id"]:
                    return self.error("预览标识不匹配。", 409)
                if action == "execute":
                    request_id = payload.get("request_id")
                    phases = [kind for kind in PHASES if kind in payload.get("phases", [])]
                    if not request_id or not phases:
                        return self.error("缺少请求编号或执行阶段。")
                    if request_id in RUNS:
                        return self.json_response({**RUNS[request_id], "duplicate": True}, 202)
                    if job["status"] not in ("ready", "completed", "partial", "interrupted"):
                        return self.error("当前任务不可执行。", 409)
                    if not any(obj["kind"] in phases and obj["status"] in ("pending", "failed") for obj in job["objects"]):
                        return self.error("没有可执行对象。", 409)
                    job.update(status="running", _phases=phases, _ticks=0, updated_at=now())
                    run = {"job_id": job_id, "run_id": "run-" + uuid.uuid4().hex[:10], "status": "running", "duplicate": False}
                    RUNS[request_id] = run
                    if self.scenario() == "uncertain" and request_id not in UNCERTAIN_SEEN:
                        UNCERTAIN_SEEN.add(request_id)
                        return self.error("测试：响应中断；后台已接受请求。", 503)
                    return self.json_response(run, 202)
                if action == "reconcile":
                    if job["status"] == "running":
                        return self.error("请等待当前执行结束。", 409)
                    unknown = [obj for obj in job["objects"] if obj["status"] == "unknown"][:1]
                    for obj in unknown:
                        obj.update(status="already_deleted", reason="本机模拟读取核实：已确认删除。")
                    job["summary"], job["phase_results"] = counts(job["objects"])
                    job["updated_at"] = now()
                    return self.json_response({"job_id": job_id, "checked": len(unknown), "read_only": True})
            return self.error("本机 mock 不支持此操作。", 404)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    reset()
    # Intentionally never accept a host argument or bind to a public interface.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Local-only Meta asset UI fixture: http://127.0.0.1:" + str(args.port) + "/fb-post-ad-delete.html", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
