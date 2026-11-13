"""Versioned type/audience/theme rules. Classification never guesses drama type."""
from collections import defaultdict
import json

from .core import labels, label_index, language
from .tags import normalized

from features.drama_audience.store import AUDIENCES, VERSION, records as audience_records, direction as audience_direction
UNKNOWN = '__unknown__'
TYPES = {'local': '本土剧', 'translated': '翻译剧', UNKNOWN: '未标注'}
THEMES = {'ceo_wealth': '豪门总裁', 'sweet_romance': '甜宠恋爱', 'marital': '婚恋纠葛',
          'family': '萌宝家庭', 'revenge': '逆袭复仇', 'period_intrigue': '古装权谋',
          'fantasy': '奇幻穿越', 'suspense': '悬疑冒险', 'growth': '生活成长', UNKNOWN: '未标注'}

# A semantic label contributes to one broad theme. Synonyms and labels mapping
# to the same broad theme are deduplicated before splitting any revenue.
THEME_TAGS = {
    'ceo_wealth': '''beautiful_ceo billionaire boss_lady business ceo heiress nobility secretary''',
    'sweet_romance': '''age_gap ballroom bdsm bl bonded_by_fate campus_romance childhood_crush
        contract_couple dating_show_game first_love first_sight_love gay_for_you gl golden_boy
        group_favorite harem hate_to_love hero_rescues_beauty love_triangle loving_hubby matchmaking
        office_romance playboy pretend_romance rom_com romance secret_love sweet sweet_love
        female_oriented_reverse_harem male_priented_reverse uncle''',
    'marital': '''affair angsty betrayal bitter_love contract_vows divorce divorce_aftermath
        flash_marriage forbidden_love forced_love forgiveness four_way_love housewife
        love_after_divorce love_after_vows misunderstanding o_n_s reconciliation redemption regret
        reject reject_marriage reunion runaway_bride_groom second_chance substitute_bride
        the_substitute twisted winning_back_wife xxl_wife''',
    'family': '''baby_on_board blended_family child_in_law cute_pet family family_affection family_drama
        family_reunion genius_babies hot_daddy kinship_recognition mother_in_law nanny parent_with_child
        pet pregnant_runaway secret_child single_dad single_mom son_in_law step_siblings twins''',
    'revenge': '''comeback counterattack fake_vs_real_heiress hidden_identity hidden_strength humiliation
        identity_reveal identity_twist level_up live_in_son_in_law medical_genius nobody overpowered_dominance
        revenge satisfying_revenge satisfying_reversal sudden_wealth super_warrior underdog_rise war_god''',
    'period_intrigue': '''alternative_history ancient empress general history imperial_prince knight
        political_intrigue prince princess princess_consort queen royal_family''',
    'fantasy': '''1001_nights alien alpha alternate_universe angel beastfolk beasts blood_awakening body_swap
        cultivator demi_humans dragon dungeons_dragons elf european_dragons fantasy fantasy_world fated_mates
        folklore_literature future greek_myth grimm_fairy_tales high_fantasy mages_medieval mages_modern mages_other
        mecha merfolk meta_human mythological_fiction norse_myth priest priestess rebirth reincarnation
        resurrection saintess sci_fi shapeshift_dragon superhero supernatural superpower system time_travel
        vampire werewolf western_fantasy witch witch_wizard''',
    'suspense': '''action adventurous agent apocalypse confined_survival crime crime_investigation crime_lord
        disaster drug_lord extreme_survival hidden_roles kidnap mafia mystery pirates spy survival survival_game
        thrill_calls thrilling_suspenseful twist''',
    'growth': '''artist athlete campus celebrity chef contemporary entertainment era female_growth female_lead
        firefighter heartwarming_healing inspirational lawyer maid medical_drama professor real_world rural
        sports_competition strong_woman student teacher teenager tough_girl ugly_duckling_to_swan urban workplace''',
}
TAG_THEME = {}
for _theme, _names in THEME_TAGS.items():
    for _name in _names.split():
        if _name in TAG_THEME:raise ValueError('duplicate_theme_mapping:'+_name)
        TAG_THEME[_name] = _theme

def raw_labels(value):
    if isinstance(value, str):
        try:parsed=json.loads(value)
        except ValueError:parsed=None
        value=parsed if isinstance(parsed,list) else value.split(',')
    return list(dict.fromkeys(v.strip() for v in value or [] if isinstance(v,str) and v.strip()))


def classify(raw, drama_type=UNKNOWN, *, status='matched', info_values=(), audience_record=None, expected_language=None):
    raw=raw_labels(raw);semantic=labels(raw)
    direction=audience_direction(audience_record, expected_language)
    topics=list(dict.fromkeys(TAG_THEME[tag] for tag in semantic if tag in TAG_THEME)) or [UNKNOWN]
    return dict(drama_type=drama_type, drama_type_name=TYPES[drama_type],
                audience=direction['key'], audience_name=AUDIENCES[direction['key']], audience_basis=direction['basis'],
                audience_rules=direction['rules'], audience_evidence=direction['evidence'],
                audience_source=direction['source'], audience_fetched_at=direction['fetched_at'], audience_status=direction['status'],
                themes=topics, theme_names=[THEMES[k] for k in topics], raw_labels=raw,
                semantic_tags=semantic, mapping_status=status, resource_type_values=list(info_values),
                unmapped_labels=[v for k,v in semantic.items() if k.startswith(('unmapped_','ambiguous_'))],
                taxonomy_version=VERSION, type_source='ads_drama_info.resource_type')


def classification_index(resources, infos, content_ids=()):
    grouped, types=defaultdict(list),defaultdict(list)
    labels_index=label_index(resources)
    for row in resources:
        if str(row.get('app_id'))=='1479':grouped[(str(row['content_id']),language(row.get('language')))].append(row)
    for row in infos:
        if str(row.get('app_id'))=='1479':types[(str(row['content_id']),language(row.get('language')))].append(row)
    result={}
    cache=audience_records([key[0] for key in set(grouped)|set(types)]+list(content_ids))
    keys=set(grouped)|set(types)|{(cid,language(lang)) for cid,r in cache.items() for lang in r['languages']}
    for key in keys:
        info=types[key];values={str(r.get('resource_type')) for r in info}
        series={str(r.get('series_code') or '') for r in info}- {''}
        value=next(iter(values)) if len(values)==1 and len(series)<=1 else None
        dtype={'1':'local','2':'translated'}.get(value,UNKNOWN)
        status='missing_drama_info' if not info else 'ambiguous_drama_info' if len(values)>1 or len(series)>1 else 'unlabeled_type' if dtype==UNKNOWN else 'matched'
        label_status=labels_index.get(key,{}).get('status','missing_labels')
        raw=[tag for row in grouped[key] for tag in raw_labels(row.get('labels'))] if label_status!='ambiguous_drama' else []
        result[key]=classify(raw,dtype,status=status,info_values=sorted(values), audience_record=cache.get(key[0]), expected_language=key[1])
        result[key]['label_status']=label_status
        result[key]['content_id'],result[key]['language']=key
    return result


def collect_classifications(query, schema, ids):
    """Use the caller's existing read-only connection; exact product/version keys."""
    resources,infos=[],[]
    ids=sorted(set(str(value) for value in ids))
    for start in range(0,len(ids),500):
        batch=ids[start:start+500];placeholders=','.join('%s' for _ in batch)
        # These are the same product/content indexes used by portrait refresh.
        # Without the hint MySQL can scan the entire product for every batch.
        for table,index,columns,target in [('ads_drama_resource','content_id','content_id,app_id,language,series_code,labels',resources),
                                           ('ads_drama_info','ac','content_id,app_id,language,series_code,resource_type',infos)]:
            sql='SELECT DISTINCT '+columns+' FROM `'+schema+'`.'+table+' FORCE INDEX('+index+') WHERE app_id=%s AND content_id IN ('+placeholders+')'
            target.extend(query(sql,[1479]+batch))
    return classification_index(resources,infos,ids)
