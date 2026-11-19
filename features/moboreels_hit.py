"""Read-only Mobo identities and classifications for explicit hit-drama requests."""
from features.hit_drama_target import identifier, present_target, target_language

MOBO_APP_ID = 2373
LANGUAGES = {2:"zh-Hant",3:"en",4:"es",5:"pt-br",6:"fr",7:"ru",8:"it",9:"ja",
             10:"ar",11:"id",12:"th",13:"vi",14:"ko",15:"fil",16:"de",17:"hi",18:"bn",21:"ms",22:"tr"}

MOBO_PRODUCTS = frozenset({'moboreels', 'moboshort'})


def institution_id():
    import os
    from features.moboreels_library.settings import read_env_file
    config = read_env_file(os.environ.get('MOBOREELS_MACRO_ENV_FILE', '/etc/moboreels-macro.env'))
    value = os.environ.get('MOBOREELS_INSTITUTION_USER_ID', config.get('MOBOREELS_INSTITUTION_USER_ID', ''))
    if not value:
        raise ValueError('Moboreels 机构范围尚未配置')
    return value


def resolve(query, schema, request):
    value = identifier(request['id_type'], request['drama_id'])
    scope = 'd.institution_user_id=%s AND d.project_type=2 AND d.is_show=1'
    if request['id_type'] == 'resource_id':
        sql = (f'SELECT DISTINCT d.story_id,d.lang_id FROM `{schema}`.ads_drama_info i FORCE INDEX(scoo) '
               'JOIN ads_ai.cdreader_koc_drama d ON d.story_id=i.content_id '
               f'WHERE i.app_id=%s AND i.series_code=%s AND BINARY i.series_code=BINARY %s AND {scope} LIMIT 1001')
        params = (MOBO_APP_ID, value, value, institution_id())
    else:
        sql = ('SELECT d.story_id,d.lang_id FROM ads_ai.cdreader_koc_drama d '
               f'WHERE {scope} AND d.story_id=%s AND BINARY d.story_id=BINARY %s LIMIT 1001')
        params = (institution_id(), value, value)
    rows = list(query(sql, params))
    target = present_target([dict(content_id=str(row['story_id']), language=LANGUAGES.get(row['lang_id'], '')) for row in rows], request)
    return dict(target, catalog='moboreels', eligibility='provider_listed')


def classifications(query, ids):
    from features.x_tag_revenue.taxonomy import classify
    result = {}
    ids = list(dict.fromkeys(str(value) for value in ids))
    for offset in range(0, len(ids), 100):
        batch = ids[offset:offset+100]
        rows = query('SELECT story_id,lang_id,c_name,channel_text FROM ads_ai.cdreader_koc_drama '
                     'WHERE institution_user_id=%s AND project_type=2 AND is_show=1 '
                     'AND story_id IN ('+','.join('%s' for _ in batch)+')', (institution_id(), *batch))
        for row in rows:
            cid = str(row['story_id'])
            if cid not in batch:
                continue
            try:
                lang = target_language(LANGUAGES.get(row['lang_id'], ''))
            except ValueError:
                continue
            raw = [label.strip() for label in str(row.get('c_name') or '').split(',') if label.strip()]
            aliases = {'家庭亲情': 'family_affection', '萌宝': 'family'}
            metadata = classify([aliases.get(label, label) for label in raw])
            metadata['raw_labels'] = raw
            # Mobo explicitly supplies its audience channel. Never infer it from
            # genre tags or borrow an equal numeric ID from DramaWave's cache.
            channel = str(row.get('channel_text') or '').strip()
            audience = {'女频': ('female', '女性'), '男频': ('male', '男性')}.get(channel)
            metadata.update(audience=audience[0] if audience else '__unknown__',
                            audience_name=audience[1] if audience else '未标注',
                            audience_basis='provider_channel' if audience else 'missing',
                            audience_rules=['moboreels_channel_text'], audience_evidence=[channel] if channel else [],
                            audience_source='cdreader_koc_drama.channel_text', audience_fetched_at='',
                            audience_status='matched' if audience else 'missing')
            metadata.update(content_id=cid, language=lang, catalog='moboreels',
                            label_status='matched', type_source='not_used', labels_source='cdreader_koc_drama.c_name')
            result[(cid, lang)] = metadata
    return result
