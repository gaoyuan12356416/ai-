"""Material-first reverse lookups. All SQL uses the existing read-only FIFO.

Scalar values use the existing source/original-source indexes. Composite and
whitespace encodings are separately enumerated once, not once per material.
No publication-date/status filter is allowed to hide an old, still-live Ad.
"""
from collections import Counter
from .policy import parse_ids
from .sql_source import _inside, _quote, _chunks, _ids, SqlSourceError

COMPOSITE_RANGE_SIZE=200000

def complex_value(column):
    return "(LOCATE(',',{c})>0 OR LEFT(TRIM({c}),1)='[' OR {c} REGEXP '[[:space:]]')".format(c=column)


def material_rows(source, material_ids, scope, progress=lambda **kw: None, stop=lambda: False):
    materials=set(_ids(material_ids)); products=set(_ids(scope)); aliases=set(); found={}
    metrics=Counter()
    if not materials or not products:
        return [], dict(metrics)
    expressions=tuple((key,"CAST(%s AS CHAR)"%key) for key in
        ('id','ad_id','product','source_id','original_source_id','creative_id',
         'ad_account_id','user_id','publish_queue_id','video_id','image_ids'))

    def pages(table, fields, predicate, order_column='id'):
        cursor=None
        with source._session() as session:
            while True:
                if stop(): raise InterruptedError('stop_requested')
                where=predicate
                if cursor is not None:
                    if order_column=='id': where+=' AND id>%d'%cursor[1]
                    else: where+=' AND ({c}>{v} OR ({c}={v} AND id>{i}))'.format(
                        c=order_column,v=_quote(cursor[0]),i=cursor[1])
                order='id' if order_column=='id' else order_column+',id'
                rows=session.query(source._select(fields,table,
                    'WHERE '+where+' ORDER BY '+order+' LIMIT '+str(source.page_size)))
                if len(rows)>source.page_size: raise SqlSourceError('ad_lineage_incomplete')
                for row in rows:
                    rid=int(row['id']); current=(str(row.get(order_column,'')),rid)
                    if cursor is not None and (rid<=cursor[1] if order_column=='id' else current<=cursor):
                        raise SqlSourceError('ad_lineage_incomplete')
                    cursor=current
                metrics['sql_pages']+=1
                yield rows
                if len(rows)<source.page_size: break

    fields=(('id','CAST(id AS CHAR)'),('source_id','CAST(source_id AS CHAR)'))
    for part in _chunks(sorted(materials),1000):
        for rows in pages('ads_source FORCE INDEX(source_type)',fields,
                          'source_type=3 AND source_id IN '+_inside(part),'source_id'):
            aliases.update(row['id'] for row in rows)
    # An ads_source wrapper can itself contain multiple canonical materials.
    for rows in pages('ads_source FORCE INDEX(source_type_2)',fields,
                      'source_type=3 AND '+complex_value('source_id')):
        for row in rows:
            metrics['complex_source_rows']+=1
            try: matched=materials.intersection(parse_ids(row['source_id']))
            except ValueError: continue
            if matched: aliases.add(row['id'])
    metrics['source_aliases']=len(aliases)
    progress(stage='material_lookup',lookup_metrics=dict(metrics))

    def accept(rows, composite=False):
        for row in rows:
            if str(row['product']) not in products: raise SqlSourceError('unexpected_ad_lineage')
            if composite:
                metrics['complex_publication_rows']+=1
                try: originals=set(parse_ids(row['original_source_id']))
                except ValueError: originals=set()
                try: sources=set(parse_ids(row['source_id']))
                except ValueError: sources=set()
                if not (originals & materials or sources & aliases): continue
            row['row_id']=row['id']
            old=found.get(row['id'])
            if old is not None and old!=row: raise SqlSourceError('ad_lineage_incomplete')
            found[row['id']]=row

    for column, values, index in (
        ('original_source_id',materials,'ads_facebook_auto_created_data_original_source_id'),
        ('source_id',aliases,'ads_facebook_auto_created_data_source_id')):
        for part in _chunks(sorted(values),1000):
            # A valid nonempty original set must equal the source wrapper's
            # material set. Scalar originals were already found above; arrays
            # are covered by the complete composite pass below.
            absent_original=(" AND (original_source_id IS NULL OR TRIM(original_source_id) IN ('','0'))"
                             if column=='source_id' else '')
            for rows in pages('ads_facebook_auto_created_data FORCE INDEX('+index+')',expressions,
                              column+' IN '+_inside(part)+' AND product IN '+_inside(products)+absent_original,column):
                accept(rows)
            progress(stage='material_lookup',publication_rows=len(found),lookup_metrics=dict(metrics))
    # This completes the inverse lookup for CSV/JSON arrays without using a
    # non-indexable per-material FIND_IN_SET over the publication table.
    composite='('+complex_value('source_id')+' OR '+complex_value('original_source_id')+')'
    for number,pid in enumerate(sorted(products)):
        # Find bounded index ranges BEFORE applying non-sargable predicates.
        # A sparse composite filter over a large product otherwise reads the
        # entire product before returning even its first (possibly empty) page.
        low=0
        while True:
            if stop():raise InterruptedError('stop_requested')
            table='`'+source.schema+'`.ads_facebook_auto_created_data FORCE INDEX(idx_product)'
            base='product='+_quote(pid)+' AND id>'+str(low)
            with source._session() as session:
                bounds=session.query("SELECT JSON_OBJECT('high',MAX(id),'count',COUNT(*)) FROM "
                    '(SELECT id FROM '+table+' WHERE '+base+' ORDER BY id LIMIT '+str(COMPOSITE_RANGE_SIZE)+') AS bounded_ids')
                if len(bounds)!=1:raise SqlSourceError('ad_lineage_incomplete')
                bound=bounds[0];high=bound.get('high')
                if not high:break
                high=int(high)
                if high<=low:raise SqlSourceError('ad_lineage_incomplete')
                # Range boundary and its pages share one short read-only
                # snapshot; release the FIFO between ranges for other jobs.
                for rows in pages('ads_facebook_auto_created_data FORCE INDEX(idx_product)',expressions,
                                  base+' AND id<='+str(high)+' AND '+composite):
                    accept(rows,True)
            low=high
            metrics['composite_ranges']+=1
            metrics['publication_rows_examined_for_composites']+=int(bound['count'])
            progress(stage='material_lookup_composite',product_id=pid,products_finished=number,
                     publication_rows=len(found),lookup_metrics=dict(metrics))
            if int(bound['count'])<COMPOSITE_RANGE_SIZE:break
        progress(stage='material_lookup_composite',products_finished=number+1,
                 products_total=len(products),publication_rows=len(found),lookup_metrics=dict(metrics))
    metrics['publication_rows']=len(found)
    return list(found.values()),dict(metrics)
