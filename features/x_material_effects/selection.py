"""Request-local, immutable cumulative effects used by both X selectors."""
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from . import store
from .model import FILTERS,aggregate

ACTIVE=ContextVar('x_effect_snapshot',default=None)
POLICY='x-effects-other-accounts-v1'

def enabled_rule(rule):
    return str(rule.sort.field).startswith('x_') or any(r.constrained for r in rule.x_effect.values())

def enabled(rules):return enabled_rule(rules.drama) or enabled_rule(rules.material)

def config_enabled(config):
    return any(str(config.get(level,{}).get('sort_by','')).startswith('x_') or
               any(config.get(level,{}).get('x_'+m+'_'+bound) not in (None,'') for m in FILTERS for bound in ('min','max'))
               for level in ('drama_rule','material_rule'))

def x_snapshot(request, inputs):
    meta,path = store.current()
    products = {p.casefold() for p in getattr(request.rules,'material_products',())}
    rows = [p for p in store.rows(path,'posts') if p['language']==request.language
            and (not products or p['product'].casefold() in products)]
    return dict(meta=dict(meta,versions={'x':meta['version']}),channels=inputs,legacy=True,
                material={(r['namespace'],r['material_id']):r for r in aggregate(rows)},
                drama={(r['catalog'],r['content_id'],r['language']):r for r in aggregate(rows,level='drama')})


@contextmanager
def scope(request):
    snapshot=None
    if enabled(request.rules):
        try:
            import sqlite3
            from features.social_material_effects.selection import load
            products = getattr(request.rules,'material_products',()) or ()
            inputs = {level:getattr(request.rules,level).effect_channel for level in ('drama','material')
                      if enabled_rule(getattr(request.rules,level))}
            if set(inputs.values()) == {'x'}:
                snapshot = x_snapshot(request,inputs)
            else:
                for attempt in range(2):
                    versions, timestamps = {}, []
                    snapshot = dict(drama={},material={},channels=inputs)
                    for level,source in inputs.items():
                        values, revision, stamp = load(level,source,request.language,products)
                        if any(k in versions and versions[k]!=v for k,v in revision.items()):
                            break
                        snapshot[level] = values
                        versions.update(revision)
                        timestamps.append(stamp)
                    else:
                        snapshot['meta'] = dict(version='|'.join(versions[k] for k in sorted(versions)),
                                                versions=versions,generated_at=min(timestamps))
                        break
                else:
                    raise ValueError('social_effect_generation_changed')
        except (OSError,ValueError,KeyError,sqlite3.Error):
            from features.x_auto_posts.selector import SelectionError
            raise SelectionError('x_material_effect_unavailable','所选社媒渠道的素材效果尚未就绪，请稍后重试',503) from None
    token=ACTIVE.set(snapshot)
    try:yield
    finally:ACTIVE.reset(token)

def facts(candidate,level):
    snapshot=ACTIVE.get()
    if not snapshot:return {}
    if snapshot.get('legacy') or 'channels' not in snapshot:
        key = ((candidate.catalog,candidate.content_id,candidate.language) if level=='drama' else
               ('ads_custom_source',str(candidate.source.material_id)))
        return snapshot[level].get(key,{})
    from features.social_material_effects.selection import evidence
    if level=='drama':
        key=(candidate.catalog,candidate.content_id)
    else:
        catalog = 'moboreels' if candidate.source.product.casefold() in ('moboreels','moboshort') else 'dramawave'
        key=(catalog,candidate.source.content_id,'ads_custom_source',str(candidate.source.material_id))
    return evidence(snapshot[level].get(key))


def value(row,metric):
    raw=row.get('revenue_cents' if metric=='revenue' else metric)
    if raw is None:return None
    return Decimal(raw)/100 if metric=='revenue' else Decimal(raw)

def observed_value(row, metric):
    complete = value(row, metric)
    return complete if complete is not None else value(row.get('known', {}), metric)

def matches(row, metric, bounds):
    complete = value(row, metric)
    if complete is not None or not bounds.constrained:
        return bounds.contains(complete)
    # A measured subtotal proves a minimum for additive, nonnegative metrics.
    # Missing posts cannot prove an upper bound and are never treated as zero.
    if bounds.maximum is not None:
        return False
    return bounds.contains(observed_value(row, metric))

def apply(candidates,rule,level,rejections):
    if not ACTIVE.get():return candidates
    output=[]
    for item in candidates:
        data=facts(item,level)
        rejected=False
        for metric,bounds in rule.x_effect.items():
            if not matches(data,metric,bounds):
                key=level+'_x_'+metric;rejections[key]=rejections.get(key,0)+1;rejected=True;break
        if not rejected:output.append(item)
    if rule.sort.field.startswith('x_'):
        metric=rule.sort.field[2:]
        identity=lambda c:c.content_id if level=='drama' else str(c.source.material_id)
        output.sort(key=identity)
        known=[c for c in output if observed_value(facts(c,level),metric) is not None]
        missing=[c for c in output if observed_value(facts(c,level),metric) is None]
        known.sort(key=lambda c:observed_value(facts(c,level),metric),reverse=rule.sort.direction=='desc')
        output=known+missing
    return output

def audit(drama,material):
    snap=ACTIVE.get()
    return {} if not snap else dict(policy=POLICY,version=snap['meta']['version'],
        channels=snap['channels'], versions=snap['meta']['versions'],
        generated_at=snap['meta']['generated_at'],drama=facts(drama,'drama'),material=facts(material,'material'))
