"""Durable actual-time counter observations, independent of report refreshes.

Daily projections keep the last successful observation of each Beijing day.
The closing pass is entirely before midnight so it never assigns tomorrow's
count to yesterday. Missing days and decreasing counters remain unknown.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from . import window

METRICS = (*window.COUNTERS, 'reposts', 'quotes', 'bookmarks')


def closing_start(now, hour=21):
    local = now.astimezone(window.BJ)
    return local.replace(hour=hour, minute=0, second=0, microsecond=0) if local.hour >= hour else None


def prepare(db):
    db.execute('CREATE TABLE IF NOT EXISTS counter_history(id INTEGER PRIMARY KEY AUTOINCREMENT,'
               'post_id TEXT NOT NULL,observed_at TEXT NOT NULL,metrics TEXT NOT NULL,'
               'UNIQUE(post_id,observed_at))')
    db.execute('CREATE INDEX IF NOT EXISTS counter_history_time ON counter_history(observed_at)')


def save(db, post_id, values, stamp):
    seen = window.instant(stamp)
    values = {m:values[m] for m in METRICS if type(values.get(m)) is int and values[m] >= 0}
    if not seen or not values:
        return 0
    stamp = seen.astimezone(timezone.utc).isoformat()
    old = db.execute('SELECT metrics FROM counter_history WHERE post_id=? AND observed_at=?', (post_id, stamp)).fetchone()
    merged = dict(json.loads(old[0]) if old else {}, **values)
    raw = json.dumps(merged, sort_keys=True, separators=(',', ':'))
    if old and old[0] == raw:
        return 0
    # A new sequence number also makes late recovery visible to projections.
    db.execute('INSERT OR REPLACE INTO counter_history(post_id,observed_at,metrics) VALUES(?,?,?)', (post_id, stamp, raw))
    return 1


def save_post(db, post):
    groups = {}
    for metric in METRICS:
        stamp = (post.get('_metric_times') or {}).get(metric, post.get('metrics_at'))
        if stamp:
            groups.setdefault(stamp, {})[metric] = post.get(metric)
    return sum(save(db, post['post_id'], values, stamp) for stamp, values in groups.items())


def prune(db, now=None):
    cutoff = ((now or datetime.now(timezone.utc))-timedelta(days=35)).isoformat()
    db.execute('DELETE FROM counter_history WHERE observed_at<?', (cutoff,))


def project(db, raw_path, cutoff):
    """Merge newly saved samples, including recovered old dates, exactly once."""
    path = Path(raw_path)
    if not path.is_file():
        return {}
    previous = {}
    if any(r[1] == 'prior' for r in db.execute('PRAGMA database_list')):
        previous = json.loads(db.execute('SELECT value FROM prior.metadata').fetchone()[0])
    cursor = int(previous.get('history_cursor', 0))
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=15)) as raw:
        if not raw.execute("SELECT 1 FROM sqlite_master WHERE name='counter_history'").fetchone():
            return {}
        # One read transaction prevents a collection commit moving the watermark.
        raw.execute('BEGIN')
        high = raw.execute('SELECT COALESCE(MAX(id),0) FROM counter_history').fetchone()[0]
        posts = {r[0] for r in db.execute('SELECT post_id FROM posts')}
        prior_ids = {r[0] for r in db.execute('SELECT post_id FROM prior.posts')} if previous else set()
        raw.execute('CREATE TEMP TABLE newly_seen(post_id TEXT PRIMARY KEY)')
        raw.executemany('INSERT INTO newly_seen VALUES(?)', ((p,) for p in posts-prior_ids))
        count = 0
        for ident, pid, stamp, metrics in raw.execute('SELECT * FROM counter_history '
                'WHERE (id>? OR post_id IN (SELECT post_id FROM newly_seen)) AND id<=? ORDER BY id', (cursor, high)):
            if pid in posts:
                window.record(db, dict(json.loads(metrics), post_id=pid, metrics_at=stamp), cutoff)
                count += 1
    return dict(history_cursor=high, history_samples_merged=count,
                interaction_method='daily_last_observation_delta', closing_window='21:00-24:00 Asia/Shanghai')
