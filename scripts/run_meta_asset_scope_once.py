"""Audited operator recovery on a subset of an existing task's exact products.

Run from a verified Git release. Uses the live Cookie owner, source reader,
ledger and ordinary service execution; the operation ID is never regenerated.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-job', required=True)
    parser.add_argument('--products', nargs='+', required=True)
    parser.add_argument('--operation', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    live = Path('/root/drama_material_service')
    disk = Path('/mnt/data-disk')
    assert subprocess.check_output(['findmnt','-n','-o','UUID',str(disk)], text=True).strip() == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert disk.resolve() in args.output.resolve().parents
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    pid = subprocess.check_output(['systemctl','show','drama-material-api.service','-p','MainPID','--value'], text=True).strip()
    for item in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0'):
        if b'=' in item:
            key, value = item.split(b'=',1)
            os.environ[key.decode()] = value.decode()
    os.chdir(live)
    sys.path.insert(0, str(live))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    spec = importlib.util.spec_from_file_location('app',live/'app.py')
    app = importlib.util.module_from_spec(spec)
    sys.modules['app'] = app
    spec.loader.exec_module(app)
    # Live features can be a regular package, which outranks a release's
    # namespace directory. Bind only this package to the verified release.
    package = 'features.fb_ad_asset_delete'
    assert not any(name == package or name.startswith(package+'.') for name in sys.modules), 'Asset package was imported before release binding'
    package_root = Path(__file__).resolve().parents[1]/'features/fb_ad_asset_delete'
    package_spec = importlib.util.spec_from_file_location(package,package_root/'__init__.py',submodule_search_locations=[str(package_root)])
    package_module = importlib.util.module_from_spec(package_spec)
    sys.modules[package] = package_module
    package_spec.loader.exec_module(package_module)
    from features.fb_ad_asset_delete import bridge
    from features.fb_ad_asset_delete.core import actor_key
    assert Path(bridge.__file__).resolve().parent == package_root
    print(json.dumps({'release':str(package_root.parent.parent)}),flush=True)
    service = bridge.get_service(app.__dict__)
    original = service.store.get_job(args.source_job, include_objects=False)
    assert set(args.products) <= {p['id'] for p in original['products']}
    actor = json.loads(original['actor'])
    with sqlite3.connect('file:'+str(app.JOB_DB_PATH)+'?mode=ro',uri=True) as conn:
        rows = conn.execute('SELECT session_token FROM drama_admin_session WHERE tenant_key=? AND user_id=? AND expires_at>? ORDER BY expires_at DESC',
                            (actor[0],actor[1],int(time.time()))).fetchall()
    session = next((value for row in rows if (value := app.load_session(row[0])) and actor_key(value)==original['actor']),None)
    assert session and app.has_module_permission(session,'fb_ad_asset_delete'), 'Owner session or module permission unavailable'
    selected = service.source.selected_products(session,args.products)
    frozen_scope = dict(input_type=original['input_type'],ids=original['ids'],product_ids=args.products)
    audit = dict(source_job=args.source_job,operation=args.operation,scope=frozen_scope,
                 products=[{'id':p['id'],'name':p['name']} for p in selected],execute_authorized=args.execute)
    def save(**fields):
        audit.update(fields)
        temp = args.output/'result.tmp'
        temp.write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
        temp.replace(args.output/'result.json')
        print(json.dumps(fields,ensure_ascii=False),flush=True)
    save(stage='starting',products=audit['products'])
    backup = args.output/'before.sqlite3'
    if not backup.exists():
        with sqlite3.connect('file:'+service.store.path+'?mode=ro',uri=True) as src, sqlite3.connect(str(backup)) as dst:
            src.backup(dst)
            assert dst.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    pending = []
    service.spawn = lambda fn,*values: pending.append((fn,values))
    created = service.preview(session,dict(frozen_scope,request_id=args.operation))
    save(stage='previewing',job_id=created['job_id'],preview_id=created['preview_id'])
    while pending:
        fn, values = pending.pop(0)
        fn(*values)
    job = service.detail(session,created['job_id'])
    save(stage='preview_finished',status=job['status'],summary=job['summary'],error=job.get('error'),phase_results=job['phase_results'])
    if not args.execute or job['status'] not in ('ready','partial','completed','interrupted'):
        return
    # The main API owns the process-wide deletion budget. This worker performs
    # only reads and submits one normal idempotent execution request to it.
    payload = dict(preview_id=job['preview_id'],request_id=args.operation+'-execute',phases=['creative','ad','video'])
    save(stage='submitting_execution',execute_request_id=payload['request_id'])
    try:
        response = requests.post('http://127.0.0.1:8787/api/fb-post-ad-delete/jobs/'+job['job_id']+'/execute',
            json=payload,headers={'Origin':'https://ai.yingliangads.com','Host':'ai.yingliangads.com'},
            cookies={app.SESSION_COOKIE_NAME:session['session_token']},timeout=30,allow_redirects=False)
        result = response.json()
        if response.status_code != 202:
            save(stage='execution_rejected',http_status=response.status_code,error=result)
            return
        save(stage='executing',run=result)
    except (requests.RequestException, ValueError):
        save(stage='execution_response_unknown')
    while True:
        job = service._public(service.store.get_job(job['job_id'],include_objects=False))
        runs = [run for run in job.get('runs',[]) if run.get('request_id')==payload['request_id']]
        if not runs:
            save(stage='execution_unconfirmed',status=job['status'])
            return
        if runs[-1]['status'] != 'running':
            break
        save(stage='executing',status=job['status'],summary=job['summary'],phase_results=job['phase_results'])
        time.sleep(30)
    save(stage='finished',status=job['status'],summary=job['summary'],phase_results=job['phase_results'])


if __name__ == '__main__':
    main()
