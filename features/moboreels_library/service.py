import argparse
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import re
import secrets
import subprocess
import threading
from urllib.parse import unquote

from .catalog import Catalog
from .db import Database, DatabaseError
from .promotions import PromotionError, PromotionManager, identity
from .provider import ProviderClient
from .settings import Settings
from .sync import SyncManager


def public_run(run):
    if run is None:
        return None
    # This shared progress response must never expand with the private run ledger.
    # In particular resume_actor carries the same user identifiers as actor.
    fields = ("id", "run_id", "status", "stage", "created_at", "started_at",
              "finished_at", "updated_at", "total_pages", "fetched_pages", "verified", "error")
    row = {key: run[key] for key in fields if key in run}
    aliases = {"total_rows": "total", "processed_rows": "processed", "inserted": "new_count",
               "updated": "updated_count", "listed": "listed_count", "delisted": "delisted_count"}
    for output, source in aliases.items():
        row[output] = run.get(source, 0)
    row["unchanged"] = max(0, row["processed_rows"] - row["inserted"] - row["updated"])
    return row


class Application:
    def __init__(self, settings, db=None, provider=None, sync=None, promotions=None):
        self.settings = settings
        self.db = db or Database(settings)
        self.provider = provider or ProviderClient(settings)
        self.catalog = Catalog(settings, self.db)
        self.promotions = promotions or PromotionManager(settings, self.db, self.provider)
        self.promotions.recover()
        self.sync = sync or SyncManager(settings, self.db, self.provider)

    def dispatch(self, request):
        method, path = request.get("method"), request.get("path", "")
        actor = request.get("actor") or {}
        identity(self.settings.institution_user_id, actor)
        query, body = request.get("query") or {}, request.get("body") or {}
        if method == "GET" and path == "/dramas":
            data = self.catalog.list(query)
            statuses = self.promotions.statuses(actor, [r["story_id"] for r in data["list"]])
            for row in data["list"]:
                row["promotion_status"] = statuses.get(str(row["story_id"]))
            return 200, data
        if path == "/sync-runs" and method == "POST":
            previous = self.sync.latest()
            trigger = "scheduled" if body.get("trigger") == "scheduled" and actor.get("user_id") == "moboreels-scheduler" else "manual"
            run = self.sync.start(trigger, actor)
            return 202, {"run": public_run(run), "reused": bool(previous and previous.get("id") == run.get("id"))}
        if method == "GET" and path == "/sync-runs/latest":
            return 200, {"run": public_run(self.sync.latest()), "last_success_at": self.sync.last_success_at()}
        if method == "GET" and re.fullmatch(r"/sync-runs/[0-9a-f]{32}", path):
            run = self.sync.get(path.rsplit("/", 1)[1])
            return (200, {"run": public_run(run)}) if run else (404, {"error": "not_found", "message": "未找到同步任务"})
        match = re.fullmatch(r"/dramas/([^/]{1,192})(/promotion)?", path)
        if match:
            story = unquote(match.group(1))
            if not story or len(story) > 64 or any(ord(c) < 32 for c in story):
                raise ValueError("剧目ID无效")
            if match.group(2):
                if method == "GET":
                    return 200, {"promotion": self.promotions.get(actor, story)}
                if method == "POST":
                    result = self.promotions.create(actor, story)
                    return (202 if result["promotion"]["status"] == "creating" else 200), result
            elif method == "GET":
                drama = self.catalog.detail(story)
                if drama is None:
                    return 404, {"error": "not_found", "message": "未找到该短剧"}
                try:
                    promotion = self.promotions.get(actor, story)
                    return 200, {"drama": drama, "promotion": promotion}
                except PromotionError:
                    return 200, {"drama": drama, "promotion": None, "promotion_error": "推广记录暂不可用，可稍后单独查看"}
        return 404, {"error": "not_found", "message": "接口不存在"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # No URLs, request bodies, headers or credentials in access logs.
        pass

    def respond(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self.respond(200, {"ok": True, "service": "moboreels-library"})
        else:
            self.respond(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/internal/dispatch":
            return self.respond(404, {"error": "not_found"})
        expected = "Bearer " + self.server.app.settings.internal_token
        if not self.server.app.settings.internal_token or not secrets.compare_digest(self.headers.get("Authorization", ""), expected):
            return self.respond(403, {"error": "internal_auth_failed"})
        try:
            self.connection.settimeout(15)
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                raise ValueError("请求格式无效")
            length = int(lengths[0])
            if not 0 < length <= 32768:
                raise ValueError("请求过大")
            envelope = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(envelope, dict):
                raise ValueError("请求格式无效")
            status, payload = self.server.app.dispatch(envelope)
        except PromotionError as exc:
            status, payload = exc.status, {"error": exc.code, "message": str(exc)}
        except (ValueError, TypeError, UnicodeError):
            status, payload = 400, {"error": "invalid_request", "message": "请求参数无效"}
        except DatabaseError:
            status, payload = 503, {"error": "database_unavailable", "message": "数据库暂不可用，请稍后重试"}
        except Exception as exc:
            logging.error("Moboreels dispatch failed: %s", type(exc).__name__)
            status, payload = 503, {"error": "service_unavailable", "message": "服务暂不可用，请稍后重试"}
        self.respond(status, payload)


def main():
    argparse.ArgumentParser(description="Moboreels library service").parse_args()
    os.umask(0o077)
    settings = Settings.from_env()
    if not all((settings.internal_token, settings.institution_user_id, settings.token, settings.db_user, settings.db_password)):
        raise SystemExit("Moboreels service credentials are incomplete")
    mounted = subprocess.check_output(["findmnt", "-n", "-o", "UUID", "/mnt/data-disk"], text=True).strip()
    if mounted != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8":
        raise SystemExit("Required data disk is unavailable")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    process_lock = (settings.data_dir / "service.lock").open("a")
    fcntl.flock(process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    app = Application(settings)
    server = ThreadingHTTPServer((settings.bind_host, settings.bind_port), Handler)
    server.daemon_threads = True
    server.app = app
    stop = threading.Event()
    def mirror():
        while not stop.is_set():
            try:
                app.promotions.flush_outbox()
            except Exception as exc:
                logging.warning("promotion outbox retained: %s", type(exc).__name__)
            stop.wait(15)
    threading.Thread(target=mirror, daemon=True, name="promotion-outbox").start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
