import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from features.socialkit_analytics.hourly import notify, run_hourly, scheduled_range
from features.socialkit_analytics.refresh import run_refresh
from features.socialkit_analytics.storage import atomic_json, read_manifest
from tests.test_socialkit_analytics_data import FakeWriter, dataset


def moment(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class ScheduleTests(unittest.TestCase):
    def test_hourly_window_uses_utc_and_includes_today(self):
        result = scheduled_range(moment('2026-11-03T01:00:00Z'),
                                 {'last_full_refresh_beijing_date': '2026-11-02'})
        self.assertEqual(result, ('2026-11-01', '2026-11-03', None))

    def test_eleven_beijing_and_delayed_catchup_include_fourteen_complete_days(self):
        previous = {'last_full_refresh_beijing_date': '2026-11-02'}
        for now in ('2026-11-03T03:00:00Z', '2026-11-03T15:00:00Z'):
            self.assertEqual(scheduled_range(moment(now), previous),
                             ('2026-10-20', '2026-11-03', '2026-11-03'))
        # Catch up after midnight Beijing, before today's 11:00.
        self.assertEqual(scheduled_range(moment('2026-11-03T16:00:00Z'), previous),
                         ('2026-10-20', '2026-11-03', '2026-11-03'))

    def test_reconciliation_marker_survives_restart_and_utc_rollover(self):
        current = {'last_full_refresh_beijing_date': '2026-11-03'}
        self.assertEqual(scheduled_range(moment('2026-11-03T04:00:00Z'), current),
                         ('2026-11-01', '2026-11-03', None))
        self.assertEqual(scheduled_range(moment('2026-11-03T16:00:00Z'), current),
                         ('2026-11-01', '2026-11-03', None))
        self.assertEqual(scheduled_range(moment('2026-11-04T00:00:00Z'), current),
                         ('2026-11-02', '2026-11-04', None))
        self.assertEqual(scheduled_range(moment('2026-11-04T03:00:00Z'), current)[2], '2026-11-04')

    def test_new_install_reconciles_most_recent_due_day(self):
        self.assertEqual(scheduled_range(moment('2026-11-03T02:00:00Z')),
                         ('2026-10-20', '2026-11-03', '2026-11-02'))


class RefreshMetadataTests(unittest.TestCase):
    def refresh(self, root, marker=None, verify=lambda *args: None):
        return run_refresh({}, root, '2026-11-03', '2026-11-03', reader=object(),
                           source=object(), writer=FakeWriter(),
                           collect=lambda *args: iter([dataset('2026-11-03')]),
                           verify=verify, full_refresh_beijing_date=marker)

    def test_marker_only_publishes_on_success_and_survives_small_manual_refresh(self):
        with tempfile.TemporaryDirectory() as root:
            first = self.refresh(root, '2026-11-02')
            def failed(*args):
                raise RuntimeError('source failure')
            with self.assertRaises(RuntimeError):
                self.refresh(root, '2026-11-03', failed)
            self.assertEqual(read_manifest(root)['version'], first['version'])
            self.assertEqual(read_manifest(root)['last_full_refresh_beijing_date'], '2026-11-02')
            self.assertEqual(self.refresh(root)['last_full_refresh_beijing_date'], '2026-11-02')
            self.assertEqual(self.refresh(root, '2026-11-03')['last_full_refresh_beijing_date'], '2026-11-03')

    def test_day_freshness_is_collection_time_not_batch_start(self):
        with tempfile.TemporaryDirectory() as root:
            with patch('features.socialkit_analytics.refresh.utc_now', side_effect=[
                    '2026-11-03T03:00:00+00:00', '2026-11-03T03:10:00+00:00',
                    '2026-11-03T03:11:00+00:00']):
                result = self.refresh(root)
            day = result['date_manifest']['2026-11-03']
            self.assertEqual(day['refreshed_at'], '2026-11-03T03:10:00+00:00')
            self.assertEqual(day['collected_at'], day['refreshed_at'])
            self.assertEqual(result['generated_at'], '2026-11-03T03:11:00+00:00')


class CoordinatorTests(unittest.TestCase):
    def test_alert_uses_exact_published_version_and_failure_does_not_unpublish(self):
        with tempfile.TemporaryDirectory() as root:
            def refresh(env, target, start, end, **kwargs):
                manifest = {'version': 'published-version', 'last_full_refresh_beijing_date': kwargs['full_refresh_beijing_date']}
                atomic_json(Path(target) / 'current.json', manifest)
                return manifest
            notification = Mock(side_effect=subprocess.TimeoutExpired('alert', 600))
            result = run_hourly({}, root, 'alerts', now=moment('2026-11-03T03:00:00Z'),
                                refresh=refresh, notification=notification)
            notification.assert_called_once_with({}, root, 'alerts', 'published-version')
            self.assertEqual(read_manifest(root)['version'], 'published-version')
            self.assertEqual(result['refresh_status'], 'complete')
            self.assertEqual(result['alert_status'], 'failed')
            self.assertEqual(result['alert_error_code'], 'TimeoutExpired')
            self.assertEqual(json.loads((Path(root) / 'hourly-status.json').read_text())['version'], 'published-version')

    def test_failed_refresh_never_calls_alert(self):
        with tempfile.TemporaryDirectory() as root:
            notification = Mock()
            with self.assertRaises(RuntimeError):
                run_hourly({}, root, now=moment('2026-11-03T03:00:00Z'),
                           refresh=Mock(side_effect=RuntimeError('failed')), notification=notification)
            notification.assert_not_called()

    def test_notifier_command_is_bounded_version_pinned_and_output_suppressed(self):
        completed = subprocess.CompletedProcess([], 2, stdout='private payload', stderr='private error')
        with patch('features.socialkit_analytics.hourly.subprocess.run', return_value=completed) as run:
            result = notify({'EXAMPLE': 'private'}, 'report-root', 'alert-root', 'the-version')
        args, kwargs = run.call_args
        self.assertIn('--send', args[0])
        self.assertEqual(args[0][-6:], ['--version', 'the-version', '--socialkit-root', 'report-root', '--root', 'alert-root'])
        self.assertEqual(kwargs['timeout'], 600)
        self.assertEqual(result, {'status': 'failed', 'returncode': 2})
        self.assertNotIn('private', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
