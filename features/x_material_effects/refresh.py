"""One independently locked refresh; failed runs never replace serving data."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from . import collector, store
from .warehouse import EffectWriter
from features.social_analytics.warehouse_refresh import Reader,environment

def refresh(root=store.ROOT,*,limit=0,prepare_only=False):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    with (root/'refresh.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return dict(status='already_running')
        now=datetime.now(timezone.utc);version=now.strftime('%Y%m%dT%H%M%S%fZ');stage='discovery'
        try:
            env=environment('/root/drama_material_service/.env');reader=Reader(env)
            posts,conflicts=collector.discover();stage='metadata'
            collector.enrich(posts,reader);stage='revenue'
            revenue=collector.revenue(posts,'/mnt/data-disk/social-analytics-warehouse');stage='x_metrics'
            counters=collector.collect_metrics(posts,root/'observations.sqlite3',limit=limit)
            meta=dict(generated_at=datetime.now(timezone.utc).isoformat(),timezone='Asia/Shanghai',
                      mode='cumulative',identity_conflicts=conflicts,**revenue,**counters)
            if not prepare_only:
                stage='warehouse'
                with closing(EffectWriter(env)) as writer:meta=writer.publish(version,posts,meta,reader)
            stage='serving'
            manifest=store.publish(root,version,posts,meta)
            store.atomic_json(root/'status.json',dict(status='ok',attempted_at=meta['generated_at'],version=version))
            return dict(status='ok',version=version,posts=len(posts),materials=manifest['material_count'],**counters)
        except Exception as exc:
            code=type(exc).__name__
            store.atomic_json(root/'status.json',dict(status='error',stage=stage,error_code=code,attempted_at=now.isoformat()))
            raise RuntimeError('x_effect_refresh_failed:'+stage+':'+code) from None

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',default=store.ROOT);parser.add_argument('--limit',type=int,default=0);parser.add_argument('--prepare-only',action='store_true')
    args=parser.parse_args()
    if args.prepare_only and Path(args.root).resolve()==Path(store.ROOT):raise ValueError('isolated_preview_root_required')
    print(json.dumps(refresh(args.root,limit=args.limit,prepare_only=args.prepare_only)),flush=True)
