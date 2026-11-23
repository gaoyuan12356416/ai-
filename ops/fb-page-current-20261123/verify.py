"""Read-only business acceptance. No assignment POST, Meta write, notification or replay."""
import argparse
from contextlib import closing
import gzip
import importlib.util
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import time

from operations import *
from features.fb_page_current.read import Gate,SelectionCache,request
from features.fb_page_current.api_store import Store
from features.fb_page_current.pins import Pins
from features.fb_page_current.common import digest_rows
from features.fb_page_current import selection
from features.fb_page_portrait.server import select,PREFIX

QA=ROOT/'qa'
AUTH=QA/'synthetic-auth.sqlite3'
COOKIE='drama_admin_session=isolated-portrait-read-fixture'


def start_pin():
    QA.mkdir(exist_ok=True)
    with closing(sqlite3.connect(AUTH)) as fake,fake:
        fake.execute('CREATE TABLE IF NOT EXISTS drama_admin_session(id INTEGER PRIMARY KEY,session_token TEXT,expires_at INTEGER)')
        fake.execute('INSERT OR REPLACE INTO drama_admin_session VALUES(1,?,?)',(COOKIE.split('=',1)[1],int(time.time())+86400))
    v=current();controller=Store(reader=Gate(),pins=Pins(QA/'sample-pins.sqlite3',AUTH)).for_cookie(COOKIE)
    with closing(connect_reader(env())) as db,db.cursor() as c:
        c.execute(f'SELECT page_id FROM {PUBLIC} WHERE profile_version=%s AND (paid_cents<>0 OR social_cents<>0) '
                  'ORDER BY page_id LIMIT 90',(v,))
        ids=[p for p, in c.fetchall()]
    for part in chunks(ids,6):
        rows,meta=request(controller.store.reader,v,part,detail=True,exact=True)
        for pid,raw in rows.items():
            for a in json.loads(raw)['audiences']:
                for t in a['children']:
                    dramas=t['dramas']
                    if len(dramas)<=50:continue
                    path=a['key']+'/'+t['key'];allowed={pid}
                    code,tree=select(PREFIX+'/'+pid,{'version':[v]},allowed,controller)
                    if code!=200 or tree['meta']['version']!=v:raise RuntimeError('tree read failed')
                    code,page=select(PREFIX+'/'+pid+'/dramas',dict(path=[path],version=[v],page=['1']),allowed,controller)
                    if code!=200 or page['items']!=dramas[:50]:raise RuntimeError('first page differs')
                    code,_=select(PREFIX,{'ids':[pid+',9999999999999']},allowed,controller)
                    if code!=404:raise RuntimeError('mixed authorization not rejected')
                    fact=dict(at=stamp(),pid=pid,v=v,path=path,count=len(dramas),p1=sha(encoded(dramas[:50])),
                              p2=sha(encoded(dramas[50:100])),mixed_acl=404,kind='isolated session, real published payload')
                    atomic(QA/'pin-open.json',fact);return fact
    raise RuntimeError('no paginated sample in this slice')


def finish_pin():
    saved=json.loads((QA/'pin-open.json').read_text());pid=saved['pid'];old=saved['v']
    b=Store(reader=Gate(),pins=Pins(QA/'sample-pins.sqlite3',AUTH)).for_cookie(COOKIE)
    page=b.dramas(pid,saved['path'],old,2)
    latest=b.load([pid],detail=True)
    if not (page['version']==old and page['total']==saved['count'] and sha(encoded(page['items']))==saved['p2']
            and latest['meta']['version']>old):raise RuntimeError('cross-refresh pagination or latest failed')
    fact=dict(at=stamp(),old=old,current=latest['meta']['version'],count=page['total'],second_page_hash=saved['p2'])
    atomic(QA/'pin-verified.json',fact);return fact


def snapshot_metrics(label):
    events={};msec={}
    raw=subprocess.check_output(['journalctl','-u','fb-page-assignment.service','--since','24 hours ago','-n','6500','-o','cat','--no-pager'])
    for line in raw.splitlines():
        try:o=json.loads(line)
        except ValueError:continue
        if not isinstance(o,dict):continue
        ev=o.get('event')
        if ev not in ('assignment_selected','http_response_sent','source_query_failed','http_response_disconnected','database_failure'):continue
        events[ev]=events.get(ev,0)+1
        for field in ('profile_ms','elapsed_ms'):
            if field in o:msec.setdefault(field,[]).append(o[field])
    latency={}
    for key,values in msec.items():
        v=sorted(values);latency[key]=dict(n=len(v),p50=v[math.ceil(len(v)*.5)-1],p95=v[math.ceil(len(v)*.95)-1])
    with closing(sqlite3.connect('file:/mnt/data-disk/fb-page-assignment/outbox.sqlite3?mode=ro',uri=True,timeout=2)) as sq:
        pending=sq.execute('SELECT COUNT(*) FROM fb_page_assignment_requests WHERE revision>synced_revision').fetchone()[0]
    with closing(sqlite3.connect('file:/mnt/data-disk/meta-page-recovery/state.sqlite3?mode=ro',uri=True,timeout=2)) as sq:
        warnings=sum('profile_snapshot_unavailable' in (raw or '') for (raw,) in sq.execute('SELECT proof FROM jobs'))
    fact=dict(at=stamp(),events=events,latency=latency,outbox_pending=pending,recovery_warnings=warnings,
              recovery_pid=subprocess.check_output(['systemctl','show','meta-page-recovery-worker.service','-p','MainPID','--value'],text=True).strip())
    deployed=ROOT/'readers-live.json'
    if deployed.is_file():
        fact['since_handoff']=traffic(dt.datetime.fromisoformat(json.loads(deployed.read_text())['at']))
    atomic(QA/('metrics-'+label+'.json'),fact);return fact


def traffic(since):
    statuses={};latencies=[];alerts={};access={}
    raw=subprocess.check_output(['journalctl','-u','fb-page-assignment.service','--since','@'+str(int(since.timestamp())),'-o','cat','--no-pager'])
    for line in raw.splitlines():
        try:event=json.loads(line)
        except ValueError:continue
        if not isinstance(event,dict):continue
        if event.get('event')=='http_response_sent':
            status=str(event.get('http_status'));statuses[status]=statuses.get(status,0)+1
            if 'elapsed_ms' in event:latencies.append(event['elapsed_ms'])
        if event.get('event')=='assignment_completed':
            for item in event.get('alert_reasons') or []:
                alerts[str(item)]=alerts.get(str(item),0)+1
    for log in Path('/var/log/nginx').glob('access.log*'):
        if not log.is_file() or log.stat().st_mtime<since.timestamp():continue
        with (gzip.open if log.suffix=='.gz' else open)(log,'rb') as handle:
            for line in handle:
                if b'/api/fb-page-portraits' not in line and b'/api/integrations/v1/drama-page-assignments' not in line:continue
                m=re.search(rb'\[([^\]]+)\] "(?:GET|POST) (/api/[^ ]+) HTTP/[^"]+" (\d{3}) ',line)
                if not m:continue
                timestamp=dt.datetime.strptime(m[1].decode(),'%d/%b/%Y:%H:%M:%S %z')
                if timestamp<since:continue
                route='portrait' if m[2].startswith(b'/api/fb-page-portraits') else 'assignment'
                key=route+'_'+m[3].decode();access[key]=access.get(key,0)+1
    latencies.sort()
    return dict(since=since.isoformat(),assignment_http=statuses,assignment_alerts=alerts,nginx=access,
           p50_ms=latencies[math.ceil(len(latencies)*.5)-1] if latencies else None,
           p95_ms=latencies[math.ceil(len(latencies)*.95)-1] if latencies else None)


def recovery_evidence(label):
    saved=QA/('meta-jobs-'+label+'.json.gz')
    if saved.exists():return dict(snapshot=saved.name,existing=True)
    records=[];warnings=0;statuses={}
    with closing(sqlite3.connect('file:/mnt/data-disk/meta-page-recovery/state.sqlite3?mode=ro',uri=True,timeout=3)) as db:
        for task,ad,status,raw in db.execute('SELECT id,ad_id,status,proof FROM jobs ORDER BY id'):
            proof=json.loads(raw or '{}')
            row={key:proof.get(key) for key in ('new_page_id','new_creative_id','ad_update_accepted','confirmed_at','profile_version')}
            row.update(task=task,ad=ad,status=status,proof_sha256=sha(raw or '{}'))
            if 'profile_snapshot_unavailable' in (proof.get('profile_warnings') or []):warnings+=1
            records.append(row);statuses[status]=statuses.get(status,0)+1
    tmp=saved.with_suffix('.partial');tmp.write_bytes(gzip.compress(encoded(records).encode(),mtime=0));tmp.replace(saved)
    return dict(snapshot=saved.name,jobs=len(records),statuses=statuses,warnings=warnings,sha256=sha(saved.read_bytes()))


def fields(db,table,v,ids):
    output={}
    with db.cursor() as c:
        for part in chunks(sorted(ids),80):
            c.execute(f'SELECT page_id,window_start,window_end,paid_cents,social_cents,spend_cents,'
                      f'SHA2(summary_json,256),payload_sha256,SHA2(meta_json,256),created_at FROM {table} '
                      f'WHERE profile_version=%s AND page_id IN ('+','.join(['%s']*len(part))+')',(v,*part))
            for row in c.fetchall():output[row[0]]=row[1:]
    return output


def prepare_selection():
    v=current();proof=receipt(v)
    if selection.available(v):
        return dict(version=v,available=True,bytes=selection.FILE.stat().st_size)
    with closing(connect_reader(env())) as db,db.cursor() as c:
        # Exact latest legacy version is immutable. HOT needs a transaction so every chunk is from one release.
        if not original(c,PUBLIC):c.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
        meta=json.loads(one(c,f'SELECT meta_json FROM {PUBLIC} WHERE profile_version=%s LIMIT 1',(v,))[0])
        with closing(selection.Builder(meta,proof)) as builder:
            for pid,summary,raw,digest,amounts,ts in source_items(db,PUBLIC,v,proof['payload_hashes']):
                if sha(raw)!=digest:raise ValueError('replica detail checksum differs')
                builder.add(pid,json.loads(raw),digest)
            builder.finish()
        db.rollback()
    return dict(version=v,rows=proof['verified_rows'],bytes=selection.FILE.stat().st_size)


def compare():
    cfg=env()
    with closing(connect_reader(cfg)) as db,db.cursor() as c:
        c.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
        rd=ManualReader(db);v=current_state(rd)['version'];proof=receipt(v)
        if original(c,PUBLIC):old,new=PUBLIC,NEXT
        else:old,new=LEGACY,PUBLIC
        schemas=[]
        for table in (old,new):
            c.execute('SELECT column_name,column_type,is_nullable,column_default,extra,collation_name FROM information_schema.columns '
                      'WHERE table_schema=DATABASE() AND table_name=%s ORDER BY ordinal_position',(table,))
            schemas.append(c.fetchall())
        if len(schemas[0])!=12 or schemas[0]!=schemas[1]:raise RuntimeError('public column schema differs')
        a=fields(db,old,v,proof['payload_hashes']);b=fields(db,new,v,proof['payload_hashes'])
        if a!=b or set(a)!=set(proof['payload_hashes']):raise RuntimeError('field or scope differences')
        for table in (old,new):
            n=one(c,f'SELECT COUNT(*) FROM {table} WHERE profile_version=%s',(v,))[0]
            if n!=len(a):raise RuntimeError('extra rows outside frozen scope')
        db.rollback()
        c.execute("SELECT drama_id FROM fb_drama_page_assignment_log WHERE drama_id<>'' AND page_id IS NOT NULL "
                  "ORDER BY task_id DESC,item_index LIMIT 30")
        ids=list(dict.fromkeys(str(d) for d, in c.fetchall()))[:7]
        if not ids:raise RuntimeError('no representative drama tickets')
        # Use the actual selector and freeze all other business inputs exactly once.
        from features.fb_page_assignment.runtime import environment
        from features.fb_page_assignment.selector import assign
        prod=Path('/opt/fb-page-assignment/current/features/fb_page_assignment/sources.py')
        spec=importlib.util.spec_from_file_location('features.fb_page_assignment.legacy_read_probe',prod)
        prior=importlib.util.module_from_spec(spec);spec.loader.exec_module(prior)
        live=prior.Sources(environment('/etc/fb-page-assignment.env'))
        data=live.load(ids);pages=tuple(sorted(set(str(r['page_id']) for r in data['pages'])))
        class OldReader:
            def query(self,sql):
                return live.reader.query([s.replace('ads_ai.'+PUBLIC,'ads_ai.'+old) for s in sql])
        legacy_reader=OldReader()
        start=time.monotonic()
        old_error=None
        try:
            prior_profiles,prior_meta,prior_warnings=prior.mysql_profiles(legacy_reader,v,pages)
            old_ms=round((time.monotonic()-start)*1000,2)
        except prior.SourceError as exc:
            # Record the genuine legacy failure, then replay its identical implementation in smaller immutable slices.
            old_error=exc.code;old_ms=None;prior_profiles={};prior_warnings=[];prior_meta=None
            for part in chunks(pages,24):
                found,meta,notes=prior.mysql_profiles(legacy_reader,v,part)
                if prior_meta and prior_meta!=meta:raise RuntimeError('split old reference read crossed versions')
                prior_profiles.update(found);prior_warnings.extend(notes);prior_meta=meta
        cache=SelectionCache()
        class Counted:
            calls=0
            def query(self,sql):
                self.calls+=1
                return live.reader.query(sql)
        observed=Counted()
        start=time.monotonic()
        new_profiles,new_meta,new_warnings=cache.load(observed,v,pages,table=new)
        cold=(time.monotonic()-start)*1000
        start=time.monotonic();cache.load(observed,v,pages,table=new);warm=(time.monotonic()-start)*1000
        if observed.calls:raise RuntimeError('expected verified selection file for the compared version')
        subset=pages[:9]
        fallback=SelectionCache(selection_path=None).load(live.reader,v,subset,table=new)
        if fallback!=({pid:prior_profiles[pid] for pid in subset if pid in prior_profiles},prior_meta,[]):
            raise RuntimeError('selection SQL fallback differs')
        if (prior_profiles,prior_meta,prior_warnings)!=(new_profiles,new_meta,new_warnings) or not new_profiles:
            raise RuntimeError('selection inputs differ or missing')
        results=assign(ids,data['dramas'],data['pages'],prior_profiles)
        if results!=assign(ids,data['dramas'],data['pages'],new_profiles):raise RuntimeError('selection results differ')
        sample=list(sorted(a))[::max(1,len(a)//16)][:16]
        for detail in (False,True):
            left,lmeta=request(rd,v,sample,table=old,detail=detail,exact=True)
            right,rmeta=request(rd,v,sample,table=new,detail=detail,exact=True)
            if (left,lmeta)!=(right,rmeta):raise RuntimeError('API response differs')
    fact=dict(at=stamp(),version=v,old=old,new=new,rows=len(a),schema_columns=len(schemas[0]),selection_items=len(ids),
              candidate_pages=len(pages),profiled_candidates=len(new_profiles),result_hash=sha(encoded(results)),
              totals={k:proof[k] for k in MONEY},old_cold_ms=old_ms,old_error=old_error,new_cold_ms=round(cold,2),
              new_warm_ms=round(warm,2),selection_sql_calls=observed.calls,sql_fallback_pages=len(subset),api_sample=len(sample))
    atomic(QA/('compare-'+new+'.json'),fact);return fact


if __name__=='__main__':
    ps=argparse.ArgumentParser();ps.add_argument('action',choices=['start-pin','finish-pin','compare','metrics','prepare-selection','recovery-evidence']);ps.add_argument('--label',default='before')
    args=ps.parse_args();disk();QA.mkdir(exist_ok=True)
    run={'start-pin':start_pin,'finish-pin':finish_pin,'compare':compare,'metrics':lambda:snapshot_metrics(args.label),
         'prepare-selection':prepare_selection,'recovery-evidence':lambda:recovery_evidence(args.label)}
    print(encoded(run[args.action]()),flush=True)
