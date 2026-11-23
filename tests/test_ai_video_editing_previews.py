import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from features.ai_video_editing import previews, studio
from features.ai_video_editing.runtime import Runtime, Fault, probe


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.rt=Runtime(Path(self.tmp.name)/'private');studio.setup(self.rt)
        self.actor={'tenant_key':'preview-test','user_id':'u'}
        raw=Path(self.tmp.name)/'source.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=1080x1920:rate=40:duration=1.2',
                        '-f','lavfi','-i','sine=frequency=400:duration=1.2','-c:v','libx264','-preset','ultrafast',
                        '-threads','2','-c:a','aac','-shortest',str(raw)],check=True,capture_output=True)
        self.sha=hashlib.sha256(raw.read_bytes()).hexdigest()
        self.a=self.rt.import_asset(raw,self.sha,'Preview.mp4','Fixture',self.actor)

    def test_poster_first_private_bounded_proxy_and_idempotency(self):
        previews.request(self.rt,self.actor,self.a['id']);previews.request(self.rt,self.actor,self.a['id'])
        job=previews.claim(self.rt);previews.process(self.rt,job)
        meta=previews.metadata(self.rt,self.actor,self.a['id'])
        self.assertTrue(meta['poster_url']);self.assertFalse(meta['proxy_url'])
        job=previews.claim(self.rt);previews.process(self.rt,job)
        path,mime,_=previews.file(self.rt,self.actor,self.a['id'],'preview')
        p=probe(path)
        self.assertLessEqual(max(p['width'],p['height']),854)
        self.assertLessEqual(abs(p['duration_ms']-self.a['duration_ms']),150)
        self.assertTrue(p['has_audio']);self.assertEqual(mime,'video/mp4')
        streams=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(path)]))['streams']
        video=next(s for s in streams if s['codec_type']=='video')
        self.assertEqual(video['codec_name'],'h264');self.assertEqual(video['pix_fmt'],'yuv420p')
        num,den=map(int,video['avg_frame_rate'].split('/'));self.assertLessEqual(num/den,30)
        raw=path.read_bytes();self.assertLess(raw.index(b'moov'),raw.index(b'mdat'))
        self.assertEqual(hashlib.sha256(Path(self.a['path']).read_bytes()).hexdigest(),self.sha)
        self.assertIsNone(previews.claim(self.rt))
        with self.assertRaises(Fault):previews.file(self.rt,{'tenant_key':'other','user_id':'u'},self.a['id'],'preview')

    def test_preview_failure_keeps_original_and_can_retry(self):
        previews.request(self.rt,self.actor,self.a['id'])
        with patch.object(previews.subprocess,'run',side_effect=RuntimeError('fixture')):
            previews.process(self.rt,previews.claim(self.rt))
        self.assertEqual(previews.metadata(self.rt,self.actor,self.a['id'])['preview_status'],'failed')
        self.assertTrue(Path(self.rt.asset(self.a['id'],self.actor)['path']).is_file())
        previews.request(self.rt,self.actor,self.a['id'],retry=True)
        self.assertEqual(previews.metadata(self.rt,self.actor,self.a['id'])['preview_status'],'queued')


if __name__=='__main__':unittest.main()
