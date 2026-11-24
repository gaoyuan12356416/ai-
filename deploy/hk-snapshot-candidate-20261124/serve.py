"""Socket-activated comparison reader for the exact deployed HK reporting version."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time


def main():
    release = Path(__file__).resolve().parent
    settings = json.loads((release / 'runtime.json').read_text())
    active = Path('/data/ai-backend-offload/read-current')
    base = Path(settings['base'])
    if active.resolve() != base or int(os.environ.get('LISTEN_PID', '0')) != os.getpid():
        raise RuntimeError('active report version or socket owner changed')
    if os.environ.get('LISTEN_FDS') != '1':
        raise RuntimeError('one loopback socket is required')
    for name, expected in settings['base_files'].items():
        if hashlib.sha256((base / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('base report source changed')
    sys.path.insert(0, str(base))
    import features.social_analytics as social
    social.__path__.insert(0, str(release / 'social_analytics'))
    from features.gpu_offload import reader

    class Queries(reader.Handler):
        def do_POST(self):
            if self.path != '/v1/query':
                return self.json(404, {'error': 'unsupported_query'})
            if active.resolve() != base:
                return self.json(503, {'error': 'report_version_changed'})
            super().do_POST()

    class Activated(reader.Server):
        daemon_threads = False

        def server_bind(self):
            self.socket.close()
            self.socket = socket.socket(fileno=3)
            self.server_address = self.socket.getsockname()
            if self.server_address != ('127.0.0.1', 19987) or not self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
                raise RuntimeError('unexpected listener')
            self.server_name, self.server_port = self.server_address

        def server_activate(self):
            pass  # systemd already owns the listening socket.

        def process_request(self, request, address):
            self.last_request = time.monotonic()
            super().process_request(request, address)

    token = os.environ['OFFLOAD_INTERNAL_TOKEN']
    if len(token) < 32:
        raise RuntimeError('internal reader authentication required')
    with Activated(('127.0.0.1', 19987), os.environ['OFFLOAD_SNAPSHOT_ROOT'], token) as server:
        server.RequestHandlerClass = Queries
        server.compute = threading.BoundedSemaphore(1)
        server.timeout = 10
        server.last_request = time.monotonic()
        while time.monotonic() - server.last_request < 90:
            server.handle_request()


if __name__ == '__main__':
    main()
