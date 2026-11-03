"""Synthesis upload contracts with temporary ledgers and no network writes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import requests

from features.drama_synthesis import material_source as module
from features.drama_synthesis.core import DramaSynthesisError
from features.video_editing import VideoEditingError
from features.video_editing.material_source import CATEGORY, MaterialSourceWorker, PRODUCT_NAME
from scripts.patch_drama_material_source import patch_app


JOB_ID = 'a' * 32
HOST = 'public-12345.cos.ap-hongkong.myqcloud.com'
SESSION = {'user_id': 'author', 'open_id': 'author-open', 'tenant_key': 'tenant',
           'role': 'user', 'email': 'author@example.test'}
FIELDS = {'concat_video': 'output_video_url', 'no_bgm_video': 'output_video_no_bgm_url',
          'random_template': 'output_random_template_url'}


class Response:
    def __init__(self, status=200, payload=None, size=128, etag='"frozen-object"'):
        self.status_code = status
        self.headers = {'Content-Length': str(size), 'ETag': etag, 'x-cos-meta-sha256': 'b' * 64}
        self.payload = {'code': 0, 'data': {'id': 987654}} if payload is None else payload

    def json(self):
        return self.payload


class Http:
    def __init__(self):
        self.post_calls = []
        self.head_calls = []
        self.response = Response()
        self.head_response = Response()
        self.post_error = None

    def head(self, url, **kwargs):
        self.head_calls.append((url, kwargs))
        return self.head_response

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if self.post_error:
            raise self.post_error
        return self.response


class Catalog:
    def __init__(self):
        self.rows = []
        self.person_calls = []
        self.drama_calls = []

    def drama(self, app_id, content_id):
        self.drama_calls.append((app_id, content_id))
        return 'US', 'en'

    def initiator(self, email):
        self.person_calls.append(email)
        return 777

    def formal(self, content_sign):
        return self.rows

    def receipt(self, receipt_id):
        return [{'id': receipt_id, 'source_id': 456789, 'source_type': 3}]


class SynthesisMaterialSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job = {'job_id': JOB_ID, 'status': 'done', 'app_id': '1479', 'content_id': 'D123',
                    'drama_name': 'Example Drama', 'creator_user_id': 'author', 'creator_open_id': 'author-open'}
        for kind, field in FIELDS.items():
            self.job[field] = 'https://' + HOST + '/drama/' + JOB_ID + '/' + kind + '.mp4'
        self.catalog = Catalog()
        self.http = Http()
        self.app = SimpleNamespace(COS_DOMAIN=HOST, PUBLIC_BASE_URL='https://ai.example.test/public',
                                   AD_MATERIAL_SOURCE_API_URL='https://aa.yingliangads.com/api/material/source',
                                   AD_MATERIAL_SOURCE_API_TOKEN='test-only-token',
                                   drama_youtube_source=self.source,
                                   fetch_job_row=mock.Mock(side_effect=lambda _: self.job),
                                   json_response=mock.Mock())
        self.path = os.path.join(self.tmp.name, 'uploads.sqlite3')
        self.material = module.SynthesisMaterialSource(self.app, self.path, self.catalog, self.http)
        self.store = self.material.store

    @staticmethod
    def source(job, kind):
        url = job.get(FIELDS.get(kind, ''))
        if not url:
            raise DramaSynthesisError('youtube_source_unavailable', '所选视频产物不可用', 409)
        return kind, url

    def queued(self, kind='concat_video'):
        result = self.material.enqueue(self.job, SESSION, kind)
        self.assertEqual(result['status'], 'queued')
        return next(row for row in self.store.list('material_upload')[0] if row['material_kind'] == kind)

    def current(self, row):
        return self.store.get('material_upload', row['id'])

    def formal(self, row):
        request = row['data']['request']
        return {'id': 456789, 'product': PRODUCT_NAME, 'type': 2,
                'data_source_id': request['content_id'], 'content_sign': request['content_sign'],
                'category': CATEGORY, 'country': '', 'language': request['language'],
                'url': request['url'], 'name': request['name'], 'user_id': 248,
                'initiator': 777, 'is_delete': 0}

    def handler(self, *, session=None, body=None):
        handler = mock.Mock()
        handler.path = '/api/drama-material/jobs/' + JOB_ID + '/material-source'
        handler.headers = {'Content-Length': '32'}
        handler._session.return_value = SESSION if session is None else session
        handler._require_cookie_module.return_value = True
        handler._require_same_origin_json.return_value = True
        handler._read_json.return_value = {'material_kind': 'concat_video'} if body is None else body
        return handler

    def route(self, handler, method='POST'):
        with mock.patch.object(module, 'get_service', return_value=self.material):
            self.assertTrue(module.handle(handler, method, self.app))
        return self.app.json_response.call_args.args[1:]

    def test_independent_store_contains_only_upload_and_lease_tables(self):
        with closing(self.store.connect()) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {'ve_material_upload', 've_lease'})

    def test_fixed_mapping_uses_source_drama_and_current_clicker(self):
        row = self.queued()
        request = row['data']['request']
        self.assertEqual({key: request[key] for key in ('app_id', 'category', 'user_id', 'initiator', 'content_id')},
                         {'app_id': 3214, 'category': 'dramawave_post', 'user_id': 248,
                          'initiator': 777, 'content_id': 'D123'})
        self.assertEqual(self.catalog.drama_calls, [('1479', 'D123')])
        self.assertEqual(self.catalog.person_calls, [SESSION['email']])
        self.assertEqual(request['url'], self.job['output_video_url'])
        self.assertRegex(request['content_sign'], r'^[0-9a-f]{64}$')
        self.assertEqual(self.http.post_calls, [])

    def test_owner_open_id_and_admin_allowed_other_user_denied(self):
        module.check_access(self.job, SESSION)
        module.check_access(self.job, {**SESSION, 'user_id': ''})
        module.check_access(self.job, {'role': 'admin', 'user_id': 'admin'})
        for actor in ({}, {**SESSION, 'user_id': 'other', 'open_id': 'other-open'}):
            with self.assertRaises(VideoEditingError) as caught:
                self.material.enqueue(self.job, actor, 'concat_video')
            self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.catalog.person_calls, [])

    def test_only_completed_video_outputs_can_be_enqueued(self):
        for status in ('queued', 'downloading', 'failed'):
            with self.assertRaises(VideoEditingError) as caught:
                self.material.enqueue({**self.job, 'status': status}, SESSION, 'concat_video')
            self.assertEqual(caught.exception.code, 'drama_job_not_completed')
        for kind in ('cover_16x9', 'unknown', None):
            with self.assertRaises(VideoEditingError) as caught:
                self.material.enqueue(self.job, SESSION, kind)
            self.assertEqual(caught.exception.code, 'material_kind_invalid')
        with self.assertRaises(DramaSynthesisError):
            self.material.enqueue({**self.job, 'output_video_url': ''}, SESSION, 'concat_video')
        self.assertEqual(self.store.list('material_upload')[1], 0)

    def test_three_video_outputs_have_separate_idempotent_records(self):
        records = [self.queued(kind) for kind in FIELDS]
        self.assertEqual(len({row['id'] for row in records}), 3)
        for kind in FIELDS:
            self.assertEqual(self.material.enqueue(self.job, SESSION, kind)['status'], 'queued')
        self.assertEqual(self.store.list('material_upload')[1], 3)
        self.assertEqual(len(self.catalog.person_calls), 3)
        self.assertEqual({item['material_kind'] for item in self.material.records(JOB_ID)}, set(FIELDS))

    def test_concurrent_clicks_create_one_frozen_request(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            answers = list(executor.map(lambda _: self.material.enqueue(self.job, SESSION, 'concat_video'), range(16)))
        self.assertTrue(all(answer['status'] == 'queued' for answer in answers))
        self.assertEqual(self.store.list('material_upload')[1], 1)
        self.assertEqual(self.http.post_calls, [])

    def test_repeat_click_after_restart_reuses_ledger_without_catalog(self):
        row = self.queued()
        self.material.change(row, 'unknown_outcome', attempts=1, error='pending readback')
        fresh = module.SynthesisMaterialSource(self.app, self.path, mock.Mock(), self.http)
        fresh.catalog.drama.side_effect = AssertionError('existing record must not remap')
        self.assertEqual(fresh.enqueue(self.job, SESSION, 'concat_video')['status'], 'unknown_outcome')
        self.assertEqual(self.store.list('material_upload')[1], 1)

    def test_untrusted_or_signed_urls_rejected_without_network(self):
        invalid = ['http://' + HOST + '/x.mp4', 'https://other.example/x.mp4',
                   'https://' + HOST + '/x.mp4?token=x', 'https://' + HOST + '/x.mp4#fragment',
                   'https://user:pass@' + HOST + '/x.mp4', 'https://ai.example.test/outside/x.mp4']
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(VideoEditingError):
                self.material.enqueue({**self.job, 'output_video_url': url}, SESSION, 'concat_video')
        self.assertFalse(self.http.head_calls)
        self.material.validate_url('https://ai.example.test/public/x.mp4')

    def test_worker_validates_public_video_and_confirms_formal_receipt(self):
        row = self.queued()
        worker = MaterialSourceWorker(self.material)
        self.assertTrue(worker.tick())
        waiting = self.current(row)
        self.assertEqual(waiting['status'], 'verifying')
        self.assertEqual(waiting['data']['receipt_id'], '987654')
        self.assertEqual(waiting['data']['public_identity']['size_bytes'], 128)
        self.assertFalse(self.http.head_calls[0][1]['allow_redirects'])
        self.catalog.rows = [self.formal(row)]
        worker.process(waiting)
        result = self.current(row)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['data']['material_id'], '456789')
        self.assertEqual(self.material.records(JOB_ID)[0]['material_id'], '456789')
        self.assertEqual(len(self.http.post_calls), 1)
        self.assertEqual(self.material.enqueue(self.job, SESSION, 'concat_video')['status'], 'succeeded')

    def test_timeout_then_formal_without_receipt_never_reposts_or_claims_success(self):
        row = self.queued()
        self.http.post_error = requests.Timeout('response lost')
        worker = MaterialSourceWorker(self.material)
        worker.process(row)
        self.assertEqual(self.current(row)['status'], 'unknown_outcome')
        self.assertEqual(self.current(row)['data']['attempts'], 1)
        self.catalog.rows = [self.formal(row)]
        worker.process(self.current(row))
        result = self.current(row)
        self.assertEqual(result['status'], 'unknown_outcome')
        self.assertIn('缺少有效接口回执', result['data']['error'])
        self.assertNotIn('material_id', self.material.dto(result))
        self.assertEqual(len(self.http.post_calls), 1)

    def test_business_rejection_is_visible_and_not_retried(self):
        row = self.queued()
        self.http.response = Response(payload={'code': -1, 'message': 'invalid'})
        MaterialSourceWorker(self.material).process(row)
        self.assertEqual(self.current(row)['status'], 'failed')
        self.assertEqual(self.material.enqueue(self.job, SESSION, 'concat_video')['status'], 'failed')
        self.assertEqual(len(self.http.post_calls), 1)

    def test_existing_formal_and_interrupted_submission_are_readback_only(self):
        row = self.queued()
        self.catalog.rows = [self.formal(row)]
        worker = MaterialSourceWorker(self.material)
        worker.process(row)
        self.assertEqual(self.current(row)['status'], 'unknown_outcome')
        row = self.material.change(row, 'submitting', attempts=1)
        worker.process(row)
        self.assertEqual(self.current(row)['status'], 'unknown_outcome')
        self.assertEqual(self.http.post_calls, [])

    def test_formal_fields_and_receipt_binding_must_match(self):
        row = self.queued()
        worker = MaterialSourceWorker(self.material)
        worker.process(row)
        formal = self.formal(row)
        self.catalog.rows = [{**formal, 'data_source_id': 'WRONG'}]
        worker.process(self.current(row))
        self.assertEqual(self.current(row)['status'], 'unknown_outcome')
        self.assertIn('data_source_id', self.current(row)['data']['error'])
        self.catalog.rows = [formal]
        self.catalog.receipt = lambda _: [{'source_id': 1, 'source_type': 3}]
        worker.process(self.current(row))
        self.assertEqual(self.current(row)['status'], 'unknown_outcome')
        self.assertIn('回执未关联', self.current(row)['data']['error'])
        self.assertEqual(len(self.http.post_calls), 1)

    def test_job_deletion_before_submission_never_posts(self):
        row = self.queued()
        self.app.fetch_job_row.side_effect = None
        self.app.fetch_job_row.return_value = None
        MaterialSourceWorker(self.material).process(row)
        self.assertEqual(self.current(row)['status'], 'failed')
        self.assertEqual(self.http.post_calls, [])

    def test_output_url_change_before_submission_never_posts(self):
        row = self.queued()
        self.job['output_video_url'] = 'https://' + HOST + '/drama/changed.mp4'
        MaterialSourceWorker(self.material).process(row)
        self.assertEqual(self.current(row)['status'], 'failed')
        self.assertIn('视频已变化', self.current(row)['data']['error'])
        self.assertEqual(self.http.post_calls, [])

    def test_unreadable_public_file_waits_without_post(self):
        row = self.queued()
        self.http.head_response = Response(status=404, size=0)
        MaterialSourceWorker(self.material).process(row)
        self.assertEqual(self.current(row)['status'], 'copying')
        self.assertEqual(self.current(row)['data']['attempts'], 0)
        self.assertEqual(self.http.post_calls, [])

    def test_two_worker_ticks_hold_one_lease_and_submit_once(self):
        self.queued()
        workers = [MaterialSourceWorker(self.material), MaterialSourceWorker(self.material)]
        with ThreadPoolExecutor(max_workers=2) as executor:
            answers = list(executor.map(lambda worker: worker.tick(), workers))
        self.assertEqual(sum(bool(answer) for answer in answers), 1)
        self.assertEqual(len(self.http.post_calls), 1)

    def test_public_identity_change_during_pre_submission_retry_is_rejected(self):
        row = self.queued()
        self.material.ensure_public_copy(row)
        self.http.head_response = Response(etag='"changed-object"')
        MaterialSourceWorker(self.material).process(self.current(row))
        self.assertEqual(self.current(row)['status'], 'failed')
        self.assertIn('文件已变化', self.current(row)['data']['error'])
        self.assertEqual(self.http.post_calls, [])

    def test_route_preserves_cookie_module_same_origin_and_owner_checks(self):
        for gate in ('_require_cookie_module', '_require_same_origin_json'):
            handler = self.handler()
            getattr(handler, gate).return_value = False
            with mock.patch.object(module, 'get_service') as factory:
                self.assertTrue(module.handle(handler, 'POST', self.app))
                factory.assert_not_called()
            handler._read_json.assert_not_called()
        handler = self.handler(session={**SESSION, 'user_id': 'other', 'open_id': 'other-open'})
        status, payload = self.route(handler)
        self.assertEqual((status, payload['code']), (403, 'permission_denied'))

    def test_route_accepts_only_material_kind_not_client_override(self):
        body = {'material_kind': 'concat_video', 'url': 'https://evil.example/video.mp4',
                'app_id': 1, 'content_id': 'WRONG', 'initiator': 999, 'user_id': 999}
        status, _ = self.route(self.handler(body=body))
        self.assertEqual(status, 202)
        row = self.store.list('material_upload')[0][0]
        self.assertEqual(row['data']['request']['url'], self.job['output_video_url'])
        self.assertEqual(row['data']['request']['app_id'], 3214)
        self.assertEqual(row['data']['request']['initiator'], 777)
        self.assertTrue(self.app.json_response.call_args.kwargs['no_store'])
        self.app.json_response.reset_mock()
        status, payload = self.route(self.handler(), 'GET')
        self.assertEqual(status, 200)
        self.assertEqual(len(payload['items']), 1)

    def test_route_maps_invalid_body_and_missing_job_without_writes(self):
        for body in ([], {'material_kind': 'cover_16x9'}, {'material_kind': ['concat_video']}):
            self.app.json_response.reset_mock()
            status, _ = self.route(self.handler(body=body))
            self.assertEqual(status, 400)
        handler = self.handler()
        handler.headers['Content-Length'] = '4097'
        status, _ = self.route(handler)
        self.assertEqual(status, 413)
        self.app.fetch_job_row.side_effect = None
        self.app.fetch_job_row.return_value = None
        status, _ = self.route(self.handler())
        self.assertEqual(status, 404)
        self.assertEqual(self.store.list('material_upload')[1], 0)

    def test_decorating_history_failure_preserves_normal_job(self):
        job = deepcopy(self.job)
        with mock.patch.object(module, 'get_service', side_effect=OSError('ledger unavailable')):
            result = module.decorate(job, self.app)
        self.assertEqual(result['output_video_url'], self.job['output_video_url'])
        self.assertIsNone(result['material_source_uploads'])

    def test_patch_does_not_shadow_sys_for_unrelated_routes(self):
        original = '''import sys
import re
from urllib.parse import urlparse
def decorate_drama_synthesis_job(job):
    item = dict(job)
    return item

def next_function():
    return None
class Handler:
    def do_GET(self):
        return sys.modules[__name__]
    def do_POST(self):
        return sys.modules[__name__]
'''
        namespace = {'__name__': 'sys'}
        exec(compile(patch_app(original), '<patched-test-app>', 'exec'), namespace)
        handler = namespace['Handler']()
        handler.path = '/unrelated-route'
        import sys
        self.assertIs(handler.do_GET(), sys)
        self.assertIs(handler.do_POST(), sys)


if __name__ == '__main__':
    unittest.main()
