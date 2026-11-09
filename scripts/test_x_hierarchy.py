"""Hierarchical income, conditional probability, fallback and version identity."""
from collections import Counter
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.x_tag_revenue.core import build
from features.x_tag_revenue.taxonomy import classify, classification_index, VERSION, UNKNOWN
from features.x_tag_revenue.tags import VERSION as TAG_VERSION
from features.x_tag_revenue.routes import response
from features.x_tag_publish.policy import Decision

ACCOUNT=dict(id=1,x_user_id='channel')


def fixtures(spec):
    cat,metrics,resources,info=[],[],[],[]
    for i,(dtype,raw,cents) in enumerate(spec,1):
        key=str(i)
        cat.append(dict(platform='x',site_id='2116',key=[key,'name'],channel='x:channel',drama=key,drama_label='Drama '+key,
                        language='en',tenant='t',owner='u',date='2026-11-01',link_type='auto_template',excluded=False,
                        publications=[dict(id='post'+key,date='2026-11-01')]))
        metrics.append(dict(platform='x',site_id='2116',campaign_id=key,date='2026-11-02',revenue_cents=cents))
        resources.append(dict(app_id=1479,content_id=key,language='en',series_code='s'+key,labels=raw))
        info.append(dict(app_id=1479,content_id=key,language='en',series_code='s'+key,resource_type=dtype))
    return cat,metrics,resources,info


def portrait(spec=None):
    spec=spec or [(1,'Female,Sweet',2800),(1,'Female,CEO',1680),(1,'Female,Divorce',1120),
                 (1,'Male,Action',1400),(2,'General audience,Workplace',3000)]
    cat,metrics,resources,info=fixtures(spec)
    return build(cat,metrics,resources,'2026-11-08',info)


def snapshot(spec=None):
    return portrait(spec),dict(version='test-frozen',tag_mapping_version=TAG_VERSION,hierarchy_version=VERSION)


class TaxonomyTests(unittest.TestCase):
    def test_type_uses_actual_content_language_and_product(self):
        resources=[dict(app_id=1479,content_id='1',language='en',series_code='s',labels='Sweet')]
        infos=[dict(app_id=1479,content_id='1',language='en',series_code='s',resource_type=2),
               dict(app_id=1479,content_id='1',language='ja',series_code='s',resource_type=1),
               dict(app_id=999,content_id='1',language='en',series_code='s',resource_type=1)]
        self.assertEqual(classification_index(resources,infos)[('1','en')]['drama_type'],'translated')
        self.assertEqual(classification_index(resources,[])[('1','en')]['drama_type'],UNKNOWN)

    def test_conflicting_type_does_not_guess_local(self):
        info=[dict(app_id=1479,content_id='1',language='en',series_code='s',resource_type=t) for t in (1,2)]
        result=classification_index([],info)[('1','en')]
        self.assertEqual(result['drama_type'],UNKNOWN)
        self.assertEqual(result['mapping_status'],'ambiguous_drama_info')

    def test_audience_explicit_inferred_conflict_and_insufficient(self):
        cases=[('Male,Sweet','male','explicit'),('BL,Romance','lgbtq','explicit'),('GL','lgbtq','explicit'),
               ('Sweet,Love After Vows','female','inferred'),('War God','male','inferred'),
               ('Revenge,CEO',UNKNOWN,'unlabeled'),('Sweet,War God',UNKNOWN,'conflict'),
               ('Mystery','general','inferred'),('Male,Female',UNKNOWN,'conflict')]
        for raw,expected,basis in cases:
            actual=classify(raw)
            self.assertEqual((actual['audience'],actual['audience_basis']),(expected,basis),raw)

    def test_semantic_labels_collapse_to_unique_broad_themes(self):
        result=classify('Revenge,复仇,Counterattack,逆袭,Sweet,Romance')
        self.assertEqual(set(result['themes']),{'revenge','sweet_romance'})


class IncomeTests(unittest.TestCase):
    def test_conditional_shares_match_70_80_50_example(self):
        p=portrait()['accounts']['channel'];local=next(t for t in p['types'] if t['key']=='local')
        female=next(t for t in local['children'] if t['key']=='female')
        sweet=next(t for t in female['children'] if t['key']=='sweet_romance')
        self.assertEqual((local['share_bp'],female['share_bp'],sweet['share_bp'],sweet['channel_share_bp']),(7000,8000,5000,2800))

    def test_signed_odd_cent_revenue_conserved_at_every_level(self):
        p=portrait([(1,'Female,CEO,Sweet,Romance',101),(1,'Female,Revenge,Counterattack',-7),(2,'Male,Action,Sci Fi',9),(0,'Unknown',3)])['accounts']['channel']
        self.assertEqual(sum(t['revenue_cents'] for t in p['types']),106)
        def check(node):
            children=node.get('children',node.get('dramas',[]))
            field='revenue_cents' if 'children' in node else 'allocated_cents'
            self.assertEqual(node['revenue_cents'],sum(r[field] for r in children))
            for child in node.get('children',[]):check(child)
        for node in p['types']:check(node)

    def test_unknown_buckets_and_zero_shares(self):
        p=portrait([(0,'unclassified-123',100)])['accounts']['channel']
        self.assertEqual([p['types'][0]['key'],p['types'][0]['children'][0]['key'],p['types'][0]['children'][0]['children'][0]['key']],[UNKNOWN]*3)
        zero=portrait([(1,'Female,Sweet',0)])['accounts']['channel']['types'][0]
        self.assertIsNone(zero['share_bp'])

    def test_details_lazy_load_by_full_path_and_channel_acl(self):
        data=portrait();accounts=[dict(id=8,x_user_id='channel')]
        status,result=response(data,{},accounts,'8')
        self.assertEqual(status,200)
        leaf=result['portrait']['types'][0]['children'][0]['children'][0]
        self.assertNotIn('dramas',leaf)
        status,result=response(data,{},accounts,'8',path=['local','female','sweet_romance'])
        self.assertEqual(status,200);self.assertEqual(len(result['portrait']['dramas']),1)
        self.assertEqual(response(data,{},[],'8',path=['local'])[0],404)
        self.assertEqual(response(data,{},accounts,'8',path=['translated','female'])[0],404)


class ChoiceTests(unittest.TestCase):
    def test_conditional_probability_and_replay(self):
        frozen=snapshot();counts=Counter()
        for i in range(5000):
            d=Decision(ACCOUNT,i,frozen);counts[tuple(d.preferred())]+=1
            if i<20:self.assertEqual(d.preferred(),Decision(ACCOUNT,i,frozen).preferred())
        self.assertTrue(.26<counts[('local','female','sweet_romance')]/5000<.30,counts)
        self.assertTrue(.68<sum(v for k,v in counts.items() if k[0]=='local')/5000<.72)

    def primary(self):
        frozen=snapshot()
        for i in range(100):
            d=Decision(ACCOUNT,i,frozen)
            if d.preferred()==['local','female','sweet_romance']:return d
        self.fail('fixture seed')

    def test_higher_type_and_audience_always_beat_lower_theme(self):
        d=self.primary()
        a=classify('Female,Sweet','translated')
        b=classify('Male,Sweet','local')
        c=classify('Female,Revenge','local')
        primary=classify('Female,Sweet','local')
        self.assertEqual(sorted([a,b,c,primary],key=d.rank),[primary,c,b,a])
        self.assertEqual(d.audit(a)['fallback_level'],'type')
        self.assertEqual(d.audit(b)['fallback_level'],'audience')
        self.assertEqual(d.audit(c)['fallback_level'],'theme')

    def test_other_type_uses_its_own_audience_and_theme(self):
        d=self.primary()
        good=classify('General audience,Workplace','translated')
        bad=classify('Female,Sweet','translated')
        self.assertLess(d.rank(good),d.rank(bad))

    def test_missing_audience_preserves_type_and_original_order(self):
        frozen=snapshot([(1,'CEO',7000),(2,'Male,War God',3000)])
        d=next(Decision(ACCOUNT,i,frozen) for i in range(100) if Decision(ACCOUNT,i,frozen).type_order[0]=='local')
        first=classify('Male,Action','local');second=classify('Female,Sweet','local');foreign=classify('Male,War God','translated')
        self.assertEqual(d.rank(first),d.rank(second))
        self.assertLess(d.rank(first),d.rank(foreign))
        self.assertEqual(d.audit(first)['fallback_reason'],'no_valid_audience_profile')

    def test_missing_theme_preserves_type_and_audience(self):
        frozen=snapshot([(1,'Female',10000)])
        d=Decision(ACCOUNT,1,frozen)
        a=classify('Female,Sweet','local');b=classify('Female,Revenge','local');c=classify('Male,Sweet','local')
        self.assertEqual(d.rank(a),d.rank(b));self.assertLess(d.rank(a),d.rank(c))
        self.assertEqual(d.audit(a)['fallback_reason'],'no_valid_theme_profile')

    def test_no_income_falls_back_entirely_but_retains_classification(self):
        d=Decision(ACCOUNT,1,snapshot([(1,'Female,Sweet',0)]));a=classify('Male,War God','translated')
        self.assertEqual(d.rank(a),(0,0,0));audit=d.audit(a)
        self.assertEqual(audit['fallback_reason'],'no_positive_revenue')
        self.assertEqual(audit['classification']['drama_type'],'translated')
        self.assertEqual(audit['classification']['audience'],'male')

    def test_invalid_child_distribution_only_loses_that_layer(self):
        frozen=snapshot([(1,'Female,Sweet',200),(1,'Male,War God',-100),(2,'Male,War God',1)])
        d=next(Decision(ACCOUNT,i,frozen) for i in range(100) if Decision(ACCOUNT,i,frozen).type_order[0]=='local')
        self.assertEqual(d.rank(classify('Female,Sweet','local')),d.rank(classify('Male,War God','local')))
        self.assertLess(d.rank(classify('Male,Action','local')),d.rank(classify('Male,War God','translated')))

    def test_old_flat_profile_not_reinterpreted_and_input_not_mutated(self):
        frozen=snapshot();before=json.dumps(frozen,sort_keys=True);d=Decision(ACCOUNT,1,frozen);d.audit(classify('Female,Sweet','local'))
        self.assertEqual(json.dumps(frozen,sort_keys=True),before)
        old=copy.deepcopy(frozen);old[1].pop('hierarchy_version')
        self.assertEqual(Decision(ACCOUNT,1,old).reason,'portrait_hierarchy_outdated')


if __name__=='__main__':unittest.main()
