"""Keep product identities separate while sharing the current theme taxonomy."""
from collections import defaultdict
from .runtime import shared
shared()
from features.x_tag_revenue.core import language
from features.x_tag_revenue.taxonomy import classification_index, classify, raw_labels, UNKNOWN
from .same_drama import verified_hotdrama_links, matching_resources

PRODUCTS = {1479: 'Dramawave', 979: 'FreeReels', 3302: 'HotDrama'}


def product_id(row):
    value = int(row.get('app_id', 1479))
    if value not in PRODUCTS:
        raise ValueError('unsupported_portrait_product')
    return value


def classifications(resources, infos):
    result = {(1479, cid, lang): value for (cid, lang), value in classification_index(
        [r for r in resources if product_id(r) == 1479],
        [r for r in infos if product_id(r) == 1479]).items()}
    links = verified_hotdrama_links(infos)
    donor_resources = defaultdict(list)
    for row in resources:
        if product_id(row) == 1479:
            donor_resources[(str(row['content_id']), language(row.get('language')))].append(row)
    grouped, types = defaultdict(list), defaultdict(list)
    for rows, target in ((resources, grouped), (infos, types)):
        for row in rows:
            app_id = product_id(row)
            if app_id != 1479:
                target[(app_id, str(row['content_id']), language(row.get('language')))].append(row)
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
        link = links.get(key)
        donor = result.get((1479, key[1], key[2])) if link else None
        borrowed_labels = False
        if link and not raw and len(resource_series) <= 1 and resource_series <= {link['series_code']}:
            rows = matching_resources(donor_resources[(key[1], key[2])], link['series_code'])
            raw = [tag for row in rows for tag in raw_labels(row.get('labels'))]
            borrowed_labels = bool(raw)
        # Preserve product revenue and identity. Only an exact, unambiguous
        # content/language/series mapping may supply the donor's classification.
        item = classify(raw, dtype, status=status, info_values=sorted(values),
                        audience_record=None, expected_language=key[2])
        if donor and donor['audience'] != UNKNOWN:
            for field in ('audience', 'audience_name', 'audience_basis', 'audience_rules',
                          'audience_evidence', 'audience_source', 'audience_fetched_at', 'audience_status'):
                item[field] = donor[field]
        if link and (borrowed_labels or (donor and donor['audience'] != UNKNOWN)):
            item['classification_mapping'] = dict(link, target_app_id=key[0],
                                                  labels=borrowed_labels,
                                                  audience=bool(donor and donor['audience'] != UNKNOWN))
        item.update(app_id=key[0], content_id=key[1], language=key[2],
                    label_status='ambiguous_drama' if len(resource_series) > 1 else 'matched' if raw else 'missing_labels')
        result[key] = item
    return result


def unknown(key, audience_record=None):
    item = classify([], status='missing_drama_info', audience_record=audience_record if key[0] == 1479 else None, expected_language=key[2])
    item.update(app_id=key[0], content_id=key[1], language=key[2])
    return item
