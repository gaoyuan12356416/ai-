"""Exercise the real service entrypoint without any HTTP or database access."""
import contextlib
import io
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

from features.post_daily_report.delivery import DefiniteFailure, DeliveryUnknown

ROOT = Path(__file__).resolve().parents[1]


class DailyRetryTests(unittest.TestCase):
    def exit_code(self, error=None):
        with patch('scripts.social_publisher_daily_report.main', side_effect=error, return_value=0), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            runpy.run_path(str(ROOT / 'scripts/youtube_publisher_daily_report.py'), run_name='__main__')
        return caught.exception.code

    def test_incomplete_data_can_retry_before_delivery(self):
        for reason in ('daily_report_sources_incomplete', 'daily_report_metrics_incomplete'):
            with self.subTest(reason=reason):
                self.assertEqual(self.exit_code(ValueError(reason)), 75)

    def test_delivery_unknown_and_definite_failure_do_not_auto_retry(self):
        for error in (DeliveryUnknown('prior_delivery_unconfirmed'), DefiniteFailure('feishu_code_1')):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self.exit_code(error), 2)

    def test_success_and_already_sent_exit_cleanly(self):
        self.assertEqual(self.exit_code(), 0)

    def test_unexpected_errors_are_not_retried_or_exposed(self):
        self.assertEqual(self.exit_code(ValueError('secret diagnostic')), 1)
        self.assertEqual(self.exit_code(RuntimeError('secret diagnostic')), 1)
