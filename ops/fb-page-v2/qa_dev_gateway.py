"""Exercise the real Nginx snippets against an isolated loopback stub, never HK."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

TOKEN = 'isolated-page-v2-gateway-fixture-token-0123456789'
ROUTE = '/api/integrations/v2/drama-page-assignments'
calls = []
lock = threading.Lock()


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_GET(self): self.answer(b'', {'ok': True, 'version': 2})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        if raw == b'hold': time.sleep(1)
        if raw == b'disconnect':
            with lock: calls.append({'raw': raw})
            self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
        self.answer(raw, {'sha256': hashlib.sha256(raw).hexdigest(), 'path': self.path}, 503 if raw == b'503' else 200)

    def answer(self, raw, body, status=200):
        with lock: calls.append({'raw': raw, 'authorization': self.headers.get('Authorization')})
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Request-ID', 'stub-request-id')
        if status == 503: self.send_header('Retry-After', '7')
        self.end_headers(); self.wfile.write(data)


def main():
    folder = Path(sys.argv[1]).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parent
    sockets = []
    for port in (19846, 19847, 19848):
        s = socket.socket(); s.bind(('127.0.0.1', port)); sockets.append(s)
    for s in sockets: s.close()
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=localhost',
                    '-addext', 'subjectAltName=DNS:localhost', '-keyout', str(folder/'key.pem'), '-out', str(folder/'cert.pem')],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    proxy = (source/'dev-gateway.proxy.conf').read_text().replace('/etc/nginx/snippets/fb-page-v2-dev-auth.conf', str(folder/'auth.conf'))
    (folder/'proxy.conf').write_text(proxy)
    (folder/'auth.conf').write_text('if ($http_authorization != "Bearer '+TOKEN+'") { return 401 \'{"error":"unauthorized"}\'; }\n')
    locations = (source/'dev-gateway.locations.conf').read_text().replace('/etc/nginx/snippets/fb-page-v2-dev-proxy.conf', str(folder/'proxy.conf')).replace('127.0.0.1:19845', '127.0.0.1:19847')
    (folder/'locations.conf').write_text(locations)
    header = (source/'dev-gateway.http.conf').read_text()
    config = f'''worker_processes 1;
pid {folder}/nginx.pid;
error_log {folder}/nginx-error.log;
events {{ worker_connections 128; }}
http {{
  access_log off;
  {header}
  server {{
    listen 127.0.0.1:19846 ssl;
    listen 127.0.0.1:19848;
    server_name localhost;
    ssl_certificate {folder}/cert.pem;
    ssl_certificate_key {folder}/key.pem;
    include {folder}/locations.conf;
  }}
}}
'''
    (folder/'nginx.conf').write_text(config)
    run = lambda *args: subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stub = ThreadingHTTPServer(('127.0.0.1', 19847), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    args = ['nginx', '-p', str(folder), '-c', str(folder/'nginx.conf')]
    context = ssl.create_default_context(cafile=str(folder/'cert.pem'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))

    def call(path=ROUTE, body=b'{}', auth=TOKEN, tls=True):
        headers = {'Content-Type': 'application/json'}
        if auth is not None: headers['Authorization'] = 'Bearer '+auth
        req = urllib.request.Request(('https://localhost:19846' if tls else 'http://localhost:19848')+path, data=body, headers=headers)
        try: response = opener.open(req, timeout=5)
        except urllib.error.HTTPError as e: response = e
        with response: return response.status, json.load(response), response.headers

    started = False
    results = []
    try:
        run(*args, '-t'); run(*args); started = True
        for attempt in range(20):
            try:
                status, body, headers = call(auth=None)
                break
            except OSError:
                if attempt == 19: raise
                time.sleep(.1)
        assert status == 401 and body == {'error':'unauthorized'} and headers.get('X-Request-ID') and not calls
        assert call(auth='wrong')[0] == 401 and not calls
        assert call(tls=False)[0] == 426 and not calls
        results.append('missing/wrong Bearer and plaintext rejected before upstream')
        assert call(ROUTE+'/healthz', None)[0:2] == (200, {'ok':True,'version':2})
        raw = json.dumps({'drama_ids':['剧A','A','A'],'external_task_id':'native:1:0','page_group_id':62},ensure_ascii=False).encode()
        status, body, headers = call(body=raw)
        assert status == 200 and body['sha256'] == hashlib.sha256(raw).hexdigest() and body['path'] == ROUTE
        assert headers.get_all('X-Request-ID') == ['stub-request-id'] and calls[-1]['authorization'] == 'Bearer '+TOKEN
        results.append('body bytes, duplicates, Bearer and upstream request ID preserved')
        status, body, headers = call(body=b'503')
        assert status == 503 and headers.get('Retry-After') == '7'
        count = len(calls)
        status, body, headers = call(body=b'disconnect')
        assert status == 503 and body == {'error':'dev_gateway_unavailable'} and headers.get('Retry-After') == '1' and len(calls) == count+1
        results.append('upstream 503 preserved; disconnected upstream mapped to 503 without replay')
        count = len(calls)
        assert call(body=b'x'*65537)[0:2] == (413, {'error':'body_too_large'}) and len(calls) == count
        results.append('oversized body rejected without upstream request')
        time.sleep(3)
        with ThreadPoolExecutor(max_workers=6) as pool:
            output = list(pool.map(lambda _:call(body=b'hold'), range(6)))
        assert sorted(x[0] for x in output) == [200]*5+[429]
        assert [x for x in output if x[0] == 429][0][1] == {'error':'dev_gateway_busy'}
        results.append('five concurrent calls served; sixth receives retryable 429')
        receipt = {'passed':results, 'all_passed':True, 'upstream_calls':len(calls)}
        (folder/'receipt.json').write_text(json.dumps(receipt,indent=2))
        print(json.dumps(receipt))
    finally:
        if started: run(*args, '-s', 'quit')
        stub.shutdown(); stub.server_close()


if __name__ == '__main__': main()
