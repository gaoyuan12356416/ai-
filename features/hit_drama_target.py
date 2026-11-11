"""Resolve business resource codes into product-scoped, language-specific dramas."""
import re
from features.x_accounts.language import canonical_drama_language


def target_language(value):
    """Match the catalog's pt and account/portrait pt-br without rewriting either."""
    value = canonical_drama_language(value, default='')
    return 'pt-br' if value == 'pt' else value


def identifier(id_type, value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError('请输入一个有效的剧 ID 或资源 ID')
    value = str(value).strip()
    pattern = r'[1-9][0-9]{0,30}' if id_type == 'resource_id' else r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}'
    if id_type not in ('content_id', 'resource_id') or not re.fullmatch(pattern, value):
        raise ValueError('资源 ID 须为正整数；剧 ID 支持字母、数字、下划线和短横线')
    return value


def resolve_target(query, schema, app_id, request):
    value = identifier(request['id_type'], request['drama_id'])
    resource = request['id_type'] == 'resource_id'
    column, index = ('series_code', 'scoo') if resource else ('content_id', 'ac')
    # Use the indexed business identifier; never the episode-record primary key.
    # The binary predicate preserves content-ID case with case-insensitive MySQL collations.
    rows = list(query(f'SELECT DISTINCT content_id,language FROM `{schema}`.ads_drama_info FORCE INDEX({index}) '
                      f'WHERE app_id=%s AND {column}=%s AND BINARY {column}=BINARY %s LIMIT 1001',
                      (app_id, value, value)))
    if not rows:
        raise ValueError('资源 ID 未关联到目标产品下的剧目' if resource else '目标产品下未找到该剧 ID')
    if len(rows) >= 1001:
        raise ValueError('该资源的剧目版本过多，未完整解析，请改用具体剧 ID')
    by_language = {}
    for row in rows:
        content_id = str(row.get('content_id') or '').strip()
        if not content_id:
            continue
        if not resource and content_id != value:
            continue
        try:
            lang = target_language(row.get('language'))
        except ValueError:
            continue
        by_language.setdefault(lang, set()).add(content_id)
    if not by_language:
        raise ValueError('目标产品下的剧目缺少有效语言信息')
    by_language = {lang: sorted(ids) for lang, ids in sorted(by_language.items())}
    ids = sorted({content_id for values in by_language.values() for content_id in values})
    return dict(version=2, content_id=ids[0] if len(ids) == 1 else '', content_ids=ids,
                by_language=by_language, languages=list(by_language),
                series_code=value if resource else '')


def content_ids(target, language=None):
    """Read both new multilingual targets and already-frozen version-one runs."""
    if 'by_language' in target and language is not None:
        # Older frozen runs can still have pt keys. Canonicalize at lookup too,
        # and never infer equivalence with pt-pt or other regional variants.
        wanted = target_language(language)
        return sorted({cid for key, ids in target['by_language'].items()
                       if target_language(key) == wanted for cid in ids})
    if language is not None and target.get('languages') and target_language(language) not in {target_language(v) for v in target['languages']}:
        return []
    return list(target.get('content_ids') or ([target['content_id']] if target.get('content_id') else []))
