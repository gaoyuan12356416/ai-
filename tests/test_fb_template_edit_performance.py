import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from features.fb_auto_posts.core import ActorScope, FBAutoPostStore, StoreError
from features.fb_auto_posts.repositories import PageGroup, PagePoolRepository, RepositoryError
from features.fb_auto_posts.service import Handler, Runtime, ServiceError
from features.fb_auto_posts.validation import ValidationError
from features import social_random_schedule as daily
from features.fb_auto_posts import target_schedule
from scripts.test_fb_auto_validation import payload
from scripts import test_fb_target_midnight


class EditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.actor = ActorScope('owner', 'Owner', False, '248')
        self.now = datetime(2026, 11, 23, 4, tzinfo=timezone.utc)
        self.store = FBAutoPostStore(Path(temp.name) / 'fb.sqlite3', now_fn=lambda: self.now)
        self.template = self.store.create_template(payload(), self.actor)
        self.tid = self.template['id']
        self.store.set_template_status(self.tid, True, self.actor, 1)

    def test_enabled_save_disables_only_target_and_preserves_original_version(self):
        other = self.store.create_template(dict(payload(), name='Other'), self.actor)
        self.store.set_template_status(other['id'], True, self.actor, 1)
        result = self.store.update_template(self.tid, dict(payload(), name='Edited'), self.actor, 1)
        self.assertEqual((result['name'], result['version'], result['status']), ('Edited', 2, 'disabled'))
        self.assertEqual(self.store.get_template(other['id'], self.actor)['status'], 'enabled')
        with self.store.connect() as conn:
            original = json.loads(conn.execute('SELECT config_json FROM fb_auto_template_version WHERE template_id=? AND version=1', (self.tid,)).fetchone()[0])
        self.assertEqual(original, self.template['config'])
        self.assertEqual(self.store.list_runs(self.actor)['total'], 0)

    def test_invalid_stale_or_out_of_scope_save_does_not_disable(self):
        for raw, actor, version, error in (
            (dict(payload(), name=''), self.actor, 1, ValidationError),
            (payload(), self.actor, 2, StoreError),
            (payload(), replace(self.actor, owner_user_id='999'), 1, StoreError),
        ):
            with self.assertRaises(error):
                self.store.update_template(self.tid, raw, actor, version)
            current = self.store.get_template(self.tid, self.actor)
            self.assertEqual((current['version'], current['status']), (1, 'enabled'))

    def test_failed_database_write_rolls_back_status_and_version(self):
        with self.store.connect() as conn:
            conn.execute("CREATE TRIGGER reject_edit BEFORE UPDATE ON fb_auto_template BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.update_template(self.tid, dict(payload(), name='Edited'), self.actor, 1)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM fb_auto_template_version WHERE template_id=?', (self.tid,)).fetchone()[0], 1)
        self.assertEqual(self.store.get_template(self.tid, self.actor)['status'], 'enabled')

    def test_http_save_returns_disabled_and_rechecks_group_scope(self):
        pages = Mock()
        pages.visible_group_ids.return_value = set(payload()['group_ids'])
        runtime = Runtime(self.store, pages, Mock(), Mock(), Mock(), 'fixture')
        handler = Handler.__new__(Handler)
        handler.runtime = runtime
        handler.command = 'POST'
        handler.path = '/api/admin/fb-auto-publish/templates/' + str(self.tid)
        handler.headers = {'Authorization': 'Bearer fixture'}
        handler.read_json = lambda: dict(payload(), expected_version=1, _actor=self.actor.__dict__)
        handler.send_json = Mock()
        handler.dispatch()
        code, result = handler.send_json.call_args.args
        self.assertEqual((code, result['template']['status']), (200, 'disabled'))
        pages.visible_group_ids.assert_called_once()
        pages.list_groups.assert_not_called()

    def test_compact_plans_keep_all_times_and_do_not_modify_frozen_storage(self):
        with self.store.connect() as conn:
            plan = daily.ensure_day_plan(conn, target_schedule.source_key(self.tid), '2026-11-23', 1,
                                         self.template['config'], ['10001', '10002'], lambda _: ['08:15', '20:05'], now=self.now)
        detail = self.store.get_template(self.tid, self.actor)
        public = detail['target_daily_plans'][0]
        self.assertEqual(public['targets'], [{'target_id': t['target_id'], 'times': t['times']} for t in plan['targets']])
        self.assertNotIn('config', public)
        with self.store.connect() as conn:
            self.assertEqual(daily.get_day_plan(conn, target_schedule.source_key(self.tid), '2026-11-23'), plan)

    def test_save_pauses_frozen_tasks_without_erasing_schedule_or_receipts(self):
        fixture = test_fb_target_midnight.TargetMidnightTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.plan()
        before = fixture.tasks()
        tid = fixture.template['id']
        result = fixture.store.update_template(tid, payload(), fixture.actor, 1)
        self.assertEqual(result['status'], 'disabled')
        self.assertEqual(fixture.tasks(), before)
        self.assertIsNone(fixture.store.claim_prepare_next('test', task_id=before[0]['id']))
        self.assertIsNone(fixture.store.claim_due_slot('test'))
        fixture.store.set_template_status(tid, True, fixture.actor, 2)
        task = fixture.store.claim_prepare_next('test', task_id=before[0]['id'])
        self.assertEqual(task['id'], before[0]['id'])


class GroupReadTests(unittest.TestCase):
    def setUp(self):
        self.actor = ActorScope('a', 'A', False, '248')
        self.pages = Mock()
        self.pages.list_groups.return_value = [PageGroup('6', '248', 0, 'Test', '1479', 'Dramawave', 10, 7)]
        self.runtime = Runtime(Mock(), self.pages, Mock(), Mock(), Mock(), 'fixture')

    def test_cache_expiry_actor_isolation_and_fresh_save_check(self):
        with patch('features.fb_auto_posts.service.time.monotonic', return_value=10):
            first = self.runtime.groups(self.actor)
            self.assertEqual(self.runtime.groups(self.actor), first)
            self.assertEqual(self.pages.list_groups.call_count, 1)
            self.runtime.groups(replace(self.actor, owner_user_id='999'))
            self.assertEqual(self.pages.list_groups.call_count, 2)
        with patch('features.fb_auto_posts.service.time.monotonic', return_value=41):
            self.runtime.groups(self.actor)
            self.assertEqual(self.pages.list_groups.call_count, 3)
        self.pages.visible_group_ids.return_value = set()
        with self.assertRaises(ServiceError):
            self.runtime.resolve_source({'group_ids': ['6']}, self.actor)
        self.pages.visible_group_ids.assert_called_once()

    def test_failed_refresh_is_not_cached_or_replaced_by_stale_success(self):
        with patch('features.fb_auto_posts.service.time.monotonic', return_value=0):
            self.runtime.groups(self.actor)
        self.pages.list_groups.side_effect = RuntimeError('fixture read failed')
        with patch('features.fb_auto_posts.service.time.monotonic', return_value=31):
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    self.runtime.groups(self.actor)
        self.assertEqual(self.pages.list_groups.call_count, 3)

    def test_concurrent_refresh_coalesces_without_blocking_other_owners(self):
        entered, release = threading.Event(), threading.Event()
        groups = self.pages.list_groups.return_value
        def load(**scope):
            if scope['owner_user_id'] == '248':
                entered.set()
                self.assertTrue(release.wait(3))
            return groups
        self.pages.list_groups.side_effect = load
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(self.runtime.groups, self.actor)
            self.assertTrue(entered.wait(2))
            second = pool.submit(self.runtime.groups, self.actor)
            other = pool.submit(self.runtime.groups, replace(self.actor, owner_user_id='999'))
            try:
                self.assertTrue(other.result(timeout=2)['ok'])
            finally:
                release.set()
            self.assertEqual(first.result(), second.result())
        self.assertEqual(self.pages.list_groups.call_count, 2)

    def test_group_validation_uses_selected_live_rows_and_owner_scope(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE ads_facebook_page_group(id INTEGER, user_id INTEGER, is_delete INTEGER, type INTEGER)')
        conn.executemany('INSERT INTO ads_facebook_page_group VALUES(?,?,?,?)', [(1, 248, 0, 0), (2, 999, 0, 1), (3, 248, 1, 0), (4, 248, 0, 2)])
        class SQL:
            schema = 'fixture'
            def select(self, sql, params):
                return [dict(r) for r in conn.execute(sql.replace('`fixture`.', '').replace('%s', '?').replace(' AS CHAR', ' AS TEXT'), params)]
        repo = PagePoolRepository(SQL())
        self.assertEqual(repo.visible_group_ids(['1', '2', '3', '4'], is_admin=False, owner_user_id='248'), {'1'})
        self.assertEqual(repo.visible_group_ids(['1', '2', '3', '4'], is_admin=True, owner_user_id=''), {'1', '2'})
        conn.execute('UPDATE ads_facebook_page_group SET is_delete=1 WHERE id=1')
        self.assertEqual(repo.visible_group_ids(['1'], is_admin=False, owner_user_id='248'), set())
        with self.assertRaises(RepositoryError):
            repo.visible_group_ids(['1 OR 1=1'], is_admin=True, owner_user_id='')


if __name__ == '__main__':
    unittest.main()
