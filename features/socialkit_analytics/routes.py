"""Cookie-authenticated SocialKit snapshot report endpoints; no collection here."""
import json
import sqlite3
from urllib.parse import parse_qs

from .query import DEFAULT_ROOT, QueryError, export_csv, options, report


def dispatch(handler, parsed, app):
    actor = handler._socialkit_analytics_actor()
    if actor is None:
        return
    def reply(status, value):
        return app["json_response"](handler, status, value, no_store=True)
    prefix = "/api/socialkit-analytics/"
    if parsed.path not in tuple(prefix + name for name in ("options", "report", "export.csv")):
        return reply(404, {"error": "not_found"})
    if handler.command != "GET":
        return reply(405, {"error": "method_not_allowed", "message": "报表仅支持只读查询。"})
    try:
        params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=1600)
        root = app.get("SOCIALKIT_ANALYTICS_ROOT", DEFAULT_ROOT)
        if parsed.path.endswith("/options"):
            if set(params) - {"version"} or len(params.get("version", [])) > 1:
                raise QueryError("筛选选项参数无效。")
            value = options(root, params.get("version", [None])[0])
            value["scope"] = "all_socialkit_teams"
            return reply(200, value)
        value = report(root, params)
        if parsed.path.endswith("/export.csv"):
            payload = export_csv(value)
            handler.send_response(200)
            for key, item in {"Content-Type": "text/csv; charset=utf-8", "Content-Length": str(len(payload)), "Content-Disposition": 'attachment; filename="socialkit-%s-%s.csv"' % (value["meta"]["start"], value["meta"]["end"]), "Cache-Control": "no-store", "Vary": "Cookie", "X-Content-Type-Options": "nosniff", "X-SocialKit-Version": value["meta"]["version"]}.items():
                handler.send_header(key, item)
            handler.end_headers()
            handler.wfile.write(payload)
            return
        value = dict(value)
        value.pop("all_rows", None)
        return reply(200, value)
    except QueryError as exc:
        return reply(exc.status, {"error": exc.code, "message": str(exc)})
    except (sqlite3.Error, OSError, ValueError, KeyError):
        return reply(503, {"error": "snapshot_unavailable", "message": "报表快照读取失败，请稍后刷新。"})
