import csv
import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from features.social_analytics import warehouse_reader, warehouse_refresh
from features.social_analytics.backend_scope import restrict
from features.social_analytics.bill_income import allocate_tiktok_cents, by_campaign, merge_settled
from features.social_analytics.report import csv_export, empty, options
from features.social_analytics.adapters import CatalogErrors, PLATFORMS
from features.social_analytics.tiktok import campaign_key
from features.social_analytics.tiktok_scope import UNASSIGNED, keyed, stamp_site_tenant
from tests.test_social_analytics import catalog, metric


DAY = '2026-11-24'
PRIOR = '2026-11-23'
ADMIN = dict(role='admin', user_id='op', tenant_key='t')
SETTING = dict(SOCIAL_ANALYTICS_TT_SITE_TENANT='t')


def receipt(cid, name, owner, **values):
    return catalog('tiktok', campaign_key(cid, name), owner, date=DAY,
                   publications=[dict(id='tiktok:' + name, date=DAY, kind='video')], **values)


def effect(cid, name, revenue, iaa, day=DAY):
    return keyed(dict(metric('tiktok', cid, revenue, date=day, site_id='2250', iaa_cents=iaa), campaign=name))


class TikTokWarehouseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        rows = [receipt('5', 'verified', 'alice'),
                receipt('5', 'legacy', '', tenant='', drama='legacy-drama', link_type='tt_post')]
        self.catalog = stamp_site_tenant(rows, SETTING)
        facts = [effect('5', 'verified', 3500, 200), effect('5', 'legacy', 1426, 100),
                 effect('0001', 'TTpost', 778, 78), effect(None, '', 1749, 169),
                 effect('5', 'verified', 159, 0, PRIOR)]
        _, effects, coverage, _ = restrict(self.catalog, facts, DAY)
        meta = dict(version='v', database='snapshots/v.sqlite3', generated_at=datetime.now(timezone.utc).isoformat(),
                    start=PRIOR, end=DAY, coverage={k: sorted(days) for k, days in coverage.items()},
                    iaa_coverage={'tiktok:2250': [DAY]}, catalog_errors={}, source_metadata={})
        warehouse_refresh.snapshot(self.root, 'v', dict(catalog=self.catalog, metrics=effects, titles={}), meta)
        warehouse_refresh.atomic_json(self.root / 'current.json', meta)

    def report(self, actor=ADMIN, day=DAY, **filters):
        query = dict(start=[day], end=[day], platform=['tiktok'], group_by=['owner,platform'], **filters)
        return warehouse_reader.build_report({'SOCIAL_ANALYTICS_WAREHOUSE_ROOT': str(self.root)}, actor, query)

    def test_immutable_snapshot_reconciles_owners_group_and_csv(self):
        report = self.report()
        self.assertEqual([report['totals'][k] for k in ('revenue_cents', 'iap_cents', 'iaa_cents')], [7453, 6906, 547])
        group, = [row for row in report['rows'] if row['owner'] == UNASSIGNED]
        self.assertEqual((group['revenue_cents'], group['published_count'], group['links']), (3953, 1, 1))
        self.assertEqual(report['totals']['published_count'], 2)
        self.assertEqual(report['quality']['platforms']['tiktok']['unmatched_totals']['revenue_cents'], 3953)
        rows = list(csv.DictReader(io.StringIO(csv_export(report).decode('utf-8-sig'))))
        self.assertEqual([r['其中 TikTok 待归属收入（USD）'] for r in rows if r['owner ID'] == UNASSIGNED], ['39.53'])

    def test_filters_and_permissions_cannot_leak_unassigned_site_revenue(self):
        for actor, amount in ((dict(ADMIN, role='user', user_id='alice'), 3500),
                              (dict(ADMIN, tenant_key='another'), 0),
                              (dict(ADMIN, role='user', user_id=UNASSIGNED), 0)):
            report = self.report(actor)
            self.assertEqual(report['totals']['revenue_cents'], amount)
            self.assertEqual(report['quality']['unmatched_totals']['revenue_cents'], 0)
            self.assertNotIn(UNASSIGNED, csv_export(report).decode('utf-8-sig'))
        self.assertEqual(self.report(owner=['alice'])['totals']['revenue_cents'], 3500)
        self.assertEqual(self.report(drama=['legacy-drama'])['totals']['revenue_cents'], 1426)
        self.assertEqual(self.report(link_type=['tt_post'])['totals']['published_count'], 1)
        self.assertIn(UNASSIGNED, [r['value'] for r in options(self.catalog, ADMIN)['options']['owner']])
        self.assertEqual(stamp_site_tenant(self.catalog, dict(SOCIAL_ANALYTICS_TT_SITE_TENANT='other'))[0]['tiktok_site_tenant'], '')

    def test_earlier_effects_site_only_and_unsettled_iaa_is_pending(self):
        report = self.report(day=PRIOR)
        self.assertEqual(report['totals']['revenue_cents'], 159)
        self.assertIsNone(report['totals']['iaa_cents'])
        self.assertEqual(report['meta']['iaa_missing_dates'], {'tiktok:2250': [PRIOR]})
        self.assertEqual(self.report(day=PRIOR, owner=['alice'])['totals']['revenue_cents'], 0)
        self.assertEqual(self.report(day=PRIOR, drama=['42'])['totals']['revenue_cents'], 0)
        self.assertIsNone(self.report(day='2026-07-30')['totals']['revenue_cents'])

    def test_income_identity_and_rounding_survive_separate_bill_only_groups(self):
        sql = by_campaign(DAY, '2250')
        self.assertIn("BINARY COALESCE(campaign,'')", sql)
        self.assertIn("BINARY COALESCE(campaign_id,'')", warehouse_refresh.statements([], 'tiktok', '2250', DAY, DAY)[0])
        def bill(name):
            return dict(campaign_id='5', campaign=name, site_id='2250', date=DAY, platform='tiktok',
                        ad_events=1, purchase_events=1, iap_usd_1e4=149, iaa_usd_1e4=149)
        bills = allocate_tiktok_cents([bill('b'), bill('a')])
        self.assertEqual([(r['campaign'], r['iap_cents']) for r in bills], [('b', 1), ('a', 2)])
        facts, settled = merge_settled([], bills, ('campaign_id', 'campaign'), empty())
        self.assertTrue(settled)
        self.assertEqual(sum(r['revenue_cents'] for r in facts), 6)
        self.assertEqual(len({keyed(r)['campaign_id'] for r in facts}), 2)

    def test_hourly_ingest_preserves_prepublication_and_bill_only_days(self):
        spec = {'tiktok': PLATFORMS['tiktok']}
        historical = '2026-11-22'
        class Source:
            def query(self, statements):
                if any("'kind','metric'" in sql for sql in statements):
                    values = [dict(metric('tiktok', '5', 1), campaign=name, kind='metric', date=historical,
                                   site_id='2250', revenue_usd_1e4=149, refund_usd_1e4=0)
                              for name in ('verified', 'legacy')]
                    values.append(dict(kind='coverage', date=historical, site_id='2250'))
                    return values
                if any("'date','2026-11-24'" in sql and 'ads_drama_bills' in sql for sql in statements):
                    return [dict(campaign_id='5', campaign='verified', platform='tiktok', site_id='2250', date=DAY,
                                 purchase_events=1, ad_events=1, iap_usd_1e4=10000, iaa_usd_1e4=54715)]
                return []
        with patch.object(warehouse_refresh, 'PLATFORMS', spec), patch.object(
                warehouse_refresh, 'read_catalogs', return_value=(self.catalog, CatalogErrors())):
            data = warehouse_refresh.collect({}, Source(), historical, DAY)
        self.assertEqual(data['coverage']['tiktok:2250'], [historical, DAY])
        self.assertEqual(data['iaa_coverage']['tiktok:2250'], [DAY])
        history = [r for r in data['metrics'] if r['date'] == historical]
        self.assertEqual(sum(r['revenue_cents'] for r in history), 3)
        self.assertTrue(all(r['tiktok_unassigned'] and not r['tiktok_drama'] for r in history))
        current, = [r for r in data['metrics'] if r['date'] == DAY]
        self.assertEqual((current['revenue_cents'], current['iaa_cents']), (647, 547))
