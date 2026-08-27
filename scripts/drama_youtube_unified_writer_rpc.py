#!/usr/bin/env python3
"""Loopback-only controlled writer for the dedicated ads_ai YouTube tables."""

from __future__ import annotations

import hmac
import json
import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Mapping

import pymysql


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.drama_synthesis.core import DramaSynthesisError  # noqa: E402
from features.drama_synthesis.unified_youtube import read_secure_owned_file  # noqa: E402
from features.drama_synthesis.unified_youtube_rpc import (  # noqa: E402
    LedgerRPCError,
    SCHEMA,
    UnifiedYouTubeLedger,
    WRITER_USER,
    load_database_credential_file,
)


MAX_BODY_BYTES = 32 * 1024
RPC_PATH = "/v1/youtube-sync"
HEALTH_PATH = "/health"


def _secure_token(path_text: str) -> str:
    try:
        token = read_secure_owned_file(path_text, max_bytes=4096).decode("utf-8").strip()
    except (RuntimeError, UnicodeDecodeError):
        token = ""
    if not 32 <= len(token) <= 4096 or any(char.isspace() for char in token):
        raise RuntimeError("writer token is invalid")
    return token


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(os.environ.get(name, str(default))), high))
    except (TypeError, ValueError):
        return default


def build_ledger() -> UnifiedYouTubeLedger:
    config = load_database_credential_file(
        os.environ.get("DRAMA_YOUTUBE_WRITER_DB_CREDENTIAL_FILE", ""),
        expected_user=WRITER_USER,
    )
    host = str(config["host"])
    port = int(config["port"])
    user = str(config["user"])
    password = str(config["password"])
    database = str(config["database"])
    if host != "101.32.56.53" or port != 63353 or database != SCHEMA:
        raise RuntimeError("writer database target is invalid")
    if user != WRITER_USER or not password:
        raise RuntimeError("writer database credential is invalid")

    def connect():
        return pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            connect_timeout=5,
            read_timeout=15,
            write_timeout=15,
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
        )

    return UnifiedYouTubeLedger(connect)


class ControlledWriterHandler(BaseHTTPRequestHandler):
    server_version = "DramaYouTubeWriter/2"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def _send(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        authorization = str(self.headers.get("Authorization") or "")
        expected = "Bearer " + str(self.server.rpc_token)
        return hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        if self.path != HEALTH_PATH:
            self._send(404, {"ok": False, "code": "not_found"})
            return
        if not self._authorized():
            self._send(401, {"ok": False, "code": "unauthorized"})
            return
        try:
            result = self.server.ledger.health()
        except LedgerRPCError as exc:
            self._send(exc.status, {"ok": False, "code": exc.code})
            return
        except Exception:
            self._send(503, {"ok": False, "code": "youtube_sync_unavailable"})
            return
        self._send(200, result)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != RPC_PATH:
            self._send(404, {"ok": False, "code": "not_found"})
            return
        if not self._authorized():
            self._send(401, {"ok": False, "code": "unauthorized"})
            return
        if str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() != "application/json":
            self._send(415, {"ok": False, "code": "youtube_sync_contract_invalid"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if not 1 <= length <= MAX_BODY_BYTES:
            self._send(413, {"ok": False, "code": "youtube_sync_contract_invalid"})
            return
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            request = None
        if not isinstance(request, Mapping) or set(request) != {"action", "table", "external_id", "payload"}:
            self._send(400, {"ok": False, "code": "youtube_sync_contract_invalid"})
            return
        try:
            result = self.server.ledger.execute(
                request.get("action"), request.get("table"), request.get("external_id"), request.get("payload"),
            )
        except (DramaSynthesisError, LedgerRPCError) as exc:
            self._send(int(getattr(exc, "status", 409)), {"ok": False, "code": str(getattr(exc, "code", "youtube_sync_contract_invalid"))})
            return
        except Exception:
            self._send(503, {"ok": False, "code": "youtube_sync_unavailable"})
            return
        self._send(200, result)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = _secure_token(os.environ.get("DRAMA_YOUTUBE_UNIFIED_RPC_TOKEN_FILE", ""))
    ledger = build_ledger()
    ledger.health()
    host = str(os.environ.get("DRAMA_YOUTUBE_UNIFIED_RPC_HOST", "127.0.0.1") or "").strip()
    port = _env_int("DRAMA_YOUTUBE_UNIFIED_RPC_PORT", 18837, 1024, 65535)
    if host != "127.0.0.1" or port != 18837:
        raise RuntimeError("writer RPC bind target is invalid")
    server = HTTPServer((host, port), ControlledWriterHandler)
    server.rpc_token = token
    server.ledger = ledger
    def stop(*_args: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logging.info("controlled YouTube writer ready on loopback")
    server.serve_forever(poll_interval=0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
