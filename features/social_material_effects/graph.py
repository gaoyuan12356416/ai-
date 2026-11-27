"""GET-only engagement reads shared by the hourly report and the historical collector."""
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import sqlite3
import time

from features.fb_auto_posts.effect_collector import FeedbackError, GatedReader, MetaReader
from . import sampling, window

FIELDS = ('id,reactions.type(LIKE).limit(0).summary(total_count),comments.limit(0).summary(total_count),'
          'shares,insights.metric(post_media_view).period(lifetime)')
RATE_CODES = {'feedback_graph_'+str(code) for code in (4,17,32,613,429,80001)}
STOP_CODES = RATE_CODES | {'feedback_graph_usage_high','feedback_request_budget_reached'}


def count(value):
    return value if type(value) is int and value >= 0 else None


def extract(row):
    views = next((v['values'][0].get('value') for v in (row.get('insights') or {}).get('data', [])
                  if v.get('name')=='post_media_view' and v.get('values')), None)
    return {key:value for key,value in {
        'views':count(views), 'likes':count((row.get('reactions') or {}).get('summary',{}).get('total_count')),
        'comments':count((row.get('comments') or {}).get('summary',{}).get('total_count')),
        'shares':count((row.get('shares') or {}).get('count'))}.items() if value is not None}


class Usage:
    def __init__(self):
        self.peak = 0
        self.wait = 0
        self.http_status = 0

    def response(self,response,*args,**kwargs):
        self.http_status = response.status_code
        def visit(value):
            if isinstance(value,dict):
                for key,item in value.items():
                    if key in ('call_count','total_cputime','total_time') and isinstance(item,(int,float)):
                        self.peak = max(self.peak,item)
                    elif key=='estimated_time_to_regain_access' and isinstance(item,(int,float)):
                        self.wait = max(self.wait,int(item*60))
                    else:
                        visit(item)
            elif isinstance(value,list):
                for item in value:
                    visit(item)
        for key in ('x-app-usage','x-page-usage','x-business-use-case-usage'):
            try:
                visit(json.loads(response.headers.get(key,'{}')))
            except ValueError:
                pass
        retry = response.headers.get('Retry-After','')
        if retry.isdigit():
            self.wait = max(self.wait,int(retry))
        elif retry:
            try:
                self.wait = max(self.wait,int(parsedate_to_datetime(retry).timestamp()-time.time()))
            except (TypeError,ValueError,OverflowError):
                pass


def prepare(db):
    db.execute('CREATE TABLE IF NOT EXISTS observation(id TEXT PRIMARY KEY,native TEXT NOT NULL,'
               'metrics TEXT NOT NULL,observed TEXT,retry INTEGER NOT NULL,error TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS collection_state(key TEXT PRIMARY KEY,value INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS collection_run(id INTEGER PRIMARY KEY,mode TEXT NOT NULL,'
               'started INTEGER NOT NULL,finished INTEGER NOT NULL,stats TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS page_retry(page TEXT PRIMARY KEY,retry INTEGER NOT NULL,error TEXT NOT NULL)')
    sampling.prepare(db)
    db.commit()


def plan(posts,saved,history,now,max_posts,coverage=False):
    close = sampling.closing_start(datetime.fromtimestamp(now,timezone.utc)) if coverage else None
    if coverage:
        today = datetime.fromtimestamp(now,timezone.utc).astimezone(window.BJ).replace(hour=0,minute=0,second=0,microsecond=0)
        boundary = close or today
        due = [p for key,p in posts.items() if key not in saved or (
            (not saved[key][5] or saved[key][4]<=now) and
            (window.instant(saved[key][3]) is None or window.instant(saved[key][3])<boundary))]
        # Stable Page order keeps successive closing samples at similar times.
        due.sort(key=lambda p:(p['account_id'],p['post_id']) if close else
                 ((saved.get(p['post_id']) or ('','','',''))[3] or '',p['account_id'],p['post_id']))
    elif history:
        due = [p for key,p in posts.items() if key not in saved]
        due.sort(key=lambda p:(p['published_at'],p['post_id']))
    else:
        due = [p for key,p in posts.items() if key not in saved or saved[key][4]<=now]
        due.sort(key=lambda p:(p['post_id'] in saved,(saved.get(p['post_id']) or (0,0,0,0,0))[4],
                               -datetime.fromisoformat(p['published_at'].replace('Z','+00:00')).timestamp()))
    pages = defaultdict(list)
    for p in due:
        pages[p['account_id']].append(p)
    selected = {}
    for page,rows in pages.items():
        # A historical Page lookup can serve several full groups of posts.
        size = min(max_posts,len(rows)) if history or coverage else min(max_posts,len(rows),25)
        selected[page] = rows[:size]
        max_posts -= size
        if max_posts==0:
            break
    return len(due),selected


def engage(values):
    return 'likes' in values and 'comments' in values


def fetch_batch(batch,page,credentials,saved,graph,usage):
    natives = {p['post_id']:p.get('native') or (saved.get(p['post_id']) or (0,''))[1] for p in batch}
    pending = {p['post_id']:p for p in batch}
    samples = defaultdict(dict)
    error = 'fb_page_access_unavailable'
    stop = ''
    winning = None
    for cred in credentials[:3]:
        if not pending:
            break
        try:
            missing = [p for key,p in pending.items() if not natives[key]]
            if missing:
                rows = graph.get(dict(ids=','.join(p['object_id'] for p in missing),fields='id,post_id'),cred['value'])
                for p in missing:
                    row = rows.get(p['object_id'],{})
                    native = str(row.get('post_id') or '')
                    if native.isdigit():
                        native = page+'_'+native
                    if str(row.get('id'))==p['object_id'] and native.startswith(page+'_') and native[len(page)+1:].isdigit():
                        natives[p['post_id']] = native
            ids = [natives[key] for key in pending if natives[key]]
            if not ids:
                error = 'fb_post_id_unavailable'
                continue
            rows = graph.get(dict(ids=','.join(ids),fields=FIELDS),cred['value'])
            for key in list(pending):
                row = rows.get(natives[key],{})
                if natives[key] and row.get('id')==natives[key]:
                    samples[key].update(extract(row))
                if engage(samples[key]):
                    del pending[key]
                    winning = cred
                else:
                    error = 'fb_post_metrics_unavailable' if natives[key] else 'fb_post_id_unavailable'
        except FeedbackError as exc:
            error = 'feedback_graph_429' if usage.http_status==429 else str(exc)
            if error in STOP_CODES:
                stop = error
                break
    return natives,pending,samples,error,stop,winning


def collect(db,posts,history,graph,reader,usage,max_posts,now,coverage=False,max_seconds=0):
    saved = {r[0]:r for r in db.execute('SELECT * FROM observation')}
    holds = {r[0] for r in db.execute('SELECT page FROM page_retry WHERE retry>?',(now,))}
    eligible = {key:p for key,p in posts.items() if p['account_id'] not in holds}
    due,groups = plan(eligible,saved,history,now,max_posts,coverage)
    started = time.monotonic()
    stats = dict(graph_due=due,first_attempted=0,first_covered=0,attempted=0,graph_calls=0,
                 peak_usage=0,errors={},metrics_upstream_error='',held_pages=len(holds),
                 closing_pass=bool(coverage and sampling.closing_start(datetime.fromtimestamp(now,timezone.utc))))
    pages = list(groups)
    errors = Counter()
    for begin in range(0,len(pages),40):
        page_batch = pages[begin:begin+40]
        if graph.calls>=graph.max_calls or (max_seconds and time.monotonic()-started>=max_seconds):
            break
        try:
            credentials = reader.credentials(page_batch)
        except FeedbackError as exc:
            stats['metrics_upstream_error'] = str(exc)
            break
        for page in page_batch:
            rows,creds = groups[page],list(credentials.get(page,()))
            for offset in range(0,len(rows),25):
                if graph.calls>=graph.max_calls or (max_seconds and time.monotonic()-started>=max_seconds):
                    break
                batch = rows[offset:offset+25]
                natives,pending,samples,error,stop,winner = fetch_batch(batch,page,creds,saved,graph,usage)
                if winner is not None:
                    creds.remove(winner)
                    creds.insert(0,winner)
                stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
                for p in batch:
                    key = p['post_id']
                    fresh = samples.get(key,{})
                    sampling.save(db,key,fresh,stamp)
                    # An interrupted batch is eligible again immediately. Record only actual outcomes.
                    if key in pending and stop:
                        continue
                    old = saved.get(key)
                    values = json.loads(old[2]) if old else {}
                    had_engagement = engage(values)
                    times = values.setdefault('_times',{k:old[3] if old else None
                        for k in ('views','likes','comments','shares') if k in values})
                    times.update((k,stamp) for k in fresh)
                    if 'views' in fresh:
                        values['_views_at'] = stamp
                    elif 'views' in values and '_views_at' not in values and old:
                        values['_views_at'] = old[3]
                    values.update(fresh)
                    observed = stamp if fresh else old[3] if old else None
                    code = error if key in pending else ''
                    if code:
                        errors[code] += 1
                        interval = 21600
                    else:
                        age = now-datetime.fromisoformat(p['published_at'].replace('Z','+00:00')).timestamp()
                        interval = 3600 if age<7*86400 else 21600 if age<30*86400 else 86400
                    db.execute('INSERT OR REPLACE INTO observation VALUES(?,?,?,?,?,?)',
                               (key,natives[key],json.dumps(values),observed,now+interval,code))
                    stats['attempted'] += 1
                    stats['first_attempted'] += int(old is None)
                    stats['first_covered'] += int(not had_engagement and engage(values))
                db.commit()
                # A Page permission failure must not consume every remaining
                # batch on that Page, or delay other authorized Pages.
                if not samples and error in ('feedback_graph_10','feedback_graph_190','fb_page_access_unavailable'):
                    db.execute('INSERT OR REPLACE INTO page_retry VALUES(?,?,?)',(page,now+21600,error))
                    db.commit()
                    break
                if samples:
                    db.execute('DELETE FROM page_retry WHERE page=?',(page,))
                    db.commit()
                if stop:
                    if stop!='feedback_request_budget_reached':
                        stats['metrics_upstream_error'] = stop
                        db.execute("INSERT OR REPLACE INTO collection_state VALUES('resume_at',?)",
                                   (int(time.time())+max(900,usage.wait),))
                        db.commit()
                    break
            if stop_or_budget(stats,graph):
                break
        if stop_or_budget(stats,graph):
            break
    stats.update(graph_calls=graph.calls,peak_usage=usage.peak,errors=dict(errors))
    return stats


def stop_or_budget(stats,graph):
    return bool(stats['metrics_upstream_error']) or graph.calls>=graph.max_calls


def apply_cached(db,posts):
    for key,native,metrics,stamp,_,error in db.execute('SELECT * FROM observation'):
        p = posts.get(key)
        if p is None:
            continue
        values = json.loads(metrics)
        view_stamp = values.pop('_views_at',stamp)
        times = values.pop('_times',None) or {k:stamp for k in values if k in ('views','likes','comments','shares')}
        times['views'] = view_stamp
        if p.get('views') is not None and (p.get('metrics_at') or '')>(view_stamp or ''):
            values.pop('views',None)
            times['views'] = p.get('metrics_at')
        p['_metric_times'] = times
        p.update(values)
        p['metrics_at'] = max(filter(None,(stamp,p.get('metrics_at'))),default=None)
        if native:
            p['post_url'] = 'https://www.facebook.com/'+p['account_id']+'/posts/'+native.split('_')[-1]
        p['error_code'] = error


def sync(posts,cache,*,max_calls=160,max_posts=1500,history=False,coverage=False,max_seconds=0,interval=0.5):
    import fcntl
    cache = Path(cache)
    with closing(sqlite3.connect(cache,timeout=15)) as db,cache.with_suffix('.lock').open('a') as guard:
        prepare(db)
        stats = dict(graph_calls=0,metrics_upstream_error='')
        now = int(time.time())
        try:
            fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            stats['collection_status'] = 'other_collector_active'
        else:
            resume = db.execute("SELECT value FROM collection_state WHERE key='resume_at'").fetchone()
            if resume and resume[0]>now:
                stats.update(collection_status='rate_cooldown',resume_at=resume[0],metrics_upstream_error='feedback_graph_usage_high')
            else:
                graph = MetaReader(os.environ.get('FB_GRAPH_API_VERSION','v22.0'),max_calls=max_calls,interval=interval)
                usage = Usage()
                graph.session.hooks['response'].append(usage.response)
                with closing(graph.session):
                    try:
                        stats = collect(db,posts,history,graph,GatedReader(os.environ),usage,max_posts,now,coverage,max_seconds)
                    except FeedbackError as exc:
                        stats.update(graph_calls=graph.calls,metrics_upstream_error=str(exc),peak_usage=usage.peak)
            db.execute('INSERT INTO collection_run(mode,started,finished,stats) VALUES(?,?,?,?)',
                       ('coverage' if coverage else 'history' if history else 'hourly',now,int(time.time()),json.dumps(stats)))
            sampling.prune(db)
            db.commit()
        if not history:
            apply_cached(db,posts)
        return stats
