#!/usr/bin/env python3
import json
import logging
import os
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from common import Config
from store import ApiError, Store

TASKS = "/api/image-generation/tasks"
PRINCIPAL = "screenshot-api"


def handler_class(config, store):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ImageAPI/1.0"

        def log_message(self, fmt, *args):
            # No request headers, input URLs, prompts, or query strings in access logs.
            pass

        def reply(self, code, data):
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def authenticate(self):
            auth = self.headers.get("Authorization", "").strip()
            token = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else self.headers.get("X-API-Token", "").strip()
            tokens = config.tokens()
            if not tokens:
                raise ApiError("auth_not_configured", "接口鉴权暂不可用", 503)
            if not token or not any(secrets.compare_digest(token.encode(), t.encode()) for t in tokens):
                raise ApiError("unauthorized", "Token 缺失或无效", 401)

        def execute(self, method):
            try:
                path = urlsplit(self.path).path
                if method == "GET" and path == "/health":
                    with store.connect() as db:
                        db.execute("SELECT 1").fetchone()
                    return self.reply(200, {"status": "ok", "service": "codex-image-api"})
                self.authenticate()
                if method == "POST" and path == TASKS:
                    if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                        raise ApiError("invalid_request", "必须提供唯一的 Content-Length")
                    if self.headers.get("Content-Type", "").split(";", 1)[0].lower().strip() != "application/json":
                        raise ApiError("invalid_request", "Content-Type 必须为 application/json")
                    try:
                        size = int(self.headers["Content-Length"])
                    except (ValueError, TypeError):
                        raise ApiError("invalid_request", "Content-Length 无效")
                    if size < 1:
                        raise ApiError("invalid_request", "请求体不能为空")
                    if size > config.body_limit:
                        raise ApiError("request_too_large", "请求体不能超过 1 MiB", 413)
                    self.connection.settimeout(30)
                    raw = self.rfile.read(size)
                    if len(raw) != size:
                        raise ApiError("invalid_request", "请求体不完整")
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeError):
                        raise ApiError("invalid_request", "请求体不是有效的 UTF-8 JSON")
                    task_id, created = store.submit(PRINCIPAL, payload)
                    state = store.query(PRINCIPAL, task_id)
                    return self.reply(202 if created else 200, {k: state[k] for k in ("task_id", "status", "total")})
                if method == "GET" and re.fullmatch(TASKS + r"/ig_[0-9a-f]{32}", path):
                    return self.reply(200, store.query(PRINCIPAL, path.rsplit("/", 1)[1]))
                raise ApiError("not_found", "接口或任务不存在", 404)
            except ApiError as exc:
                self.reply(exc.http_status, {"error": {"code": exc.code, "message": exc.message}})
            except Exception:
                logging.exception("image API request failed")
                self.reply(503, {"error": {"code": "service_unavailable", "message": "服务暂不可用，请使用相同 request_id 重试"}})

        def do_POST(self):
            self.execute("POST")

        def do_GET(self):
            self.execute("GET")

    return Handler


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO)
    config = Config()
    config.root.mkdir(parents=True, exist_ok=True)
    store = Store(config.root / "state.sqlite3")
    store.initialize()
    server = ThreadingHTTPServer(("127.0.0.1", config.port), handler_class(config, store))
    logging.info("Image API listening on loopback port %d", config.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
