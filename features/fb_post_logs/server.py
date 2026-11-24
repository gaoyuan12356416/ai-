"""Loopback-only HTTP endpoint. Delegate the browser cookie to the AI backend."""

import argparse
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import sqlite3
from urllib.parse import parse_qs, urlsplit

from .manager import Manager

PREFIX = "/api/admin/fb-posts/"
PUBLIC_HOST = "ai.yingliangads.com"


class Denied(Exception):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


def admin(cookie):
    if not cookie:
        raise Denied(401, "请先登录")
    http = HTTPConnection("127.0.0.1", 8787, timeout=6)
    try:
        http.request("GET", "/api/ui/topbar", headers={"Cookie": cookie, "Host": PUBLIC_HOST})
        response = http.getresponse()
        if response.status != 200:
            raise Denied(response.status if response.status in (401, 403) else 503, "登录状态暂不可用")
        data = json.loads(response.read())
    finally:
        http.close()
    if not data.get("authenticated"):
        raise Denied(401, "请先登录")
    user = data.get("user") or {}
    if user.get("is_admin") is not True or user.get("role") != "admin" or not user.get("user_id"):
        raise Denied(403, "仅管理员可以操作发布日志")
    return {k: str(user.get(k) or "") for k in ("user_id", "tenant_key", "name")}


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, data):
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_request(self):
        parsed = urlsplit(self.path)
        if (self.command, parsed.path) not in {("GET", PREFIX + "logs"), ("POST", PREFIX + "queue-actions")}:
            return self.respond(404, {"error": "not_found"})
        try:
            user = self.server.authenticate(self.headers.get("Cookie", ""))
            if self.command == "GET":
                values = parse_qs(parsed.query, max_num_fields=10, keep_blank_values=True)
                if any(len(v) != 1 for v in values.values()):
                    raise ValueError("查询参数重复")
                result = self.server.manager.query({k: v[0] for k, v in values.items()})
            else:
                if (
                    self.headers.get("Origin") not in (None, "https://" + PUBLIC_HOST)
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"
                ):
                    raise Denied(403, "请求来源无效")
                if self.headers.get_content_type() != "application/json":
                    raise Denied(415, "需要 JSON 请求")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    raise Denied(413, "请求长度无效")
                result = self.server.manager.apply(json.loads(self.rfile.read(size)), user)
            return self.respond(200, result)
        except Denied as exc:
            return self.respond(exc.status, {"message": exc.reason})
        except (ValueError, OverflowError) as exc:
            return self.respond(400, {"message": str(exc)[:200]})
        except (sqlite3.Error, OSError, RuntimeError):
            logging.exception("Facebook log service unavailable")
            return self.respond(503, {"message": "日志服务暂不可用，请重试原请求"})
        except Exception:
            logging.exception("Facebook log service error")
            return self.respond(500, {"message": "读取或处理任务失败，请重试原请求"})

    do_GET = do_POST = handle_request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    manager = Manager()
    if args.install:
        manager.install()
        return
    server = ThreadingHTTPServer(("127.0.0.1", 18974), Handler)
    server.manager, server.authenticate = manager, admin
    server.serve_forever()


if __name__ == "__main__":
    main()
