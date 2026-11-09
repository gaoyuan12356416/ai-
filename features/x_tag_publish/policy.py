"""Stable weighted tag choice; no publishing quotas or dedupe state are shared."""
import hashlib
from contextlib import closing
import json
import math
import os
from pathlib import Path

from features.x_tag_revenue.core import label_index, labels, language
from features.x_tag_revenue.routes import read_snapshot

VERSION = 'revenue-tag-weighted-v1'
ROOT = '/mnt/data-disk/x-tag-publisher'
ID_BASE = 9000000000000


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def portrait_snapshot():
    try:
        data, meta = read_snapshot(os.environ.get('X_TAG_PORTRAIT_ROOT', '/mnt/data-disk/x-channel-tag-revenue'))
        return data, meta
    except (OSError, ValueError, TypeError, KeyError):
        return {'accounts': {}}, {'version': '', 'unavailable': True}


def weighted_order(weights, seed):
    """Exponential races: first tag has probability weight/sum(weights)."""
    ranked = []
    for key, weight in weights.items():
        if weight <= 0:
            continue
        digest = hashlib.sha256((seed + '\0' + key).encode()).digest()
        u = (int.from_bytes(digest[:8], 'big') + 1) / float(2 ** 64 + 1)
        ranked.append((-math.log(max(min(u, 1 - 2 ** -53), 2 ** -64)) / weight, key))
    return [key for _, key in sorted(ranked)]


class Decision:
    def __init__(self, account, identity, snapshot=None):
        data, meta = snapshot if snapshot is not None else portrait_snapshot()
        self.meta = meta
        self.account_id = str(account.get('id') or '')
        self.x_user_id = str(account.get('x_user_id') or '')
        self.identity = identity
        profile = data.get('accounts', {}).get(self.x_user_id, {})
        self.weights = {}
        self.names = {}
        self.reason = ''
        if meta.get('unavailable'):
            self.reason = 'portrait_unavailable'
        elif meta.get('missing_dates'):
            self.reason = 'portrait_incomplete'
        elif not profile or profile.get('status') == 'no_history':
            self.reason = 'no_revenue_history'
        elif profile.get('revenue_cents', 0) <= 0:
            self.reason = 'no_positive_revenue'
        elif any(t.get('share_bp') is None or t.get('revenue_cents', 0) < 0 for t in profile.get('tags', [])):
            self.reason = 'invalid_revenue_distribution'
        else:
            for tag in profile.get('tags', []):
                key = str(tag.get('key') or '')
                weight = tag.get('revenue_cents', 0)
                if key and key != '__unknown__' and weight > 0:
                    self.weights[key] = int(weight)
                    self.names[key] = tag.get('name') or key
            if not self.weights:
                self.reason = 'no_known_revenue_tags'
        seed = canonical([VERSION, identity, self.account_id, self.x_user_id, meta.get('version', '')])
        self.tag_order = weighted_order(self.weights, seed)
        self.positions = {tag: index for index, tag in enumerate(self.tag_order)}

    def rank(self, candidate_labels):
        keys = labels(candidate_labels)
        return min((self.positions[k] for k in keys if k in self.positions), default=len(self.positions))

    def audit(self, candidate_labels, *, fallback=''):
        keys = labels(candidate_labels)
        matched = next((tag for tag in self.tag_order if tag in keys), '')
        preferred = self.tag_order[0] if self.tag_order else ''
        reason = self.reason or fallback
        if not reason and not matched:
            reason = 'no_matching_eligible_tag'
        elif not reason and matched != preferred:
            reason = 'preferred_tag_no_eligible_material'
        return dict(policy_version=VERSION, profile_version=self.meta.get('version', ''),
                    profile_generated_at=self.meta.get('generated_at', ''),
                    profile_source_end=self.meta.get('end', ''),
                    profile_stale=bool(self.meta.get('stale') or self.meta.get('refresh_failed')),
                    account_id=self.account_id, x_user_id=self.x_user_id,
                    preferred_tag=self.names.get(preferred, preferred),
                    selected_tag=self.names.get(matched, matched), selected_tag_key=matched,
                    candidate_labels=list(keys.values()), fallback_reason=reason,
                    weights=[{'key': k, 'name': self.names[k], 'revenue_cents': v}
                             for k, v in sorted(self.weights.items())])


def catalog_labels(source, dramas):
    """Read complete current labels, with the same identity rules as portraits."""
    ids = sorted({str(d.content_id) for d in dramas})
    rows = []
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        sql = ("SELECT DISTINCT content_id,app_id,language,series_code,labels FROM `" + source.schema +
               "`.ads_drama_resource WHERE app_id=%s AND content_id IN (" + ','.join(['%s'] * len(batch)) + ')')
        rows.extend(source._stream_select(sql, [1479] + batch))
    return label_index(rows)


def initialize_ids(path):
    """Reserve a numeric campaign namespace without sharing counters with legacy."""
    import sqlite3
    with closing(sqlite3.connect(str(path), timeout=30)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        tables = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'").fetchall()
        for name, sql in tables:
            if 'AUTOINCREMENT' not in (sql or '').upper():
                continue
            # Never renumber an existing row or reset a sequence on an upgrade.
            row = conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (name,)).fetchone()
            if row is None:
                conn.execute('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)', (name, ID_BASE))
            elif int(row[0]) < ID_BASE:
                if conn.execute('SELECT 1 FROM "' + name.replace('"', '""') + '" LIMIT 1').fetchone():
                    raise RuntimeError('tag_lane_existing_id_namespace_conflict')
                conn.execute('UPDATE sqlite_sequence SET seq=? WHERE name=?', (ID_BASE, name))


def lane_paths(environ=None):
    env = os.environ if environ is None else environ
    root = Path(env.get('X_TAG_STATE_ROOT', ROOT)).resolve()
    result = {name: Path(env[name]).resolve() for name in ('X_POST_DB_PATH', 'X_AUTO_POST_DB_PATH')}
    for path in result.values():
        path.relative_to(root)
    if result['X_POST_DB_PATH'] == result['X_AUTO_POST_DB_PATH']:
        raise ValueError('tag_lane_database_collision')
    return result
