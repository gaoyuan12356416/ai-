import contextlib
import io
import json
import tempfile
import runpy
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from features.social_daily_report import prepared
from features.social_daily_report.collector import BEIJING, present
from features.social_daily_report.delivery import SocialDeliveryStore
from scripts import social_publisher_daily_report as cli
from test_social_daily_report import fixture


class FixedClock(datetime):
    value = datetime(2026, 11, 16, 11, 0, 0, tzinfo=BEIJING)

    @classmethod
    def now(cls, tz=None):
        return cls.value


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.day = '2026-11-15'
        self.report = present(fixture(), self.day)
        self.clock = patch.object(cli, 'datetime', FixedClock)
        self.clock.start()
        FixedClock.value = datetime(2026, 11, 16, 11, 0, 0, tzinfo=BEIJING)
        counts = patch.object(cli, 'check_counts', return_value=({'2284': {'rows': 10}}, []))
        self.counts = counts.start()
        self.addCleanup(counts.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.clock.stop()
        self.temp.cleanup()

    def run_cli(self, mode):
        return cli.main(['--date', self.day, '--state-dir', str(self.root), mode])

    def prepare(self):
        with patch.object(cli, 'collect', return_value=self.report), patch.object(cli, 'send_card') as send:
            self.assertEqual(self.run_cli('--prepare'), 0)
            send.assert_not_called()

    def test_prepare_before_eleven_never_claims_or_sends(self):
        FixedClock.value = FixedClock.value.replace(hour=10, minute=58)
        self.prepare()
        self.assertFalse((self.root / 'delivery.sqlite3').exists())
        self.assertTrue(prepared.load(self.root, self.day, cli.CHAT_ID))

    def test_preparation_and_send_use_the_full_collection_budget(self):
        with patch.object(cli, 'collect', return_value=self.report) as collect:
            self.assertEqual(self.run_cli('--prepare'), 0)
            self.assertEqual(collect.call_args.kwargs, {'query_budget_seconds': 480})
        # Exercise fallback collection; no prepared snapshot is available here.
        (self.root / 'prepared' / self.day / 'report.json').unlink()
        with patch.object(cli, 'collect', return_value=self.report) as collect, \
                patch.object(cli, 'send_card', return_value={'message_id': 'fallback'}):
            self.assertEqual(self.run_cli('--send'), 0)
            self.assertEqual(collect.call_args.kwargs, {'query_budget_seconds': 480})

    def test_eleven_sender_uses_complete_preparation_without_database_reads(self):
        self.prepare()
        with patch.object(cli, 'collect', side_effect=AssertionError('no database reads at eleven')), \
                patch.object(cli, 'send_card', return_value={'message_id': 'confirmed'}) as send:
            self.assertEqual(self.run_cli('--send'), 0)
            self.assertEqual(self.run_cli('--send'), 0)
            send.assert_called_once()
            self.assertIn('数据采集完成', json.dumps(send.call_args.args[2], ensure_ascii=False))

    def test_failed_refresh_keeps_last_complete_snapshot(self):
        self.prepare()
        before = (self.root / 'prepared' / self.day / 'report.json').read_bytes()
        self.report['platforms']['youtube']['quality']['state'] = 'unavailable'
        with patch.object(cli, 'collect', return_value=self.report), self.assertRaisesRegex(ValueError, 'sources_incomplete'):
            self.run_cli('--prepare')
        self.assertEqual((self.root / 'prepared' / self.day / 'report.json').read_bytes(), before)
        self.assertFalse((self.root / 'delivery.sqlite3').exists())

    def test_source_exception_before_quality_check_retries_real_entrypoint(self):
        error = cli.ReportError('AI 后台记录的 campaign ID 用户归属冲突', 503)
        entry = Path(cli.__file__).with_name('youtube_publisher_daily_report.py')
        argv = [str(entry), '--date', self.day, '--state-dir', str(self.root), '--send']
        with patch.object(cli, 'collect', side_effect=error), patch.object(cli, 'send_card') as send, \
                patch.object(sys, 'argv', argv), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as caught:
            runpy.run_path(str(entry), run_name='__main__')
        self.assertEqual(caught.exception.code, 75)
        send.assert_not_called()
        failure = json.loads(next((self.root / 'failures' / self.day).glob('*.json')).read_text(encoding='utf-8'))
        self.assertTrue(failure['retryable'])
        self.assertEqual(failure['reason'], str(error))
        store = SocialDeliveryStore(self.root / 'delivery.sqlite3')
        self.assertIsNone(store.get(self.day, cli.CHAT_ID))
        self.assertIsNone(store.batch(self.day, cli.CHAT_ID))
        store.close()

    def test_collection_exception_keeps_prepared_snapshot_and_hides_unknown_text(self):
        self.prepare()
        before = (self.root / 'prepared' / self.day / 'report.json').read_bytes()
        with patch.object(cli, 'collect', side_effect=RuntimeError('private database string')), \
                self.assertRaises(RuntimeError):
            self.run_cli('--prepare')
        self.assertEqual((self.root / 'prepared' / self.day / 'report.json').read_bytes(), before)
        failure = next((self.root / 'failures' / self.day).glob('*.json')).read_text(encoding='utf-8')
        self.assertNotIn('private database string', failure)
        self.assertFalse(json.loads(failure)['retryable'])

    def test_wrong_group_or_date_cannot_be_used(self):
        self.prepare()
        for day, chat in [('2026-11-14', cli.CHAT_ID), (self.day, 'another-chat')]:
            if day != self.day:
                self.assertIsNone(prepared.load(self.root, day, chat))
            else:
                with self.assertRaisesRegex(ValueError, 'identity_mismatch'):
                    prepared.load(self.root, day, chat)

    def test_missing_or_corrupt_preparation_falls_back_to_collection(self):
        path = self.root / 'prepared' / self.day / 'report.json'
        path.parent.mkdir(parents=True)
        path.write_text('{corrupt', encoding='utf-8')
        with patch.object(cli, 'collect', return_value=self.report) as collect, \
                patch.object(cli, 'send_card', return_value={'message_id': 'fallback'}):
            self.assertEqual(self.run_cli('--send'), 0)
            collect.assert_called_once()

    def test_preparation_does_not_wait_on_delivery_lock(self):
        try:
            import fcntl
        except ImportError:
            self.skipTest('POSIX lock validation runs on the Linux deployment')
        with (self.root / 'run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.prepare()

    def test_sender_does_not_wait_on_preparation_lock(self):
        try:
            import fcntl
        except ImportError:
            self.skipTest('POSIX lock validation runs on the Linux deployment')
        self.prepare()
        with (self.root / 'prepare.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(cli, 'collect', side_effect=AssertionError('no collect')), \
                    patch.object(cli, 'send_card', return_value={'message_id': 'on-time'}):
                self.assertEqual(self.run_cli('--send'), 0)

    def test_prepared_data_does_not_clear_unknown_delivery(self):
        self.prepare()
        store = SocialDeliveryStore(self.root / 'delivery.sqlite3')
        store.claim(self.day, cli.CHAT_ID)
        store.finish(self.day, cli.CHAT_ID, 'unknown', detail='requires readback')
        store.close()
        with patch.object(cli, 'send_card') as send:
            with self.assertRaises(cli.DeliveryUnknown):
                self.run_cli('--send')
            send.assert_not_called()

    def test_sender_remains_closed_before_ten(self):
        self.prepare()
        FixedClock.value = FixedClock.value.replace(hour=10) - timedelta(seconds=1)
        with patch.object(cli, 'send_card') as send:
            with self.assertRaisesRegex(RuntimeError, 'cutoff_not_reached'):
                self.run_cli('--send')
            send.assert_not_called()

    def poll(self):
        return cli.main(['--state-dir', str(self.root), '--poll'])

    def test_from_ten_poll_until_counts_are_ready_then_only_send_once(self):
        FixedClock.value = FixedClock.value.replace(hour=9, minute=55)
        with patch.object(cli, 'collect', return_value=self.report) as collect, \
                patch.object(cli, 'send_card', return_value={'message_id': 'confirmed'}) as send:
            self.poll()
            self.counts.assert_not_called()
            FixedClock.value = FixedClock.value.replace(hour=10, minute=0)
            self.counts.return_value = ({}, ['2250'])
            for tick in range(9):
                self.poll()
                FixedClock.value += timedelta(minutes=5)
            collect.assert_not_called()
            send.assert_not_called()
            self.counts.return_value = ({'2250': {'clicks': 0}}, [])
            self.poll()
            self.poll()
            collect.assert_called_once()
            send.assert_called_once()
            self.assertEqual(self.counts.call_count, 10)

    def test_updated_counts_invalidate_prepared_snapshot(self):
        self.prepare()
        self.counts.return_value = ({'2284': {'rows': 11}}, [])
        with patch.object(cli, 'collect', return_value=self.report) as collect, \
                patch.object(cli, 'send_card', return_value={'message_id': 'fresh'}):
            self.poll()
            collect.assert_called_once()

    def test_midnight_carries_pending_day_and_unknown_never_resends(self):
        self.counts.return_value = ({}, ['2250'])
        self.poll()
        FixedClock.value += timedelta(hours=15)
        self.counts.return_value = ({'2250': {'rows': 1}}, [])
        with patch.object(cli, 'collect', return_value=self.report) as collect, \
                patch.object(cli, 'send_card', side_effect=cli.DeliveryUnknown('missing receipt')) as send:
            self.assertEqual(self.poll(), 1)
            self.assertEqual(collect.call_args.args[0], self.day)
            self.poll()
            send.assert_called_once()
            with contextlib.closing(SocialDeliveryStore(self.root / 'delivery.sqlite3')) as store:
                self.assertEqual(store.get(self.day, cli.CHAT_ID)['status'], 'unknown')
                self.assertEqual(store.due_days(FixedClock.value.replace(hour=10), cli.CHAT_ID), ['2026-11-16'])


class CountSourceTests(unittest.TestCase):
    def test_zero_partial_stale_and_missing_site_rows(self):
        from features.social_daily_report import readiness
        with contextlib.closing(sqlite3.connect(':memory:')) as db:
            db.execute("ATTACH ':memory:' AS kunlunads_dev")
            db.execute("""CREATE TABLE kunlunads_dev.ads_facebook_page_insight
                (dt TEXT, site_id TEXT, updated_at TEXT, clicks INTEGER, views INTEGER, installs INTEGER)""")
            db.execute("CREATE INDEX kunlunads_dev.sd ON ads_facebook_page_insight(site_id,dt)")
            day = '2026-11-25'
            fresh = '2026-11-26 00:15:00'
            stale = '2026-11-25 23:59:59'
            rows = [(day, site, fresh, 0, 0, 0) for site in readiness.SITES if site != '2250']
            rows += [(day, '2049', stale, 4, 5, 1), (day, '2116', None, 3, 1, 0),
                     ('2026-11-24', '2250', fresh, 1, 2, 0)]
            db.executemany("INSERT INTO kunlunads_dev.ads_facebook_page_insight VALUES(?,?,?,?,?,?)", rows)

            def query(settings, sql, limit):
                sql = sql.replace('FORCE INDEX(sd)', 'INDEXED BY sd')
                return [json.loads(bytes.fromhex(row[0])) for row in db.execute(sql)]

            with patch.object(readiness, 'read_settings', return_value={}), patch.object(readiness, 'query_rows', query):
                evidence, waiting = readiness.check_counts(day, 'fixture')
                self.assertEqual(waiting, ['2049', '2116', '2250'])
                self.assertEqual(evidence['2284']['clicks'], 0)
                db.execute("UPDATE kunlunads_dev.ads_facebook_page_insight SET updated_at=?", (fresh,))
                db.execute("INSERT INTO kunlunads_dev.ads_facebook_page_insight VALUES(?,?,?,?,?,?)",
                           (day, '2250', fresh, 0, 0, 0))
                self.assertEqual(readiness.check_counts(day, 'fixture')[1], [])


if __name__ == '__main__':
    unittest.main()
