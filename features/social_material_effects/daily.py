"""Daily changes from retained observations and verified dated income.

Temporary tables belong to this read request, never to the serving snapshots.
Publication dates only establish when a counter can start at zero; they are not
the report's date filter. A material with no evidence on a day is not a data row.
"""
from datetime import date, datetime, timedelta, timezone
import json
from . import data, window

COUNTERS = (*window.COUNTERS, 'reposts', 'quotes', 'bookmarks')
DIMENSIONS = ('data_date', 'channel', 'namespace', 'material_id')
LABELS = ('material_name', 'catalog', 'content_id', 'drama_name', 'product', 'language')


def date_range(start='', end='', now=None):
    today = (now or datetime.now(timezone.utc)).astimezone(window.BJ).date()
    last = date.fromisoformat(end) if end else today-timedelta(days=1)
    first = date.fromisoformat(start) if start else last-timedelta(days=6)
    if first > last:
        raise ValueError('invalid_data_date_range')
    return first.isoformat(), last.isoformat(), today.isoformat()


def setup(db, details):
    if details:
        columns = ['data_date TEXT', 'channel TEXT'] + [f+' TEXT' for f in data.FIELDS]
        columns += ['account_ids TEXT'] + [m+' INTEGER' for m in data.METRICS]
    else:
        columns = [f+' TEXT' for f in (*DIMENSIONS, *LABELS, 'metrics_at')]
        columns += ['post_count INTEGER', 'account_count INTEGER', 'missing_posts INTEGER']
        for metric in data.METRICS:
            columns.extend((metric+' INTEGER', 'n_'+metric+' INTEGER', 'known_'+metric+' INTEGER'))
    db.execute('CREATE TEMP TABLE daily_result('+','.join(columns)+')')


def source(db, channel, conditions, args, start, end, details):
    """Append one channel, preserving missing posts in a material's coverage."""
    tables = {r[0] for r in db.execute(f'SELECT name FROM {channel}.sqlite_master WHERE type=\'table\'')}
    if not {'counter_day', 'income_day', 'income_scope'} <= tables:
        return dict(available=False, first=None, last=None)
    first_counter, last_counter = db.execute(f'SELECT MIN(day),MAX(day) FROM {channel}.counter_day').fetchone()
    first_income, last_income = db.execute(f'SELECT MIN(start),MAX(end) FROM {channel}.income_scope').fetchone()
    meta = json.loads(db.execute(f'SELECT value FROM {channel}.metadata').fetchone()[0])
    generated = window.instant(meta.get('generated_at'))
    retained_from = (generated.astimezone(window.BJ).date()-timedelta(days=32)).isoformat() if generated else None
    # The sparse rows are pruned on projection publication. A stale upstream
    # income scope must not turn pruned history into verified zero income.
    if first_income and retained_from:
        first_income = max(first_income, retained_from)
    if first_income and first_income > last_income:
        first_income = last_income = None
    firsts, lasts = [s for s in (first_counter, first_income) if s], [s for s in (last_counter, last_income) if s]
    info = dict(available=True, first=min(firsts) if firsts else None, last=max(lasts) if lasts else None)
    if not firsts or max(start, info['first']) > min(end, info['last']):
        return info
    first, last = max(start, info['first']), min(end, info['last'])
    predicate = ' WHERE '+' AND '.join(conditions) if conditions else ''
    db.execute(f'CREATE TEMP TABLE selected_posts AS SELECT *,date(published_at,\'+8 hours\') AS published_bj,'
               f'date(published_at) AS published_utc FROM {channel}.posts'+predicate, args)
    db.execute('CREATE UNIQUE INDEX selected_post_id ON selected_posts(post_id)')
    # Pivot once, rather than performing a separate indexed lookup per metric.
    db.execute('CREATE TEMP TABLE samples AS SELECT c.post_id,c.day,MAX(c.observed_at) metrics_at,'+
               ','.join(f"MAX(CASE WHEN c.metric='{m}' THEN c.value END) AS {m}" for m in COUNTERS)+
               f' FROM {channel}.counter_day c JOIN selected_posts p USING(post_id) WHERE c.day BETWEEN ? AND ? '
               'GROUP BY c.day,c.post_id', ((date.fromisoformat(first)-timedelta(days=1)).isoformat(), last))
    db.execute('CREATE UNIQUE INDEX sample_key ON samples(day,post_id)')
    db.execute('CREATE TEMP TABLE income_gaps AS SELECT s.post_id,j.value AS day '
               f'FROM {channel}.income_scope s JOIN selected_posts p USING(post_id),json_each(s.missing) j '
               'WHERE j.value BETWEEN ? AND ?', (first, last))
    db.execute('CREATE INDEX income_gap_key ON income_gaps(post_id,day)')
    # Sparse income rows mean zero only inside the verified coverage scope.
    income_known = 's.start<=d.data_date AND s.end>=d.data_date AND d.income_first<=d.data_date AND g.post_id IS NULL'
    facts = [f"'{channel}' AS channel", 'd.data_date']
    facts += ['a.metrics_at AS metrics_at' if f == 'metrics_at' else 'p.'+f for f in data.FIELDS]
    facts += ['p.account_ids', '(a.post_id IS NOT NULL OR i.post_id IS NOT NULL OR ('+income_known+')) AS has_data']
    for metric in data.METRICS:
        if metric in COUNTERS:
            baseline = f'CASE WHEN p.published_bj=d.data_date THEN 0 ELSE b.{metric} END'
            facts.append(f'CASE WHEN a.{metric}>=({baseline}) THEN a.{metric}-({baseline}) END AS {metric}')
        elif metric == 'revenue_cents':
            facts.append(f'CASE WHEN {income_known} THEN COALESCE(i.revenue_cents,0) END AS revenue_cents')
        else:
            facts.append('NULL AS '+metric)
    daily = ('WITH d AS (SELECT ? AS data_date,? AS previous_date,? AS income_first), facts AS (SELECT '+','.join(facts)+
             ' FROM selected_posts p CROSS JOIN d LEFT JOIN samples a ON a.post_id=p.post_id AND a.day=d.data_date '
             'LEFT JOIN samples b ON b.post_id=p.post_id AND b.day=d.previous_date '
             f'LEFT JOIN {channel}.income_scope s ON s.post_id=p.post_id '
             f'LEFT JOIN {channel}.income_day i ON i.post_id=p.post_id AND i.day=d.data_date '
             'LEFT JOIN income_gaps g ON g.post_id=p.post_id AND g.day=d.data_date '
             # UTC income can belong to the date before the Beijing publish day.
             "WHERE COALESCE(p.published_utc,'0001-01-01')<=d.data_date) ")
    if details:
        names = ['data_date', 'channel', *data.FIELDS, 'account_ids', *data.METRICS]
        # Include unknown posts of a material with data, so coverage agrees.
        result = 'SELECT '+','.join(names)+' FROM facts WHERE EXISTS(SELECT 1 FROM facts f '
        result += 'WHERE f.namespace=facts.namespace AND f.material_id=facts.material_id AND f.has_data)'
    else:
        names = [*DIMENSIONS, *LABELS, 'metrics_at', 'post_count', 'account_count', 'missing_posts']
        values = list(DIMENSIONS) + ['MAX('+f+')' for f in LABELS]
        values += ['MIN(metrics_at)', 'COUNT(*)', 'account_count_of(account_ids)',
                   'SUM(CASE WHEN '+' OR '.join(m+' IS NULL' for m in (*window.COUNTERS, 'revenue_cents'))+' THEN 1 ELSE 0 END)']
        for metric in data.METRICS:
            names.extend((metric, 'n_'+metric, 'known_'+metric))
            values.extend((f'CASE WHEN COUNT({metric})=COUNT(*) THEN SUM({metric}) END', f'COUNT({metric})', f'SUM({metric})'))
        result = 'SELECT '+','.join(values)+' FROM facts GROUP BY '+','.join(DIMENSIONS)+' HAVING MAX(has_data)'
    current = date.fromisoformat(first)
    while current.isoformat() <= last:
        db.execute('INSERT INTO daily_result('+','.join(names)+') '+daily+result,
                   (current.isoformat(), (current-timedelta(days=1)).isoformat(), retained_from))
        current += timedelta(days=1)
    for table in ('income_gaps', 'samples', 'selected_posts'):
        db.execute('DROP TABLE '+table)
    return info


def results(db, details, page, size, sort, direction):
    if details:
        sums = ','.join(f'SUM({m}) AS {m},COUNT({m}) AS n_{m}' for m in data.METRICS)
        summary = dict(db.execute('SELECT COUNT(*) total,COUNT(*) post_count,'+sums+' FROM daily_result').fetchone())
        order = {'post_count':'post_id', 'account_count':'account_id'}.get(sort, sort)
    else:
        sums = ','.join(f'SUM(known_{m}) AS {m},COALESCE(SUM(n_{m}),0) AS n_{m}' for m in data.METRICS)
        summary = dict(db.execute('SELECT COUNT(*) total,COALESCE(SUM(post_count),0) post_count,'+sums+' FROM daily_result').fetchone())
        order = 'known_'+sort if sort in data.METRICS else sort
    summary['material_count'] = db.execute('SELECT COUNT(*) FROM (SELECT 1 FROM daily_result '
                                           'GROUP BY channel,namespace,material_id)').fetchone()[0]
    summary['data_days'] = db.execute('SELECT COUNT(DISTINCT data_date) FROM daily_result').fetchone()[0]
    tie = ',channel,post_id' if details else ',channel,namespace,material_id'
    secondary = (',views DESC' if details else ',known_views DESC') if sort == 'data_date' else ',data_date DESC'
    order_by = order+' IS NULL,'+order+' '+direction+secondary+tie
    items = []
    for record in db.execute('SELECT * FROM daily_result ORDER BY '+order_by+' LIMIT ? OFFSET ?', (size, (page-1)*size)):
        row = dict(record)
        if details:
            for key in ('owner', 'tenant', 'account_ids'):
                row.pop(key, None)
        else:
            row['coverage'] = {m:row.pop('n_'+m) for m in data.METRICS}
            row['known'] = {m:row.pop('known_'+m) for m in data.METRICS}
        items.append(row)
    return items, summary
