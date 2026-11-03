import copy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from features.post_daily_report.delivery import DefiniteFailure, DeliveryStore
from features.socialkit_gap_alert.digest import run_digest, render_html, summary_card, totals
from features.socialkit_gap_alert.cards import payload_size, MAX_BYTES
from features.socialkit_gap_alert.runner import CHAT_ID
from tests.test_socialkit_gap_alert import NOW, report, event


class DigestTests(unittest.TestCase):
    def test_large_report_is_one_small_card_and_html_keeps_every_row(self):
        data = report([event(str(i), ['facebook', 'youtube', 'x'][i % 3]) for i in range(1000)])
        data['events'][0]['drama_name'] = '<script>alert("x")</script>&测试'
        data['pending'] = [dict(platform='x', drama='unknown', reason='drama_attribution_unverified')]
        html = render_html(data)
        card = summary_card(data, 'https://example.com/report.html')
        self.assertEqual(html.count('data-platform='), 1000)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>alert', html)
        self.assertIn('unknown', html)
        self.assertLess(payload_size(card, CHAT_ID), MAX_BYTES)
        self.assertNotIn('测试剧', json.dumps(card, ensure_ascii=False))
        self.assertEqual(totals(data['events'])['socialkit'], 1000)

    def test_preview_never_publishes_or_claims(self):
        with tempfile.TemporaryDirectory() as root:
            publisher, sender = Mock(), Mock()
            result = run_digest(root=root, collector=lambda *a, **k: report(), clock=lambda: NOW,
                                publisher=publisher, sender=sender)
            self.assertEqual(result['status'], 'preview')
            self.assertTrue(Path(result['path']).exists())
            self.assertFalse((Path(root) / 'digest-delivery.sqlite3').exists())
            publisher.assert_not_called()
            sender.assert_not_called()

    def test_today_manual_send_counts_as_daily_send_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            publisher = Mock(return_value='https://example.com/a.html')
            sender = Mock(return_value={'message_id': 'm1'})
            args = dict(root=root, send=True, collector=lambda *a, **k: report(), clock=lambda: NOW,
                        publisher=publisher, sender=sender)
            self.assertEqual(run_digest(**args)['sent_cards'], 1)
            args['collector'] = Mock(side_effect=AssertionError('must not recollect'))
            self.assertEqual(run_digest(**args)['status'], 'already_sent')
            self.assertEqual(sender.call_count, 1)
            self.assertEqual(publisher.call_count, 1)

    def test_unknown_outcome_never_auto_resends(self):
        with tempfile.TemporaryDirectory() as root:
            sender = Mock(side_effect=TimeoutError())
            args = dict(root=root, send=True, collector=lambda *a, **k: report(), clock=lambda: NOW,
                        publisher=Mock(return_value='https://example.com/a.html'), sender=sender)
            with self.assertRaises(TimeoutError):
                run_digest(**args)
            self.assertEqual(run_digest(**args)['status'], 'held')
            self.assertEqual(sender.call_count, 1)

    def test_definite_failure_retries_identical_frozen_payload_and_uuid(self):
        with tempfile.TemporaryDirectory() as root:
            sender = Mock(side_effect=[DefiniteFailure('rejected'), {'message_id': 'm2'}])
            publisher = Mock(return_value='https://example.com/a.html')
            args = dict(root=root, send=True, collector=lambda *a, **k: report(), clock=lambda: NOW,
                        publisher=publisher, sender=sender)
            with self.assertRaises(DefiniteFailure):
                run_digest(**args)
            args['collector'] = Mock(side_effect=AssertionError('must retain frozen evidence'))
            self.assertEqual(run_digest(**args)['status'], 'sent')
            self.assertEqual(sender.call_args_list[0], sender.call_args_list[1])
            publisher.assert_called_once()

    def test_html_publish_failure_does_not_claim_or_send(self):
        with tempfile.TemporaryDirectory() as root:
            sender = Mock()
            with self.assertRaises(RuntimeError):
                run_digest(root=root, send=True, collector=lambda *a, **k: report(), clock=lambda: NOW,
                           publisher=Mock(side_effect=RuntimeError()), sender=sender)
            store = DeliveryStore(Path(root) / 'digest-delivery.sqlite3')
            self.assertIsNone(store.get(NOW.date().isoformat(), CHAT_ID))
            store.close()
            sender.assert_not_called()

    def test_source_snapshot_freshness_and_day_change_prevent_send(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'source.json'
            source.write_text(json.dumps(report()), encoding='utf-8')
            sender = Mock()
            with self.assertRaisesRegex(ValueError, 'stale'):
                run_digest(root=root, send=True, comparison_file=source, clock=lambda: NOW + timedelta(hours=3), sender=sender)
            sender.assert_not_called()
        with tempfile.TemporaryDirectory() as root:
            wall = [NOW]
            def publisher(*args):
                wall[0] += timedelta(days=1)
                return 'https://example.com/a.html'
            with self.assertRaisesRegex(ValueError, 'statistics_day_changed'):
                run_digest(root=root, send=True, collector=lambda *a, **k: report(), clock=lambda: wall[0],
                           publisher=publisher, sender=sender)
            sender.assert_not_called()

    def test_next_day_can_send_once_and_timer_has_no_catchup(self):
        with tempfile.TemporaryDirectory() as root:
            sender = Mock(return_value={'message_id': 'm'})
            for day in (NOW, NOW + timedelta(days=1)):
                result = run_digest(root=root, send=True, collector=lambda *a, **k: report(day=a[3]),
                                    clock=lambda: day, publisher=lambda *a: 'https://example.com/a.html', sender=sender)
                self.assertEqual(result['sent_cards'], 1)
            self.assertEqual(sender.call_count, 2)
        unit = (Path(__file__).resolve().parents[1] / 'ops/socialkit-analytics/socialkit-gap-digest.timer').read_text()
        self.assertIn('11:30:00 Asia/Shanghai', unit)
        self.assertIn('Persistent=false', unit)


if __name__ == '__main__':
    unittest.main()
