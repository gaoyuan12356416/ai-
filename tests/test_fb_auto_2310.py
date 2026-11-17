"""Landing migration: preserve attribution, ownership and confirmed post counts."""
import copy
import html
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from features.fb_auto_posts.links import build_w2a_url, validate_w2a_url, write_short_redirect
from features.fb_auto_posts.effect_collector import GatedReader
from features.social_analytics import facebook
from features.social_analytics.backend_scope import restrict
from features.social_analytics.report import campaign_index, effect_sites, parameters, resolve, summarize
from features.social_analytics.routes import scoped_facebook_source
from features.social_daily_report import billing
from tests.test_social_backend_scope import DAY, link, metric
from tests.test_social_daily_billing import bill
from tests import test_social_facebook_attribution as attribution_fixture
from tests.test_social_facebook_attribution import long_url

OLD = 'https://www.dramawavew2a.com/ads/0/2049/view'
NEW = 'https://www.dramawavew2a.com/ads/101/2310/view'


def legacy():
    value = link(date=DAY)
    value['link_type'] = 'auto_template'
    return value


class LandingMigrationTests(unittest.TestCase):
    def test_new_task_uses_2310_with_original_eight_parameters(self):
        url = build_w2a_url(dict(username='10001', timestamp=1787191200,
            material_language='en', drama_name='A Drama', tag='hook', task_id=7,
            page_name='Page', page_id='10001', material_name='素材 & opening',
            material_id='501', content_id='AcWE9aQz8q'))
        self.assertTrue(url.startswith(NEW + '?'))
        old = url.replace(NEW, OLD, 1)
        self.assertEqual(validate_w2a_url(old), old)
        self.assertEqual(urlsplit(url).query, urlsplit(old).query)

    def test_existing_source_is_immutable_and_every_http_target_moves(self):
        original = long_url('2049', '7', af_ad='素材 & opening_contentid[dramA]')
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_short_redirect(Path(tmp) / 'fb', 7, original)
            before = dest.read_bytes()
            self.assertEqual(write_short_redirect(dest.parent, 7, original), dest)
            self.assertEqual(dest.read_bytes(), before)
            served = before.replace((OLD + '?').encode(), (NEW + '?').encode()).decode()
        targets = [html.unescape(x) for x in re.findall(r'(?:href|content)="([^\"]+)"', served)
                   if NEW in x]
        js = json.loads(re.search(r'location.replace\((.*?)\);', served).group(1))
        targets.append(js)
        self.assertEqual(len(targets), 4)
        for target in targets:
            target = target.removeprefix('0;url=')
            self.assertEqual(target, original.replace(OLD, NEW, 1))
        self.assertNotIn(OLD, served)

    def test_new_legacy_source_url_keeps_template_owner_and_source_identity(self):
        fixture = attribution_fixture.FacebookAttributionTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.change("UPDATE fb_auto_task SET long_url=?", (long_url('2049', '1').replace(OLD, NEW, 1),))
        result = fixture.run_collect(mode='full')
        self.assertEqual(result['status'], 'complete')
        catalog, errors, _ = fixture.read()
        self.assertFalse(errors)
        row = next(row for row in catalog if row['site_id'] == '2049')
        self.assertEqual(row['owner'], 'creator')
        self.assertEqual(row['link_type'], 'auto_template')
        self.assertEqual(effect_sites(row), ('2049', '2310'))

    def test_two_landing_sites_share_one_owner_and_one_publication(self):
        catalog = [legacy()]
        metrics = [metric(cents=100), metric(cents=200, site_id='2310')]
        c, kept, coverage, _ = restrict(catalog, metrics, DAY)
        report = summarize(c, kept, {'_system_report': True}, parameters({
            'start': [DAY], 'end': [DAY], 'platform': ['facebook'], 'group_by': ['owner,platform']}),
            'test', metric_coverage=coverage)
        self.assertEqual(report['totals']['revenue_cents'], 300)
        self.assertEqual(report['totals']['published_count'], 1)
        self.assertEqual(report['totals']['links'], 1)
        self.assertEqual(report['rows'][0]['owner'], 'owner')

    def test_2310_reused_id_requires_original_page_and_material(self):
        _, kept, _, audit = restrict([legacy()], [metric(site_id='2310', adset_id='other-page')], DAY)
        self.assertFalse(kept)
        self.assertEqual(audit['outside'][0]['reason'], 'outside_backend_frozen_source_ids')

    def test_cross_site_owner_conflict_remains_unresolved(self):
        first = legacy()
        second = copy.deepcopy(first)
        second.update(site_id='2310', owner='different')
        self.assertIsNone(resolve(campaign_index([first, second])[('facebook', '2310', '6')]))

    def test_2310_indexed_query_includes_legacy_frozen_triple(self):
        with patch('features.social_analytics.backend_scope.facebook_source', return_value=([], 'now')) as read:
            scoped_facebook_source({}, DAY, DAY, '2310', [legacy()])
        self.assertEqual(read.call_args.kwargs['allowed_sources'], [('6', 'page', 'material')])

    def test_billing_matches_2310_without_reassigning_user_or_double_counting(self):
        row = legacy()
        row['publications'][0]['date'] = '2026-10-25'
        value = billing.aggregate([row], [bill(1, 'facebook', cid='6', site_id='2310', cents=250)], '2026-10-25')
        self.assertEqual(value['total'].revenue_cents, 250)
        self.assertEqual(value['owners'][('tenant', 'owner')].orders, 1)
        wrong = billing.aggregate([row], [bill(1, 'facebook', cid='6', site_id='2310', ad_id='other')], '2026-10-25')
        self.assertEqual(wrong['total'].revenue_cents, 0)

    def test_feedback_reads_both_sites_but_checks_frozen_material(self):
        reader = GatedReader.__new__(GatedReader)
        reader.query = Mock(return_value=[dict(dt='2026-10-19', campaign_id='6', campaign='', material_id='55', amount=1.25),
                                         dict(dt='2026-10-19', campaign_id='6', campaign='', material_id='99', amount=88)])
        result = reader.revenue([dict(id=6, page_id='999', material_id='55', created_at_utc='2026-10-19T01:00:00+00:00')],
                                datetime(2026, 10, 26, tzinfo=timezone.utc))
        self.assertEqual(result, {6: 1.25})
        self.assertIn("site_id IN ('2049','2310')", reader.query.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
