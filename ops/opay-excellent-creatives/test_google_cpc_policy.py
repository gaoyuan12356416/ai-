import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import google_creatives as google
import opay_excellent_creatives as report
from test_google_creatives import CONFIG, source, mapping, insert_dimension


class GoogleCpcPolicyTests(unittest.TestCase):
    def build(self, materials, campaign_spend=10000, campaign_clicks=1000,
              campaign_impressions=100000, extra=(), missing_campaign_fx=False, incomplete_material=None):
        with tempfile.TemporaryDirectory() as temp, contextlib.closing(
                report.cache_conn(Path(temp) / 'cache.sqlite3')) as connection:
            insert_dimension(connection)
            dim = dict(connection.execute('SELECT * FROM material_dim').fetchone())
            dims, sources, links = {}, [], []
            for ident, spend, impressions, clicks in materials:
                if ident != 9:
                    changed = dict(dim, custom_source_id=ident)
                    connection.execute('INSERT INTO material_dim VALUES(%s)' % ','.join('?' for _ in changed), list(changed.values()))
                resource = 'customers/1234567890/assets/%s' % ident
                dims[ident] = {'product': 'Opay', 'material_type': 2}
                sources.append(source(resource_id=resource, cost=str(spend * 1000000), impressions=str(impressions), clicks=str(clicks)))
                links.append(mapping(asset_name=resource, resource_id=str(ident), source_custom_id=str(ident)))
            sources.extend(extra)
            sources.append(source(resource_id='campaign', type='0', asset_type='0',
                                  cost=str(campaign_spend * 1000000), clicks=str(campaign_clicks), impressions=str(campaign_impressions)))
            mappings = google.collapse_mappings([s['resource_id'] for s in sources if s['type'] == '3'], links)
            rows, _ = google.normalize_rows(report.google_context(), sources, CONFIG, mappings, dims, {'1234567890': 'USD'}, {})
            google.store_month(report.google_context(), connection, '2026-07', rows, mappings, {})
            if missing_campaign_fx:
                connection.execute("UPDATE google_insight SET usd_amount=NULL WHERE row_type=0")
            if incomplete_material:
                connection.execute("UPDATE google_insight SET usd_amount=NULL WHERE custom_source_id=? AND dt='2026-07-02'", (incomplete_material,))
            return report.build_month_payload(connection, '2026-07', 'final', report.load_keyword_config(), {})

    def selected(self, payload):
        return {r['custom_source_id']: r['selection_rule'] for r in payload['rows']}

    def test_a_cpc_ignores_af_and_b_minimum(self):
        payload = self.build([(9, 2, 1000, 200), (10, 1, 1000, 20)])
        self.assertEqual(self.selected(payload), {9: 'A'})
        row = payload['rows'][0]
        self.assertIsNone(row['af_d0_first_transactions'])
        self.assertEqual(row['evidence']['material_cpc'], 0.01)
        self.assertEqual(row['evidence']['platform_cpc'], 0.013636)

    def test_a_uses_platform_not_mapped_pool_denominator(self):
        # Mapped 600, all PIC/VID 1000 (not Campaign 10000): both cross 500.
        extra = [source(resource_id='customers/1234567890/assets/999', cost='400000000', clicks='0')]
        payload = self.build([(9, 400, 10000, 800), (10, 200, 10000, 400)], extra=extra)
        self.assertEqual(self.selected(payload), {9: 'A', 10: 'A'})
        self.assertEqual(payload['rows'][1]['evidence']['cumulative_spend_ratio'], 0.6)
        self.assertEqual(payload['benchmarks'][0]['spend'], 1000)

    def test_a_crossover_ties_and_cpc_equal_excluded(self):
        extra = [source(resource_id='customers/1234567890/assets/999', cost='650000000', clicks='0')]
        payload = self.build([(9, 400, 10000, 80), (10, 200, 10000, 40),
                              (11, 200, 10000, 20), (12, 50, 10000, 10)], extra=extra)
        self.assertEqual(self.selected(payload), {9: 'A', 10: 'A'})
        self.assertEqual(payload['rows'][1]['evidence']['cumulative_spend_ratio'], 0.53333333)
        self.assertEqual(payload['rows'][1]['evidence']['platform_cpc'], 10)

    def test_a_coverage_below_half_disables_a(self):
        extra = [source(resource_id='customers/1234567890/assets/999', cost='501000000', clicks='0')]
        payload = self.build([(9, 499, 10000, 1000)], extra=extra)
        self.assertEqual(self.selected(payload), {})
        self.assertFalse(payload['audits'][0]['rule_a_available'])

    def test_a_zero_clicks_not_qualified(self):
        self.assertEqual(self.selected(self.build([(9, 6000, 10000, 0)])), {})
        self.assertEqual(self.selected(self.build([(9, 6000, 10000, 800)], campaign_clicks=0)), {})

    def test_b_uses_all_picture_video_weighted_ctr_not_campaign(self):
        extra = [source(resource_id='customers/1234567890/assets/999', cost='0', clicks='0', impressions='900000', asset_type='4')]
        payload = self.build([(9, 6000, 100000, 2000)], campaign_spend=100000,
                             campaign_clicks=50000, extra=extra)
        self.assertEqual(self.selected(payload), {9: 'B'})
        self.assertEqual(payload['rows'][0]['evidence']['platform_ctr'], 0.002)
        self.assertEqual(payload['benchmarks'][0]['picture_video_impressions'], 1000000)
        self.assertEqual(payload['benchmarks'][0]['ctr'], 0.002)

    def test_b_excludes_text_and_other_app_from_baseline(self):
        extras = [source(asset_type='1', resource_id='customers/1234567890/assets/999', impressions='9999999', clicks='0'),
                  source(app_id='pk', app_name='OPayPakistan', resource_id='customers/1234567890/assets/888', impressions='9999999', clicks='0')]
        payload = self.build([(9, 6000, 100000, 2000)], campaign_spend=100000, extra=extras)
        self.assertEqual(self.selected(payload), {})  # NG CTR equals itself
        self.assertEqual(payload['benchmarks'][0]['picture_video_impressions'], 100000)

    def test_a_plus_b_unaffected_by_campaign_fx_missing(self):
        extra = [source(resource_id='customers/1234567890/assets/999', clicks='0', cost='1000000000')]
        kwargs = dict(materials=[(9, 6000, 100000, 2000)], extra=extra)
        self.assertEqual(self.selected(self.build(**kwargs)), {9: 'A+B'})
        self.assertEqual(self.selected(self.build(**kwargs, missing_campaign_fx=True)), {9: 'A+B'})

    def test_b_strict_1000_and_ctr_equal(self):
        extra = [source(resource_id='customers/1234567890/assets/999', clicks='0', cost='0')]
        self.assertEqual(self.selected(self.build([(9, 1000, 100000, 2000)], extra=extra)), {})
        self.assertEqual(self.selected(self.build([(9, 1000.01, 100000, 2000)], extra=extra)), {9: 'B'})
        self.assertEqual(self.selected(self.build([(9, 999.99, 100000, 2000)], extra=extra)), {})
        self.assertEqual(self.selected(self.build([(9, 6000, 100000, 2000)], campaign_spend=100000)), {})

    def test_partial_usd_of_incomplete_material_cannot_enable_a(self):
        extra = [source(resource_id='customers/1234567890/assets/10', dt='2026-07-02', cost='1000000'),
                 source(resource_id='campaign2', type='0', asset_type='0', dt='2026-07-02', cost='0')]
        payload = self.build([(9, 4000, 10000, 800), (10, 3000, 10000, 600)], extra=extra, incomplete_material=10)
        self.assertEqual(self.selected(payload), {9: 'B'})
        self.assertGreater(payload['audits'][0]['exact_mapped_spend'], 5000)
        self.assertEqual(payload['audits'][0]['eligible_mapped_spend'], 4000)
        self.assertFalse(payload['audits'][0]['rule_a_available'])

    def test_campaign_changes_never_change_picvid_results(self):
        values = [(9, 2, 1000, 200), (10, 1, 1000, 20)]
        for amount, clicks in ((0, 0), (100000000, 10000000), (1, 1)):
            payload = self.build(values, campaign_spend=amount, campaign_clicks=clicks)
            self.assertEqual(self.selected(payload), {9: 'A'})
            self.assertEqual(payload['benchmarks'][0]['spend'], 3)

    def test_non_google_b_minimum_remains_5000(self):
        total = {'clicks': 10, 'impressions': 1000}
        row = {'spend_cents': 100001, 'clicks': 20, 'impressions': 1000}
        self.assertFalse(report.rule_b_qualifies(row, total))
        self.assertTrue(report.rule_b_qualifies(row, total, minimum_spend_cents=100000))

    def test_campaign_af_is_not_mixed_into_picvid_benchmark(self):
        with mock.patch.object(report, 'month_aggregates', return_value=({}, {(1, 'NG OPay'): 124815}, {}, [])):
            payload = self.build([(9, 2, 1000, 200), (10, 1, 1000, 20)])
        bench = payload['benchmarks'][0]
        self.assertIsNone(bench['af_d0_first_transactions'])
        self.assertIsNone(bench['metrics']['d0_cpa'])
        self.assertIsNone(bench['metrics']['apm'])
        self.assertFalse(payload['rows'][0]['evidence']['platform_cpa_available'])
        self.assertEqual(self.selected(payload), {9: 'A'})

    def test_publisher_rejects_frozen_old_google_policy_before_public_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / 'cache.sqlite3'
            report.save_month_snapshot('2026-07', 'final', cache_db=cache, data_root=root, media_enabled=False)
            output = root / 'public'
            output.mkdir()
            latest = output / 'latest.json'
            report.atomic_write(latest, b'old-manifest', binary=True)
            with mock.patch.object(report, 'load_snapshot', return_value={'schema_version': 2, 'selection_policy': {}}):
                with self.assertRaisesRegex(RuntimeError, 'Google CPC/image-video policy'):
                    report.publish_visible_state(cache_db=cache, data_root=root, output_dir=output)
            self.assertEqual(latest.read_bytes(), b'old-manifest')
            self.assertFalse((output / 'index.html').exists())


if __name__ == '__main__':
    unittest.main()
