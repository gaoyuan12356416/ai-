"""Page-specific copy, tenant boundaries and immutable automatic task receipts."""
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from features.fb_auto_posts import page_caption
from features.fb_auto_posts.hit_material import publish_material, task_policy
from tests import test_fb_profile_integration as fixtures


class PageCaptionTests(unittest.TestCase):
    run_slot = fixtures.PlannerTests.run_slot
    tasks = fixtures.PlannerTests.tasks

    def setUp(self):
        fixtures.PlannerTests.setUp(self)
        self.rows = tuple(replace(row, drama_description='A story with {url} in its text.') for row in self.rows)
        with self.store.connect() as conn:
            conn.execute('CREATE TABLE fb_video_page(tenant_key TEXT,page_id TEXT,config_instance_id TEXT,version INTEGER,caption_template TEXT,created_by TEXT,deleted INTEGER DEFAULT 0,PRIMARY KEY(tenant_key,page_id))')
        self.configure('11', '💕Watch NOW {url}\n📺{desc}')

    def configure(self, page, caption, tenant='tenant-a', creator='operator', deleted=0):
        with self.store.connect() as conn:
            conn.execute('INSERT OR REPLACE INTO fb_video_page VALUES(?,?,?,?,?,?,?)',
                         (tenant, page, 'config-'+page, 3, caption, creator, deleted))
            conn.commit()

    def run_once(self):
        with patch('features.fb_auto_posts.core.load_profiles', return_value=self.profile):
            return self.run_slot('caption-test')

    def test_page_priority_links_and_fallback_are_frozen(self):
        self.run_once()
        first, second = self.tasks()
        self.assertTrue(first['message_text'].startswith('💕Watch NOW https://'))
        self.assertEqual(first['message_text'].count(first['short_url']), 1)
        self.assertIn('A story with {url} in its text.', first['message_text'])
        self.assertIn('AIpost', first['long_url'])
        self.assertTrue(any(c['_page_caption_description'] for c in self.configs))
        audit = json.loads(first['selection_json'])['caption']
        self.assertEqual((audit['source'], audit['page_version']), ('page_management', 3))
        self.assertEqual(json.loads(second['selection_json'])['caption']['fallback_reason'], 'page_not_configured')
        before = self.tasks()
        self.configure('11', 'Changed {name}')
        self.assertTrue(self.run_once()['idempotent'])
        self.assertEqual(before, self.tasks())

    def test_only_same_tenant_config_is_used_regardless_of_creator(self):
        self.configure('12', 'Same tenant {name}', creator='other')
        self.configure('11', 'Foreign caption', tenant='tenant-b', creator='outsider')
        self.run_once()
        self.assertEqual(self.tasks()[1]['message_text'], 'Same tenant Drama')
        self.assertNotIn('Foreign caption', self.tasks()[0]['message_text'])

    def test_ambiguous_creator_namespace_falls_back(self):
        self.configure('99', 'Foreign caption', tenant='tenant-b')
        self.run_once()
        self.assertTrue(all(json.loads(t['selection_json'])['caption']['fallback_reason']=='page_caption_tenant_unresolved' for t in self.tasks()))

    def test_invalid_or_empty_caption_falls_back(self):
        for text, reason in [('', 'page_caption_empty'), ('{unknown}', 'page_caption_macro_invalid'),
                             ('{{url}', 'page_caption_macro_invalid'), ('{url} {{url}}', 'page_caption_url_repeated')]:
            with self.subTest(text=text):
                self.configure('11', text)
                with self.store.connect() as conn:
                    snap = page_caption.load(conn, 'operator', ['11'])
                config, audit = page_caption.choose({'message_template':'fallback'}, '11', self.rows[0], snap)
                self.assertEqual(config['message_template'], 'fallback')
                self.assertEqual(audit['fallback_reason'], reason)

    def test_missing_description_uses_fallback(self):
        self.rows = tuple(replace(row, drama_description='') for row in self.rows)
        self.run_once()
        task = self.tasks()[0]
        self.assertEqual(json.loads(task['selection_json'])['caption']['fallback_reason'], 'page_caption_desc_unavailable')
        self.assertFalse(task['short_url'])
        self.assertIn('Drama', task['message_text'])

    def test_deleted_caption_is_not_used(self):
        self.configure('11', 'Deleted', deleted=1)
        self.run_once()
        self.assertEqual(json.loads(self.tasks()[0]['selection_json'])['caption']['fallback_reason'], 'page_not_configured')

    def with_url_fallback(self):
        self.template = self.store.create_template(dict(self.raw, message_template='Fallback {{url}}'), self.actor,
            dict(app_id='1479', product='Dramawave', metric_product='Dramawave', metric_platform=0))

    def test_invalid_page_caption_keeps_fallback_link(self):
        self.with_url_fallback()
        self.configure('11', '{unsupported}')
        self.run_once()
        first = self.tasks()[0]
        self.assertEqual(first['message_text'], 'Fallback '+first['short_url'])
        self.assertTrue(first['long_url'])

    def test_page_copy_without_url_does_not_keep_unused_fallback_link(self):
        self.with_url_fallback()
        self.configure('11', 'Page copy')
        self.run_once()
        first = self.tasks()[0]
        self.assertEqual((first['message_text'], first['short_url'], first['long_url']), ('Page copy', '', ''))

    def test_valid_page_copy_does_not_expand_an_oversized_unused_fallback(self):
        self.template=self.store.create_template(dict(self.raw,message_template='Fallback {{desc}} {{url}}'),self.actor,
            dict(app_id='1479',product='Dramawave',metric_product='Dramawave',metric_platform=0))
        self.configure('11','Page copy')
        self.page_rows=self.page_rows[:1]
        self.rows=tuple(replace(row,drama_description='x'*6000) for row in self.rows)
        self.run_once()
        self.assertEqual(self.tasks()[0]['message_text'],'Page copy')

    def test_page_without_desc_can_select_material_even_if_fallback_requires_desc(self):
        from features.fb_auto_posts.repositories import MaterialRepository
        self.template=self.store.create_template(dict(self.raw,message_template='Fallback {{desc}} {{url}}'),self.actor,
            dict(app_id='1479',product='Dramawave',metric_product='Dramawave',metric_platform=0))
        self.configure('11','Watch {url}')
        def select(sql,params):
            if 'FORCE INDEX(PRIMARY)' in sql:
                return [dict(material_id='1',content_id='drama',media_url='https://media.test/1.mp4',language='en',video_duration=60)]
            if 'MAX(d0.id)' in sql:return [dict(content_id='drama',drama_name='Drama',language='en',resource_type_v2='1')]
            if 'description_count' in sql:return []
            raise AssertionError(sql)
        metrics=SimpleNamespace(load_metric_window=lambda **_:SimpleNamespace(by_drama={},by_material={},generation_ids=(),dates=()))
        self.materials=MaterialRepository(SimpleNamespace(schema='ads',blacklist_schema='ads',select=select),metrics,now_fn=lambda:fixtures.NOW)
        with patch('features.fb_auto_posts.core.load_profiles',return_value=({'pages':{}},{})):
            self.run_slot('optional-description')
        first,second=self.tasks()
        self.assertEqual(first['status'],'planned')
        self.assertEqual(first['message_text'],'Watch '+first['short_url'])
        self.assertEqual(second['status'],'skipped')

    def test_oversized_expansion_falls_back_without_truncating_page_copy(self):
        self.configure('11', '{desc}')
        self.rows = tuple(replace(row, drama_description='x'*5001) for row in self.rows)
        self.run_once()
        self.assertEqual(json.loads(self.tasks()[0]['selection_json'])['caption']['fallback_reason'], 'page_caption_expanded_too_long')

    def test_exactly_5000_characters_remain_valid(self):
        self.configure('11', '{desc}')
        self.rows = tuple(replace(row, drama_description='x'*5000) for row in self.rows)
        self.run_once()
        self.assertEqual(len(self.tasks()[0]['message_text']), 5000)
        self.assertEqual(json.loads(self.tasks()[0]['selection_json'])['caption']['source'], 'page_management')

    def test_missing_page_table_is_safe_fallback(self):
        with sqlite3.connect(':memory:') as conn:
            self.assertEqual(page_caption.load(conn, 'operator', ['11'])['reason'], 'page_caption_lookup_unavailable')

    def test_all_approved_languages_keep_two_macros(self):
        bank = json.loads((Path(__file__).resolve().parents[1]/'ops/fb-page-caption/approved-20261113.json').read_text(encoding='utf-8'))
        self.assertEqual(len(bank), 23)
        for hook, label, tag in bank.values():
            text = f'💕{hook} 👉 {{url}}\n\n📺{label}\n{{desc}}\n\n#shortdrama #shortdrama{tag} #dramaseries #dramawave'
            result = page_caption.normalize(text)
            self.assertEqual(result.count('{{url}}'), 1)
            self.assertEqual(result.count('{{desc}}'), 1)

    def test_exact_material_flow_keeps_its_manifest_and_caption_receipt(self):
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_auto_template SET status='enabled' WHERE id=?", (self.template['id'],))
            conn.commit()
        configs = []
        def exact(config, mid):
            configs.append(config)
            return self.rows[0]
        runtime = SimpleNamespace(store=self.store, pages=self.pages,
            materials=SimpleNamespace(exact_material=exact), executor=SimpleNamespace(live_enabled=True),
            prebuild_enabled=True, resolve_source=lambda *args: None,
            max_publishable_pages=100, max_jobs_per_slot=100, max_daily_jobs=1000)
        request = {'expected_version':1, 'material_id':'1', 'operation_id':'caption-test'}
        result = publish_material(runtime, self.template['id'], self.actor, request)
        self.assertEqual(result['queued'], 2)
        self.assertTrue(configs[0]['_page_caption_description'])
        with self.store.connect() as conn:
            run = dict(conn.execute('SELECT * FROM fb_auto_run WHERE id=?',(result['run_id'],)).fetchone())
        self.assertEqual(json.loads(self.tasks()[0]['selection_json'])['caption']['source'], 'page_management')
        self.assertTrue(task_policy(run, self.tasks()[0], json.loads(run['config_json']))['waive_drama_cooldown'])
        before = self.tasks()
        self.configure('11', 'New text')
        self.assertTrue(publish_material(runtime, self.template['id'], self.actor, request)['idempotent'])
        self.assertEqual(before, self.tasks())

    def test_exact_material_missing_desc_only_skips_pages_that_require_it(self):
        self.template=self.store.create_template(dict(self.raw,message_template='Fallback {{desc}} {{url}}'),self.actor,
            dict(app_id='1479',product='Dramawave',metric_product='Dramawave',metric_platform=0))
        self.configure('11','Watch {url}')
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_auto_template SET status='enabled' WHERE id=?",(self.template['id'],))
        def exact(config,mid):
            self.assertNotIn('{{desc}}',config['message_template'])
            self.assertTrue(config['_page_caption_description'])
            return replace(self.rows[0],drama_description='')
        runtime=SimpleNamespace(store=self.store,pages=self.pages,materials=SimpleNamespace(exact_material=exact),
            executor=SimpleNamespace(live_enabled=True),prebuild_enabled=True,resolve_source=lambda *a:None,
            max_publishable_pages=100,max_jobs_per_slot=100,max_daily_jobs=1000)
        result=publish_material(runtime,self.template['id'],self.actor,
            {'expected_version':1,'material_id':'1','operation_id':'optional-desc'})
        self.assertEqual((result['queued'],result['skipped']),(1,1))
        self.assertEqual(self.tasks()[0]['message_text'],'Watch '+self.tasks()[0]['short_url'])
        self.assertEqual(self.tasks()[1]['skip_reason'],'fb_auto_caption_source_missing')

    def test_executor_sends_frozen_page_copy_with_mock_transport_only(self):
        from features.fb_auto_posts.publisher import AutoPostExecutor,GraphResult
        from features.fb_auto_posts.repositories import PageCredential
        self.page_rows=self.page_rows[:1]
        self.run_once()
        original=self.tasks()[0]
        self.configure('11','Changed after planning')
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_auto_template SET status='enabled' WHERE id=?",(self.template['id'],))
            conn.execute("UPDATE fb_auto_task SET status='ready',media_url='https://media.test/prepared.mp4',prepared_media_url='https://media.test/prepared.mp4',prepared_at_utc=?",(fixtures.NOW.isoformat(),))
        sent=[];links=[]
        def publish(pid,credential,media,message,**kwargs):
            sent.append((pid,message));return GraphResult('success',post_id='fixture-only')
        pages=SimpleNamespace(eligible_credentials=lambda pid:[PageCredential(pid,'fixture-user','fixture-credential','fixture-not-a-token')])
        executor=AutoPostExecutor(self.store,pages,SimpleNamespace(publish_video=publish),live_enabled=True,
            min_request_interval_seconds=0,short_link_writer=lambda *args:links.append(args))
        with patch('requests.sessions.Session.request',side_effect=AssertionError('network forbidden in fixture')):
            executor.execute_next('fixture-worker')
        self.assertEqual(sent,[('11',original['message_text'])])
        self.assertEqual(len(links),1)
        self.assertEqual(self.tasks()[0]['status'],'submitted')


class OptionalDescriptionTests(unittest.TestCase):
    def test_optional_page_description_hydrates_without_discarding_missing_rows(self):
        from features.fb_auto_posts.repositories import MaterialRepository
        from features.fb_auto_posts.validation import normalize_template_payload
        from scripts.test_fb_auto_validation import payload
        def select(sql, params):
            if 'FORCE INDEX(PRIMARY)' in sql:
                return [dict(material_id=cid, content_id=cid, media_url='https://media.test/'+cid+'.mp4', language='en', video_duration=60) for cid in ('1','2')]
            if 'DISTINCT TRIM(d.content_id) content_id' in sql:
                return [{'content_id':'1'}, {'content_id':'2'}]
            if 's.data_source_id IN' in sql:
                cid=str(params[1])
                return [dict(material_id=cid, content_id=cid, media_url='https://media.test/'+cid+'.mp4', language='en', video_duration=60)]
            if 'MAX(d0.id)' in sql:
                return [dict(content_id=cid, language='en', resource_type_v2='1') for cid in ('1','2')]
            if 'description_count' in sql:
                return [dict(content_id='1', drama_description='A valid story', description_count=1)]
            raise AssertionError(sql)
        mysql=SimpleNamespace(schema='ads', blacklist_schema='ads', select=select)
        metrics=SimpleNamespace(load_metric_window=lambda **_:SimpleNamespace(by_drama={},by_material={},generation_ids=(),dates=()))
        repo=MaterialRepository(mysql, metrics, now_fn=lambda:fixtures.NOW, candidate_limit=5, metric_prefilter_batch_size=1)
        for required in (False, True):
            config=normalize_template_payload(payload())
            config.update(app_id='1479',product='Dramawave',metric_product='Dramawave',metric_platform=0,
                          language='en',_page_caption_description=True,
                          message_template='{{desc}}' if required else 'Fallback')
            result=repo.candidate_snapshot(config)
            self.assertEqual([r.material_id for r in result.candidates], ['1'] if required else ['1','2'])
            self.assertEqual(result.candidates[0].drama_description, 'A valid story')


class ExistingMetricReaderTests(unittest.TestCase):
    def test_reader_can_open_while_migration_holds_shared_lock(self):
        from features.fb_auto_posts.core import FBAutoPostStore
        from contextlib import closing
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'metrics.sqlite3'
            FBAutoPostStore(path)
            with closing(sqlite3.connect(path)) as migration:
                migration.execute('BEGIN')
                migration.execute('SELECT * FROM fb_auto_metric_active_pointer').fetchall()
                before=path.stat().st_mtime_ns
                reader=FBAutoPostStore(path,initialize_storage=False)
                with reader.connect() as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM fb_auto_metric_active_pointer').fetchone()[0],0)
                self.assertEqual(path.stat().st_mtime_ns,before)

    def test_existing_reader_rejects_missing_schema_without_creating_it(self):
        from features.fb_auto_posts.core import FBAutoPostStore
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'absent.sqlite3'
            with self.assertRaisesRegex(ValueError,'existing_metric_database_required'):
                FBAutoPostStore(path,initialize_storage=False)
            self.assertFalse(path.exists())


if __name__=='__main__':unittest.main()
