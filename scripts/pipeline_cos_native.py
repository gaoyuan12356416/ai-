"""Serve COS preparations using the owning channel's exact runtime and database."""
import argparse
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def serve(cfg):
    # A versioned copy of the active owner's package supplies the channel's
    # business logic. Its main() and background runner entrypoints never start.
    root = Path(cfg["runtime"])
    sys.path.insert(0, str(root))
    os.chdir(root)
    import features.pipeline_dispatch as dispatch
    dispatch.__path__.insert(0, cfg["overlay"])
    from features.pipeline_dispatch.adapters import AdapterError
    channel = cfg["channel"]
    if channel == "fb":
        from features.fb_auto_posts.service import build_runtime
        from features.pipeline_dispatch.fb_native import FBNativeBridge
        bridge = FBNativeBridge(build_runtime())
    elif channel == "x":
        from features.x_accounts import oauth_service
        if cfg.get("tagged"):
            from features.x_tag_publish.runtime import install
            install(oauth_service)
        from features.pipeline_dispatch.x_native import XNativeBridge
        bridge = XNativeBridge(oauth_service)
    else:
        from features.pipeline_dispatch.tt_native import TTNativeBridge
        if cfg["automatic"]:
            from features.tt_auto_posts.service import build_service_from_env
        else:
            from features.tt_posts.service import build_service_from_env
        bridge = TTNativeBridge(build_service_from_env(), automatic=cfg["automatic"])
    secret = os.environ[cfg["token_env"]]
    if len(secret) < 32:
        raise RuntimeError("canonical bridge token unavailable")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def reply(self, code, value):
            body=json.dumps(value,ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (ConnectionResetError,BrokenPipeError):
                pass
        def do_GET(self):
            self.reply(200 if self.path == "/health" else 404, {"ready":self.path == "/health","channel":channel})
        def do_POST(self):
            if not hmac.compare_digest("Bearer "+secret,self.headers.get("Authorization","")):
                self.reply(403,{"code":"unauthorized"})
                return
            try:
                prefix = cfg["prefix"] + "/"
                action = self.path[len(prefix):]
                if not self.path.startswith(prefix) or action not in {"reserve","checkpoint","complete","fail","reconcile","renew"}:
                    raise AdapterError("cos_prepare_only")
                size = int(self.headers.get("Content-Length",0))
                if not 0 < size <= 8 * 1024**2:
                    raise ValueError()
                payload=json.loads(self.rfile.read(size))
                job=payload["job"]
                if (job.get("node_id"),job.get("stage"),job.get("channel")) != ("cos-cloud","prepare",channel):
                    raise AdapterError("cos_prepare_only")
                if action == "renew" and channel == "fb":
                    result=bridge.renew_cloud(job,payload["claim"])
                elif action == "renew":
                    result={"accepted":True}
                else:
                    result=bridge.dispatch(action,payload)
                self.reply(200,result)
            except Exception as exc:
                self.reply(409,{"code":getattr(exc,"code","cos_native_request_failed")})
    server=ThreadingHTTPServer(("127.0.0.1",cfg["port"]),Handler)
    server.daemon_threads=False
    server.serve_forever()


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--config",required=True)
    args=parser.parse_args()
    serve(json.loads(Path(args.config).read_text()))
