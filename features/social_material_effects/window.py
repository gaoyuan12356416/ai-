"""Complete-day organic windows, with observed counter baselines and dated income.

Counters are net changes between the last observations on the two boundary
days. They are sampling-based, not platform event-time analytics. Missing
boundary observations and counter resets stay unknown; no lifetime fallback.
"""
from datetime import date, datetime, timedelta, timezone
import json

BJ = timezone(timedelta(hours=8))
COUNTERS = ('views', 'likes', 'comments', 'shares')


def bounds(days, now=None):
    days = int(days)
    if not 1 <= days <= 30:
        raise ValueError('invalid_social_metric_window')
    today = (now or datetime.now(timezone.utc)).astimezone(BJ).date()
    return ((today-timedelta(days=days)).isoformat(), (today-timedelta(days=1)).isoformat())


def description(start, end):
    return dict(start=start, end=end, timezone='Asia/Shanghai', revenue_timezone='UTC',
                mode='window_increment', interaction_method='daily_last_observation_delta')


def instant(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def prepare(db, previous, generated_at):
    db.execute('CREATE TABLE counter_day(post_id TEXT,metric TEXT,day TEXT,value INTEGER,observed_at TEXT,'
               'PRIMARY KEY(day,metric,post_id)) WITHOUT ROWID')
    db.execute('CREATE TABLE income_day(post_id TEXT,day TEXT,revenue_cents INTEGER,PRIMARY KEY(post_id,day)) WITHOUT ROWID')
    db.execute('CREATE TABLE income_scope(post_id TEXT PRIMARY KEY,start TEXT,end TEXT,missing TEXT) WITHOUT ROWID')
    cutoff = ((instant(generated_at) or datetime.now(timezone.utc)).astimezone(BJ).date()-timedelta(days=32)).isoformat()
    if previous.is_file():
        db.execute('ATTACH DATABASE ? AS prior', (previous.resolve().as_uri()+'?mode=ro&immutable=1',))
        if db.execute("SELECT 1 FROM prior.sqlite_master WHERE name='counter_day'").fetchone():
            db.execute('INSERT INTO counter_day SELECT * FROM prior.counter_day WHERE day>=?', (cutoff,))
    return cutoff


def record(db, post, cutoff):
    for metric in COUNTERS:
        value = post.get(metric)
        stamp = (post.get('_metric_times') or {}).get(metric, post.get('metrics_at'))
        seen = instant(stamp)
        if type(value) is not int or value < 0 or seen is None:
            continue
        day = seen.astimezone(BJ).date().isoformat()
        if day < cutoff:
            continue
        stamp = seen.astimezone(timezone.utc).isoformat()
        db.execute('INSERT INTO counter_day VALUES(?,?,?,?,?) ON CONFLICT(day,metric,post_id) DO UPDATE '
                   'SET value=excluded.value,observed_at=excluded.observed_at '
                   'WHERE excluded.observed_at>counter_day.observed_at',
                   (post['post_id'], metric, day, value, stamp))
    income = post.get('_window_income')
    if income:
        db.execute('INSERT INTO income_scope VALUES(?,?,?,?)',
                   (post['post_id'],income['start'],income['end'],json.dumps(income.get('missing',[]))))
        db.executemany('INSERT INTO income_day VALUES(?,?,?)',
                       ((post['post_id'],day,cents) for day,cents in income.get('daily',{}).items() if day>=cutoff))


def totals(db, source, level, language, products, start, end):
    """Return the same counted-evidence tuples as the cumulative selector."""
    aliases = {'ja':('ja','jp'),'tl':('tl','fil')}.get(language,(language,))
    where = 'lower(p.language) IN ('+','.join('?' for _ in aliases)+')'
    args = list(aliases)
    if products:
        where += ' AND p.product COLLATE NOCASE IN ('+','.join('?' for _ in products)+')'
        args.extend(products)
    end_at = datetime.combine(date.fromisoformat(end)+timedelta(days=1),datetime.min.time(),BJ).timestamp()
    start_at = datetime.combine(date.fromisoformat(start),datetime.min.time(),BJ).timestamp()
    where += " AND strftime('%s',p.published_at)+0<?"
    args.append(end_at)
    fields = 'p.catalog,p.content_id'+(',p.namespace,p.material_id' if level=='material' else '')
    rows = db.execute(f'SELECT p.post_id,{fields},p.published_at FROM {source}.posts p WHERE {where}',args).fetchall()
    count = len(fields.split(','))
    if not db.execute(f"SELECT 1 FROM {source}.sqlite_master WHERE name='counter_day'").fetchone():
        # Old snapshots can still identify candidates, but cannot supply window data.
        observed, income, scopes = {}, {}, {}
    else:
        baseline = (date.fromisoformat(start)-timedelta(days=1)).isoformat()
        observed = {(r[0],r[1],r[2]):r[3] for r in db.execute(
            f'SELECT c.post_id,c.metric,c.day,c.value FROM {source}.counter_day c JOIN {source}.posts p USING(post_id) '
            f'WHERE c.day IN (?,?) AND {where}',(baseline,end,*args))}
        income = dict(db.execute(f'SELECT c.post_id,SUM(c.revenue_cents) FROM {source}.income_day c JOIN {source}.posts p USING(post_id) '
            f'WHERE c.day BETWEEN ? AND ? AND {where} GROUP BY c.post_id',(start,end,*args)))
        scopes = {r[0]:r[1:] for r in db.execute(f'SELECT c.* FROM {source}.income_scope c JOIN {source}.posts p USING(post_id) WHERE {where}',args)}
    baseline = (date.fromisoformat(start)-timedelta(days=1)).isoformat()
    result = {}
    for row in rows:
        pid, key = row[0], tuple(row[1:1+count])
        target = result.setdefault(key,[0]+[None,0]*5)
        target[0] += 1
        published = instant(row[-1])
        new = published is not None and published.timestamp() >= start_at
        for i,metric in enumerate((*COUNTERS,'revenue')):
            if metric == 'revenue':
                scope = scopes.get(pid)
                value = None
                if scope:
                    first = max(start, published.astimezone(timezone.utc).date().isoformat()) if published else start
                    if scope[0]<=first and scope[1]>=end and not any(first<=d<=end for d in json.loads(scope[2])):
                        value = income.get(pid,0)
            else:
                before = 0 if new else observed.get((pid,metric,baseline))
                after = observed.get((pid,metric,end))
                value = after-before if before is not None and after is not None and after>=before else None
            if value is not None:
                target[1+2*i] = (target[1+2*i] or 0)+value
                target[2+2*i] += 1
    return result
