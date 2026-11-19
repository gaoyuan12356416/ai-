"""Growth recommendations change the ordinary selector and its preview receipts."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_growth_profile.selection import GrowthDecision


def profile(theme, *, source='language_peers', catalog='dramawave'):
    group = dict(language='en', source=source, stage='24h', recommendations=[
        dict(theme=theme, weight=1.0, score=62, posts=7, accounts=3, dramas=2)])
    return dict(version='fixture', themes=[], mature_posts=0, cold_start=dict(by_catalog={catalog: group}))


def decide(portrait):
    return GrowthDecision(dict(id=101, drama_language='en'), ['auto', 123], dict(mode='growth', profile=portrait))


def labels(theme, catalog='dramawave'):
    return dict(catalog=catalog, themes=[theme], audience='female')


class GrowthColdSelectionTests(unittest.TestCase):
    def test_service_preview_changes_actual_drama_and_material_when_evidence_changes(self):
        from features.x_growth_profile import store
        from scripts import test_x_auto_post_selector as fixtures
        from scripts.test_x_publish_profile_choice import PublishProfileTests

        case = PublishProfileTests()
        self.addCleanup(case.doCleanups)
        case.setUp()
        case.svc.selector.source.dramas = [fixtures.drama(key) for key in ('C8', 'C9')]
        case.svc.selector.source.materials = {key: [fixtures.material(mid, key)] for key, mid in
                                               [('C8', '801'), ('C9', '901')]}
        template_id = case.template(profile_model='growth')
        catalog = {('C8', 'en'): labels('fantasy'), ('C9', 'en'): labels('revenge')}
        with patch('features.x_tag_publish.policy.catalog_labels', return_value=catalog):
            for theme, content_id, material_id in [('revenge', 'C9', '901'), ('fantasy', 'C8', '801')]:
                with store.connect(write=True) as db:
                    db.execute('UPDATE growth_profile SET data_json=? WHERE account_id=101', (json.dumps(profile(theme)),))
                for repeat in range(2):
                    preview = case.svc.preview(template_id, dict(expected_version=1, account_id='101'))
                    item = preview['items'][0]
                    self.assertTrue(item['ok'], item)
                    selection = item['selection']
                    self.assertEqual((selection['drama']['content_id'], selection['material']['material_id']),
                                     (content_id, material_id))
                    receipt = selection['reservation']['tag_profile']
                    self.assertEqual((receipt['growth_role'], receipt['growth_source'], receipt['selected_tag_key']),
                                     ('cold_start', 'language_peers', theme))
                    self.assertEqual((receipt['growth_evidence_posts'], receipt['growth_mature_posts']), (7, 0))
                    self.assertFalse(preview['reserved'])
        self.assertEqual(case.db.list_runs(), [])

    def test_reserves_exploration_and_balances_underused_themes(self):
        recent = []
        candidates = [labels(theme) for theme in ('revenge', 'fantasy', 'marital')]
        with patch('features.x_growth_profile.selection.recent_choices', return_value=recent):
            for _ in range(10):
                decision = decide(profile('revenge'))
                decision.prepare(candidates)
                chosen = min(candidates, key=decision.rank)
                recent.append(decision.audit(chosen))
        self.assertEqual(sum(row['growth_role'] == 'cold_start' for row in recent), 6)
        self.assertEqual(sorted(row['selected_tag_key'] for row in recent if row['growth_role'] == 'explore'),
                         ['fantasy', 'fantasy', 'marital', 'marital'])

    def test_same_theme_other_catalog_cannot_claim_recommendation_or_strict_match(self):
        from features.x_tag_publish.hit_drama import match_reason

        decision = decide(profile('revenge', source='catalog_peers'))
        wrong_product, right_product = labels('revenge', 'moboreels'), labels('revenge')
        self.assertTrue(match_reason(decision, right_product))
        with patch('features.x_growth_profile.selection.recent_choices', return_value=[]):
            decision.prepare([wrong_product, right_product])
        self.assertLess(decision.rank(right_product), decision.rank(wrong_product))
        self.assertEqual(decision.audit(right_product)['growth_source'], 'catalog_peers')
        other_receipt = decision.audit(wrong_product)
        self.assertEqual(other_receipt['growth_role'], 'explore')
        self.assertNotIn('growth_source', other_receipt)
        self.assertTrue(match_reason(decision, right_product))

    def test_evidence_used_only_when_target_language_and_material_tags_match(self):
        data = profile('revenge')
        candidates = [labels('revenge')]
        with patch('features.x_growth_profile.selection.recent_choices', return_value=[]):
            for change, available in [('en', [labels('fantasy')]), ('ja', candidates)]:
                data['cold_start']['by_catalog']['dramawave']['language'] = change
                decision = decide(data)
                decision.prepare(available)
                self.assertNotIn('growth_source', decision.audit(available[0]))
                self.assertEqual(decision.audit(available[0])['growth_role'], 'explore')

    def test_own_mature_choice_wins_and_other_products_keep_their_own_evidence(self):
        data = profile('revenge', catalog='moboreels')
        data.update(policy=dict(weights={'fantasy': .8}), themes=[dict(catalog='dramawave', language='en',
                    audience='female', theme='fantasy', mature=True, score=70, effective_posts=7)])
        decision = decide(data)
        with patch('features.x_growth_profile.selection.recent_choices', return_value=[]):
            dw = labels('fantasy')
            mobo = labels('revenge', 'moboreels')
            decision.prepare([labels('revenge'), dw, mobo])
            self.assertEqual(decision.audit(dw)['growth_role'], 'exploit')
            decision.prepare([mobo])
            self.assertEqual(decision.audit(mobo)['growth_source'], 'language_peers')


if __name__ == '__main__':
    unittest.main()
