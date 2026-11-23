"""Loopback-only report worker and atomic immutable snapshot receiver."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import threading
import time
import uuid
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .cache import SingleFlightCache, Response, annotated, cache_key
from .reports import render, validate_actor, validate_target
from features.social_analytics.warehouse_reader import current

MAX_SNAPSHOT = 512 * 1024 * 1024


def accept_snapshot(root, stream, length, expected_hash):
    if not 4 < length <= MAX_SNAPSHOT or not re.fullmatch('[a-f0-9]{64}', expected_hash):
        raise ValueError('invalid_snapshot')
    header = stream.read(4)
    if len(header) != 4: raise ValueError('truncated_snapshot')
    manifest_size = struct.unpack('!I', header)[0]
    if not 0 < manifest_size <= 65536 or manifest_size + 4 >= length:
        raise ValueError('invalid_manifest')
    raw = stream.read(manifest_size)
    if len(raw) != manifest_size: raise ValueError('truncated_manifest')
    manifest = json.loads(raw)
    version = manifest.get('version', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', version): raise ValueError('invalid_version')
    expected_path = 'snapshots/' + version + '.sqlite3'
    if manifest.get('database') != expected_path: raise ValueError('invalid_snapshot_path')
    snapshots = Path(root) / 'snapshots'
    snapshots.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = snapshots / ('.incoming-' + uuid.uuid4().hex)
    remaining = length - 4 - manifest_size
    digest = hashlib.sha256()
    try:
        with temporary.open('xb') as output:
            while remaining:
                data = stream.read(min(1024 * 1024, remaining))
                if not data: raise ValueError('truncated_snapshot')
                output.write(data); digest.update(data); remaining -= len(data)
            output.flush(); os.fsync(output.fileno())
        if digest.hexdigest() != expected_hash: raise ValueError('snapshot_checksum_mismatch')
        deadline = time.monotonic() + 30
        with closing(sqlite3.connect(temporary.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            if db.execute('PRAGMA quick_check').fetchone() != ('ok',): raise ValueError('snapshot_invalid')
            for table in ('catalog', 'effects', 'titles'):
                db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone()
        target = Path(root) / expected_path
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected_hash:
                raise ValueError('immutable_version_conflict')
            temporary.unlink()
        else:
            temporary.chmod(0o400)
            temporary.replace(target)
        pointer = Path(root) / ('.current-' + uuid.uuid4().hex)
        pointer.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        pointer.replace(Path(root) / 'current.json')
        # Each snapshot already contains all retained history; three versions
        # cover serving/rollback without accumulating an hourly disk backlog.
        versions = sorted(snapshots.glob('*.sqlite3'), key=lambda item: item.stat().st_mtime, reverse=True)
        keep = set(versions[:3]) | {target}
        for previous in versions:
            if previous not in keep: previous.unlink()
        return {'ok': True, 'version': version, 'bytes': length - manifest_size - 4}
    finally:
        if temporary.exists(): temporary.unlink()


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, root, token):
        super().__init__(address, Handler)
        self.root, self.token = Path(root), token
        self.cache, self.compute, self.upload = SingleFlightCache(), threading.BoundedSemaphore(2), threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_): pass

    def authorized(self):
        return hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.token)

    def reply(self, value):
        self.send_response(value.status)
        for name, data in value.headers.items():
            if name.lower() not in ('content-length', 'connection', 'transfer-encoding'): self.send_header(name, data)
        self.send_header('Content-Length', str(len(value.body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers(); self.wfile.write(value.body); self.close_connection = True

    def json(self, status, payload):
        self.reply(Response(status, {'Content-Type': 'application/json; charset=utf-8'}, json.dumps(payload).encode()))

    def do_GET(self):
        if not self.authorized(): return self.json(401, {'error': 'unauthorized'})
        if self.path != '/v1/status': return self.json(404, {'error': 'not_found'})
        try:
            manifest, path = current(self.server.root)
            return self.json(200, {'ok': path.is_file(), 'version': manifest['version'], 'data_time': manifest['generated_at'], 'sql_enabled': False})
        except Exception: return self.json(503, {'error': 'snapshot_unavailable', 'sql_enabled': False})

    def do_POST(self):
        if not self.authorized(): return self.json(401, {'error': 'unauthorized'})
        self.connection.settimeout(180)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if self.path == '/v1/snapshot':
                if not self.server.upload.acquire(False): return self.json(409, {'error': 'snapshot_sync_running'})
                try:
                    result = accept_snapshot(self.server.root, self.rfile, length, self.headers.get('X-Snapshot-SHA256', ''))
                finally: self.server.upload.release()
                return self.json(200, result)
            if self.path != '/v1/query': return self.json(404, {'error': 'unsupported_query'})
            if not 0 < length <= 65536: raise ValueError('invalid_request_size')
            request = json.loads(self.rfile.read(length))
            actor, target = request['actor'], request['target']
            validate_actor(actor); validate_target(target)
            snapshot = current(self.server.root)
            manifest, _ = snapshot
            key = cache_key(actor, target, manifest['version'])
            def compute():
                if not self.server.compute.acquire(timeout=2): raise RuntimeError('report_compute_busy')
                try: return render(self.server.root, actor, target, snapshot=snapshot)
                finally: self.server.compute.release()
            value, stale, hit = self.server.cache.get(key, compute)
            return self.reply(annotated(value, stale=stale, hit=hit))
        except (KeyError, ValueError): return self.json(400, {'error': 'invalid_request'})
        except (BrokenPipeError, ConnectionResetError): self.close_connection = True
        except Exception: return self.json(503, {'error': 'snapshot_unavailable', 'message': '快照暂不可用，未知数据不按零统计。'})


def main():
    token = os.environ['OFFLOAD_INTERNAL_TOKEN']
    if len(token) < 32: raise ValueError('internal_token_required')
    Server(('127.0.0.1', int(os.environ.get('OFFLOAD_READER_PORT', '19960'))),
           os.environ['OFFLOAD_SNAPSHOT_ROOT'], token).serve_forever()


if __name__ == '__main__': main()
