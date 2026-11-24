"""Check the ingress and its failure paths using public static requests only."""
import argparse
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from nginx_config import render


HOST = 'ai.yingliangads.com'
REF = 'https://' + HOST + '/social-analytics.html'
UA = 'Offload-static-validation/1'


def call(port, url, extra=None, method='GET', tls=False):
    headers = {'Host': HOST, 'Referer': REF, 'User-Agent': UA, 'X-AI-Static-Probe': 'verify',
               'Accept-Encoding': 'identity'}
    headers.update(extra or {})
    if tls:
        # Preserve hostname verification and SNI while connecting to the local ingress.
        conn = http.client.HTTPSConnection(HOST, port, timeout=5)
        conn._create_connection = lambda addr, timeout, *args: socket.create_connection(('127.0.0.1', port), timeout)
    else:
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    start = time.monotonic()
    try:
        conn.request(method, url, headers=headers)
        response = conn.getresponse()
        head = {key.lower(): value for key, value in response.getheaders()}
        raw = response.read()
        size = len(raw)
        if head.get('content-encoding') == 'gzip' and raw:
            raw = gzip.decompress(raw)
        return response.status, head, raw, size, round((time.monotonic() - start) * 1000, 2)
    finally:
        conn.close()


def public_checks(port, manifest, tls=False):
    evidence = []
    for url, spec in manifest['urls'].items():
        record = manifest['files'][spec['file']]
        for encoding in ('identity', 'gzip'):
            pair = {}
            for group in ('native', 'verify'):
                status, head, body, size, ms = call(port, url, {'Accept-Encoding': encoding,
                    'X-AI-Static-Probe': group}, tls=tls)
                assert status == 200 and hashlib.sha256(body).hexdigest() == record['sha256'], (url, status, group)
                assert head.get('cache-control', '') == spec['cache_control'], (url, group, 'cache policy')
                assert not head.get('set-cookie') and head['content-type'] == record['content_type'], (url, group, 'mime')
                assert head.get('x-ai-static-source') == ('hk-release' if group == 'verify' else None), (url, group, head.get('x-ai-static-source'))
                if group == 'verify' and encoding == 'gzip':
                    assert head.get('content-encoding') == 'gzip' and 'accept-encoding' in head.get('vary', '').lower()
                    assert head.get('etag', '').removeprefix('W/') == record['etag']
                if encoding == 'identity':
                    assert head.get('etag') == record['etag'], (url, group, head.get('etag'))
                pair[group] = {'bytes': size, 'ms': ms, 'modified': head.get('last-modified')}
            assert pair['verify']['modified'] == pair['native']['modified'], (url, 'modified')
            if encoding == 'gzip':
                evidence.append({'url': url, **pair})
    url = next(u for u in manifest['urls'] if 'social-analytics.js?' in u)
    name = manifest['urls'][url]['file']
    assert call(port, url, {'If-None-Match': manifest['files'][name]['etag']}, tls=tls)[0] == 304
    assert call(port, url, {'Range': 'bytes=0-7'}, tls=tls)[0] == 206
    for extras in ({'Referer': 'https://' + HOST + '/another.html'}, {'Authorization': 'Test excluded'}):
        assert 'x-ai-static-source' not in call(port, url, extras, tls=tls)[1]
    assert 'x-ai-static-source' not in call(port, url + '&other=1', tls=tls)[1]
    status, head, body, _, _ = call(port, '/social-analytics.html', method='HEAD', tls=tls)
    assert status == 200 and head.get('x-ai-static-source') == 'hk-release' and not body
    for path in ('/__ai_hk_files/' + name, '/__ai_hk_check/' + name):
        assert call(port, path, tls=tls)[0] == 404
    return evidence


def loopback_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def alive(proc, port):
    for _ in range(60):
        if proc.poll() is not None:
            raise RuntimeError('isolated test listener exited before serving')
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.1):
                return
        except OSError:
            time.sleep(.05)
    raise RuntimeError('test listener unavailable')


def isolated(root, manifest):
    with tempfile.TemporaryDirectory(prefix='hk-static-test-') as temp:
        lab = Path(temp)
        lab.chmod(0o755)
        live = lab / 'native'
        live.mkdir()
        for name in manifest['files']:
            shutil.copy2(root / 'public' / name, live / name)
        check = loopback_port()
        guard = subprocess.Popen([sys.executable, '-Bu', str(root / 'static_assets.py'), 'guard', '--release', str(root),
                                 '--port', str(check), '--live-root', str(live), '--suspend-file', str(lab / 'suspend')],
                                 stdout=subprocess.DEVNULL)
        web = None
        try:
            alive(guard, check)
            port = loopback_port()
            http, server = render(manifest, 0, check, live, root / 'public')
            config = lab / 'nginx.conf'
            config.write_text('daemon off; master_process off; pid ' + str(lab / 'nginx.pid') +
                '; error_log stderr crit; events {worker_connections 64;} http {access_log off; '
                'include /etc/nginx/mime.types; ' + http + ' server {listen 127.0.0.1:' + str(port) +
                '; server_name ' + HOST + '; root ' + str(live) + '; ' + server +
                ' location / {try_files $uri =404; add_header Cache-Control $hk_asset_cache;} }}')
            web = subprocess.Popen(['nginx', '-p', str(lab), '-c', str(config)], stdout=subprocess.DEVNULL)
            alive(web, port)
            public_checks(port, manifest)
            url = '/social-analytics.html'
            live_file = live / url[1:]
            stamp = live_file.stat()
            body = live_file.read_bytes()
            changed = b'X' + body[1:]
            live_file.write_bytes(changed)
            os.utime(live_file, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            status, headers, data, _, _ = call(port, url)
            assert (status, headers.get('x-ai-static-source'), data) == (200, 'native-fallback', changed)
            live_file.unlink()
            assert call(port, url)[0] == 404
            shutil.copy2(root / 'public' / url[1:], live_file)
            (lab / 'suspend').touch()
            assert call(port, url)[1].get('x-ai-static-source') == 'native-fallback'
            (lab / 'suspend').unlink()
            guard.terminate()
            guard.wait(timeout=3)
            status, headers, data, _, _ = call(port, url)
            assert (status, headers.get('x-ai-static-source'), data) == (200, 'native-fallback', body)
        finally:
            for proc in (web, guard):
                if proc and proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
    return {'isolated': 'passed', 'cases': ['canonical-byte-drift', 'deleted-file', 'emergency-suspend',
                                             'guard-outage', 'per-URL-HTTP-parity', 'routing-exclusions']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('isolated', 'live'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / 'manifest.json').read_text())
    print(json.dumps(isolated(root, manifest) if args.mode == 'isolated' else
                     {'verified': public_checks(443, manifest, tls=True)}))
