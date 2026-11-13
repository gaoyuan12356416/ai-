"""Hourly incremental discovery; known authoritative labels never expire."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
import json
import os
from pathlib import Path
import time

from . import store
from .client import fetch


def priority_ids():
    ids = set()
    root = Path('/mnt/data-disk/x-channel-tag-revenue')
    if (root/'current.json').exists():
        manifest = json.loads((root/'current.json').read_text())
        data = json.loads((root/manifest['file']).read_text())
        for profile in data['accounts'].values():
            for audience in profile.get('audiences', []):
                for theme in audience.get('children', []):
                    ids.update(r['content_id'] for r in theme.get('dramas', []))
    root = Path('/mnt/data-disk/fb-page-portraits')
    if (root/'current.json').exists():
        manifest = json.loads((root/'current.json').read_text())
        data = json.loads((root/(manifest['version']+'.input.json')).read_text())
        ids.update(str(r['content_id']) for kind in ('paid', 'social') for r in data[kind] if r.get('content_id'))
    return ids


def discover(db_path, reader, pages=200):
    with closing(store.connect(db_path, True)) as db:
        def get(key):
            row=db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
            return int(row[0]) if row else None
        def save(key,value):
            with db:db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',(key,str(value)))
        cursor=get('drama_info_cursor')
        if cursor is None:
            # Pin a head for new arrivals independently of the historical backlog.
            rows=reader.query(["SELECT HEX(JSON_OBJECT('id',id)) FROM kunlunads_dev.ads_drama_info "
                               "WHERE app_id=1479 ORDER BY id DESC LIMIT 1"],timeout=40)
            cursor=int(rows[0]['id']) if rows else 0
            with db:
                db.executemany('INSERT OR REPLACE INTO metadata VALUES(?,?)',
                    [('drama_info_cursor',str(cursor)),('drama_info_backfill',str(cursor+1))])
        total = 0
        for _ in range(pages):
            rows = reader.query(["SELECT HEX(JSON_OBJECT('id',id,'content_id',content_id)) "
                                 "FROM kunlunads_dev.ads_drama_info WHERE app_id=1479 AND id>%d "
                                 "ORDER BY id LIMIT 1000" % cursor], timeout=40)
            if not rows:
                break
            store.enqueue([r['content_id'] for r in rows], db_path,priority=10)
            cursor = max(int(r['id']) for r in rows)
            save('drama_info_cursor',cursor)
            total += len(rows)
            if len(rows) < 1000:
                break
        backfill=get('drama_info_backfill') or 0
        for _ in range(min(pages,10)):
            if not backfill:break
            rows=reader.query(["SELECT HEX(JSON_OBJECT('id',id,'content_id',content_id)) "
                               "FROM kunlunads_dev.ads_drama_info WHERE app_id=1479 AND id<%d "
                               "ORDER BY id DESC LIMIT 1000" % backfill],timeout=40)
            store.enqueue([r['content_id'] for r in rows],db_path)
            backfill=min(int(r['id']) for r in rows) if len(rows)==1000 else 0
            save('drama_info_backfill',backfill)
            total+=len(rows)
    return total


def fill(db_path, limit=1000, workers=4, fetcher=fetch):
    with closing(store.connect(db_path)) as db:
        ids = [r[0] for r in db.execute('SELECT content_id FROM pending ORDER BY priority DESC,attempts,updated_at,content_id LIMIT ?', (limit,))]
    success, errors = 0, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(fetcher, cid): cid for cid in ids}
        for future in as_completed(jobs):
            cid = jobs[future]
            try:
                store.save(future.result(), db_path)
                success += 1
            except Exception as exc:
                errors += 1
                error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                if error not in ('exact_drama_not_found', 'audience_contract_missing', 'language_missing', 'upstream_business_error'):
                    error = type(exc).__name__
                with closing(store.connect(db_path, True)) as db, db:
                    db.execute('UPDATE pending SET attempts=attempts+1,last_error=?,updated_at=? WHERE app_id=1479 AND content_id=?', (error, store.now(), cid))
            if (success+errors) % 100 == 0:
                print(json.dumps(dict(event='audience_progress',done=success+errors,total=len(ids),success=success,errors=errors)),flush=True)
    return dict(success=success, errors=errors, **store.counts(db_path))


def main():
    import fcntl
    p = argparse.ArgumentParser()
    p.add_argument('--db', default=store.DEFAULT_DB)
    p.add_argument('--env', default='/root/drama_material_service/.env')
    p.add_argument('--max-items', type=int, default=1000)
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--priority-only', action='store_true')
    p.add_argument('--refresh-id', action='append', default=[])
    args = p.parse_args()
    os.umask(0o022)
    store.initialize(args.db)
    with open(str(args.db)+'.sync.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"status":"already_running"}')
            return
        if args.refresh_id:
            for cid in args.refresh_id:
                store.save(fetch(cid), args.db, replace=True)
            print(json.dumps(store.counts(args.db)))
            return
        ids = priority_ids()
        store.enqueue(ids, args.db, priority=100)
        if not args.priority_only:
            from features.social_analytics.warehouse_refresh import Reader, environment
            print(json.dumps(dict(event='discovered',rows=discover(args.db, Reader(environment(args.env))))),flush=True)
        result = fill(args.db, args.max_items, max(1,min(args.workers,8)))
        result.update(at=store.now(), status='partial' if result['errors'] else 'ok')
        with closing(store.connect(args.db, True)) as db, db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES('last_sync',?)", (json.dumps(result),))
        print(json.dumps(result),flush=True)
        if not args.priority_only:
            # Retry missing rank display metadata without refreshing stored audiences.
            try:
                from features.drama_pay_rank.metadata import refresh as refresh_rank_metadata
                print(json.dumps(dict(event='rank_details',**refresh_rank_metadata(Reader(environment(args.env))))),flush=True)
            except Exception as exc:
                print(json.dumps(dict(event='rank_details_pending',error=type(exc).__name__)),flush=True)


if __name__ == '__main__':
    main()
