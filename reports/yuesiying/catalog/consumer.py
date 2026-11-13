"""Catalog selection mirrors the existing localization builder's input contract."""
import collections
import hashlib
import json
from pathlib import Path
import re

from store import canonical, file_sha, iter_json_array, metadata, normalize, require, scoped_rows


def norm(s):
    s=s.lower().strip();s=re.sub(r'^(?:en验证[_-])?ai(?:翻转|小语种|解说)-[a-z-]+-\d+-\d+:\d+-','',s);s=re.sub(r'^[a-z-]+-\d+-00:00-','',s)
    s=s.replace('en已验证_','').replace('en已验证','en').replace('纯净版','').replace('en验证','').replace('_翻转_','_')
    s=re.sub(r'(?<=[_-])(?:en|tw|zh-tw|ar|de|el|es|fr|hi|id|it|ja|ko|ms|pl|pt|ru|th|tl|tr|vi|小语种)(?=[_-])','',s)
    s=re.sub(r'(?:[._-](?:mp4|png|jpg|jpeg))+$','',s)
    return re.sub(r'[^\w\u4e00-\u9fff]','',s).replace('_','')


def fuzzy_key(name):
    """Name evidence only, never a statement that video contents are identical."""
    name=re.sub(r'^(?:跑量迭代[_\-\s]*)+', '', name, flags=re.I)
    return norm(name)


def grams(key):
    return {key[i:i+3] for i in range(len(key)-2)}


def fuzzy_matches(mats, original_ids):
    """Bounded, same-resource candidate retrieval; every result needs review.

    Index distinct names, not every pair of material IDs. Match 3-gram Dice
    >= .55 with >= 6 common grams, or containment of >= 12 characters.
    Neither duration nor a high score can waive human review.
    """
    keys=collections.defaultdict(list)
    for mid,row in mats.items():
        if row['l'].lower()!='en':keys[fuzzy_key(row['n'])].append(mid)
    keygrams={key:grams(key) for key in keys}
    index=collections.defaultdict(set)
    for key,parts in keygrams.items():
        for part in parts:index[part].add(key)
    found={};cached={}
    for mid in sorted(original_ids):
        if mid not in mats:continue
        mother=mats[mid];key=fuzzy_key(mother['n'])
        if key not in cached:
            parts=grams(key);counts=collections.Counter()
            for part in parts:counts.update(index.get(part,()))
            if key and key in keys:counts[key]=len(parts)
            candidates=[]
            for other,common in counts.items():
                exact=bool(key) and key==other
                if common<6 and not exact:continue
                score=1.0 if exact else 2*common/(len(parts)+len(keygrams[other]))
                contained=min(len(key),len(other))>=12 and (key in other or other in key)
                if exact or score>=.55 or contained:
                    candidates.append((other,round(score,4),'核心名称包含' if contained else '核心名称部分相似'))
            cached[key]=candidates
        hits=[]
        for other,score,reason in cached[key]:
            for target in keys[other]:
                if target!=mid and mats[target]['l'].lower()!=mother['l'].lower():
                    hits.append({'mid':target,'score':score,'reason':reason})
        found[mid]=sorted(hits,key=lambda r:(-r['score'],int(r['mid'])))
    return found


def inputs(workdir):
    root=Path(workdir);site=root/'site'
    data=json.loads((site/'data.json').read_text());catalog=collections.defaultdict(set)
    for row in json.loads((root/'catalog.json').read_text()):
        catalog[str(row['content_id'])].add((str(row['series_code']),row['language'].lower()))
    originals=collections.defaultdict(set)
    for resource,file in data['materials']['files'].items():
        originals[resource].update(json.loads((site/file).read_text())['meta'])
    contents={cid for cid,identity in catalog.items() if len(identity)==1 and next(iter(identity))[0] in originals}
    return catalog,originals,contents


def select(catalog,originals,library):
    scoped=collections.defaultdict(dict)
    for row in library:
        cid=str(row['data_source_id']);identity=catalog.get(cid,set())
        if len(identity)!=1:continue
        resource,lang=next(iter(identity))
        if not lang or lang!=row['language'].lower() or resource not in originals:continue
        mid=str(row['id']);scoped[resource][mid]={'n':row['name'],'l':row['language'],'d':float(row['video_duration'] or 0),
            'k':norm(row['name']),'c':str(row['created_at']),'ids':[],'cid':cid,
            'v':next((v for v in ['AI翻转','AI解说','AI小语种'] if v in row['name']),'普通版本')}
    kept={};potential=set();matches={}
    for resource,mats in scoped.items():
        matches[resource]=fuzzy_matches(mats,originals[resource])
        candidates={hit['mid'] for hits in matches[resource].values() for hit in hits}
        kept[resource]={mid:row for mid,row in mats.items() if mid in originals[resource] or mid in candidates}
        potential.update(mid for mid,row in kept[resource].items() if row['l'].lower()!='en')
    return {'kept':kept,'potential':sorted(potential),
            'matches':matches,
            'order':[(resource,list(rows)) for resource,rows in kept.items()]}


def load_library(workdir, config):
    root=Path(workdir);manifest=root/'catalog-generation.json'
    if not manifest.exists():
        return iter_json_array(root/'library.json')
    binding=json.loads(manifest.read_text())
    require((root/'inventory-time.txt').read_text().strip()==binding['generated_at'],
            'catalog_binding_stale_use_refresh_source_and_bind')
    legacy=root/'library.json'
    if binding['source_kind']=='legacy-json' and legacy.exists():
        require(file_sha(legacy)==binding['source_sha256'],'legacy_library_changed_refresh_catalog_binding')
    _,_,contents=inputs(root)
    return scoped_rows(config,binding['generation_id'],contents)


def bind(workdir,config,generation):
    root=Path(workdir);record=metadata(config,generation)
    require(record and record['status']=='verified','catalog_not_verified')
    legacy=root/'library.json'
    if record['source_kind']=='legacy-json' and legacy.exists():
        require(file_sha(legacy)==record['source_sha256'],'legacy_library_does_not_match_generation')
    binding={key:record[key] for key in ('generation_id','generated_at','source_kind','source_sha256')}
    files=[('catalog-generation.json',canonical(binding)+'\n')]
    inventory=root/'inventory-time.txt'
    if not inventory.exists() or inventory.read_text().strip()!=record['generated_at']:
        files.insert(0,('inventory-time.txt',record['generated_at']))
    for name,text in files:
        temporary=root/('.'+name+'.catalog-tmp');temporary.write_text(text,encoding='utf-8');temporary.chmod(0o600)
        temporary.replace(root/name)
    return binding


def validate_legacy(workdir,config,generation):
    root=Path(workdir);catalog,originals,contents=inputs(root)
    old=[row for row in iter_json_array(root/'library.json') if str(row['data_source_id']) in contents]
    new=scoped_rows(config,generation,contents)
    require(len(old)==len(new) and all(normalize(before)==after for before,after in zip(old,new)),
            'catalog_filtered_rows_or_order_changed')
    # Compare the real raw legacy input against stored values, so conversions
    # cannot hide a change in material ID spelling or other consumer behavior.
    expected,actual=select(catalog,originals,old),select(catalog,originals,new)
    require(expected==actual,'localization_selection_changed')
    return {'generation_id':generation,'content_ids':len(contents),'matched_rows':len(old),
            'kept_materials':sum(map(len,expected['kept'].values())),'potential_materials':len(expected['potential']),
            'ordered_rows_sha256':hashlib.sha256(canonical(new).encode()).hexdigest(),
            'consumer_result_sha256':hashlib.sha256(canonical(expected).encode()).hexdigest(),
            'material_ids_and_order_equal':True,'key_fields_equal':True}
