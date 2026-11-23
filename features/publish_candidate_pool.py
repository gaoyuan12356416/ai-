"""A submitted list restricts one template run; it never expands its targets."""
from features.hit_drama_target import identifier, content_ids
from features.hit_publish import material_ids

POLICY = 'template-candidate-pool-v1'
MARKER = 'candidate_pool'


def normalize(value):
    if not isinstance(value, dict) or value.get('kind') not in ('drama', 'material'):
        raise ValueError('候选池类型无效')
    kind = value['kind']
    if set(value) != ({'kind', 'ids', 'id_type'} if kind == 'drama' else {'kind', 'ids'}):
        raise ValueError('候选池字段无效')
    ids = value.get('ids')
    if not isinstance(ids, list) or not 1 <= len(ids) <= 200:
        raise ValueError('每次请输入 1–200 个 ID')
    if kind == 'material':
        return dict(kind=kind, ids=material_ids(ids))
    return dict(kind=kind, id_type=value['id_type'],
                ids=list(dict.fromkeys(identifier(value['id_type'], item) for item in ids)))


def resolve(pool, resolve_drama, query, schema, products):
    """Freeze only identity boundaries. All eligibility stays with the template."""
    targets = []
    if pool['kind'] == 'drama':
        for value in pool['ids']:
            targets.append(resolve_drama(dict(id_type=pool['id_type'], drama_id=value)))
    else:
        marks = ','.join('%s' for _ in pool['ids'])
        rows = query(f'SELECT CAST(id AS CHAR) material_id,TRIM(data_source_id) content_id,product '
                     f'FROM `{schema}`.ads_custom_source WHERE id IN ({marks})', tuple(pool['ids']))
        allowed_products = {p.casefold() for p in products}
        from features.moboreels_hit import MOBO_PRODUCTS
        groups = {}
        for row in rows:
            if str(row.get('material_id')) not in pool['ids'] or str(row.get('product', '')).casefold() not in allowed_products:
                continue
            cid = str(row.get('content_id') or '').strip()
            if cid:
                catalog = 'moboreels' if str(row['product']).casefold() in MOBO_PRODUCTS else 'dramawave'
                groups.setdefault(catalog, set()).add(cid)
        targets = [dict(catalog=catalog, content_ids=sorted(ids)) for catalog, ids in groups.items()]
    if not targets or not any(content_ids(t) for t in targets):
        raise ValueError('所选产品中没有可识别的候选内容')
    if len({(t.get('catalog', 'dramawave'), cid) for t in targets for cid in content_ids(t)}) > 5000:
        raise ValueError('候选剧目版本超过 5000 个，请缩小本次范围')
    return dict(policy=POLICY, pool=pool, targets=targets)


def allowed_content(pool, language=None, catalog=None):
    return sorted({cid for t in pool['targets']
                   if catalog is None or t.get('catalog', 'dramawave') == catalog
                   for cid in content_ids(t, language)})


def allows(pool, content_id, language=None, catalog=None, material_id=None):
    return (str(content_id) in allowed_content(pool, language, catalog)
            and (material_id is None or pool['pool']['kind'] != 'material'
                 or str(material_id) in pool['pool']['ids']))
