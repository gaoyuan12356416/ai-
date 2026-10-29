import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import x_live_credit_monitor as monitor


def row(identity=1, status='active', approved=1, expiry='2026-10-19T20:00:00Z'):
    return {'id': identity, 'x_user_id': str(identity), 'status': status,
            'publish_approved': approved, 'access_expires_at': expiry}


class ProductionCreditsTest(unittest.TestCase):
    def test_only_active_approved_and_latest_token_selected(self):
        selected, refresh = monitor.select_account([
            row(1, expiry='2026-10-18T00:00:00Z'), row(2),
            row(3, status='disabled', expiry='2027-01-01T00:00:00Z'),
            row(4, approved=0, expiry='2027-01-01T00:00:00Z')], monitor.epoch('2026-10-19T03:00:00Z'))
        self.assertEqual(selected['id'], 2)
        self.assertFalse(refresh)

    def test_expired_token_requires_existing_service_refresh(self):
        self.assertTrue(monitor.select_account([row()], monitor.epoch('2026-10-19T20:00:00Z'))[1])
        with self.assertRaises(monitor.MonitorError):
            monitor.select_account([row(status='revoked')], 0)

    def test_test_app_or_changed_production_app_is_refused(self):
        with patch.object(monitor, 'live_environment', return_value=({'X_CLIENT_ID': 'new-test-app'}, 9)), \
             patch.object(monitor, 'account_rows') as accounts:
            with self.assertRaisesRegex(monitor.MonitorError, 'rebind_required'):
                monitor.production_balance({'expected_client_id_sha256': hashlib.sha256(b'production-app').hexdigest()})
            accounts.assert_not_called()

    def test_fresh_production_token_read_only_and_no_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / '1.json'
            token.write_text(json.dumps({'access_token': 'production-user-token'}))
            env = {'X_CLIENT_ID': 'production-app', 'X_TOKENS_DIR': '/var/lib/x-post-automation/tokens'}
            original_resolve = Path.resolve
            def resolve(path, *args, **kwargs):
                if str(path).replace('\\', '/') == '/var/lib/x-post-automation/tokens':
                    return Path(directory)
                return original_resolve(path, *args, **kwargs)
            with patch.object(monitor, 'live_environment', return_value=(env, 9)), \
                 patch.object(monitor, 'account_rows', return_value=[row(expiry='2099-01-01T00:00:00Z')]), \
                 patch.object(monitor, 'refresh_if_needed') as refresh, \
                 patch.object(Path, 'resolve', resolve), \
                 patch.object(monitor.common, 'fetch_balance', return_value={'total_balance': '85.09'}) as fetch:
                balance, evidence = monitor.production_balance({'expected_client_id_sha256': hashlib.sha256(b'production-app').hexdigest()})
                fetch.assert_called_once_with('production-user-token')
                refresh.assert_not_called()
                self.assertEqual(evidence['service'], 'x-post-automation.service')
                self.assertEqual(balance['total_balance'], '85.09')

    def test_failure_after_three_checks_only_once_and_no_fake_zero(self):
        state = {'last_balance': {'total_balance': '85.09'}}
        sender = Mock(return_value={'status': 'sent', 'message_id': 'test'})
        for _ in range(5):
            monitor.failure(state, 'network_error', persist=lambda s: None, sender=sender)
        sender.assert_called_once()
        self.assertEqual(state['last_balance']['total_balance'], '85.09')
        self.assertIn('当前余额未知', sender.call_args.args[0])

    def test_changed_app_alert_immediate(self):
        sender = Mock(return_value={'status': 'sent'})
        monitor.failure({}, 'production_app_changed_rebind_required', persist=lambda s: None, sender=sender)
        sender.assert_called_once()

    def test_unknown_failure_alert_not_retried(self):
        state = {'consecutive_failures': 3}
        sender = Mock(side_effect=TimeoutError())
        monitor.failure(state, 'error', persist=lambda s: None, sender=sender)
        monitor.failure(state, 'error', persist=lambda s: None, sender=sender)
        sender.assert_called_once()

    def test_production_message_identifies_correct_scope_and_server(self):
        result = monitor.live_message({'total_balance': '85.09', 'prepaid_balance': '85.09', 'free_balance': '0'}, True)
        self.assertIn('AI 后台当前运行', result)
        self.assertIn('不依赖电脑或 Codex', result)
        self.assertNotIn('新 App 所属', result)


if __name__ == '__main__':
    unittest.main()
