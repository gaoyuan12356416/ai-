"""Application switching must never use another app's user credentials."""
import json
import contextlib
import sys
import types
import unittest
from unittest import mock

import test_x_accounts as fixtures
from features.x_accounts import oauth_service as service
import x_app_credit_monitor as credits
import x_live_credit_monitor as monitor


class AppSwitchTest(unittest.TestCase):
    setUp = fixtures.XAccountsTestCase.setUp
    tearDown = fixtures.XAccountsTestCase.tearDown
    new_state = fixtures.XAccountsTestCase.new_state
    complete = fixtures.XAccountsTestCase.complete

    def test_switch_blocks_profile_refresh_and_publish_before_network(self):
        original = self.complete()
        token_before = service.token_path(original['x_user_id']).read_bytes()
        with mock.patch.object(service, 'CLIENT_ID', 'new-app'), \
             mock.patch.object(service, 'token_request') as token_request, \
             mock.patch.object(service, 'user_request') as user_request:
            item = service.find_account(original['id'])
            self.assertEqual(item['status'], 'reauth_required')
            self.assertEqual(item['app_authorization_status'], 'pending')
            self.assertFalse(item['publish_eligible'])
            self.assertFalse(item['authorization_refreshable'])
            for only_refresh in (False, True):
                with self.assertRaises(service.ServiceError):
                    service.verify_account(item['id'], self.owner, only_refresh_required=only_refresh)
            with self.assertRaises(service.ServiceError):
                with service.publish_credentials(item['id'], self.owner):
                    self.fail('cross-app token yielded')
            token_request.assert_not_called()
            user_request.assert_not_called()
        self.assertEqual(service.token_path(original['x_user_id']).read_bytes(), token_before)

    def test_reauthorization_keeps_identity_owner_language_approval(self):
        original = self.complete()
        service.set_account_drama_language(original['id'], 'ja', self.admin)
        with contextlib.closing(service.db_connect()) as conn:
            conn.execute("UPDATE x_authorized_account SET status='reauth_required' WHERE id=?", (original['id'],))
            conn.commit()
        with mock.patch.object(service, 'CLIENT_ID', 'new-app'):
            updated = self.complete(publish_approved=False)
            self.assertEqual(updated['id'], original['id'])
            self.assertEqual(updated['owner'], original['owner'])
            self.assertEqual(updated['drama_language'], 'ja')
            self.assertTrue(updated['publish_approved'])
            self.assertEqual(updated['status'], 'active')
            self.assertTrue(service.token_matches_current_app(service.read_token_file(original['x_user_id'])))

    def test_unbound_legacy_token_requires_reauth_when_enabled(self):
        item = self.complete()
        token = service.read_token_file(item['x_user_id'])
        token.pop('oauth_client_id_sha256')
        service.atomic_write_json(service.token_path(item['x_user_id']), token)
        with mock.patch.object(service, 'REQUIRE_APP_BOUND_TOKENS', True):
            self.assertEqual(service.find_account(item['id'])['status'], 'reauth_required')

    def test_disabled_account_stays_disabled_after_app_switch(self):
        item = self.complete()
        service.logout_account(item['id'], self.owner)
        with mock.patch.object(service, 'CLIENT_ID', 'new-app'):
            self.assertEqual(service.find_account(item['id'])['status'], 'disabled')

    def test_billing_without_authorized_users_and_production_pin(self):
        env = {'X_CLIENT_ID': 'app', 'X_BILLING_BEARER_TOKEN': 'billing-token'}
        import hashlib
        with mock.patch.object(monitor, 'live_environment', return_value=(env, 10)), \
             mock.patch.object(monitor, 'account_rows') as rows, \
             mock.patch.object(credits, 'fetch_balance', return_value={'total_balance': '109.97'}) as fetch:
            value, evidence = monitor.production_balance({'expected_client_id_sha256': hashlib.sha256(b'app').hexdigest()})
            rows.assert_not_called()
            fetch.assert_called_once_with('billing-token')
            self.assertEqual(value['total_balance'], '109.97')
            self.assertEqual(evidence['token_source'], 'production_app_billing')

    def test_ten_dollar_threshold_rearms_and_deduplicates(self):
        state = {}
        sender = mock.Mock(return_value={'status': 'sent', 'message_id': 'receipt'})
        def run(value):
            return credits.update(state, {'total_balance': value}, notify=True, setup=False,
                persist=lambda value: None, sender=sender, threshold='10', message_builder=lambda *_: 'low balance')
        self.assertEqual(run('10')['status'], 'healthy')
        run('9.99'); run('8')
        sender.assert_called_once()
        run('10'); run('9')
        self.assertEqual(sender.call_count, 2)

    def test_group_alert_contains_real_mention_and_receipt(self):
        transport = types.SimpleNamespace(BASE_URL='https://open.feishu.cn/open-apis',
            get_tenant_access_token=mock.Mock(return_value='tenant'),
            post_json=mock.Mock(return_value={'code': 0, 'data': {'message_id': 'receipt'}}))
        config = {'receive_id_type': 'chat_id', 'receive_id': 'oc_test', 'mention_open_id': 'ou_lisa',
                  'recipient_name': 'Lisa', 'app_id': 'app', 'app_secret': 'secret'}
        with mock.patch.dict(sys.modules, {'x_credit_feishu_transport': transport}):
            result = monitor.send_alert(config, 'Please recharge', 'dedup-id')
        self.assertEqual(result['message_id'], 'receipt')
        args = transport.post_json.call_args.args
        self.assertTrue(args[0].endswith('receive_id_type=chat_id'))
        self.assertEqual(args[1]['receive_id'], 'oc_test')
        self.assertIn('<at user_id="ou_lisa">Lisa</at>', json.loads(args[1]['content'])['text'])
        self.assertEqual(args[1]['uuid'], 'dedup-id')


if __name__ == '__main__':
    unittest.main()
