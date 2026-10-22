"""Read-only real catalog comparison and running Excel HTTP preview; never submit."""
import argparse
import base64
import io
import json
import pathlib
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.fb_video_publish.sources import SourceRepository, BrokerReadOnlyMySQL
from features.fb_video_publish.identity_client import IdentityClient
from features.fb_video_publish.excel import HEADERS
from openpyxl import Workbook
import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--page-id', required=True)
    parser.add_argument('--material-ids', nargs='+', required=True)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    args = parser.parse_args()
    pid = subprocess.check_output(['systemctl', 'show', 'fb-auto-post-service.service', '-p', 'MainPID', '--value'], text=True).strip()
    env = dict(v.split('=', 1) for v in pathlib.Path('/proc/' + pid + '/environ').read_text().split('\0') if '=' in v)
    source = SourceRepository(None, SimpleNamespace(mysql=BrokerReadOnlyMySQL(env)))
    original = source.resolve_materials(args.material_ids)
    manual = source.resolve_materials(args.material_ids, enforce_blacklist=False)
    db = 'file:/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3?mode=ro'
    with sqlite3.connect(db, uri=True) as conn:
        row = conn.execute('SELECT snapshot_json FROM fb_video_task WHERE page_id=? ORDER BY created_at_utc DESC LIMIT 1', (args.page_id,)).fetchone()
        if row:
            actor = json.loads(row[0])['actor']
        else:
            config = conn.execute('SELECT tenant_key,created_by FROM fb_video_page WHERE page_id=? AND deleted=0', (args.page_id,)).fetchall()
            if len(config) != 1:
                raise RuntimeError('Need a unique existing Page configuration owner')
            actor = dict(tenant_key=config[0][0], user_id=config[0][1])
    actor = IdentityClient(env).resolve(actor['tenant_key'], actor['user_id'])
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(list(HEADERS))
    for mid in args.material_ids:
        sheet.append(['Dramawave', args.page_id, '原片', '是', mid, '', None, ''])
    stream = io.BytesIO()
    workbook.save(stream)
    operation = uuid.uuid4().hex
    payload = dict(operation_id=operation, filename='blacklist-readonly-check.xlsx',
                   content_base64=base64.b64encode(stream.getvalue()).decode())
    with requests.Session() as session:
        session.trust_env = False
        response = session.post('http://127.0.0.1:18835/api/fb-video-publish/excel/preview',
            headers={'Authorization': 'Bearer ' + env['FB_AUTO_POST_INTERNAL_TOKEN'],
                     'X-FB-Video-Actor': json.dumps(actor)}, json=payload, timeout=(5, 90))
        result = response.json()
    with sqlite3.connect(db, uri=True) as conn:
        count = conn.execute('SELECT COUNT(*) FROM fb_video_batch WHERE operation_id=?', (operation,)).fetchone()[0]
    evidence = dict(checked_at=datetime.now(timezone.utc).isoformat(), page_id=args.page_id,
        material_ids=args.material_ids, original_allowed=list(original['items']), original_errors=original['errors'],
        manual_allowed=list(manual['items']), manual_errors=manual['errors'], http_status=response.status_code,
        http_ok=result.get('ok'), http_summary=result.get('summary'), http_errors=result.get('skipped', []),
        http_error_code=result.get('code'), operation_created=bool(count), submitted=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(evidence, ensure_ascii=False))
    if count or len(manual['items']) != len(args.material_ids) or response.status_code != 200:
        raise RuntimeError('Read-only acceptance failed; inspect safe evidence')


if __name__ == '__main__':
    main()
