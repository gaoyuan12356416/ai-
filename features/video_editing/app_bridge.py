"""Narrow main-API bridge; import the business service only on its own routes."""
from pathlib import Path
from urllib.parse import urlsplit

STATIC = {
    "/video-editing.html": "text/html; charset=utf-8",
    "/video-editing.js": "application/javascript; charset=utf-8",
    "/video-editing.css": "text/css; charset=utf-8",
}


def handle(handler, method, app):
    path = urlsplit(handler.path).path
    if path in STATIC:
        if method != "GET":
            app.json_response(handler, 405, {"error": "method_not_allowed"}, no_store=True)
            return True
        # The shell provides the shared Feishu login. All data and writes are
        # protected by the Cookie-only video_editing module gate in routes.py.
        target = Path(app.__file__).resolve().parent / "static" / path.lstrip("/")
        try:
            data = target.read_bytes()
        except OSError:
            app.json_response(handler, 404, {"error": "not_found"}, no_store=True)
            return True
        handler.send_response(200)
        handler.send_header("Content-Type", STATIC[path])
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        handler.wfile.write(data)
        return True
    from .routes import handle as dispatch
    return dispatch(handler, method, app)
