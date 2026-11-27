"""Daily Facebook coverage and a stable 23:00-24:00 Beijing closing pass."""
import argparse
import json
from . import data, graph


def candidates(root):
    posts = {}
    with data.reading(('facebook',),root) as (db,_):
        for key,page,published,url in db.execute('SELECT post_id,account_id,published_at,post_url FROM facebook.posts'):
            prefix,separator,obj = key.partition('_')
            if prefix!=page or not separator or not obj.isdigit():
                continue
            base = 'https://www.facebook.com/'+page+'/posts/'
            post = (url or '').removeprefix(base) if (url or '').startswith(base) else ''
            posts[key] = dict(post_id=key,account_id=page,object_id=obj,published_at=published,
                              native=page+'_'+post if post.isdigit() else '')
    return posts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-calls',type=int,default=400)
    parser.add_argument('--max-posts',type=int,default=6000)
    parser.add_argument('--max-seconds',type=int,default=240)
    parser.add_argument('--interval',type=float,default=0.3)
    args = parser.parse_args()
    if min(args.max_calls,args.max_posts)<1:
        parser.error('limits must be positive')
    root = data.root_path()
    from datetime import datetime,timezone
    from . import facebook,sampling
    # Newly published receipts must also be eligible in the closing hour.
    posts = facebook.discover()[0] if sampling.closing_start(datetime.now(timezone.utc)) else candidates(root)
    stats = graph.sync(posts,root/'facebook-observations.sqlite3',
                       max_calls=args.max_calls,max_posts=args.max_posts,history=True,coverage=True,
                       max_seconds=args.max_seconds,interval=args.interval)
    print(json.dumps(stats),flush=True)
    return int(bool(stats.get('metrics_upstream_error')) and
               stats['metrics_upstream_error'] not in graph.STOP_CODES)


if __name__=='__main__':
    raise SystemExit(main())
