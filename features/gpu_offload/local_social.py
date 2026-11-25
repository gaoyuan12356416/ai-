"""96-only social reporting. The source of facts is one published immutable file."""
import json
import os
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path

from .cache import Response
from .gateway import Gateway
from .reports import Capture, validate_target
from features.social_analytics import routes, warehouse_reader


class LocalReports:
    paths = frozenset(('/api/social-analytics/report', '/api/social-analytics/export.csv'))

    def __init__(self, root, deployment):
        self.root = Path(root)
        self.revision = deployment['compute_revision']
        self.primary_files = {Path(p): tuple(v) for p, v in deployment['primary_file_stats'].items()}
        self.slots = threading.BoundedSemaphore(2)

    def compatible(self):
        # Concurrent releases on the main API must take precedence over a pinned copy.
        try:
            for path, expected in self.primary_files.items():
                s = path.stat()
                if (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns) != expected:
                    return False
        except OSError:
            return False
        return True

    def confirmed_snapshot(self, state):
        if not isinstance(state, dict) or state.get('state') != 'complete' or not state.get('version'):
            return None
        snapshot = warehouse_reader.current(self.root)
        return snapshot if snapshot[0]['version'] == state['version'] else None

    def render(self, actor, target, snapshot):
        parsed = validate_target(target)
        if parsed.path not in self.paths:
            raise ValueError('unsupported_local_report')
        if not self.slots.acquire(blocking=False):
            return None
        try:
            capture = Capture(actor)

            def reply(handler, status, payload, **unused):
                handler.status = status
                handler.headers['Content-Type'] = 'application/json; charset=utf-8'
                handler.wfile.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))

            routes.dispatch(capture, parsed, {
                'SOCIAL_ANALYTICS_WAREHOUSE_ROOT': str(self.root),
                'SOCIAL_ANALYTICS_FROZEN_SNAPSHOT': snapshot, 'json_response': reply})
            manifest = snapshot[0]
            age = datetime.now(timezone.utc) - datetime.fromisoformat(manifest['generated_at'])
            capture.headers.update({'X-AI-Backend': 'cpu-origin-optimized', 'X-AI-Compute-Revision': self.revision,
                                    'X-AI-Data-Version': manifest['version'], 'X-AI-Data-Time': manifest['generated_at'],
                                    'X-AI-Stale': str(age.total_seconds() > 7200).lower()})
            return Response(capture.status, capture.headers, capture.wfile.getvalue())
        finally:
            self.slots.release()


def main():
    deployment = json.loads(Path(os.environ['SOCIAL_ORIGIN_RELEASE']).read_text())
    local = LocalReports(deployment['warehouse'], deployment)
    server = Gateway(('127.0.0.1', int(os.environ['SOCIAL_ORIGIN_PORT'])),
                     os.environ['OFFLOAD_GATEWAY_CONFIG'], os.environ['OFFLOAD_INTERNAL_TOKEN'], local)
    print(json.dumps({'kind': 'cpu-origin-start', 'revision': local.revision, 'compatible': local.compatible()}), flush=True)
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
