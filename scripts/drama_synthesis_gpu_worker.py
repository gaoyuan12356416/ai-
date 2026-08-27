#!/usr/bin/env python3
"""Loopback-only HK media worker surface; it never exposes CPU/YouTube routes."""

from __future__ import annotations

import json
import os
import re
import secrets
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


def render_concurrency(environ=None):
    env = os.environ if environ is None else environ
    try:
        value = int(env.get("DRAMA_GPU_MAX_CONCURRENCY", "1"))
    except (TypeError, ValueError):
        raise ValueError("DRAMA_GPU_MAX_CONCURRENCY must be an integer in 1..8") from None
    if not 1 <= value <= 8:
        raise ValueError("DRAMA_GPU_MAX_CONCURRENCY must be an integer in 1..8")
    return value


RENDER_SLOTS = threading.BoundedSemaphore(render_concurrency())


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
    server_version = "drama-synthesis-gpu/1"

    def _reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
        if self.path not in {"/api/gpu-video/render", "/api/gpu-video/cover"}:
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
        if self.path == "/api/gpu-video/render" and not valid_content_id(payload):
            self._reply(400, {"code": "invalid_content_id"})
            return
        acquired = False
        try:
            if self.path == "/api/gpu-video/render":
                acquired = RENDER_SLOTS.acquire(blocking=False)
                if not acquired:
                    self._reply(503, {"code": "gpu_render_busy", "error": "制作节点忙，请稍后重试"})
                    return
                result = drama_app.handle_gpu_video_render(payload)
            else:
                # A render may wait for its cover callback. Never take a render
                # slot here, or max_concurrency=1 would deadlock that job.
                result = drama_app.handle_gpu_video_cover(payload)
            self._reply(200, result)
        except DramaSynthesisError as exc:
            self._reply(exc.status, {"code": exc.code, "error": str(exc)})
        except Exception:
            self._reply(500, {"code": "gpu_render_failed", "error": "制作失败"})
        finally:
            if acquired:
                RENDER_SLOTS.release()

    def log_message(self, _format, *_args):
        return


def main():
    host = os.environ.get("DRAMA_GPU_HOST", "127.0.0.1")
    port = int(os.environ.get("DRAMA_GPU_PORT", "8787"))
    if host != "127.0.0.1" or not os.environ.get("GPU_VIDEO_WORKER_TOKEN"):
        raise SystemExit("loopback host and worker token are required")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
