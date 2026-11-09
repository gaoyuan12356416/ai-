"""Stable weighted tag choice; no publishing quotas or dedupe state are shared."""
import hashlib
from collections.abc import Mapping
from contextlib import closing
import json
import math
import os
from pathlib import Path

from features.x_tag_revenue.core import label_index, labels, language
from features.x_tag_revenue.routes import read_snapshot
from features.x_tag_revenue.tags import VERSION as TAG_MAPPING_VERSION, lookup
from features.x_tag_revenue.taxonomy import VERSION as HIERARCHY_VERSION, UNKNOWN, TYPES, AUDIENCES, THEMES, classify, collect_classifications

VERSION = 'revenue-hierarchy-weighted-v3'
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
        self.profile = profile
        self.orders = {}
        self.node_weights = {}
        self.reason = ''
        if meta.get('unavailable'):
            self.reason = 'portrait_unavailable'
        elif meta.get('missing_dates'):
            self.reason = 'portrait_incomplete'
        elif not profile or profile.get('status') == 'no_history':
            self.reason = 'no_revenue_history'
        elif meta.get('tag_mapping_version') != TAG_MAPPING_VERSION:
            self.reason = 'portrait_mapping_outdated'
        elif meta.get('hierarchy_version') != HIERARCHY_VERSION or profile.get('hierarchy_version') != HIERARCHY_VERSION:
            self.reason = 'portrait_hierarchy_outdated'
        elif profile.get('revenue_cents', 0) <= 0:
            self.reason = 'no_positive_revenue'
        seed = canonical([VERSION, identity, self.account_id, self.x_user_id, meta.get('version', ''), HIERARCHY_VERSION])
        def visit(nodes, path):
            valid = nodes and all(n.get('share_bp') is not None and n.get('revenue_cents', 0) >= 0 for n in nodes)
            weights = {n['key']: n['revenue_cents'] for n in nodes if n.get('matchable') and n.get('revenue_cents', 0) > 0} if valid else {}
            self.node_weights[path] = weights
            self.orders[path] = weighted_order(weights, seed+'\0'+canonical(path))
            for n in nodes:
                if n.get('children'):
                    visit(n['children'], path+(n['key'],))
        if not self.reason:
            visit(profile.get('types', []), ())
            if not self.orders.get(()):self.reason = 'no_valid_type_profile'
        self.weights = self.node_weights.get((), {})
        self.type_order = self.orders.get((), [])

    def rank(self, metadata):
        if self.reason:return (0, 0, 0)
        data = metadata if isinstance(metadata, dict) else classify(metadata)
        dtype, audience = data.get('drama_type', UNKNOWN), data.get('audience', UNKNOWN)
        def position(path, key):
            order = self.orders.get(path, [])
            return order.index(key) if key in order else len(order)
        rank = (position((), dtype),)
        # Missing income at one layer retains its ancestors and the old order
        # inside that branch. Lower layers never override a missing parent.
        if dtype not in self.type_order or not self.orders.get((dtype,)):
            return rank+(0, 0)
        rank += (position((dtype,), audience),)
        if audience not in self.orders[(dtype,)] or not self.orders.get((dtype, audience)):
            return rank+(0,)
        return rank+(min((position((dtype, audience), k) for k in data.get('themes', [UNKNOWN])), default=len(self.orders[(dtype, audience)])),)

    def preferred(self):
        path=[]
        for _ in range(3):
            order=self.orders.get(tuple(path),[])
            if not order:break
            path.append(order[0])
        return path

    def audit(self, metadata, *, fallback=''):
        data = metadata if isinstance(metadata, dict) and metadata else classify(metadata or [], status='candidate_metadata_missing' if not metadata else 'matched')
        dtype, audience = data.get('drama_type', UNKNOWN), data.get('audience', UNKNOWN)
        order = self.orders.get((dtype, audience), []) if audience in self.orders.get((dtype,), []) else []
        matched = next((key for key in order if key in data.get('themes', [])), '')
        preferred = self.preferred()
        selected = [dtype, audience, matched or (data.get('themes') or [UNKNOWN])[0]]
        reason, level = self.reason or fallback, 'all' if self.reason or fallback else ''
        if metadata is not None and not reason:
            if dtype != preferred[0]:reason,level='preferred_type_no_eligible_material','type'
            elif not self.orders.get((dtype,)):reason,level='no_valid_audience_profile','audience'
            elif audience != self.orders[(dtype,)][0]:reason,level='preferred_audience_no_eligible_material','audience'
            elif not order:reason,level='no_valid_theme_profile','theme'
            elif not matched:reason,level='no_matching_eligible_theme','theme'
            elif matched != order[0]:reason,level='preferred_theme_no_eligible_material','theme'
        names = (TYPES, AUDIENCES, THEMES)
        return dict(policy_version=VERSION, tag_mapping_version=TAG_MAPPING_VERSION,
                    hierarchy_version=HIERARCHY_VERSION, classification=data,
                    preferred_path=preferred, preferred_path_names=[names[i][key] for i,key in enumerate(preferred)],
                    selected_path=selected if metadata is not None else [],
                    selected_path_names=[names[i][key] for i,key in enumerate(selected)] if metadata is not None else [],
                    fallback=bool(reason), fallback_level=level,
                    profile_mapping_version=self.meta.get('tag_mapping_version', ''),
                    profile_version=self.meta.get('version', ''),
                    profile_generated_at=self.meta.get('generated_at', ''),
                    profile_source_end=self.meta.get('end', ''),
                    profile_stale=bool(self.meta.get('stale') or self.meta.get('refresh_failed')),
                    account_id=self.account_id, x_user_id=self.x_user_id,
                    preferred_tag=THEMES[preferred[2]] if len(preferred)>2 else '',
                    selected_tag=THEMES[matched] if matched else '', selected_tag_key=matched,
                    candidate_labels=data.get('raw_labels', []), candidate_tag_ids=data.get('themes', []), fallback_reason=reason,
                    weights=[{'key': k, 'name': TYPES[k], 'revenue_cents': v} for k,v in sorted(self.weights.items())],
                    conditional_weights=[dict(path=list(path), weights=values) for path,values in self.node_weights.items()
                                         if path in ((), (dtype,), (dtype,audience))])


def catalog_labels(source, dramas):
    """Exact-version classifications shared with income portraits."""
    ids = [d.content_id for d in dramas]
    if not ids:
        return {}
    factory = getattr(source, 'connection_factory', None)
    cursor_factory = getattr(source, '_streaming_cursor', None)
    if not callable(factory) or not callable(cursor_factory):
        return collect_classifications(source._stream_select, source.schema, ids)
    # Keep the existing read-only connection factory and its access checks, but
    # reuse one connection across the complete catalog instead of reconnecting
    # for each batch. Never truncate candidates: that would bias the hierarchy.
    with closing(factory()) as connection:
        def query(sql, params):
            with closing(cursor_factory(connection)) as cursor:
                cursor.execute(sql, tuple(params))
                while True:
                    rows = cursor.fetchmany(500)
                    if not rows:
                        break
                    for row in rows:
                        if not isinstance(row, Mapping):
                            raise ValueError('candidate_metadata_row_invalid')
                        yield dict(row)
        return collect_classifications(query, source.schema, ids)


def ranked_dramas(source, candidates, context, batch_size=40):
    """Yield the exact stable hierarchy order without reading unused labels.

    (0, 0, 0) is the best possible rank. Such candidates can be yielded in
    original order as soon as a batch is classified. Lower ranks wait until
    every candidate has been considered, so an absent preferred combination
    never causes premature fallback or skips a better candidate in a later batch.
    """
    deferred = []
    decision = context['decision']
    def rank(drama):
        return decision.rank(context['catalog'].get((drama.content_id, language(drama.language)), {}))
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start:start + batch_size]
        try:
            context['catalog'].update(catalog_labels(source, batch))
        except Exception:
            context['fallback'] = 'candidate_metadata_unavailable'
            # Already yielded candidates failed ordinary material admission.
            # Resume the old order for every remaining candidate, once only.
            yield from deferred
            yield from candidates[start:]
            return
        for drama in batch:
            if rank(drama) == (0, 0, 0):
                yield drama
            else:
                deferred.append(drama)
    yield from sorted(deferred, key=rank)


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
