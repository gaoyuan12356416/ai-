import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from features.ai_video_editing.runtime import Fault, Runtime


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'requires FFmpeg')
class IndependentEditorTest(unittest.TestCase):
    def test_upload_permission_and_idempotent_render_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            rt=Runtime(Path(directory)/'private')
            actor={'tenant_key':'a','user_id':'u'}
            stranger={'tenant_key':'b','user_id':'u'}
            source=Path(directory)/'owned.mp4'
            subprocess.run(['ffmpeg','-loglevel','error','-f','lavfi','-i','color=c=blue:s=720x1280:r=30:d=1',
                '-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t','1','-c:v','libx264','-preset','ultrafast',
                '-c:a','aac',str(source)],check=True)
            digest=hashlib.sha256(source.read_bytes()).hexdigest()
            copy=Path(directory)/'upload'
            shutil.copyfile(source,copy)
            asset=rt.import_asset(copy,digest,'episode.mp4','test',actor,2)
            self.assertEqual(rt.assets(stranger,'',4,0)['total'],0)
            with self.assertRaises(Fault):rt.asset(asset['id'],stranger)
            self.assertEqual(rt.series(actor)['items'][0]['asset_count'],1)
            unit={'id':'unit','title':'episode','clips':[{'source_id':asset['id'],'start_ms':0,'end_ms':1000}]}
            draft={'name':'first','units':[unit],'operation_id':'create_once'}
            project=rt.save(actor,draft)['item']
            self.assertEqual(rt.save(actor,draft)['item']['id'],project['id'])
            payload={'unit_ids':['unit'],'expected_version':1,'operation_id':'render_once'}
            result=rt.render(project['id'],actor,payload)
            self.assertEqual(result,rt.render(project['id'],actor,payload))
            job=rt.claim()
            self.assertIsNone(rt.claim())
            tid,lease=job['id'],job['lease']
            self.assertEqual(rt.worker_asset(tid,lease,asset['id'])['sha256'],digest)
            with self.assertRaises(Fault):rt.worker_asset(tid,'wrong',asset['id'])
            rt.update_job(tid,lease,'complete',source,digest)
            self.assertTrue(rt.completion_receipt(tid,lease,digest))
            with self.assertRaises(Fault):rt.completion_receipt(tid,lease,'different')
            media=rt.list_records('tasks',actor,tid)['item']['output']['preview_url']
            self.assertTrue(media.startswith('/api/ai-video-editing/tasks/'))
            with self.assertRaises(Fault):rt.list_records('tasks',stranger,tid)
