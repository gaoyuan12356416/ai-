"""Separate growth evidence and operator settings. Publishing ledgers stay read-only."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

DEFAULT_PATH = '/mnt/data-disk/x-tag-publisher/growth/state.sqlite3'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS channel_mode(
 account_id INTEGER PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('revenue','growth')),
 version INTEGER NOT NULL, updated_at TEXT NOT NULL, actor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mode_audit(
 id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, old_mode TEXT NOT NULL,
 new_mode TEXT NOT NULL, version INTEGER NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS growth_post(
 post_id TEXT PRIMARY KEY, account_id INTEGER NOT NULL, x_user_id TEXT NOT NULL,
 content_id TEXT NOT NULL, language TEXT NOT NULL, catalog TEXT NOT NULL,
 published_at TEXT NOT NULL, duration REAL, metadata_json TEXT NOT NULL,
 media_id TEXT NOT NULL, source TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS growth_post_account ON growth_post(account_id,published_at);
CREATE TABLE IF NOT EXISTS post_snapshot(
 post_id TEXT NOT NULL, stage TEXT NOT NULL, observed_at TEXT NOT NULL,
 metrics_json TEXT NOT NULL, PRIMARY KEY(post_id,stage));
CREATE TABLE IF NOT EXISTS follower_snapshot(
 account_id INTEGER NOT NULL, bucket TEXT NOT NULL, observed_at TEXT NOT NULL,
 followers INTEGER NOT NULL CHECK(followers>=0), PRIMARY KEY(account_id,bucket));
CREATE TABLE IF NOT EXISTS growth_profile(
 account_id INTEGER PRIMARY KEY, version TEXT NOT NULL, computed_at TEXT NOT NULL,
 data_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collector_state(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
'''

def stamp():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def path():
    return Path(os.getenv('X_GROWTH_DB_PATH', DEFAULT_PATH))

@contextmanager
def connect(db_path=None, *, write=False):
    target = Path(db_path or path())
    db = sqlite3.connect(str(target) if write else target.resolve().as_uri()+'?mode=ro', uri=not write, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        yield db
        if write:
            db.commit()
    except Exception:
        if write:
            db.rollback()
        raise
    finally:
        db.close()

def initialize(db_path=None):
    target = Path(db_path or path())
    target.parent.mkdir(parents=True, exist_ok=True)
    with connect(target, write=True) as db:
        db.executescript(SCHEMA)

def selection_context(account, db_path=None):
    target = Path(db_path or path())
    default = dict(mode='revenue', mode_version=0, profile={})
    if not target.exists():
        return default
    with connect(target) as db:
        row = db.execute('SELECT mode,version FROM channel_mode WHERE account_id=?', (int(account['id']),)).fetchone()
        if not row:
            return default
        value = dict(mode=row['mode'], mode_version=row['version'], profile={})
        if row['mode'] == 'growth':
            profile = db.execute('SELECT data_json FROM growth_profile WHERE account_id=?', (int(account['id']),)).fetchone()
            value['profile'] = json.loads(profile[0]) if profile else {}
        return value

def channel_details(account_id, db_path=None):
    context = selection_context({'id': account_id}, db_path)
    target = Path(db_path or path())
    if target.exists():
        with connect(target) as db:
            row = db.execute('SELECT data_json FROM growth_profile WHERE account_id=?', (int(account_id),)).fetchone()
            context['growth_profile'] = json.loads(row[0]) if row else {}
            state = db.execute("SELECT value_json FROM collector_state WHERE key='last_run'").fetchone()
            context['collector'] = json.loads(state[0]) if state else {}
    context.pop('profile', None)
    return context

def set_mode(account_id, mode, expected_version, actor, db_path=None):
    if mode not in ('revenue', 'growth') or type(expected_version) is not int or expected_version < 0:
        raise ValueError('invalid_profile_mode')
    with connect(db_path, write=True) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT mode,version FROM channel_mode WHERE account_id=?', (account_id,)).fetchone()
        old, version = (row['mode'], row['version']) if row else ('revenue', 0)
        if version != expected_version:
            raise ValueError('profile_mode_conflict')
        if old == mode:
            return channel_details(account_id, db_path)
        now = stamp()
        db.execute('INSERT OR REPLACE INTO channel_mode VALUES(?,?,?,?,?)', (account_id, mode, version+1, now, actor))
        db.execute('INSERT INTO mode_audit(account_id,old_mode,new_mode,version,actor,created_at) VALUES(?,?,?,?,?,?)',
                   (account_id, old, mode, version+1, actor, now))
    return channel_details(account_id, db_path)

class SnapshotStore:
    """Freeze the explicitly selected mode/profile with newly created tasks."""
    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        return getattr(self.original, name)

    def create_task(self, **kwargs):
        account = dict(kwargs.get('account_snapshot') or {})
        if account.get('id') and 'selection_profile' not in account:
            account['selection_profile'] = selection_context(account)
        return self.original.create_task(**dict(kwargs, account_snapshot=account))
