"""Rebuild only audience grouping from frozen per-drama financial facts."""
from copy import deepcopy
from collections import Counter
from .store import records, VERSION
from features.x_tag_revenue.taxonomy import classify
from features.x_tag_revenue.hierarchy import build_tree


def facts(profile):
    rows={}
    for audience in profile.get('audiences',[]):
        for theme in audience.get('children',[]):
            for row in theme.get('dramas',[]):
                key=(row['content_id'],row['language'])
                fact={k:v for k,v in row.items() if k not in ('allocated_cents','classification')}
                if key in rows and rows[key]!=fact:raise ValueError('conflicting_drama_facts')
                rows[key]=fact
    return rows


def validate(profile):
    def visit(nodes,total):
        if sum(n['revenue_cents'] for n in nodes)!=total:raise ValueError('income_not_conserved')
        shares=[n.get('share_bp') for n in nodes]
        if shares and all(v is not None for v in shares) and sum(shares)!=10000:raise ValueError('shares_not_conserved')
        for node in nodes:
            if 'children' in node:visit(node['children'],node['revenue_cents'])
            elif sum(r['allocated_cents'] for r in node['dramas'])!=node['revenue_cents']:raise ValueError('drama_income_not_conserved')
    visit(profile['audiences'],profile['revenue_cents'])


def rebuild(profile,cache):
    before=facts(profile);dramas={};classifications={};old_classes={}
    for a in profile.get('audiences',[]):
        for t in a.get('children',[]):
            for r in t.get('dramas',[]):old_classes[(r['content_id'],r['language'])]=r['classification']
    for key,row in before.items():
        drama={k:v for k,v in row.items() if k!='published_count'}
        drama['posts']={(key,i) for i in range(row['published_count'])}
        dramas[key]=drama
        old=old_classes[key]
        c=classify(old.get('raw_labels',[]),old.get('drama_type','__unknown__'),
                   status=old.get('mapping_status','matched'),info_values=old.get('resource_type_values',[]),
                   audience_record=cache.get(key[0]),expected_language=key[1])
        # Theme policy and each drama's exact historical theme membership stay fixed.
        c.update(themes=old['themes'],theme_names=old['theme_names'])
        classifications[key]=c
    if sum(r['revenue_cents'] for r in dramas.values())!=profile['revenue_cents'] or len(dramas)!=profile['drama_count']:
        raise ValueError('source_facts_not_conserved')
    result=deepcopy(profile);result.update(build_tree(dramas,classifications))
    if profile.get('status') in ('signed_adjustment','no_income'):
        for a in result['audiences']:
            a['share_bp']=None
            for t in a['children']:t['share_bp']=t['channel_share_bp']=None
        for c in result['combinations']:c['channel_share_bp']=None
    if 'focus' in result:
        a=result['audiences'][0] if result['audiences'] and profile.get('status')=='ready' else {}
        simple=lambda n:{k:v for k,v in n.items() if k not in ('children','dramas')}
        result['focus']=dict(audience=simple(a),themes=[simple(t) for t in a.get('children',[])[:3]])
    validate(result)
    if facts(result)!=before:raise ValueError('financial_or_publication_facts_changed')
    return result


def project(profiles):
    ids={cid for p in profiles.values() for cid,lang in facts(p)}
    cache=records(ids)
    result={key:rebuild(p,cache) for key,p in profiles.items()}
    changed=[]
    for key,p in profiles.items():
        old={a['key']:a['revenue_cents'] for a in p['audiences']}
        new={a['key']:a['revenue_cents'] for a in result[key]['audiences']}
        if old!=new:changed.append(dict(channel=key,before=old,after=new))
    return result,dict(channels=len(profiles),changed_channels=len(changed),changes=changed,
                       drama_ids=len(ids),stored=len(cache),missing=sorted(ids-set(cache)),
                       status_counts=dict(Counter(r['status'] for r in cache.values())),
                       revenue_cents=sum(p['revenue_cents'] for p in profiles.values()),taxonomy_version=VERSION)
