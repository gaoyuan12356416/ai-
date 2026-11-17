import ast
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from features.drama_synthesis.storage_targets import checkpoint_target, artifact_url
from features.drama_synthesis import gpu_cache
from types import SimpleNamespace


class CutoverTests(unittest.TestCase):
    def test_fb_source_origins_keep_url_boundaries(self):
        from urllib.parse import urlsplit, urlunsplit
        for rel in ['features/fb_video_publish/sources.py','features/fb_auto_posts/manual_batch.py']:
            tree=ast.parse((ROOT/rel).read_text(encoding='utf-8'))
            nodes=[n for n in tree.body if (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id.endswith('_HOST') for t in n.targets)) or (isinstance(n,ast.FunctionDef) and n.name=='prepare_source_url')]
            class Invalid(Exception): pass
            ns={'urlsplit':urlsplit,'urlunsplit':urlunsplit,'SourceError':Invalid,'BatchError':Invalid}
            exec(compile(ast.Module(body=nodes,type_ignores=[]),rel,'exec'),ns)
            fn=ns['prepare_source_url'];host='ads-ai-1306474899.cos.ap-hongkong.myqcloud.com'
            self.assertEqual(fn('https://'+host+'/a.mp4'),'https://'+host+'/a.mp4')
            self.assertTrue(fn('http://advertising-1306474899.cos.ap-hongkong.myqcloud.com/a.mp4').startswith('https://'))
            for url in ['https://'+host+'.evil.example/a.mp4','https://user@'+host+'/a.mp4','http://'+host+'/a.mp4','https://'+host+'/a.mp4#part']:
                with self.assertRaises(Invalid):fn(url)

    def test_new_upload_and_old_checkpoint_are_separate(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            'COS_LEGACY_BUCKET_DOMAINS': json.dumps({'old-bucket': 'old.example.com'})
        }):
            path = Path(folder) / 'checkpoint.json'
            self.assertEqual(checkpoint_target(path, 'new-bucket', 'new.example.com'), ('new-bucket', 'new.example.com'))
            path.write_text(json.dumps({'target': {'bucket': 'old-bucket', 'key': 'video.mp4'}}))
            self.assertEqual(checkpoint_target(path, 'new-bucket', 'new.example.com'), ('old-bucket', 'old.example.com'))
            path.write_text(json.dumps({'target': {'bucket': 'unexpected-bucket'}}))
            with self.assertRaises(ValueError):
                checkpoint_target(path, 'new-bucket', 'new.example.com')

    def test_artifact_host_is_bound_to_bucket(self):
        with patch.dict(os.environ, {'COS_LEGACY_BUCKET_DOMAINS': '{"old-bucket":"old.example.com"}'}):
            url = lambda key: 'https://new.example.com/' + key
            self.assertEqual(artifact_url('old-bucket', 'v.mp4', 'new-bucket', url), 'https://old.example.com/v.mp4')
            self.assertEqual(artifact_url('new-bucket', 'v.mp4', 'new-bucket', url), 'https://new.example.com/v.mp4')
            with self.assertRaises(ValueError):
                artifact_url('foreign', 'v.mp4', 'new-bucket', url)

    def test_corrupt_checkpoint_is_not_new_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'checkpoint.json'; path.write_text('broken')
            with self.assertRaises(Exception):
                checkpoint_target(path, 'new-bucket', 'new.example.com')

    def test_legacy_domains_reject_userinfo(self):
        with patch.dict(os.environ, {'COS_LEGACY_BUCKET_DOMAINS': '{"old":"user@host"}'}):
            with self.assertRaises(ValueError):
                artifact_url('old', 'v.mp4', 'new', str)

    def test_old_gpu_receipt_still_requires_exact_integrity(self):
        url='https://old.example.com/v.mp4'
        artifact={'url':url,'bucket':'old-bucket','key':'v.mp4','sha256':'a'*64,
                  'size_bytes':12,'etag':'verified-etag','binding':'b'*32}
        result={'gpu_result_manifest_version':3,'input_fingerprint':'c'*64,
                'output_video_url':url,'gpu_result_artifacts':{'output_video_url':artifact}}
        headers={'content-length':'12',gpu_cache.SIZE_HEADER:'12',gpu_cache.SHA_HEADER:'a'*64,
                 gpu_cache.BINDING_HEADER:'b'*32,'etag':'verified-etag'}
        calls=[]
        def head(**kw): calls.append(kw);return headers
        with patch.dict(os.environ, {'COS_LEGACY_BUCKET_DOMAINS':'{"old-bucket":"old.example.com"}'}):
            args={'client':SimpleNamespace(head_object=head),'bucket':'new-bucket','url_for_key':lambda k:'https://new.example.com/'+k}
            self.assertTrue(gpu_cache.verify_artifacts(result,{'concat_video':True},**args))
            self.assertEqual(calls[-1],{'Bucket':'old-bucket','Key':'v.mp4'})
            headers[gpu_cache.SHA_HEADER]='d'*64
            with self.assertRaises(Exception): gpu_cache.verify_artifacts(result,{'concat_video':True},**args)

    def test_renewed_session_retains_only_old_and_new_prefixes(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('issuer',ROOT/'scripts/pipeline_cos_credentials.py')
        issuer=importlib.util.module_from_spec(spec);spec.loader.exec_module(issuer)
        requests=[]
        def sts(action,body,*args):
            requests.append((action,body))
            if action=='GetCallerIdentity':return {'AccountId':'123'}
            return {'Credentials':{},'ExpiredTime':9999999999}
        cfg={'region':'ap-hongkong','bucket':'new-123','prefix':'prepare/v1','delivery_bucket':'new-123','delivery_prefix':'delivery/v1',
             'legacy_work_ids':['12:1'],'legacy_storage':{'bucket':'old-123','delivery_bucket':'old-delivery-123'}}
        with tempfile.TemporaryDirectory() as folder,patch.object(issuer,'sts',sts):
            path=Path(folder);(path/'keys').write_text('COS_SECRET_ID=dummy\nCOS_SECRET_KEY=dummy\n')
            issuer.issue(cfg,path/'keys',path/'session')
        policy=json.loads(requests[-1][1]['Policy'])
        resources=policy['statement'][1]['resource']
        self.assertEqual(len(resources),4)
        self.assertTrue(all(r.endswith(('/prepare/v1/*','/delivery/v1/*')) for r in resources))
        self.assertEqual(len(policy['statement'][0]['resource']),2)


if __name__ == '__main__': unittest.main()
