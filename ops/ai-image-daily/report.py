#!/usr/bin/env python3
"""Daily report runner, durable publish recovery and isolated COS publication."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.request import Request, urlopen

from collector import Catalog, TZ, collect_days, collect_lineage, literal
from analysis import build_report
from render import render_report

PREFIX = 'codex-artifacts/html/dramawave-ai-image-daily'
DISK_UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'


def atomic_bytes(path, data):
    path=Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary=path.with_name(path.name+'.tmp')
    with temporary.open('wb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))


def write_json(path, value):
    atomic_bytes(path,json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8'))


def load_json(path, default=None):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else default


def sha(data):
    return hashlib.sha256(data).hexdigest()


def config(path):
    env={}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key,value=line.split('=',1);env[key.strip()]=value.strip().strip(chr(34)).strip(chr(39))
    return env


class Publisher:
    def __init__(self, env):
        from qcloud_cos import CosConfig, CosS3Client
        self.client=CosS3Client(CosConfig(Region=env['COS_REGION'],SecretId=env['COS_SECRET_ID'],SecretKey=env['COS_SECRET_KEY']))
        self.bucket=env['COS_BUCKET']
        if self.bucket!='advertising-1306474899':
            raise ValueError('unexpected_cos_bucket')
        self.domain='https://advertising-1306474899.cos.ap-hongkong.myqcloud.com'

    def url(self,key):
        return self.domain+'/'+key

    def get(self,key):
        from qcloud_cos.cos_exception import CosServiceError
        try:
            response=self.client.get_object(Bucket=self.bucket,Key=key)
            return response['Body'].get_raw_stream().read()
        except CosServiceError as error:
            if error.get_status_code()==404:
                return None
            raise

    def put(self,key,data):
        if not key.startswith(PREFIX+'/'):
            raise ValueError('cos_key_out_of_scope')
        self.client.put_object(Bucket=self.bucket,Key=key,Body=data,ContentType='text/html; charset=utf-8',
                               CacheControl='no-cache, max-age=0, must-revalidate',ContentDisposition='inline')

    def verify(self,key,data):
        expected=sha(data)
        # Verify public URL separately from the authenticated COS API.
        with urlopen(Request(self.url(key)+'?verify='+expected[:16],headers={'Cache-Control':'no-cache'}),timeout=35) as response:
            actual=response.read()
            if response.status!=200 or 'text/html' not in response.headers.get('Content-Type','') or sha(actual)!=expected:
                raise RuntimeError('public_cos_readback_failed')
        return dict(url=self.url(key),sha256=expected,bytes=len(data),http_status=200)


def publish_pending(state,publisher):
    pending=load_json(state/'pending.json')
    existing_history=load_json(state/'history.json',[])
    if existing_history and pending['report_date'] < max(r['date'] for r in existing_history):
        raise ValueError('pending_date_older_than_latest')
    run=Path(pending['run']).resolve()
    if (state/'runs').resolve() not in run.parents:
        raise ValueError('pending_run_outside_state')
    data=(run/'index.html').read_bytes()
    if sha(data)!=pending['sha256']:
        raise ValueError('pending_artifact_changed')
    archive_key=PREFIX+'/daily/'+pending['report_date']+'/index.html'
    existing=publisher.get(archive_key)
    if existing is not None and sha(existing)!=sha(data):
        raise ValueError('immutable_daily_snapshot_conflict')
    if existing is None:
        publisher.put(archive_key,data)
    archive_receipt=publisher.verify(archive_key,data)
    latest_key=PREFIX+'/index.html'
    latest=publisher.get(latest_key)
    backup=state/'backups'/run.name
    backup.mkdir(parents=True,exist_ok=True)
    if not (backup/'rollback.json').exists():
        if latest is not None and sha(latest)!=sha(data):
            atomic_bytes(backup/'index.html',latest)
        write_json(backup/'rollback.json',dict(previous_exists=latest is not None,previous_sha256=sha(latest) if latest is not None else None,
                                              installed_sha256=sha(data),key=latest_key))
    if latest is None or sha(latest)!=sha(data):
        publisher.put(latest_key,data)
    receipt=publisher.verify(latest_key,data)
    history=load_json(state/'history.json',[])
    if not any(r['date']==pending['report_date'] for r in history):
        history.append(pending['history_entry'])
    history.sort(key=lambda r:r['date'],reverse=True)
    write_json(state/'history.json',history)
    write_json(run/'publish-receipt.json',dict(latest=receipt,archive=archive_receipt,backup=str(backup)))
    report=load_json(run/'report.json')
    status=dict(state='published',report_date=pending['report_date'],generated_at=report['generated_at'],
                published_at=dt.datetime.now(TZ).isoformat(timespec='seconds'),run=str(run),url=receipt['url'],
                sha256=receipt['sha256'],validation=report['validation'],summary=report['summary'],backup=str(backup))
    write_json(state/'status.json',status)
    (state/'pending.json').unlink()
    print(json.dumps(status,ensure_ascii=False),flush=True)
    return status


def restore_public(state,publisher,backup):
    backup=Path(backup).resolve()
    if (state/'backups').resolve() not in backup.parents:
        raise ValueError('backup_outside_state')
    meta=load_json(backup/'rollback.json')
    if not meta['previous_exists'] or not (backup/'index.html').exists():
        raise ValueError('no_previous_public_page')
    previous=(backup/'index.html').read_bytes()
    current=publisher.get(meta['key'])
    if sha(previous)!=meta['previous_sha256'] or current is None or sha(current)!=meta['installed_sha256']:
        raise ValueError('rollback_public_page_drift')
    publisher.put(meta['key'],previous)
    print(json.dumps(dict(restored=publisher.verify(meta['key'],previous))))


def run(args):
    import fcntl
    state=Path(args.state).resolve()
    if Path('/mnt/data-disk') not in state.parents:
        raise ValueError('state_must_be_on_data_disk')
    if subprocess.check_output(['findmnt','-n','-o','UUID','/mnt/data-disk'],text=True).strip()!=DISK_UUID:
        raise ValueError('data_disk_not_mounted')
    state.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (state/'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"state":"skipped","reason":"already_running"}');return
        today=dt.datetime.now(TZ).date()
        date=args.date or (today-dt.timedelta(days=1)).isoformat()
        if dt.date.fromisoformat(date)>=today:
            raise ValueError('complete_natural_day_required')
        env=config(args.env_file)
        publisher=Publisher(env)
        if args.restore_public:
            return restore_public(state,publisher,args.restore_public)
        # Recovery owns the existing attempt, never overwrites a frozen daily snapshot.
        if (state/'pending.json').exists():
            publish_pending(state,publisher)
        history=load_json(state/'history.json',[])
        if history and date < max(entry['date'] for entry in history):
            raise ValueError('report_date_older_than_latest')
        if any(entry['date']==date for entry in history):
            print(json.dumps(dict(state='skipped',reason='date_already_published',report_date=date)));return
        generated=dt.datetime.now(TZ)
        run_dir=state/'runs'/generated.strftime('%Y%m%dT%H%M%S%f')
        run_dir.mkdir(parents=True,exist_ok=False)
        catalog=Catalog(env)
        print(json.dumps(dict(phase='lineage',report_date=date)),flush=True)
        mapped,unresolved,apps,counts=collect_lineage(catalog,args.source_db,args.sync_db)
        write_json(run_dir/'lineage.json',dict(materials=mapped,unresolved=unresolved,apps=apps,counts=counts))
        print(json.dumps(dict(phase='lineage_complete',counts=counts,unresolved=len(unresolved))),flush=True)
        end=dt.date.fromisoformat(date)
        dates=[(end-dt.timedelta(days=n)).isoformat() for n in range(6,-1,-1)]
        facts=collect_days(catalog,mapped,apps,dates)
        write_json(run_dir/'facts.json',facts)
        optimizers=sorted({str(row['optimizer']) for row in facts if row['kind']=='ai'})
        names={}
        if optimizers:
            rows=catalog.query("SELECT JSON_OBJECT('id',id,'name',username) FROM admin_users WHERE id IN ("+','.join(str(int(i)) for i in optimizers)+')')
            names={str(r['id']):r['name'] for r in rows}
        report=build_report(mapped,unresolved,facts,date,generated.isoformat(timespec='seconds'),names,'scheduled' if history else 'initial')
        report['query_count']=catalog.queries
        report['home_url']=publisher.url(PREFIX+'/index.html')
        entry=dict(date=date,url=publisher.url(PREFIX+'/daily/'+date+'/index.html'),generated_at=report['generated_at'],snapshot_kind=report['snapshot_kind'])
        html=render_report(report,sorted(history+[entry],key=lambda r:r['date'],reverse=True)).encode('utf-8')
        write_json(run_dir/'report.json',report)
        atomic_bytes(run_dir/'index.html',html)
        write_json(run_dir/'validation.json',report['validation'])
        pending=dict(run=str(run_dir),report_date=date,sha256=sha(html),history_entry=entry)
        write_json(state/'pending.json',pending)
        return publish_pending(state,publisher)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--state',default='/mnt/data-disk/ai-image-daily')
    parser.add_argument('--env-file',default='/root/drama_material_service/.env')
    parser.add_argument('--source-db',default='/root/drama_material_service/data/drama_material_jobs.sqlite3')
    parser.add_argument('--sync-db',default='/mnt/data-disk/youtube-cover-material-sync/sync.sqlite3')
    parser.add_argument('--date')
    parser.add_argument('--restore-public')
    args=parser.parse_args()
    try:
        run(args)
    except Exception as error:
        # Keep tokens, SQL and raw response bodies out of logs and public artifacts.
        failure=dict(state='failed',at=dt.datetime.now(TZ).isoformat(timespec='seconds'),error_type=type(error).__name__)
        if isinstance(error,(ValueError,RuntimeError)) and str(error).replace('_','').replace('-','').replace('.','').isalnum():
            failure['reason']=str(error)
        state=Path(args.state)
        if state.exists() and Path('/mnt/data-disk') in state.resolve().parents:
            write_json(state/'last-failure.json',failure)
        print(json.dumps(failure),file=sys.stderr,flush=True)
        return 1
    return 0


if __name__=='__main__':
    sys.exit(main())
