#!/usr/bin/env python3
"""Loopback-only HK media worker surface; it never exposes CPU/YouTube routes."""

from __future__ import annotations

import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as drama_app  # noqa: E402


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
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        return bool(token) and secrets.compare_digest(token, supplied)

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
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2 * 1024 * 1024:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if self.path == "/api/gpu-video/render":
                result = drama_app.handle_gpu_video_render(payload)
            elif self.path == "/api/gpu-video/cover":
                result = drama_app.handle_gpu_video_cover(payload)
            else:
                self._reply(404, {"code": "not_found"})
                return
            self._reply(200, result)
        except Exception:
            self._reply(500, {"code": "gpu_render_failed", "error": "制作失败"})

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
