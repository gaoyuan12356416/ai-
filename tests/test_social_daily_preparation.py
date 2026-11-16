import contextlib
import io
import json
import tempfile
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

    def test_sender_remains_closed_before_eleven(self):
        self.prepare()
        FixedClock.value -= timedelta(seconds=1)
        with patch.object(cli, 'send_card') as send:
            with self.assertRaisesRegex(RuntimeError, 'cutoff_not_reached'):
                self.run_cli('--send')
            send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
