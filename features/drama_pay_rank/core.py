from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from urllib.request import Request

from features.drama_audience.client import read_json
from features.drama_audience.store import records, language, now, AUDIENCES

ROOT = '/mnt/data-disk/drama-pay-rank'
ENDPOINT = 'https://marketing.mydramawave.com/api/internal/material/series-pay-rank/list'


def database(root=ROOT, write=False):
    path = Path(root)/'rank.sqlite3'
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path), timeout=15)
        db.executescript('''CREATE TABLE IF NOT EXISTS snapshots (
          date TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS status (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);''')
    else:
        db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=3)
    return db


def validate(data):
    if data.get('code') != 200:
        raise ValueError('upstream_business_error')
    payload = data.get('data') or {}
    day, rows = payload.get('date', ''), payload.get('list', [])
    if not day or not rows:
        raise ValueError('not_generated')
    date.fromisoformat(day)
    identities, ranks = set(), set()
    for row in rows:
        if row.get('date') != day or not isinstance(row.get('series_key'), str) or not row['series_key'] or not row.get('language_code'):
            raise ValueError('invalid_rank_identity')
        if type(row.get('rank')) is not int or not 1 <= row['rank'] <= 100:
            raise ValueError('invalid_rank')
        key=(row['language_code'],row['series_key']); rank=(row['language_code'],row['rank'])
        if key in identities or rank in ranks:
            raise ValueError('duplicate_rank')
        identities.add(key); ranks.add(rank)
    return {'date':day,'list':[{k:r[k] for k in ('date','language_code','series_key','rank')} for r in rows]}


def update(token, root=ROOT, fetcher=read_json, today=None):
    today = today or datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    with closing(database(root, True)) as db:
        try:
            data = validate(fetcher(Request(ENDPOINT,data=b'{}',headers={'Content-Type':'application/json','token':token},method='POST')))
            stamp = now()
            status = dict(status='ok' if data['date']==today else 'not_generated',attempted_at=stamp,date=data['date'],expected_date=today)
            with db:
                db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?)',(data['date'],stamp,json.dumps(data,ensure_ascii=False)))
                db.execute('INSERT OR REPLACE INTO status VALUES(1,?)',(json.dumps(status),))
            return status
        except Exception as exc:
            status=dict(status='not_generated' if str(exc)=='not_generated' else 'error',
                        attempted_at=now(),expected_date=today,error=type(exc).__name__)
            with db:
                db.execute('INSERT OR REPLACE INTO status VALUES(1,?)',(json.dumps(status),))
            return status


def read(day='', lang='en', root=ROOT, audience_db=None):
    if day:
        date.fromisoformat(day)
    try:
        with closing(database(root)) as db:
            dates = [r[0] for r in db.execute('SELECT date FROM snapshots ORDER BY date DESC')]
            selected = day or (dates[0] if dates else '')
            row = db.execute('SELECT fetched_at,payload FROM snapshots WHERE date=?',(selected,)).fetchone()
            status_row=db.execute('SELECT payload FROM status WHERE id=1').fetchone()
            status=json.loads(status_row[0]) if status_row else {}
    except sqlite3.OperationalError:
        dates, selected, row, status = [], day, None, {'status':'not_generated'}
    payload=json.loads(row[1]) if row else {'list':[]}
    languages=sorted({r['language_code'] for r in payload['list']})
    rows=sorted([dict(r) for r in payload['list'] if not lang or r['language_code']==lang],key=lambda r:(r['language_code'],r['rank']))
    catalog=records([r['series_key'] for r in rows],audience_db)
    from .metadata import read as read_metadata
    details=read_metadata(root)
    for r in rows:
        info=catalog.get(r['series_key'])
        exact=bool(info and language(r['language_code']) in info['languages'])
        r.update(name=info['name'] if exact else '',audience=AUDIENCES.get(info['audience'],'未标注') if exact else '',metadata_status=info['status'] if exact else 'unmatched')
        detail=details.get((r['series_key'],language(r['language_code'])),{})
        r.update(resource_id=detail.get('resource_id',''),labels=detail.get('labels',[]),
                 label_names=detail.get('label_names',[]),secondary_labels=detail.get('secondary_labels',[]),
                 detail_status=detail.get('status','missing'))
    return dict(date=selected,dates=dates,languages=languages,language_code=lang,list=rows,
                fetched_at=row[0] if row else '',status=status,
                availability='available' if row and rows else 'language_empty' if row else 'not_generated',
                window_start=(date.fromisoformat(selected)-timedelta(days=3)).isoformat() if selected else '',
                window_end=(date.fromisoformat(selected)-timedelta(days=1)).isoformat() if selected else '')
