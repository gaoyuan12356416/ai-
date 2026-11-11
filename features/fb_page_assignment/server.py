"""Loopback HTTP owner for the exact external integration route."""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import threading
import uuid

from .runtime import encoded, environment, log

ROUTE = "/api/integrations/v1/drama-page-assignments"
MAX_BODY = 65536


def handler(service, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PageAssignment/1"

        def log_message(self, fmt, *args):
            # Do not print raw paths, bodies or headers (including tokens).
            pass

        def reply(self, status, body):
            data = encoded(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path == "/healthz":
                self.reply(200, {"ok": True, "service": "fb-page-assignment"})
            else:
                self.reply(405 if self.path == ROUTE else 404, {"task_id": uuid.uuid4().hex, "results": []})

        def do_POST(self):
            task_id = uuid.uuid4().hex
            if self.path != ROUTE:
                return self.reply(404, {"task_id": task_id, "results": []})
            supplied = self.headers.get("Authorization", "")
            expected = "Bearer " + token
            if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
                log("unauthorized", task_id=task_id)
                return self.reply(401, {"task_id": task_id, "results": []})
            payload = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid_body_length")
                self.connection.settimeout(15)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("incomplete_body")
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError, OSError):
                status, body = service.request_error(task_id, "invalid_json_or_body", payload)
                return self.reply(status, body)
            try:
                status, body = service.handle(task_id, payload)
            except Exception as exc:
                log("request_internal_error", task_id=task_id, error_type=type(exc).__name__)
                # Last-resort exceptions still pass through the durable error path.
                from .service import failure_row, validate, InvalidRequest
                external = payload.get("external_task_id", "") if isinstance(payload, dict) else ""
                if not isinstance(external, str) or len(external) > 128:
                    external = ""
                try:
                    external, ids = validate(payload)
                except InvalidRequest:
                    ids = []
                rows = [failure_row("internal_error", "接口处理异常", cid) for cid in ids]
                status, body = service.finish(task_id, external,
                    rows or [failure_row("internal_error", "接口处理异常")], 503,
                    [dict(drama_id=cid, page_id=None) for cid in ids], ["internal_error"])
            return self.reply(status, body)

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True)
    parser.add_argument("--port", type=int, default=8943)
    parser.add_argument("--data-root", default="/mnt/data-disk/fb-page-assignment")
    args = parser.parse_args()
    env = environment(args.env)
    token = env.get("FB_PAGE_ASSIGNMENT_API_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("independent_api_token_not_configured")
    from .sources import Sources
    from .storage import Store, Writer
    from .notify import Notifier
    from .service import Service
    root = Path(args.data_root)
    root.mkdir(parents=True, exist_ok=True)
    service = Service(Sources(env), Store(root / "outbox.sqlite3"), Writer(env), Notifier(env))
    service.start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(service, token))
    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log("service_started", port=args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
