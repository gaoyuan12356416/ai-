"""Failure observations stay useful without retaining credentials or doing I/O."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import builtins

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.diagnostics import exception_failure_fields, failure_fields
from features.pipeline_dispatch.worker import ReceiptOutbox, execute_claim
from features.pipeline_dispatch.client import CoordinatorError
from features.x_posts.service import XPostError
from scripts.test_pipeline_worker import Client, JOB, KEY
from scripts import test_pipeline_coordinator as coordinator_fixture


class FailureDiagnostics(unittest.TestCase):
    def test_trusted_x_error_preserves_actual_http_not_sidecar_status(self):
        error = XPostError('x_upstream_error', 'X媒体初始化失败(HTTP 403): forbidden', 502)
        fields = exception_failure_fields(error)
        self.assertEqual(fields['upstream_http_status'], 403)
        self.assertIn('forbidden', fields['error_message'])
        self.assertNotIn('upstream_http_status', exception_failure_fields(XPostError('x_upstream_error', '响应结构无效', 502)))

    def test_worker_durable_failure_strips_urls_headers_and_query_credentials(self):
        message = ('X初始化失败(HTTP 403): forbidden Bearer bearer-credential '
                   'https://api.example/private?access_token=query-credential '
                   'client_secret=client-credential {"api_key":"api-credential"} '
                   'password=pass-credential token=plain-credential')
        class Executor:
            def execute(self, job, context):
                raise XPostError('x_upstream_error', message, 502)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'outbox.db'
            client = Client()
            result = execute_claim(JOB, client, ReceiptOutbox(path), KEY, Executor())
            self.assertEqual(result['upstream_http_status'], 403)
            self.assertFalse(result['unknown_outcome'])
            with closing(sqlite3.connect(path)) as connection:
                saved = connection.execute('SELECT payload FROM receipt_outbox').fetchone()[0]
            for value in ('bearer-credential', 'api.example', 'query-credential', 'client-credential', 'api-credential', 'pass-credential', 'plain-credential'):
                self.assertNotIn(value, saved)
            self.assertIn('forbidden', saved)
            self.assertEqual([route for route, _ in client.calls if route.startswith('jobs/') and route not in ('jobs/heartbeat',)], ['jobs/fail'])

    def test_arbitrary_exception_text_and_fake_named_class_are_not_serialized(self):
        class XPostError(RuntimeError):
            code = 'foreign_error'
            status = 403
        for error in (RuntimeError('password=secret-material'), XPostError('Bearer secret-material')):
            class Executor:
                def execute(self, job, context):
                    context.write_started = True
                    raise error
            with tempfile.TemporaryDirectory() as temp:
                result = execute_claim(JOB, Client(), ReceiptOutbox(Path(temp) / 'outbox'), KEY, Executor())
                self.assertTrue(result['unknown_outcome'])
                self.assertNotIn('error_message', result)
                self.assertNotIn('upstream_http_status', result)
                self.assertNotIn('secret-material', json.dumps(result))

    def test_authority_sanitizes_again_before_native_and_storage(self):
        case = coordinator_fixture.CoordinatorTests('runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.offer(); claim = case.claim()
        seen = []
        def native_fail(job, payload):
            seen.append(payload)
            return {'canonical_no_write': True}
        case.native.fail = native_fail
        request = {**claim, 'receipt_id': 'safe-failure-1', 'error_code': 'x_upstream_error',
                   'unknown_outcome': False, 'upstream_http_status': 502,
                   'error_message': 'X失败(HTTP 403): forbidden https://host.invalid/?token=private-query Bearer private-bearer "secret":"private-json"'}
        response = case.service.route('/internal/pipeline/jobs/fail', request, case.auth)
        self.assertEqual(response['status'], 'failed')
        self.assertEqual(seen[0]['upstream_http_status'], 403)
        self.assertEqual(case.store.get_job(claim['job_id'])['error_message'], seen[0]['error_message'])
        with closing(case.store.connect()) as connection:
            persisted = '\n'.join(connection.iterdump())
        for value in ('host.invalid', 'private-query', 'private-bearer', 'private-json'):
            self.assertNotIn(value, persisted)
            self.assertNotIn(value, json.dumps(seen))
        self.assertTrue(case.service.route('/internal/pipeline/jobs/fail', request, case.auth)['duplicate'])
        self.assertEqual(len(seen), 1)

    def test_sanitization_is_bounded_and_status_ambiguity_is_not_guessed(self):
        fields = failure_fields('HTTP text (HTTP 403) (HTTP 429) ' + 'details ' * 300)
        self.assertLessEqual(len(fields['error_message']), 500)
        self.assertNotIn('upstream_http_status', fields)
        self.assertEqual(failure_fields({'error': 'do not stringify'}), {})
        quoted = XPostError('x_upstream_error', '失败(HTTP 403): forbidden client_secret="multiple private words"', 502)
        self.assertNotIn('private', exception_failure_fields(quoted)['error_message'])
        self.assertNotIn('words', exception_failure_fields(quoted)['error_message'])

    def test_optional_import_or_formatter_failure_cannot_lose_unknown_receipt(self):
        error = XPostError('x_upstream_error', 'safe detail', 502, unknown_outcome=True)
        original_import = builtins.__import__
        def unavailable(name, *args, **kwargs):
            if name == 'features.x_posts.service':
                raise ImportError('private import detail')
            return original_import(name, *args, **kwargs)
        class Executor:
            def execute(self, job, context):
                context.write_started = True
                raise error
        patches = [patch('builtins.__import__', side_effect=unavailable),
                   patch('features.x_posts.service.redact_text', side_effect=RuntimeError('private formatter detail')),
                   patch('features.x_posts.service.redact_text', return_value=None),
                   patch.object(XPostError, '__str__', side_effect=RuntimeError('private str detail'))]
        for boundary in patches:
            with tempfile.TemporaryDirectory() as temp, boundary:
                path = Path(temp) / 'outbox'
                result = execute_claim(JOB, Client(), ReceiptOutbox(path), KEY, Executor())
                self.assertEqual(result['error_code'], 'x_upstream_error')
                self.assertTrue(result['unknown_outcome'])
                self.assertNotIn('error_message', result)
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM receipt_outbox').fetchone()[0], 1)

    def test_broker_rate_limit_cools_only_bound_resource_and_keeps_write_state(self):
        for code, status, expected in (('x_post_rate_limited', 409, True), ('coordinator_http_429', 429, True), ('x_upstream_error', 502, False)):
            for started in (False, True):
                class Broker(Client):
                    def post(self, route, payload):
                        if route == 'jobs/begin-operation':
                            error = CoordinatorError(code, status)
                            error.account_resource = 'x:untrusted-other-account'
                            raise error
                        return super().post(route, payload)
                class Executor:
                    def execute(self, job, context):
                        context.write_started = started
                        with context.operation('x.create', {'body_sha256': 'a' * 64}):
                            raise AssertionError('broker rejection must not issue write credentials')
                with tempfile.TemporaryDirectory() as temp:
                    result = execute_claim(JOB, Broker(), ReceiptOutbox(Path(temp) / 'outbox'), KEY, Executor())
                    self.assertEqual(result['unknown_outcome'], started)
                    self.assertEqual('rate_limit_scope' in result, expected)
                    if expected:
                        self.assertEqual(result['rate_limit_scope'], 'account')
                        self.assertEqual(result['account_resource'], JOB['payload']['account_resource'])


if __name__ == '__main__': unittest.main()
