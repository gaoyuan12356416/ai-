"""Read-only production catalog and running preview checks. Never submits."""
import argparse
import base64
import io
import json
import pathlib
import sqlite3
import subprocess
import sys
import uuid
import zipfile
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
    parser.add_argument('--catalog-only', action='store_true')
    args = parser.parse_args()
    pid = subprocess.check_output(['systemctl', 'show', 'fb-auto-post-service.service', '-p', 'MainPID', '--value'], text=True).strip()
    env = dict(v.split('=', 1) for v in pathlib.Path('/proc/' + pid + '/environ').read_text().split('\0') if '=' in v)
    source = SourceRepository(None, SimpleNamespace(mysql=BrokerReadOnlyMySQL(env)))
    db = 'file:/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3?mode=ro'
    with sqlite3.connect(db, uri=True) as conn:
        owners = conn.execute('SELECT tenant_key,created_by FROM fb_video_page WHERE page_id=? AND deleted=0', (args.page_id,)).fetchall()
        if len(owners) != 1:
            raise RuntimeError('Need unique existing Page owner')
        tenant, user = owners[0]
        configured = {r[0] for r in conn.execute('SELECT page_id FROM fb_video_page WHERE tenant_key=? AND deleted=0', (tenant,))}
    actor = IdentityClient(env).resolve(tenant, user)
    pages = source.list_pages(actor)
    available = [p for p in pages if p['page_id'] not in configured and p.get('eligible_token_count') and not p.get('blocked_reason')]
    if not available:
        raise RuntimeError('No authorized unconfigured Page for read-only preview')
    target = available[0]['page_id']
    resolved = source.resolve_materials(args.material_ids, manual=True, enforce_blacklist=False)
    evidence = dict(checked_at=datetime.now(timezone.utc).isoformat(), submitted=False,
                    manual_source_count=len(resolved['items']), source_errors=resolved['errors'],
                    unconfigured_page=target, accessible_pages=len(pages))
    if len(resolved['items']) != len(args.material_ids):
        raise RuntimeError('Source waiver did not resolve all supplied materials')
    operations=[]
    if not args.catalog_only:
        headers={'Authorization':'Bearer '+env['FB_AUTO_POST_INTERNAL_TOKEN'], 'X-FB-Video-Actor':json.dumps(actor)}
        with requests.Session() as session:
            session.trust_env=False
            def post(path, payload):
                operations.append(payload['operation_id'])
                response=session.post('http://127.0.0.1:18835/api/fb-video-publish/'+path, headers=headers, json=payload, timeout=(5,90))
                data=response.json()
                if response.status_code != 200 or not data.get('ok'):
                    raise RuntimeError('Preview failed: '+str(data.get('code')))
                return data
            body=dict(schema_version=2,operation_id=uuid.uuid4().hex,material_ids=args.material_ids,page_ids=[target],
                      processing_mode='original',material_schedules={},pairs=[dict(material_id=m,page_id=target,
                      config_instance_id='',config_version=0,caption_template='',title='') for m in args.material_ids])
            result=post('preflight',body)
            evidence['manual_preview_items']=len(result['items'])
            evidence['blank_defaults_preserved']=all(not i['caption'] and not i['title'] for i in result['items'])
            book=Workbook(); sheet=book.active; sheet.append(list(HEADERS))
            for mid in args.material_ids:
                sheet.append(['Dramawave',target,'原片','是',mid,'',None,''])
            stream=io.BytesIO(); book.save(stream)
            with zipfile.ZipFile(stream,'a',compression=zipfile.ZIP_STORED) as archive:
                archive.writestr('unused.bin',b'x'*(4*1024*1024+1))
            payload=dict(operation_id=uuid.uuid4().hex,filename='readonly-large-preview.xlsx',content_base64=base64.b64encode(stream.getvalue()).decode())
            result=post('excel/preview',payload)
            evidence.update(excel_summary=result.get('summary'), excel_file_bytes=len(stream.getvalue()),
                            excel_skipped=result.get('skipped'))
            if result['summary']['valid'] != len(args.material_ids) or not evidence['blank_defaults_preserved']:
                raise RuntimeError('Preview acceptance differs from expected scope')
            response=session.get('http://127.0.0.1:18835/api/fb-video-publish/pages?purpose=publish',headers=headers,timeout=(5,90))
            items=response.json().get('items',[])
            evidence['unconfigured_page_in_picker']=any(p['page_id']==target and p['version']==0 for p in items)
            if not evidence['unconfigured_page_in_picker']:
                raise RuntimeError('Unconfigured Page missing from picker')
    with sqlite3.connect(db,uri=True) as conn:
        for op in operations:
            if conn.execute('SELECT COUNT(*) FROM fb_video_batch WHERE operation_id=?',(op,)).fetchone()[0]:
                raise RuntimeError('Read-only preview unexpectedly created a batch')
    evidence['operations_created']=0
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(evidence,ensure_ascii=False))


if __name__=='__main__':
    main()
