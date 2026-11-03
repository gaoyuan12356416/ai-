"""Slow media transport is bounded without retrying an uncertain Post."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.executors import NodeExecutors
from features.x_posts.service import HttpResponse, XPostError, XApiClient
from features.x_posts.media_limits import MAX_UPLOAD_SEGMENTS, MAX_POST_VIDEO_BYTES
from scripts.test_pipeline_executors import Context


class TransportTests(unittest.TestCase):
    def run_upload(self, fail_post=False):
        body = b'x' * (2 * 1024 * 1024 + 5)
        requests = []
        def download(artifact, path, context):
            path.write_bytes(body); return path
        def request(_http, method, url, **kw):
            requests.append((url, kw))
            if url.endswith('/initialize'): return HttpResponse(200, {}, b'{"data":{"id":"99"}}')
            if url.endswith('/2/tweets'):
                if fail_post: raise TimeoutError('lost response after Post request')
                return HttpResponse(201, {}, b'{"data":{"id":"12345"}}')
            return HttpResponse(200, {}, b'{"data":{}}')
        with tempfile.TemporaryDirectory() as temp:
            executor = NodeExecutors({'state_root':temp,'backend':'cpu'})
            with patch.object(executor, '_download', side_effect=download), patch('features.x_posts.service.UrllibHttpClient.request', new=request):
                artifact={'url':'https://media.example/video','sha256':hashlib.sha256(body).hexdigest(),'size':len(body)}
                if fail_post:
                    with self.assertRaises(XPostError) as caught:
                        executor.x_publish({'account_resource':'x:account:1','request':{'body':'frozen'}},artifact,Context())
                    self.assertTrue(caught.exception.unknown_outcome)
                else:
                    result=executor.x_publish({'account_resource':'x:account:1','request':{'body':'frozen'}},artifact,Context())
                    self.assertEqual(result['post_id'],'12345')
        return requests

    def test_small_segments_and_media_timeout_preserve_single_post(self):
        requests=self.run_upload()
        chunks=[kw for url,kw in requests if url.endswith('/append')]
        self.assertEqual(len(chunks),3)
        self.assertTrue(all(len(kw['body']) < 1024*1024+1024 for kw in chunks))
        self.assertTrue(all(kw['timeout']==120 for url,kw in requests if '/2/media/' in url))
        posts=[kw for url,kw in requests if url.endswith('/2/tweets')]
        self.assertEqual(len(posts),1); self.assertEqual(posts[0]['timeout'],30)
        self.assertEqual(json.loads(posts[0]['body']),{'text':'frozen','media':{'media_ids':['99']}})

    def test_post_timeout_stays_unknown_and_is_never_reissued(self):
        requests=self.run_upload(fail_post=True)
        self.assertEqual(sum(url.endswith('/2/tweets') for url,_ in requests),1)

    def test_maximum_media_stays_within_existing_segment_limit(self):
        captured=[]
        def upload(client,*args,**kwargs):
            captured.append(client.chunk_bytes); return {'media_id':'99'}
        with tempfile.TemporaryDirectory() as temp:
            executor=NodeExecutors({'state_root':temp,'backend':'cpu'})
            artifact={'url':'https://media.example/video','sha256':'a'*64,'size':MAX_POST_VIDEO_BYTES}
            with patch.object(executor,'_download',side_effect=lambda a,p,c:p), patch.object(XApiClient,'upload_media',new=upload), patch.object(XApiClient,'create_post',return_value={'post_id':'123'}):
                executor.x_publish({'account_resource':'x:account:1','request':{'body':'frozen'}},artifact,Context())
        self.assertLessEqual((MAX_POST_VIDEO_BYTES+captured[0]-1)//captured[0],MAX_UPLOAD_SEGMENTS)


if __name__=='__main__': unittest.main()
