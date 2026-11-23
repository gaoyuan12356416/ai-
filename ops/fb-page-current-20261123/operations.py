"""Scoped operations for the Page current-table migration. Run from an exact release."""
from contextlib import closing,contextmanager
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import pymysql
from features.fb_page_current.common import (COLUMNS,LEGACY,MONEY,NEXT,PAGE,PORTRAITS,PUBLIC,ROOT,STAGE,STATE,
    atomic,checked,checked_table,chunks,current_state,encoded,sha,stamp,unpack,writer)
from features.fb_page_current.publish import promote
from features.fb_page_current.schema import install

UUID='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'


def disk():
    if subprocess.check_output(['findmnt','-no','UUID','-T','/mnt/data-disk'],text=True).strip()!=UUID:
        raise RuntimeError('expected data disk not mounted')
    os.umask(0o077)
    ROOT.mkdir(exist_ok=True)


def env():
    values={}
    for line in Path('/root/drama_material_service/.env').read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            k,v=line.split('=',1)
            if k.startswith(('ADMIN_MAPPING_MYSQL_','AD_CONTROL_ACTION_LOG_READER_MYSQL_')):
                values[k]=v.strip().strip('"').strip("'")
    return values


def connect_reader(cfg):
    pre='AD_CONTROL_ACTION_LOG_READER_MYSQL_'
    if (cfg[pre+'HOST'],cfg[pre+'PORT'])!=('101.32.56.53','63350'):raise RuntimeError('replica identity')
    db=pymysql.connect(host=cfg[pre+'HOST'],port=63350,user=cfg[pre+'USER'],password=cfg[pre+'PASSWORD'],
        database='ads_ai',charset='utf8mb4',connect_timeout=5,read_timeout=55,write_timeout=10,autocommit=True)
    with db.cursor() as c:
        c.execute('SELECT DATABASE(),@@read_only')
        if c.fetchone()!=('ads_ai',1):raise RuntimeError('wrong reader')
        c.execute("SET SESSION time_zone='+00:00',max_execution_time=20000")
        c.execute('SET SESSION TRANSACTION READ ONLY')
    return db


class ManualReader:
    def __init__(self,db):self.db=db
    def query(self,statements,**kwargs):
        result=[]
        with self.db.cursor() as c:
            for statement in statements:
                c.execute(statement)
                result.extend(json.loads(bytes.fromhex(row[0])) for row in c.fetchall())
        return result


def one(c,sql,args=()):
    c.execute(sql,args)
    return c.fetchone()


def shape(c,table):
    c.execute('SELECT COLUMN_NAME FROM information_schema.statistics WHERE table_schema=DATABASE() '
              "AND table_name=%s AND index_name='PRIMARY' ORDER BY seq_in_index",(table,))
    return tuple(x[0] for x in c.fetchall())


def original(c,table):
    return shape(c,table)==('profile_version','page_id')


def versions_on(c,table):
    checked_table(table)
    versions=[]
    after=''
    # One range seek per version keeps the legacy inventory independent of the size of its JSON rows.
    while True:
        row=one(c,f'SELECT profile_version FROM {table} WHERE profile_version>%s ORDER BY profile_version LIMIT 1',(after,))
        if row is None:return versions
        after=row[0]
        versions.append(after)


def receipt(version):
    p=PORTRAITS/(checked(version)+'.receipt.json')
    proof=json.loads(p.read_text())
    if proof['version']!=version or proof['verified_rows']!=len(proof['payload_hashes']):raise RuntimeError('invalid legacy receipt')
    return proof


def current():
    return checked(json.loads((PORTRAITS/'current.json').read_text())['version'])


def source_items(db,table,v,ids):
    checked_table(table);checked(v)
    with db.cursor() as c:
        for part in chunks(sorted(ids),16):
            c.execute(f'SELECT page_id,summary_json,COMPRESS(detail_json),payload_sha256,paid_cents,social_cents,spend_cents,created_at '
                      f'FROM {table} WHERE profile_version=%s AND page_id IN ('+','.join(['%s']*len(part))+')',(v,*part))
            rows=c.fetchall()
            if {r[0] for r in rows}!=set(part):raise RuntimeError('source pages missing')
            for pid,summary,packet,digest,a,b,s,instant in rows:
                yield pid,summary,unpack(packet),digest,[a,b,s],instant


def baseline(db):
    v=current();proof=receipt(v)
    with db.cursor() as c:
        ddl=one(c,'SHOW CREATE TABLE '+PUBLIC)[1]
        names={t:shape(c,t) for t in [PUBLIC,NEXT,LEGACY,STATE,STAGE]}
        c.execute('SELECT table_name,data_length,index_length FROM information_schema.tables WHERE table_schema=DATABASE() '
                  'AND table_name LIKE %s',(PUBLIC+'%',))
        sizes=c.fetchall()
        versions=versions_on(c,PUBLIC)
    units={}
    for unit in ['fb-page-portrait.service','fb-page-portrait-refresh.timer','fb-page-portrait-refresh.service',
                 'fb-page-assignment.service','meta-page-recovery-worker.service']:
        units[unit]=subprocess.check_output(['systemctl','show',unit,'-p','ActiveState,SubState,WorkingDirectory,MainPID'],text=True).splitlines()
    fact=dict(at=stamp(),current=v,page_count=proof['verified_rows'],ddl=ddl,primary=names,sizes=sizes,
              version_count=len(versions),versions=versions,services=units)
    if not (ROOT/'baseline.json').exists():atomic(ROOT/'baseline.json',fact)
    return {k:fact[k] for k in ('at','current','page_count','primary','sizes','version_count')}


def prepare(db,cfg):
    with db.cursor() as c:
        if not original(c,PUBLIC) or shape(c,LEGACY):raise RuntimeError('not pre-cutover')
        pk=shape(c,NEXT)
        if pk and pk!=('page_id',):
            if one(c,f'SELECT COUNT(*) FROM {NEXT}')[0]:raise RuntimeError('nonempty incompatible shadow')
    with closing(writer(cfg)) as w:install(w,pk,bool(pk))
    if not (ROOT/'mode.json').exists():atomic(ROOT/'mode.json',dict(mode='shadow',at=stamp()))
    return {'prepared':NEXT}


def seed(db,cfg):
    v=current();proof=receipt(v)
    with db.cursor() as c:
        if not original(c,PUBLIC):raise RuntimeError('seed requires legacy source')
        meta=json.loads(one(c,f'SELECT meta_json FROM {PUBLIC} WHERE profile_version=%s LIMIT 1',(v,))[0])
    if meta['version']!=v or meta['audit']!={k:proof[k] for k in MONEY}:raise RuntimeError('source receipt totals')
    got=promote(ManualReader(db),cfg,NEXT,meta,source_items(db,PUBLIC,v,proof['payload_hashes']),matching=proof['payload_hashes'])
    return dict(version=v,rows=got['verified_rows'])


def main():
    ps=argparse.ArgumentParser();ps.add_argument('action',choices=['baseline','prepare','seed','state'])
    action=ps.parse_args().action
    disk();cfg=env()
    with closing(connect_reader(cfg)) as db:
        if action=='baseline':out=baseline(db)
        elif action=='prepare':out=prepare(db,cfg)
        elif action=='seed':out=seed(db,cfg)
        else:out=current_state(ManualReader(db));out.pop('meta',None)
    print(encoded(out),flush=True)


if __name__=='__main__':main()
