"""Offline checks for separate backend/daily capabilities and owner revocation."""
import contextlib
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from features.x_accounts import oauth_service as oauth
from features.x_posts.service import XPostError, XPostStore
from features.x_video_publish import runtime
from features.x_video_publish.core import PersonalStore
from features.x_video_publish.errors import PublishingError
from features.x_video_publish.navigation import ensure_navigation
from features.x_video_publish.service import PublishingService
from scripts import x_video_pool_scheduler as runner


class Handler:
    def __init__(self, role, payload, extra=None):
        self.role, self.response = role, None
        raw = json.dumps(payload).encode()
        self.headers = Message()
        for key, value in [('Content-Type', 'application/json'), ('Content-Length', str(len(raw))),
                           ('Authorization', 'Bearer inert-test-token')]+(extra or []):
            self.headers[key] = value
        self.rfile = io.BytesIO(raw)
    def internal_role(self): return self.role
    def send_json(self, status, data): self.response = (status, data)


class RuntimeTests(unittest.TestCase):
    def test_backend_cannot_tick_daily_cannot_impersonate_browser_and_auto_neither(self):
        for role, path in [('backend', 'pool/tick'), ('daily', 'dispatch'), ('auto', 'dispatch'), ('auto', 'pool/start')]:
            handler = Handler(role, {'worker_id': 'test'})
            with mock.patch.object(runtime, 'services') as services:
                runtime.dispatch(handler, oauth, '/internal/video-publish/'+path)
                self.assertEqual(403, handler.response[0]); services.assert_not_called()

    def test_forwarded_and_duplicate_bearer_rejected_before_data_access(self):
        for header in [('X-Forwarded-For', '127.0.0.1'), ('Forwarded', 'for=127.0.0.1'), ('Authorization', 'Bearer second')]:
            handler = Handler('backend', {}, [header])
            with mock.patch.object(runtime, 'services') as services:
                runtime.dispatch(handler, oauth, '/internal/video-publish/dispatch')
                self.assertEqual(403, handler.response[0]); services.assert_not_called()

    def test_daily_can_only_start_and_tick_with_worker_id(self):
        service, scheduler = mock.Mock(), mock.Mock()
        scheduler.tick.return_value = {'status': 'tick_complete', 'slots_created': 0}
        with mock.patch.object(runtime, 'services', return_value=(service, scheduler)):
            handler = Handler('daily', {'worker_id': 'isolated-worker'})
            runtime.dispatch(handler, oauth, '/internal/video-publish/pool/tick')
            scheduler.tick.assert_called_once_with('isolated-worker')
            self.assertEqual(200, handler.response[0]); service.dispatch.assert_not_called()
            handler = Handler('daily', {'worker_id': 'isolated-worker', 'actor': {}})
            runtime.dispatch(handler, oauth, '/internal/video-publish/pool/tick')
            self.assertEqual(400, handler.response[0])

    def test_commit_unknown_is_not_claimed_rejected(self):
        payload = {'method': 'POST', 'path': '/batches', 'actor': {}, 'payload': {}, 'query': {}}
        service = mock.Mock(); service.dispatch.side_effect = RuntimeError('after-commit-failure')
        with mock.patch.object(runtime, 'services', return_value=(service, mock.Mock())):
            handler = Handler('backend', payload)
            runtime.dispatch(handler, oauth, '/internal/video-publish/dispatch')
            self.assertEqual(503, handler.response[0]); self.assertNotIn('accepted', handler.response[1])
            service.dispatch.side_effect = XPostError('invalid_request', 'invalid request', 400)
            handler = Handler('backend', payload)
            runtime.dispatch(handler, oauth, '/internal/video-publish/dispatch')
            self.assertEqual(False, handler.response[1]['accepted'])

    def test_runner_disabled_never_calls_sidecar(self):
        request = mock.Mock()
        self.assertEqual(0, runner.run({}, request=request)); request.assert_not_called()

    def test_runner_reestablishes_start_cutoff_after_sidecar_restart(self):
        calls = []
        class Stop:
            def is_set(self): return len(calls) >= 4
            def wait(self, seconds): return self.is_set()
        def request(url, token, payload):
            calls.append((url, token, payload))
            return {'status': 'not_started' if len(calls) == 2 else 'started' if url.endswith('/start') else 'tick_complete'}
        runner.run({'X_VIDEO_PUBLISH_POOL_SCHEDULER_ENABLED': '1', 'X_POST_DAILY_INTERNAL_TOKEN': 'inert'}, Stop(), request)
        self.assertEqual(['start','tick','start','tick'], [c[0].rsplit('/',1)[-1] for c in calls])
        self.assertEqual(1, len({c[2]['worker_id'] for c in calls}))

    def test_navigation_preserves_disabled_existing_items_and_old_permissions(self):
        original = [{'key':'x_platform','module':'x_accounts','items':[
            {'key':'xAccounts','module':'x_accounts','enabled':False},
            {'key':'xVideoPublish','module':'x_video_publish','enabled':False}]}]
        result = ensure_navigation(original)
        self.assertEqual('x_accounts', original[0]['module'])
        self.assertEqual('x_accounts', result[0]['module'])
        self.assertFalse(result[0]['items'][0]['enabled']); self.assertFalse(result[0]['items'][1]['enabled'])
        self.assertEqual(1, sum(i['key']=='xVideoPublish' for i in result[0]['items']))

    def test_full_history_page_returns_bounded_summaries_not_every_caption(self):
        # Fifty maximum-size batches must fit the 2 MiB internal response cap.
        # Details are fetched separately; a growing history must remain readable.
        service = object.__new__(PublishingService)
        service.enabled = False
        service.oauth = SimpleNamespace(require_actor_subject=lambda actor: actor)
        task = {'caption':'private caption'*200, 'source_snapshot':{'drama_description':'large description'*200}}
        batches = [{'id':i,'source':'manual','status':'queued','created_at':'2030-01-01T00:00:00Z',
                    'tasks':[task]*200} for i in range(50)]
        service.store = mock.Mock()
        service.store.list_batches.return_value = {'items':batches,'total':100}
        result = service.dispatch('GET','/batches',{'tenant_key':'t','user_id':'u'},query={'limit':'50'})
        self.assertEqual('50', result['next_cursor'])
        self.assertEqual(200, result['items'][0]['total_tasks'])
        self.assertLess(len(json.dumps(result).encode()), 30000)
        self.assertNotIn('private caption', json.dumps(result))

    def test_permission_revocation_blocks_next_x_request_and_legacy_is_unchanged(self):
        actor = {'tenant_key':'tenant-test','user_id':'owner-test','name':'Test'}
        account = {'id':1,'x_user_id':'1900000000000000011','status':'active','publish_approved':True,
                   'publish_eligible':True,'drama_language':'en','owner_tenant_key':'tenant-test','owner_user_id':'owner-test'}
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(oauth, 'POST_DB_PATH', Path(temp)/'posts.sqlite'):
            store = PersonalStore(oauth.POST_DB_PATH)
            materials = {'501':{'material_id':'501','drama_name':'Drama','language':'en'}}
            preview = store.preview(actor, {'source':'manual','rows':[{'material_id':'501','account_id':'1','caption':'Hi'}]}, {'1':account}, materials)
            batch = store.commit(actor, preview['preview_token'], 'runtime-permission-test', {'1':account}, materials)
            run_id = batch['tasks'][0]['manual_run_id']
            @contextlib.contextmanager
            def credentials(*args, **kwargs): yield account, 'inert-token'
            with mock.patch.object(oauth, 'publish_credentials', side_effect=credentials), \
                 mock.patch.object(oauth, 'verify_account'), \
                 mock.patch.object(oauth, 'find_scoped_account_row', return_value=account) as find, \
                 mock.patch.object(oauth, 'row_to_item', side_effect=lambda row:row), \
                 mock.patch('features.x_video_publish.transport.broker', return_value={'actor':actor}) as broker:
                provider = oauth.publishing_token_provider(1, actor, 'en', personal_run_id=run_id)
                self.assertEqual('inert-token', provider())
                self.assertEqual('mine', find.call_args.args[-1])
                broker.side_effect = PublishingError('identity_permission_denied','revoked',403)
                with self.assertRaises(XPostError) as denied: provider()
                self.assertEqual(403, denied.exception.status)
                # Unrelated legacy publishing never calls the new permission broker.
                broker.reset_mock()
                self.assertEqual('inert-token', oauth.publishing_token_provider(1, actor, 'en')())
                broker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
