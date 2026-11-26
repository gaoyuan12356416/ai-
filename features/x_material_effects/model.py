"""Pure, conservative aggregation of distinct post observations."""
from collections import defaultdict
from datetime import datetime, timezone

METRICS = ('views', 'likes', 'comments', 'shares', 'reposts', 'quotes', 'bookmarks', 'revenue_cents', 'refund_cents')
FILTERS = ('views', 'likes', 'comments', 'shares', 'revenue')
PUBLIC_FIELDS = dict(views='impression_count', likes='like_count', comments='reply_count',
                     reposts='retweet_count', quotes='quote_count', bookmarks='bookmark_count')

def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)

def interval(published_at, now):
    days = (now - instant(published_at)).total_seconds() / 86400
    return 3600 if days <= 7 else 21600 if days <= 30 else 86400

def extract(post):
    public = post.get('public_metrics') or {}
    result = {name: public.get(field) if type(public.get(field)) is int and public[field] >= 0 else None
              for name, field in PUBLIC_FIELDS.items()}
    result['shares'] = (result['reposts'] + result['quotes']
                        if result['reposts'] is not None and result['quotes'] is not None else None)
    return result

def aggregate(posts, *, level='material'):
    """Missing is not zero. A known subtotal and coverage accompany incomplete totals."""
    groups = defaultdict(list)
    seen = set()
    for post in posts:
        identity = str(post['post_id'])
        if identity in seen:
            raise ValueError('duplicate_post_in_aggregate')
        seen.add(identity)
        key = ((post['namespace'], post['material_id']) if level == 'material'
               else (post['catalog'], post['content_id'], post['language']))
        groups[key].append(post)
    output = []
    for key, rows in groups.items():
        row = {k: rows[0].get(k, '') for k in ('namespace','material_id','material_name','product','catalog',
                                              'content_id','drama_name','language')}
        row.update(post_count=len(rows), account_count=len({a for p in rows for a in p.get('account_ids',[p['account_id']])}),
                   first_published_at=min(p['published_at'] for p in rows),
                   last_published_at=max(p['published_at'] for p in rows), coverage={}, known={})
        for metric in METRICS:
            values = [p[metric] for p in rows if p.get(metric) is not None]
            row['coverage'][metric] = len(values)
            row['known'][metric] = sum(values) if values else None
            row[metric] = sum(values) if len(values) == len(rows) else None
        times = [p.get('metrics_at') for p in rows if p.get('metrics_at')]
        row['metrics_at'] = min(times) if times else None
        row['metrics_latest_at'] = max(times) if times else None
        row['revenue_through'] = min((p.get('revenue_through') or '' for p in rows), default='')
        row['stale_posts'] = sum(bool(p.get('error_code')) for p in rows)
        output.append(row)
    return output

def number(row, field):
    value = row.get('revenue_cents' if field == 'revenue' else field)
    return None if value is None else value / 100 if field == 'revenue' else value
