"""Shared table identities, replica protocol and durable refresh receipts."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import zlib

PUBLIC = 'fb_page_revenue_profile'
NEXT = PUBLIC + '_next'
LEGACY = PUBLIC + '_legacy_20261123'
STATE = PUBLIC + '_state'
STAGE = PUBLIC + '_stage'
ROOT = Path('/mnt/data-disk/fb-page-current-20261123')
PORTRAITS = Path('/mnt/data-disk/fb-page-portraits')
MONEY = ('paid_cents', 'social_cents', 'spend_cents')
VERSION = re.compile(r'[0-9]{8}T[0-9]{12}Z')
PAGE = re.compile(r'[1-9][0-9]{0,31}')
COLUMNS = ('profile_version','page_id','window_start','window_end',*MONEY,
           'summary_json','detail_json','meta_json','payload_sha256','created_at')


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def sha(value):
    return hashlib.sha256(value if isinstance(value,bytes) else value.encode()).hexdigest()


def literal(value):
    return '0x'+str(value).encode().hex() if str(value) else "''"


def checked(value, pattern=VERSION):
    if not isinstance(value,str) or not pattern.fullmatch(value):
        raise ValueError('invalid snapshot identity')
    return value


def checked_table(table):
    if table not in (PUBLIC,NEXT,LEGACY):
        raise ValueError('unknown portrait table')
    return table


def atomic(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.'+path.name+'.',delete=False) as f:
        tmp=Path(f.name)
        f.write(encoded(value).encode())
        f.flush()
        os.fsync(f.fileno())
    try:
        tmp.replace(path)
        fd=os.open(path.parent,os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def pack(raw):
    body=zlib.compress(raw,3)
    return struct.pack('<I',len(raw))+body+(b'.' if body.endswith(b' ') else b'')


def unpack(data):
    raw=zlib.decompress(data[4:])
    if len(raw)!=struct.unpack('<I',data[:4])[0]:
        raise ValueError('compressed payload length mismatch')
    return raw


def scope_hash(ids):
    return sha(encoded(sorted(ids)))


def read_config():
    conf=json.loads((ROOT/'mode.json').read_text())
    if conf['mode'] not in ('shadow','live-mirror','live','legacy'):
        raise ValueError('invalid migration state')
    return conf['mode']


def chunks(seq,n):
    for start in range(0,len(seq),n):
        yield seq[start:start+n]


def query(reader,sql):
    return reader.query(['USE ads_ai',sql])


def current_state(reader):
    rows=query(reader,"SELECT HEX(JSON_OBJECT('version',active_version,'pages',page_count,"
               "'scope',scope_sha256,'manifest',manifest_sha256,'meta',meta_json)) FROM "+STATE+" WHERE singleton=1")
    if len(rows)!=1:raise RuntimeError('missing portrait state')
    return rows[0]


def writer(env):
    import pymysql
    db=pymysql.connect(host='101.32.56.53',port=63353,user=env['ADMIN_MAPPING_MYSQL_USER'],
                       password=env['ADMIN_MAPPING_MYSQL_PASSWORD'],database='ads_ai',charset='utf8mb4',
                       binary_prefix=True,connect_timeout=4,read_timeout=150,write_timeout=45,autocommit=True)
    try:
        with db.cursor() as c:
            c.execute('SELECT DATABASE(),@@read_only')
            if c.fetchone()!=('ads_ai',0):raise RuntimeError('wrong write endpoint')
            c.execute("SET SESSION time_zone='+00:00',lock_wait_timeout=2,innodb_lock_wait_timeout=5")
        return db
    except Exception:
        db.close()
        raise


def digest_rows(reader,table,version,ids,stage=False):
    if stage:
        if table!=STAGE:raise ValueError('invalid stage')
    else:checked_table(table)
    actual={}
    for part in chunks(sorted(ids),64):
        keys=','.join(literal(checked(pid,PAGE)) for pid in part)
        field="IF(detail_json IS NULL,NULL,SHA2(detail_json,256))"
        statement=("SELECT HEX(JSON_OBJECT('id',page_id,'stored',payload_sha256,'calc',"+field+
                   ",'summary',SHA2(summary_json,256),'a',paid_cents,'b',social_cents,'c',spend_cents)) FROM "+table+
                   " WHERE profile_version="+literal(checked(version))+" AND page_id IN ("+keys+")")
        for row in query(reader,statement):
            if row['id'] in actual:raise RuntimeError('duplicate page')
            actual[row['id']]=row
    return actual

