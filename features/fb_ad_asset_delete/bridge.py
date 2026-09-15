"""Thin adapter for the existing AI-backend HTTP server and Cookie ACL."""
import json
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import parse_qs
from .core import AssetError, redact
from .graph import GraphClient
from .service import Service
from .source import SqlSource
from .store import Store, StoreError
from .video_index import MysqlVideoStream, VideoIndex

PREFIX = "/api/fb-post-ad-delete"
_service = None
_lock = threading.Lock()


def get_service(app):
    global _service
    with _lock:
        if _service is None:
            root = Path("/mnt/data-disk").resolve()
            path = Path(os.environ.get("FB_AD_ASSET_DELETE_DB_PATH", str(root / "fb-ad-asset-delete/tasks.sqlite3"))).resolve()
            if root not in path.parents or not os.path.ismount(str(root)):
                raise AssetError("data_disk_unavailable", "任务数据盘不可用，停止新增操作", 503)
            if str(app["MYSQL_HOST"]) != "101.32.56.53" or str(app["MYSQL_PORT"]) != "63350":
                raise AssetError("read_only_source_required", "业务源库必须使用配置的只读端点", 503)
            query = lambda sql, timeout: app["ad_control_run_mysql"](sql, timeout_seconds=timeout, via_stdin=True)
            if query("SELECT @@read_only", 10) != [["1"]]:
                raise AssetError("read_only_source_required", "无法确认业务源库只读状态", 503)
            def check_disk():
                if not os.path.ismount(str(root)):
                    raise AssetError("data_disk_unavailable", "任务数据盘不可用，停止新增操作", 503)
            video_index = VideoIndex(path.parent / "video-reference-index",
                MysqlVideoStream(app["MYSQL_BASE_CMD"], app["MYSQL_PASSWORD"], app["AD_CONTROL_DB_NAME"]),
                validate_storage=check_disk)
            source = SqlSource(query, schema=app["AD_CONTROL_DB_NAME"], video_index=video_index,
                lookup_actor=lambda session: app["lookup_admin_group_for_actor"](app["ad_material_actor"](session)),
                reference_graph_factory=lambda: GraphClient(source.token, version=os.environ.get("FB_AD_ASSET_DELETE_GRAPH_VERSION", "v25.0")))

            def authorize(session):
                fresh = app["load_session"](session.get("session_token", ""))
                if not fresh or not app["has_module_permission"](fresh, "fb_ad_asset_delete"):
                    raise AssetError("permission_revoked", "登录或模块权限已失效，停止新增删除请求", 403)
                return fresh

            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _service = Service(Store(str(path)), source,
                lambda: GraphClient(source.token, version=os.environ.get("FB_AD_ASSET_DELETE_GRAPH_VERSION", "v25.0")),
                authorize=authorize)
        return _service


def _legacy(app, session, service, job_id=None):
    path = Path(app["JOB_DB_PATH"]).resolve()
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fb_post_ad_delete_job'").fetchone():
            return []
        terms, args = [], []
        if session.get("role") != "admin":
            terms.append("actor_user_id=?")
            args.append(app["ad_control_actor"](session))
        if job_id:
            terms.append("job_id=?")
            args.append(job_id)
        where = " WHERE " + " AND ".join(terms) if terms else ""
        rows = conn.execute("SELECT * FROM fb_post_ad_delete_job" + where + " ORDER BY updated_at DESC LIMIT 100", args).fetchall()
    products = {p["id"]: p for p in service.products(session)["items"]}
    items = []
    for row in rows:
        row = dict(row)
        def decoded(field, default):
            try:
                return json.loads(row.get(field + "_json") or "null") or default
            except (TypeError, ValueError):
                return default
        ids = [str(x) for x in decoded("products", [])]
        if session.get("role") != "admin" and any(pid not in products for pid in ids):
            continue
        item = {k: row.get(k, "") for k in ("job_id", "preview_id", "phase", "status", "created_at", "updated_at", "finished_at")}
        item.update(read_only=True, schema_version=1, input_type="series_code", ids=decoded("series_ids", []),
            products=[products.get(pid, dict(id=pid, name="历史产品 " + pid, kind="", parent_id="", parent_name="")) for pid in ids],
            summary=decoded("summary", {}), objects=[], dramas=[], blockers=[], total=0, page=1, page_size=50)
        if job_id:
            item["legacy_items"] = decoded("items", [])
            item["legacy_logs"] = [{"ts": str(log.get("ts", "")), "level": str(log.get("level", "")), "message": redact(log.get("message", ""))} for log in decoded("logs", []) if isinstance(log, dict)]
        items.append(item)
    return items


def dispatch(handler, parsed, app):
    send = lambda status, payload: app["json_response"](handler, status, payload, no_store=True)
    # Do not inherit the legacy FEISHU_AUTH_ENFORCE bypass or API-token session.
    session = app["load_session"](handler._cookies().get(app["SESSION_COOKIE_NAME"], ""))
    if not session:
        return send(401, {"error": "cookie_auth_required", "message": "请先登录 AI 后台"})
    if not app["has_module_permission"](session, "fb_ad_asset_delete"):
        return send(403, {"error": "permission_denied", "message": "未分配 Meta 剧集广告删除权限", "module": "fb_ad_asset_delete"})
    try:
        method, suffix = handler.command, parsed.path[len(PREFIX):]
        if method == "POST" and not handler._require_same_origin_json():
            return
        if suffix in ("/delete-posts", "/delete-ads"):
            return send(410, {"error": "legacy_execution_closed", "message": "旧删除接口已关闭，请使用新页面重新预览"})
        service = get_service(app)
        if method == "GET" and suffix == "/products":
            return send(200, service.products(session))
        if method == "GET" and suffix == "/jobs":
            data = service.list_jobs(session)
            data["items"].extend(_legacy(app, session, service))
            data["items"].sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
            return send(200, data)
        match = re.fullmatch(r"/jobs/([A-Za-z0-9_-]{1,80})(?:/(execute|reconcile|recheck))?", suffix)
        if method == "GET" and match and not match[2]:
            params = parse_qs(parsed.query)
            try:
                data = service.detail(session, match[1], **{k: params[k][0] for k in ("page", "page_size", "kind", "status") if k in params})
            except StoreError as exc:
                if exc.code != "not_found":
                    raise
                rows = _legacy(app, session, service, match[1])
                if not rows:
                    raise AssetError("not_found", "任务不存在", 404)
                data = rows[0]
            return send(200, data)
        if method == "POST" and (suffix == "/preview" or match and match[2]):
            length = handler.headers.get("Content-Length", "")
            if handler.headers.get("Transfer-Encoding") or not re.fullmatch(r"[0-9]{1,6}", length) or int(length) > 65536:
                handler.close_connection = True
                raise AssetError("invalid_body_length", "请求体超过允许大小或长度无效", 413)
            payload = handler._read_json()
            if not isinstance(payload, dict):
                raise AssetError("invalid_body", "请求体必须为 JSON 对象")
            if suffix == "/preview":
                return send(202, service.preview(session, payload))
            if match[2] == "execute":
                return send(202, service.execute(session, match[1], payload))
            if match[2] == "recheck":
                return send(202, service.recheck(session, match[1], payload))
            return send(200, service.reconcile(session, match[1], payload))
        return send(404, {"error": "not_found", "message": "接口不存在"})
    except AssetError as exc:
        return send(exc.status, {"error": exc.code, "message": exc.message})
    except StoreError as exc:
        code = exc.code
        message = {"not_found": "任务不存在；历史任务不能继续删除", "preview_mismatch": "预览已变化，请重新预览", "job_not_ready": "任务当前不能启动，请等待查询或执行完成", "request_conflict": "此请求标识已用于其他执行参数", "ledger_unavailable": "台账无法写入，已停止新增删除请求"}.get(code, "任务台账拒绝此次操作，请刷新后核实任务状态")
        return send(404 if code == "not_found" else 503 if code == "ledger_unavailable" else 409, {"error": code, "message": message})
    except (TypeError, ValueError):
        return send(400, {"error": "invalid_input", "message": "请求参数无效"})
    except Exception:
        return send(503, {"error": "service_unavailable", "message": "服务暂不可用，请核实任务状态后重试"})
