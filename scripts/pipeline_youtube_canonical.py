#!/usr/bin/env python3
"""Loopback-only YouTube canonical RPC; no business scheduler or worker loop."""
import argparse
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


PREFIX = '/internal/pipeline-dispatch/youtube/'
ACTIONS = frozenset({'reserve', 'begin-operation', 'checkpoint', 'complete', 'fail', 'reconcile', 'pause'})
MAX_REQUEST = 8 * 1024 * 1024
PACKAGE = '_pipeline_youtube_canonical_dispatch'


def within(path, root):
    try:
        Path(path).resolve().relative_to(root)
        return True
    except (ValueError, TypeError):
        return False


def load_dispatch(dispatch_root):
    """Relative imports use this release; absolute features imports stay live."""
    package_root = Path(dispatch_root).resolve() / 'features' / 'pipeline_dispatch'
    existing = sys.modules.get(PACKAGE)
    if existing is not None:
        if Path(existing.__file__).resolve() != package_root / '__init__.py':
            raise RuntimeError('dispatch_package_already_loaded')
    else:
        spec = importlib.util.spec_from_file_location(PACKAGE, package_root / '__init__.py', submodule_search_locations=[str(package_root)])
        if spec is None or spec.loader is None:
            raise RuntimeError('dispatch_package_unavailable')
        module = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(PACKAGE, None)
            raise
    return importlib.import_module(PACKAGE + '.youtube_native')


def load_bridge(runtime_root, dispatch_root):
    runtime_root, dispatch_root = Path(runtime_root).resolve(), Path(dispatch_root).resolve()
    if not (runtime_root / 'app.py').is_file():
        raise RuntimeError('runtime_app_unavailable')
    existing = sys.modules.get('app')
    if existing is not None and Path(existing.__file__).resolve() != runtime_root / 'app.py':
        raise RuntimeError('runtime_app_already_loaded')
    # Never let this release's complete business modules replace the deployed
    # app's versions. Only pipeline_dispatch is loaded from dispatch_root.
    sys.path[:] = [str(runtime_root)] + [p for p in sys.path if Path(p or os.getcwd()).resolve() not in {runtime_root, dispatch_root}]
    features = importlib.import_module('features')
    if any(not within(path, runtime_root) for path in features.__path__):
        raise RuntimeError('runtime_features_already_loaded')
    app = importlib.import_module('app')
    runtime = importlib.import_module('features.youtube_auto_publish.runtime')
    if not within(runtime.__file__, runtime_root):
        raise RuntimeError('runtime_youtube_module_mismatch')
    workflow = runtime.build_service(app)
    # Constructors reuse/add tables in the original native ledger. They do not
    # start a lane, call app.main, recover screenshots, render or notify.
    return load_dispatch(dispatch_root).YouTubeNativeBridge(workflow)


def error_code(exc):
    value = getattr(exc, 'code', '')
    if isinstance(value, str) and re.fullmatch(r'(?:youtube|canonical|dispatch|reference|storage)_[a-z0-9_]{1,100}', value):
        return value
    return 'youtube_dispatch_failed'


def handler_type(bridge, token):
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError('canonical_token_required')

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def setup(self):
            super().setup()
            self.connection.settimeout(120)

        def log_message(self, *_args):
            pass  # No task bodies, request URLs, credentials or exception text.

        def respond(self, status, value):
            body = json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
            try:
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Exact durable receipts can be delivered again.

        def do_GET(self):
            if self.path != '/health':
                return self.respond(404, {'code': 'not_found'})
            return self.respond(200, {'ok': True, 'service': 'pipeline-youtube-canonical'})

        def do_POST(self):
            action = self.path[len(PREFIX):] if self.path.startswith(PREFIX) else ''
            if action not in ACTIONS:
                return self.respond(404, {'code': 'not_found'})
            supplied = self.headers.get('Authorization', '')
            if not secrets.compare_digest(supplied.encode('utf-8'), ('Bearer ' + token).encode('utf-8')):
                return self.respond(403, {'code': 'pipeline_youtube_bridge_disabled_or_unauthorized'})
            lengths = self.headers.get_all('Content-Length', [])
            try:
                size = int(lengths[0]) if len(lengths) == 1 else 0
            except ValueError:
                size = 0
            if self.headers.get('Transfer-Encoding') or not 0 < size <= MAX_REQUEST:
                return self.respond(413, {'code': 'request_size_invalid'})
            try:
                raw = self.rfile.read(size)
                payload = json.loads(raw) if len(raw) == size else None
                if not isinstance(payload, dict):
                    raise ValueError()
            except (ValueError, UnicodeError, TimeoutError):
                return self.respond(400, {'code': 'invalid_request'})
            try:
                result = bridge.dispatch(action, payload)
            except Exception as exc:
                return self.respond(409, {'code': error_code(exc)})
            return self.respond(200, result)

    return Handler


class CanonicalServer(ThreadingHTTPServer):
    daemon_threads = False  # Normal shutdown waits for already accepted RPCs.
    block_on_close = True

    def handle_error(self, _request, _client_address):
        # Socket/protocol failures must not print exception bodies or traces.
        print('youtube_dispatch_connection_failed', file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', required=True)
    parser.add_argument('--dispatch-root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--port', type=int, default=18847)
    args = parser.parse_args(argv)
    token = os.environ.get('PIPELINE_YOUTUBE_CANONICAL_TOKEN', '')
    if os.environ.get('PIPELINE_YOUTUBE_CANONICAL_BRIDGE_ENABLED') != '1' or len(token) < 32:
        raise RuntimeError('canonical_bridge_disabled_or_unconfigured')
    if not 1024 <= args.port <= 65535:
        raise ValueError('canonical_port_invalid')
    bridge = load_bridge(args.runtime_root, args.dispatch_root)
    def stop(_signal, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with CanonicalServer(('127.0.0.1', args.port), handler_type(bridge, token)) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(error_code(exc), file=sys.stderr)
        raise SystemExit(1) from None
