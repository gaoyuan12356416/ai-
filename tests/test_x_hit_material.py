from contextlib import closing
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import test_x_auto_post_selector as fx
from scripts.test_x_hit_drama import HitDramaTests, snapshot
from features.hit_publish import POLICY, timing
from features.x_tag_publish.selection import ProfileSelector
from features.x_tag_publish.hit_timing import assert_due
from features.x_auto_posts.publisher import AutoPostExecutor


class MaterialBatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture=HitDramaTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.service,self.store,self.template=self.fixture.service,self.fixture.store,self.fixture.template
        selector=self.service.selector
        selector.source._stream_select=lambda sql,args:[dict(material_id='1',content_id='123')]
        selector.history=SimpleNamespace(seen_material_ids=lambda ids:[])
        selector.material_validator=SimpleNamespace(validate=lambda mid:dict(material_id=mid,content_id='123',material_language='en',source_duration=60))
        self.request=dict(expected_version=1,material_ids=['1','2'],idempotency_key='new-material-123',confirmed=True,_actor=self.fixture.actor)
        profile,meta=snapshot();profile['accounts']['x-102']=profile['accounts']['x-101'].copy()
        self.profile=(profile,meta)

    def publish(self,request=None):
        with patch('features.x_tag_publish.hit_material.catalog_labels',return_value={('123','en'):dict(audience='female',themes=['sweet_romance'])}),patch('features.x_tag_publish.hit_material.portrait_snapshot',return_value=self.profile):
            return self.service.run_now(self.template['id'],request or self.request)

    def test_one_to_one_and_response_loss_reuses_binding(self):
        result=self.publish()
        self.assertEqual(result['matched_channels'],2)
        self.assertEqual({e['material_id'] for e in result['assignments']},{'1','2'})
        self.assertEqual(len({e['account_id'] for e in result['assignments']}),2)
        with patch.object(self.service.selector.material_validator,'validate',side_effect=AssertionError('no rehydrate')):
            again=self.publish()
        self.assertEqual(result['assignments'],again['assignments']);self.assertTrue(again['idempotent'])
        with self.assertRaises(Exception):self.publish(dict(self.request,material_ids=['3']))

    def test_invalid_material_is_local_and_long_video_uses_capable_account(self):
        def validate(mid):
            if mid=='2':raise ValueError('missing')
            return dict(material_id=mid,content_id='123',material_language='en',source_duration=1300)
        self.service.selector.material_validator.validate=validate
        self.service.account_bridge.accounts_by_id['101'].update(long_video_eligible=False,subscription_type='none')
        self.service.account_bridge.accounts_by_id['102'].update(long_video_eligible=True,subscription_type='premium')
        result=self.publish()
        self.assertEqual([(e['material_id'],e['account_id']) for e in result['assignments']],[('1','102')])
        self.assertEqual(result['skipped_materials'][0]['material_id'],'2')

    def test_future_exact_selector_reserves_only_assigned_material(self):
        release=int((fx.NOW+timedelta(days=1)).timestamp())
        source=fx.FakeSource([replace(fx.drama('C1'),deploy_time=release)],{'C1':[fx.material('101','C1'),fx.material('102','C1')]})
        hit=dict(account_ids=['101'],target=dict(content_id='C1'),assignments={'101':dict(material_id='102')},release_times={'C1:en':release},timing_policy=POLICY)
        store=fx.FakeStore();store.get_run=lambda _:SimpleNamespace(blacklist_snapshot={'hit_material_publish':hit})
        selector=ProfileSelector(source,fx.MetricWindowRepository(fx.FakeMetricStore()),fx.FakePublishedHistory(),store,material_validator=fx.FakeValidator())
        executor=object.__new__(AutoPostExecutor);executor.store=store;executor.selector=selector
        with patch.dict(os.environ,X_POST_AUTOMATION_INTERNAL_URL='http://127.0.0.1:8821',X_POST_AUTO_INTERNAL_TOKEN='a'*48),patch('features.x_tag_publish.selection.XPostAutoBridgeClient') as bridge,patch('features.x_tag_publish.policy.portrait_snapshot',return_value=snapshot()),patch('features.x_tag_publish.policy.catalog_labels',return_value={('C1','en'):dict(audience='female',themes=['sweet_romance'])}):
            bridge.return_value.account_snapshot.return_value=dict(id=101,x_user_id='x-101',drama_language='en')
            result=executor._phase_selector('claim').select_and_reserve(fx.request())
        self.assertEqual(result.material.source.material_id,'102')
        self.assertEqual(result.drama.deploy_time,release)

    def test_reserved_future_task_prepares_now_and_waits_to_publish(self):
        result=self.publish(dict(self.request,material_ids=['1']))
        task=self.store.list_tasks(run_id=result['run_id'])[0]
        now=self.service._now();release=now+timedelta(days=1)
        plan=timing({'123:en':int(release.timestamp())},'123','en',now)
        self.store.reserve_material(task_id=task.id,material_id='1',content_id='123',selection_snapshot={'hit_publish_timing':plan})
        self.assertEqual(self.store.get_task(task.id).scheduled_at_utc,plan['publish_at_utc'])
        claim=self.store.claim_next_executable_task(worker_id='prepare',lease_seconds=60)
        self.assertEqual(claim.claim_phase,'prepare')
        self.store.transition_task(task.id,'ready',expected_statuses={'preparing'},claim_token=claim.reveal_claim_token(),updates={'claim_token':'','lease_expires_at_utc':'','claim_phase':'','claim_worker':''})
        self.assertIsNone(self.store.claim_next_executable_task(worker_id='publish',lease_seconds=60))
        self.assertEqual(self.store.claim_next_executable_task(worker_id='publish',lease_seconds=60,now=release-timedelta(minutes=30)).claim_phase,'publish')


class FinalTimingTests(unittest.TestCase):
    def test_dispatch_prepares_early_but_retains_publish_due_time(self):
        from features.x_tag_publish.dispatch import TagLedgerAdapter
        from features.pipeline_dispatch.adapters import LedgerSpec, stamp
        due = fx.NOW + timedelta(days=1)
        adapter = TagLedgerAdapter(LedgerSpec('x', 'queue', '', 'x_post_queue'))
        row = dict(id=9000000000001, status='queued', account_id=101, material_id='1',
                   scheduled_at=due.isoformat(), created_at=fx.NOW.isoformat(),
                   random_template_enabled=1, ancestor_source_keys=['x:auto:9000000000002'],
                   _hit_publish_timing=dict(prepare_at_utc=fx.NOW.isoformat()))
        offer = adapter.project(row, fx.NOW)
        self.assertEqual((offer['stage'], offer['scheduled_at']), ('prepare', stamp(fx.NOW)))
        retry = fx.NOW + timedelta(minutes=2)
        self.assertEqual(adapter.project(dict(row, next_attempt_at_utc=retry.isoformat()), fx.NOW)['scheduled_at'], stamp(retry))
        ready = dict(row, random_prepared=True, _dispatch_artifact=dict(url='https://example.test/prepared.mp4', sha256='a'*64, size=100, duration_seconds=60, mime='video/mp4', profile='test'))
        offer = adapter.project(ready, fx.NOW)
        self.assertEqual((offer['stage'], offer['scheduled_at']), ('publish', stamp(due)))
        # Existing queue behavior retains its previous eligibility timestamp.
        del row['_hit_publish_timing']
        self.assertEqual(adapter.project(row, fx.NOW)['scheduled_at'], stamp(due))

    def test_canonical_check_rejects_early_write_and_accepts_exact_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            post,auto=Path(folder)/'post.db',Path(folder)/'auto.db'
            due=fx.NOW+timedelta(hours=3)
            with closing(sqlite3.connect(post)) as c, c:
                c.execute('CREATE TABLE x_post_manual_run(id,trigger_source,external_task_key)')
                c.execute("INSERT INTO x_post_manual_run VALUES(1,'auto_template','x-auto-task-10')")
            with closing(sqlite3.connect(auto)) as c, c:
                c.execute('CREATE TABLE x_auto_task(id,account_id,material_id,selection_json,scheduled_at_utc)')
                c.execute('INSERT INTO x_auto_task VALUES(10,101,1,?,?)',(json.dumps({'hit_publish_timing':{'publish_at_utc':due.isoformat()}}),due.isoformat()))
            store=SimpleNamespace(db_path=post,get_queue=lambda q:dict(id=q,manual_run_id=1,account_id=101,material_id='1'))
            with self.assertRaisesRegex(Exception,'预约时间'):assert_due(auto.as_uri(),store,5,fx.NOW)
            assert_due(auto.as_uri(),store,5,due)
