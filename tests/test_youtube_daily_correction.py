"""Synthetic correction delivery tests; all Feishu and OS locks are mocked."""
from contextlib import closing
import copy
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from features.post_daily_report.delivery import DeliveryStore, DeliveryUnknown, DefiniteFailure
from features.youtube_daily_report.collector import BEIJING, summarize
from scripts import youtube_publisher_daily_report as runner


DAY = '2026-09-19'
EDITION = 'campaign-id-v2'


class FrozenNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 1, 12, 0, tzinfo=BEIJING).astimezone(tz)


def synthetic_report(available=True):
    catalog = [dict(key=('synthetic-campaign', 'frozen-name'), link_id=1, tenant='tenant-a',
        owner='owner-a', owner_label='Synthetic Owner', drama='drama-a', drama_label='Synthetic Drama',
        channel='channel-a', channel_label='Synthetic Channel', language='en', language_label='en',
        link_type='manual', link_type_label='手动短链', date=DAY, excluded=False)]
    metrics = [dict(campaign_id='synthetic-campaign', campaign='misnamed',
        revenue_cents=2500, refund_cents=0, installs=3, views=30, clicks=20,
        recharge=1, raw_impressions=0, source_rows=1, updated_at_utc=DAY+' 23:00:00')]
    return summarize([], catalog, metrics if available else [], DAY, 'UTC')


class CorrectionDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='youtube-correction-test-')
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.edition = self.state / 'corrections' / EDITION
        old = DeliveryStore(self.state/'delivery.sqlite3', namespace='youtube-publisher-daily-report')
        try:
            self.old_uuid = old.claim(DAY, runner.CHAT_ID)
            old.finish(DAY, runner.CHAT_ID, 'sent', 'synthetic-original-receipt')
        finally:
            old.close()
        for kind in ('reports', 'previews'):
            folder = self.state/kind/DAY
            folder.mkdir(parents=True)
            for name in ('report.json','card.json','preview.html','receipt.json'):
                (folder/name).write_text('original-'+kind+'-'+name, encoding='utf-8')
        self.original = {p: p.read_bytes() for p in self.state.rglob('*') if p.is_file()}
        self.report = synthetic_report()
        self.fcntl = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=Mock())
        patches = [patch.object(runner, 'datetime', FrozenNow),
                   patch.object(runner, 'os', SimpleNamespace(name='posix', umask=Mock())),
                   patch.dict('sys.modules', {'fcntl': self.fcntl}),
                   patch.object(runner, 'collect', side_effect=lambda *args: copy.deepcopy(self.report)),
                   patch.object(runner, 'send_card', return_value=dict(code=0,message_id='synthetic-correction-receipt')),
                   patch.object(runner.time, 'sleep'),
                   patch('sys.stdout', new_callable=io.StringIO),
                   patch('sys.stderr', new_callable=io.StringIO)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def invoke(self, correction=True, mode='--send', day=DAY):
        args = ['--state-dir', str(self.state), '--date', day, mode]
        if correction:
            args += ['--correction', EDITION]
        return runner.main(args)

    def assert_original_unchanged(self):
        for path, expected in self.original.items():
            self.assertEqual(path.read_bytes(), expected, str(path))

    def delivery(self, day=DAY):
        path = self.edition/'delivery.sqlite3'
        if not path.exists():
            return None
        with closing(sqlite3.connect(path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute('SELECT * FROM delivery WHERE report_date=? AND chat_id=?',
                                     (day,runner.CHAT_ID)).fetchone()
            return dict(row) if row else None

    def test_correction_requires_explicit_date_before_any_collection_or_send(self):
        with self.assertRaises(SystemExit) as error:
            runner.main(['--correction',EDITION,'--state-dir',str(self.state),'--send'])
        self.assertEqual(error.exception.code,2)
        runner.collect.assert_not_called()
        runner.send_card.assert_not_called()
        self.assertFalse(self.edition.exists())
        self.assert_original_unchanged()

    def test_correction_label_is_fixed_not_arbitrary_path_or_new_namespace(self):
        for edition in ('other-version','../escape'):
            with self.subTest(edition=edition), self.assertRaises(SystemExit):
                runner.main(['--correction',edition,'--date',DAY,'--state-dir',str(self.state),'--send'])
        runner.send_card.assert_not_called()
        self.assert_original_unchanged()

    def test_correction_has_separate_archive_ledger_uuid_and_shared_lock(self):
        self.assertEqual(self.invoke(),0)
        row = self.delivery()
        self.assertEqual(row['status'],'sent')
        self.assertEqual(row['message_id'],'synthetic-correction-receipt')
        self.assertNotEqual(row['request_uuid'],self.old_uuid)
        self.assertEqual(runner.send_card.call_args.args[3],row['request_uuid'])
        report_path = self.edition/'reports'/DAY/'report.json'
        self.assertEqual(json.loads(report_path.read_text(encoding='utf-8'))['correction'],EDITION)
        self.assertTrue((self.edition/'reports'/DAY/'receipt.json').exists())
        self.assertEqual(Path(self.fcntl.flock.call_args.args[0].name),self.state/'run.lock')
        self.assertFalse((self.edition/'run.lock').exists())
        card = runner.send_card.call_args.args[2]
        self.assertIn('更正版',card['header']['title']['content'])
        self.assertIn('唯一 campaign ID 关联后台发布及手动短链记录',json.dumps(card,ensure_ascii=False))
        self.assert_original_unchanged()

    def test_sent_correction_is_idempotent_and_does_not_refresh_sent_archive(self):
        self.invoke()
        snapshots = {p:p.read_bytes() for p in (self.edition/'reports'/DAY).iterdir()}
        runner.collect.reset_mock()
        runner.send_card.reset_mock()
        self.assertEqual(self.invoke(),0)
        runner.collect.assert_not_called()
        runner.send_card.assert_not_called()
        for path, value in snapshots.items():
            self.assertEqual(path.read_bytes(),value)
        self.assertEqual(self.delivery()['attempts'],1)
        self.assert_original_unchanged()

    def test_unknown_send_is_recorded_and_blocks_retry_before_collection(self):
        runner.send_card.side_effect = TimeoutError('synthetic timeout')
        with self.assertRaises(DeliveryUnknown):
            self.invoke()
        self.assertEqual(self.delivery()['status'],'unknown')
        self.assertEqual(runner.send_card.call_count,1)
        runner.send_card.reset_mock()
        runner.collect.reset_mock()
        with self.assertRaises(DeliveryUnknown):
            self.invoke()
        runner.send_card.assert_not_called()
        runner.collect.assert_not_called()
        self.assertEqual(self.delivery()['attempts'],1)
        self.assert_original_unchanged()

    def test_in_progress_correction_blocks_retry(self):
        self.edition.mkdir(parents=True)
        store = DeliveryStore(self.edition/'delivery.sqlite3',namespace='synthetic-correction')
        try:
            store.claim(DAY,runner.CHAT_ID)
        finally:
            store.close()
        with self.assertRaises(DeliveryUnknown):
            self.invoke()
        runner.collect.assert_not_called()
        runner.send_card.assert_not_called()
        self.assert_original_unchanged()

    def test_missing_metrics_cannot_consume_correction_delivery_or_send(self):
        self.report = synthetic_report(False)
        with self.assertRaisesRegex(RuntimeError,'correction_requires_available_metrics'):
            self.invoke()
        runner.send_card.assert_not_called()
        self.assertIsNone(self.delivery())
        self.assertFalse((self.edition/'reports'/DAY).exists())
        self.assert_original_unchanged()
        # Recovery is sendable because the missing-data attempt never claimed it.
        self.report = synthetic_report(True)
        self.assertEqual(self.invoke(),0)
        self.assertEqual(self.delivery()['attempts'],1)
        self.assert_original_unchanged()

    def test_preview_is_separate_and_does_not_claim_delivery(self):
        self.assertEqual(self.invoke(mode='--preview'),0)
        self.assertTrue((self.edition/'previews'/DAY/'preview.html').exists())
        self.assertFalse((self.edition/'delivery.sqlite3').exists())
        runner.send_card.assert_not_called()
        self.assert_original_unchanged()

    def test_unavailable_preview_is_explicitly_unknown_and_still_does_not_send(self):
        self.report = synthetic_report(False)
        self.assertEqual(self.invoke(mode='--preview'),0)
        report = json.loads((self.edition/'previews'/DAY/'report.json').read_text(encoding='utf-8'))
        self.assertFalse(report['metrics_available'])
        self.assertIsNone(report['totals']['revenue_cents'])
        runner.send_card.assert_not_called()
        self.assert_original_unchanged()

    def test_ordinary_sent_report_keeps_its_existing_idempotency(self):
        self.assertEqual(self.invoke(correction=False),0)
        runner.collect.assert_not_called()
        runner.send_card.assert_not_called()
        self.assertFalse(self.edition.exists())
        self.assert_original_unchanged()

    def test_ordinary_unsent_report_still_can_notify_unknown_source_data(self):
        self.report = synthetic_report(False)
        self.report['report_date'] = '2026-09-18'
        self.assertEqual(self.invoke(correction=False,day='2026-09-18'),0)
        runner.send_card.assert_called_once()
        card = runner.send_card.call_args.args[2]
        self.assertIn('待核实',json.dumps(card,ensure_ascii=False))
        self.assertNotIn('更正版',card['header']['title']['content'])

    def test_busy_shared_lock_prevents_correction_collection_or_send(self):
        self.fcntl.flock.side_effect = BlockingIOError('synthetic busy lock')
        self.assertEqual(self.invoke(),0)
        runner.collect.assert_not_called()
        runner.send_card.assert_not_called()
        self.assertIsNone(self.delivery())
        self.assert_original_unchanged()

    def test_definite_failure_retries_use_same_uuid_then_stop_at_three(self):
        runner.send_card.side_effect = DefiniteFailure('synthetic rejection')
        with self.assertRaises(DefiniteFailure):
            self.invoke()
        self.assertEqual(runner.send_card.call_count,3)
        self.assertEqual(len({c.args[3] for c in runner.send_card.call_args_list}),1)
        self.assertEqual(self.delivery()['status'],'failed')
        self.assertEqual(self.delivery()['attempts'],3)
        runner.send_card.reset_mock()
        with self.assertRaises(DefiniteFailure):
            self.invoke()
        runner.send_card.assert_not_called()
        self.assert_original_unchanged()


if __name__ == '__main__':
    unittest.main()
