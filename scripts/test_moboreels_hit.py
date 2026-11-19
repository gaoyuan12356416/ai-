"""Regression coverage for Mobo identity, strict profiles and scoped candidates."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.moboreels_hit import resolve, classifications


class IdentityTests(unittest.TestCase):
    def test_resource_expands_only_listed_mobo_languages(self):
        calls=[]
        def query(sql,args):
            calls.append((sql,args))
            return [dict(story_id='164754322',lang_id=3),dict(story_id='164754322',lang_id=3),dict(story_id='900',lang_id=5)]
        with patch.dict(os.environ,MOBOREELS_INSTITUTION_USER_ID='scope'):
            target=resolve(query,'kunlunads_dev',dict(id_type='resource_id',drama_id='XEY468'))
        self.assertEqual(target['by_language'],{'en':['164754322'],'pt-br':['900']})
        self.assertEqual(target['catalog'],'moboreels')
        sql,args=calls[0]
        self.assertEqual(args,(2373,'XEY468','XEY468','scope'))
        for fragment in ('i.series_code=%s','BINARY i.series_code','d.is_show=1','d.project_type=2','d.institution_user_id=%s'):
            self.assertIn(fragment,sql)

    def test_story_id_never_expands_other_language_versions(self):
        calls=[]
        with patch.dict(os.environ,MOBOREELS_INSTITUTION_USER_ID='scope'):
            target=resolve(lambda sql,args:(calls.append(sql) or [dict(story_id='123',lang_id=3),dict(story_id='124',lang_id=11)]),
                           'ads',dict(id_type='content_id',drama_id='123'))
        self.assertEqual(target['content_ids'],['123'])
        self.assertNotIn('ads_drama_info',calls[0])

    def test_provider_channel_is_authoritative_and_unknown_stays_unknown(self):
        rows=[dict(story_id='123',lang_id=3,c_name='家庭亲情,萌宝,超能力',channel_text='女频'),
              dict(story_id='124',lang_id=3,c_name='甜宠',channel_text=''),
              dict(story_id='125',lang_id=3,c_name='甜宠',channel_text='男频')]
        with patch.dict(os.environ,MOBOREELS_INSTITUTION_USER_ID='scope'):
            result=classifications(lambda *a:rows,['123','124','125'])
        self.assertEqual(result[('123','en')]['audience'],'female')
        self.assertIn('family',result[('123','en')]['themes'])
        self.assertEqual(result[('123','en')]['audience_source'],'cdreader_koc_drama.channel_text')
        self.assertEqual(result[('124','en')]['audience'],'__unknown__')
        self.assertEqual(result[('125','en')]['audience'],'male')



class FacebookTests(unittest.TestCase):
    def test_candidate_sql_filters_exact_hit_before_limit(self):
        from features.fb_auto_posts.moboreels import candidate_snapshot, validate_rules
        from features.fb_auto_posts.repositories import RepositoryError
        config=dict(message_template='{{m_url}}',material_products=['Moboreels'],material_data_source=2,
            language='en',material_rule=dict(duration_min_seconds=1,duration_max_seconds=100),
            drama_launch_window_days=7,hit_drama_publish=dict(catalog='moboreels',content_ids=['123','456'],by_language={'en':['123'],'id':['456']}))
        calls=[];repo=SimpleNamespace(mysql=SimpleNamespace(schema='ads',select=lambda sql,args:(calls.append((sql,args)) or [])),
            monotonic_fn=lambda:0,catalog_deadline_seconds=10,catalog_max_pages=5,candidate_limit=1)
        with patch('features.fb_video_publish.moboreels.institution_id',return_value='scope'):
            self.assertEqual(candidate_snapshot(repo,config).candidates,())
        sql,args=calls[0]
        self.assertLess(sql.index('d.story_id IN (%s)'),sql.index('LIMIT'))
        self.assertIn('123',args);self.assertNotIn('456',args)
        for change in (dict(drama_rule={'spend_min':1}),dict(feedback_selection={'enabled':True}),dict(drama_rule={'resource_type_v2':['x']})):
            with self.assertRaises(RepositoryError):validate_rules(dict(config,**change))

    def test_mobo_fb_real_planner_preserves_profile_cooldown_and_receipt(self):
        from scripts.test_fb_hit_drama import HitDramaTests
        from features.fb_auto_posts.hit_drama import publish_drama
        fixture=HitDramaTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        with fixture.store.connect() as conn:
            config=json.loads(conn.execute('SELECT config_json FROM fb_auto_template_version').fetchone()[0])
            config['message_template']='{{m_url}}'
            conn.execute('UPDATE fb_auto_template_version SET config_json=?',(json.dumps(config),))
        fixture.mysql.select=lambda *a:[dict(story_id='123',lang_id=3)]
        metadata={('123','en'):dict(audience='female',themes=['sweet_romance'])}
        with patch.dict(os.environ,MOBOREELS_INSTITUTION_USER_ID='scope'),patch('features.moboreels_hit.classifications',return_value=metadata),patch('features.hit_publish.release_times',side_effect=AssertionError('must not borrow DW timing')):
            first=publish_drama(fixture.runtime,fixture.template['id'],fixture.actor,dict(fixture.request,id_type='resource_id',drama_id='XEY468'))
            repeat=publish_drama(fixture.runtime,fixture.template['id'],fixture.actor,dict(fixture.request,id_type='resource_id',drama_id='XEY468'))
            second=publish_drama(fixture.runtime,fixture.template['id'],fixture.actor,dict(fixture.request,operation_id='second123'))
        self.assertEqual(first['queued'],1);self.assertTrue(repeat['idempotent']);self.assertEqual(second['queued'],0)
        self.assertEqual(fixture.configs[0]['hit_drama_publish']['catalog'],'moboreels')


class XTests(unittest.TestCase):
    def test_mobo_portuguese_catalog_accepts_normalized_selection_language(self):
        from features.x_tag_publish.material_catalog import CatalogSource
        calls=[]
        source=SimpleNamespace(schema='ads',material_data_source=6,_stream_select=lambda sql,args:(calls.append((sql,args)) or [dict(story_id='123',story_name='Target')]))
        with patch.dict(os.environ,MOBOREELS_INSTITUTION_USER_ID='scope'):
            rows=CatalogSource(source,['Moboreels']).list_drama_rows(language='pt')
        self.assertEqual(len(rows),1);self.assertEqual(calls[0][1][1],5)
        self.assertEqual(rows[0].language,'pt')

    def test_mobo_execution_keeps_exact_catalog_and_material(self):
        from dataclasses import replace
        from scripts import test_x_auto_post_selector as f
        from scripts.test_x_hit_drama import snapshot, MARKER
        from features.x_tag_publish.selection import ProfileSelector
        source=f.FakeSource([],{'123':[f.material('101','123',product='Moboreels')],
                               '999':[f.material('102','999',product='Moboreels')]})
        original_materials=source.list_material_rows
        source.list_material_rows=lambda *,content_id,language,products=None:original_materials(content_id=content_id,language=language)
        source.schema='ads';source.material_data_source=6
        source._stream_select=lambda *args:[dict(story_id='123',story_name='Target'),dict(story_id='999',story_name='Other')]
        target=dict(catalog='moboreels',content_ids=['123'],by_language={'en':['123']})
        store=f.FakeStore();store.get_run=lambda _:SimpleNamespace(blacklist_snapshot={MARKER:dict(account_ids=['101'],account_content_ids={'101':['123']},target=target)})
        selector=ProfileSelector(source,f.MetricWindowRepository(f.FakeMetricStore()),f.FakePublishedHistory(),store,material_validator=f.FakeValidator(overrides={"content_id":"123","product":"Moboreels","material_product":"Moboreels"}))
        request=f.request(f.rules(drama={"resource_types":[]}));request=replace(request,rules=replace(request.rules,material_products=('Moboreels',)))
        with patch.dict(os.environ,MOBOREELS_INSTITUTION_USER_ID='scope',X_POST_AUTOMATION_INTERNAL_URL='http://127.0.0.1:8821',X_POST_AUTO_INTERNAL_TOKEN='a'*48),patch('features.x_tag_publish.selection.XPostAutoBridgeClient') as bridge,patch('features.x_tag_publish.policy.portrait_snapshot',return_value=snapshot()),patch('features.moboreels_hit.classifications',return_value={('123','en'):dict(audience='female',themes=['sweet_romance'])}):
            bridge.return_value.account_snapshot.return_value=dict(id=101,x_user_id='x-101',drama_language='en')
            result=selector.select_and_reserve(request)
        self.assertEqual(result.drama.content_id,'123');self.assertEqual(result.drama.catalog,'moboreels')
        self.assertEqual(source.material_calls,[('123','en')])

    def test_mobo_template_resolution_and_strict_frozen_scope(self):
        from scripts.test_x_hit_drama import HitDramaTests,snapshot,MARKER
        from features.x_tag_publish.hit_drama import resolve as xresolve
        fixture=HitDramaTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.service.selector.source._stream_select=lambda *a:[dict(story_id='123',lang_id=3)]
        with patch.dict(os.environ,MOBOREELS_INSTITUTION_USER_ID='scope'):
            target=xresolve(fixture.service.selector.source,dict(id_type='resource_id',drama_id='XEY468'),dict(material_products=['Moboreels']))
        with patch('features.x_tag_publish.hit_drama.resolve',return_value=target),patch('features.moboreels_hit.classifications',return_value={('123','en'):dict(audience='female',themes=['sweet_romance'])}),patch('features.x_tag_publish.hit_drama.portrait_snapshot',return_value=snapshot()),patch('features.hit_publish.release_times',side_effect=AssertionError('must not query DW')):
            result=fixture.service.run_now(fixture.template['id'],dict(fixture.payload,id_type='resource_id',drama_id='XEY468'))
        frozen=fixture.store.get_run(result['run_id']).blacklist_snapshot[MARKER]
        self.assertEqual(result['matched_channels'],1)
        self.assertEqual(frozen['account_content_ids'],{'101':['123']})
        self.assertEqual(frozen['target']['catalog'],'moboreels')
        self.assertEqual(frozen['release_times'],{})


if __name__=='__main__':unittest.main()
