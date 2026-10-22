"""Additive SQLite metadata. This database never stores media bytes or credentials."""
from contextlib import contextmanager, closing
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from . import require


def now():
    return int(time.time())


def identifier(prefix):
    return prefix + uuid.uuid4().hex


def owner(actor):
    require(isinstance(actor, dict) and str(actor.get('user_id', '')).strip(), 'authentication_required', '请先登录', 401)
    return json.dumps([str(actor.get('tenant_key', '')), str(actor['user_id'])], ensure_ascii=False, separators=(',', ':'))


def can_manage(row, actor):
    return bool(actor.get('is_admin')) or row.get('owner') == owner(actor)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS ve_source(id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL,
              name TEXT NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS ve_upload(id TEXT PRIMARY KEY,owner TEXT NOT NULL,operation_id TEXT NOT NULL,
              asset_id TEXT NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
              UNIQUE(owner,operation_id));
            CREATE TABLE IF NOT EXISTS ve_project(id TEXT PRIMARY KEY,owner TEXT NOT NULL,name TEXT NOT NULL,
              version INTEGER NOT NULL,data TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS ve_project_operation(owner TEXT NOT NULL,operation_id TEXT NOT NULL,
              project_id TEXT NOT NULL,draft_hash TEXT NOT NULL,PRIMARY KEY(owner,operation_id));
            CREATE TABLE IF NOT EXISTS ve_task(id TEXT PRIMARY KEY,owner TEXT NOT NULL,project_id TEXT NOT NULL DEFAULT '',
              operation_id TEXT NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL,
              created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,UNIQUE(owner,operation_id));
            CREATE TABLE IF NOT EXISTS ve_project_version(id TEXT PRIMARY KEY,owner TEXT NOT NULL,project_id TEXT NOT NULL,
              version INTEGER NOT NULL,data TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
              UNIQUE(project_id,version));
            CREATE TABLE IF NOT EXISTS ve_batch(id TEXT PRIMARY KEY,owner TEXT NOT NULL,project_id TEXT NOT NULL,
              operation_id TEXT NOT NULL,data TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
              UNIQUE(owner,operation_id));
            CREATE TABLE IF NOT EXISTS ve_output(id TEXT PRIMARY KEY,owner TEXT NOT NULL,task_id TEXT NOT NULL UNIQUE,
              batch_id TEXT NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS ve_source_kind_status ON ve_source(kind,status,updated_at);
            CREATE INDEX IF NOT EXISTS ve_task_status ON ve_task(status,created_at,id);
            CREATE TABLE IF NOT EXISTS ve_lease(name TEXT PRIMARY KEY,holder TEXT NOT NULL,expires_at INTEGER NOT NULL);
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=20000')
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def row(value):
        if value is None:
            return None
        result = dict(value)
        result['data'] = json.loads(result['data'])
        return result

    @staticmethod
    def table(kind):
        require(kind in {'source', 'upload', 'project', 'task', 'project_version', 'batch', 'output'}, 'invalid_record', '记录类型无效')
        return 've_' + kind

    def get(self, kind, record_id, db=None):
        if db is not None:
            return self.row(db.execute('SELECT * FROM '+self.table(kind)+' WHERE id=?', (record_id,)).fetchone())
        with closing(self.connect()) as conn:
            return self.get(kind, record_id, conn)

    def insert(self, kind, values, db):
        values = dict(values)
        values.setdefault('created_at', now()); values.setdefault('updated_at', now())
        values['data'] = encode(values.get('data', {}))
        fields = tuple(values)
        db.execute('INSERT INTO '+self.table(kind)+' ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')', tuple(values[f] for f in fields))

    def update(self, kind, record_id, values, db):
        values = dict(values); values['updated_at'] = now()
        if 'data' in values:
            values['data'] = encode(values['data'])
        require(set(values) <= {'name','status','data','version','updated_at'}, 'invalid_update', '更新字段无效')
        db.execute('UPDATE '+self.table(kind)+' SET '+','.join(f+'=?' for f in values)+' WHERE id=?', (*values.values(), record_id))

    def list(self, kind, where='1=1', args=(), limit=100, offset=0):
        with closing(self.connect()) as db:
            total = db.execute('SELECT COUNT(*) FROM '+self.table(kind)+' WHERE '+where, args).fetchone()[0]
            rows = db.execute('SELECT * FROM '+self.table(kind)+' WHERE '+where+' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
        return [self.row(r) for r in rows], total

    def lease(self, holder, ttl=120):
        with self.transaction() as db:
            row = db.execute("SELECT holder,expires_at FROM ve_lease WHERE name='dispatcher'").fetchone()
            if row and row['holder'] != holder and row['expires_at'] > now():
                return False
            db.execute("INSERT INTO ve_lease(name,holder,expires_at) VALUES('dispatcher',?,?) ON CONFLICT(name) DO UPDATE SET holder=excluded.holder,expires_at=excluded.expires_at", (holder, now()+ttl))
        return True
