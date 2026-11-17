"""Offline checks for the explicit-drama launch-window exception on each lane."""
import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FBTests(unittest.TestCase):
    def setUp(self):
        from features.fb_auto_posts.repositories import MaterialRepository
        from features.fb_auto_posts.validation import normalize_template_payload
        from scripts.test_fb_auto_validation import payload
        self.now = datetime(2026, 11, 17, 4, tzinfo=timezone.utc)
        self.deploy = int((self.now - timedelta(days=34)).timestamp())
        self.queries = []
        self.duration = 60
        self.config = normalize_template_payload(payload())
        self.config.update(app_id='1479', product='Dramawave', metric_product='Dramawave',
                           metric_platform=0, language='en', drama_launch_window_days=7)
        self.hit = dict(version=2, content_id='C1', content_ids=['C1'],
                        by_language={'en': ['C1']}, languages=['en'])
        metrics = SimpleNamespace(load_metric_window=lambda **_: SimpleNamespace(
            by_drama={}, by_material={}, generation_ids=(), dates=()))
        mysql = SimpleNamespace(schema='ads', blacklist_schema='ads', select=self.select)
        self.repo = MaterialRepository(mysql, metrics, now_fn=lambda: self.now)

    def select(self, sql, params):
        self.queries.append((sql, params))
        if 'MAX(d0.id)' in sql:
            return ([dict(content_id='C1', language='en', resource_type_v2='1')]
                    if params[1] < self.deploy <= params[2] else [])
        if 'FROM `ads`.ads_custom_source s' in sql:
            priority = 's.data_source_id IN' in sql
            lower, upper = params[5:7] if priority else params[4:6]
            if not lower < self.deploy <= upper or (not priority and params[-2] != 0):
                return []
            return [dict(material_id='101', content_id='C1', media_url='https://media.test/101.mp4',
                         language='en', video_duration=self.duration)]
        if 'DISTINCT TRIM(d.content_id)' in sql:
            return [dict(content_id='C1')] if params[1] < self.deploy <= params[2] else []
        raise AssertionError('Unexpected SQL in offline fixture')

    def test_explicit_drama_ignores_old_age_without_mutating_template(self):
        self.config['hit_drama_publish'] = self.hit
        before = deepcopy(self.config)
        self.assertEqual(len(self.repo.candidate_snapshot(self.config).candidates), 1)
        self.assertEqual(self.config, before)
        self.assertEqual(self.config['drama_launch_window_days'], 7)
        for sql, params in self.queries:
            if 'MAX(d0.id)' in sql:
                self.assertEqual(params[1], 0)
            elif 's.data_source_id IN' in sql:
                self.assertEqual(params[5], 0)

    def test_ordinary_and_material_only_runs_keep_seven_days(self):
        for extras in ({}, {'hit_material_publish': {'material_id': '101'}}):
            with self.subTest(extras=extras):
                self.assertFalse(self.repo.candidate_snapshot({**self.config, **extras}).candidates)

    def test_duration_and_spend_filters_still_apply(self):
        self.config['hit_drama_publish'] = self.hit
        self.duration = 601
        self.assertFalse(self.repo.candidate_snapshot(self.config).candidates)
        self.duration = 60
        self.config['drama_rule']['spend_min'] = 1
        self.assertFalse(self.repo.candidate_snapshot(self.config).candidates)

    def test_positive_release_time_and_future_schedule_policy_are_preserved(self):
        from features.hit_publish import POLICY
        self.config['hit_drama_publish'] = self.hit
        self.deploy = 0
        self.assertFalse(self.repo.candidate_snapshot(self.config).candidates)
        self.deploy = int((self.now + timedelta(days=2)).timestamp())
        self.assertFalse(self.repo.candidate_snapshot(self.config).candidates)
        self.hit.update(timing_policy=POLICY, release_times={'C1:en': self.deploy})
        self.assertEqual(len(self.repo.candidate_snapshot(self.config).candidates), 1)


class XTests(unittest.TestCase):
    def setUp(self):
        from scripts import test_x_auto_post_selector as fixture
        from features.x_tag_publish.selection import ProfileSelector
        from features.x_auto_posts.repositories import MetricWindowRepository
        self.fixture = fixture
        self.source = fixture.FakeSource(
            [fixture.drama('C1', deploy_time=int(fixture.NOW.timestamp()) - 34 * 86400)],
            {'C1': [fixture.material('101', 'C1')]})
        self.store = fixture.FakeStore()
        self.store.get_run = lambda _: SimpleNamespace(blacklist_snapshot=self.frozen)
        self.history = fixture.FakePublishedHistory()
        self.frozen = {'hit_drama_publish': dict(account_ids=['101'],
            target=dict(content_id='C1', content_ids=['C1'], by_language={'en': ['C1']}))}
        self.request = fixture.request(fixture.rules(drama={'launch_window_days': 7}))
        self.selector = ProfileSelector(self.source, MetricWindowRepository(fixture.FakeMetricStore()),
            self.history, self.store, material_validator=fixture.FakeValidator())
        self.patches = [
            patch.dict('os.environ', X_POST_AUTOMATION_INTERNAL_URL='http://fixture', X_POST_AUTO_INTERNAL_TOKEN='fixture'),
            patch('features.x_tag_publish.selection.XPostAutoBridgeClient'),
            patch('features.x_tag_publish.selection.Decision', return_value=SimpleNamespace(
                reason='', weights={'male': 1}, node_weights={('male',): {'revenge': 1}})),
            patch('features.x_tag_publish.policy.catalog_labels', return_value={
                ('C1', 'en'): {'audience': 'male', 'themes': ['revenge']}}),
            patch('features.x_tag_publish.selection.ranked_dramas', side_effect=lambda _, rows, __: rows),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_explicit_drama_removes_source_and_hard_gate_age_only(self):
        before = self.request.rules.as_dict()
        result = self.selector.preview(self.request)
        self.assertEqual(result.material.source.material_id, '101')
        self.assertIsNone(self.source.drama_call['deploy_since_epoch'])
        self.assertEqual(self.request.rules.as_dict(), before)
        self.assertEqual(self.request.rules.drama.launch_window_days, 7)
        self.assertFalse(self.store.reserve_calls)

    def test_ordinary_and_material_only_runs_keep_seven_days(self):
        from features.x_auto_posts.selector import NoEligibleMaterial
        for frozen in ({}, {'hit_material_publish': self.frozen['hit_drama_publish']}):
            with self.subTest(marker=list(frozen)):
                self.frozen = frozen
                with self.assertRaises(NoEligibleMaterial):
                    self.selector.preview(self.request)
                self.assertEqual(self.source.drama_call['deploy_since_epoch'],
                                 int(self.request.now.timestamp()) - 7 * 86400)

    def test_duration_history_and_metrics_still_apply(self):
        from features.x_auto_posts.selector import NoEligibleMaterial
        duration = replace(self.request.rules.material,
                           duration_seconds=self.fixture.rules(material={'duration_seconds': {'max': 10}}).material.duration_seconds)
        with self.assertRaises(NoEligibleMaterial):
            self.selector.preview(replace(self.request, rules=replace(self.request.rules, material=duration)))
        self.history.seen.add('101')
        with self.assertRaises(NoEligibleMaterial):
            self.selector.preview(self.request)
        self.history.seen.clear()
        metric_rule = self.fixture.rules(drama={'launch_window_days': 7, 'spend': {'min': 1}})
        with self.assertRaises(NoEligibleMaterial):
            self.selector.preview(replace(self.request, rules=metric_rule))

    def test_positive_release_time_and_profile_matching_still_apply(self):
        from features.x_auto_posts.selector import NoEligibleMaterial
        old = self.source.dramas[0]
        self.source.dramas = [replace(old, deploy_time=0)]
        with self.assertRaises(NoEligibleMaterial):
            self.selector.preview(self.request)
        self.source.dramas = [old]
        with patch('features.x_tag_publish.policy.catalog_labels', return_value={
                ('C1', 'en'): {'audience': 'female', 'themes': ['revenge']}}):
            with self.assertRaises(NoEligibleMaterial):
                self.selector.preview(self.request)

    def test_account_drama_cooldown_still_applies(self):
        from features.x_auto_posts.selector import NoEligibleMaterial
        self.store.cooldown_content_ids = lambda **_: {'C1'}
        rules = replace(self.request.rules,
                        drama=replace(self.request.rules.drama, cooldown_days=1))
        with self.assertRaises(NoEligibleMaterial):
            self.selector.preview(replace(self.request, rules=rules))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=['fb', 'x'], required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FBTests if args.platform == 'fb' else XTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
