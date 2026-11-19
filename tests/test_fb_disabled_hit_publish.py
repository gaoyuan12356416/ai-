"""Explicit publishes on disabled templates, without any GPU or Meta writes."""
import json
import unittest
from datetime import timedelta

from scripts import test_fb_hit_drama as drama_fixture
from tests import test_fb_hit_material_batch as material_fixture
from features.fb_auto_posts import explicit_publish
from features.fb_auto_posts.core import ActorScope, StoreError
from features.fb_auto_posts.hit_drama import publish_drama


class DisabledHitPublishTests(unittest.TestCase):
    def fixture(self, kind):
        fixture = material_fixture.BatchTests() if kind == 'material' else drama_fixture.HitDramaTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store, actor, tid = fixture.store, fixture.actor, fixture.template['id']
        store.set_template_status(tid, False, actor, 1)
        if kind == 'material':
            publish = fixture.publish
        else:
            publish = lambda request=None: publish_drama(fixture.runtime, tid, actor, request or fixture.request)
        return fixture, publish

    def test_disabled_hit_drama_and_material_reach_publish_claim_without_enabling(self):
        for kind in ('drama', 'material'):
            with self.subTest(kind=kind):
                f, publish = self.fixture(kind)
                result = publish()
                self.assertGreater(result['queued'], 0)
                self.assertEqual(publish()['run_id'], result['run_id'])
                with f.store.connect() as conn:
                    task = dict(conn.execute("SELECT * FROM fb_auto_task WHERE status='planned' ORDER BY id LIMIT 1").fetchone())
                    config = json.loads(conn.execute('SELECT config_json FROM fb_auto_run WHERE id=?', (result['run_id'],)).fetchone()[0])
                    self.assertIs(config[explicit_publish.MARKER], True)
                    self.assertEqual(conn.execute('SELECT status FROM fb_auto_template').fetchone()[0], 'disabled')
                    self.assertEqual(conn.execute('SELECT count(*) FROM fb_auto_due_slot').fetchone()[0], 0)
                self.assertIsNotNone(f.store.claim_prepare_next('prepare', task_id=task['id']))
                self.assertTrue(f.store.defer_prepare(task['id'], 'temporary', 'retry')['deferred'])
                now = f.store.now_fn()
                f.store.now_fn = lambda: now + timedelta(minutes=6)
                self.assertIsNotNone(f.store.claim_prepare_next('prepare', task_id=task['id']))
                prepared = f.store.complete_prepare(task['id'], {'media_url': 'https://media.test/prepared.mp4'})
                self.assertEqual(prepared['status'], 'ready')
                # Future materials still wait for their frozen publication time.
                if kind == 'material':
                    self.assertIsNone(f.store.claim_next('publish', task_id=task['id']))
                    from datetime import datetime
                    due = datetime.fromisoformat(task['planned_publish_at_utc'])
                    f.store.now_fn = lambda: due
                self.assertIsNotNone(f.store.claim_next('publish', task_id=task['id']))
                self.assertEqual(f.store.get_template(f.template['id'], f.actor)['status'], 'disabled')
                self.assertIsNone(f.store.claim_next('second-publisher', task_id=task['id']))

    def test_status_changes_preserve_new_explicit_runs_and_old_runs_stay_paused(self):
        for kind in ('drama', 'material'):
            with self.subTest(kind=kind):
                f, publish = self.fixture(kind)
                result = publish()
                f.store.set_template_status(f.template['id'], True, f.actor, 1)
                f.store.set_template_status(f.template['id'], False, f.actor, 1)
                with f.store.connect() as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM fb_auto_task WHERE status='planned'").fetchone()[0], result['queued'])
                    config = json.loads(conn.execute('SELECT config_json FROM fb_auto_run').fetchone()[0])
                    config.pop(explicit_publish.MARKER)
                    conn.execute('UPDATE fb_auto_run SET config_json=?', (json.dumps(config),))
                self.assertIsNone(f.store.claim_prepare_next('old-run'))

    def test_ordinary_manual_auto_version_owner_and_deleted_guards_remain(self):
        for kind in ('drama', 'material'):
            with self.subTest(kind=kind):
                f, publish = self.fixture(kind)
                with self.assertRaises(StoreError):
                    f.store.enqueue_manual_due_slot(f.template['id'], f.actor, expected_template_version=1, operation_id='ordinary-manual-operation')
                with self.assertRaises(StoreError):
                    f.store.create_run(f.template['id'], 'auto-test', 'auto', f.actor, f.runtime.pages, f.runtime.materials, expected_template_version=1)
                with self.assertRaises(StoreError):
                    publish(dict(f.request, expected_version=2))
                with self.assertRaises(StoreError):
                    f.store.get_template(f.template['id'], ActorScope('other', 'other', False, 'other'))
                result = publish()
                with f.store.connect() as conn:
                    conn.execute("UPDATE fb_auto_template SET status='deleted'")
                self.assertIsNone(f.store.claim_prepare_next('deleted'))
                with self.assertRaises(StoreError):
                    publish(dict(f.request, operation_id='new-after-delete'))

    def test_disabled_material_does_not_reserve_automatic_daily_capacity(self):
        f, publish = self.fixture('material')
        f.runtime.max_daily_jobs = 2
        self.assertEqual(publish()['queued'], 2)


if __name__ == '__main__':
    unittest.main()
