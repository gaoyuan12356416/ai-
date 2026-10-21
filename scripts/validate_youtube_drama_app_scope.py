"""Read-only source/HTTP evidence; no app import, task, link or platform writes."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace


ROOT = Path('/root/drama_material_service')
FIELDS = ('id', 'name', 'app_id', 'content_id', 'language', 'macro_name', 'macro_desc',
          'macro_url', 'drama_status', 'drama_message', 'drama_cover_status',
          'drama_cover_message', 'drama_cover_url', 'link_ready', 'source_job_id',
          'source_kind', 'long_url', 'duration')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def snapshot(c):
    queries = {
        'preparations': 'SELECT id,state,version,body FROM youtube_auto_preparation ORDER BY id',
        'publication_ledger': 'SELECT id,operation_id,title,description_rendered,status,video_state,video_id,comment_id,unknown_outcome,thumbnail_status,processing_status,public_status FROM drama_youtube_publish ORDER BY id',
        'short_links': 'SELECT * FROM drama_material_short_link ORDER BY id',
    }
    return {key: {'count': len(rows), 'sha256': digest(rows)}
            for key, query in queries.items() for rows in [c.execute(query).fetchall()]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--material-id', required=True)
    parser.add_argument('--expected-status', choices=('matched', 'ambiguous'), required=True)
    parser.add_argument('--resolver', type=Path)
    parser.add_argument('--http', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '/mnt/data-disk'], text=True).strip() != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Data disk mismatch')
    output = args.output.resolve()
    if Path('/mnt/data-disk') not in output.parents:
        raise RuntimeError('Evidence must be on data disk')
    os.umask(0o077)
    pid = subprocess.check_output(['systemctl', 'show', 'drama-material-api.service', '-p', 'MainPID', '--value'], text=True).strip()
    env = dict(item.split('=', 1) for item in Path('/proc/' + pid + '/environ').read_text().split('\0') if '=' in item)
    sys.path.insert(0, str(ROOT))
    from features.youtube_auto_publish.runtime import readonly_runner
    from features.youtube_auto_publish.source import MaterialSource
    from features.youtube_auto_publish.templates import render
    resolver_path = args.resolver or ROOT / 'features/youtube_auto_publish/drama.py'
    spec = importlib.util.spec_from_file_location('features.youtube_auto_publish.drama_validation', resolver_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = SimpleNamespace(MYSQL_PASSWORD=env.get('DRAMA_DB_PASSWORD') or env.get('ADMIN_MAPPING_MYSQL_PASSWORD', ''),
                          MYSQL_USER=env.get('DRAMA_DB_USER') or env.get('ADMIN_MAPPING_MYSQL_USER', ''))
    reader = readonly_runner(app)
    source = MaterialSource(env['YOUTUBE_AUTO_MATERIAL_SQL_FILE'], reader,
                            drama_resolver=module.DramaMetadataResolver(reader))
    with sqlite3.connect('file:' + str(ROOT / 'data/drama_material_jobs.sqlite3') + '?mode=ro', uri=True) as c:
        before = snapshot(c)
        started = time.monotonic()
        material = source.get(args.material_id)
        result = {'resolver_sha256': hashlib.sha256(resolver_path.read_bytes()).hexdigest(),
                  'source_seconds': round(time.monotonic() - started, 3),
                  'material': {key: material.get(key) for key in FIELDS}, 'before': before}
        if args.http:
            import requests
            row = c.execute("SELECT s.session_token FROM drama_admin_session s JOIN drama_admin_user u ON u.user_id=s.user_id AND u.tenant_key=s.tenant_key WHERE s.expires_at>? AND u.role='admin' AND u.status='active' ORDER BY s.expires_at DESC LIMIT 1", (time.time(),)).fetchone()
            if not row:
                raise RuntimeError('No existing admin session for read-only HTTP verification')
            session = requests.Session()
            session.trust_env = False
            session.cookies.set(env.get('DRAMA_SESSION_COOKIE_NAME', 'drama_admin_session'), row[0],
                                domain='ai.yingliangads.com', secure=True)
            http_material = None
            for attempt in range(12):
                response = session.get('https://ai.yingliangads.com/api/youtube-auto-publish/materials',
                                       params={'search': args.material_id, **({'refresh': '1'} if attempt == 0 else {})}, timeout=30)
                if response.status_code != 200:
                    raise RuntimeError('Materials HTTP status ' + str(response.status_code))
                data = response.json()
                item = next((x for x in data.get('items', []) if str(x.get('id')) == args.material_id), None)
                if item and not data.get('cache', {}).get('refreshing'):
                    http_material = item
                    break
                time.sleep(1)
            if http_material is None:
                raise RuntimeError('Fresh HTTP material unavailable')
            result['http'] = {'status': 200, 'attempts': attempt + 1,
                              'material': {key: http_material.get(key) for key in FIELDS}}
            assert http_material['drama_status'] == args.expected_status
            assert (http_material['macro_name'], http_material['macro_desc']) == (material['macro_name'], material['macro_desc'])
        assert material['drama_status'] == args.expected_status
        if args.expected_status == 'matched':
            assert material['app_id'] == '1479' and material['link_ready']
            assert material['drama_message'] == '' and material['macro_name'] and material['macro_desc']
            result['rendered_title'] = render('{name}', material, 'title')
            result['rendered_description'] = render('{desc}', material, 'description')
        result['after'] = snapshot(c)
        result['ledger_unchanged_during_validation'] = before == result['after']
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'output': str(output), 'material_id': material['id'],
                          'status': material['drama_status'], 'link_ready': material['link_ready'],
                          'cover_status': material['drama_cover_status'],
                          'source_seconds': result['source_seconds'],
                          'http_status': result.get('http', {}).get('status'),
                          'ledger_unchanged': result['ledger_unchanged_during_validation']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
