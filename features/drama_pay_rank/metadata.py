"""Persist rank display metadata; web requests never query the source database."""
from collections import defaultdict
from contextlib import closing
import json
import sqlite3
from urllib.parse import urlencode
from urllib.request import Request

from features.drama_audience.store import language, now
from features.drama_audience.client import read_json, URL
from features.x_tag_revenue.taxonomy import classify, raw_labels
from .core import ROOT, database


def index(resources, infos):
    grouped, identities = defaultdict(list), defaultdict(list)
    for rows, target in ((resources, grouped), (infos, identities)):
        for row in rows:
            if str(row.get('app_id')) == '1479':
                target[(str(row['content_id']), language(row.get('language')))].append(row)
    result = {}
    for key in grouped.keys() | identities.keys():
        rows = grouped[key]
        codes = {str(r.get('series_code') or '').strip() for r in rows + identities[key]} - {''}
        conflict = len(codes) > 1
        types = {str(r['resource_type_v2']).strip() for r in rows + identities[key]
                 if r.get('resource_type_v2') is not None and str(r['resource_type_v2']).strip()}
        raw = list(dict.fromkeys(tag for r in rows for tag in raw_labels(r.get('labels')))) if not conflict else []
        classification = classify(raw)
        result[key] = dict(resource_id=next(iter(codes)) if len(codes) == 1 else '',
                           resource_type_v2=next(iter(types)) if len(types) == 1 and not conflict else '',
                           resource_type_status='conflict' if conflict or len(types) > 1 else 'matched' if types else 'missing',
                           labels=raw, label_names=list(classification['semantic_tags'].values()),
                           secondary_labels=classification['theme_names'] if rows and not conflict else [],
                           status='conflict' if conflict else 'matched' if rows and codes else 'partial')
    return result


def read(root=ROOT):
    try:
        with closing(database(root)) as db:
            return {(r[0], r[1]): json.loads(r[2]) for r in db.execute(
                'SELECT content_id,language,payload FROM rank_metadata WHERE app_id=1479')}
    except sqlite3.OperationalError:
        return {}


def resolve_details(cid, data):
    if data.get('code') != 200:
        raise ValueError('upstream_business_error')
    resources = []
    for row in (data.get('data') or {}).get('list', []):
        if row.get('app_id') != 'com.dramawave.app' or row.get('id') != cid:
            continue
        if not isinstance(row.get('language'), list) or not isinstance(row.get('labels'), (str, list)):
            continue
        for lang in row['language']:
            if lang:
                resources.append(dict(app_id=1479, content_id=cid, language=lang,
                                      series_code=row.get('resource_id'), labels=row['labels'],
                                      resource_type_v2=row.get('resource_type_v2')))
    return index(resources, [])


def fetch_details(cid):
    return resolve_details(cid, read_json(Request(URL+'?'+urlencode({'content_id':cid}))))


def project_types(root=ROOT):
    """Add cached display fields to rank rows; retain provider facts and timestamps.

    The serving reader already passes through snapshot row fields. This additive
    projection activates the column without restarting the shared publishing API.
    """
    changed = 0
    with closing(database(root, True)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        details = {(r[0], r[1]): json.loads(r[2]) for r in db.execute(
            'SELECT content_id,language,payload FROM rank_metadata WHERE app_id=1479')}
        for day, payload in db.execute('SELECT date,payload FROM snapshots').fetchall():
            data = json.loads(payload)
            for row in data['list']:
                detail = details.get((row['series_key'], language(row['language_code'])), {})
                row.update(resource_type_v2=detail.get('resource_type_v2', ''),
                           resource_type_status=detail.get('resource_type_status', 'missing'))
            updated = json.dumps(data, ensure_ascii=False)
            if updated != payload:
                db.execute('UPDATE snapshots SET payload=? WHERE date=?', (updated, day))
                changed += 1
    return changed


def refresh(reader, root=ROOT, fetcher=fetch_details, refresh_day=''):
    with closing(database(root, True)) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS rank_metadata (
            app_id INTEGER NOT NULL, content_id TEXT NOT NULL COLLATE BINARY,
            language TEXT NOT NULL, payload TEXT NOT NULL, fetched_at TEXT NOT NULL,
            PRIMARY KEY(app_id,content_id,language))''')
        wanted = {(r['series_key'], language(r['language_code']))
                  for snapshot in db.execute('SELECT payload FROM snapshots')
                  for r in json.loads(snapshot[0])['list']}
        current = {(r['series_key'], language(r['language_code']))
                   for snapshot in db.execute('SELECT payload FROM snapshots WHERE date=?', (refresh_day,))
                   for r in json.loads(snapshot[0])['list']}
    known = read(root)
    missing = {key for key in wanted if key in current or key not in known
               or known[key]['status'] == 'partial' or 'resource_type_v2' not in known[key]}
    ids = sorted({cid for cid, _ in missing})
    saved = 0
    for offset in range(0, len(ids), 50):
        batch_ids = set(ids[offset:offset+50])
        values = ','.join('0x' + cid.encode('utf-8').hex() for cid in sorted(batch_ids))
        statements = []
        for table, hint, extra in [('ads_drama_resource', 'content_id', ",'labels',labels"),
                                   ('ads_drama_info', 'ac', ",'resource_type_v2',resource_type_v2")]:
            statements.append("SELECT DISTINCT HEX(JSON_OBJECT('kind','" + table + "','app_id',app_id,"
                              "'content_id',content_id,'language',language,'series_code',series_code" + extra + ")) "
                              "FROM kunlunads_dev." + table + " FORCE INDEX(" + hint + ") "
                              "WHERE app_id=1479 AND content_id IN (" + values + ") LIMIT 50001")
        rows = reader.query(statements, timeout=60)
        resources = [r for r in rows if r['kind'] == 'ads_drama_resource']
        infos = [r for r in rows if r['kind'] == 'ads_drama_info']
        if max(len(resources), len(infos)) >= 50001:
            raise ValueError('rank_metadata_capacity_exceeded')
        with closing(database(root, True)) as db, db:
            for key, value in index(resources, infos).items():
                if key not in missing or key[0] not in batch_ids:
                    continue
                db.execute('INSERT OR REPLACE INTO rank_metadata VALUES(1479,?,?,?,?)',
                           (*key, json.dumps(value, ensure_ascii=False), now()))
                saved += 1
        print(json.dumps(dict(event='rank_metadata_progress',processed=min(offset+50,len(ids)),total=len(ids),saved=saved)),flush=True)
    # Some newly listed language versions precede the SQL material catalog.
    # Fill only those gaps from the exact product/content/language API row.
    latest = read(root)
    pending = {key for key in missing if key not in latest or latest[key]['status'] == 'partial'
               or latest[key].get('resource_type_status') == 'missing'}
    for cid in sorted({key[0] for key in pending}):
        try:
            details = fetcher(cid)
            with closing(database(root, True)) as db, db:
                for key, value in details.items():
                    if key in pending and key[0] == cid and value['status'] != 'partial':
                        # An API gap fill must not discard an exact SQL type if
                        # this optional field is absent from the API response.
                        if value.get('resource_type_status') == 'missing' and latest.get(key, {}).get('resource_type_v2'):
                            value.update(resource_type_v2=latest[key]['resource_type_v2'],
                                         resource_type_status=latest[key]['resource_type_status'])
                        db.execute('INSERT OR REPLACE INTO rank_metadata VALUES(1479,?,?,?,?)',
                                   (*key, json.dumps(value, ensure_ascii=False), now()))
                        if value.get('resource_type_status') != 'missing':
                            pending.remove(key)
        except Exception as exc:
            print(json.dumps(dict(event='rank_metadata_api_pending',error=type(exc).__name__)),flush=True)
    return dict(requested=len(missing), saved=len(missing)-len(pending), unresolved=len(pending),
                projected_snapshots=project_types(root))


def main():
    import argparse
    from features.social_analytics.warehouse_refresh import Reader, environment
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh-day', default='')
    args = parser.parse_args()
    print(json.dumps(refresh(Reader(environment('/root/drama_material_service/.env')),
                             refresh_day=args.refresh_day)), flush=True)


if __name__ == '__main__':
    main()
