"""Named existing 24-hour Mobo drama/resource lookups, with fixed read SQL."""
import datetime as dt


def recent_ids(cursor,institution,kind,now=None):
    if kind not in ('story','resource'):raise ValueError('invalid_metadata_kind')
    end=now or dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    start=end-dt.timedelta(hours=24)
    cursor.execute('SELECT story_id FROM ads_ai.cdreader_koc_drama '
        'WHERE institution_user_id=%s AND project_type=2 AND is_show=1 '
        'AND source_publish_time BETWEEN %s AND %s ORDER BY source_publish_time DESC,story_id DESC',
        (institution,start.strftime('%Y-%m-%d %H:%M:%S'),end.strftime('%Y-%m-%d %H:%M:%S')))
    stories=[row['story_id'] for row in cursor.fetchall()]
    if kind=='story':return dict(ids=stories,story_count=len(stories),unmapped_count=0)
    resources={}
    for offset in range(0,len(stories),500):
        ids=list(dict.fromkeys(str(value) for value in stories[offset:offset+500]))
        cursor.execute('SELECT DISTINCT content_id AS story_id,series_code AS resource_id '
            'FROM kunlunads_dev.ads_drama_info FORCE INDEX(ac) WHERE app_id=2373 AND content_id IN ('+
            ','.join(['%s']*len(ids))+')',ids)
        codes={story:set() for story in ids}
        for row in cursor.fetchall():
            story,code=str(row.get('story_id') or ''),str(row.get('resource_id') or '').strip()
            if story in codes and code:codes[story].add(code)
        resources.update({story:next(iter(values)) for story,values in codes.items() if len(values)==1})
    return dict(ids=list(dict.fromkeys(resources[story] for story in stories if story in resources)),
                story_count=len(stories),unmapped_count=len(stories)-len(resources))
