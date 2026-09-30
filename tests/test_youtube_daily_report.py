import json
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from features.post_daily_report.delivery import DeliveryStore, DeliveryUnknown
from features.youtube_daily_report.collector import collect, collect_metrics, load_ledger, summarize
from features.youtube_daily_report.report import build_card


def publication(number=1, owner='a', name='甲', when='2026-09-19T01:00:00Z', **changes):
    row = dict(id=number, job_id='task'+str(number), source_kind='custom_source', preparation_id='task'+str(number),
        operator_user_id=owner, operator_name=name, video_id='video'+str(number), video_state='published',
        privacy_status='public', public_status='succeeded', workflow='reviewed_thumbnail',
        video_published_at_utc=when, status='published', created_at_utc='2026-09-17T01:00:00Z')
    row.update(changes)
    return row


def link(number=1, campaign='campaign', **changes):
    """A synthetic frozen analytics catalog row, not a mutable channel record."""
    row = dict(key=('task'+str(number), campaign), link_id=number, tenant='tenant-a',
        owner='a', owner_label='甲', drama='drama'+str(number), drama_label='短剧'+str(number),
        channel='channel'+str(number), channel_label='频道'+str(number), language='zh',
        language_label='zh', link_type='auto', link_type_label='自动发布',
        date='2026-09-17', excluded=False)
    row.update(changes)
    return row


def metric(number=1, campaign='campaign', **changes):
    row = dict(campaign_id='task'+str(number),campaign=campaign,revenue_cents=1234,refund_cents=0,
        installs=3,views=50,clicks=21,recharge=2,raw_impressions=0,source_rows=4,updated_at_utc='2026-09-20 02:40:00')
    row.update(changes)
    return row


class ReportTests(unittest.TestCase):
    def result(self, publications=None, links=None, metrics=None, zone='Asia/Shanghai'):
        return summarize(publications if publications is not None else [publication()],
            links if links is not None else [link()], metrics if metrics is not None else [metric()],
            '2026-09-19', zone)

    def test_beijing_half_open_boundaries(self):
        rows=[publication(1,when='2026-09-18T15:59:59Z'),publication(2,when='2026-09-18T16:00:00Z'),
            publication(3,when='2026-09-19T15:59:59Z'),publication(4,when='2026-09-19T16:00:00Z')]
        report=self.result(rows)
        self.assertEqual(report['totals']['published'],2)
        self.assertEqual(report['rows'][0]['publication_ids'],[2,3])

    def test_utc_boundary_option(self):
        rows=[publication(1,when='2026-09-18T17:00:00Z'),publication(2,when='2026-09-19T20:00:00Z')]
        self.assertEqual(self.result(rows,zone='UTC')['rows'][0]['publication_ids'],[2])

    def test_historical_content_with_zero_new_publications(self):
        result=self.result([publication(when='2026-09-15T00:00:00Z')])
        self.assertEqual(result['rows'][0]['published'],0)
        self.assertEqual(result['rows'][0]['revenue_cents'],1234)

    def test_effects_never_require_channel_field(self):
        self.assertEqual(self.result(metrics=[metric(channel='')])['totals']['revenue_cents'],1234)

    def test_video_count_deduplicates_same_owner(self):
        self.assertEqual(self.result([publication(),publication(2,video_id='video1')])['totals']['published'],1)

    def test_ambiguous_video_owner_rejected(self):
        with self.assertRaisesRegex(ValueError,'publication_owner_ambiguous'):
            self.result([publication(),publication(2,owner='b',video_id='video1')])

    def test_unknown_and_private_not_published(self):
        rows=[publication(1,video_state='unknown'),publication(2,privacy_status='private'),
              publication(3,public_status='pending'),publication(4,video_id='')]
        self.assertEqual(self.result(rows)['totals']['published'],0)

    def test_confirmed_video_with_comment_failure_still_counts(self):
        self.assertEqual(self.result([publication(status='failed')])['totals']['published'],1)

    def test_legacy_success_and_mapping(self):
        row=publication(workflow='legacy',public_status='pending')
        result=self.result([row],[link(campaign='ai_youtube')],[metric(campaign='ai_youtube')])
        self.assertEqual(result['totals']['published'],1)
        self.assertEqual(result['matched_campaigns'],1)

    def test_campaign_id_matches_even_when_source_name_is_wrong(self):
        report=self.result(metrics=[metric(campaign='external')])
        self.assertEqual(report['matched_campaigns'],1)
        self.assertEqual(report['unmatched']['revenue_cents'],0)
        self.assertEqual(report['totals']['revenue_cents'],1234)

    def test_shared_link_with_multiple_owners_not_arbitrarily_assigned(self):
        report=self.result([publication(),publication(2,owner='b',job_id='task1')],
            [link(),link(2,key=('task1','other-name'),owner='b')])
        self.assertEqual(report['unmatched']['campaigns'],1)

    def test_canary_excluded_and_totals_reconcile(self):
        report=self.result([publication(name='internal-deployment-canary')],
            [link(excluded=True)])
        self.assertEqual(report['rows'],[])
        self.assertEqual(report['excluded_canary']['revenue_cents'],1234)
        self.assertEqual(report['totals']['published'],0)

    def test_missing_source_is_not_zero(self):
        report=self.result(metrics=[])
        self.assertFalse(report['metrics_available'])
        self.assertIsNone(report['totals']['revenue_cents'])
        self.assertIsNone(report['rows'][0]['clicks'])
        self.assertEqual(report['totals']['published'],1)

    def test_duplicate_metrics_fail(self):
        with self.assertRaisesRegex(ValueError,'duplicate_metric_key'):
            self.result(metrics=[metric(),metric()])

    def test_no_cross_owner_allocation(self):
        report=self.result([publication(),publication(2,owner='b',name='乙')],
            [link(),link(2,owner='b',owner_label='乙')],[metric(),metric(2,revenue_cents=4)])
        self.assertEqual(report['totals']['revenue_cents'],1238)
        self.assertEqual([(r['owner_id'],r['revenue_cents']) for r in report['rows']],[('a',1234),('b',4)])

    def test_card_explains_timezones_and_unavailable_impressions(self):
        card=build_card(self.result())
        rendered=json.dumps(card,ensure_ascii=False)
        for text in ['北京时间自然日','UTC 自然日','YouTube 曝光：未接入','$12.34','全部历史内容',
                     '唯一 campaign ID 关联后台发布及手动短链记录']:
            self.assertIn(text,rendered)
        self.assertNotIn('曝光 **0**',rendered)

    def test_negative_count_rejected(self):
        with self.assertRaisesRegex(ValueError,'negative_metric'):
            self.result(metrics=[metric(clicks=-1)])

    def test_collector_error_is_unknown_not_zero(self):
        with patch('features.youtube_daily_report.collector.load_ledger',return_value=([publication()],[link()])), \
             patch('features.youtube_daily_report.collector.collect_metrics',side_effect=RuntimeError('secret')):
            report=collect('2026-09-19')
        self.assertFalse(report['metrics_available'])
        self.assertIsNone(report['totals']['installs'])
        self.assertNotIn('secret',json.dumps(report))

    def test_sql_uses_gate_readonly_and_bounded_site_date(self):
        settings=dict(ADMIN_MAPPING_MYSQL_USER='user',ADMIN_MAPPING_MYSQL_PASSWORD='not-real')
        output=type('Completed',(),dict(returncode=0,stdout='{"kind":"connection","read_only":1}\n',stderr=''))()
        with patch('features.youtube_daily_report.collector.read_settings',return_value=settings), \
             patch('features.youtube_daily_report.collector.subprocess.run',return_value=output) as run:
            self.assertEqual(collect_metrics('2026-09-19'),[])
        args,kwargs=run.call_args
        self.assertEqual(args[0][0],'/usr/bin/mysql')
        self.assertNotIn('SQL_GATE_BYPASS',kwargs['env'])
        self.assertIn('READ ONLY',kwargs['input'])
        self.assertIn("site_id='2284' AND dt='2026-09-19'",kwargs['input'])
        self.assertIn('GROUP BY BINARY campaign_id,BINARY campaign',kwargs['input'])
        self.assertLessEqual(kwargs['timeout'],60)

    def test_wrong_replica_flag_rejected(self):
        settings=dict(ADMIN_MAPPING_MYSQL_USER='user',ADMIN_MAPPING_MYSQL_PASSWORD='not-real')
        output=type('Completed',(),dict(returncode=0,stdout='{"kind":"connection","read_only":0}\n',stderr=''))()
        with patch('features.youtube_daily_report.collector.read_settings',return_value=settings), \
             patch('features.youtube_daily_report.collector.subprocess.run',return_value=output):
            with self.assertRaisesRegex(RuntimeError,'readonly_replica'):
                collect_metrics('2026-09-19')

    def test_delivery_namespace_cannot_collide_with_existing_report(self):
        with tempfile.TemporaryDirectory() as folder:
            first=DeliveryStore(Path(folder)/'old.db')
            second=DeliveryStore(Path(folder)/'youtube.db',namespace='youtube-publisher-daily-report')
            try:
                a=first.claim('2026-09-19','chat')
                b=second.claim('2026-09-19','chat')
                self.assertNotEqual(a,b)
                second.finish('2026-09-19','chat','sent','receipt')
                self.assertIsNone(second.claim('2026-09-19','chat'))
            finally:
                first.close();second.close()

    def test_unknown_delivery_blocks_resend(self):
        with tempfile.TemporaryDirectory() as folder:
            store=DeliveryStore(Path(folder)/'youtube.db',namespace='youtube-publisher-daily-report')
            try:
                store.claim('2026-09-19','chat')
                store.finish('2026-09-19','chat','unknown')
                with self.assertRaises(DeliveryUnknown):
                    store.claim('2026-09-19','chat')
            finally:
                store.close()


class CampaignAttributionTests(unittest.TestCase):
    PREFIX = 'campaign-prefix-abcdefghijklmnop'
    LONG_A = PREFIX + '-one'
    LONG_B = PREFIX + '-two'

    def result(self, catalog, metrics, publications=None):
        return summarize(publications or [], catalog, metrics, '2026-09-19', 'UTC')

    def assert_conserved(self, report):
        for field in ('revenue_cents', 'refund_cents', 'installs', 'views', 'clicks', 'recharge'):
            self.assertEqual(report['totals'][field] + report['unmatched'][field] +
                             report['excluded_canary'][field], report['source_totals'][field], field)

    def test_manual_link_has_revenue_but_no_confirmed_publication(self):
        report = self.result([link(link_type='manual')], [metric(campaign='wrong', channel='other-owner')])
        self.assertEqual(report['totals']['published'], 0)
        self.assertEqual(report['totals']['revenue_cents'], 1234)
        self.assertEqual((report['rows'][0]['tenant'], report['rows'][0]['owner_id']), ('tenant-a', 'a'))
        self.assertEqual(report['validation']['attribution'], 'unique_frozen_campaign_id_v2')
        self.assert_conserved(report)

    def test_multiple_source_names_add_once_under_one_case_sensitive_id(self):
        report = self.result([link()], [metric(campaign='wrong-one', revenue_cents=100),
                                      metric(campaign='', revenue_cents=200),
                                      metric(campaign_id='Task1', revenue_cents=300)])
        self.assertEqual(report['totals']['revenue_cents'], 300)
        self.assertEqual(report['unmatched']['revenue_cents'], 300)
        self.assertEqual(report['matched_campaigns'], 2)
        self.assert_conserved(report)

    def test_exact_32_character_alias_of_unique_long_id_matches(self):
        self.assertEqual(len(self.PREFIX), 32)
        report = self.result([link(key=(self.LONG_A, 'frozen'))],
                             [metric(campaign_id=self.PREFIX, campaign='wrong')])
        self.assertEqual(report['totals']['revenue_cents'], 1234)
        self.assertEqual(report['matched_metrics'][0]['frozen_campaign_id'], self.LONG_A)
        self.assert_conserved(report)

    def test_other_prefix_lengths_and_wrong_case_are_never_fuzzy_matched(self):
        metrics = [metric(campaign_id=value, campaign=str(i)) for i, value in enumerate(
            (self.LONG_A[:31], self.LONG_A[:33], self.PREFIX.upper()))]
        report = self.result([link(key=(self.LONG_A, 'frozen'))], metrics)
        self.assertEqual(report['totals']['revenue_cents'], 0)
        self.assertEqual(report['unmatched']['revenue_cents'], 3*1234)
        self.assert_conserved(report)

    def test_prefix_collision_stays_unmatched_even_when_name_points_to_one_link(self):
        catalog = [link(key=(self.LONG_A, 'first')),
                   link(2, key=(self.LONG_B, 'second'), drama='drama1', channel='channel1')]
        report = self.result(catalog, [metric(campaign_id=self.PREFIX, campaign='first')])
        self.assertEqual(report['totals']['revenue_cents'], 0)
        self.assertEqual(report['unmatched']['revenue_cents'], 1234)
        self.assert_conserved(report)

    def test_full_long_id_still_matches_when_prefix_is_ambiguous(self):
        catalog = [link(key=(self.LONG_A, 'first')),
                   link(2, key=(self.LONG_B, 'second'), owner='b', owner_label='乙')]
        metrics = [metric(campaign_id=self.LONG_A, campaign='second', revenue_cents=100),
                   metric(campaign_id=self.LONG_B, campaign='first', revenue_cents=200),
                   metric(campaign_id=self.PREFIX, campaign='first', revenue_cents=300)]
        report = self.result(catalog, metrics)
        self.assertEqual({r['owner_id']: r['revenue_cents'] for r in report['rows']}, {'a': 100, 'b': 200})
        self.assertEqual(report['unmatched']['revenue_cents'], 300)
        self.assert_conserved(report)

    def test_complete_32_character_id_colliding_with_long_alias_is_ambiguous(self):
        catalog = [link(key=(self.PREFIX, 'short')),
                   link(2, key=(self.LONG_A, 'long'), owner='b')]
        report = self.result(catalog, [metric(campaign_id=self.PREFIX, campaign='short')])
        self.assertEqual(report['totals']['revenue_cents'], 0)
        self.assertEqual(report['unmatched']['revenue_cents'], 1234)

    def test_same_id_conflicts_in_any_frozen_dimension_remain_unmatched(self):
        for changes in [dict(owner='b'), dict(tenant='tenant-b'), dict(channel='other'),
                        dict(drama='other'), dict(language='en'), dict(link_type='manual')]:
            with self.subTest(changes=changes):
                other = link(key=('task1', 'different-name'), link_id=2, **changes)
                report = self.result([link(), other], [metric()])
                self.assertEqual(report['totals']['revenue_cents'], 0)
                self.assertEqual(report['unmatched']['revenue_cents'], 1234)
                self.assert_conserved(report)

    def test_missing_owner_or_tenant_never_claims_money(self):
        for changes in [dict(owner=''), dict(tenant='')]:
            with self.subTest(changes=changes):
                report = self.result([link(**changes)], [metric()])
                self.assertEqual(report['totals']['revenue_cents'], 0)
                self.assertEqual(report['unmatched']['revenue_cents'], 1234)
                self.assert_conserved(report)

    def test_same_owner_id_in_two_tenants_has_separate_money_and_unresolved_post_tenant(self):
        catalog = [link(), link(2, tenant='tenant-b', owner_label='另一个租户的甲')]
        report = self.result(catalog, [metric(revenue_cents=100), metric(2,revenue_cents=200)],
                             [publication()])
        people = {(r['tenant'], r['owner_id']): r for r in report['rows']}
        self.assertEqual(people[('tenant-a','a')]['revenue_cents'], 100)
        self.assertEqual(people[('tenant-b','a')]['revenue_cents'], 200)
        self.assertEqual(people[('', 'a')]['published'], 1)
        self.assertEqual(people[('', 'a')]['revenue_cents'], 0)
        self.assertEqual(report['totals']['published'], 1)
        self.assert_conserved(report)

    def test_all_canary_candidates_excluded_but_mixed_real_and_canary_stay_unmatched(self):
        for mixed in (False, True):
            with self.subTest(mixed=mixed):
                catalog = [link(excluded=True)]
                if mixed:
                    catalog.append(link(key=('task1','real-name'), link_id=2, excluded=False))
                report = self.result(catalog, [metric(campaign='source-name')])
                self.assertEqual(report['totals']['revenue_cents'], 0)
                self.assertEqual(report['excluded_canary']['revenue_cents'], 0 if mixed else 1234)
                self.assertEqual(report['unmatched']['revenue_cents'], 1234 if mixed else 0)
                self.assert_conserved(report)

    def test_duplicate_frozen_records_do_not_multiply_source_money(self):
        catalog = [link(), link(), link(key=('task1','second-name'), link_id=2)]
        report = self.result(catalog, [metric()])
        self.assertEqual(report['totals']['revenue_cents'], 1234)
        self.assertEqual(report['matched_campaigns'], 1)
        self.assert_conserved(report)

    def test_wrong_raw_identity_fields_cannot_override_frozen_owner(self):
        report = self.result([link(link_type='manual')], [metric(channel='b', owner_id='b', tenant='other')])
        self.assertEqual(report['rows'][0]['owner_id'], 'a')
        self.assertEqual(report['rows'][0]['tenant'], 'tenant-a')
        self.assertEqual(report['matched_metrics'][0]['owner_id'], 'a')
        self.assertEqual(report['matched_metrics'][0]['tenant'], 'tenant-a')

    def test_mixed_matched_unknown_conflicted_and_canary_money_conserves_every_metric(self):
        catalog = [link(), link(2, excluded=True),
                   link(3), link(4,key=('task3','different-name'),owner='b')]
        metrics = [metric(revenue_cents=100,refund_cents=-5),
                   metric(2,revenue_cents=200,refund_cents=7),
                   metric(3,revenue_cents=300,refund_cents=9),
                   metric(9,revenue_cents=-10,refund_cents=3)]
        report = self.result(catalog, metrics)
        self.assertEqual(report['source_totals']['revenue_cents'], 590)
        self.assertEqual(report['totals']['revenue_cents'], 100)
        self.assertEqual(report['excluded_canary']['revenue_cents'], 200)
        self.assertEqual(report['unmatched']['revenue_cents'], 290)
        self.assert_conserved(report)


class LedgerReadTests(unittest.TestCase):
    def test_actual_sqlite_manual_ledger_is_included_read_only_without_a_post(self):
        with tempfile.TemporaryDirectory(prefix='youtube-ledger-read-') as folder:
            path = Path(folder)/'jobs.sqlite3'
            with closing(sqlite3.connect(path)) as connection:
                with connection:
                    connection.executescript('''
                        CREATE TABLE drama_admin_user(user_id TEXT,tenant_key TEXT,name TEXT);
                        CREATE TABLE drama_material_short_link(id INTEGER PRIMARY KEY,job_id TEXT,
                          material_kind TEXT,content_id TEXT,long_url TEXT,created_at_utc TEXT,
                          published_at_utc TEXT,publish_state TEXT);
                        CREATE TABLE youtube_link_attribution(link_id INTEGER,context TEXT);
                        CREATE TABLE youtube_manual_short_link(link_id INTEGER,tenant TEXT,owner TEXT,context_json TEXT);
                        CREATE TABLE drama_youtube_publish(id INTEGER,job_id TEXT,source_kind TEXT,
                          preparation_id TEXT,operator_user_id TEXT,operator_name TEXT,video_id TEXT,
                          video_state TEXT,privacy_status TEXT,public_status TEXT,workflow TEXT,
                          video_published_at_utc TEXT,status TEXT,created_at_utc TEXT,channel_id TEXT,content_id TEXT);
                        CREATE TABLE youtube_auto_preparation(id TEXT,tenant TEXT,owner TEXT,body TEXT);
                        CREATE TABLE drama_material_job(job_id TEXT,drama_name TEXT,language TEXT);
                        INSERT INTO drama_admin_user VALUES('a','tenant-a','冻结生成人');
                    ''')
                    url = 'https://www.dramawavew2a.com/ads/101/2284/view?' + urlencode(
                        dict(af_c_id='synthetic-manual-campaign',c='frozen-manual-name',af_channel='wrong-raw-owner'))
                    connection.execute('INSERT INTO drama_material_short_link VALUES(?,?,?,?,?,?,?,?)',
                        (1,'manual-job','youtube_manual','drama-a',url,'2026-09-18T00:00:00Z',
                         '2026-09-18T00:00:01Z','published'))
                    connection.execute('INSERT INTO youtube_manual_short_link VALUES(?,?,?,?)',
                        (1,'tenant-a','a',json.dumps(dict(content_id='drama-a',drama_name='冻结剧名',
                                                        channel_id='channel-a',channel_name='冻结频道',language='en'))))
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch('subprocess.run',side_effect=AssertionError('No external platform calls')):
                publications,catalog = load_ledger(path)
                report = summarize(publications,catalog,[metric(campaign_id='synthetic-manual-campaign',
                    campaign='wrong-source-name')],'2026-09-19','UTC')
            self.assertEqual(publications,[])
            self.assertEqual(len(catalog),1)
            self.assertEqual(catalog[0]['link_type'],'manual')
            self.assertEqual(report['totals']['published'],0)
            self.assertEqual(report['rows'][0]['name'],'冻结生成人')
            self.assertEqual(report['totals']['revenue_cents'],1234)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()),['jobs.sqlite3'])


if __name__=='__main__':
    unittest.main()
