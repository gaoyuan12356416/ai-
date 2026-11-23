import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from features.moboreels_daily_report.collector import assemble, blank, collect_publications, serialize, utc_day
from features.moboreels_daily_report.report import build_cards, CHAT_ID, size
from features.moboreels_daily_report.delivery import SocialDeliveryStore, deliver
from features.moboreels_daily_report.transport import DeliveryUnknown, DefiniteFailure
from features.moboreels_library.promotions import publisher_identity


class DailyReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.link = dict(tenant_key='tenant1', user_id='owner1', publisher_name='张三',
            platform='x', task_id='x-queue:1', account_id='3', material_id='4', short_url='https://example/m/a',
            purpose='post', created_at='2026-11-21T23:00:00Z')
        self.x = self.root / 'x.db'
        with closing(sqlite3.connect(self.x)) as c, c:
            c.executescript('CREATE TABLE x_post_queue(id INTEGER,material_id TEXT);'
                'CREATE TABLE x_post_publish_log(queue_id INTEGER,account_id INTEGER,status TEXT,unknown_outcome INTEGER,'
                'x_post_id TEXT,published_at TEXT,short_url TEXT,post_text TEXT);')
            c.execute('INSERT INTO x_post_queue VALUES(1,\'4\')')
            c.execute('INSERT INTO x_post_publish_log VALUES(1,3,\'published\',0,\'123\',\'2026-11-22T12:00:00Z\',?,\'\')',
                      (self.link['short_url'],))

    def facts(self, links=None):
        return collect_publications(links or [self.link], '2026-11-22', [self.x], self.root / 'fb.db')

    def report(self):
        row = serialize(blank())
        row.update(order_recharge_usd='12.345', order_refund_usd='1.234', order_net_usd='11.111',
                   advertising_revenue_usd='0.002', total_revenue_usd='11.113', clicks=3)
        row['dimensions'] = [dict(key='publisher', value=publisher_identity('org', self.link), label='张三'),
                             dict(key='platform', value='shared', label='多渠道共用')]
        effects = dict(list=[row], summary=row, complete_order_days=1, complete_ad_days=1,
                       click_coverage_from='2026-01-01')
        facts, issues = self.facts()
        return assemble('2026-11-22', effects, [self.link], facts, issues, 'org')

    def test_confirmed_native_post_only_and_dedup(self):
        facts, issues = self.facts([self.link, dict(self.link)])
        self.assertEqual(len(facts), 1)
        self.assertEqual(issues, [])
        with closing(sqlite3.connect(self.x)) as c, c:
            c.execute('UPDATE x_post_publish_log SET unknown_outcome=1')
        self.assertEqual(self.facts()[0], [])

    def test_mismatched_account_cannot_count(self):
        link = dict(self.link, account_id='99')
        facts, issues = self.facts([link])
        self.assertEqual(facts, [])
        self.assertTrue(issues)

    def test_mobo_url_in_frozen_post_text_with_legacy_tracking_url(self):
        with closing(sqlite3.connect(self.x)) as c, c:
            c.execute('UPDATE x_post_publish_log SET short_url=\'https://legacy/s/1\',post_text=?',
                      ('Watch ' + self.link['short_url'] + ' now',))
        self.assertEqual(len(self.facts()[0]), 1)
        with closing(sqlite3.connect(self.x)) as c, c:
            c.execute('UPDATE x_post_publish_log SET post_text=?', (self.link['short_url'] + 'DIFFERENT',))
        self.assertEqual(self.facts()[0], [])

    def test_nonpublic_links_and_future_links_do_not_count(self):
        self.assertEqual(self.facts([dict(self.link, purpose='manual_home')])[0], [])
        self.assertEqual(self.facts([dict(self.link, created_at='2026-11-23T00:00:00Z')])[0], [])

    def test_same_post_conflicting_owner_is_not_arbitrarily_assigned(self):
        facts, issues = self.facts([self.link, dict(self.link, user_id='other')])
        self.assertEqual(facts, [])
        self.assertTrue(issues)

    def test_facebook_success_requires_receipt_and_matching_date(self):
        fb = self.root / 'fb.db'
        with closing(sqlite3.connect(fb)) as c, c:
            c.execute('CREATE TABLE fb_auto_task(id INTEGER,page_id TEXT,status TEXT,unknown_outcome INTEGER,'
                      'graph_post_id TEXT,completed_at_utc TEXT,short_url TEXT,material_id TEXT,message_text TEXT)')
            c.execute('INSERT INTO fb_auto_task VALUES(1,\'3\',\'published\',0,\'3_45\','
                      '\'2026-11-23T07:59:59+08:00\',?,\'4\',\'\')', (self.link['short_url'],))
        facts, issues = self.facts([dict(self.link, platform='fb', task_id='fb:auto:1')])
        self.assertEqual(len(facts), 1)
        self.assertEqual(issues, [])

    def test_missing_source_is_reported(self):
        self.x.unlink()
        facts, issues = self.facts()
        self.assertEqual(facts, [])
        self.assertIn('X 发布回执来源未完整读取', issues)

    def test_exact_money_conservation_and_shared_not_duplicated(self):
        report = self.report()
        self.assertEqual(report['totals']['total_revenue_usd'], '11.113')
        self.assertEqual(report['rows'][0]['platforms']['x']['total_revenue_usd'], '0')
        self.assertEqual(report['totals']['published_count'], 1)
        self.assertTrue(report['quality']['complete'])

    def test_failed_finance_never_shown_as_zero_income(self):
        facts, _ = self.facts()
        report = assemble('2026-11-22', None, [self.link], facts, ['收益查询未完成'], 'org')
        text = json.dumps(build_cards(report), ensure_ascii=False)
        self.assertIn('待同步', text)
        self.assertNotIn('$0.00', text)
        self.assertEqual(report['totals']['published_count'], 1)

    def test_tenants_with_same_user_remain_separate(self):
        report = assemble('2026-11-22', None, [self.link, dict(self.link, tenant_key='tenant2')], [], [], 'org')
        self.assertEqual(len(report['rows']), 2)

    def test_utc_day_requires_timezone(self):
        self.assertEqual(utc_day('2026-11-23T07:59:59+08:00'), '2026-11-22')
        self.assertIsNone(utc_day('2026-11-22T00:00:00'))

    def test_card_fragment_size(self):
        report = self.report()
        report['rows'] = [dict(copy.deepcopy(report['rows'][0]), name='测试发布人' + str(i)) for i in range(150)]
        cards = build_cards(report)
        self.assertGreater(len(cards), 1)
        self.assertTrue(all(size(c) <= 29000 for c in cards))

    def test_duplicate_delivery_is_not_sent(self):
        store = SocialDeliveryStore(self.root / 'delivery.db')
        self.addCleanup(store.close)
        store.freeze('2026-11-22', CHAT_ID, self.report(), [{'test': 1}, {'test': 2}])
        calls = []
        def sender(card, uid):
            calls.append(uid)
            return dict(message_id='message' + str(len(calls)))
        self.assertEqual(deliver(store, '2026-11-22', CHAT_ID, sender)['status'], 'sent')
        self.assertEqual(deliver(store, '2026-11-22', CHAT_ID, sender)['status'], 'already_sent')
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])

    def test_unknown_delivery_never_retried(self):
        store = SocialDeliveryStore(self.root / 'delivery.db')
        self.addCleanup(store.close)
        store.freeze('2026-11-22', CHAT_ID, self.report(), [{'test': 1}])
        calls = []
        def sender(card, uid):
            calls.append(uid)
            raise TimeoutError()
        for _ in range(2):
            with self.assertRaises(DeliveryUnknown):
                deliver(store, '2026-11-22', CHAT_ID, sender)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
