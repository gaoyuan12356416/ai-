"""Bounded write-time SQL contexts in one FIFO snapshot; no lifetime cache."""
from .sql_source import SqlSourceError, _ids, _ID, _inside, _quote
from .policy import parse_ids


def current_contexts(source, candidates):
    if not 1 <= len(candidates) <= 64:
        raise ValueError('context_batch_size_out_of_range')
    wanted={(_ids([c['ad_id']])[0],_ids([c['creative_id']])[0]) for c in candidates}
    ids=sorted({key[0] for key in wanted})
    expressions=tuple((key,'CAST(a.%s AS CHAR)' % key) for key in
        ('id','ad_id','product','source_id','original_source_id','creative_id','ad_account_id',
         'user_id','publish_queue_id','video_id','image_ids'))+(
        ('created_at',"DATE_FORMAT(a.created_at,'%Y-%m-%d %H:%i:%s.%f')"),
        ('queue_id','CAST(pq.id AS CHAR)'),('queue_product','CAST(pq.product AS CHAR)'),
        ('queue_user_id','CAST(pq.user_id AS CHAR)'),('default_token','CAST(pq.default_token AS CHAR)'),
        ('setting_id','CAST(p.id AS CHAR)'),('setting_parent','CAST(p.ads_app_id AS CHAR)'),
        ('parent_setting_id','CAST(parent.id AS CHAR)'),('default_user','CAST(p.default_user AS CHAR)'),
        ('available',"EXISTS(SELECT 1 FROM `{s}`.ads_facebook_info i "
         "WHERE i.user_id=CASE WHEN pq.default_token=1 THEN p.default_user "
         "WHEN pq.default_token=-1 THEN a.user_id ELSE NULL END "
         "AND TRIM(i.accessToken)<>'' LIMIT 1)".format(s=source.schema)))
    joins=("ads_facebook_auto_created_data a FORCE INDEX(ad_id) "
        "LEFT JOIN `{s}`.ads_template_make_queue pq ON pq.id=a.publish_queue_id "
        "LEFT JOIN `{s}`.ads_apps_setting p ON p.id=a.product "
        "LEFT JOIN `{s}`.ads_apps_setting parent ON parent.id=p.ads_app_id").format(s=source.schema)
    rows={aid:[] for aid in ids};cursor=None;closed=set();seen=set();contexts={};all_sources=set()
    with source.snapshot():
        with source._session() as session:
            while True:
                where='WHERE a.ad_id IN '+_inside(ids)
                if cursor:
                    where+=' AND (a.ad_id>%s OR (a.ad_id=%s AND a.id>%d))' % (
                        _quote(cursor[0]),_quote(cursor[0]),cursor[1])
                page=session.query(source._select(expressions,joins,
                    where+' ORDER BY a.ad_id,a.id LIMIT %d' % source.page_size))
                if len(page)>source.page_size:raise SqlSourceError('ad_lineage_incomplete')
                for row in page:
                    aid=str(row.get('ad_id'));rid=str(row.get('id') or '')
                    if aid not in rows:raise SqlSourceError('unexpected_ad_lineage')
                    if not _ID.fullmatch(rid) or rid in seen:raise SqlSourceError('ad_lineage_incomplete')
                    if cursor:
                        if aid==cursor[0] and int(rid)<=cursor[1]:raise SqlSourceError('ad_lineage_incomplete')
                        if aid!=cursor[0]:
                            closed.add(cursor[0])
                            if aid in closed:raise SqlSourceError('ad_lineage_incomplete')
                    seen.add(rid);cursor=(aid,int(rid));row['row_id']=rid;rows[aid].append(row)
                if len(page)<source.page_size:break
        for aid,cid in wanted:
            context={'rows':rows[aid],'scope':{},'credential_users':[],'sources':{}}
            users=set();keys=set()
            try:
                for row in rows[aid]:
                    if str(row.get('creative_id'))!=cid:continue
                    product=str(row.get('product') or '');parent=str(row.get('setting_parent') or '')
                    if not _ID.fullmatch(product) or str(row.get('setting_id'))!=product:
                        raise SqlSourceError('current_product_unavailable')
                    if product in ('1479','3302'):context['scope'][product]=product
                    elif parent in ('1479','3302'):
                        if str(row.get('parent_setting_id'))!=parent:raise SqlSourceError('current_product_unavailable')
                        context['scope'][product]=parent
                    user=source._credential_user(source._credential_identity(row),row)
                    available=row.get('available')
                    if type(available) not in (bool,int) or available not in (0,1):
                        raise SqlSourceError('invalid_credential_availability')
                    if not available:raise SqlSourceError('publishing_credential_unavailable')
                    users.add(user);value=row.get('source_id')
                    if value is None or value==0 or isinstance(value,str) and value.strip() in ('','0'):continue
                    try:keys.update(parse_ids(value))
                    except (TypeError,ValueError):raise SqlSourceError('invalid_source_mapping') from None
                context['credential_users']=sorted(users,key=int);context['_source_keys']=keys
                all_sources.update(keys)
            except SqlSourceError as exc:context['error']=exc.code
            contexts[(aid,cid)]=context
        sources=source.sources(sorted(all_sources,key=int))
        for context in contexts.values():
            keys=context.pop('_source_keys',set())
            if not keys<=set(sources):context['error']='invalid_source_mapping'
            context['sources']={key:sources[key] for key in keys if key in sources}
    return contexts
