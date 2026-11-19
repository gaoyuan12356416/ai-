"""Publishing model choice across preview, immediate execution, schedules and retries."""
from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features.x_auto_posts.core import XAutoPostStore
from features.x_auto_posts.publisher import AutoLiveGates
from features.x_auto_posts.repositories import MetricWindowRepository
from features.x_auto_posts.validation import ValidationError
from features.x_growth_profile import store as growth
from features.x_tag_publish.hit_drama import TagAutoPostService
from features.x_tag_publish.selection import ProfileSelector
from scripts.test_x_auto_post_validation import valid_payload
from scripts import test_x_auto_post_service as service_fixtures
from scripts import test_x_auto_post_selector as fixtures


class PublishProfileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        self.enter(patch.dict(os.environ, X_GROWTH_DB_PATH=str(folder / 'growth.sqlite3'),
                              X_AUTO_POST_DB_PATH='', X_POST_DB_PATH='',
                              X_POST_AUTOMATION_INTERNAL_URL='http://fixture', X_POST_AUTO_INTERNAL_TOKEN='fixture'))
        growth.initialize()
        self.write_versions('before')
        self.now = fixtures.NOW
        self.db = XAutoPostStore(folder / 'auto.sqlite3', now_fn=lambda: self.now)
        bridge = service_fixtures.FakeBridge()
        for account in bridge.accounts_by_id.values():
            account['drama_language'] = 'en'
        source = fixtures.FakeSource([fixtures.drama('C9')], {'C9': [fixtures.material('901', 'C9')]})
        selector = ProfileSelector(source, MetricWindowRepository(fixtures.FakeMetricStore()),
                                   fixtures.FakePublishedHistory(), self.db, material_validator=fixtures.FakeValidator())
        self.svc = TagAutoPostService(self.db, bridge, selector,
                                      service_fixtures.FakeExecutor(AutoLiveGates(True, True, True)),
                                      now_fn=lambda: self.now, runner_kick_path='')
        bridge_read = self.enter(patch('features.x_tag_publish.selection.XPostAutoBridgeClient'))
        bridge_read.return_value.account_snapshot.side_effect = lambda aid: dict(bridge.accounts_by_id[str(aid)])
        self.enter(patch('features.x_tag_publish.policy.catalog_labels',
                         return_value={('C9','en'): {'audience':'male', 'themes':['revenge']}}))
        self.enter(patch('features.x_tag_publish.policy.portrait_snapshot',
                         return_value=({'accounts': {}}, {'unavailable': True})))

    def enter(self, ctx):
        value = ctx.start()
        self.addCleanup(ctx.stop)
        return value

    def write_versions(self, label):
        with growth.connect(write=True) as db:
            for aid in ('101', '102'):
                version = label + '-' + aid
                db.execute('INSERT OR REPLACE INTO growth_profile VALUES(?,?,?,?)',
                           (int(aid), version, '', json.dumps({'version': version, 'themes': [dict(
                               language='en', audience='male', theme='revenge', mature=True, score=73.25, effective_posts=8)]})))

    def template(self, **config):
        payload = valid_payload(drama_launch_window_days=0, schedule={'mode':'fixed','times':['18:00']}, **config)
        return self.svc.create_template(payload)['template']['id']

    def test_model_choice_from_preview_through_manual_and_scheduled_tasks(self):
        tid = self.template(profile_model='growth')
        preview = self.svc.preview(tid, {'expected_version':1})
        for row in preview['items']:
            self.assertTrue(row['ok'], row)
            audit = row['selection']['reservation']['tag_profile']
            self.assertEqual((audit['profile_mode'], audit['profile_version']), ('growth', 'before-' + row['account_id']))
        self.assertFalse(preview['reserved'])
        self.assertEqual(self.db.list_runs(), [])

        action = {'expected_version':1, 'idempotency_key':'manual-test-0001','confirmed':True,'profile_model':'revenue'}
        first = self.svc.run_now(tid, action)
        self.assertEqual(first['run']['profile_model'], 'revenue')
        self.assertTrue(all(t.account_snapshot['selection_profile']['mode']=='revenue' for t in self.db.list_tasks(run_id=first['run_id'])))
        self.assertEqual(self.db.get_template(tid).config['profile_model'], 'growth')
        self.assertTrue(self.svc.run_now(tid, action)['idempotent'])
        with self.assertRaisesRegex(service_fixtures.AutoPostServiceError, 'idempotency'):
            self.svc.run_now(tid, dict(action, profile_model='growth'))

        self.svc.set_enabled(tid, True, {'expected_version':1})
        scheduled = self.svc.tick()['created_runs']
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(self.svc.run(scheduled[0])['run']['profile_model'], 'growth')
        versions = {t.account_id:t.account_snapshot['selection_profile']['profile']['version'] for t in self.db.list_tasks(run_id=scheduled[0])}
        self.assertEqual(versions, {'101':'before-101', '102':'before-102'})
        self.write_versions('updated')
        self.assertEqual(self.svc.tick()['created_runs'], [])
        self.assertEqual(versions, {t.account_id:t.account_snapshot['selection_profile']['profile']['version'] for t in self.db.list_tasks(run_id=scheduled[0])})

    def test_exact_drama_and_material_use_the_same_run_scoped_model(self):
        tid = self.template()
        source = self.svc.selector.source
        source.schema, source.app_id = 'test', 1479

        def query(sql, params):
            if 'ads_custom_source' in sql:
                return [{'material_id':'901', 'content_id':'C9'}]
            return [{'content_id':'C9', 'language':'en', 'deploy_time':int(self.now.timestamp())}]

        source._stream_select = query
        catalog = {('C9','en'):{'audience':'male','themes':['revenge']}}
        self.enter(patch('features.x_tag_publish.hit_drama.resolve', return_value={
            'content_id':'C9', 'content_ids':['C9'], 'languages':['en'], 'by_language':{'en':['C9']}}))
        for module in ['hit_drama','hit_material']:
            self.enter(patch('features.x_tag_publish.' + module + '.catalog_labels', return_value=catalog))
            self.enter(patch('features.x_tag_publish.' + module + '.portrait_snapshot',
                             side_effect=AssertionError('growth run consulted revenue profiles')))
        for key, target in [('drama', {'drama_id':'C9','id_type':'content_id'}), ('material', {'material_ids':['901']})]:
            action = dict(target, expected_version=1, confirmed=True, profile_model='growth',
                          idempotency_key='exact-test-' + key)
            run = self.svc.run_now(tid, action)
            self.assertEqual(run['profile_model'], 'growth')
            self.assertTrue(run['matched_channels'])
            facts = self.db.get_run(run['run_id']).blacklist_snapshot
            audits = (facts['hit_drama_publish']['profiles'].values() if key == 'drama'
                      else [item['profile'] for item in run['assignments']])
            self.assertTrue(all(a['selected_tag_key']=='revenge' and a['growth_role']=='strict_match' for a in audits))
            for task in self.db.list_tasks(run_id=run['run_id']):
                self.assertEqual(task.account_snapshot['selection_profile']['profile']['version'], 'before-' + task.account_id)
            self.assertTrue(self.svc.run_now(tid, action)['idempotent'])
            with self.assertRaises(service_fixtures.AutoPostServiceError) as caught:
                self.svc.run_now(tid, dict(action, profile_model='revenue'))
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.db.get_template(tid).config['profile_model'], 'revenue')

    def test_growth_override_and_legacy_template_default(self):
        tid = self.template()
        self.assertEqual(self.db.get_template(tid).config['profile_model'], 'revenue')
        with self.assertRaises(ValidationError):
            self.svc.preview(tid, {'expected_version':1, 'profile_model':None})
        preview = self.svc.preview(tid, {'expected_version':1,'profile_model':'growth','account_id':'102'})
        self.assertEqual(preview['items'][0]['selection']['reservation']['tag_profile']['profile_version'], 'before-102')
        first = self.svc.run_now(tid, {'expected_version':1, 'idempotency_key':'growth-test-001', 'confirmed':True,'profile_model':'growth'})
        tasks = self.db.list_tasks(run_id=first['run_id'])
        self.assertEqual([t.account_snapshot['selection_profile']['profile']['version'] for t in tasks], ['before-101','before-102'])
        self.assertEqual(self.db.get_template(tid).config['profile_model'], 'revenue')


if __name__ == '__main__':
    unittest.main()
