#!/usr/bin/env python3
"""Collect Post effects without calling any publishing or GPU API."""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from features.fb_auto_posts.effect_collector import GatedReader,MetaReader,collect
from features.fb_auto_posts.effects import write_effect
from scripts.fb_auto_post_metric_runner import single_flight


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--import-file')
    parser.add_argument('--max-posts',type=int,default=750)
    args=parser.parse_args()
    if not 1<=args.max_posts<=1000:
        parser.error('max-posts must be 1..1000')
    path=os.environ.get('FB_AUTO_POST_DB_PATH','/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3')
    with single_flight('/mnt/data-disk/fb-auto-post-publisher/post-effects.lock'):
        conn=sqlite3.connect('file:'+path+'?mode=rw',uri=True,timeout=15)
        conn.row_factory=sqlite3.Row
        try:
            if args.import_file:
                rows=json.loads(Path(args.import_file).read_text(encoding='utf-8'))
                if not isinstance(rows,list) or len(rows)>10000:
                    raise ValueError('feedback_import_size_invalid')
                with conn:
                    for row in rows:
                        write_effect(conn,row['task_id'],row,stage='baseline')
                result={'status':'imported','posts':len(rows)}
            else:
                graph=MetaReader(os.environ.get('FB_GRAPH_API_VERSION','v22.0'))
                try:
                    result=collect(conn,GatedReader(os.environ),graph,max_posts=args.max_posts)
                finally:
                    graph.session.close()
            print(json.dumps(result,sort_keys=True))
        finally:
            conn.close()


if __name__=='__main__':
    main()
