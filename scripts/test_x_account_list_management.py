"""Account removal and billing regressions using isolated SQLite and fake X."""
import concurrent.futures
import contextlib
import json
import io
import sqlite3
import threading
import unittest
from unittest import mock

from scripts.test_x_accounts import XAccountsTestCase
from features.x_accounts import oauth_service as service, credits, client


class AccountManagementTests(unittest.TestCase):
    setUp = XAccountsTestCase.setUp
    tearDown = XAccountsTestCase.tearDown
    complete = XAccountsTestCase.complete
    new_state = XAccountsTestCase.new_state

    def test_remove_is_idempotent_hides_account_and_preserves_history(self):
        item = self.complete()
        token_before = service.token_path(item['x_user_id']).read_bytes()
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.execute('CREATE TABLE test_history(account_id INTEGER, post_id TEXT)')
            conn.execute('INSERT INTO test_history VALUES(?,?)', (item['id'], 'historical-post'))
            conn.commit()
        service.delete_account(item['id'], self.admin)
        service.delete_account(item['id'], self.admin)
        self.assertEqual(service.list_accounts(self.admin, 'all')['total'], 0)
        self.assertEqual(service.list_accounts(self.owner, 'mine')['total'], 0)
        deleted = service.find_account(item['id'])
        self.assertEqual(deleted['status'], 'disabled')
        self.assertFalse(deleted['publish_approved'])
        self.assertFalse(deleted['publish_eligible'])
        self.assertTrue(deleted['deleted_at'])
        self.assertEqual(service.token_path(item['x_user_id']).read_bytes(), token_before)
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            self.assertEqual(conn.execute('SELECT post_id FROM test_history').fetchone()[0], 'historical-post')
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM x_oauth_event WHERE event_type='account_deleted'").fetchone()[0], 1)

    def test_only_admin_can_remove_even_with_platform_permission(self):
        item = self.complete()
        for actor in (self.owner, dict(self.other_owner, x_platform_access=True)):
            with self.assertRaises(service.ServiceError) as caught:
                service.delete_account(item['id'], actor)
            self.assertEqual(caught.exception.status, 403)
        self.assertTrue(service.find_account(item['id'])['publish_eligible'])

    def test_upload_lock_blocks_remove_without_disabling_account(self):
        item = self.complete()
        held, release = threading.Event(), threading.Event()
        def upload():
            with service.account_lock('x:' + item['x_user_id']):
                held.set()
                release.wait(5)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(upload)
            self.assertTrue(held.wait(2))
            try:
                with self.assertRaises(service.ServiceError) as caught:
                    service.delete_account(item['id'], self.admin)
                self.assertEqual(caught.exception.code, 'x_account_busy')
                self.assertTrue(service.find_account(item['id'])['publish_eligible'])
            finally:
                release.set()
                future.result()

    def test_removed_account_cannot_be_enabled_verified_or_published(self):
        item = self.complete()
        service.delete_account(item['id'], self.admin)
        with mock.patch.object(service, 'user_request') as upstream:
            with self.assertRaises(service.ServiceError):
                service.set_account_publish_approval(item['id'], True, self.admin)
            with self.assertRaises(service.ServiceError):
                service.verify_account(item['id'], self.admin, 'all')
            with self.assertRaises(service.ServiceError):
                with service.publish_credentials(item['id'], self.admin, 'all'):
                    self.fail('deleted account reached publisher')
            upstream.assert_not_called()

    def test_reauthorization_restores_same_identity_without_allowing_publication(self):
        item = self.complete()
        service.delete_account(item['id'], self.admin)
        restored = self.complete(publish_approved=False)
        self.assertEqual(restored['id'], item['id'])
        self.assertEqual(restored['deleted_at'], '')
        self.assertEqual(restored['status'], 'active')
        self.assertFalse(restored['publish_approved'])
        self.assertEqual(service.list_accounts(self.admin, 'all')['total'], 1)

    def test_publisher_catalog_retains_disabled_tombstone(self):
        item = self.complete()
        service.delete_account(item['id'], self.admin)
        catalog = service.auto_template_accounts_request({})['items']
        self.assertEqual(catalog[0]['id'], item['id'])
        self.assertFalse(catalog[0]['publish_eligible'])
        # A saved schedule sees an unavailable account, not an unknown identity.
        catalog = service.post_schedule_account_options_request(
            {'actor': self.admin}, service.POST_MATERIAL_POOL_NAVIGATION_ITEM)['items']
        self.assertEqual(catalog[0]['status'], 'disabled')

    def test_additive_migration_preserves_existing_account(self):
        item = self.complete()
        service.ensure_storage()
        service.ensure_storage()
        self.assertEqual(service.find_account(item['id'])['x_user_id'], item['x_user_id'])

    def test_audit_failure_rolls_back_removal(self):
        item = self.complete()
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.execute("CREATE TRIGGER fail_delete_audit BEFORE INSERT ON x_oauth_event WHEN NEW.event_type='account_deleted' BEGIN SELECT RAISE(ABORT,'audit unavailable'); END")
            conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            service.delete_account(item['id'], self.admin)
        self.assertTrue(service.find_account(item['id'])['publish_eligible'])

    def test_node_upload_and_unknown_receipt_block_removal_without_python_lock(self):
        item = self.complete()
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.executescript('CREATE TABLE x_post_queue(id INTEGER,account_id INTEGER,relay_account_id INTEGER,status TEXT);'
                               'CREATE TABLE x_post_publish_log(queue_id INTEGER,status TEXT,unknown_outcome INTEGER);')
            conn.execute('INSERT INTO x_post_queue VALUES(1,?,0,?)', (item['id'], 'publishing'))
            conn.execute("INSERT INTO x_post_publish_log VALUES(1,'media_uploading',0)")
            conn.commit()
        with self.assertRaises(service.ServiceError) as caught:
            service.delete_account(item['id'], self.admin)
        self.assertEqual(caught.exception.code, 'x_account_busy')
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.execute("UPDATE x_post_queue SET status='failed'")
            conn.execute("UPDATE x_post_publish_log SET status='failed',unknown_outcome=1")
            conn.commit()
        with self.assertRaises(service.ServiceError):
            service.delete_account(item['id'], self.admin)
        self.assertTrue(service.find_account(item['id'])['publish_eligible'])

    def test_unattempted_queue_does_not_prevent_removal(self):
        item = self.complete()
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.executescript('CREATE TABLE x_post_queue(id INTEGER,account_id INTEGER,relay_account_id INTEGER,status TEXT);'
                               'CREATE TABLE x_post_publish_log(queue_id INTEGER,status TEXT,unknown_outcome INTEGER);')
            conn.execute('INSERT INTO x_post_queue VALUES(1,?,0,?)', (item['id'], 'queued'))
            conn.execute("INSERT INTO x_post_publish_log VALUES(1,'reserved',0)")
            conn.commit()
        self.assertTrue(service.delete_account(item['id'], self.admin)['deleted'])


class CreditsTests(unittest.TestCase):
    def test_official_balance_parsing_accepts_negative_prepaid_and_rejects_partial_results(self):
        for data, expected in [
            ({'total_balance':1,'prepaid_balance':-3,'free_balance':4}, '1'),
            ({'total_balance':0,'prepaid_balance':-3,'free_balance':0}, '0'),
            ({'total_balance':True,'prepaid_balance':1,'free_balance':0}, None),
            ({'total_balance':9,'prepaid_balance':1,'free_balance':0}, None),
            ({'total_balance':'NaN','prepaid_balance':1,'free_balance':0}, None),
        ]:
            with self.subTest(data=data), mock.patch.object(credits.urllib.request, 'build_opener') as opener:
                opener.return_value.open.return_value = io.BytesIO(json.dumps({'data':data}).encode())
                result = credits.fetch_current_credits('secret', 'Lisa API')
                if expected is None:
                    self.assertEqual(result['status'], 'unavailable')
                    self.assertNotIn('total_balance', result)
                else:
                    self.assertEqual(result['total_balance'], expected)

    def test_live_balance_uses_current_billing_token_and_only_safe_fields(self):
        with mock.patch.object(credits, 'fetch_balance', return_value={
                'total_balance':'12.50','prepaid_balance':'10.00','free_balance':'2.50'}) as fetch:
            result = credits.fetch_current_credits('private-billing-token', 'Lisa API')
        fetch.assert_called_once_with('private-billing-token')
        self.assertEqual(result['status'], 'live')
        self.assertEqual(result['total_balance'], '12.50')
        self.assertIn('checked_at_utc', result)
        self.assertNotIn('private-billing-token', json.dumps(result))

    def test_failure_is_unknown_not_zero_and_never_exposes_upstream_body(self):
        with mock.patch.object(credits, 'fetch_balance', side_effect=RuntimeError('private-token-body')):
            result = credits.fetch_current_credits('private-token', 'Lisa API')
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('total_balance', result)
        self.assertNotIn('private-token', json.dumps(result))

    def test_missing_billing_configuration_does_not_rotate_user_credentials(self):
        with mock.patch.object(credits, 'fetch_balance') as fetch:
            result = credits.fetch_current_credits('', 'Lisa API')
        fetch.assert_not_called()
        self.assertEqual(result['status'], 'unavailable')

    def test_client_paths_and_actor(self):
        actor = {'role':'admin','user_id':'test','tenant_key':'test','x_platform_access':True}
        with mock.patch.object(client, '_request', return_value={}) as request:
            client.delete_x_account(12, actor)
            self.assertEqual(request.call_args.args[0], '/internal/accounts/12/delete')
            self.assertTrue(request.call_args.kwargs['payload']['actor']['x_platform_access'])
            client.query_x_api_credits(actor)
            self.assertEqual(request.call_args.args[0], '/internal/accounts/credits')


if __name__ == '__main__':
    unittest.main()
