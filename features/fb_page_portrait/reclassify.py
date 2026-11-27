"""Reclassify frozen HotDrama facts without changing revenue, scope or dates."""
from .runtime import shared
shared()
from features.x_tag_revenue.hierarchy import build_tree

MONEY = ('paid_cents', 'social_cents', 'spend_cents', 'revenue_cents')


def facts(profile):
    dramas, classifications = {}, {}
    for audience in profile['audiences']:
        for theme in audience['children']:
            for row in theme.get('dramas', []):
                key = (int(row.get('app_id', 1479)), row['content_id'], row['language'])
                drama = {k: v for k, v in row.items()
                         if k not in ('allocated_cents', 'published_count', 'classification')}
                drama['posts'] = {(key, i) for i in range(row['published_count'])}
                if key in dramas and (dramas[key] != drama or classifications[key] != row['classification']):
                    raise ValueError('frozen_drama_facts_conflict')
                dramas[key], classifications[key] = drama, row['classification']
    if len(dramas) != profile['drama_count'] or any(
            sum(d[field] for d in dramas.values()) != profile[field] for field in MONEY):
        raise ValueError('frozen_drama_totals_mismatch')
    return dramas, classifications


def reclassify(profile, replacements):
    dramas, classified = facts(profile)
    changed = False
    for key in dramas:
        replacement = replacements.get(key)
        if key[0] == 3302 and replacement and replacement.get('classification_mapping'):
            if classified[key] != replacement:
                classified[key] = replacement
                changed = True
    if not changed:
        return profile
    tree = build_tree(dramas, classified)
    valid = profile['revenue_cents'] > 0 and all(d['revenue_cents'] >= 0 for d in dramas.values())
    if not valid:
        for audience in tree['audiences']:
            audience['share_bp'] = None
            for theme in audience['children']:
                theme['share_bp'] = theme['channel_share_bp'] = None
        for combination in tree['combinations']:
            combination['channel_share_bp'] = None
    primary = tree['audiences'][0] if valid and tree['audiences'] else {}
    simple = lambda n: {k: v for k, v in n.items() if k not in ('children', 'dramas')}
    result = dict(profile, **tree)
    result['focus'] = dict(audience=simple(primary), themes=[simple(t) for t in primary.get('children', [])[:3]])
    # Duplicate theme leaves are never summed back into financial facts.
    recovered, _ = facts(result)
    if recovered != dramas:
        raise ValueError('reclassification_changed_business_facts')
    return result
