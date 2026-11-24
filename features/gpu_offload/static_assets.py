"""Build immutable public assets on HK and validate canary responses at the ingress."""
import argparse
import gzip
import hashlib
import json
import os
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.parse import urlsplit


def read_manifest(root):
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    for name, record in manifest['files'].items():
        if not re.fullmatch(r'[a-z0-9-]+\.(?:js|css|html)', name):
            raise ValueError('invalid public filename')
        if not (0 < record['size'] <= 256 * 1024 and re.fullmatch(r'[0-9a-f]{64}', record['sha256'])):
            raise ValueError('invalid asset identity')
    return manifest


def verified_bytes(file, record, *, check_mtime=False):
    with file.open('rb') as handle:
        meta = os.fstat(handle.fileno())
        if meta.st_size != record['size'] or (check_mtime and int(meta.st_mtime) != record['mtime']):
            raise ValueError('asset generation changed')
        data = handle.read(record['size'] + 1)
    if hashlib.sha256(data).hexdigest() != record['sha256']:
        raise ValueError('asset digest changed')
    return data


def prepare_release(root, build=False):
    """Only run against an unpublished release before switching its pointer."""
    manifest = read_manifest(root)
    plain_bytes = packed_bytes = 0
    for name, record in manifest['files'].items():
        plain = root / 'public' / name
        data = verified_bytes(plain, record)
        packed = plain.with_name(name + '.gz')
        if build:
            with packed.open('xb') as output:
                with gzip.GzipFile(filename='', mode='wb', fileobj=output, compresslevel=9, mtime=0) as stream:
                    stream.write(data)
            for path in (plain, packed):
                os.utime(str(path), (record['mtime'], record['mtime']))
        if gzip.decompress(packed.read_bytes()) != data:
            raise ValueError('incorrect compressed asset')
        for path in (plain, packed):
            if int(path.stat().st_mtime) != record['mtime']:
                raise ValueError('incorrect asset timestamp')
        plain_bytes += len(data)
        packed_bytes += packed.stat().st_size
    return {'files': len(manifest['files']), 'bytes': plain_bytes, 'gzip_bytes': packed_bytes}


class AssetHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_HEAD(self):
        self.do_GET()

    def finish_response(self, status, headers=(), body=b''):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command == 'GET' and body:
            self.wfile.write(body)

    def do_GET(self):
        server = self.server
        url = urlsplit(self.path)
        if not url.path.startswith('/') or url.scheme or url.netloc:
            return self.finish_response(404)
        name = url.path[1:]
        entry = server.manifest['files'].get(name)
        if not entry:
            return self.finish_response(404)
        if server.live_root is not None:
            event = {'event': 'static_decision', 'file': name,
                     'operator': self.headers.get('X-Static-Operator') == '1'}
            try:
                if server.suspend_file and server.suspend_file.exists():
                    return self.finish_response(409)
                verified_bytes(server.live_root / name, entry, check_mtime=True)
            except (ValueError, OSError):
                print(json.dumps(dict(event, use='native')), flush=True)
                return self.finish_response(409)
            print(json.dumps(dict(event, use='hk')), flush=True)
            return self.finish_response(200, [('X-Accel-Redirect', '/__ai_hk_files/' + name)])
        # This listener is loopback-only and has no application credentials.
        # The ingress receives a verified copy at release time, never per visit.
        data = server.payloads[name]
        headers = [('Content-Type', entry['content_type']), ('ETag', entry['etag']),
                   ('Last-Modified', formatdate(entry['mtime'], usegmt=True)), ('Cache-Control', 'no-cache')]
        self.finish_response(200, headers, data)


def serve(root, mode, port, live_root, suspend_file):
    manifest = read_manifest(root)
    server = ThreadingHTTPServer(('127.0.0.1', port), AssetHandler)
    server.manifest = manifest
    server.live_root = live_root if mode == 'guard' else None
    server.suspend_file = suspend_file
    server.payloads = {} if mode == 'guard' else {
        name: verified_bytes(root / 'public' / name, row) for name, row in manifest['files'].items()}
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('build', 'verify', 'origin', 'guard'))
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--port', type=int)
    parser.add_argument('--live-root', type=Path)
    parser.add_argument('--suspend-file', type=Path)
    args = parser.parse_args()
    if args.action in ('build', 'verify'):
        print(json.dumps(prepare_release(args.release, build=args.action == 'build')))
    else:
        if not args.port or (args.action == 'guard' and not args.live_root):
            parser.error('listener address configuration is incomplete')
        serve(args.release, args.action, args.port, args.live_root, args.suspend_file)


if __name__ == '__main__':
    main()
