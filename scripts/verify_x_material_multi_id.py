"""Read-only deployed broker checks; keep internal credentials in memory."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ids', nargs='+', required=True)
    args = parser.parse_args()
    assert all(value.isascii() and value.isdecimal() for value in args.ids)
    pid = subprocess.check_output(['systemctl', 'show', 'drama-material-api.service', '-p', 'MainPID', '--value'], text=True).strip()
    env = dict(item.split(b'=', 1) for item in Path('/proc/' + pid + '/environ').read_bytes().split(b'\0') if b'=' in item)
    token = env[b'X_POST_AUTOMATION_INTERNAL_TOKEN'].decode()
    results = []
    for separator in (',', ' ', ', '):
        body = json.dumps({'action': 'list', 'query': {'q': separator.join(args.ids), 'product': 'Dramawave', 'limit': '50'}}).encode()
        request = urllib.request.Request('http://127.0.0.1:8787/internal/x-video-publish/source', data=body,
                                         headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=90) as response:
            data = json.load(response)
        found = [str(row['material_id']) for row in data['items']]
        assert len(found) == len(set(found)) and set(found) <= set(args.ids)
        results.append({'separator': repr(separator), 'ids': found, 'missing': sorted(set(args.ids) - set(found)),
                        'seconds': round(time.monotonic() - started, 2), 'empty_reason': data.get('empty_reason', '')})
    assert all(row['ids'] == results[0]['ids'] for row in results)
    for name in ('x-video-publish.html', 'x-video-publish.js'):
        with urllib.request.urlopen('https://ai.yingliangads.com/' + name + '?v=20261104multiid1', timeout=30) as response:
            public = response.read()
        assert hashlib.sha256(public).digest() == hashlib.sha256((Path('/root/drama_material_service/static') / name).read_bytes()).digest()
    print(json.dumps({'searches': results, 'public_assets_match': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
