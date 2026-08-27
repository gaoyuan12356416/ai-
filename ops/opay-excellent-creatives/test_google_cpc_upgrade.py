"""Independent-validator mutation tests; only disposable local fixtures are used."""

import ast
import copy
import hashlib
import json
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import opay_excellent_creatives as report
import test_google_cpc_policy as policy_fixtures
from test_google_creatives import source
import validate_google_cpc_upgrade as validator
from validate_v2_upgrade import ValidationError


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class ReadOnlyFixture:
    """Snapshot real fixture cache rows and reject any unanticipated SQL/write."""
    def __init__(self, connection):
        self.facts = [dict(row) for row in connection.execute('SELECT * FROM google_insight')]
        self.dims = {row['custom_source_id']: (row['product'], row['material_type'])
                     for row in connection.execute('SELECT * FROM material_dim')}
        self.refreshed = {row['month'] for row in connection.execute('SELECT * FROM google_month_refresh')}

    def close(self):
        pass

    def execute(self, sql, params=()):
        if sql == 'PRAGMA quick_check':
            return Cursor([('ok',)])
        if sql == 'SELECT 1 FROM google_month_refresh WHERE month=?':
            return Cursor([(1,)] if params[0] in self.refreshed else [])
        if sql.startswith('SELECT * FROM google_insight WHERE'):
            month, app = params
            return Cursor([row for row in self.facts if row['dt'][:7] == month and row['app'] == app])
        if sql.startswith('SELECT product,material_type FROM material_dim WHERE'):
            return Cursor([self.dims[params[0]]] if params[0] in self.dims else [])
        raise AssertionError('unexpected SQL (read-only fixture): ' + sql)


class GoogleCpcUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.build_fixture()

    def build_fixture(self, mutate_cache=None, **kwargs):
        original_build = report.build_month_payload

        def capture(connection, *args, **options):
            if mutate_cache:
                mutate_cache(connection)
            payload = original_build(connection, *args, **options)
            self.cache = ReadOnlyFixture(connection)
            return payload

        kwargs.setdefault('materials', [(9, 6000, 100000, 2000)])
        kwargs.setdefault('extra', [source(resource_id='customers/1234567890/assets/999', cost='1000000000', clicks='0')])
        with mock.patch.object(report, 'build_month_payload', side_effect=capture):
            payload = policy_fixtures.GoogleCpcPolicyTests().build(**kwargs)
        # Match the actual public JSON reader's Decimal semantics.
        self.payload = json.loads(json.dumps(payload), parse_float=Decimal)
        self.baseline = copy.deepcopy(self.payload)

    def validate(self, candidate=None):
        candidate = self.payload if candidate is None else candidate
        manifest_bytes = b'local-in-memory-manifest'
        digest = hashlib.sha256(manifest_bytes).hexdigest()
        with mock.patch.object(validator, 'MONTHS', ('2026-07',)), \
                mock.patch.object(validator, 'load_manifest', return_value=(
                    {'data_version': 'fixture'}, {'2026-07': {}}, digest)), \
                mock.patch.object(validator, 'load_month', side_effect=lambda root, manifest, entry, schema, side:
                                  (self.baseline if side == 'baseline' else candidate, 'unused')), \
                mock.patch.object(validator.sqlite3, 'connect', return_value=self.cache) as connect, \
                mock.patch.object(Path, 'read_bytes', return_value=manifest_bytes):
            result = validator.validate('qa-baseline', 'qa-candidate', 'qa-cache')
            self.assertIn('?mode=ro', connect.call_args.args[0])
            self.assertTrue(connect.call_args.kwargs['uri'])
            return result

    @staticmethod
    def google(payload, field='rows', app='NG OPay'):
        return next(row for row in payload[field] if row['channel'] == 'Google' and row['app'] == app)

    def reject_mutation(self, mutate, pattern='mismatch|precision|selected set|frozen|refreshed'):
        candidate = copy.deepcopy(self.payload)
        mutate(candidate)
        with self.assertRaisesRegex(ValidationError, pattern):
            self.validate(candidate)

    def test_valid_generator_payload_and_zero_asset_scope_pass(self):
        result = self.validate()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['months'][0]['google'][1]['count'], 0)

    def test_zero_campaign_usd_does_not_change_picvid_a(self):
        self.build_fixture(campaign_spend=0)
        row = self.google(self.payload)
        self.assertEqual(row['selection_rule'], 'A+B')
        self.assertEqual(row['evidence']['cumulative_spend_ratio'], Decimal('0.85714286'))
        self.assertEqual(self.validate()['status'], 'PASS')
        self.reject_mutation(lambda p: self.google(p)['evidence'].update(cumulative_spend_ratio=None), 'finite JSON number')

    def test_missing_campaign_fx_keeps_picvid_a_and_cumulative(self):
        self.build_fixture(missing_campaign_fx=True)
        self.assertEqual(self.google(self.payload)['selection_rule'], 'A+B')
        self.assertEqual(self.google(self.payload)['evidence']['cumulative_spend_ratio'], Decimal('0.85714286'))
        self.assertEqual(self.validate()['status'], 'PASS')
        self.reject_mutation(lambda p: self.google(p, 'audits').update(platform_fx_missing_rows=1))
        self.reject_mutation(lambda p: self.google(p, 'audits').update(platform_fx_missing_native_spend={'USD': 10000}))

    def test_missing_campaign_account_day_keeps_b(self):
        self.build_fixture(mutate_cache=lambda c: c.execute('DELETE FROM google_insight WHERE row_type=0'))
        self.assertEqual(self.google(self.payload)['selection_rule'], 'A+B')
        self.assertEqual(self.validate()['status'], 'PASS')
        self.reject_mutation(lambda p: self.google(p, 'audits').update(baseline_missing_account_days=0))

    def test_incomplete_material_uses_whole_month_exclusion_and_native_gap(self):
        extras = [source(resource_id='customers/1234567890/assets/10', dt='2026-07-02', cost='1000000'),
                  source(resource_id='campaign2', type='0', asset_type='0', dt='2026-07-02', cost='0')]
        self.build_fixture(materials=[(9, 4000, 10000, 800), (10, 3000, 10000, 600)],
                           extra=extras, incomplete_material=10)
        self.assertEqual([r['custom_source_id'] for r in self.payload['rows']], [9])
        self.assertEqual(self.payload['rows'][0]['selection_rule'], 'B')
        self.assertEqual(self.validate()['status'], 'PASS')
        audit = self.google(self.payload, 'audits')
        self.assertEqual(audit['eligible_mapped_spend'], 4000)
        for field, bad in (('incomplete_material_count', 0), ('fx_missing_rows', 0),
                           ('eligible_mapped_spend', 7000), ('fx_missing_native_spend', {})):
            with self.subTest(field=field):
                self.reject_mutation(lambda p: self.google(p, 'audits').update({field: bad}))

    def test_crossover_ties_and_cpc_equality_exclusion(self):
        self.build_fixture(materials=[(9, 400, 10000, 80), (10, 200, 10000, 40),
                                      (11, 200, 10000, 20), (12, 50, 10000, 10)],
                           extra=[source(resource_id='customers/1234567890/assets/999', cost='650000000', clicks='0')])
        self.assertEqual({r['custom_source_id'] for r in self.payload['rows']}, {9, 10})
        self.assertEqual(self.validate()['status'], 'PASS')

    def test_missing_or_extra_selected_material_is_rejected(self):
        self.reject_mutation(lambda p: p['rows'].clear())
        self.reject_mutation(lambda p: p['rows'].append(dict(p['rows'][0], custom_source_id=999)))

    def test_wrong_selection_and_boolean_values_are_rejected(self):
        self.reject_mutation(lambda p: self.google(p).update(selection_rule='B'))
        for field in ('rule_a_pass', 'rule_b_pass', 'rule_a_available', 'in_top_50_percent',
                      'platform_cpa_available', 'platform_cpa_finite'):
            for value in (0, 1, 'true', None):
                if validator.exact_equal(self.google(self.payload)['evidence'][field], value):
                    continue
                with self.subTest(field=field, value=value):
                    self.reject_mutation(lambda p: self.google(p)['evidence'].update({field: value}))
        for value in (0, 1, 'true', None, False):
            with self.subTest(audit=value):
                self.reject_mutation(lambda p: self.google(p, 'audits').update(rule_a_available=value))

    def test_wrong_evidence_flags_counts_and_coverage_are_rejected(self):
        changes = {'rule_a_available': False, 'in_top_50_percent': False, 'eligible_mapped_spend': 0,
                   'picture_video_clicks': 0, 'picture_video_impressions': 0,
                   'source_row_count': 99, 'ad_day_count': 99, 'asset_count': 99, 'fx_sources': [],
                   'exact_mapping_spend_coverage': Decimal('0.123'), 'rule_a_unavailable_reason': 'wrong'}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.reject_mutation(lambda p: self.google(p)['evidence'].update({field: value}))

    def test_wrong_or_missing_provenance_and_missing_value_statuses_are_rejected(self):
        for field in ('mapping_status', 'metric_source', 'usd_status', 'af_status', 'installs_status',
                      'platform_ctr_scope', 'platform_spend_scope', 'platform_cpc_scope', 'rule_a_metric'):
            with self.subTest(field=field):
                self.reject_mutation(lambda p: self.google(p)['evidence'].update({field: 'wrong'}))
                self.reject_mutation(lambda p: self.google(p)['evidence'].pop(field))

    def test_incorrect_or_overprecision_ctr_and_cpc_evidence_is_rejected(self):
        for field in ('material_ctr', 'platform_ctr', 'material_cpc', 'platform_cpc', 'cumulative_spend_ratio'):
            for value in (Decimal('0.987654321'), Decimal('999')):
                with self.subTest(field=field, value=value):
                    self.reject_mutation(lambda p: self.google(p)['evidence'].update({field: value}))

    def test_wrong_audit_counts_mapping_statuses_and_status_are_rejected(self):
        for field in ('fx_missing_rows', 'platform_fx_missing_rows', 'incomplete_material_count',
                      'invalid_row_count', 'asset_count', 'baseline_missing_account_days', 'selected_count'):
            with self.subTest(field=field):
                self.reject_mutation(lambda p: self.google(p, 'audits').update({field: 123}))
        self.reject_mutation(lambda p: self.google(p, 'audits').update(mapping_status_counts={'exact': True, 'unmapped': 1}))
        self.reject_mutation(lambda p: self.google(p, 'audits').update(mapping_status_counts={'exact': 2}))
        self.reject_mutation(lambda p: self.google(p, 'audits').update(status='not_refreshed'))

    def test_wrong_audit_provenance_and_metrics_are_rejected(self):
        for field, value in (('metric_source', 'wrong'), ('rule_b_ctr_source', 'wrong'),
                             ('rule_a_metric', 'wrong'), ('eligible_mapped_spend', 0),
                             ('picture_video_ctr', Decimal('0.987654321')), ('platform_cpc', 999),
                             ('mapping_coverage', Decimal('0.123')), ('exact_mapped_spend', 999),
                             ('fx_missing_native_spend', {'USD': 1})):
            with self.subTest(field=field):
                self.reject_mutation(lambda p: self.google(p, 'audits').update({field: value}))

    def test_rejected_mapping_categories_conserve_spend_and_counts(self):
        extras = [source(resource_id=f'customers/1234567890/assets/{999+i}', cost='1250000', clicks='0')
                  for i in range(3)]
        def mutate_cache(connection):
            for i, status in enumerate(('ambiguous', 'out_of_scope', 'app_mismatch')):
                connection.execute('UPDATE google_insight SET mapping_status=? WHERE resource_id=?',
                                   (status, f'customers/1234567890/assets/{999+i}'))
        self.build_fixture(extra=extras, mutate_cache=mutate_cache)
        self.assertEqual(self.validate()['status'], 'PASS')
        for field in ('ambiguous_spend', 'out_of_scope_spend', 'invalid_mapping_spend'):
            with self.subTest(field=field):
                self.reject_mutation(lambda p: self.google(p, 'audits').update({field: 0}))

    def test_main_refresh_gate_is_preserved(self):
        self.cache.refreshed.clear()
        with self.assertRaisesRegex(ValidationError, 'missing Google refresh'):
            self.validate()

    def test_frozen_non_google_whole_fields_remain_protected(self):
        frozen = copy.deepcopy(self.payload['rows'][0])
        frozen.update(channel='Meta', custom_source_id=777)
        frozen['evidence']['unknown'] = {'ordered': [1, 2], 'flag': True}
        self.payload['rows'].append(frozen)
        self.baseline = copy.deepcopy(self.payload)
        self.assertEqual(self.validate()['status'], 'PASS')
        self.reject_mutation(lambda p: p['rows'][-1].update(maker='changed'))
        self.reject_mutation(lambda p: p['rows'][-1]['evidence']['unknown'].update(flag=1))
        self.reject_mutation(lambda p: p['rows'][-1]['evidence']['unknown'].update(ordered=[2, 1]))
        for field in ('audits', 'benchmarks'):
            self.reject_mutation(lambda p: next(r for r in p[field] if r['channel'] == 'Meta').update(unknown='changed'))

    def test_validator_remains_independent_of_generator_and_mapping_helpers(self):
        tree = ast.parse(Path(validator.__file__).read_text(encoding='utf-8'))
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
        self.assertNotIn('opay_excellent_creatives', imports)
        self.assertNotIn('google_creatives', imports)

    def test_approved_july_fixture_guard_rejects_id_spend_and_group_drift(self):
        approved = json.loads((Path(__file__).parent / 'fixtures/2026-07-google-picvid-approved.json').read_text())
        payload = {'rows': [dict(r, channel='Google', evidence={k: r[k] for k in ('rule_a_pass', 'rule_b_pass')}) for r in approved['rows']],
                   'benchmarks': [dict(s, channel='Google') for s in approved['scopes']]}
        validator.validate_approved_july(payload, approved)
        self.assertEqual(len(payload['rows']), 46)
        for key, value in (('custom_source_id', 999), ('spend', '0'), ('selection_rule', 'B')):
            mutated = copy.deepcopy(payload)
            mutated['rows'][0][key] = value
            with self.assertRaises(ValidationError):
                validator.validate_approved_july(mutated, approved)


if __name__ == '__main__':
    unittest.main()
