#!/usr/bin/env python3
"""Root reader for a fixed metric-window DTO. No arbitrary SQL or write routes."""
import hmac
import json
import os
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_auto_posts.core import StoreError
from features.fb_auto_posts.metric_mysql import GatedMetricReader, config_from_file, window_from_rows
from features.fb_auto_posts.metrics import metric_date

def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


class WindowBroker:
    """Coalesce identical reads; reuse aggregates only after checking READY pointers."""
    def __init__(self, reader, *, budget=55, cache_bytes=64 * 1024 * 1024):
        self.reader, self.budget, self.cache_limit = reader, budget, cache_bytes
        self.lock, self.slots = threading.Lock(), threading.BoundedSemaphore(2)
        self.flights, self.cache = {}, OrderedDict()
        self.cache_size = self.active = self.shared = self.hits = self.loads = 0

    def state(self):
        with self.lock:
            return dict(active_reads=self.active, pending_windows=len(self.flights),
                        cached_windows=len(self.cache), cache_bytes=self.cache_size,
                        shared_requests=self.shared, cache_hits=self.hits, aggregate_loads=self.loads)

    def get(self, product, platform, days):
        days = tuple(sorted(days))
        key, deadline = (product, platform, days), time.monotonic() + self.budget
        with self.lock:
            future = self.flights.get(key)
            leader = future is None
            if leader:
                future = self.flights[key] = Future()
            else:
                self.shared += 1
        if not leader:
            try:
                return future.result(timeout=self.budget)
            except FutureTimeout:
                raise StoreError("fb_auto_metric_query_timeout", "指标只读查询超时", 503) from None
        acquired = False
        try:
            acquired = self.slots.acquire(timeout=max(0, deadline - time.monotonic()))
            if not acquired:
                raise StoreError("fb_auto_metric_query_timeout", "指标查询排队超时，请重试同一操作", 503)
            with self.lock:
                self.active += 1
                cached = any(version[0] == key for version in self.cache)
            body = None
            if cached:
                generations = self.reader.window_generations(product, platform, days, deadline=deadline)
                window = window_from_rows(product, platform, days, generations)
                version = (key, window.generation_ids)
                with self.lock:
                    body = self.cache.get(version)
                    if body is not None:
                        self.cache.move_to_end(version)
                        self.hits += 1
            if body is None:
                # Cold reads retain the original consistent-snapshot query. If a
                # pointer changed since the probe, cache its actual generation IDs.
                rows = self.reader.window(product, platform, days, deadline=deadline)
                window = window_from_rows(product, platform, days, rows)
                body = encode({"ok": True, "rows": rows})
                version = (key, window.generation_ids)
                with self.lock:
                    self.loads += 1
                    if len(body) <= self.cache_limit:
                        previous = self.cache.pop(version, None)
                        if previous is not None:
                            self.cache_size -= len(previous)
                        self.cache[version] = body
                        self.cache_size += len(body)
                        while len(self.cache) > 8 or self.cache_size > self.cache_limit:
                            _, old = self.cache.popitem(last=False)
                            self.cache_size -= len(old)
            future.set_result(body)
            return body
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            if acquired:
                with self.lock:
                    self.active -= 1
                self.slots.release()
            with self.lock:
                self.flights.pop(key, None)


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
        self.send_body(status, encode(value))

    def send_body(self, status, body):
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        self.send_json(200 if self.path == "/health" else 404, {"ok": self.path == "/health", "storage": "ads_ai",
                                                             **self.server.broker.state()})

    def do_POST(self):
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
            self.send_body(200, self.server.broker.get(product, platform, days))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except StoreError as exc:
            self.send_json(exc.status, {"ok": False, "code": exc.code, "message": str(exc),
                                       **({"conflicts": exc.conflicts} if hasattr(exc, "conflicts") else {})})
        except Exception:
            self.send_json(503, {"ok": False, "code": "fb_auto_metric_query_failed", "message": "指标只读服务暂不可用"})


def main():
    token = os.environ["FB_AUTO_POST_INTERNAL_TOKEN"]
    if len(token) < 32: raise ValueError("internal token missing")
    config = config_from_file(os.environ.get("FB_AUTO_METRIC_CONFIG", "/etc/codex-private-storage-archive.json"))
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("FB_AUTO_METRIC_BROKER_PORT", "18449"))), Handler)
    server.token, server.broker = token, WindowBroker(GatedMetricReader(config))
    server.serve_forever()


if __name__ == "__main__": main()
