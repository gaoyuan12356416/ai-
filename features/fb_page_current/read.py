"""Read an entire request from one replica snapshot; preserve the version actually read."""
from collections import OrderedDict
import json
import os
from pathlib import Path
import subprocess
import threading

from .common import PAGE,PUBLIC,checked,checked_table,chunks,literal,unpack


def scope(table,hint,exact=False):
    checked_table(table);hint=literal(checked(hint))
    table='ads_ai.'+table
    if exact:return f' FROM {table} WHERE profile_version={hint}'
    # Old format has multiple rows per Page. New format has exactly one version.
    # Prefer the published pointer when it exists in *this* replica transaction.
    return (f' FROM {table} WHERE profile_version=COALESCE((SELECT profile_version FROM {table} '
            f'WHERE profile_version={hint} LIMIT 1),(SELECT MAX(profile_version) FROM {table}))')


def request(reader,hint,ids,*,table=PUBLIC,detail=False,exact=False,step=48,transform=None):
    where=scope(table,hint,exact)
    select=["SELECT HEX(meta_json)"+where+" LIMIT 1"]
    keys={checked(p,PAGE) for p in ids}
    ids=tuple(sorted(keys))
    field='HEX(COMPRESS(detail_json))' if detail else 'summary_json'
    for batch in chunks(ids,step):
        select.append("SELECT HEX(JSON_OBJECT('id',page_id,'value',"+field+",'hash',payload_sha256))"+
                      where+" AND page_id IN ("+','.join(literal(p) for p in batch)+")")
    rows=reader.query(select)
    if not rows:raise FileNotFoundError('snapshot_expired')
    meta=rows[0]
    chosen=checked(meta['version'])
    if exact and chosen!=hint:raise RuntimeError('pinned snapshot mismatch')
    output={}
    for r in rows[1:]:
        pid=r['id']
        if pid in output or pid not in keys:raise RuntimeError('unexpected page identity')
        if detail:
            from .common import sha
            raw=unpack(bytes.fromhex(r['value']))
            if sha(raw)!=r['hash']:raise RuntimeError('invalid page payload')
        else:raw=r['value'].encode()
        output[pid]=transform(pid,raw) if transform else raw
    return output,meta


class Gate:
    """Use the same host FIFO and MySQL read-only transaction as other automated readers."""
    def __init__(self,env_file=Path('/root/drama_material_service/.env')):
        vals={}
        for line in env_file.read_text().splitlines():
            if line.startswith('AD_CONTROL_ACTION_LOG_READER_MYSQL_') and '=' in line:
                k,v=line.split('=',1);vals[k.split('_MYSQL_',1)[1]]=v.strip().strip('"').strip("'")
        if (vals.get('HOST'),vals.get('PORT'))!=('101.32.56.53','63350'):raise ValueError('replica configuration')
        self.user=vals['USER'];self.password=vals['PASSWORD']

    def query(self,statements):
        env=dict(os.environ,MYSQL_PWD=self.password,SQL_GATE_JOB_NAME='fb-page-portrait-http',
                 SQL_GATE_WAIT_TIMEOUT_SECONDS='8',SQL_GATE_EXEC_TIMEOUT_SECONDS='30',SQL_GATE_RETRY_DELAYS_SECONDS='')
        env.pop('SQL_GATE_BYPASS',None)
        sql=("delimiter //\nSET SESSION time_zone='+00:00',max_execution_time=8000,tx_isolation='REPEATABLE-READ'; "
             "START TRANSACTION READ ONLY; SELECT @@read_only; "+'; '.join(statements)+"; ROLLBACK//\n")
        run=subprocess.run(['/usr/bin/mysql','-h','101.32.56.53','-P','63350','-u',self.user,'-D','ads_ai',
                            '--connect-timeout=3','--skip-reconnect','--compress','-N','-B','-r','--default-character-set=utf8mb4'],
                           input=sql,env=env,text=True,encoding='utf-8',capture_output=True,timeout=45)
        lines=run.stdout.splitlines()
        if run.returncode or not lines or lines[0]!='1':raise RuntimeError('gated portrait replica unavailable')
        return [json.loads(bytes.fromhex(s)) for s in lines[1:] if s.strip()]


class SelectionCache:
    def __init__(self):
        self.items=OrderedDict()
        self.lock=threading.Lock()

    def load(self,reader,version,ids,*,table=PUBLIC):
        key=(reader,table,version,ids)
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                return self.items[key]
        warnings=[]
        def project(pid,raw):
            p=json.loads(raw)
            if not isinstance(p,dict) or p.get('page_id')!=pid:
                warnings.append('profile_invalid:'+pid)
                return None
            for audience in p.get('audiences') or []:
                for theme in audience.get('children') or []:theme.pop('dramas',None)
            return {k:p.get(k) for k in ('page_id','status','audiences')}
        projected,meta=request(reader,version,ids,detail=True,transform=project,table=table)
        found={pid:value for pid,value in projected.items() if value is not None}
        value=(found,meta,warnings)
        with self.lock:
            key=(reader,table,meta['version'],ids)
            self.items[key]=value;self.items.move_to_end(key)
            if len(self.items)>8:self.items.popitem(last=False)
        return value

