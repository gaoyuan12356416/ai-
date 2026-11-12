"""Exercise Page profile choice through the real FB repository and planner."""
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from features.fb_auto_posts.core import ActorScope, FBAutoPostStore
from features.fb_auto_posts.profile_material import choose_profile_material
from features.fb_auto_posts.profile_selection import Decision, HIERARCHY_VERSION, TAXONOMY_VERSION
from features.fb_auto_posts.repositories import CandidateSnapshot, MaterialCandidate, MaterialRepository, PageTarget
from scripts.test_fb_auto_validation import payload


NOW = datetime(2026, 11, 12, 4, tzinfo=timezone.utc)


def material(mid, cid, audience='female', theme='sweet_romance'):
    return MaterialCandidate(str(mid), str(cid), f'https://media.test/{mid}.mp4', 'edit',
        'Drama', 'en', Decimal(60), Decimal(0), None, Decimal(0), None, '1')


def classification(audience, theme):
    return {'audience': audience, 'themes': [theme], 'raw_labels': []}


def profiles(preferences):
    pages = {}
    for page, (audience, theme) in preferences.items():
        leaf = {'key': theme, 'revenue_cents': 100, 'share_bp': 10000, 'matchable': True}
        pages[page] = {'page_id': page, 'status': 'ready', 'revenue_cents': 100,
            'hierarchy_version': HIERARCHY_VERSION,
            'audiences': [{'key': audience, 'revenue_cents': 100, 'share_bp': 10000,
                           'matchable': True, 'children': [leaf]}]}
    return {'pages': pages}, {'version': 'fixture-v1', 'generated_at': NOW.isoformat(),
        'taxonomy_version': TAXONOMY_VERSION, 'hierarchy_version': HIERARCHY_VERSION,
        'start': '2026-11-05', 'end': '2026-11-11'}


class MaterialChoiceTests(unittest.TestCase):
    def setUp(self):
        self.rows = (material(1, 'old'), material(2, 'preferred'), material(3, 'preferred'), material(4, 'later'))
        self.labels = {('old', 'en'): classification('male', 'revenge'),
            ('preferred', 'en'): classification('female', 'sweet_romance'),
            ('later', 'en'): classification('female', 'family')}
        self.snapshot = CandidateSnapshot(self.rows, (), (), self.labels)
        self.decision = Decision('11', ['slot'], profiles({'11': ('female', 'sweet_romance')}))
        self.config = {'app_id': '1479'}
        # Deliberately pick the last permitted material: a broad randomization
        # after mere sorting would select the wrong drama in this fixture.
        self.materials = SimpleNamespace(choose_from=lambda rows, excluded: rows[-1] if rows else None)

    def choose(self, **changes):
        args = dict(decision=self.decision, snapshot=self.snapshot, candidate_limit=500,
            materials=self.materials, config=self.config, page_id='11', slot_key='slot', scores={})
        args.update(changes)
        return choose_profile_material(self.rows, (), **args)

    def test_material_randomization_stays_inside_selected_drama(self):
        selected, audit = self.choose()
        self.assertEqual((selected.content_id, selected.material_id), ('preferred', '3'))
        self.assertEqual(audit['tag_profile']['selected_path'], ['female', 'sweet_romance'])
        self.assertFalse(audit['tag_profile']['fallback'])

    def test_feedback_and_exploration_cannot_override_profile_drama(self):
        scores = {('en', 'material_id', '1'): {'clicks_per_1000_smoothed': 999,
            'd3_revenue_per_post': 999, 'posts': 10, 'pages': 5}}
        for exploration in (0, 100):
            config = dict(self.config, feedback_selection={'enabled': True, 'rollout_percent': 100,
                                                          'exploration_percent': exploration})
            chosen, _ = self.choose(config=config, scores=scores)
            self.assertEqual(chosen.content_id, 'preferred')

    def test_occupied_preferred_materials_fall_back_within_audience(self):
        selected, audit = choose_profile_material(self.rows, ('2', '3'), decision=self.decision,
            snapshot=self.snapshot, candidate_limit=500, materials=self.materials,
            config=self.config, page_id='11', slot_key='slot', scores={})
        self.assertEqual(selected.content_id, 'later')
        self.assertEqual(audit['tag_profile']['fallback_level'], 'theme')

    def test_catalog_failure_restores_original_bounded_candidate_order(self):
        snapshot = CandidateSnapshot(self.rows, (), (), self.labels, 'candidate_metadata_unavailable')
        selected, audit = self.choose(snapshot=snapshot, candidate_limit=1)
        self.assertEqual(selected.content_id, 'old')
        self.assertEqual(audit['tag_profile']['fallback_reason'], 'candidate_metadata_unavailable')

    def test_unsupported_product_keeps_original_material_selection(self):
        selected, audit = self.choose(config={'app_id': 'other'})
        self.assertEqual(selected.content_id, 'later')
        self.assertEqual(audit['tag_profile']['fallback_reason'], 'unsupported_profile_product')

    def test_portuguese_source_language_matches_portrait_taxonomy_key(self):
        rows = tuple(replace(row, language='pt') for row in self.rows)
        labels = {(content_id, 'pt-br'): value for (content_id, _), value in self.labels.items()}
        snapshot = CandidateSnapshot(rows, (), (), labels)
        selected, audit = choose_profile_material(rows, (), decision=self.decision, snapshot=snapshot,
            candidate_limit=500, materials=self.materials, config=self.config, page_id='11', slot_key='slot', scores={})
        self.assertEqual(selected.content_id, 'preferred')
        self.assertFalse(audit['tag_profile']['fallback'])
        self.assertEqual(selected.language, 'pt')


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FBAutoPostStore(Path(self.temp.name) / 'fb.sqlite3', now_fn=lambda: NOW)
        self.actor = ActorScope('operator', 'Operator', True, '')
        self.raw = payload()
        self.template = self.store.create_template(self.raw, self.actor,
            dict(app_id='1479', product='Dramawave', metric_product='Dramawave', metric_platform=0))
        self.page_rows = [PageTarget('6', ('6',), page, '1', 'UTC', 'en', 1) for page in ('11', '12')]
        self.pages = SimpleNamespace(list_pages=lambda *a, **k: self.page_rows, legacy_conflicts=lambda *_: [])
        self.rows = (material(1, 'female'), material(2, 'male'), material(3, 'female'))
        self.labels = {('female', 'en'): classification('female', 'sweet_romance'),
                       ('male', 'en'): classification('male', 'revenge')}
        self.configs = []
        def snapshot(config):
            self.configs.append(config)
            return CandidateSnapshot(self.rows, (), (), self.labels)
        self.materials = SimpleNamespace(candidate_snapshot=snapshot,
            choose_from=lambda rows, excluded: rows[0] if rows else None)
        self.profile = profiles({'11': ('female', 'sweet_romance'), '12': ('male', 'revenge')})

    def run_slot(self, slot):
        return self.store.create_run(self.template['id'], slot, 'manual', self.actor, self.pages, self.materials)

    def tasks(self):
        with self.store.connect() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM fb_auto_task ORDER BY id')]

    def test_page_specific_selection_is_frozen_and_replay_never_reselects(self):
        with patch('features.fb_auto_posts.core.load_profiles', return_value=self.profile):
            first = self.run_slot('manual-one')
        tasks = self.tasks()
        self.assertEqual([(row['page_id'], row['content_id']) for row in tasks], [('11', 'female'), ('12', 'male')])
        self.assertTrue(all(config['_profile_selection'] for config in self.configs))
        for row in tasks:
            audit = json.loads(row['selection_json'])['tag_profile']
            self.assertEqual(audit['page_id'], row['page_id'])
            self.assertEqual(audit['selected_content_id'], row['content_id'])
            self.assertEqual(audit['profile_version'], 'fixture-v1')
        with patch('features.fb_auto_posts.core.load_profiles', side_effect=AssertionError('no redraw')):
            second = self.run_slot('manual-one')
        self.assertTrue(second['idempotent'])
        self.assertEqual(second['run_id'], first['run_id'])
        self.assertEqual(self.tasks(), tasks)

    def test_next_slot_respects_existing_material_reservation(self):
        self.page_rows = self.page_rows[:1]
        with patch('features.fb_auto_posts.core.load_profiles', return_value=self.profile):
            self.run_slot('manual-one')
            self.run_slot('manual-two')
        self.assertEqual([row['material_id'] for row in self.tasks()], ['1', '3'])

    def test_no_page_income_does_not_expand_catalog(self):
        with patch('features.fb_auto_posts.core.load_profiles', return_value=({'pages': {}}, {})):
            self.run_slot('manual-one')
        self.assertFalse(any(config['_profile_selection'] for config in self.configs))
        self.assertEqual([row['material_id'] for row in self.tasks()], ['1', '1'])
        self.assertEqual(json.loads(self.tasks()[0]['selection_json'])['tag_profile']['fallback_reason'], 'no_revenue_history')

    def enable_drama_cooldown(self):
        raw = dict(self.raw, drama_cooldown_hours=24)
        self.template = self.store.create_template(raw, self.actor,
            dict(app_id='1479', product='Dramawave', metric_product='Dramawave', metric_platform=0))

    def test_next_slot_keeps_drama_cooldown_and_records_audience_fallback(self):
        self.enable_drama_cooldown()
        self.page_rows = self.page_rows[:1]
        with patch('features.fb_auto_posts.core.load_profiles', return_value=self.profile):
            self.run_slot('manual-one')
            self.run_slot('manual-two')
        tasks = self.tasks()
        self.assertEqual([row['content_id'] for row in tasks], ['female', 'male'])
        audit = json.loads(tasks[1]['selection_json'])['tag_profile']
        self.assertEqual(audit['fallback_level'], 'audience')

    def test_unprofiled_page_recreates_global_bound_before_drama_cooldown(self):
        self.enable_drama_cooldown()
        self.rows = tuple(material(i, f'old-{i}') for i in range(1, 1001)) + (material(1001, 'preferred'),)
        self.labels = {(row.content_id, 'en'): classification('male', 'revenge') for row in self.rows[:-1]}
        self.labels[('preferred', 'en')] = classification('female', 'sweet_romance')
        profile = profiles({'11': ('female', 'sweet_romance')})
        with patch('features.fb_auto_posts.core.load_profiles', return_value=profile), patch(
                'features.fb_auto_posts.core.cooldown_content_ids', return_value={row.content_id for row in self.rows[:-1]}):
            self.run_slot('manual-one')
        tasks = self.tasks()
        self.assertEqual(tasks[0]['content_id'], 'preferred')
        self.assertEqual(tasks[1]['status'], 'skipped')
        self.assertEqual(tasks[1]['skip_reason'], 'fb_auto_no_video_after_drama_cooldown')


class CatalogCoverageTests(unittest.TestCase):
    def test_late_preferred_drama_survives_original_top_n(self):
        rows = [material(i, str(i)) for i in range(1, 7)]
        catalog = {(row.content_id, 'en'): classification('female' if i == 5 else 'male',
            'sweet_romance' if i == 5 else 'revenge') for i, row in enumerate(rows)}
        queries = []
        def select(sql, params):
            queries.append(sql)
            if 'DISTINCT TRIM(d.content_id) content_id' in sql:
                return [{'content_id': row.content_id} for row in rows]
            if 's.data_source_id IN' in sql:
                row = rows[int(params[1]) - 1]
                return [dict(material_id=row.material_id, content_id=row.content_id,
                    media_url=row.media_url, language='en', video_duration=60)]
            if 'MAX(d0.id)' in sql:
                return [dict(content_id=str(params[-1]), language='en', resource_type_v2='1')]
            raise AssertionError(sql)
        mysql = SimpleNamespace(schema='ads', blacklist_schema='ads', select=select)
        metrics = SimpleNamespace(load_metric_window=lambda **_: SimpleNamespace(
            by_drama={}, by_material={}, generation_ids=(), dates=()))
        repo = MaterialRepository(mysql, metrics, now_fn=lambda: NOW,
                                  candidate_limit=2, metric_prefilter_batch_size=1)
        from features.fb_auto_posts.validation import normalize_template_payload
        config = normalize_template_payload(payload())
        config.update(app_id='1479', product='Dramawave', metric_product='Dramawave',
                      metric_platform=0, language='en', _profile_selection=True, _candidate_limit=2)
        with patch('features.fb_auto_posts.profile_selection.load_classifications',
                   side_effect=lambda _, ids: {key: value for key, value in catalog.items() if key[0] in ids}):
            snapshot = repo.candidate_snapshot(config)
        self.assertEqual([row.material_id for row in snapshot.candidates], ['1', '2', '6'])
        self.assertEqual(sum('s.data_source_id IN' in query for query in queries), 6)
        decision = Decision('11', ['slot'], profiles({'11': ('female', 'sweet_romance')}))
        selected, _ = choose_profile_material(snapshot.candidates, (), decision=decision,
            snapshot=snapshot, candidate_limit=2, materials=repo, config=config,
            page_id='11', slot_key='slot', scores={})
        self.assertEqual(selected.material_id, '6')


if __name__ == '__main__':
    unittest.main()
