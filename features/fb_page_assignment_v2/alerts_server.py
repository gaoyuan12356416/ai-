"""Independent alert ingress and Feishu delivery on the CPU host."""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .alerts import AlertHub, FeishuSender, ROUTE
from .ledger import encoded

LOG = logging.getLogger(__name__)


def handler(hub, bearer, wake):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, doc):
            raw = encoded(doc).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path == "/healthz":
                return self.reply(200, {"ok": True, "delivery": hub.status()})
            self.reply(404, {})

        def do_POST(self):
            if self.path != ROUTE:
                return self.reply(404, {})
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), bearer):
                return self.reply(401, {})
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 65536 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid_length")
                self.connection.settimeout(4)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("incomplete_request")
                ids = hub.accept(json.loads(raw))
            except (ValueError, UnicodeError, RecursionError, OSError) as exc:
                if isinstance(exc, OSError) and not isinstance(exc, TimeoutError):
                    LOG.error("v2_alert_ingest_storage_error class=%s", type(exc).__name__)
                    return self.reply(503, {})
                return self.reply(400, {})
            except Exception as exc:
                LOG.error("v2_alert_ingest_failure class=%s", type(exc).__name__)
                return self.reply(503, {})
            wake.set()
            self.reply(200, {"accepted": ids})
    return Handler


def deliver(hub, sender):
    hub.summaries()
    msg = hub.claim()
    if not msg:
        return False
    try:
        receipt = sender.send(msg)
    except Exception as exc:
        uncertain = getattr(exc, "uncertain", True)
        state = "unknown" if uncertain else "pending"
        error = str(getattr(exc, "code", type(exc).__name__))
        hub.finish(msg, state, error=error)
        LOG.warning("v2_feishu_delivery id=%s state=%s reason=%s", msg["id"], state, error)
    else:
        hub.finish(msg, "sent", receipt=receipt)
        LOG.info("v2_feishu_delivered id=%s message_id=%s", msg["id"], receipt)
    return True


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--root", default="/mnt/data-disk/fb-page-assignment-v2/alerts")
    cli.add_argument("--port", type=int, default=19844)
    args = cli.parse_args()
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("FB_PAGE_ASSIGNMENT_API_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("alert_relay_bearer_missing")
    hub, sender = AlertHub(args.root), FeishuSender()
    wake, stopped = threading.Event(), threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(hub, ("Bearer " + token).encode(), wake))
    server.daemon_threads = False
    hub.interrupted()

    def pump():
        while not stopped.is_set():
            try:
                if deliver(hub, sender):
                    continue
            except Exception as exc:
                LOG.error("v2_feishu_worker_failure class=%s", type(exc).__name__)
            wake.wait(0.75)
            wake.clear()

    thread = threading.Thread(target=pump, name="v2-feishu")
    thread.start()
    def stop(*unused):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        stopped.set()
        wake.set()
        thread.join()


if __name__ == "__main__":
    main()
