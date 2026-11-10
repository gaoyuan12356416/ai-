"""Build conserved Page portraits from frozen, read-only source extracts."""
import argparse
from collections import defaultdict, Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
from pathlib import Path

from features.x_tag_revenue.core import language, rounded_parts
from features.x_tag_revenue.tags import VERSION as TAG_VERSION, lookup
from features.x_tag_revenue.taxonomy import classification_index, classify, THEMES

VERSION = 'fb-page-effect-20261110-v1'
AUDIENCES = {'female':'女频故事','male':'男频故事','general':'通用题材','lgbtq':'同性爱情题材','__unknown__':'方向待识别'}
MONEY = ('spend','af_revenue0','af_revenue','revenue')
COUNTS = ('impressions','clicks','installs','purchase','row_count')

def cents(x):
    return int(Decimal(str(x or 0))*100)

def build(raw):
    scope = list(map(str, raw['scope']))
    assert len(scope)==len(set(scope))==236, 'scope_must_match_frozen_236_pages'
    assert (date.fromisoformat(raw['end'])-date.fromisoformat(raw['start'])).days==6
    assert all(w['start']==raw['start'] and w['end']==raw['end'] and w['kind'] in ('recent','none') for w in raw['windows'].values()), 'only_latest_seven_days_allowed'
    members = {str(r['page_id']): r for r in raw['membership'] if not r['deleted_at'] and str(r['page_id']) in scope}
    assert set(members)==set(scope), 'membership_changed_review_required'
    info = {str(r['page_id']):r for r in raw['pages']}
    classifications = classification_index(raw['resources'], raw['drama_info'])
    content_names = {(str(r['content_id']),language(r['language'])):r.get('name','') for r in raw['drama_info']}
    series_lookup = defaultdict(set)
    for r in raw['drama_info']:
        if str(r['app_id'])=='1479':series_lookup[(str(r.get('series_code') or ''),language(r['language']))].add(str(r['content_id']))
    grouped = defaultdict(lambda: defaultdict(lambda: {k:0 for k in MONEY+COUNTS}))
    observed = defaultdict(set)
    mapping_counts=Counter()
    for r in raw['effects']:
        pid = str(r['page_id'])
        assert pid in scope, 'out_of_scope_effect'
        window=raw['windows'][pid]
        if not window['start'] or not window['start']<=r['dt']<=window['end']:continue
        lang=language(r['language']); cid=str(r.get('data_source_id') or '')
        key=(cid,lang)
        mapping='content_language'
        if key not in classifications:
            candidates=series_lookup[(str(r.get('series_code') or ''),lang)]
            if len(candidates)==1:
                cid=next(iter(candidates));key=(cid,lang);mapping='unique_series_language'
            else:
                cid='unmatched:'+str(r.get('series_code') or cid or 'unknown');key=(cid,lang);mapping='unmatched'
        mapping_counts[mapping]+=1
        row=grouped[pid][key]
        row.update(content_id=cid,language=lang,name=content_names.get(key) or str(r.get('series_code') or cid),mapping=mapping)
        for k in MONEY:row[k]+=cents(r.get(k))
        for k in COUNTS:row[k]+=int(r.get(k) or 0)
        if cents(r.get('spend'))>0:observed[pid].add(r['dt'])

    output=[]
    for pid in scope:
        base=info[pid];window=raw['windows'][pid]
        dramas=[dict(row,classification=classifications.get(key,classify([],status='unmatched_drama'))) for key,row in grouped[pid].items()]
        # Zero-only lines cannot create a portrait or inflate drama counts.
        dramas=[d for d in dramas if any(d[k] for k in MONEY)]
        totals={k:sum(d[k] for d in dramas) for k in MONEY+COUNTS}
        leaves=defaultdict(lambda: {k:Fraction() for k in MONEY})
        flat=defaultdict(lambda: {k:Fraction() for k in MONEY})
        leaf_drama=defaultdict(set)
        for d in dramas:
            classification=d['classification'];themes=classification['themes']
            for theme in themes:
                path=(classification['audience'],theme)
                for k in MONEY:leaves[path][k]+=Fraction(d[k],len(themes))
                leaf_drama[path].add((d['content_id'],d['language']))
            tags=classification['semantic_tags'] or {'__unknown__':'未识别标签'}
            for tag in tags:
                for k in MONEY:flat[tag][k]+=Fraction(d[k],len(tags))
        def distribute(data):
            allocated={k:rounded_parts({key:v[k] for key,v in data.items()},totals[k]) for k in MONEY}
            return {key:{k:allocated[k][key] for k in MONEY} for key in data}
        alloc=distribute(leaves);flat_alloc=distribute(flat)
        combinations=[dict(audience=a,audience_name=AUDIENCES[a],theme=t,theme_name=THEMES[t],**v,drama_count=len(leaf_drama[(a,t)])) for (a,t),v in alloc.items()]
        combinations.sort(key=lambda r:(-r['af_revenue0'],-r['spend'],r['audience'],r['theme']))
        # X-post chooses the parent audience first, then its largest theme.
        audience_totals=defaultdict(int)
        for row in combinations:audience_totals[row['audience']]+=row['af_revenue0']
        main_audience=max(audience_totals,key=lambda a:(audience_totals[a],a)) if audience_totals and totals['af_revenue0']>0 and totals['spend']>0 else None
        focus=next((r for r in combinations if r['audience']==main_audience),None)
        audiences=[]
        audience_shares=rounded_parts({a:Fraction(v*10000,totals['af_revenue0']) for a,v in audience_totals.items()},10000) if totals['af_revenue0']>0 else {}
        for a,v in sorted(audience_totals.items(),key=lambda item:(-item[1],item[0])):
            children=[dict(r) for r in combinations if r['audience']==a]
            shares=rounded_parts({r['theme']:Fraction(r['af_revenue0']*10000,v) for r in children},10000) if v>0 else {}
            for r in children:r.update(share_bp=shares.get(r['theme']))
            audiences.append(dict(key=a,name=AUDIENCES[a],revenue_cents=v,share_bp=audience_shares.get(a),children=children))
        tags=[dict(key=tag,**lookup(tag),**v) for tag,v in flat_alloc.items()]
        tags.sort(key=lambda r:(-r['af_revenue0'],-r['spend'],r['key']))
        current_lang=members[pid]['language'] or ''
        aligned_spend=sum(d['spend'] for d in dramas if language(d['language'])==language(current_lang)) if current_lang else None
        known_spend=sum(d['spend'] for d in dramas if d['classification']['themes']!=['__unknown__'])
        confidence='none' if totals['spend']==0 else 'thin' if len(observed[pid])<3 or len(dramas)<3 else 'usable'
        unavailable=base['status']!=0 or base['forbidden_status']!=0 or base['is_permission']!=1
        profile=dict(page_id=pid,name=base['page_name'],language=current_lang,backup=not current_lang,
                     status=base['status'],forbidden=base['forbidden_status'],permission=base['is_permission'],
                     available=not unavailable,followers=base['fan_ccount'],window=window,
                     age_days=(date.fromisoformat(raw['end'])-date.fromisoformat(window['end'])).days if window['end'] else None,
                     **totals,drama_count=len(dramas),active_days=len(observed[pid]),evidence=confidence,
                     known_spend=known_spend,aligned_spend=aligned_spend,profile_eligible=totals['spend']>0 and totals['af_revenue0']>0,
                     focus=focus,audiences=audiences,combinations=combinations,tags=tags,
                     dramas=sorted(dramas,key=lambda r:(-r['af_revenue0'],-r['spend'])),geo=raw.get('geo',{}).get(pid,{}))
        for k in MONEY:
            assert sum(r[k] for r in combinations)==totals[k]
            assert sum(r[k] for r in tags)==totals[k]
        output.append(profile)
    output.sort(key=lambda p:(-p['af_revenue0'],-p['spend'],p['page_id']))
    version=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    meta=dict(version=version,algorithm=VERSION,tag_version=TAG_VERSION,hierarchy_reference='x-audience-theme-20261110-v2',
              generated_at=datetime.now(timezone.utc).isoformat(),group_id=62,page_count=len(scope),
              start=raw['start'],end=raw['end'],history_lookup=False,currency='source_report_unit',
              source='kunlunads_dev.ads_custom_source_insight',product='Dramawave',platform=0,
              primary_revenue='af_revenue0',allocation='equal_distinct_themes_within_content_audience',
              scope_sha256=hashlib.sha256('\n'.join(sorted(scope)).encode()).hexdigest(),mapping_counts=dict(mapping_counts),
              audit={k:sum(p[k] for p in output) for k in MONEY},scope_counts=dict(Counter(p['window']['kind'] for p in output)),
              source_extract_sha256=raw.get('source_extract_sha256'))
    return dict(meta=meta,pages=output,themes=THEMES,audiences=AUDIENCES)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('raw');parser.add_argument('output');args=parser.parse_args()
    source=Path(args.raw).read_bytes();raw=json.loads(source);raw['source_extract_sha256']=hashlib.sha256(source).hexdigest()
    result=build(raw);Path(args.output).write_text(json.dumps(result,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    print(json.dumps(result['meta'],ensure_ascii=False,indent=2))
