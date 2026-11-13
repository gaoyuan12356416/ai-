"""Conserved conditional income trees, rebuilt from drama revenue, not old tags."""
from collections import defaultdict, Counter
from fractions import Fraction

from .core import rounded_parts
from .taxonomy import UNKNOWN, AUDIENCES, THEMES, classify
from features.drama_audience.store import records as audience_records

VERSION = 'x-audience-theme-20261110-v2'


def build_tree(dramas, classifications):
    leaves=defaultdict(dict)
    metadata={}
    missing=audience_records([key[0] for key in dramas if key not in classifications])
    for key,drama in dramas.items():
        data=classifications.get(key) or classify([],status='missing_drama_info',
                            audience_record=missing.get(key[0]),expected_language=key[1])
        metadata[key]=data
        themes=data['themes']
        part=Fraction(drama['revenue_cents'],len(themes))
        for theme in themes:
            leaves[(data['audience'],theme)][key]=part
    amounts={path:sum(parts.values(),Fraction()) for path,parts in leaves.items()}
    total=sum(d['revenue_cents'] for d in dramas.values())
    if sum(amounts.values(),Fraction())!=total:raise ValueError('hierarchy_total_not_conserved')

    def shares(values):
        parent=sum(values.values(),Fraction())
        return rounded_parts({k:v*10000/parent for k,v in values.items()},10000) if parent>0 and all(v>=0 for v in values.values()) else {}

    global_shares=shares(amounts)
    combinations=[]

    def nodes(prefix, parent_cents):
        level=len(prefix)
        names=(AUDIENCES,THEMES)[level]
        values=defaultdict(Fraction)
        for path,value in amounts.items():
            if path[:level]==prefix:values[path[level]]+=value
        cents=rounded_parts(values,parent_cents)
        proportions=shares(values)
        result=[]
        for key in values:
            path=prefix+(key,)
            drama_keys={drama_key for leaf,parts in leaves.items() if leaf[:level+1]==path for drama_key in parts}
            posts=set().union(*(dramas[k]['posts'] for k in drama_keys))
            node=dict(key=key,name=names[key],level=level+1,revenue_cents=cents[key],share_bp=proportions.get(key),
                      matchable=key!=UNKNOWN,drama_count=len(drama_keys),published_count=len(posts))
            if level<1:
                node['children']=nodes(path,cents[key])
                if sum(c['revenue_cents'] for c in node['children'])!=cents[key]:raise ValueError('hierarchy_children_not_conserved')
            else:
                parts=rounded_parts(leaves[path],cents[key])
                node['dramas']=[]
                for drama_key,value in parts.items():
                    drama=dramas[drama_key]
                    node['dramas'].append(dict({k:v for k,v in drama.items() if k!='posts'},
                                               allocated_cents=value,published_count=len(drama['posts']),
                                               classification=metadata[drama_key]))
                node['dramas'].sort(key=lambda d:(-d['allocated_cents'],d['content_id'],d['language']))
                node['channel_share_bp']=global_shares.get(path)
                combinations.append(dict(path=list(path),name='·'.join((AUDIENCES[path[0]],THEMES[path[1]])),
                                         revenue_cents=cents[key],channel_share_bp=global_shares.get(path)))
            result.append(node)
        return sorted(result,key=lambda r:(-r['revenue_cents'],r['key']))

    tree=nodes((),total)
    combinations.sort(key=lambda r:(-r['revenue_cents'],r['path']))
    return dict(hierarchy_version=VERSION,audiences=tree,combinations=combinations,
                audience_basis_counts=dict(Counter(data['audience_basis'] for data in metadata.values())))


def rebuild_profile(profile):
    """Reproject a frozen profile from unique drama facts, never rounded tag sums."""
    dramas, classifications = {}, {}
    def visit(nodes):
        for node in nodes:
            if 'children' in node:
                visit(node['children'])
            for row in node.get('dramas', []):
                key = (row['content_id'], row['language'])
                # Every publication belongs to one exact drama version. Synthetic
                # identities preserve distinct publication counts across themes.
                drama = {k: row[k] for k in ('content_id', 'language', 'name', 'revenue_cents')}
                drama['posts'] = {(key, i) for i in range(row['published_count'])}
                if key in dramas and (dramas[key] != drama or classifications[key] != row['classification']):
                    raise ValueError('profile_drama_facts_conflict')
                dramas[key], classifications[key] = drama, row['classification']
    visit(profile.get('audiences', profile.get('types', [])))
    if sum(d['revenue_cents'] for d in dramas.values()) != profile['revenue_cents']:
        raise ValueError('profile_drama_revenue_mismatch')
    if len(dramas) != profile['drama_count']:
        raise ValueError('profile_drama_count_mismatch')
    result = {k: v for k, v in profile.items() if k not in ('types', 'audiences', 'combinations', 'focus')}
    result.update(build_tree(dramas, classifications))
    return result
