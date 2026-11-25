import json
import os
from pathlib import Path
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

DEFAULT_DB = '/mnt/data-disk/drama-audience/catalog.sqlite3'
VERSION = 'api-audience-20261113-v1'
UNKNOWN = '__unknown__'
AUDIENCES = {'female': '女性', 'male': '男性', 'lgbtq': 'LGBTQ+', 'general': '通用',
             'young_adult': '年轻成年人群', 'family_all': '合家欢',
             'ethnic_es_pt': '特殊族裔（西语、葡语）', UNKNOWN: '未标注'}
BY_NAME = {v: k for k, v in AUDIENCES.items() if k != UNKNOWN}


def now():
    return datetime.now(timezone.utc).isoformat()


def language(value):
    value = str(value or '').strip().lower().replace('_', '-')
    return {'jp': 'ja', 'pt': 'pt-br', 'zh-tw': 'zh-tw'}.get(value, value)


def path(value=None):
    return Path(value or os.environ.get('DRAMA_AUDIENCE_DB', DEFAULT_DB))


def connect(value=None, write=False):
    target = path(value)
    if write:
        target.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(target), timeout=20)
    else:
        db = sqlite3.connect(target.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    return db


def initialize(value=None):
    with closing(connect(value, True)) as db, db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS drama_audience (
          app_id INTEGER NOT NULL, content_id TEXT NOT NULL COLLATE BINARY,
          languages_json TEXT NOT NULL, audience_labels_json TEXT NOT NULL,
          audience TEXT NOT NULL, status TEXT NOT NULL, name TEXT NOT NULL,
          source TEXT NOT NULL, fetched_at TEXT NOT NULL,
          PRIMARY KEY(app_id,content_id));
        CREATE TABLE IF NOT EXISTS pending (
          app_id INTEGER NOT NULL, content_id TEXT NOT NULL COLLATE BINARY,
          priority INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
          PRIMARY KEY(app_id,content_id));
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')


def enqueue(ids, value=None, priority=0):
    with closing(connect(value, True)) as db, db:
        for cid in sorted(set(str(v) for v in ids if v)):
            if not db.execute('SELECT 1 FROM drama_audience WHERE app_id=1479 AND content_id=?', (cid,)).fetchone():
                db.execute('''INSERT INTO pending VALUES(1479,?,?,0,'',?)
                  ON CONFLICT(app_id,content_id) DO UPDATE SET priority=MAX(priority,excluded.priority)''',
                           (cid, priority, now()))


def save(record, value=None, replace=False):
    with closing(connect(value, True)) as db, db:
        verb = 'INSERT OR REPLACE' if replace else 'INSERT OR IGNORE'
        db.execute(verb + ' INTO drama_audience VALUES(?,?,?,?,?,?,?,?,?)',
                   (1479, record['content_id'], json.dumps(record['languages']),
                    json.dumps(record['audience_labels'], ensure_ascii=False), record['audience'],
                    record['status'], record.get('name', ''), 'admin.mydramawave.com/material/list2', now()))
        db.execute('DELETE FROM pending WHERE app_id=1479 AND content_id=?', (record['content_id'],))


def records(ids, value=None):
    result = {}
    ids = sorted(set(str(v) for v in ids if v))
    try:
        with closing(connect(value)) as db:
            for offset in range(0, len(ids), 400):
                batch = ids[offset:offset+400]
                for row in db.execute('SELECT * FROM drama_audience WHERE app_id=1479 AND content_id IN ('+
                                      ','.join('?' for _ in batch)+')', batch):
                    r = dict(row)
                    r['languages'] = json.loads(r.pop('languages_json'))
                    r['audience_labels'] = json.loads(r.pop('audience_labels_json'))
                    result[r['content_id']] = r
    except (OSError, sqlite3.Error, ValueError):
        return {}
    return result


def direction(record=None, expected_language=None):
    status = record.get('status', 'missing') if record else 'missing'
    if record and expected_language and language(expected_language) not in record['languages']:
        status = 'language_mismatch'
    key = record['audience'] if record and status in ('matched', 'unlabeled') else UNKNOWN
    return dict(key=key, basis='api' if key != UNKNOWN else status,
                rules=['authoritative_audience_labels'], evidence=record.get('audience_labels', []) if record else [],
                source='material_list2.audience_labels', fetched_at=record.get('fetched_at', '') if record else '',
                status=status)


def counts(value=None):
    with closing(connect(value)) as db:
        return dict(stored=db.execute('SELECT COUNT(*) FROM drama_audience').fetchone()[0],
                    pending=db.execute('SELECT COUNT(*) FROM pending').fetchone()[0],
                    statuses=dict(db.execute('SELECT status,COUNT(*) FROM drama_audience GROUP BY status')))
