"""Running pool preview and immutable-ledger readback; never enqueues or publishes."""
import argparse
import hashlib
import json
import pathlib
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone

import requests

sys.path.insert(0, '/opt/fb-auto-post/current')
from features.fb_video_publish.identity_client import IdentityClient

DB = pathlib.Path('/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3')


def connect(path=DB):
    return closing(sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True))


def pool_facts(page):
    with connect() as conn:
        conn.execute('BEGIN')
        result = {}
        for table in ('page', 'pool', 'pool_item', 'pool_operation', 'pool_slot'):
            rows = conn.execute('SELECT * FROM fb_video_' + table + ' WHERE page_id=? ORDER BY rowid', (page,)).fetchall()
            result[table] = dict(count=len(rows), sha256=hashlib.sha256(json.dumps(rows,ensure_ascii=False).encode()).hexdigest())
        return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--page-id', required=True)
    p.add_argument('--material-ids', nargs='+', required=True)
    p.add_argument('--manifest', type=pathlib.Path, required=True)
    p.add_argument('--output', type=pathlib.Path, required=True)
    args = p.parse_args()
    pid = subprocess.check_output(['systemctl','show','fb-auto-post-service.service','-p','MainPID','--value'],text=True).strip()
    env = dict(v.split('=',1) for v in pathlib.Path('/proc/'+pid+'/environ').read_text().split('\0') if '=' in v)
    with connect() as conn:
        owners = conn.execute('SELECT tenant_key,created_by FROM fb_video_page WHERE page_id=? AND deleted=0',
                              (args.page_id,)).fetchall()
    if len(owners) != 1:
        raise RuntimeError('Need a unique configured Page owner')
    actor = IdentityClient(env).resolve(*owners[0])
    headers = {'Authorization':'Bearer '+env['FB_AUTO_POST_INTERNAL_TOKEN'], 'X-FB-Video-Actor':json.dumps(actor)}
    before = pool_facts(args.page_id)
    with requests.Session() as session:
        session.trust_env = False
        url = 'http://127.0.0.1:18835/api/fb-video-publish/pages/' + args.page_id + '/pool'
        response = session.get(url, headers=headers, timeout=(5,60))
        response.raise_for_status(); pool = response.json()['pool']
        payload = dict(config_instance_id=pool['config_instance_id'], expected_version=pool['version'],
                       material_ids=args.material_ids)
        response = session.post(url+'/items/resolve', headers=headers, json=payload, timeout=(5,90))
        response.raise_for_status(); resolved = response.json()
        if resolved['summary'] != dict(total=len(args.material_ids),valid=len(args.material_ids),skipped=0):
            raise RuntimeError('Pool resolve still rejects supplied materials: '+json.dumps(resolved['skipped'],ensure_ascii=False))
        response = session.post(url+'/items/preview', headers=headers, json=payload, timeout=(5,90))
        response.raise_for_status(); preview = response.json()
        if len(preview['items']) != len(args.material_ids):
            raise RuntimeError('Pool preview count differs')
    if before != pool_facts(args.page_id):
        raise RuntimeError('Page/pool changed during read-only verification; reconcile concurrent operator activity')
    manifest = json.loads(args.manifest.read_text())
    preserved = {}
    with connect(args.manifest.parent/'operational.sqlite3') as old, connect() as live:
        old.execute('BEGIN'); live.execute('BEGIN')
        for table, columns, key in (
                ('fb_video_task','task_id,snapshot_json,caption,short_url,long_url,source_kind,pool_instance_id,pool_sequence','task_id'),
                ('fb_video_link','*','id')):
            rows = old.execute('SELECT '+columns+' FROM '+table).fetchall()
            for row in rows:
                if live.execute('SELECT '+columns+' FROM '+table+' WHERE '+key+'=?',(row[0],)).fetchone() != row:
                    raise RuntimeError('Historical frozen facts changed: '+table)
            preserved[table] = len(rows)
        if live.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite integrity check failed')
    for item in manifest['files']:
        if hashlib.sha256(pathlib.Path(item['target']).read_bytes()).hexdigest() != item['after']:
            raise RuntimeError('Deployed file hash drift')
    evidence = dict(checked_at=datetime.now(timezone.utc).isoformat(), page_id=args.page_id,
                    material_ids=args.material_ids, summary=resolved['summary'], preview_count=len(preview['items']),
                    pool_status=pool['status'], pool_version=pool['version'], submitted=False, pool_facts_unchanged=True,
                    historical_rows_preserved=preserved, quick_check='ok', installed_hashes=len(manifest['files']),
                    commit=manifest['commit'])
    args.output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(evidence,ensure_ascii=False))


if __name__ == '__main__':
    main()
