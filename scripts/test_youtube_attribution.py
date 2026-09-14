"""Offline URL, identity mapping, crash recovery and real SQLite claim tests."""
import base64
from contextlib import closing
import copy
import html
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import test_youtube_auto_engine as fixtures
from features.drama_synthesis.core import ImmutableFilesystemPublisher, build_long_url
from features.youtube_auto_publish.attribution import AttributionLinks, mapped_user_id
from features.youtube_auto_publish.runtime import attribution_user_resolver
from features.youtube_auto_publish.service import YouTubeWorkflow
from features.youtube_auto_publish.templates import WorkflowError


class AttributionTests(unittest.TestCase):
    setUp = fixtures.EngineTests.setUp

    def setup_workflow(self, resolve=None):
        self.actor = dict(tenant_key='test', user_id='opaque-owner', role='user')
        self.material = dict(id='123', name='视频 & = [test]', content_id='content1', app_id='1479',
            language='es', macro_name='¡Drama! & friends', macro_desc='Description', drama_status='matched',
            source_job_id='a'*32, source_kind='concat_video', url='https://media.example.test/video.mp4')
        self.channel = dict(id='12', name='Channel & Co ', channel_id=fixtures.CHANNEL,
            youtube_account_id='11', eligible=True, comment_eligible=True, scopes=[fixtures.COMMENT_SCOPE])
        root = self.root/'links';root.mkdir()
        self.publisher = ImmutableFilesystemPublisher(root)
        self.resolver = resolve or Mock(return_value='789')
        self.links = AttributionLinks(self.store.db_path, self.store, self.publisher, self.resolver)
        self.workflow = YouTubeWorkflow(self.store.db_path, self.covers, SimpleNamespace(get=lambda _:copy.deepcopy(self.material)),
            lambda _:[self.channel.copy()], Mock(side_effect=AssertionError('legacy path used')), self.store, attribution_links=self.links)
        asset = self.workflow.upload_cover(self.actor, {'data':base64.b64encode(self.cover.read_bytes()).decode()})['asset']
        self.payload = dict(operation_id='attribution-test-operation', material_id='123', channel_id='12',
            title_template='{name}', description_template='{desc}\n{url}', comment_template='{url}',
            cover_source='local', cover_asset_id=asset['id'])

    def create(self):
        self.task = self.workflow.create_task(self.actor, self.payload)['task']
        return self.task

    def body(self):
        with closing(sqlite3.connect(self.store.db_path)) as c:
            return json.loads(c.execute('SELECT body FROM youtube_auto_preparation WHERE id=?', (self.task['id'],)).fetchone()[0])

    def test_actual_record_and_all_eight_fields_roundtrip(self):
        self.setup_workflow();self.create();self.workflow.run_once()
        b=self.body();query=parse_qs(urlsplit(b['material']['long_url']).query)
        self.assertEqual(set(query),{'c','af_adset','af_adset_id','af_ad','af_ad_id','af_channel','af_c_id','af_dp'})
        self.assertEqual(query['af_channel'],['789'])
        self.assertEqual(query['af_c_id'],[self.task['id']])
        self.assertEqual(query['af_adset'],['Channel & Co'])
        self.assertEqual(query['af_ad_id'],['123'])
        self.assertEqual(query['af_ad'],['视频 & = [test]_contentid[content1]'])
        self.assertIn('nonees*¡Drama! & friends*none*1',query['c'][0])
        self.assertEqual(b['material']['source_job_id'],'a'*32)
        ledger=self.store.youtube_task(1)
        self.assertEqual(ledger['job_id'],self.task['id'])
        self.assertIn(b['material']['macro_url'],ledger['description_rendered'])
        wrapper=(self.root/'links'/'1.html').read_text()
        self.assertIn(html.escape(b['material']['long_url'],quote=True),wrapper)
        self.assertEqual(ledger['reviewed_phase'],'upload')
        self.assertIsNotNone(self.store.claim_reviewed_youtube('test','2099-01-01T00:00:00Z'))

    def test_missing_or_ambiguous_mapping_has_no_reservation(self):
        for rows in [[], [('789',),('790',)], [('opaque-owner',)], [('',)]]:
            with self.subTest(rows=rows),self.assertRaises(WorkflowError):mapped_user_id(rows)
        self.assertEqual(mapped_user_id([('789',),('789',)]),'789')
        self.setup_workflow(Mock(side_effect=WorkflowError('attribution_user_missing','missing',409)))
        with self.assertRaises(WorkflowError):self.create()
        with closing(sqlite3.connect(self.store.db_path)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM drama_material_short_link').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM drama_youtube_publish').fetchone()[0],0)

    def test_idempotent_create_and_recovery_keep_original_mapping(self):
        self.setup_workflow();self.create()
        self.resolver.return_value='900'
        repeated=self.workflow.create_task(self.actor,self.payload)['task']
        self.assertEqual(repeated['id'],self.task['id'])
        self.resolver.assert_called_once()
        # A response loss before the preparation insert reuses the durable reservation.
        original=self.links.prepare(copy.deepcopy(self.material),self.channel,self.actor,self.task['id'])
        self.assertEqual(original['attribution']['sub_user_id'],'789')
        self.resolver.assert_called_once()

    def test_failed_wrapper_never_claims_and_retry_reuses_ledger(self):
        self.setup_workflow();self.create()
        with patch.object(self.publisher,'publish',side_effect=OSError('disk unavailable')):
            self.workflow.run_once()
        self.assertEqual(self.body()['status'],'enqueue_failed')
        self.assertTrue(self.workflow.get_task(self.actor,self.task['id'])['task']['can_retry'])
        self.assertEqual(self.store.youtube_task(1)['reviewed_phase'],'attribution_pending')
        self.assertIsNone(self.store.claim_reviewed_youtube('test','2099-01-01T00:00:00Z'))
        self.workflow.retry(self.actor,self.task['id']);self.workflow.run_once()
        self.assertEqual(self.body()['publish_id'],1)
        self.assertEqual(self.store.youtube_task(1)['reviewed_phase'],'upload')
        self.assertIsNone(self.store.youtube_task(2))

    def test_failure_after_wrapper_write_recovers_same_bytes(self):
        self.setup_workflow();self.create()
        finish=self.links.finalize
        def lost(*args):
            finish(*args)
            raise RuntimeError('response loss')
        with patch.object(self.links,'finalize',side_effect=lost):self.workflow.run_once()
        path=self.root/'links'/'1.html';before=path.read_bytes()
        self.assertIsNone(self.store.claim_reviewed_youtube('test','2099-01-01T00:00:00Z'))
        self.workflow.retry(self.actor,self.task['id']);self.workflow.run_once()
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.body()['publish_id'],1)

    def test_legacy_links_and_tasks_remain_unchanged(self):
        self.setup_workflow()
        old=self.store.ensure_short_link('b'*32,'concat_video','oldcontent',self.publisher)
        self.create();self.workflow.run_once()
        self.assertEqual(self.store.short_link('b'*32,'concat_video'),{k:v for k,v in old.items() if k!='reused'})
        self.assertEqual(old['long_url'],build_long_url('b'*32,'oldcontent'))

    def test_different_publishing_tasks_do_not_reuse_source_link(self):
        self.setup_workflow();first=self.create()
        self.payload['operation_id']='attribution-test-operation-2';second=self.create()
        self.assertNotEqual(first['material']['macro_url'],second['material']['macro_url'])
        self.assertNotEqual(first['material']['link_job_id'],second['material']['link_job_id'])

    def test_resolver_uses_tenant_scoped_email_and_readonly_exact_mapping(self):
        self.setup_workflow()
        with closing(sqlite3.connect(self.store.db_path)) as c:
            c.execute('CREATE TABLE drama_admin_user(user_id TEXT,tenant_key TEXT,email TEXT)')
            c.execute('INSERT INTO drama_admin_user VALUES(?,?,?)',('opaque-owner','test',' PERSON@example.test '))
            c.commit()
        app=SimpleNamespace(JOB_DB_PATH=self.store.db_path,ADMIN_MAPPING_MYSQL_HOST='101.32.56.53',
            ADMIN_MAPPING_MYSQL_PORT='63350',ADMIN_MAPPING_MYSQL_DATABASE='kunlunads_dev',
            ADMIN_MAPPING_MYSQL_USER='offline',ADMIN_MAPPING_MYSQL_PASSWORD='offline')
        with patch('features.youtube_auto_publish.runtime.subprocess.run',return_value=SimpleNamespace(stdout='1\n789\n')) as run:
            self.assertEqual(attribution_user_resolver(app)(dict(self.actor,email='untrusted@example.test')),'789')
        sql=run.call_args[0][0][-1]
        self.assertIn('SELECT DISTINCT sub_user_id',sql)
        self.assertIn('admin_user_group',sql)
        self.assertIn('person@example.test'.encode().hex(),sql)
        self.assertNotIn('admin_users',sql)
        self.assertIn('START TRANSACTION READ ONLY',sql)
        with self.assertRaises(WorkflowError):attribution_user_resolver(app)(dict(self.actor,tenant_key='other'))


if __name__=='__main__':unittest.main()
