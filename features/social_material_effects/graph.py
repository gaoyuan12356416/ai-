"""Incremental GET-only enrichment, independent of the Facebook publishing queue."""
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json
import os
import sqlite3
import time

FIELDS = ('id,reactions.type(LIKE).limit(0).summary(total_count),comments.limit(0).summary(total_count),'
          'shares,insights.metric(post_media_view).period(lifetime)')


def count(value):
    return value if type(value) is int and value >= 0 else None


def extract(row):
    views = next((v['values'][0].get('value') for v in (row.get('insights') or {}).get('data', [])
                  if v.get('name')=='post_media_view' and v.get('values')), None)
    return {key:value for key,value in {
        'views':count(views), 'likes':count((row.get('reactions') or {}).get('summary',{}).get('total_count')),
        'comments':count((row.get('comments') or {}).get('summary',{}).get('total_count')),
        'shares':count((row.get('shares') or {}).get('count'))}.items() if value is not None}


def sync(posts, cache, *, max_calls=160, max_posts=1500):
    from features.fb_auto_posts.effect_collector import FeedbackError, GatedReader, MetaReader
    graph = MetaReader(os.environ.get('FB_GRAPH_API_VERSION','v22.0'), max_calls=max_calls)
    now = int(time.time())
    with closing(sqlite3.connect(cache)) as db, closing(graph.session):
        db.execute('CREATE TABLE IF NOT EXISTS observation(id TEXT PRIMARY KEY,native TEXT NOT NULL,'
                   'metrics TEXT NOT NULL,observed TEXT,retry INTEGER NOT NULL,error TEXT NOT NULL)')
        saved = {r[0]:r for r in db.execute('SELECT * FROM observation')}
        due = [p for k,p in posts.items() if k not in saved or saved[k][4]<=now]
        due.sort(key=lambda p:(p['post_id'] in saved, (saved.get(p['post_id']) or (0,0,0,0,0))[4],
                               -datetime.fromisoformat(p['published_at'].replace('Z','+00:00')).timestamp()))
        pending = defaultdict(list)
        for p in due:
            group = pending[p['account_id']]
            if len(group)<25:
                group.append(p)
        by_page = {}
        slots = max_posts
        for page,group in pending.items():
            by_page[page] = group[:slots]
            slots -= len(by_page[page])
            if not slots:
                break
        status = ''
        try:
            reader = GatedReader(os.environ)
            pages = list(by_page)
            credentials = {}
            for index,(page, group) in enumerate(by_page.items()):
                if index % 40 == 0:
                    credentials = reader.credentials(pages[index:index+40])
                for i in range(0,len(group),25):
                    if graph.calls >= max_calls:
                        break
                    batch = group[i:i+25]
                    natives = {p['post_id']:(p['native'] or (saved.get(p['post_id']) or (0,''))[1]) for p in batch}
                    remaining = {p['post_id']:p for p in batch}
                    error = 'fb_page_access_unavailable'
                    def save(key, metrics, observed, retry, code):
                        db.execute('INSERT OR REPLACE INTO observation VALUES(?,?,?,?,?,?)',
                                   (key,natives[key],json.dumps(metrics),observed,retry,code))
                    for cred in credentials.get(page,[])[:3]:
                        if not remaining or graph.calls >= max_calls:
                            break
                        try:
                            missing = [p for key,p in remaining.items() if not natives[key]]
                            if missing:
                                rows = graph.get(dict(ids=','.join(p['object_id'] for p in missing),
                                                      fields='id,post_id'),cred['value'])
                                for p in missing:
                                    row = rows.get(p['object_id'], {})
                                    native = str(row.get('post_id') or '')
                                    if native.isdigit():
                                        native = page + '_' + native
                                    if str(row.get('id')) == p['object_id'] and native.startswith(page+'_') and native[len(page)+1:].isdigit():
                                        natives[p['post_id']] = native
                            native_ids = [natives[key] for key in remaining if natives[key]]
                            if not native_ids:
                                error = 'fb_post_id_unavailable'
                                continue
                            result = graph.get(dict(ids=','.join(native_ids),fields=FIELDS),cred['value'])
                            for key,p in list(remaining.items()):
                                row = result.get(natives[key], {})
                                values = extract(row) if row.get('id') == natives[key] else {}
                                if not values:
                                    error = 'fb_post_metrics_unavailable'
                                    continue
                                observed = datetime.now(timezone.utc).isoformat(timespec='seconds')
                                age = now-datetime.fromisoformat(p['published_at'].replace('Z','+00:00')).timestamp()
                                interval = 3600 if age<7*86400 else (21600 if age<30*86400 else 86400)
                                save(key,values,observed,now+interval,'')
                                del remaining[key]
                        except FeedbackError as exc:
                            error = str(exc)
                            if error in ('feedback_request_budget_reached','feedback_graph_usage_high'):
                                status = error
                                break
                    for key in remaining:
                        old = saved.get(key)
                        save(key,json.loads(old[2]) if old else {},old[3] if old else None,now+6*3600,error)
                    db.commit()
                    if status:
                        break
                if status or graph.calls >= max_calls:
                    break
        except FeedbackError as exc:
            status = str(exc)
        for key,native,metrics,stamp,_,error in db.execute('SELECT * FROM observation'):
            p = posts.get(key)
            if p is None:
                continue
            values = json.loads(metrics)
            if p.get('views') is not None and (p.get('metrics_at') or '') > (stamp or ''):
                values.pop('views',None)
            p.update(values)
            p['metrics_at'] = max(filter(None,(stamp,p.get('metrics_at'))),default=None)
            if native:
                p['post_url'] = 'https://www.facebook.com/'+p['account_id']+'/posts/'+native.split('_')[-1]
            p['error_code'] = error
        return dict(graph_calls=graph.calls,graph_due=len(due),metrics_upstream_error=status)
