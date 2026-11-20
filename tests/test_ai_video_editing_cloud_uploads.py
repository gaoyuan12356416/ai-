import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from features.ai_video_editing.cloud_uploads import CloudUploads, PART_BYTES
from features.ai_video_editing.cloud_media import validate_url, materialize
from features.ai_video_editing.runtime import Fault, Runtime, public_asset


class Missing(Exception):
    def get_status_code(self):
        return 404


class COS:
    def __init__(self):
        self.remote = {}
        self.objects = {}
        self.creates = self.completes = 0
        self.lose_create = self.lose_complete = self.public = False

    def create_multipart_upload(self, **kw):
        self.creates += 1
        self.remote[kw['Key']] = {'id': str(self.creates), 'metadata': kw['Metadata'], 'parts': {}}
        if self.lose_create:
            raise TimeoutError()
        return {'UploadId': str(self.creates)}

    def list_multipart_uploads(self, **kw):
        return {'Upload': [{'Key': key, 'UploadId': obj['id']} for key, obj in self.remote.items()]}

    def list_parts(self, **kw):
        obj = self.remote[kw['Key']]
        return {'Part': [{'PartNumber': n, 'Size': len(body), 'ETag': hashlib.md5(body).hexdigest()}
                         for n, body in obj['parts'].items()]}

    def get_presigned_url(self, **kw):
        return ('https://test-123.cos.ap-hongkong.myqcloud.com/' + kw['Key'] +
                '?q-signature=test&q-url-param-list=partnumber%3Buploadid&uploadId=test&partNumber=1')

    def head_object(self, **kw):
        if kw['Key'] not in self.objects:
            raise Missing()
        return self.objects[kw['Key']]

    def complete_multipart_upload(self, **kw):
        self.completes += 1
        obj = self.remote[kw['Key']]
        self.objects[kw['Key']] = {'Content-Length': sum(len(p) for p in obj['parts'].values()),
                                  'ETag': '"object-etag"', **obj['metadata']}
        if self.lose_complete:
            raise TimeoutError()

    def get_object_acl(self, **kw):
        return {'AccessControlList': {'Grant': [{'Grantee': {'URI': 'AllUsers'} if self.public else {'ID': 'owner'}}]}}


class CloudUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rt = Runtime(self.tmp.name)
        self.cos = COS()
        self.cloud = self.rt.cloud = CloudUploads(self.rt, self.cos, 'test-123', 'ap-hongkong')
        self.actor = {'tenant_key': 'one', 'user_id': 'u'}
        self.other = {'tenant_key': 'two', 'user_id': 'u'}
        self.payload = {'operation_id': 'operation-one', 'name': 'movie.mp4', 'series': 'test', 'episode': 1, 'size': 7}
        self.bytes = b'example'

    def uploaded(self):
        session = self.cloud.create(self.actor, self.payload)
        uid = session['id']
        md5 = base64.b64encode(hashlib.md5(self.bytes).digest()).decode()
        signature = self.cloud.sign(uid, self.actor, 1, {'md5': md5})
        self.assertEqual(signature['headers']['x-cos-psize-max'], '7')
        row = self.cloud.row(uid)
        self.cos.remote[self.cloud.key(row)]['parts'][1] = self.bytes
        return uid

    def ready(self):
        uid = self.uploaded()
        self.cloud.complete(uid, self.actor)
        job = self.cloud.claim()
        body = {'sha256': hashlib.sha256(self.bytes).hexdigest(),
                'metadata': {'duration_ms': 1000, 'width': 160, 'height': 240, 'has_audio': True}}
        self.cloud.worker_update(uid, job['lease'], 'verified', body)
        return uid, job, body

    def test_resumable_parts_and_owner_isolation(self):
        uid = self.uploaded()
        resumed = self.cloud.create(self.actor, self.payload)
        self.assertEqual(resumed['id'], uid)
        self.assertEqual(resumed['uploaded_parts'][0]['size'], 7)
        self.assertEqual(self.cos.creates, 1)
        with self.assertRaises(Fault):
            self.cloud.row(uid, self.other)
        with self.assertRaises(Fault):
            self.cloud.complete(uid, self.other)
        with self.assertRaises(Fault):
            self.cloud.sign(uid, self.actor, 1, {'md5': base64.b64encode(b'x' * 16).decode()})
        with self.assertRaises(Fault):
            self.cloud.create(self.actor, {**self.payload, 'size': 8})

    def test_lost_initialization_recovers_same_remote_upload(self):
        self.cos.lose_create = True
        with self.assertRaises(Fault):
            self.cloud.create(self.actor, self.payload)
        session = self.cloud.create(self.actor, self.payload)
        self.assertEqual(session['status'], 'uploading')
        self.assertEqual(self.cos.creates, 1)

    def test_lost_completion_reconciles_without_reupload(self):
        uid = self.uploaded()
        self.cos.lose_complete = True
        with self.assertRaises(Fault):
            self.cloud.complete(uid, self.actor)
        row = self.cloud.complete(uid, self.actor)
        self.assertEqual(row['status'], 'queued')
        self.assertEqual(self.cos.completes, 1)

    def test_incomplete_mismatched_and_public_objects_do_not_enter_library(self):
        uid = self.cloud.create(self.actor, self.payload)['id']
        with self.assertRaises(Fault):
            self.cloud.complete(uid, self.actor)
        uid = self.uploaded()
        row = self.cloud.row(uid)
        self.cos.remote[self.cloud.key(row)]['parts'][1] = b'changed'
        with self.assertRaises(Fault):
            self.cloud.complete(uid, self.actor)
        self.cos.remote[self.cloud.key(row)]['parts'][1] = self.bytes
        self.cos.public = True
        with self.assertRaises(Fault):
            self.cloud.complete(uid, self.actor)
        self.assertIsNone(self.cloud.claim())
        self.assertEqual(self.rt.assets(self.actor, '', 10, 0)['total'], 0)

    def test_verified_receipt_is_idempotent_and_private(self):
        uid, job, body = self.ready()
        self.assertTrue(self.cloud.worker_update(uid, job['lease'], 'verified', body)['ok'])
        with self.assertRaises(Fault):
            self.cloud.worker_update(uid, job['lease'], 'verified', {**body, 'sha256': 'a' * 64})
        public = self.cloud.dto(self.cloud.row(uid))['asset']
        self.assertNotIn('_cos', public)
        self.assertNotIn('_cos', public['metadata'])
        self.assertNotIn('path', public)
        self.assertEqual(self.rt.assets(self.actor, '', 10, 0)['total'], 1)
        self.assertFalse((self.rt.root / 'uploads' / (public['id'] + '.mp4')).exists())
        with self.assertRaises(Fault):
            self.rt.asset(public['id'], self.other)

    def test_expired_worker_lease_and_old_receipt_cannot_replace_new_owner(self):
        uid = self.uploaded()
        self.cloud.complete(uid, self.actor)
        first = self.cloud.claim()
        self.assertIsNone(self.cloud.claim())
        self.cloud.update(uid, expires=0)
        second = self.cloud.claim()
        self.assertNotEqual(first['lease'], second['lease'])
        with self.assertRaises(Fault):
            self.cloud.worker_update(uid, first['lease'], 'heartbeat', {})
        self.cloud.worker_update(uid, second['lease'], 'heartbeat', {})

    def test_legacy_source_and_lazy_cloud_cache(self):
        uid, _, _ = self.ready()
        asset = self.rt.asset(self.cloud.row(uid)['asset_id'], self.actor)
        def fetch(location, target):
            self.assertEqual(location['sha256'], hashlib.sha256(self.bytes).hexdigest())
            Path(target).write_bytes(self.bytes)
        with patch('features.ai_video_editing.cloud_media.download', side_effect=fetch) as get:
            self.rt.local_asset(asset['id'], self.actor)
            self.rt.local_asset(asset['id'], self.actor)
            self.assertEqual(get.call_count, 1)
        legacy = {'path': asset['path']}
        self.assertEqual(materialize(self.rt, legacy), legacy)

    def test_signed_media_allows_only_hong_kong_storage(self):
        good = 'https://test-123.cos.ap-hongkong.myqcloud.com/ai-video-editing/v1/uploads/id/source.mp4?q=x'
        self.assertEqual(validate_url(good), good)
        for url in [good.replace('https:', 'http:'), good.replace('ap-hongkong', 'ap-beijing'),
                    good.replace('test-123.', 'user:pass@test-123.'), good.replace('/ai-video-editing/', '/other/')]:
            with self.assertRaises(Fault):
                validate_url(url)


if __name__ == '__main__':
    unittest.main()
