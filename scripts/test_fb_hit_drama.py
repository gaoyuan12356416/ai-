import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from features.fb_auto_posts.core import FBAutoPostStore, ActorScope
from features.fb_auto_posts.hit_drama import publish_drama
from features.fb_auto_posts.repositories import PageTarget, MaterialCandidate, CandidateSnapshot, MaterialRepository
from scripts.test_fb_auto_validation import payload


class HitDramaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=FBAutoPostStore(Path(self.temp.name)/'fb.sqlite3',now_fn=lambda:datetime(2026,11,11,6,tzinfo=timezone.utc))
        self.actor=ActorScope('u','operator',True,'')
        self.template=self.store.create_template(payload(),self.actor,dict(app_id='1479',product='Dramawave',metric_product='Dramawave',metric_platform=0))
        with self.store.connect() as conn:conn.execute("UPDATE fb_auto_template SET status='enabled'")
        self.pages=SimpleNamespace(list_pages=lambda *a,**k:[PageTarget('6',('6',),'11','1','UTC','en',1),PageTarget('6',('6',),'12','1','UTC','id',1)],legacy_conflicts=lambda *_:[])
        self.mysql=SimpleNamespace(schema='ads',select=lambda sql,args:[dict(content_id='123',language='en',deploy_time=1600000000)])
        def material(mid,cid,lang):return MaterialCandidate(mid,cid,'https://media.test/'+mid+'.mp4','m','drama',lang,Decimal(60),Decimal(0),None,Decimal(0),None,'1')
        self.configs=[]
        def candidates(config):
            self.configs.append(config)
            # Include another drama to exercise the final transactional boundary.
            return CandidateSnapshot((material('1','999',config['language']),material('2','123','en')),(),())
        self.materials=SimpleNamespace(mysql=self.mysql,candidate_snapshot=candidates,choose_from=lambda rows,excluded:next(iter(rows),None))
        self.runtime=SimpleNamespace(store=self.store,pages=self.pages,materials=self.materials,executor=SimpleNamespace(live_enabled=True),prebuild_enabled=True,max_publishable_pages=2000,max_jobs_per_slot=5000,max_daily_jobs=10000)
        self.request=dict(expected_version=1,drama_id='123',id_type='content_id',operation_id='request123')
        from tests.test_fb_profile_integration import profiles, classification
        snapshot = profiles({'11': ('female', 'sweet_romance'), '12': ('female', 'sweet_romance')})
        profile_patch = patch('features.fb_auto_posts.hit_profile.load_profiles', return_value=snapshot)
        catalog_patch = patch('features.fb_auto_posts.hit_profile.load_classifications',
            side_effect=lambda mysql, ids: {(cid, lang): classification('female', 'sweet_romance')
                                           for cid in ids for lang in ('en', 'id')})
        profile_patch.start();catalog_patch.start()
        self.addCleanup(profile_patch.stop);self.addCleanup(catalog_patch.stop)

    def test_target_language_and_atomic_replay(self):
        result=publish_drama(self.runtime,self.template['id'],self.actor,self.request)
        self.assertEqual((result['queued'],result['skipped']),(1,0))
        self.assertEqual(result['matched_pages'],1)
        self.assertEqual(result['skipped_pages'][0]['reason'],'language_not_matched')
        with self.store.connect() as conn:
            tasks=[dict(r) for r in conn.execute('SELECT * FROM fb_auto_task')]
            self.assertEqual([r['content_id'] for r in tasks if r['status']=='planned'],['123'])
            config=json.loads(conn.execute('SELECT config_json FROM fb_auto_run').fetchone()[0])
            self.assertEqual(config['hit_drama_publish']['content_id'],'123')
            audit=json.loads(tasks[0]['selection_json'])['tag_profile']
            self.assertTrue(audit['matched']);self.assertEqual(audit['content_id'],'123')
        self.mysql.select=lambda *_:(_ for _ in ()).throw(AssertionError('replay must not query source'))
        again=publish_drama(self.runtime,self.template['id'],self.actor,self.request)
        self.assertEqual(again['run_id'],result['run_id']);self.assertTrue(again['idempotent'])
        with self.assertRaisesRegex(Exception,'操作标识'):publish_drama(self.runtime,self.template['id'],self.actor,dict(self.request,drama_id='124'))

    def test_material_cooldown_is_retained(self):
        first=publish_drama(self.runtime,self.template['id'],self.actor,self.request)
        second=publish_drama(self.runtime,self.template['id'],self.actor,dict(self.request,operation_id='request456'))
        self.assertEqual(first['queued'],1);self.assertEqual(second['queued'],0)

    def test_invalid_resource_creates_no_run(self):
        self.mysql.select=lambda *_:[]
        with self.assertRaisesRegex(Exception,'资源 ID'):publish_drama(self.runtime,self.template['id'],self.actor,dict(self.request,id_type='resource_id'))
        with self.store.connect() as conn:self.assertEqual(conn.execute('SELECT count(*) FROM fb_auto_run').fetchone()[0],0)

    def test_exact_source_query_does_not_fall_back_to_all_drama_scan(self):
        queries=[]
        def select(sql,params):queries.append((sql,params));return []
        mysql=SimpleNamespace(schema='ads',blacklist_schema='ads',select=select)
        metrics=SimpleNamespace(load_metric_window=lambda **_:SimpleNamespace(by_drama={},by_material={},generation_ids=(),dates=()))
        repo=MaterialRepository(mysql,metrics)
        with self.store.connect() as conn:config=json.loads(self.store._template_row(conn,self.template['id'],self.actor)['config_json'])
        config.update(language='en',hit_drama_publish=dict(content_id='123'))
        self.assertEqual(repo.candidate_snapshot(config).candidates,())
        self.assertEqual(len(queries),1)
        self.assertIn('s.data_source_id IN (%s)',queries[0][0]);self.assertIn('123',queries[0][1])

    def test_business_resource_selects_its_language_version_per_page(self):
        self.mysql.select=lambda *_:[dict(content_id='enAbc',language='en',deploy_time=1600000000),dict(content_id='idXyz',language='id',deploy_time=1600000000)]
        def candidates(config):
            cid={'en':'enAbc','id':'idXyz'}[config['language']]
            m=MaterialCandidate('2' if cid=='enAbc' else '3',cid,'https://media.test/a.mp4','m','drama',config['language'],Decimal(60),Decimal(0),None,Decimal(0),None,'1')
            other=MaterialCandidate('4','otherDrama','https://media.test/other.mp4','m','other',config['language'],Decimal(60),Decimal(0),None,Decimal(0),None,'1')
            return CandidateSnapshot((other,m),(),())
        self.materials.candidate_snapshot=candidates
        result=publish_drama(self.runtime,self.template['id'],self.actor,dict(self.request,id_type='resource_id',drama_id='30911'))
        self.assertEqual(result['queued'],2);self.assertEqual(result['languages'],['en','id'])
        with self.store.connect() as conn:
            self.assertEqual([tuple(r) for r in conn.execute("SELECT page_id,content_id FROM fb_auto_task WHERE status='planned' ORDER BY page_id")],[('11','enAbc'),('12','idXyz')])

    def test_missing_target_language_never_scans_unrelated_catalog(self):
        queries=[]
        mysql=SimpleNamespace(schema='ads',blacklist_schema='ads',select=lambda *a:queries.append(a) or [])
        metrics=SimpleNamespace(load_metric_window=lambda **_:SimpleNamespace(by_drama={},by_material={},generation_ids=(),dates=()))
        repo=MaterialRepository(mysql,metrics)
        with self.store.connect() as conn:config=json.loads(self.store._template_row(conn,self.template['id'],self.actor)['config_json'])
        config.update(language='id',hit_drama_publish=dict(version=2,by_language={'en':['enAbc']},content_ids=['enAbc']))
        self.assertEqual(repo.candidate_snapshot(config).candidates,());self.assertEqual(queries,[])

    def test_unrelated_cooldown_does_not_misreport_missing_target_material(self):
        self.materials.candidate_snapshot=lambda config: CandidateSnapshot((),(),())
        with patch('features.fb_auto_posts.core.cooldown_content_ids', return_value={'other-drama'}):
            result=publish_drama(self.runtime,self.template['id'],self.actor,self.request)
        self.assertEqual(result['queued'],0)
        self.assertEqual(result['skip_summary'],[{'reason':'fb_auto_no_eligible_video','count':1}])

    def test_frozen_matching_does_not_add_page_from_later_scope_reads(self):
        original=self.pages.list_pages
        calls=[]
        def changing_pages(*args,**kwargs):
            calls.append(1)
            rows=original(*args,**kwargs)
            return rows if len(calls)==1 else rows+[PageTarget('6',('6',),'13','1','UTC','en',1)]
        self.pages.list_pages=changing_pages
        result=publish_drama(self.runtime,self.template['id'],self.actor,self.request)
        with self.store.connect() as conn:
            self.assertEqual([row[0] for row in conn.execute('SELECT page_id FROM fb_auto_task')],['11'])
        self.assertEqual(result['matched_pages'],1)

    def test_no_matching_profile_creates_no_run(self):
        with patch('features.fb_auto_posts.hit_profile.load_profiles', return_value=({'pages':{}},{})):
            with self.assertRaisesRegex(Exception,'没有语言'):
                publish_drama(self.runtime,self.template['id'],self.actor,self.request)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM fb_auto_run').fetchone()[0],0)

    def test_legacy_receipt_does_not_claim_profile_matches(self):
        result=publish_drama(self.runtime,self.template['id'],self.actor,self.request)
        with self.store.connect() as conn:
            config=json.loads(conn.execute('SELECT config_json FROM fb_auto_run WHERE id=?',(result['run_id'],)).fetchone()[0])
            for key in ('page_ids','profiles','page_content_ids','page_languages','match_policy','skipped_pages'):
                config['hit_drama_publish'].pop(key,None)
            conn.execute('UPDATE fb_auto_run SET config_json=? WHERE id=?',(json.dumps(config),result['run_id']))
        replay=publish_drama(self.runtime,self.template['id'],self.actor,self.request)
        self.assertIsNone(replay['matched_pages'])
        self.assertEqual(replay['match_policy'],'legacy_language_only')

    def test_each_matched_language_version_survives_candidate_limit(self):
        def select(sql,params):
            if 's.data_source_id IN' in sql:
                cid=params[1]
                return [dict(material_id=str(i),content_id=cid,media_url='https://media.test/a.mp4',
                             language='en',video_duration=60) for i in ([1,2,3] if cid=='A' else [4])]
            if 'MAX(d0.id)' in sql:
                return [dict(content_id=params[-1],language='en',resource_type_v2='1')]
            raise AssertionError(sql)
        mysql=SimpleNamespace(schema='ads',blacklist_schema='ads',select=select)
        metrics=SimpleNamespace(load_metric_window=lambda **_:SimpleNamespace(by_drama={},by_material={},generation_ids=(),dates=()))
        repo=MaterialRepository(mysql,metrics,candidate_limit=2,metric_prefilter_batch_size=1)
        with self.store.connect() as conn:
            config=json.loads(self.store._template_row(conn,self.template['id'],self.actor)['config_json'])
        config.update(language='en',drama_cooldown_hours=0,hit_drama_publish=dict(
            match_policy='language_audience_theme_positive_v1',by_language={'en':['A','B']},content_ids=['A','B']))
        snapshot=repo.candidate_snapshot(config)
        self.assertEqual([item.material_id for item in snapshot.candidates],['1','2','4'])


if __name__=='__main__':unittest.main()
