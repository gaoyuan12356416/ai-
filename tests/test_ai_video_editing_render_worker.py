import hashlib
import math
from http.server import ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from features.ai_video_editing import render_worker as worker
from features.ai_video_editing.runtime import Runtime


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'needs ffmpeg and ffprobe')
class IndependentRenderTests(unittest.TestCase):
    def test_variants_enlarge_center_and_keep_identical_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'pattern.mp4'
            subprocess.run(['ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i',
                'color=black:s=180x320:r=25:d=2,drawbox=x=57:y=80:w=66:h=160:c=white:t=fill',
                '-f', 'lavfi', '-i', 'sine=f=400:d=2', '-c:v', 'mpeg4', '-c:a', 'aac', str(source)], check=True)
            results = []
            for z in (100,104):
                target = root / ('out%d.mp4' % z)
                clip = {'source_id':'pattern', 'start_ms':115, 'end_ms':1730}
                if z != 100:
                    clip['zoom_percent'] = z
                receipt = worker.render({'clips':[clip]}, {'pattern':source}, target, root / ('r%d' % z), encoder='libx264')
                def data(options):
                    return subprocess.check_output(['ffmpeg', '-loglevel', 'error', '-i', str(target), *options, '-'])
                bitstream = data(['-map','0:a:0','-c:a','copy','-f','adts'])
                pcm = data(['-map','0:a:0','-f','s16le'])
                frame = data(['-map','0:v:0','-frames:v','1','-pix_fmt','gray','-f','rawvideo'])
                width = sum(value > 128 for value in frame[640 * 720:641 * 720])
                results.append((receipt['frames'], hashlib.sha256(bitstream).digest(), hashlib.sha256(pcm).digest(),
                                hashlib.sha256(frame).digest(), width))
            base, zoomed = results
            self.assertEqual(base[:3], zoomed[:3])
            self.assertNotEqual(base[3], zoomed[3])
            self.assertAlmostEqual(zoomed[4] / base[4], 1.04, delta=.012)

    def test_real_http_render_silent_vfr_fractional_cuts_and_lost_completion(self):
        from features.ai_video_editing.http_api import Handler
        actor = {'user_id': 'operator', 'tenant_key': 'one'}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = Runtime(root / 'cpu')
            gpu = root / 'gpu'
            gpu.mkdir()
            def create(name, video, flags):
                path = root / (name + '.mp4')
                subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', video,
                                *flags, '-c:v', 'mpeg4', '-y', str(path)], check=True)
                return runtime.import_asset(path, worker.fingerprint(path), name, '', actor)['id']
            one = create('one', 'testsrc2=s=160x96:d=4:r=24', ['-f', 'lavfi', '-i', 'sine=f=700:d=4', '-c:a', 'aac'])
            two = create('two', 'testsrc2=s=96x144:d=4:r=15', ['-vf', "select='not(mod(n,3))+lt(t,2)'", '-vsync', 'vfr'])
            clips = [{'source_id': one, 'start_ms': 2157, 'end_ms': 3380, 'fit': 'cover', 'mirror': True},
                     {'source_id': two, 'start_ms': 1550, 'end_ms': 2910, 'fit': 'contain'},
                     {'source_id': one, 'start_ms': 28, 'end_ms': 1050, 'mute': True}]
            project = runtime.save(actor, {'operation_id': 'create_test', 'name': '独立剪辑', 'units': [
                {'id': 'test', 'title': '三段', 'clips': clips}]})['item']
            runtime.render(project['id'], actor, {'operation_id': 'render_test', 'expected_version': project['version']})
            class QuietHandler(Handler):
                def log_message(self, *args):
                    pass
            with ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler) as http:
                http.runtime = runtime
                http.worker_token = 'test-local-token'
                thread = threading.Thread(target=http.serve_forever, daemon=True)
                thread.start()
                try:
                    api = worker.InternalAPI('http://127.0.0.1:%d/api/ai-video-editing/internal' % http.server_port,
                                             http.worker_token)
                    job = api.post('/claim')['item']
                    real_complete = api.complete
                    def disconnected(*args):
                        real_complete(*args)
                        raise worker.TransferError('lost response')
                    with patch.object(api, 'complete', side_effect=disconnected):
                        worker.run_job(api, job, gpu, 'libx264', 'ffmpeg', 'ffprobe')
                    self.assertEqual(len(list(gpu.glob('render-*/pending.json'))), 1)
                    with patch.object(api, 'complete', side_effect=AssertionError('duplicate upload')):
                        worker.reconcile(api, gpu)
                    self.assertEqual(list(gpu.iterdir()), [])
                    row = runtime.list_records('tasks', actor, job['id'])['item']
                    self.assertEqual(row['status'], 'completed')
                    target = runtime.root / 'outputs' / (job['id'] + '.mp4')
                    video, audio, duration = worker.inspect(target, 'ffprobe')
                    frames = sum(worker.frame(c['end_ms']) - worker.frame(c['start_ms']) for c in clips)
                    self.assertEqual(int(video['nb_frames']), frames)
                    self.assertAlmostEqual(duration, frames * 1000 / 30, delta=1)
                    self.assertEqual(audio['channels'], 2)
                    def sample(seconds):
                        result = subprocess.check_output(['ffmpeg', '-loglevel', 'error', '-ss', str(seconds), '-i', str(target),
                            '-t', '0.25', '-vn', '-ac', '1', '-ar', '8000',
                            '-f', 's16le', '-'])
                        values = [int.from_bytes(result[i:i+2], 'little', signed=True) for i in range(0,len(result),2)]
                        return math.sqrt(sum(x*x for x in values) / max(1,len(values)))
                    self.assertGreater(sample(.3), 500)
                    self.assertLess(sample(1.8), 5)
                    self.assertLess(sample(3.1), 5)
                finally:
                    http.shutdown()
                    thread.join(2)


if __name__ == '__main__':
    unittest.main()
