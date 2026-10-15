"""One-shot retry receipts and read-only recovery checks for the frozen scope."""
import argparse
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import time
import urllib.request

STATE = Path('/mnt/data-disk/codex/tasks/screenshot-model-repair-20261015')
DB = '/root/drama_material_service/data/drama_material_jobs.sqlite3'
FIELDS = ['square_1x1_url', 'landscape_1_91x1_url', 'portrait_4x5_url']
SIZES = [(1200, 1200), (1200, 628), (1200, 1500)]


def rows():
    frozen = json.loads((STATE / 'frozen-failures.json').read_text(encoding='utf-8'))
    conn = sqlite3.connect('file:' + DB + '?mode=ro', uri=True, timeout=10)
    conn.execute('PRAGMA query_only=ON'); conn.row_factory = sqlite3.Row
    result = []
    for previous in frozen:
        row = dict(conn.execute('SELECT * FROM drama_screenshot_job WHERE job_id=?', (previous['job_id'],)).fetchone())
        assert row['content_id'] == previous['content_id'], 'identity drift'
        for field in FIELDS:
            if previous[field]: assert row[field] == previous[field], 'existing output replaced'
        result.append(row)
    conn.close()
    return result


def retry(selected):
    pid = subprocess.check_output(['systemctl', 'show', 'drama-material-api', '-p', 'MainPID', '--value']).decode().strip()
    env = dict(x.decode().split('=', 1) for x in Path('/proc/' + pid + '/environ').read_bytes().split(b'\0') if b'=' in x)
    token = env.get('DRAMA_SCREENSHOT_API_TOKEN') or env.get('DRAMA_SCREENSHOT_API_TOKENS', '').split(',')[0].strip()
    assert token, 'configured screenshot API token missing'
    receipts = STATE / 'retry-receipts'; receipts.mkdir(exist_ok=True)
    for row in selected:
        job_id = row['job_id']; receipt = receipts / (job_id + '.json')
        if receipt.exists():
            print('ALREADY_ATTEMPTED', job_id, row['status']); continue
        assert row['status'] == 'failed', 'only frozen failed rows may be retried'
        with receipt.open('x', encoding='utf-8') as output:
            json.dump({'job_id': job_id, 'intent_at': time.time(), 'result': 'pending_readback'}, output)
        request = urllib.request.Request('http://127.0.0.1:8787/api/drama-screenshot-material/jobs/' + job_id + '/retry',
                                         data=b'{}', headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        # An exception leaves the durable intent pending: reconcile before any replay.
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.load(response)
            assert response.status == 202 and payload['job_id'] == job_id
            assert payload.get('force_remake') is False
        receipt.write_text(json.dumps({'job_id': job_id, 'accepted_at': time.time(), 'response': payload}, indent=2), encoding='utf-8')
        print('ACCEPTED', job_id, 'missing_sizes', payload['retry_count'], flush=True)


def verify(selected):
    from PIL import Image, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    verification = STATE / 'verified-outputs'; verification.mkdir(exist_ok=True)
    for row in selected:
        assert row['status'] == 'done', row['job_id'] + ' is not done'
        checks = []
        for field, dimensions in zip(FIELDS, SIZES):
            url = row[field]
            assert url.startswith('https://'), 'missing public HTTPS output'
            with urllib.request.urlopen(url, timeout=45) as response:
                raw = response.read()
                assert response.status == 200
            with Image.open(io.BytesIO(raw)) as image:
                image.load(); assert image.size == dimensions, str(image.size)
            checks.append({'field': field, 'url': url, 'size': dimensions,
                           'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
        (verification / (row['job_id'] + '.json')).write_text(json.dumps(checks, indent=2), encoding='utf-8')
        print('VERIFIED', row['job_id'], len(checks), flush=True)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=['retry', 'status', 'verify'])
    parser.add_argument('--only'); args = parser.parse_args()
    selected = [r for r in rows() if not args.only or r['job_id'] == args.only]
    assert selected, 'empty selection'
    if args.action == 'retry': retry(selected)
    elif args.action == 'verify': verify(selected)
    else:
        print(json.dumps({'counts': dict(Counter(r['status'] for r in selected)), 'jobs': [
            {'job_id': r['job_id'], 'status': r['status'], 'progress': r['progress'],
             'detail': r['progress_detail'], 'outputs': sum(bool(r[f]) for f in FIELDS),
             'error': r['error_message'][:300] if r['status'] == 'failed' else ''} for r in selected]}, ensure_ascii=False))


if __name__ == '__main__': main()
