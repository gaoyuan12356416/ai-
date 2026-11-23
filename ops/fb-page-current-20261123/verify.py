"""Read-only business acceptance. No assignment POST, Meta write, notification or replay."""
import argparse
from contextlib import closing
import importlib.util
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import time

from operations import *
from features.fb_page_current.read import Gate,SelectionCache,request
from features.fb_page_current.api_store import Store
from features.fb_page_current.pins import Pins
from features.fb_page_current.common import digest_rows
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
    atomic(QA/('metrics-'+label+'.json'),fact);return fact


def fields(db,table,v,ids):
    output={}
    with db.cursor() as c:
        for part in chunks(sorted(ids),80):
            c.execute(f'SELECT page_id,window_start,window_end,paid_cents,social_cents,spend_cents,'
                      f'SHA2(summary_json,256),payload_sha256,SHA2(meta_json,256) FROM {table} '
                      f'WHERE profile_version=%s AND page_id IN ('+','.join(['%s']*len(part))+')',(v,*part))
            for row in c.fetchall():output[row[0]]=row[1:]
    return output


def compare():
    cfg=env()
    with closing(connect_reader(cfg)) as db,db.cursor() as c:
        rd=ManualReader(db);v=current_state(rd)['version'];proof=receipt(v)
        if original(c,PUBLIC):old,new=PUBLIC,NEXT
        else:old,new=LEGACY,PUBLIC
        a=fields(db,old,v,proof['payload_hashes']);b=fields(db,new,v,proof['payload_hashes'])
        if a!=b or set(a)!=set(proof['payload_hashes']):raise RuntimeError('field or scope differences')
        for table in (old,new):
            n=one(c,f'SELECT COUNT(*) FROM {table} WHERE profile_version=%s',(v,))[0]
            if n!=len(a):raise RuntimeError('extra rows outside frozen scope')
        c.execute("SELECT drama_id FROM fb_drama_page_assignment_log WHERE drama_id<>'' AND page_id IS NOT NULL "
                  "ORDER BY task_id DESC,item_index LIMIT 30")
        ids=list(dict.fromkeys(str(d) for d, in c.fetchall()))[:7]
        if not ids:raise RuntimeError('no representative drama tickets')
        # Use the actual selector and freeze all other business inputs exactly once.
        from features.fb_page_assignment import sources as new_sources
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
        prior_profiles,prior_meta,prior_warnings=prior.mysql_profiles(legacy_reader,v,pages)
        old_ms=(time.monotonic()-start)*1000
        cache=SelectionCache()
        start=time.monotonic()
        new_profiles,new_meta,new_warnings=cache.load(live.reader,v,pages,table=new)
        cold=(time.monotonic()-start)*1000
        start=time.monotonic();cache.load(live.reader,v,pages,table=new);warm=(time.monotonic()-start)*1000
        if (prior_profiles,prior_meta,prior_warnings)!=(new_profiles,new_meta,new_warnings) or not new_profiles:
            raise RuntimeError('selection inputs differ or missing')
        results=assign(ids,data['dramas'],data['pages'],prior_profiles)
        if results!=assign(ids,data['dramas'],data['pages'],new_profiles):raise RuntimeError('selection results differ')
        sample=list(sorted(a))[::max(1,len(a)//16)][:16]
        for detail in (False,True):
            left,lmeta=request(rd,v,sample,table=old,detail=detail,exact=True)
            right,rmeta=request(rd,v,sample,table=new,detail=detail,exact=True)
            if (left,lmeta)!=(right,rmeta):raise RuntimeError('API response differs')
    fact=dict(at=stamp(),version=v,old=old,new=new,rows=len(a),amounts=proof['paid_cents'],selection_items=len(ids),
              candidate_pages=len(pages),profiled_candidates=len(new_profiles),result_hash=sha(encoded(results)),
              old_cold_ms=round(old_ms,2),new_cold_ms=round(cold,2),new_warm_ms=round(warm,2),api_sample=len(sample))
    atomic(QA/('compare-'+new+'.json'),fact);return fact


if __name__=='__main__':
    ps=argparse.ArgumentParser();ps.add_argument('action',choices=['start-pin','finish-pin','compare','metrics']);ps.add_argument('--label',default='before')
    args=ps.parse_args();disk();QA.mkdir(exist_ok=True)
    run={'start-pin':start_pin,'finish-pin':finish_pin,'compare':compare,'metrics':lambda:snapshot_metrics(args.label)}
    print(encoded(run[args.action]()),flush=True)

