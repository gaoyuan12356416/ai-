"""Material-first reverse lookups. All SQL uses the existing read-only FIFO.

Scalar values use the existing source/original-source indexes. Composite and
whitespace encodings are separately enumerated once, not once per material.
No publication-date/status filter is allowed to hide an old, still-live Ad.
"""
from collections import Counter
from .policy import parse_ids
from .sql_source import _inside, _quote, _chunks, _ids, SqlSourceError

# Adjacent, exhaustive string ranges permit InnoDB index-condition pushdown.
# Include the open-ended first/last ranges, including whitespace and JSON.
COMPOSITE_BOUNDS=('0','1','2','3','4','5','6','7','8','9','A','G','N','U')

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
        seen_ids=set()
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
                    # String ordering belongs to MySQL's column collation,
                    # not Python Unicode ordering (notably JSON/whitespace).
                    if rid in seen_ids or (cursor is not None and order_column=='id' and rid<=cursor[1]):
                        raise SqlSourceError('ad_lineage_incomplete')
                    seen_ids.add(rid)
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
    # Complete both indexed columns independently. A product-index pass fetches
    # tens of millions of full records; these range scans reject ordinary
    # scalar values in the secondary index before fetching product/full rows.
    # EXPLAIN on the live reader confirms range + index_condition + no filesort.
    bounds=(None,)+COMPOSITE_BOUNDS+(None,)
    for column,index in (('source_id','ads_facebook_auto_created_data_source_id'),
                         ('original_source_id','ads_facebook_auto_created_data_original_source_id')):
        for low,high in zip(bounds,bounds[1:]):
            predicates=[complex_value(column),'product IN '+_inside(products)]
            if low is not None:predicates.append(column+'>='+_quote(low))
            if high is not None:predicates.append(column+'<'+_quote(high))
            for rows in pages('ads_facebook_auto_created_data FORCE INDEX('+index+')',expressions,
                              ' AND '.join(predicates),column):
                accept(rows,True)
            metrics['composite_ranges']+=1
            progress(stage='material_lookup_composite',composite_column=column,
                     publication_rows=len(found),lookup_metrics=dict(metrics))
    metrics['publication_rows']=len(found)
    return list(found.values()),dict(metrics)
