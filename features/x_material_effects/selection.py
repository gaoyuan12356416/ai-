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

@contextmanager
def scope(request):
    snapshot=None
    if enabled(request.rules):
        try:
            meta,path=store.current()
            posts=[p for p in store.rows(path,'posts') if p['language']==request.language and
                   p['product'].casefold() in {v.casefold() for v in request.rules.material_products}]
            snapshot=dict(meta=meta,material={(r['namespace'],r['material_id']):r for r in aggregate(posts)},
                          drama={(r['catalog'],r['content_id'],r['language']):r for r in aggregate(posts,level='drama')})
        except (OSError,ValueError,KeyError):
            from features.x_auto_posts.selector import SelectionError
            raise SelectionError('x_material_effect_unavailable','X 素材效果尚未就绪，请稍后重试',503) from None
    token=ACTIVE.set(snapshot)
    try:yield
    finally:ACTIVE.reset(token)

def facts(candidate,level):
    snapshot=ACTIVE.get()
    if not snapshot:return {}
    key=(candidate.catalog,candidate.content_id,candidate.language) if level=='drama' else ('ads_custom_source',str(candidate.source.material_id))
    return snapshot[level].get(key,{})

def value(row,metric):
    raw=row.get('revenue_cents' if metric=='revenue' else metric)
    if raw is None:return None
    return Decimal(raw)/100 if metric=='revenue' else Decimal(raw)

def apply(candidates,rule,level,rejections):
    if not ACTIVE.get():return candidates
    output=[]
    for item in candidates:
        data=facts(item,level)
        rejected=False
        for metric,bounds in rule.x_effect.items():
            if not bounds.contains(value(data,metric)):
                key=level+'_x_'+metric;rejections[key]=rejections.get(key,0)+1;rejected=True;break
        if not rejected:output.append(item)
    if rule.sort.field.startswith('x_'):
        metric=rule.sort.field[2:]
        identity=lambda c:c.content_id if level=='drama' else str(c.source.material_id)
        output.sort(key=identity)
        known=[c for c in output if value(facts(c,level),metric) is not None]
        missing=[c for c in output if value(facts(c,level),metric) is None]
        known.sort(key=lambda c:value(facts(c,level),metric),reverse=rule.sort.direction=='desc')
        output=known+missing
    return output

def audit(drama,material):
    snap=ACTIVE.get()
    return {} if not snap else dict(policy=POLICY,version=snap['meta']['version'],
        generated_at=snap['meta']['generated_at'],drama=facts(drama,'drama'),material=facts(material,'material'))
