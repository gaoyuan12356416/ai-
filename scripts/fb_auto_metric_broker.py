#!/usr/bin/env python3
"""Root reader for a fixed metric-window DTO. No arbitrary SQL or write routes."""
import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_auto_posts.core import StoreError
from features.fb_auto_posts.metric_mysql import GatedMetricReader, config_from_file, window_from_rows
from features.fb_auto_posts.metrics import metric_date

SLOTS = threading.BoundedSemaphore(2)


def validate_window(value):
    if (not isinstance(value, dict) or set(value) != {"product", "platform", "dates"}
            or value["product"] != "Dramawave" or type(value["platform"]) is not int or value["platform"] != 0
            or not isinstance(value["dates"], list) or not 1 <= len(value["dates"]) <= 30):
        raise StoreError("fb_auto_metric_product_unsupported", "指标窗口请求无效", 400)
    days = tuple(dict.fromkeys(metric_date(day) for day in value["dates"]))
    return value["product"], value["platform"], days


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        self.send_json(200 if self.path == "/health" else 404, {"ok": self.path == "/health", "storage": "ads_ai"})

    def do_POST(self):
        acquired = False
        try:
            if self.path != "/window":
                self.send_json(404, {"ok": False}); return
            expected = "Bearer " + self.server.token
            if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                self.send_json(401, {"ok": False}); return
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise StoreError("fb_auto_metric_request_invalid", "指标请求大小无效", 400)
            product, platform, days = validate_window(json.loads(self.rfile.read(size)))
            acquired = SLOTS.acquire(timeout=2)
            if not acquired:
                raise StoreError("fb_auto_metric_query_failed", "指标查询繁忙", 503)
            rows = self.server.reader.window(product, platform, days)
            window_from_rows(product, platform, days, rows)
            self.send_json(200, {"ok": True, "rows": rows})
        except StoreError as exc:
            self.send_json(exc.status, {"ok": False, "code": exc.code, "message": str(exc),
                                       **({"conflicts": exc.conflicts} if hasattr(exc, "conflicts") else {})})
        except Exception:
            self.send_json(503, {"ok": False, "code": "fb_auto_metric_query_failed", "message": "指标只读服务暂不可用"})
        finally:
            if acquired: SLOTS.release()


def main():
    token = os.environ["FB_AUTO_POST_INTERNAL_TOKEN"]
    if len(token) < 32: raise ValueError("internal token missing")
    config = config_from_file(os.environ.get("FB_AUTO_METRIC_CONFIG", "/etc/codex-private-storage-archive.json"))
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("FB_AUTO_METRIC_BROKER_PORT", "18449"))), Handler)
    server.token, server.reader = token, GatedMetricReader(config)
    server.serve_forever()


if __name__ == "__main__": main()
