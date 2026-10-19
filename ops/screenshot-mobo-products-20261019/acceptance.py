"""Audited two-product live canaries. Each intent is persisted before its only POST."""
import argparse
from contextlib import closing
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import urllib.request

from PIL import Image

LIVE = Path('/root/drama_material_service')
STATE = Path('/mnt/data-disk/codex/tasks/screenshot-mobo-products-20261019')
MANIFEST = STATE / 'canaries.json'
SIZES = {'square_1x1_url': (1200, 1200), 'landscape_1_91x1_url': (1200, 628), 'portrait_4x5_url': (1200, 1500)}
RATIOS = {'square_1x1_url': '1:1', 'landscape_1_91x1_url': '1.91:1', 'portrait_4x5_url': '4:5'}


def db():
    c = sqlite3.connect('file:' + str(LIVE / 'data/drama_material_jobs.sqlite3') + '?mode=ro', uri=True, timeout=10)
    c.execute('PRAGMA query_only=ON')
    c.row_factory = sqlite3.Row
    return c


def save(value):
    temporary = MANIFEST.with_suffix('.new')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(str(temporary), str(MANIFEST))


def token():
    pid = subprocess.check_output(['systemctl', 'show', 'drama-material-api', '-p', 'MainPID', '--value'], universal_newlines=True).strip()
    env = dict(s.split('=', 1) for s in Path('/proc/' + pid + '/environ').read_text().split('\0') if '=' in s)
    return env.get('AUTO_COVER_API_TOKEN') or env.get('DRAMA_SCREENSHOT_API_TOKEN') or env['AI_COVER_API_TOKEN']


def submit(content_id='84322322'):
    assert not MANIFEST.exists(), 'canaries already prepared; inspect and reconcile, never resubmit'
    items = [{'app_id': app, 'content_id': content_id, 'kind': kind, 'stage': 'planned'}
             for app, kind in [('3360', 'single'), ('2373', 'batch')]]
    with closing(db()) as c:
        for item in items:
            assert not c.execute('SELECT job_id FROM drama_screenshot_job WHERE app_id=? AND content_id=?',
                                 (item['app_id'], item['content_id'])).fetchone(), 'existing canary: review before any POST'
    state = {'purpose': 'operator-authorized real acceptance, independent of automatic spend filter',
             'prepared_at': dt.datetime.utcnow().isoformat(), 'items': items}
    save(state)
    bearer = token()
    for item in items:
        item['stage'] = 'request_prepared'
        save(state)
        batch = item['kind'] == 'batch'
        payload = {'app_id': item['app_id']}
        payload['content_ids' if batch else 'content_id'] = [item['content_id']] if batch else item['content_id']
        request = urllib.request.Request('http://127.0.0.1:8787/api/drama-screenshot-material/jobs' + ('/batch' if batch else ''),
                                         data=json.dumps(payload).encode(),
                                         headers={'Authorization': 'Bearer ' + bearer, 'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                item['receipt'] = json.load(response)
            receipt = item['receipt']['items'][0] if batch else item['receipt']
            assert receipt.get('job_id'), 'no accepted job ID'
            item['job_id'] = receipt['job_id']
            item['stage'] = 'accepted'
        except Exception as exc:
            item['stage'] = 'unknown_outcome'
            item['error_type'] = type(exc).__name__
            save(state)
            raise
        save(state)
    print(json.dumps([{'app_id': i['app_id'], 'job_id': i['job_id'], 'stage': i['stage']} for i in items]))


def material_ids_for_jobs(jobs):
    return [str(int(hashlib.sha1((job['job_id'] + ':' + field.removesuffix('_url')).encode()).hexdigest()[:15], 16))
            for job in jobs for field in SIZES]


def material_rows(content_ids, material_ids=()):
    module = runpy.run_path(str(LIVE / 'scripts/auto_submit_cover_synthesis.py'))
    module['load_env_file'](str(LIVE / '.env'))
    env = os.environ.copy()
    env['MYSQL_PWD'] = env.get('DRAMA_DB_PASSWORD') or env.get('ADMIN_MAPPING_MYSQL_PASSWORD') or ''
    # Manual read-only verification uses the reserved pool; automated jobs retain the FIFO CLI.
    command = ['/usr/bin/mysql.real', '-h', '101.32.56.53', '-P', '63350', '-u',
               env.get('DRAMA_DB_USER') or env.get('ADMIN_MAPPING_MYSQL_USER'),
               '--connect-timeout=8', '--default-character-set=utf8mb4', '-N', '-B', '-e']
    ids = ','.join(module['mysql_escape'](i) for i in content_ids)
    sql = "SET SESSION TRANSACTION READ ONLY; SET SESSION MAX_EXECUTION_TIME=20000; SELECT @@read_only; "
    sql += "SELECT id,product,data_source_id,url,name,type,data_source FROM kunlunads_dev.ads_custom_source WHERE data_source_id IN (%s) AND data_source=6 AND type=1" % ids
    if material_ids:
        # Receiving rows use the external material ID, not the drama ID, and
        # start with type=2 even for images. Never hide an accepted upload from
        # retry preflight by filtering those rows as formal image materials.
        external_ids = ','.join(module['mysql_escape'](i) for i in material_ids)
        sql += " UNION ALL SELECT id,product,data_source_id,url,name,type,data_source FROM kunlunads_dev.ads_custom_source WHERE data_source_id IN (%s) AND data_source=102" % external_ids
    result = subprocess.run(command + [sql], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=30)
    if result.returncode:
        raise RuntimeError('material verification SQL failed')
    lines = result.stdout.splitlines()
    assert lines and lines[0] == '1', 'read-only endpoint required'
    return [dict(zip(('id', 'product', 'data_source_id', 'url', 'name', 'type', 'data_source'), line.split('\t'))) for line in lines[1:]]


def check():
    state = json.loads(MANIFEST.read_text())
    jobs = []
    callbacks = []
    with closing(db()) as c:
        for item in state['items']:
            row = c.execute('SELECT * FROM drama_screenshot_job WHERE app_id=? AND content_id=?',
                            (item['app_id'], item['content_id'])).fetchall()
            assert len(row) == 1, 'canary must resolve to exactly one persisted task'
            job = dict(row[0])
            assert not item.get('job_id') or item['job_id'] == job['job_id'], 'canary ID mismatch'
            jobs.append(job)
            callback = c.execute("SELECT detail_json FROM drama_admin_audit_log WHERE action='callback_screenshot_ai_source' AND target_id=? ORDER BY id DESC LIMIT 1", (job['job_id'],)).fetchone()
            callbacks.append(json.loads(callback[0]) if callback else None)
    print(json.dumps([{'app_id': j['app_id'], 'job_id': j['job_id'], 'status': j['status'],
                      'progress': j['progress'], 'detail': j['progress_detail'], 'error': j['error_message'][:300]} for j in jobs], ensure_ascii=False))
    if any(j['status'] != 'done' for j in jobs):
        return
    images = []
    for job, callback in zip(jobs, callbacks):
        assert callback and callback.get('sent_count') == 3 and callback.get('status_code') == 200
        body = callback['response']
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = json.loads(body.replace('\\"', '"'))
        assert parsed.get('code') == 0, 'business callback failure'
        for field, size in SIZES.items():
            url = job[field]
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read(30 * 1024 * 1024)
            picture = Image.open(io.BytesIO(data))
            picture.load()
            assert picture.size == size, 'unexpected output dimensions'
            target = STATE / 'images' / (job['job_id'] + '-' + field + '.jpg')
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(data)
            images.append({'job_id': job['job_id'], 'app_id': job['app_id'], 'url': url,
                           'size': list(size), 'sha256': hashlib.sha256(data).hexdigest()})
    rows = material_rows(sorted(set(j['content_id'] for j in jobs)), material_ids_for_jobs(jobs))
    materials = []
    for job in jobs:
        expected = 'moboreels' if job['app_id'] == '3360' else 'moboshort'
        for field in SIZES:
            # ai-source ingests bytes under a new COS URL; retain the stable
            # original asset name and verify the ingested image bytes as well.
            name = '%s-%s-%s-%s' % (job['content_id'], job['drama_name'], RATIOS[field], job['job_id'][:8])
            matches = [r for r in rows if r['name'] == name and r['data_source_id'] == job['content_id']
                       and r['data_source'] == '6']
            assert len(matches) == 1, 'material asset name must have exactly one ingested row'
            assert matches[0]['product'].lower() == expected, 'uploaded material belongs to wrong product'
            assert matches[0]['type'] == '1', 'uploaded material is not an image'
            with urllib.request.urlopen(matches[0]['url'], timeout=30) as response:
                ingested = response.read(30 * 1024 * 1024)
            image = next(i for i in images if i['job_id'] == job['job_id'] and i['url'] == job[field])
            assert hashlib.sha256(ingested).hexdigest() == image['sha256'], 'ingested image bytes differ'
            matches[0]['sha256'] = image['sha256']
            materials.append(matches[0])
    result = {'accepted': True, 'verified_at': dt.datetime.utcnow().isoformat(),
              'jobs': [{'app_id': j['app_id'], 'job_id': j['job_id'], 'content_id': j['content_id'], 'status': j['status']} for j in jobs],
              'callbacks': callbacks, 'images': images, 'materials': materials}
    (STATE / 'acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print('ACCEPTED: 2 jobs, 6 decoded public images, 6 exact product material records')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('submit', 'check'))
    parser.add_argument('--content-id', default='84322322')
    parser.add_argument('--manifest-name', default='canaries.json', choices=('canaries.json', 'canaries-alternate.json'))
    args = parser.parse_args()
    MANIFEST = STATE / args.manifest_name
    submit(args.content_id) if args.action == 'submit' else check()
