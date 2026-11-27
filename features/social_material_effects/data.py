"""Immutable report projections. Each channel advances independently."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

ROOT = '/mnt/data-disk/social-material-effects'
CHANNELS = ('x', 'facebook')
METRICS = ('views', 'likes', 'comments', 'shares', 'reposts', 'quotes', 'bookmarks', 'revenue_cents', 'refund_cents')
FILTERS = ('views', 'likes', 'comments', 'shares', 'revenue')
FIELDS = ('post_id', 'namespace', 'material_id', 'material_name', 'catalog', 'content_id', 'drama_name',
          'product', 'language', 'account_id', 'account_username', 'published_at', 'source', 'post_url',
          'metrics_at', 'error_code', 'revenue_through', 'owner', 'tenant')


def channel(value):
    if value not in (*CHANNELS, 'all'):
        raise ValueError('无效的社媒数据渠道')
    return value


def channels(value):
    return CHANNELS if channel(value) == 'all' else (value,)


def utc():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def root_path(root=None):
    return Path(root or os.environ.get('SOCIAL_MATERIAL_EFFECT_ROOT', ROOT))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class AccountCount:
    def __init__(self):
        self.ids = set()

    def step(self, ids):
        self.ids.update(json.loads(ids))

    def finalize(self):
        return len(self.ids)


def publish(root, source, posts, meta):
    if source not in CHANNELS:
        raise ValueError('invalid_channel')
    root = root_path(root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / (source + '.sqlite3')
    temp = root / (source + '.next')
    temp.unlink(missing_ok=True)
    with closing(sqlite3.connect(temp.resolve().as_uri(), uri=True)) as db:
        db.execute('PRAGMA journal_mode=OFF')
        from . import window
        cutoff = window.prepare(db, target, meta.get('generated_at'))
        db.execute('CREATE TABLE posts(' + ','.join(f + ' TEXT' for f in FIELDS)
                   + ',account_ids TEXT,' + ','.join(m + ' INTEGER' for m in METRICS) + ',PRIMARY KEY(post_id))')
        def records():
            for p in posts:
                window.record(db, p, cutoff)
                yield (*(str(p[f]) if p.get(f) is not None else None for f in FIELDS),
                       encoded(sorted(set(map(str, p.get('account_ids') or [p['account_id']])))),
                       *(p.get(m) for m in METRICS))
        db.executemany('INSERT INTO posts VALUES(' + ','.join('?' for _ in range(len(FIELDS)+len(METRICS)+1)) + ')', records())
        db.execute('CREATE INDEX material_posts ON posts(namespace,material_id)')
        db.execute('CREATE INDEX post_date ON posts(published_at)')
        db.execute('CREATE INDEX post_account ON posts(account_id)')
        dims = 'catalog,content_id,language,product,namespace,material_id'
        sums = ','.join(f'SUM({m}) AS {m},COUNT({m}) AS n_{m}' for m in METRICS)
        db.execute(f'CREATE TABLE rollup AS SELECT {dims},COUNT(*) AS posts,{sums} FROM posts GROUP BY {dims}')
        db.execute('CREATE INDEX lookup ON rollup(language,product COLLATE NOCASE,catalog)')
        count = db.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
        from . import sampling
        history_path = root/'facebook-observations.sqlite3' if source == 'facebook' else Path(
            os.environ.get('X_MATERIAL_EFFECT_OBSERVATIONS', '/mnt/data-disk/x-material-effects/observations.sqlite3'))
        history = sampling.project(db, history_path, cutoff)
        meta = dict(meta, channel=source, post_count=count, window_schema=1, **history)
        db.execute('CREATE TABLE metadata(value TEXT NOT NULL)')
        db.execute('INSERT INTO metadata VALUES(?)', (encoded(meta),))
        db.commit()
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('invalid_social_effect_projection')
    os.chmod(temp, 0o644)
    os.replace(temp, target)
    return meta


@contextmanager
def reading(sources, root=None):
    with closing(sqlite3.connect('file::memory:?cache=private', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.create_aggregate('account_count_of', 1, AccountCount)
        meta = {}
        for source in sources:
            if source not in CHANNELS:
                raise ValueError('invalid_channel')
            path = (root_path(root) / (source + '.sqlite3')).resolve()
            db.execute('ATTACH DATABASE ? AS ' + source, (path.as_uri() + '?mode=ro&immutable=1',))
            meta[source] = json.loads(db.execute('SELECT value FROM ' + source + '.metadata').fetchone()[0])
        yield db, meta
