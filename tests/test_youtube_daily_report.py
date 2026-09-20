import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


def link(number=1, campaign='campaign'):
    return dict(job_id='task'+str(number),material_kind='custom_source',
        long_url='https://www.dramawavew2a.com/ads/101/2284/view?af_c_id=task'+str(number)+'&c='+campaign)


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

    def test_campaign_id_without_exact_frozen_name_not_assigned(self):
        report=self.result(metrics=[metric(campaign='external')])
        self.assertEqual(report['matched_campaigns'],0)
        self.assertEqual(report['unmatched']['revenue_cents'],1234)
        self.assertEqual(report['totals']['revenue_cents'],0)

    def test_shared_link_with_multiple_owners_not_arbitrarily_assigned(self):
        report=self.result([publication(),publication(2,owner='b',job_id='task1')])
        self.assertEqual(report['unmatched']['campaigns'],1)

    def test_canary_excluded_and_totals_reconcile(self):
        report=self.result([publication(name='internal-deployment-canary')])
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
            [link(),link(2)],[metric(),metric(2,revenue_cents=4)])
        self.assertEqual(report['totals']['revenue_cents'],1238)
        self.assertEqual([(r['owner_id'],r['revenue_cents']) for r in report['rows']],[('a',1234),('b',4)])

    def test_card_explains_timezones_and_unavailable_impressions(self):
        card=build_card(self.result())
        rendered=json.dumps(card,ensure_ascii=False)
        for text in ['北京时间自然日','UTC 自然日','YouTube 曝光：未接入','$12.34','全部历史内容']:
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


if __name__=='__main__':
    unittest.main()
