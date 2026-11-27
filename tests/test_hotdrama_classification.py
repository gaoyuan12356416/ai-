import copy
import unittest
from unittest.mock import patch

from features.fb_page_portrait.classification import classifications
from features.fb_page_portrait.reclassify import reclassify, facts
from features.fb_page_portrait.same_drama import verified_hotdrama_links
from features.fb_page_assignment_v2.snapshot import extend_hotdrama_catalog
from features.x_tag_revenue.hierarchy import build_tree


def info(app, **kw):
    return dict(app_id=app, content_id='A', language='ro', series_code='S', resource_type='1', **kw)


class HotDramaTests(unittest.TestCase):
    def setUp(self):
        self.infos = [info(1479), info(3302)]
        self.resources = [dict(app_id=1479, content_id='A', language='ro', series_code='S', labels=['Romance'])]
        self.audience = {'A': dict(languages=['ro'], audience='male', status='matched',
                                  audience_labels=['男性'], fetched_at='2026-11-26T00:00:00+00:00')}
        self.patch = patch('features.x_tag_revenue.taxonomy.audience_records', return_value=self.audience)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_verified_identity_borrows_classification_and_preserves_product(self):
        got = classifications(self.resources, self.infos)
        hot = got[3302, 'A', 'ro']
        self.assertEqual((hot['app_id'], hot['audience'], hot['themes']), (3302, 'male', ['sweet_romance']))
        self.assertEqual(hot['classification_mapping']['series_code'], 'S')
        self.assertEqual(got[1479, 'A', 'ro']['audience'], 'male')

    def test_missing_conflicting_different_case_language_series_never_borrow(self):
        for changes in ({'series_code': 'X'}, {'series_code': ''}, {'content_id': 'a'}, {'language': 'en'}):
            rows = [self.infos[0], {**self.infos[1], **changes}]
            self.assertFalse(verified_hotdrama_links(rows))
        rows = self.infos + [{**self.infos[0], 'series_code': 'other'}]
        self.assertFalse(verified_hotdrama_links(rows))
        got = classifications(self.resources, [self.infos[1]])[3302, 'A', 'ro']
        self.assertEqual(got['audience'], '__unknown__')
        self.assertNotIn('classification_mapping', got)
        got = classifications(self.resources, [self.infos[0], info(979)])[979, 'A', 'ro']
        self.assertEqual(got['audience'], '__unknown__')

    def test_own_labels_win_and_conflicting_donor_resources_are_not_used(self):
        own = {**self.resources[0], 'app_id': 3302, 'labels': ['Revenge']}
        got = classifications(self.resources + [own], self.infos)[3302, 'A', 'ro']
        self.assertEqual(got['themes'], ['revenge'])
        self.assertFalse(got['classification_mapping']['labels'])
        got = classifications([{**self.resources[0], 'series_code': 'other'}], self.infos)[3302, 'A', 'ro']
        self.assertEqual(got['audience'], 'male')
        self.assertEqual(got['themes'], ['__unknown__'])

    def test_v2_catalog_uses_the_same_verified_mapping(self):
        directions = {'1479': {'A': ['male', ['ro', 'en']]}}
        warm = {}
        resources = {('1479', 'A'): self.resources}
        extend_hotdrama_catalog(resources, self.infos, directions, warm, {'romance': 'romance'}, {'romance': 'sweet_romance'})
        self.assertEqual(directions['3302'], {'A': ['male', ['ro']]})
        self.assertEqual(warm['3302'], {'A': {'ro': ['sweet_romance']}})
        own = {**self.resources[0], 'app_id': 3302, 'labels': ['Unmapped own label']}
        resources[('3302', 'A')] = [own]
        warm = {'3302': {'A': {'ro': []}}}
        extend_hotdrama_catalog(resources, self.infos, directions, warm, {'romance': 'romance'}, {'romance': 'sweet_romance'})
        self.assertEqual(warm['3302']['A']['ro'], [])
        resources[('3302', 'A')] = [{**own, 'series_code':'', 'labels':[]}]
        extend_hotdrama_catalog(resources, self.infos, directions, warm, {'romance': 'romance'}, {'romance': 'sweet_romance'})
        self.assertEqual(warm['3302']['A']['ro'], ['sweet_romance'])

    def test_rebuild_conserves_product_facts_money_and_publication_counts(self):
        old = classifications([], [self.infos[1]])[3302, 'A', 'ro']
        new = classifications(self.resources, self.infos)
        dramas = {(app, 'A', 'ro'): dict(app_id=app, content_id='A', language='ro', name='name',
                  revenue_cents=101, paid_cents=100, social_cents=1, spend_cents=80,
                  posts={((app, 'A', 'ro'), 0)}) for app in (1479, 3302)}
        classified = {k: copy.deepcopy(old) for k in dramas}
        classified[1479, 'A', 'ro']['themes'] = ['marital', 'sweet_romance']
        tree = build_tree(dramas, classified)
        page = dict(page_id='123', revenue_cents=202, paid_cents=200, social_cents=2, spend_cents=160,
                    drama_count=2, status='ready', active_days=3, **tree)
        before = copy.deepcopy(page)
        changed = reclassify(page, new)
        self.assertEqual(facts(changed)[0], facts(before)[0])
        self.assertEqual(page, before)
        self.assertEqual(facts(changed)[1][1479, 'A', 'ro'], classified[1479, 'A', 'ro'])
        self.assertEqual(sum(a['revenue_cents'] for a in changed['audiences']), 202)
        self.assertTrue(any(a['key'] == 'male' for a in changed['audiences']))

    def test_signed_adjustments_keep_undefined_percentages(self):
        old = classifications([], [self.infos[1]])[3302, 'A', 'ro']
        dramas = {(3302, 'A', 'ro'): dict(app_id=3302,content_id='A',language='ro',name='A',
                  revenue_cents=-100,paid_cents=-100,social_cents=0,spend_cents=20,posts=set())}
        page = dict(page_id='123',revenue_cents=-100,paid_cents=-100,social_cents=0,spend_cents=20,
                    drama_count=1,status='signed_adjustment',active_days=1,
                    **build_tree(dramas,{(3302,'A','ro'):old}))
        result = reclassify(page,classifications(self.resources,self.infos))
        self.assertEqual(result['status'],'signed_adjustment')
        self.assertEqual(result['focus'],dict(audience={},themes=[]))
        self.assertIsNone(result['audiences'][0]['share_bp'])
        self.assertIsNone(result['combinations'][0]['channel_share_bp'])
        self.assertEqual(facts(result)[0],facts(page)[0])


if __name__ == '__main__':
    unittest.main()
