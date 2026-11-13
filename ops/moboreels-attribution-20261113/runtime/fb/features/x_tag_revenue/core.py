"""Exact revenue conservation and equal allocation across distinct drama labels."""
from collections import defaultdict
from fractions import Fraction
import json
from .tags import lookup, VERSION as TAG_MAPPING_VERSION

UNKNOWN = '__unknown__'


def language(value):
    value = str(value or '').strip().lower().replace('_', '-')
    return {'jp': 'ja', 'pt': 'pt-br'}.get(value, value)


def labels(value):
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            parsed = None
        value = parsed if isinstance(parsed, list) else value.split(',')
    result = {}
    for item in value or []:
        if not isinstance(item, str):
            continue
        if item.strip():
            tag = lookup(item)
            result.setdefault(tag['id'], tag['name_zh'])
    return result


def label_index(rows):
    grouped = defaultdict(list)
    for row in rows:
        if str(row.get('app_id')) == '1479':
            grouped[(str(row['content_id']), language(row.get('language')))].append(row)
    result = {}
    for key, values in grouped.items():
        series = {str(row.get('series_code') or '') for row in values} - {''}
        merged = {}
        for row in values:
            for tag, name in labels(row.get('labels')).items():
                merged.setdefault(tag, name)
        status = 'ambiguous_drama' if len(series) > 1 else 'matched' if merged else 'missing_labels'
        result[key] = {'status': status, 'tags': merged if status == 'matched' else {}}
    return result


def rounded_parts(values, total=None):
    """Largest remainder rounding, preserving signed integer-cent totals exactly."""
    keys = sorted(values)
    floors = {key: values[key].numerator // values[key].denominator for key in keys}
    if total is None:
        total = round(sum(values.values(), Fraction()))
    remaining = total - sum(floors.values())
    if remaining < 0 or remaining > len(keys):
        raise ValueError('allocation_not_conserved')
    order = sorted(keys, key=lambda key: (-(values[key] - floors[key]), key))
    for key in order[:remaining]:
        floors[key] += 1
    return floors


def build(catalog, metrics, resources, end, drama_info=None):
    # Retain the existing global campaign identity and publication boundary.
    from features.social_analytics.report import campaign_index, resolve
    from features.social_analytics.backend_scope import eligible
    index = campaign_index([r for r in catalog if r['platform'] == 'x'])
    tags = label_index(resources)
    from .taxonomy import classification_index
    from .hierarchy import build_tree
    classifications = classification_index(resources, drama_info or [])
    channels = {}
    unresolved = 0
    unresolved_rows = 0

    def bucket(channel):
        return channels.setdefault(channel, {'dramas': {}, 'posts': set(), 'incomplete': False})

    resolved = {}
    for key, candidates in index.items():
        row = resolve(candidates)
        if not row:
            for candidate in candidates:
                if candidate.get('channel'):
                    bucket(candidate['channel'])['incomplete'] = True
            continue
        if not any(eligible(candidate, end) for candidate in candidates):
            continue
        resolved[key] = row
        channel = bucket(row['channel'])
        drama_key = (str(row.get('drama') or ''), language(row.get('language')))
        drama = channel['dramas'].setdefault(drama_key, {
            'content_id': drama_key[0], 'language': drama_key[1],
            'name': row.get('drama_label') or drama_key[0] or '未记录剧目',
            'revenue_cents': 0, 'posts': set(),
        })
        for candidate in candidates:
            for fact in candidate.get('publications', []):
                if fact['date'] <= end:
                    channel['posts'].add(fact['id'])
                    drama['posts'].add(fact['id'])

    total = 0
    for metric in metrics:
        if metric['platform'] != 'x' or metric['date'] > end:
            continue
        cents = metric['revenue_cents']
        if type(cents) is not int:
            raise ValueError('invalid_revenue_cents')
        total += cents
        key = ('x', str(metric['site_id']), str(metric['campaign_id']))
        row = resolved.get(key)
        if row is None or not any(eligible(r, metric['date']) for r in index[key]):
            unresolved += cents
            unresolved_rows += 1
            if row:
                bucket(row['channel'])['incomplete'] = True
            continue
        drama_key = (str(row.get('drama') or ''), language(row.get('language')))
        bucket(row['channel'])['dramas'][drama_key]['revenue_cents'] += cents

    output = {}
    for channel_id, channel in channels.items():
        amounts = defaultdict(Fraction)
        detail = defaultdict(dict)
        names = {}
        channel_total = sum(d['revenue_cents'] for d in channel['dramas'].values())
        unknown = 0
        for key, drama in channel['dramas'].items():
            metadata = tags.get(key, {'status': 'missing_drama', 'tags': {}})
            selected = metadata['tags'] or {UNKNOWN: '未识别标签'}
            if not metadata['tags']:
                unknown += drama['revenue_cents']
            part = Fraction(drama['revenue_cents'], len(selected))
            for tag, name in selected.items():
                names.setdefault(tag, name)
                amounts[tag] += part
                detail[tag][key] = {
                    **{k: v for k, v in drama.items() if k != 'posts'},
                    'published_count': len(drama['posts']), 'fraction': part,
                    'label_count': len(metadata['tags']), 'mapping_status': metadata['status'],
                    'labels': list(metadata['tags'].values()),
                }
        cents_by_tag = rounded_parts(amounts, channel_total)
        unknown = sum(value for key, value in cents_by_tag.items() if not lookup(key)['matchable'])
        meaningful_share = channel_total > 0 and all(value >= 0 for value in amounts.values())
        shares = rounded_parts({key: value * 10000 / channel_total for key, value in amounts.items()}, 10000) if meaningful_share else {}
        tag_rows = []
        for tag, value in amounts.items():
            parts = rounded_parts({key: row['fraction'] for key, row in detail[tag].items()}, cents_by_tag[tag])
            dramas = []
            for key, row in detail[tag].items():
                dramas.append({k: v for k, v in row.items() if k != 'fraction'} | {'allocated_cents': parts[key]})
            dramas.sort(key=lambda r: (-r['allocated_cents'], r['content_id'], r['language']))
            post_ids = set().union(*(channel['dramas'][key]['posts'] for key in detail[tag]))
            tag_rows.append(dict(key=tag, name=names[tag], mapping_status=lookup(tag)['status'],
                                 matchable=lookup(tag)['matchable'], revenue_cents=cents_by_tag[tag],
                                 share_bp=shares.get(tag), drama_count=len(dramas),
                                 published_count=len(post_ids), dramas=dramas))
        tag_rows.sort(key=lambda r: (-r['revenue_cents'], r['key']))
        output[channel_id.removeprefix('x:')] = dict(
            revenue_cents=channel_total, unknown_revenue_cents=unknown,
            known_share_bp=round(Fraction(channel_total - unknown, channel_total) * 10000) if meaningful_share else None,
            published_count=len(channel['posts']), drama_count=len(channel['dramas']),
            status='partial' if channel['incomplete'] else 'complete', tags=tag_rows)
        output[channel_id.removeprefix('x:')].update(build_tree(channel['dramas'], classifications))
    included = sum(row['revenue_cents'] for row in output.values())
    if included + unresolved != total:
        raise ValueError('channel_revenue_not_conserved')
    return dict(accounts=output, mapping_version=TAG_MAPPING_VERSION, audit=dict(source_revenue_cents=total, included_revenue_cents=included,
                                          unassigned_revenue_cents=unresolved, unassigned_rows=unresolved_rows))
