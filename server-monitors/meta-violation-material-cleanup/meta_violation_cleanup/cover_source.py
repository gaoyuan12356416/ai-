"""Read-only canonical material discovery and bounded cover enrichment."""
from collections import Counter
from datetime import datetime, timedelta
from contextlib import closing
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit

from .sql_source import SqlSource, SqlSourceError, _inside, _quote, _chunks, _ids
from .policy import product_scope, build_evidence, qualifying_policies, material_identity
from .runner import Runner
from .indexed_source import material_rows
from .current_contexts import current_contexts
from .runtime import CST, now

POLICY_VERSION = '20261028-cover-v1'


def cover_url(value):
    text = str(value or '').strip()
    try:
        parsed = urlsplit(text)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
                parsed.username or parsed.password or any(ord(c) < 32 for c in text)):
            return ''
        # Existing production CDN alias, not a change to the cover path/query.
        if parsed.hostname == 'static.mydramawave.com':
            text = text.replace('://static.mydramawave.com', '://static-v1.mydramawave.com', 1)
        return text
    except ValueError:
        return ''


def select_cover(metadata, root_id, rows):
    """All materials in one simple creative must resolve to the same drama."""
    product = {'1479':'dramawave','3302':'hotdrama'}.get(str(root_id))
    if not product or any(str(m.get('data_source')) != '6' or
                          str(m.get('product') or '').strip().casefold() != product for m in metadata):
        return {'error': 'cover_material_product_or_type_mismatch'}
    keys = {(str(m.get('data_source_id') or '').strip(),
             str(m.get('language') or '').strip().casefold()) for m in metadata}
    if len(keys) != 1 or not all(k[0] and k[1] for k in keys):
        return {'error': 'cover_drama_identity_ambiguous'}
    content, language = next(iter(keys))
    matches = set()
    for row in rows:
        if (str(row.get('app_id')) != str(root_id) or str(row.get('content_id')) != content or
                str(row.get('language') or '').strip().casefold() != language):
            continue
        raw = str(row.get('cover') or '').strip()
        if not raw:
            continue
        url = cover_url(raw)
        if not url:
            return {'error': 'cover_url_invalid'}
        matches.add(url)
    if len(matches) != 1:
        return {'error': 'cover_missing' if not matches else 'cover_ambiguous'}
    return {'cover_url': next(iter(matches)), 'cover_version': '',
            'content_id': content, 'language': language}


class CoverSource(SqlSource):
    def __init__(self, config):
        super().__init__(config)
        self.shared_ledger = Path(config['shared_ledger'])

    def confirmed_deleted_ads(self):
        # Positive historical receipts are permanent; do this before expensive
        # current-lineage/credential lookups and Graph reads for the first scan.
        with closing(sqlite3.connect(self.shared_ledger.resolve().as_uri()+'?mode=ro', uri=True)) as db:
            return {r[0][3:] for r in db.execute(
                "SELECT object_key FROM fb_asset_delete_v2_receipts WHERE object_key LIKE 'ad:%'")}

    def material_metadata(self, ids):
        result = {}
        with self._session() as session:
            for part in _chunks(_ids(ids), 1000):
                sql = self._select((('id', 'CAST(id AS CHAR)'),
                                    ('data_source_id', 'data_source_id'),
                                    ('data_source', 'data_source'),
                                    ('language', 'language'), ('product', 'product')),
                                   'ads_custom_source FORCE INDEX(PRIMARY)',
                                   'WHERE id IN ' + _inside(part) + ' ORDER BY id LIMIT %d' % (len(part) + 1))
                for row in session.query(sql):
                    mid = str(row.get('id'))
                    if mid not in part or mid in result:
                        raise SqlSourceError('cover_material_result_invalid')
                    result[mid] = row
        return result

    def drama_covers(self, wanted):
        result = []
        by_root = {}
        for root, content, language in wanted:
            by_root.setdefault(root, set()).add(content)
        with self._session() as session:
            for root, contents in sorted(by_root.items()):
                for part in _chunks(sorted(contents), 20):
                    sql = ('SELECT DISTINCT JSON_OBJECT(\'app_id\',CAST(app_id AS CHAR),'
                           "'content_id',content_id,'language',language,'cover',TRIM(COALESCE(cover,''))) "
                           'FROM `%s`.ads_drama_resource FORCE INDEX(content_id) '
                           'WHERE app_id=%s AND content_id IN (%s) LIMIT 1001') % (
                               self.schema, _quote(root), ','.join(_quote(x) for x in part))
                    rows = session.query(sql)
                    if len(rows) > 1000:
                        raise SqlSourceError('cover_result_incomplete')
                    result.extend(rows)
        return result

    def prepare_candidates(self, tasks):
        if not tasks:
            return {}
        candidates = [dict(t.get('candidate', t), id=t['id']) for t in tasks]
        for c in candidates:
            c['creative_id'] = c.get('old_creative_id', c.get('creative_id'))
        result = {}
        for part in _chunks(candidates, 64):
            contexts = current_contexts(self, part)
            mids = sorted({str(mid) for c in part for mid in c.get('material_ids', [])})
            metadata = self.material_metadata(mids) if mids else {}
            wanted = {(str(c['root_id']), str(metadata[mid].get('data_source_id') or '').strip(),
                       str(metadata[mid].get('language') or '').strip().casefold())
                      for c in part for mid in map(str, c.get('material_ids', [])) if mid in metadata}
            covers = self.drama_covers({k for k in wanted if k[1] and k[2]}) if wanted else []
            for c in part:
                out = dict(c, validated=False)
                ctx = contexts[(str(c['ad_id']), str(c['creative_id']))]
                identity = material_identity(ctx.get('rows'), ctx.get('sources'), ctx.get('scope'), c['creative_id'])
                matching = [r for r in ctx.get('rows', []) if str(r.get('creative_id')) == c['creative_id']]
                accounts = {str(r.get('ad_account_id') or '').removeprefix('act_') for r in matching}
                error = ctx.get('error') or identity.get('reason')
                if not error and (identity.get('root_id') != c['root_id'] or
                        set(identity.get('material_ids', [])) != set(c.get('material_ids', [])) or
                        accounts != {c['account_id']} or ctx.get('credential_users') != [c['user_id']]):
                    error = 'current_context_changed'
                if not error and any(str(mid) not in metadata for mid in c.get('material_ids', [])):
                    error = 'cover_material_missing'
                if not error:
                    resolved = select_cover([metadata[str(mid)] for mid in c['material_ids']], c['root_id'], covers)
                    error = resolved.get('error')
                    out.update(resolved)
                out.update(error=error or '', validated=not bool(error), identity=identity,
                           old_creative_id=c['creative_id'])
                result[str(c['id'])] = out
        return result


def scan(source, store, batch_id, audit=None, stop=lambda: False):
    """Complete source scan; no Graph reads or media writes."""
    began = time.monotonic()
    cutoff = datetime.now(CST)
    start, end = (cutoff - timedelta(days=3)).isoformat(), cutoff.isoformat()
    meta = {'policy_version': POLICY_VERSION, 'window_start': start, 'window_end': end}
    store.begin_batch(batch_id, meta)
    scope = product_scope(source.products())
    runner = Runner({}, source, None, None, None, None, stop=stop)
    violations = source.violations(start, end)
    rows = [r for r in violations if qualifying_policies(r.get('issues_info'))]
    lineage, sources = runner.lineage_data(sorted({str(r['ad_id']) for r in rows}), rows, scope)
    evidence = build_evidence(rows, lineage, sources, scope, start, end)
    qualified = {'materials': {mid: dict(e, window_start=start, window_end=end, triggered_at=now())
                               for mid, e in evidence['materials'].items() if e['count'] >= 3}}
    new_materials = store.add_triggers(qualified)
    triggered = store.triggered_materials()
    progress = lambda **kw: audit.update(batch_id, **kw) if audit else None
    found, lookup = material_rows(source, set(triggered), scope, progress, stop)
    existing = store.known_keys()
    deleted = source.confirmed_deleted_ads()
    unseen_ids = sorted({str(r['ad_id']) for r in found if
                         str(r['ad_id']) not in deleted and
                         (str(r.get('ad_account_id') or '').removeprefix('act_'),
                          str(r['ad_id']), str(r.get('creative_id'))) not in existing})
    counts = Counter()
    counts['historical_deleted_ads_skipped'] = len({str(r['ad_id']) for r in found if str(r['ad_id']) in deleted})
    for part in _chunks(unseen_ids, 1900):
        if stop():
            raise InterruptedError('scan_stopped')
        with source.snapshot():
            lineages, sources = runner.lineage_data(part, scope=scope)
            route_rows = [r for values in lineages.values() for r in values if str(r.get('product')) in scope]
            routes = source.selected_credentials(route_rows)
        for aid in part:
            all_rows = lineages.get(aid, [])
            for cid in sorted({str(r.get('creative_id') or '') for r in all_rows}):
                identity = material_identity(all_rows, sources, scope, cid)
                if identity['reason']:
                    counts['identity_skipped'] += 1
                    continue
                matching_materials = [mid for mid in identity['material_ids'] if mid in triggered and
                                      identity['root_id'] in triggered[mid].get('root_ids', [triggered[mid].get('root_id')])]
                if not matching_materials:
                    continue
                matched = [r for r in all_rows if str(r.get('creative_id')) == cid and str(r.get('product')) in scope]
                selected = [routes.get(str(r['id']), {}) for r in matched]
                users = {x.get('user_id') for x in selected if x.get('user_id') and not x.get('error')}
                accounts = {str(r.get('ad_account_id') or '').removeprefix('act_') for r in matched}
                if len(users) != 1 or len(accounts) != 1 or any(x.get('error') or not x.get('user_id') for x in selected):
                    counts['route_skipped'] += 1
                    continue
                candidate = dict(identity, ad_id=aid, old_creative_id=cid,
                                 account_id=next(iter(accounts)), user_id=next(iter(users)),
                                 triggered_material_ids=matching_materials, policy_version=POLICY_VERSION)
                if store.enqueue(batch_id, candidate):
                    counts['new_ads'] += 1
    # Waiting tasks keep their original trigger; this new hour supplies a fresh execution batch.
    for row in store.due_waiting(limit=1000000):
        if store.requeue_waiting(row['id'], batch_id):
            counts['review_rechecks'] += 1
    summary = dict(counts, new_materials=new_materials, triggered_materials=len(triggered),
                   violation_rows=len(violations), policy_rows=len(rows), lookup=lookup,
                   seconds=round(time.monotonic() - began, 3), **meta)
    store.seal_batch(batch_id, summary)
    if audit:
        audit.artifact(batch_id, 'cover-scan.json', {'summary': summary, 'evidence': evidence})
        audit.update(batch_id, stage='queued', scan_summary=summary)
    return summary
