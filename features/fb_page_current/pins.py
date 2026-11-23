"""Keep per-opened-Page details until the originating login session expires."""
from contextlib import closing
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import sqlite3
import time
import zlib

from .common import ROOT,checked,PAGE,encoded,sha

AUTH=Path('/root/drama_material_service/data/drama_material_jobs.sqlite3')
PIN_DB=ROOT/'pins'/'open-pages.sqlite3'


def session(cookie,auth=AUTH):
    jar=SimpleCookie()
    jar.load(cookie)
    value=jar.get(os.environ.get('DRAMA_SESSION_COOKIE_NAME','drama_admin_session'))
    if not value:raise PermissionError('session expired')
    with closing(sqlite3.connect(auth.as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        row=db.execute('SELECT id,expires_at FROM drama_admin_session WHERE session_token=? AND expires_at>?',
                       (value.value,int(time.time()))).fetchone()
    if row is None:raise PermissionError('session expired')
    return row


class Pins:
    def __init__(self,path=PIN_DB,auth=AUTH):
        self.path=path;self.auth=auth

    def db(self):
        self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        db=sqlite3.connect(self.path,timeout=4)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS payload(v TEXT,p TEXT,meta TEXT NOT NULL,body BLOB NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(v,p))')
        db.execute('CREATE TABLE IF NOT EXISTS lease(v TEXT,p TEXT,sid INTEGER,expires INTEGER NOT NULL,PRIMARY KEY(v,p,sid),'
                   'FOREIGN KEY(v,p) REFERENCES payload(v,p) ON DELETE CASCADE)')
        return db

    def save(self,p,v,meta,raw,owner):
        digest=sha(raw);body=zlib.compress(raw,3)
        with closing(self.db()) as db,db:
            db.execute('INSERT OR IGNORE INTO payload VALUES(?,?,?,?,?)',(v,p,encoded(meta),body,digest))
            if db.execute('SELECT digest FROM payload WHERE v=? AND p=?',(v,p)).fetchone()!=(digest,):
                raise RuntimeError('pinned snapshot identity changed')
            db.execute('INSERT OR REPLACE INTO lease VALUES(?,?,?,?)',(v,p,*owner))

    def get(self,p,v,owner):
        if not self.path.exists():return None
        with closing(self.db()) as db,db:
            row=db.execute('SELECT meta,body,digest FROM payload WHERE v=? AND p=?',(v,p)).fetchone()
            if row:
                db.execute('INSERT OR REPLACE INTO lease VALUES(?,?,?,?)',(v,p,*owner))
                raw=zlib.decompress(row[1])
                if sha(raw)!=row[2]:raise RuntimeError('pinned page damaged')
                return raw,json.loads(row[0])
        return None

    def clean(self):
        if not self.path.exists():return 0
        now=int(time.time())
        with closing(self.db()) as db,closing(sqlite3.connect(self.auth.as_uri()+'?mode=ro',uri=True,timeout=3)) as auth,db:
            owners=[sid for sid, in db.execute('SELECT DISTINCT sid FROM lease')]
            for i in range(0,len(owners),100):
                part=owners[i:i+100];marks=','.join('?' for _ in part)
                valid=set(id for id, in auth.execute('SELECT id FROM drama_admin_session WHERE expires_at>? AND id IN ('+marks+')',(now,*part)))
                db.executemany('DELETE FROM lease WHERE sid=?',[(sid,) for sid in part if sid not in valid])
            db.execute('DELETE FROM lease WHERE expires<=?',(now,))
            return db.execute('DELETE FROM payload WHERE NOT EXISTS (SELECT 1 FROM lease WHERE lease.v=payload.v AND lease.p=payload.p)').rowcount
