"""Independent capacity settings gateway; never owns jobs or publisher state."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .cloud_admin import session_actor
from .core import DispatchError
from .monitor import MonitorError, capacity_payload, coordinator_request


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, result):
        body = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/pipeline-monitor/drain-diagnostics":
            try:
                session_actor(self.headers.get("Cookie", ""))
                from .drain_diagnostics import read_snapshot
                return self.reply(200, read_snapshot())
            except (DispatchError, MonitorError) as exc:
                return self.reply(exc.status, {"error": exc.code})
            except (OSError, ValueError):
                return self.reply(503, {"error": "diagnostics_unavailable"})
        if self.path == "/health":
            self.reply(200, {"ok": True, "service": "pipeline-capacity-gateway"})
        else:
            self.reply(404, {"error": "not_found"})

    def do_PUT(self):
        if self.path != "/api/pipeline-monitor/cos-capacity":
            return self.reply(404, {"error": "not_found"})
        try:
            origin = self.headers.get("Origin")
            if (self.headers.get("Sec-Fetch-Site") == "cross-site"
                    or (origin and (urlsplit(origin).scheme not in {"http", "https"}
                                    or urlsplit(origin).netloc.lower() != self.headers.get("Host", "").lower()))):
                raise DispatchError("same_origin_required", status=403)
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                raise DispatchError("json_required", status=415)
            actor = session_actor(self.headers.get("Cookie", ""))
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise DispatchError("request_size_invalid", status=413)
            raw = json.loads(self.rfile.read(length))
            allowed = {"expected_version", "host_id", "stage", "max_workers", "min_pending", "scaling_mode"}
            if (not isinstance(raw, dict) or set(raw) - allowed
                    or (raw.get("host_id"), raw.get("stage")) != ("cos-cloud", "prepare")):
                raise DispatchError("cos_pool_required", status=400)
            result = coordinator_request("PUT", "/internal/pipeline-monitor/capacity", capacity_payload(raw, actor))
            self.reply(200, result)
        except (DispatchError, MonitorError) as exc:
            self.reply(exc.status, {"error": exc.code, "message": str(exc)})
        except (ValueError, TypeError):
            self.reply(400, {"error": "invalid_request"})
        except OSError:
            self.reply(503, {"error": "session_service_unavailable"})


def main():
    port = int(os.environ.get("PIPELINE_CAPACITY_GATEWAY_PORT", "18960"))
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
