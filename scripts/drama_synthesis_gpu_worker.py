#!/usr/bin/env python3
"""Loopback-only HK media worker surface; it never exposes CPU/YouTube routes."""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import sys
import threading
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as drama_app  # noqa: E402
from features.drama_synthesis.core import DramaSynthesisError  # noqa: E402
from features.drama_synthesis.async_runtime import AsyncRuntime, runtime_error, safe_error  # noqa: E402


def render_concurrency(environ=None):
    env = os.environ if environ is None else environ
    try:
        value = int(env.get("DRAMA_GPU_MAX_CONCURRENCY", "1"))
    except (TypeError, ValueError):
        raise ValueError("DRAMA_GPU_MAX_CONCURRENCY must be 1") from None
    if value != 1:
        raise ValueError("DRAMA_GPU_MAX_CONCURRENCY must be 1")
    return value


RENDER_SLOTS = threading.BoundedSemaphore(render_concurrency())
RUNTIME = None
RUNTIME_LOCK = threading.Lock()


def get_runtime():
    global RUNTIME
    with RUNTIME_LOCK:
        if RUNTIME is None:
            root = getattr(drama_app, "WORK_ROOT", None)
            cache = getattr(drama_app, "cached_gpu_video_result", None)
            if not root or not callable(cache):
                raise runtime_error("gpu_runtime_unavailable")
            try:
                limit = int(os.environ.get("DRAMA_GPU_QUEUE_LIMIT", "8"))
            except ValueError:
                raise runtime_error("gpu_runtime_unavailable") from None
            RUNTIME = AsyncRuntime(
                root, drama_app.handle_gpu_video_render, cache,
                can_resume=getattr(drama_app, "gpu_video_resume_ready", None),
                render_slots=RENDER_SLOTS, queue_limit=limit,
            )
        return RUNTIME


def valid_job_id(payload):
    return isinstance(payload, dict) and bool(
        isinstance(payload.get("job_id"), str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", payload["job_id"])
    )


def valid_content_id(payload):
    value = payload.get("content_id")
    if value is None:
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return False
    if any(char in "/\\" or unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        return False
    # The legacy renderer embeds this value in a single filename component.
    # Leave room for episode-range and variant suffixes; Unicode remains valid.
    return len(value.encode("utf-8")) <= 200


class Handler(BaseHTTPRequestHandler):
    server_version = "drama-synthesis-gpu/2"

    def _reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # A disconnected submitter does not cancel or fail durable work.
            return

    def _authorized(self):
        token = os.environ.get("GPU_VIDEO_WORKER_TOKEN", "")
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        supplied = header[len("Bearer "):]
        return bool(token) and secrets.compare_digest(token.encode("utf-8"), supplied.encode("utf-8"))

    def do_GET(self):
        if self.path == "/healthz":
            self._reply(200, {"ok": True, "role": "media-only"})
            return
        if self.path.startswith("/api/gpu-video/jobs/"):
            if not self._authorized():
                self._reply(401, {"code": "unauthorized"})
                return
            try:
                self._reply(200, get_runtime().get(self.path[len("/api/gpu-video/jobs/"):]))
            except DramaSynthesisError as exc:
                error = safe_error(exc)
                self._reply(exc.status, {"code": error["code"], "error": error["message"]})
            except Exception:
                self._reply(503, {"code": "gpu_runtime_unavailable", "error": "制作节点正在恢复，请稍后查询"})
            return
        if self.path == "/api/gpu-video/random-overlay/catalog" and self._authorized():
            try:
                self._reply(200, {"item": drama_app.drama_random_template_catalog()})
            except Exception:
                self._reply(503, {"code": "gpu_asset_catalog_unavailable"})
            return
        self._reply(404, {"code": "not_found"})

    def do_POST(self):
        if not self._authorized():
            self._reply(401, {"code": "unauthorized"})
            return
        resume_match = re.fullmatch(r"/api/gpu-video/jobs/([A-Za-z0-9][A-Za-z0-9_-]{0,127})/resume", self.path)
        if self.path not in {"/api/gpu-video/render", "/api/gpu-video/cover", "/api/gpu-video/jobs"} and not resume_match:
            self._reply(404, {"code": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2 * 1024 * 1024:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            self._reply(400, {"code": "invalid_request"})
            return
        if not valid_job_id(payload):
            self._reply(400, {"code": "invalid_job_id"})
            return
        if self.path != "/api/gpu-video/cover" and not valid_content_id(payload):
            self._reply(400, {"code": "invalid_content_id"})
            return
        try:
            if self.path == "/api/gpu-video/jobs":
                self._reply(202, get_runtime().submit(payload))
                return
            if resume_match:
                if resume_match[1] != payload["job_id"]:
                    self._reply(400, {"code": "invalid_job_id"})
                    return
                self._reply(202, get_runtime().resume(payload, payload.get("expected_generation")))
                return
            if self.path == "/api/gpu-video/render":
                result = get_runtime().run_sync(payload)
            else:
                # A render may wait for its cover callback. Never take a render
                # slot here, or max_concurrency=1 would deadlock that job.
                result = drama_app.handle_gpu_video_cover(payload)
            self._reply(200, result)
        except DramaSynthesisError as exc:
            error = safe_error(exc)
            self._reply(exc.status, {"code": error["code"], "error": error["message"]})
        except Exception:
            self._reply(500, {"code": "gpu_render_failed", "error": "制作失败"})

    def log_message(self, _format, *_args):
        return


def main():
    host = os.environ.get("DRAMA_GPU_HOST", "127.0.0.1")
    port = int(os.environ.get("DRAMA_GPU_PORT", "8787"))
    if host != "127.0.0.1" or not os.environ.get("GPU_VIDEO_WORKER_TOKEN"):
        raise SystemExit("loopback host and worker token are required")
    runtime = get_runtime()  # reconcile before any authoritative GET 404
    server = ThreadingHTTPServer((host, port), Handler)

    def shutdown(_signum, _frame):
        runtime.stop_intake()
        # shutdown must not run in the serve_forever thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        runtime.stop_intake()
        server.server_close()
        runtime.close(timeout=30)


if __name__ == "__main__":
    main()
