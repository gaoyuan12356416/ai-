"""Offline range, reservation and publish-time contracts against each runtime."""
import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.drama_launch_window import normalize, bounds, release_timing, frozen_release_times, POLICY


class WindowTests(unittest.TestCase):
    def test_validation_and_legacy_values(self):
        for value, expected in [(0, 0), ('7', 7), (' -1 ~ 0 ', '-1~0'),
                                ('【-1～0】', '-1~0'), ('-3~-1', '-3~-1'), ('0~7', '0~7')]:
            self.assertEqual(normalize(value), expected)
        for value in [True, None, '', '-1', '0~-1', '-3651~0', '0~3651', '1.5~2', 'bad']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize(value)

    def test_rolling_inclusive_bounds_and_exact_due(self):
        now = datetime(2026, 11, 17, 8, 30, tzinfo=timezone.utc)
        epoch = int(now.timestamp())
        self.assertEqual(bounds('-1~0', epoch), (epoch, epoch + 86400))
        self.assertEqual(bounds('-2~-1', epoch), (epoch + 86400, epoch + 172800))
        self.assertEqual(bounds('0~7', epoch), (epoch - 7 * 86400, epoch))
        plan = release_timing(epoch + 3600, now)
        self.assertEqual(plan['publish_at_utc'], (now + timedelta(hours=1)).isoformat())
        self.assertEqual(plan['advance_seconds'], 0)


class XTests(unittest.TestCase):
    def setUp(self):
        from scripts import test_x_auto_post_selector as f
        self.f = f
        self.epoch = int(f.NOW.timestamp())

    def selection(self, release, window='-1~0', history=()):
        f = self.f
        source = f.FakeSource([f.drama('C1', deploy_time=release)], {'C1': [f.material('101', 'C1')]})
        store = f.FakeStore()
        validator = f.FakeValidator()
        from features.hit_publish import future_allowed
        original = validator.validate
        def validate(mid):
            if release > self.epoch:
                self.assertTrue(future_allowed('C1', 'en', release))
                self.assertFalse(future_allowed('OTHER', 'en', release))
            return original(mid)
        validator.validate = validate
        selector = f.TwoStageSelector(source, f.MetricWindowRepository(f.FakeMetricStore()),
            f.FakePublishedHistory(history), store, material_validator=validator)
        result = selector.select_and_reserve(f.request(f.rules(drama={'launch_window_days': window})))
        return result, store, source

    def test_future_selection_and_boundary_exclusions(self):
        from features.x_auto_posts.selector import NoEligibleMaterial
        for offset in (0, 1, 43200, 86400):
            result, store, source = self.selection(self.epoch + offset)
            self.assertEqual(result.material.source.material_id, '101')
            self.assertEqual(source.drama_call['now_epoch'], self.epoch + 86400)
            snapshot = store.reserve_calls[0]['selection_snapshot']
            if offset:
                self.assertEqual(snapshot['hit_publish_timing']['policy'], POLICY)
                self.assertEqual(snapshot['hit_publish_timing']['publish_at_utc'],
                                 datetime.fromtimestamp(self.epoch + offset, timezone.utc).isoformat())
        for offset in (-1, 86401):
            with self.assertRaises(NoEligibleMaterial):
                self.selection(self.epoch + offset)
        for window in (0, 7):
            with self.assertRaises(NoEligibleMaterial):
                self.selection(self.epoch + 3600, window)
        with self.assertRaises(NoEligibleMaterial):
            self.selection(self.epoch + 3600, history=('101',))

    def test_real_reservation_hydration_and_final_publish_gate(self):
        from scripts.test_x_auto_post_store import AutoPostStoreTests
        from features.x_tag_publish.hit_timing import assert_due
        from features.x_posts.service import XPostError
        fixture = AutoPostStoreTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        task = fixture.create_task(fixture.create_run(fixture.create_template(), 'future-range'))
        release = int(fixture.clock().timestamp()) + 3600
        timing = release_timing(release, fixture.clock())
        snapshot = dict(drama=dict(content_id='C1', language='en', deploy_time=release), hit_publish_timing=timing)
        fixture.store.reserve_material(task_id=task.id, material_id='101', content_id='C1',
                                       series_code='S1', selection_snapshot=snapshot)
        task = fixture.store.get_task(task.id)
        self.assertEqual(datetime.fromisoformat(task.scheduled_at_utc), datetime.fromtimestamp(release, timezone.utc))
        self.assertEqual(frozen_release_times(task), {'C1:en': release})
        postdb = Path(fixture.temp.name) / 'post.sqlite3'
        with sqlite3.connect(postdb) as conn:
            conn.execute('CREATE TABLE x_post_manual_run(id INTEGER, trigger_source TEXT, external_task_key TEXT)')
            conn.execute('INSERT INTO x_post_manual_run VALUES(1,?,?)', ('auto_template', 'x-auto-task-' + str(task.id)))
        post = SimpleNamespace(db_path=postdb, get_queue=lambda _: dict(manual_run_id=1, account_id=task.account_id, material_id='101'))
        with self.assertRaises(XPostError):
            assert_due(str(fixture.db_path), post, 1, fixture.clock())
        assert_due(str(fixture.db_path), post, 1, datetime.fromtimestamp(release, timezone.utc))
        self.assertEqual(fixture.store.get_task(task.id).selection, snapshot)


class FBTests(unittest.TestCase):
    def setUp(self):
        from features.fb_auto_posts.repositories import MaterialRepository
        from features.fb_auto_posts.validation import normalize_template_payload
        from scripts.test_fb_auto_validation import payload
        self.now = datetime(2026, 11, 17, 8, tzinfo=timezone.utc)
        self.epoch = int(self.now.timestamp())
        self.release = self.epoch + 3600
        self.config = normalize_template_payload({**payload(), 'drama_launch_window_days': '-1~0'})
        self.config.update(language='en', app_id='1479', product='Dramawave', metric_product='Dramawave', metric_platform=0)
        metrics = SimpleNamespace(load_metric_window=lambda **_: SimpleNamespace(by_drama={}, by_material={}, generation_ids=(), dates=()))
        mysql = SimpleNamespace(schema='ads', blacklist_schema='ads', select=self.query)
        self.repo = MaterialRepository(mysql, metrics, now_fn=lambda: self.now)

    def query(self, sql, params):
        if 'MAX(d0.id)' in sql:
            return [dict(content_id='C1', language='en', resource_type_v2='1', deploy_time=self.release)] if params[1] < self.release <= params[2] else []
        if 'DISTINCT TRIM(d.content_id)' in sql:
            return [dict(content_id='C1')] if params[1] < self.release <= params[2] else []
        if 'ads_custom_source s' in sql:
            priority = 's.data_source_id IN' in sql
            low, high = params[5:7] if priority else params[4:6]
            if low < self.release <= high and (priority or params[-2] == 0):
                return [dict(material_id='101', content_id='C1', language='en', video_duration=30, media_url='https://media.test/101.mp4')]
            return []
        raise AssertionError(sql)

    def test_future_range_source_filters_and_release_snapshot(self):
        for offset in (0, 1, 43200, 86400):
            self.release = self.epoch + offset
            candidates = self.repo.candidate_snapshot(self.config).candidates
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].deploy_time, self.release)
        for offset in (-1, 86401):
            self.release = self.epoch + offset
            self.assertFalse(self.repo.candidate_snapshot(self.config).candidates)
        self.release = self.epoch + 3600
        for window in (0, 7):
            self.assertFalse(self.repo.candidate_snapshot({**self.config, 'drama_launch_window_days': window}).candidates)
        self.config['drama_rule']['spend_min'] = 1
        self.assertFalse(self.repo.candidate_snapshot(self.config).candidates)

    def test_real_task_is_claimable_only_at_release(self):
        from scripts.test_fb_auto_store import StoreTests, Pages, Materials
        from features.fb_auto_posts.repositories import CandidateSnapshot
        fixture = StoreTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        release = int(fixture.now.timestamp()) + 3600
        original = Materials().candidate_snapshot({}).candidates[0]
        material = replace(original, deploy_time=release)
        class FutureMaterials(Materials):
            def candidate_snapshot(self, config):
                return CandidateSnapshot((material,), (), ())
        fixture.enable()
        result = fixture.store.create_run(fixture.template['id'], 'manual:future-range', 'manual', fixture.actor, Pages(), FutureMaterials())
        with fixture.store.connect() as conn:
            row = dict(conn.execute("SELECT * FROM fb_auto_task WHERE run_id=? AND status='planned'", (result['run_id'],)).fetchone())
            self.assertEqual(datetime.fromisoformat(row['planned_publish_at_utc']), datetime.fromtimestamp(release, timezone.utc))
            self.assertEqual(json.loads(row['selection_json'])['hit_publish_timing']['advance_seconds'], 0)
            conn.execute("UPDATE fb_auto_task SET status='ready',prepared_at_utc=?,prepared_media_url='https://media.test/ready.mp4',media_url='https://media.test/ready.mp4' WHERE id=?", (fixture.now.isoformat(), row['id']))
        self.assertIsNone(fixture.store.claim_next('future-test', 900))
        fixture.now = datetime.fromtimestamp(release, timezone.utc)
        claimed = fixture.store.claim_next('future-test', 900)
        self.assertEqual(claimed['id'], row['id'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--platform', choices=['x', 'fb'], required=True)
    args = parser.parse_args()
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                               for cls in (WindowTests, XTests if args.platform == 'x' else FBTests))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
