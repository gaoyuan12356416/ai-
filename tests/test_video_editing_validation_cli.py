import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.video_editing import VideoEditingError
from features.video_editing import uploads as uploads_module
from test_video_editing_uploads import FakeCos

spec=importlib.util.spec_from_file_location('validation_cli',Path(__file__).resolve().parents[1]/'scripts/validate_video_editing_cos.py')
cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)


class ValidationCliTests(unittest.TestCase):
    def test_environment_values_are_literal_never_shell_expanded(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{},clear=True):
            path=Path(tmp)/'env'
            path.write_text('VE_TEST="$(do_not_execute)"\nVE_WORD=value\n',encoding='utf-8')
            cli.load_environment([path])
            self.assertEqual(os.environ['VE_TEST'],'$(do_not_execute)')
    def test_invalid_run_id_cannot_escape_isolated_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(VideoEditingError):cli.dispatch({'op':'create','run_id':'../production'},None,Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()),[])
    def test_sign_expiry_override_is_isolated_and_restored(self):
        cos=FakeCos();sdk=SimpleNamespace(CosConfig=lambda **kwargs:None,CosS3Client=lambda *args,**kwargs:cos)
        app=SimpleNamespace(COS_BUCKET='bucket',COS_REGION='ap-hongkong',COS_SECRET_ID='dummy',COS_SECRET_KEY='dummy')
        with tempfile.TemporaryDirectory() as tmp,patch.dict(sys.modules,{'qcloud_cos':sdk}),patch.dict(os.environ,{},clear=True):
            root=Path(tmp);base={'run_id':'canary_20261022'}
            created=cli.dispatch({**base,'op':'create','operation_id':'operation_1','name':'test.mp4','size_bytes':12,'content_type':'video/mp4'},app,root)
            import base64,hashlib
            signed=cli.dispatch({**base,'op':'sign','upload_id':created['item']['id'],'part_number':1,
                                 'content_md5':base64.b64encode(hashlib.md5(b'hello world!').digest()).decode(),'validation_expiry_seconds':1},app,root)
            self.assertTrue(signed['ok']);self.assertEqual(cos.signs[-1]['Expired'],1)
            self.assertEqual(uploads_module.SIGN_SECONDS,600)
            self.assertTrue(cos.signs[-1]['Key'].startswith('video-editing/validation/canary_20261022/'))
    def test_external_exception_text_never_reaches_json(self):
        stream=io.StringIO()
        with patch.object(sys,'argv',['validate']),patch.object(sys,'stdin',io.StringIO('{"op":"catalog_products"}')),patch.object(sys,'stdout',stream),\
             patch.object(cli,'load_environment'),patch.object(cli,'load_live_app',side_effect=RuntimeError('SECRET_SIGNED_URL')):
            self.assertEqual(cli.main(),1)
        self.assertNotIn('SECRET',stream.getvalue());self.assertEqual(json.loads(stream.getvalue())['code'],'validation_failed')


if __name__=='__main__':unittest.main()
