"""Read-only, paginated material rows grouped by publishing channel."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from . import data


def visible_x(actor):
    from features.x_material_effects.collector import readonly, ACCOUNT_DB
    with closing(readonly(ACCOUNT_DB)) as db:
        return [str(r[0]) for r in db.execute('SELECT id FROM x_authorized_account WHERE owner_user_id=? AND owner_tenant_key=?',
                (actor['user_id'],actor.get('tenant_key','')))]


def day_bound(text, end):
    value = datetime.strptime(text,'%Y-%m-%d').replace(tzinfo=timezone(timedelta(hours=8)))
    return (value + timedelta(days=int(end))).timestamp()


def query(params, actor, root=None):
    def get(key, default=''):
        return params.get(key,[default])[0]
    sources = data.channels(get('channel','all'))
    details = get('view') == 'posts'
    page, size = max(1,int(get('page','1'))), max(1,min(100,int(get('page_size','50'))))
    sort, direction = get('sort','views'), get('direction','desc')
    if sort not in (*data.METRICS,'post_count','account_count','last_published_at') or direction not in ('asc','desc'):
        raise ValueError('invalid_sort')
    with data.reading(sources,root) as (db,meta):
        selects, args = [], []
        for source in sources:
            conditions = []
            if details and actor.get('role') != 'admin':
                if source == 'x':
                    ids = visible_x(actor)
                    conditions.append('account_id IN (' + ','.join('?' for _ in ids) + ')' if ids else '0')
                    args.extend(ids)
                else:
                    conditions.extend(("owner<>'' AND owner=?", "tenant<>'' AND tenant=?"))
                    args.extend((actor['user_id'],actor.get('tenant_key','')))
            for key in ('namespace','material_id','product','language'):
                if get(key):
                    conditions.append(key+'=?')
                    args.append(get(key))
            if get('account_id'):
                conditions.append('EXISTS(SELECT 1 FROM json_each(account_ids) WHERE value=?)' if source=='x' else 'account_id=?')
                args.append(get('account_id'))
            search = get('q').strip()
            if search:
                conditions.append('(' + ' OR '.join("instr(lower("+k+"),lower(?))>0" for k in
                                  ('material_id','material_name','content_id','drama_name')) + ')')
                args.extend([search]*4)
            for key,operator in [('start','>='),('end','<')]:
                if get(key):
                    conditions.append("strftime('%s',published_at)+0 "+operator+" ?")
                    args.append(int(day_bound(get(key),key=='end')))
            predicate = ' WHERE '+' AND '.join(conditions) if conditions else ''
            selects.append(f"SELECT '{source}' AS channel,* FROM {source}.posts" + predicate)
        filtered = 'WITH filtered AS (' + ' UNION ALL '.join(selects) + ') '
        count = db.execute(filtered+'SELECT COUNT(*) post_count,' + ','.join(
            f'SUM({k}) AS {k},COUNT({k}) AS n_{k}' for k in data.METRICS)+' FROM filtered',args).fetchone()
        groups = 'channel,namespace,material_id'
        if details:
            total = count['post_count']
            order = {'post_count':'post_id','account_count':'account_id','last_published_at':'published_at'}.get(sort,sort)
            sql = filtered+'SELECT * FROM filtered'
        else:
            total = db.execute(filtered+f'SELECT COUNT(*) FROM (SELECT 1 FROM filtered GROUP BY {groups})',args).fetchone()[0]
            order = sort
            sql = filtered + f'SELECT {groups},' + ','.join('MAX('+k+') AS '+k for k in
                  ('material_name','catalog','content_id','drama_name','product','language')) + ',COUNT(*) post_count,' + (
                  'account_count_of(account_ids) account_count,MIN(published_at) first_published_at,MAX(published_at) last_published_at,'
                  "MIN(metrics_at) metrics_at, SUM(CASE WHEN COALESCE(error_code,'')<>'' THEN 1 ELSE 0 END) stale_posts,") + ','.join(
                  f'CASE WHEN COUNT({k})=COUNT(*) THEN SUM({k}) END AS {k},COUNT({k}) AS n_{k},SUM({k}) AS known_{k}'
                  for k in data.METRICS) + f' FROM filtered GROUP BY {groups}'
        tie = ',channel,post_id' if details else ',channel,namespace,material_id'
        # Sort aggregate results by observed subtotal just as the template ranks them.
        order_col = 'known_'+order if not details and order in data.METRICS else order
        sql = 'SELECT * FROM ('+sql+') ORDER BY '+order_col+' IS NULL,'+order_col+' '+direction+tie+' LIMIT ? OFFSET ?'
        items = []
        for r in db.execute(sql,[*args,size,(page-1)*size]):
            row = dict(r)
            if details:
                for key in ('owner','tenant','account_ids'):
                    row.pop(key,None)
            else:
                row['coverage'] = {k:row.pop('n_'+k) for k in data.METRICS}
                row['known'] = {k:row.pop('known_'+k) for k in data.METRICS}
            items.append(row)
    stamp = datetime.now(timezone.utc)
    def stale(key):
        return any((stamp-datetime.fromisoformat(m[key].replace('Z','+00:00'))).total_seconds()>7200 for m in meta.values())
    summary_meta = dict(channels={k:{field:m.get(field) for field in ('version','generated_at','revenue_through','metrics_upstream_error','revenue_incomplete_sites')} for k,m in meta.items()},
        generated_at=min(m['generated_at'] for m in meta.values()), revenue_through=min(m['revenue_through'] for m in meta.values()),
        stale=stale('generated_at'), revenue_stale=stale('revenue_source_generated_at'),
        metrics_upstream_error=meta.get('x',{}).get('metrics_upstream_error',''))
    return dict(ok=True,items=items,total=total,page=page,page_size=size,meta=summary_meta,post_count=count['post_count'],
                summary={k:count[k] for k in data.METRICS},coverage={k:count['n_'+k] for k in data.METRICS})
