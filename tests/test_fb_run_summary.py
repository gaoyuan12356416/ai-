import json
import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from features.fb_auto_posts.run_summary import annotate_runs


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE fb_auto_task(run_id,status,planned_publish_at_utc,lease_owner,lease_expires_at_utc,completed_at_utc);
            CREATE TABLE fb_auto_mobo_wait(run_id);
        ''')
        self.now = datetime(2026, 11, 24, 9, tzinfo=timezone.utc)
        self.run = dict(id=1, status='running', template_status='enabled', trigger_type='auto',
                        config_json='{}', completed_at_utc='', queued_tasks=1897)

    def add(self, status, n=1, due='2026-11-22T03:00:00Z', lease='', completed=''):
        self.conn.executemany('INSERT INTO fb_auto_task VALUES(?,?,?,?,?,?)',
                              [(1, status, due, 'worker' if lease else '', lease, completed)] * n)

    def summary(self):
        before = list(self.conn.iterdump())
        self.conn.execute('PRAGMA query_only=ON')
        annotate_runs(self.conn, [self.run], self.now)
        self.assertEqual(before, list(self.conn.iterdump()))
        c = self.run['task_counts']
        self.assertEqual(c['total'], sum(c[k] for k in ('published', 'pending', 'active', 'review', 'failed', 'skipped', 'cancelled')))
        return self.run

    def test_actual_nov22_case_is_overdue_not_running_or_1897_waiting(self):
        self.add('published', 1852)
        self.add('failed', 42)
        self.add('ready', 3)
        result = self.summary()
        self.assertEqual(result['display_status'], 'attention_required')
        self.assertEqual(result['task_counts']['pending'], 3)
        self.assertEqual(result['task_counts']['overdue'], 3)
        self.assertEqual(result['queued_tasks'], 1897)
        self.assertEqual(result['status'], 'running')
        self.assertEqual(result['display_completed_at_utc'], '')

    def test_deleted_template_pending_is_stopped(self):
        self.run['template_status'] = 'deleted'
        self.add('ready')
        self.assertEqual(self.summary()['display_status'], 'stopped')

    def test_disabled_template_pending_is_paused(self):
        self.run['template_status'] = 'disabled'
        self.add('planned')
        self.assertEqual(self.summary()['display_status'], 'paused')

    def test_explicit_manual_run_remains_independent(self):
        self.run.update(template_status='disabled', trigger_type='manual', config_json=json.dumps({'explicit_publish_independent': 1}))
        self.add('ready', due='2026-11-24T08:00:00Z')
        self.assertEqual(self.summary()['display_status'], 'waiting')

    def test_valid_lease_is_running_even_when_template_disabled(self):
        self.run['template_status'] = 'disabled'
        self.add('preparing', lease='2026-11-24T10:00:00+00:00')
        self.add('unknown')
        self.assertEqual(self.summary()['display_status'], 'running')

    def test_expired_lease_is_review_not_active(self):
        self.add('running', lease='2026-11-24T08:00:00Z')
        result = self.summary()
        self.assertEqual(result['display_status'], 'attention_required')
        self.assertEqual(result['task_counts']['active'], 0)
        self.assertEqual(result['task_counts']['stale_active'], 1)

    def test_unknown_or_submitted_not_completed(self):
        self.run.update(template_status='deleted', completed_at_utc='2026-11-23T01:00:00Z')
        self.add('unknown'); self.add('submitted'); self.add('ready')
        result = self.summary()
        self.assertEqual(result['display_status'], 'attention_required')
        self.assertIn('模板已删除', result['display_reason'])
        self.assertEqual(result['display_completed_at_utc'], '')

    def test_beijing_midnight_and_future_are_separate(self):
        self.add('planned', due='2026-11-23T16:00:00Z')
        self.add('ready', due='2026-11-24T10:00:00+00:00')
        result = self.summary()
        self.assertEqual(result['display_status'], 'waiting')
        self.assertEqual(result['task_counts']['overdue'], 0)
        self.assertEqual(result['task_counts']['scheduled'], 1)

    def test_future_only_is_scheduled(self):
        self.add('planned', due='2026-11-24T10:00:00Z')
        self.assertEqual(self.summary()['display_status'], 'scheduled')

    def test_material_wait_is_not_an_invented_task(self):
        self.conn.execute('INSERT INTO fb_auto_mobo_wait VALUES(1)')
        result = self.summary()
        self.assertEqual(result['display_status'], 'waiting')
        self.assertEqual(result['task_counts']['total'], 0)
        self.assertEqual(result['task_counts']['waiting_materials'], 1)

    def test_terminal_counts_replace_stale_running_without_writes(self):
        self.add('published', completed='2026-11-22T10:00:00Z')
        self.add('failed_without_retry', completed='2026-11-22T11:00:00Z')
        result = self.summary()
        self.assertEqual(result['display_status'], 'partial_failed')
        self.assertEqual(result['display_completed_at_utc'], '2026-11-22T11:00:00Z')

    def test_unrecognized_status_is_not_success(self):
        self.add('future_new_status')
        self.assertEqual(self.summary()['display_status'], 'attention_required')

    def test_authorized_ids_only_and_chunk_limit(self):
        self.add('published')
        runs = [dict(self.run, id=rid) for rid in range(2, 1003)]
        annotate_runs(self.conn, runs, self.now)
        self.assertTrue(all(r['task_counts']['total'] == 0 for r in runs))


class StoreIntegrationTests(unittest.TestCase):
    def test_scoped_list_and_detail_share_read_only_statistics(self):
        from scripts.test_fb_target_midnight import TargetMidnightTests
        from features.fb_auto_posts.core import StoreError
        fixture = TargetMidnightTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.plan()
        fixture.now = datetime(2026, 11, 24, 9, tzinfo=timezone.utc)
        store, actor = fixture.store, fixture.actor
        with store.connect() as conn:
            before = list(conn.iterdump())
        result = store.list_runs(actor)
        self.assertTrue(result['items'])
        for row in result['items']:
            detail = store.get_run(row['id'], actor)['run']
            self.assertEqual(row['task_counts'], detail['task_counts'])
            self.assertEqual(row['display_status'], detail['display_status'])
            self.assertNotIn('config_json', row)
        stranger = replace(actor, owner_user_id='999')
        self.assertEqual(store.list_runs(stranger)['items'], [])
        with self.assertRaises(StoreError):
            store.get_run(result['items'][0]['id'], stranger)
        with store.connect() as conn:
            self.assertEqual(before, list(conn.iterdump()))


if __name__ == '__main__':
    unittest.main()
