"""Seven report days; paid D0 + confirmed social income, conserved to cents."""
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from .runtime import shared
shared()
from features.x_tag_revenue.core import language
from features.x_tag_revenue.taxonomy import classification_index, VERSION as TAXONOMY_VERSION
from features.x_tag_revenue.hierarchy import build_tree, VERSION as HIERARCHY_VERSION


def cents(value):
    return int((Decimal(str(value or 0))*100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def build(raw):
    assert (date.fromisoformat(raw['end'])-date.fromisoformat(raw['start'])).days == 6
    scope = sorted(set(raw['page_ids']))
    assert scope and len(scope) <= 10000
    classifications = classification_index(raw['resources'], raw['infos'])
    names = {(str(r['content_id']), language(r['language'])): r.get('name') or str(r['content_id']) for r in raw['infos']}
    grouped = defaultdict(dict)
    days = defaultdict(set)
    audit = dict(paid_cents=0, social_cents=0, spend_cents=0)
    for kind in ('paid', 'social'):
        for r in raw[kind]:
            pid = str(r['page_id'])
            assert pid in scope and raw['start'] <= r['date'] <= raw['end']
            key = (str(r.get('content_id') or '__missing__'), language(r.get('language')))
            value = grouped[pid].setdefault(key, dict(content_id=key[0], language=key[1], name=names.get(key) or key[0],
                     revenue_cents=0, paid_cents=0, social_cents=0, spend_cents=0, posts=set()))
            income = cents(r['revenue']) if kind == 'paid' else int(r['revenue_cents'])
            spend = cents(r.get('spend')) if kind == 'paid' else 0
            value[kind+'_cents'] += income
            value['revenue_cents'] += income
            value['spend_cents'] += spend
            value['posts'].update(r.get('posts', []))
            audit[kind+'_cents'] += income
            audit['spend_cents'] += spend
            if income or spend: days[pid].add(r['date'])
    pages = {}
    for pid in scope:
        dramas = {k:v for k,v in grouped[pid].items() if v['revenue_cents'] or v['spend_cents'] or v['paid_cents'] or v['social_cents']}
        total = {field: sum(r[field] for r in dramas.values()) for field in ('revenue_cents','paid_cents','social_cents','spend_cents')}
        assert total['revenue_cents'] == total['paid_cents'] + total['social_cents']
        # Signed source corrections remain visible. Negative drama income is not
        # silently clamped into a fabricated positive audience percentage.
        valid_shares = total['revenue_cents'] > 0 and all(r['revenue_cents'] >= 0 for r in dramas.values())
        tree = build_tree(dramas, classifications)
        if not valid_shares:
            for a in tree['audiences']:
                a['share_bp'] = None
                for t in a['children']: t['share_bp'] = t['channel_share_bp'] = None
            for c in tree['combinations']: c['channel_share_bp'] = None
        status = 'ready' if valid_shares else 'signed_adjustment' if any(r['revenue_cents'] < 0 for r in dramas.values()) else 'no_income'
        primary = tree['audiences'][0] if valid_shares and tree['audiences'] else {}
        simple = lambda node: {k:v for k,v in node.items() if k not in ('children','dramas')}
        pages[pid] = dict(page_id=pid, **total, **tree, status=status, drama_count=len(dramas), active_days=len(days[pid]),
                         focus=dict(audience=simple(primary), themes=[simple(t) for t in primary.get('children',[])[:3]]))
    assert all(sum(p[k] for p in pages.values()) == v for k,v in audit.items())
    generated = datetime.now(timezone.utc)
    return dict(meta=dict(version=generated.strftime('%Y%m%dT%H%M%S%fZ'), generated_at=generated.isoformat(),
                start=raw['start'], end=raw['end'], history_lookup=False, currency='USD',
                paid_metric='af_revenue0', social_scope='ai_backend_confirmed_publications',
                taxonomy_version=TAXONOMY_VERSION, hierarchy_version=HIERARCHY_VERSION,
                social_version=raw['social_version'], page_count=len(pages), audit=audit), pages=pages)
