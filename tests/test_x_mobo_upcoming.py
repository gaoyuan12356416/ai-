"""X template discovery, native wait and quota checks without an external write."""
import os
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.x_auto_posts.core import XAutoPostStore, XAutoPostStoreError
from features.x_auto_posts.publisher import AutoPostExecutor, _ClaimedSelectionStore
from features.x_auto_posts.repositories import MetricWindowRepository
from features.x_auto_posts.selector import MoboUpcomingWait, NoEligibleMaterial
from features.x_auto_posts.service import XAutoPostService, _PreviewStore
from features.x_tag_publish.selection import ProfileSelector
from scripts.test_x_auto_post_selector import (
    NOW, FakeMetricStore, FakePublishedHistory, FakeSource, FakeValidator,
    drama, material, request, rules,
)


class Source(FakeSource):
    schema = 'fixture'
    material_data_source = 6

    def __init__(self):
        super().__init__(materials={})
        self.at = int((NOW + timedelta(hours=23)).timestamp())
        self.listed = False
        self.materialises = False
        self.discoveries = 0
        self.unavailable = False

    def _stream_select(self, sql, params):
        if 'example_id' in sql:
            return [{'content_id': '77', 'lang': 'en', 'n': 1, 'example_id': '220'}]
        if 'cdreader_koc_drama' not in sql:
            self.discoveries += 1
        if self.unavailable:
            raise OSError('fixture source unavailable')
        return [dict(content_id='77', lang_min='en', lang_max='en',
                     deploy_min=None, deploy_max=None, live_min=None, live_max=None,
                     plan_min=self.at, plan_max=self.at, is_show=int(self.listed),
                     lang_id=3 if self.listed else -1, story_name='Upcoming')]

    def list_material_rows(self, *, content_id, language, products=None):
        if content_id == '77' and self.materialises:
            return [material('220', '77', product='Moboreels')]
        return self.materials.get(content_id, [])


class UpcomingTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.now = NOW
        self.store = XAutoPostStore(Path(temp.name) / 'auto.sqlite3', now_fn=lambda: self.now)
        self.config = dict(account_ids=['100'], language_source='account', body_template='{{desc}}',
                           material_products=['Moboreels'], drama_rule={}, material_rule={},
                           drama_launch_window_days='-1~0', schedule={'mode': 'random', 'daily_count': 1})
        self.template = self.store.create_template(name='Upcoming', config=self.config,
                                                    confirmation={'accepted': True})
        self.source = Source()
        self.validator = FakeValidator(overrides={'content_id': '77'})
        self.decision = SimpleNamespace(audit=lambda *args, **kwargs: {}, reason='fixture')
        for active in [
            patch.dict(os.environ, X_POST_AUTOMATION_INTERNAL_URL='http://fixture', X_POST_AUTO_INTERNAL_TOKEN='fixture'),
            patch('features.moboreels_upcoming.institution_id', return_value='fixture'),
            patch('features.x_tag_publish.selection.XPostAutoBridgeClient'),
            patch('features.x_tag_publish.selection.Decision', return_value=self.decision),
            patch('features.x_tag_publish.selection.ranked_dramas', side_effect=lambda source, rows, context: rows),
        ]:
            active.start()
            self.addCleanup(active.stop)

    def selector(self, store):
        return ProfileSelector(self.source, MetricWindowRepository(FakeMetricStore()),
                               FakePublishedHistory(), store, material_validator=self.validator)

    def run_slot(self, key='slot', trigger='manual'):
        selector = self.selector(self.store)
        service = SimpleNamespace(store=self.store, selector=selector, _run_create_lock=Lock(),
                                  _run_tasks=lambda *args: None, _now=lambda: self.now)
        return XAutoPostService._create_run(service, self.template, trigger_type=trigger, scheduled_at=self.now,
                                            publish_time='18:00', run_key=key)[0]

    def task(self, run, account='100'):
        return self.store.create_task(run_id=run.id, account_id=account, language='en',
                                      account_snapshot={'username': 'fixture', 'language_source': 'account',
                                                        'drama_language': 'en'}, account_snapshot_version=2)

    def req(self, run, task, **extra):
        rule = rules(material_products=self.config['material_products'],
                     drama={'resource_types': [], 'launch_window_days': '-1~0'})
        return request(rule, run_id=run.id, task_id=task.id, template_id=self.template.id,
                       template_version=self.template.version, account_id=task.account_id, now=self.now, **extra)

    def test_original_slot_waits_then_reserves_once_after_midnight(self):
        run = self.run_slot()
        self.assertEqual(self.run_slot().id, run.id)
        self.assertEqual(self.source.discoveries, 1)
        self.assertNotIn('stories', run.blacklist_snapshot['mobo_upcoming'])
        plan = self.store.mobo_upcoming_plan(run.id, lambda: self.fail('rediscovered plan'))
        self.assertEqual(plan['stories']['77']['example_material_id'], '220')
        task = self.task(run)
        claim = self.store.claim_next_executable_task(worker_id='fixture', lease_seconds=180, now=self.now)
        token = claim.reveal_claim_token()
        with self.assertRaises(MoboUpcomingWait) as waiting:
            self.selector(_ClaimedSelectionStore(self.store, token)).select_and_reserve(self.req(run, task))
        self.assertEqual(waiting.exception.upcoming['pending'], 1)
        executor = SimpleNamespace(store=self.store, _now=lambda: self.now)
        AutoPostExecutor._retry_or_fail(executor, claim.task, 'selection', waiting.exception, token)
        self.assertEqual(self.store.get_task(task.id).status, 'retry_wait')
        self.assertIsNone(self.store.get_task_reservation(task.id))
        self.assertIsNone(self.store.claim_next_executable_task(worker_id='fixture', lease_seconds=180, now=self.now))
        self.now += timedelta(days=2)
        self.source.listed = True
        claim = self.store.claim_next_executable_task(worker_id='fixture', lease_seconds=180, now=self.now)
        current = self.selector(_ClaimedSelectionStore(self.store, claim.reveal_claim_token()))
        self.source.at = int((self.now + timedelta(hours=2)).timestamp())
        with self.assertRaises(MoboUpcomingWait) as rescheduled:
            current.select_and_reserve(self.req(run, task))
        self.assertEqual(rescheduled.exception.upcoming['ready'], 0)
        self.now += timedelta(hours=2)
        with self.assertRaises(MoboUpcomingWait) as missing:
            current.select_and_reserve(self.req(run, task))
        self.assertEqual(missing.exception.upcoming['ready'], 1)
        self.source.materialises = True
        self.assertEqual(current.select_and_reserve(self.req(run, task)).material.source.material_id, '220')
        updated = self.store.get_task(task.id)
        self.assertEqual(updated.id, task.id)
        self.assertEqual(updated.scheduled_at_utc, self.now.isoformat())
        self.assertEqual(self.store.get_run(run.id).scheduled_at_utc, NOW.isoformat())
        self.assertTrue(current.select_and_reserve(self.req(run, task)).idempotent)

    def test_preview_is_readonly_old_or_explicit_runs_do_not_scan(self):
        plan = self.selector(self.store).upcoming_plan(self.config, self.now)
        fake_run, fake_task = SimpleNamespace(id=1), SimpleNamespace(id=1, account_id='100')
        req = self.req(fake_run, fake_task, preview_upcoming=plan)
        with self.assertRaises(MoboUpcomingWait):
            self.selector(_PreviewStore(self.store)).select_and_reserve(req)
        with self.store._reader() as conn:
            for table in ('x_auto_run','x_auto_task','x_auto_mobo_upcoming','x_auto_material_ledger'):
                self.assertEqual(conn.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)
        when = dict(scheduled_at_utc=self.now, shanghai_date=self.now.date().isoformat(), publish_time='18:00')
        run = self.store.create_run(template_id=self.template.id, template_version=1, run_key='legacy',
                                    trigger_type='manual', **when)
        with self.assertRaises(NoEligibleMaterial):
            self.selector(self.store).select_and_reserve(self.req(run,self.task(run)))
        legacy_hit = self.store.create_run(template_id=self.template.id, template_version=1, run_key='explicit',
                                           trigger_type='manual', mobo_upcoming=plan,
                                           blacklist_snapshot={'hit_material_publish': {'account_ids': []}}, **when)
        with patch('features.moboreels_upcoming.assess', side_effect=AssertionError('explicit run used watch')):
            with self.assertRaises(NoEligibleMaterial):
                self.selector(self.store).select_and_reserve(self.req(legacy_hit,self.task(legacy_hit)))
        sel = self.selector(self.store)
        self.assertIsNone(sel.upcoming_plan(dict(self.config, drama_launch_window_days=0), self.now))
        self.assertIsNone(sel.upcoming_plan(dict(self.config, material_products=['Dramawave']), self.now))
        self.assertEqual(self.source.discoveries, 1)

    def test_daily_slot_limit_is_atomic_and_manual_is_independent(self):
        self.template = self.store.set_template_enabled(self.template.id, True, expected_version=1)
        run1 = self.run_slot('first', 'auto')
        run2 = self.run_slot('second', 'auto')
        first, second = self.task(run1), self.task(run2)
        self.now += timedelta(days=1)
        self.source.listed, self.source.materialises = True, True
        sel = self.selector(self.store)
        sel.select_and_reserve(self.req(run1, first))
        evidence = {'mobo_upcoming': {'pending': 0, 'ready': 1}}
        with self.assertRaises(XAutoPostStoreError) as full:
            self.store.reserve_material(second.id, material_id='221',content_id='77',series_code='77',
                                        selection_snapshot=evidence)
        self.assertEqual(full.exception.code, 'x_mobo_upcoming_daily_full')
        self.assertIsNone(self.store.get_task_reservation(second.id))
        with self.assertRaises(NoEligibleMaterial) as already_used:
            sel.select_and_reserve(self.req(run2, second))
        self.assertIn('x_mobo_upcoming_daily_full', already_used.exception.rejection_counts)
        self.store.set_template_enabled(self.template.id, False, expected_version=1)
        with self.assertRaises(NoEligibleMaterial) as paused:
            sel.select_and_reserve(self.req(run2, second))
        self.assertIn('x_mobo_upcoming_paused', paused.exception.rejection_counts)
        manual = self.task(self.run_slot('manual'))
        self.store.reserve_material(manual.id, material_id='222',content_id='77',series_code='77',
                                    selection_snapshot=evidence)

    def test_discovery_read_failure_keeps_anchor_and_sibling_product(self):
        self.source.unavailable = True
        run = self.run_slot()
        self.assertEqual(run.blacklist_snapshot['mobo_upcoming']['scanned_at'], int(NOW.timestamp()))
        task = self.task(run)
        self.now += timedelta(hours=1)
        self.source.unavailable = False
        with self.assertRaises(MoboUpcomingWait):
            self.selector(self.store).select_and_reserve(self.req(run, task))
        self.assertEqual(self.store.mobo_upcoming_plan(run.id, None)['scanned_at'], int(NOW.timestamp()))

        # A valid same-language non-Mobo material may use its normal signed window.
        self.config['material_products'].append('Dramawave')
        self.source.unavailable = True
        self.source.dramas = [drama('77', deploy_time=int(self.now.timestamp()))]
        self.source.materials['77'] = [material('330','77')]
        self.assertEqual(self.selector(self.store).select_and_reserve(self.req(run,task)).material.source.material_id, '330')

    def test_impossible_mobo_metric_filter_finishes_without_waiting(self):
        from dataclasses import replace
        run = self.run_slot()
        task = self.task(run)
        constrained = rules(material_products=['Moboreels'], drama={'resource_types': [],
                            'launch_window_days': '-1~0', 'spend': {'min': '1'}})
        with self.assertRaises(NoEligibleMaterial):
            self.selector(self.store).select_and_reserve(replace(self.req(run,task), rules=constrained))


if __name__ == '__main__':
    unittest.main()
