"""Publish local projections from verified X data and confirmed Facebook receipts."""
import argparse
from contextlib import closing
import fcntl
import json
import os
import sqlite3
from . import data, facebook


def project_x(root):
    from features.x_material_effects import store, collector
    meta, path = store.current()
    with closing(store.connect(path)) as db:
        posts = [json.loads(row[0]) for row in db.execute('SELECT payload FROM posts')]
        # Read already verified local daily income; no platform requests here.
        copies = [dict(p) for p in posts]
        try:
            income = collector.revenue(copies,facebook.WAREHOUSE,daily=True)
        except (OSError,sqlite3.Error,ValueError) as exc:
            meta = dict(meta,window_income_error=type(exc).__name__)
        else:
            for post, enriched in zip(posts,copies):
                if '_window_income' in enriched:
                    post['_window_income'] = enriched['_window_income']
            meta = dict(meta,window_income_version=income['revenue_source_version'])
        return data.publish(root,'x',posts,meta)


def project_facebook(root, graph_calls):
    from features.social_analytics.warehouse_refresh import Reader, environment
    from .graph import sync
    posts, conflicts = facebook.discover()
    facebook.resolve_auto_tenants(posts)
    reader = Reader(environment('/root/drama_material_service/.env'))
    facebook.enrich(posts,reader)
    revenue = facebook.revenue(posts)
    try:
        metrics = sync(posts,root/'facebook-observations.sqlite3',max_calls=graph_calls)
    except Exception as exc:
        # Existing Page observations and verified revenue are independently usable.
        metrics = dict(metrics_upstream_error='fb_collection_'+type(exc).__name__)
        print(json.dumps(metrics),flush=True)
    stamp = data.utc()
    return data.publish(root,'facebook',posts.values(), dict(version=stamp,generated_at=stamp,
                        identity_conflicts=conflicts,timezone='Asia/Shanghai',currency='USD',**revenue,**metrics))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--channel', choices=(*data.CHANNELS,'all'), default='all')
    parser.add_argument('--graph-calls', type=int, default=160)
    args = parser.parse_args()
    root = data.root_path()
    root.mkdir(parents=True, exist_ok=True)
    failed = False
    for ch in data.channels(args.channel):
        with (root/(ch+'.lock')).open('a') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            try:
                meta = project_x(root) if ch=='x' else project_facebook(root,args.graph_calls)
                print(json.dumps({k:meta[k] for k in ('channel','version','post_count')}),flush=True)
            except Exception as exc:
                import traceback
                print(json.dumps(dict(channel=ch,error=type(exc).__name__,
                                      location=[(os.path.basename(f.filename),f.lineno) for f in traceback.extract_tb(exc.__traceback__)])),flush=True)
                failed = True
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
