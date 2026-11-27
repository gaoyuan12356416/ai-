"""Read candidate evidence from a pinned channel generation before top-N selection."""
from decimal import Decimal
from functools import lru_cache
import sqlite3
from . import data, window

METRICS = tuple('revenue_cents' if k=='revenue' else k for k in data.FILTERS)


def normal_language(code):
    text = str(code or '').strip().lower().replace('_','-')
    return {'jp':'ja','fil':'tl'}.get(text,text)


def load(level, source, language, products, root=None, *, dates=None):
    root = data.root_path(root)
    sources = data.channels(source)
    # Hold immutable file handles while reading their metadata and rows. The
    # stat fingerprint is only a cache discriminator for the next reader.
    files = tuple((p.stat().st_ino,p.stat().st_mtime_ns) for p in (root/(s+'.sqlite3') for s in sources))
    return _load(str(root),files,level,sources,normal_language(language),tuple(sorted(v.lower() for v in products)), dates)


@lru_cache(maxsize=12)
def _load(root, files, level, sources, language, products, dates=None):
    with data.reading(sources,root) as (db,meta):
        if dates is not None:
            facts = {}
            for source in sources:
                for key, values in window.totals(db,source,level,language,products,*dates).items():
                    total = facts.setdefault(key,[0]+[None,0]*len(METRICS))
                    total[0] += values[0]
                    for i in range(len(METRICS)):
                        value, n = values[1+2*i:3+2*i]
                        if value is not None:
                            total[1+2*i] = (total[1+2*i] or 0)+value
                        total[2+2*i] += n
            return facts, {s:m['version'] for s,m in meta.items()}, min(m['generated_at'] for m in meta.values())
        aliases = {'ja':('ja','jp'),'tl':('tl','fil')}.get(language,(language,))
        where = 'lower(language) IN (' + ','.join('?' for _ in aliases) + ')'
        args = list(aliases)
        if products:
            where += ' AND product COLLATE NOCASE IN ('+ ','.join('?' for _ in products) + ')'
            args.extend(products)
        fields = 'catalog,content_id' + (',namespace,material_id' if level=='material' else '')
        q = ' UNION ALL '.join(f'SELECT * FROM {s}.rollup WHERE {where}' for s in sources)
        totals = ','.join(f'SUM({k}),SUM(n_{k})' for k in METRICS)
        count = len(fields.split(','))
        facts = {tuple(row[:count]):tuple(row[count:]) for row in db.execute(
            f'SELECT {fields},SUM(posts),{totals} FROM ({q}) GROUP BY {fields}', args*len(sources))}
        return facts, {s:m['version'] for s,m in meta.items()}, min(m['generated_at'] for m in meta.values())


def evidence(record):
    if not record:
        return {}
    result = dict(post_count=record[0],known={},coverage={})
    for i,metric in enumerate(METRICS):
        known, coverage = record[1+2*i:3+2*i]
        result[metric] = known if coverage==record[0] else None
        result['known'][metric], result['coverage'][metric] = known, coverage
    return result


def active(rule):
    return str(rule.get('sort_by','')).startswith('x_') or any(rule.get('x_'+m+'_'+bound) not in (None,'')
            for m in data.FILTERS for bound in ('min','max'))


class FacebookRules:
    def __init__(self, config, language, catalog, now=None):
        from features.material_products import selected
        self.rules = {}
        self.versions = {}
        self.catalog = catalog
        self.dates = window.bounds(config.get('metric_window_days',7),now)
        for level in ('drama','material'):
            rule = config.get(level+'_rule',{})
            if not active(rule):
                continue
            source = rule.get('effect_channel','facebook')
            index, version, _ = load(level,source,language,selected(config),dates=self.dates)
            limits = []
            for i,metric in enumerate(data.FILTERS):
                low,high = (rule.get('x_'+metric+'_'+k) for k in ('min','max'))
                if low not in (None,'') or high not in (None,''):
                    scale = 100 if metric=='revenue' else 1
                    limits.append((i,None if low in (None,'') else Decimal(str(low))*scale,
                                   None if high in (None,'') else Decimal(str(high))*scale))
            order = rule['sort_by']
            self.rules[level] = (index, limits, data.FILTERS.index(order[2:]) if order.startswith('x_') else None,
                                 rule['sort_direction']=='desc', source)
            self.versions.update(version)

    def record(self,level,cid,mid=None):
        index = self.rules[level][0]
        return index.get((self.catalog,cid,'ads_custom_source',str(mid)) if level=='material' else (self.catalog,cid))

    def matches(self,level,cid,mid=None):
        rule = self.rules.get(level)
        if not rule or not rule[1]:
            return True
        row = self.record(level,cid,mid)
        if not row:
            return False
        for i,low,high in rule[1]:
            value,n = row[1+2*i:3+2*i]
            if value is None or (low is not None and value<low) or (high is not None and (n!=row[0] or value>high)):
                return False
        return True

    def sorts(self,level):
        rule = self.rules.get(level)
        return rule is not None and rule[2] is not None

    def rank(self,level,cid,mid=None):
        rule = self.rules[level]
        row = self.record(level,cid,mid)
        value = row[1+2*rule[2]] if row else None
        return (value is None, 0 if value is None else -value if rule[3] else value)

    def audit(self,cid,mid):
        if not self.rules:
            return {}
        return dict(versions=self.versions, window=window.description(*self.dates), **{level:dict(channel=rule[4],
                    facts=evidence(self.record(level,cid,mid))) for level,rule in self.rules.items()})


def facebook_rules(config, language, catalog='dramawave', *, now=None):
    try:
        return FacebookRules(config,language,catalog,now)
    except (OSError, sqlite3.Error) as exc:
        from features.fb_auto_posts.repositories import RepositoryError
        raise RepositoryError('fb_social_effect_unavailable','所选社媒效果快照暂不可用',503) from exc
