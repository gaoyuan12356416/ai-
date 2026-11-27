"""Read-only material report grouped and filtered by data date."""
from contextlib import closing
from datetime import datetime, timezone
from . import data, daily, window


def visible_x(actor):
    from features.x_material_effects.collector import readonly, ACCOUNT_DB
    with closing(readonly(ACCOUNT_DB)) as db:
        return [str(r[0]) for r in db.execute('SELECT id FROM x_authorized_account WHERE owner_user_id=? AND owner_tenant_key=?',
                (actor['user_id'],actor.get('tenant_key','')))]


def filters(get, source, details, actor):
    conditions, args = [], []
    if details and actor.get('role') != 'admin':
        if source == 'x':
            ids = visible_x(actor)
            conditions.append('account_id IN ('+','.join('?' for _ in ids)+')' if ids else '0')
            args.extend(ids)
        else:
            conditions.extend(("owner<>'' AND owner=?", "tenant<>'' AND tenant=?"))
            args.extend((actor['user_id'], actor.get('tenant_key','')))
    for key in ('namespace', 'material_id', 'product', 'language'):
        if get(key):
            conditions.append(key+'=?')
            args.append(get(key))
    if get('account_id'):
        conditions.append('EXISTS(SELECT 1 FROM json_each(account_ids) WHERE value=?)' if source == 'x' else 'account_id=?')
        args.append(get('account_id'))
    search = get('q').strip()
    if search:
        conditions.append('('+' OR '.join('instr(lower('+k+'),lower(?))>0' for k in
                                         ('material_id', 'material_name', 'content_id', 'drama_name'))+')')
        args.extend([search]*4)
    return conditions, args


def query(params, actor, root=None, *, now=None):
    def get(key, default=''):
        return params.get(key, [default])[0]
    start, end, today = daily.date_range(get('start'), get('end'), now)
    sources = data.channels(get('channel','all'))
    unavailable = [s for s in sources if not (data.root_path(root)/(s+'.sqlite3')).is_file()]
    sources = tuple(s for s in sources if s not in unavailable)
    if not sources:
        raise OSError('social_effect_snapshot_unavailable')
    details = get('view') == 'posts'
    page, size = max(1, int(get('page','1'))), max(1, min(100, int(get('page_size','50'))))
    sort, direction = get('sort','data_date'), get('direction','desc')
    if sort not in (*data.METRICS, 'post_count', 'account_count', 'data_date') or direction not in ('asc', 'desc'):
        raise ValueError('invalid_sort')
    with data.reading(sources, root) as (db, meta):
        daily.setup(db, details)
        availability = {}
        for source in sources:
            conditions, args = filters(get, source, details, actor)
            availability[source] = daily.source(db, source, conditions, args, start, end, details)
        items, count = daily.results(db, details, page, size, sort, direction)
    stamp = now or datetime.now(timezone.utc)
    def stale(key):
        return any(window.instant(m.get(key)) is None or
                   (stamp-window.instant(m[key])).total_seconds() > 7200 for m in meta.values())
    summary_meta = dict(unavailable_channels=unavailable,
        channels={k:{field:m.get(field) for field in ('version','generated_at','revenue_through','metrics_upstream_error','revenue_incomplete_sites','closing_window')} for k,m in meta.items()},
        generated_at=min(m.get('generated_at','') for m in meta.values()),
        revenue_through=min(m.get('revenue_through','') for m in meta.values()),
        stale=stale('generated_at'), revenue_stale=stale('revenue_source_generated_at'),
        metrics_upstream_error=meta.get('x',{}).get('metrics_upstream_error',''),
        dates=dict(window.description(start, end), mode='daily_increment', grouping=['data_date','channel','namespace','material_id'],
                   includes_today=start<=today<=end, availability=availability))
    return dict(ok=True, items=items, total=count['total'], page=page, page_size=size, meta=summary_meta,
                post_count=count['post_count'], material_count=count['material_count'], data_days=count['data_days'],
                summary={m:count[m] for m in data.METRICS}, coverage={m:count['n_'+m] for m in data.METRICS})
