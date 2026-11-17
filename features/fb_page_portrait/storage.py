"""ads_ai is authoritative; indexed immutable SQLite serves bounded projections."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import time
from .runtime import encoded, atomic_json, literal

TABLE = 'ads_ai.fb_page_revenue_profile'
ROOT = '/mnt/data-disk/fb-page-portraits'
VERSION_RE = r'[0-9]{8}T[0-9]{12}Z'
DDL = '''CREATE TABLE IF NOT EXISTS ads_ai.fb_page_revenue_profile (
 profile_version VARCHAR(32) NOT NULL,page_id VARCHAR(32) NOT NULL,
 window_start DATE NOT NULL,window_end DATE NOT NULL,
 paid_cents BIGINT NOT NULL,social_cents BIGINT NOT NULL,spend_cents BIGINT NOT NULL,
 summary_json LONGTEXT NOT NULL,detail_json LONGTEXT NOT NULL,meta_json LONGTEXT NOT NULL,
 payload_sha256 CHAR(64) NOT NULL,created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(profile_version,page_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Managed Facebook Page 7-day paid plus confirmed-social income portraits' '''


def projections(profile):
    tree = {k:v for k,v in profile.items() if k!='audiences'}
    tree['audiences'] = [{**a, 'children': [{k:v for k,v in t.items() if k!='dramas'} for t in a['children']]} for a in profile['audiences']]
    summary = {k:v for k,v in tree.items() if k not in ('audiences','combinations','audience_basis_counts')}
    summary['audiences'] = [{k:v for k,v in a.items() if k!='children'} for a in tree['audiences']]
    return summary,tree


def write(data,env,reader,*,create_table=False):
    import pymysql
    version = data['meta']['version']
    assert re.fullmatch(VERSION_RE,version)
    pages = data['pages']
    payloads = {pid:encoded(p) for pid,p in pages.items()}
    hashes = {pid:hashlib.sha256(body.encode()).hexdigest() for pid,body in payloads.items()}
    existing = []
    if not create_table:
        existing = reader.query([f'SELECT HEX(JSON_OBJECT(\'page_id\',page_id,\'hash\',payload_sha256)) FROM {TABLE} WHERE profile_version={literal(version)}'],timeout=30)
    if existing and {r['page_id']:r['hash'] for r in existing} != hashes:
        raise RuntimeError('existing_version_conflicts_do_not_rewrite')
    if not existing:
        with pymysql.connect(host='101.32.56.53',port=63353,user=env['ADMIN_MAPPING_MYSQL_USER'],
                    password=env['ADMIN_MAPPING_MYSQL_PASSWORD'],database='ads_ai',charset='utf8mb4',
                    connect_timeout=3,read_timeout=30,write_timeout=30,autocommit=False) as db:
            with db.cursor() as q:
                q.execute('SELECT DATABASE(),@@read_only')
                assert q.fetchone() == ('ads_ai',0)
                if create_table: q.execute(DDL)
                for pid,p in pages.items():
                    summary,_ = projections(p)
                    q.execute('INSERT INTO '+TABLE+' (profile_version,page_id,window_start,window_end,paid_cents,social_cents,spend_cents,summary_json,detail_json,meta_json,payload_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                              (version,pid,data['meta']['start'],data['meta']['end'],p['paid_cents'],p['social_cents'],p['spend_cents'],encoded(summary),payloads[pid],encoded(data['meta']),hashes[pid]))
            db.commit()
    verified = False
    for attempt in range(5):
        check = reader.query([f"SELECT HEX(JSON_OBJECT('page_id',page_id,'hash',SHA2(detail_json,256),'paid_cents',paid_cents,'social_cents',social_cents,'spend_cents',spend_cents)) FROM {TABLE} WHERE profile_version={literal(version)}"],timeout=30)
        if {r['page_id']:r['hash'] for r in check} == hashes:
            verified = True; break
        time.sleep(2)
    if not verified: raise RuntimeError('ads_ai_readback_pending_do_not_repeat_write')
    for field,total in data['meta']['audit'].items():
        assert sum(r[field] for r in check) == total
    return dict(version=version,table=TABLE,verified_rows=len(check),payload_hashes=hashes,**data['meta']['audit'])


def publish(data,proof,root=ROOT):
    root = Path(root);root.mkdir(parents=True,exist_ok=True)
    version = data['meta']['version']
    assert proof['version']==version and proof['verified_rows']==len(data['pages'])
    final = root/(version+'.sqlite3')
    temp = root/(version+'.building')
    if final.exists() or temp.exists(): raise RuntimeError('projection_version_already_exists')
    with closing(sqlite3.connect(temp)) as db:
        db.executescript('''CREATE TABLE profiles(page_id TEXT PRIMARY KEY,summary TEXT NOT NULL,tree TEXT NOT NULL);
        CREATE TABLE dramas(page_id TEXT,path TEXT,position INTEGER,payload TEXT,PRIMARY KEY(page_id,path,position));
        CREATE TABLE metadata(payload TEXT);''')
        for pid,p in data['pages'].items():
            assert hashlib.sha256(encoded(p).encode()).hexdigest()==proof['payload_hashes'][pid]
            summary,tree = projections(p)
            db.execute('INSERT INTO profiles VALUES(?,?,?)',(pid,encoded(summary),encoded(tree)))
            for a in p['audiences']:
                for t in a['children']:
                    path=a['key']+'/'+t['key']
                    db.executemany('INSERT INTO dramas VALUES(?,?,?,?)',[(pid,path,i,encoded(d)) for i,d in enumerate(t['dramas'])])
        meta = {k:v for k,v in data['meta'].items() if k not in ('audit','page_count')}
        db.execute('INSERT INTO metadata VALUES(?)',(encoded(meta),))
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        db.commit()
    temp.replace(final)
    if (root/'current.json').exists(): atomic_json(root/'previous.json',json.loads((root/'current.json').read_text()))
    atomic_json(root/'current.json',dict(version=version,database=final.name))


class Store:
    def __init__(self,root=ROOT):self.root=Path(root)

    def connect(self,version=None):
        current=json.loads((self.root/'current.json').read_text())
        if version is not None and not re.fullmatch(VERSION_RE,version):raise ValueError('invalid_version')
        chosen=version or current['version']
        path=self.root/(chosen+'.sqlite3')
        if not path.exists():raise FileNotFoundError('snapshot_expired')
        return sqlite3.connect('file:'+str(path)+'?mode=ro&immutable=1',uri=True)

    def load(self,ids,*,detail=False,version=None):
        assert 1<=len(ids)<=20
        with closing(self.connect(version)) as db:
            field='tree' if detail else 'summary'
            rows=db.execute(f'SELECT page_id,{field} FROM profiles WHERE page_id IN ('+','.join('?' for _ in ids)+')',ids).fetchall()
            meta=json.loads(db.execute('SELECT payload FROM metadata').fetchone()[0])
        return dict(items={pid:json.loads(p) for pid,p in rows},meta=meta)

    def dramas(self,pid,path,version,page):
        with closing(self.connect(version)) as db:
            total=db.execute('SELECT COUNT(*) FROM dramas WHERE page_id=? AND path=?',(pid,path)).fetchone()[0]
            rows=db.execute('SELECT payload FROM dramas WHERE page_id=? AND path=? AND position>=? AND position<? ORDER BY position',
                            (pid,path,(page-1)*50,page*50)).fetchall()
        return dict(items=[json.loads(r[0]) for r in rows],page=page,page_size=50,total=total,version=version)
