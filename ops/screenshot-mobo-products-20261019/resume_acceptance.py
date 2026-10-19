"""After upstream supports both packages, resume only the two preserved canaries.

Default is read-only. One durable intent per API request; never replay an uncertain POST.
"""
import argparse
from contextlib import closing
import datetime as dt
import hashlib
import json
from pathlib import Path
import urllib.request

import acceptance

JOBS = {'984145dd754346369b1263afab73ee9b': '3360', 'bd8c36b18ce941e0b0c2760dfc3e1add': '2373'}
OUTPUT = acceptance.STATE / 'canaries-resume.json'


def validate_preserved(job, images):
    assert JOBS.get(job['job_id']) == job['app_id'] and job['content_id'] == '67767322', 'canary identity changed'
    assert job['status'] == 'failed' and 'app_package' in job['error_message'], 'only definitive rejected canaries may resume'
    assets = json.loads(job['assets_json'])
    for field in acceptance.SIZES:
        image = next(i for i in images if i['job_id'] == job['job_id'] and i['field'] == field)
        assert image['url'] == job[field], 'generated URL changed'
        asset = assets[field.removesuffix('_url')]
        path = Path(asset['public_output_path'])
        assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == image['sha256'], 'local generated image missing or changed; do not regenerate'


def save(value):
    temporary = OUTPUT.with_suffix('.new')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(OUTPUT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--upstream-ready', action='store_true', help='Operator confirms both product packages are now supported.')
    args = parser.parse_args()
    assert not OUTPUT.exists(), 'resume has already been prepared; reconcile durable tasks and receipts, never replay'
    images = json.loads((acceptance.STATE / 'generated-images-verification.json').read_text())['generated_images_verified']
    with closing(acceptance.db()) as conn:
        jobs = [dict(conn.execute('SELECT * FROM drama_screenshot_job WHERE job_id=?', (jid,)).fetchone()) for jid in JOBS]
    for job in jobs:
        validate_preserved(job, images)
    rows = acceptance.material_rows(['67767322'])
    names = {f"67767322-{j['drama_name']}-{ratio}-{j['job_id'][:8]}" for j in jobs for ratio in acceptance.RATIOS.values()}
    assert not any(row['name'] in names for row in rows), 'existing receiving or formal material requires reconciliation'
    print('PREFLIGHT: two failed canaries, six intact local images, no existing uploaded materials')
    if not args.apply:
        return
    assert args.upstream_ready, 'confirm upstream support before applying'
    state = {'prepared_at': dt.datetime.utcnow().isoformat(), 'items': []}
    save(state)
    bearer = acceptance.token()
    for job in jobs:
        item = {'job_id': job['job_id'], 'app_id': job['app_id'], 'stage': 'request_prepared'}
        state['items'].append(item)
        save(state)
        request = urllib.request.Request('http://127.0.0.1:8787/api/drama-screenshot-material/jobs/' + job['job_id'] + '/retry',
                                         data=b'{}', headers={'Authorization': 'Bearer ' + bearer, 'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                item['receipt'] = json.load(response)
            receipt = item['receipt']
            assert receipt['job_id'] == job['job_id'] and receipt['force_remake'] is False
            assert receipt['preserved_count'] == 3 and receipt['retry_count'] == 0, 'unexpected regeneration plan'
            item['stage'] = 'accepted'
        except Exception as exc:
            item['stage'] = 'unknown_outcome'
            item['error_type'] = type(exc).__name__
            save(state)
            raise
        save(state)
    print('RESUMED: existing job IDs and three generated sizes preserved for each product')


if __name__ == '__main__':
    main()
