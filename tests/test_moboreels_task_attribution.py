"""Task identities preserve owner scope, channel attribution and exact totals."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest

from features.moboreels_library.clicks import BEIJING, ClickStats
from features.moboreels_library.effects import Effects, read_records
from features.moboreels_library.orders import ORDER_TABLE, ReportStore, Reports
from features.moboreels_library.promotions import PromotionManager, identity
from features.moboreels_library.settings import Settings
from features.moboreels_library.short_links import ShortLinks
from test_moboreels_promotions import ACTOR, FakeDB, FakeProvider


class TaskAttributionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.settings = Settings(Path(temp.name), 'institution', 'test', 'test', 'test', 'test')
        self.day = str(datetime.now(BEIJING).date())
        self.params = {'date_from': self.day, 'date_to': self.day}
        self.actor = dict(ACTOR)
        self.other = dict(ACTOR, tenant_key='another-tenant', name='Another owner')
        self.owner = identity('institution', self.actor)[0]
        manager = PromotionManager(self.settings, FakeDB(), FakeProvider())
        links = ShortLinks(self.settings, manager.db, manager)
        for actor, platform, task in [(self.actor, 'x', 'one'), (self.actor, 'fb', 'two'), (self.other, 'x', 'one')]:
            links.resolve(dict(actor=actor, platform=platform, task_id=task, story_id='42',
                               account_id='acct-' + platform, material_id='material', purpose='post'))
        self.links = read_records(self.settings.data_dir/'short-links.sqlite3', 'links')
        self.mappings = read_records(self.settings.data_dir/'promotions.sqlite3', 'promotions')
        self.source = []
        for index, mapping in enumerate(self.mappings):
            mapping['id'] = index + 1
            base = dict(_kind='order', date=self.day, code=mapping['koc_code'], story_id='42', story_name='Fixture',
                        real_star_user_id=mapping['star_user_id'], classification_reason='confirmed',
                        source_status_text='recharge', coo_order_status_raw='1', source_project_type='2',
                        undated_count=0, count=1)
            self.source.append(dict(base, classification=0, amount_usd='10.00', absolute_amount_usd='10.00'))
            if index == 0:
                self.source.append(dict(base, classification=1, amount_usd='-2.00', absolute_amount_usd='2.00'))
            self.source.append(dict(_kind='ad', date=self.day, code=mapping['koc_code'], amount_usd='0.25', count=1))
        self.source.append(dict(_kind='ad_day', date=self.day))
        self.queries = []

        def read(sql):
            self.queries.append(sql)
            if 'FROM ' + ORDER_TABLE in sql:
                # Intentionally return all owners; the report must enforce scope too.
                return deepcopy(self.source)
            if 'FROM ads_ai.cdreader_koc_promotion' in sql:
                return deepcopy(self.mappings)
            return []

        self.store = ReportStore(self.settings, SimpleNamespace(_read=read))
        self.clicks = ClickStats(self.settings)
        with self.clicks.connect() as conn:
            for link in self.links:
                conn.execute('INSERT INTO daily VALUES(?,?,?,?,?)', (self.day, link['short_code'], 'unknown', 1, 0))
        reports = SimpleNamespace(store=self.store, _jobs=lambda: [])
        self.effects = Effects(self.settings, reports, self.clicks)

    def test_owner_reports_include_task_ids_and_exclude_same_user_in_other_tenant(self):
        result = self.store.query(self.params, star_id=self.owner,
                                  owner_scope=(self.actor['tenant_key'], self.actor['user_id']))
        self.assertEqual(result['summary']['total_revenue_usd'], '18.50')
        self.assertEqual(result['summary']['order_recharge_count'], 2)
        self.assertEqual(result['summary']['order_refund_usd'], '2.00')
        self.assertEqual(len(result['list']), 2)
        self.assertEqual({r['publisher_id'] for r in result['list']}, {self.owner})
        self.assertNotIn(self.owner, {r['real_star_user_id'] for r in result['list']})
        self.assertIn("real_star_user_id IN (SELECT star_user_id", self.queries[0])
        self.assertIn('tenant_key=', self.queries[0])
        self.assertIn('user_id=', self.queries[0])
        self.assertIn("status='succeeded'", self.queries[0])

    def test_effects_join_task_income_to_channels_but_group_by_actual_publisher(self):
        result = self.effects.query(self.params, self.actor)
        self.assertEqual(result['summary']['total_revenue_usd'], '18.50')
        self.assertEqual(result['summary']['clicks'], 2)
        self.assertEqual(result['total'], 2)
        self.assertEqual({r['dimensions'][0]['value'] for r in result['list']}, {'x', 'fb'})
        self.assertEqual({r['dimensions'][1]['value'] for r in result['list']}, {self.owner})
        self.assertEqual({r['dimensions'][1]['label'] for r in result['list']}, {ACTOR['name']})
        x = self.effects.query(dict(self.params, platform='x'), self.actor)
        self.assertEqual(x['summary']['total_revenue_usd'], '8.25')
        self.assertEqual(x['summary']['clicks'], 1)

    def test_admin_aggregates_once_and_account_filter_remains_distinct(self):
        result = self.effects.query(dict(self.params, group_by='publisher,account'), dict(self.actor, is_admin=True))
        self.assertEqual(result['summary']['total_revenue_usd'], '28.75')
        self.assertEqual(result['summary']['clicks'], 3)
        self.assertEqual(result['total'], 3)

    def test_click_report_shows_name_without_legacy_owner_promotion(self):
        result = self.clicks.query(dict(self.params, group_by='platform_publisher'), self.actor)
        self.assertEqual(result['summary']['clicks'], 2)
        self.assertEqual({r['publisher_name'] for r in result['list']}, {ACTOR['name']})

    def test_order_scope_adapter_passes_authenticated_tenant_and_user(self):
        manager = Reports.__new__(Reports)
        manager.settings, manager.store = self.settings, self.store
        manager._jobs = lambda: []
        manager.latest = lambda: None
        manager.last_success_at = lambda: None
        result = manager.query(self.params, self.actor)
        self.assertEqual(result['summary']['total_revenue_usd'], '18.50')


if __name__ == '__main__':
    unittest.main()
