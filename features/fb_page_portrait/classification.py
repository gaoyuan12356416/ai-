"""Keep product identities separate while sharing the current theme taxonomy."""
from collections import defaultdict
from .runtime import shared
shared()
from features.x_tag_revenue.core import language
from features.x_tag_revenue.taxonomy import classification_index, classify, raw_labels, UNKNOWN

PRODUCTS = {1479: 'Dramawave', 979: 'FreeReels'}


def product_id(row):
    value = int(row.get('app_id', 1479))
    if value not in PRODUCTS:
        raise ValueError('unsupported_portrait_product')
    return value


def classifications(resources, infos):
    result = {(1479, cid, lang): value for (cid, lang), value in classification_index(
        [r for r in resources if product_id(r) == 1479],
        [r for r in infos if product_id(r) == 1479]).items()}
    grouped, types = defaultdict(list), defaultdict(list)
    for rows, target in ((resources, grouped), (infos, types)):
        for row in rows:
            if product_id(row) == 979:
                target[(979, str(row['content_id']), language(row.get('language')))].append(row)
    for key in set(grouped) | set(types):
        info = types[key]
        values = {str(r.get('resource_type')) for r in info}
        series = {str(r.get('series_code') or '') for r in info} - {''}
        value = next(iter(values)) if len(values) == 1 and len(series) <= 1 else None
        dtype = {'1': 'local', '2': 'translated'}.get(value, UNKNOWN)
        status = ('missing_drama_info' if not info else 'ambiguous_drama_info'
                  if len(values) > 1 or len(series) > 1 else 'unlabeled_type' if dtype == UNKNOWN else 'matched')
        resource_series = {str(r.get('series_code') or '') for r in grouped[key]} - {''}
        raw = [tag for r in grouped[key] for tag in raw_labels(r.get('labels'))] if len(resource_series) <= 1 else []
        # The existing authoritative audience catalog is DramaWave-only. Do not
        # look up a FreeReels content ID against that unrelated product catalog.
        item = classify(raw, dtype, status=status, info_values=sorted(values),
                        audience_record=None, expected_language=key[2])
        item.update(app_id=979, content_id=key[1], language=key[2],
                    label_status='ambiguous_drama' if len(resource_series) > 1 else 'matched' if raw else 'missing_labels')
        result[key] = item
    return result


def unknown(key, audience_record=None):
    item = classify([], status='missing_drama_info', audience_record=audience_record if key[0] == 1479 else None, expected_language=key[2])
    item.update(app_id=key[0], content_id=key[1], language=key[2])
    return item
