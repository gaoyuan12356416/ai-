"""Scoped rollout while independent read-only recovery previews keep running."""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3

import deploy_meta_asset_recovery as rollout
from deploy_meta_delete_throughput import require_api_stopped


BASE = 'd7e53d5'
FILES = ['features/fb_ad_asset_delete/'+name+'.py' for name in ('graph','service','source','store')]
FILES += ['static/fb-post-ad-delete.html','static/fb-post-ad-delete.js']


def no_writes():
    path = rollout.DISK/'fb-ad-asset-delete/tasks.sqlite3'
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as conn:
        for table in ('fb_asset_delete_v2_runs','fb_asset_delete_v2_rechecks'):
            assert not conn.execute('SELECT 1 FROM '+table+" WHERE status='running' LIMIT 1").fetchone(), 'active execution or recheck'
    # Preview workers send GET only. Main-process previews become interrupted
    # through the existing owner-recovery check; independent previews survive.


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('prepare','apply','rollback'))
    parser.add_argument('path',type=Path)
    args=parser.parse_args()
    rollout.BASE,rollout.BASELINE_HASHES,rollout.FILES,rollout.idle=BASE,{},FILES,no_writes
    if args.mode=='prepare':
        rollout.prepare(args.path)
    else:
        require_api_stopped()
        rollout.switch(args.path,args.mode=='rollback')


if __name__=='__main__':
    main()
