import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from features.ai_video_editing import smart_api, smart_media, smart_plans
from features.ai_video_editing.smart_common import SmartError


ACTOR = {'user_id': 'one', 'tenant_key': 'org'}
OTHER = {'user_id': 'two', 'tenant_key': 'org'}


class Runtime:
    def __init__(self, root, file):
        self.db_path = root / 'independent.sqlite'
        self.assets = {sid: {'id': sid, 'name': 'Episode', 'path': str(file), 'duration_ms': 12000,
                             'has_audio': True, 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}
                       for sid in ('source_a', 'source_b')}

    def asset(self, aid, actor):
        if actor['user_id'] != 'one' or aid not in self.assets:
            raise SmartError('asset_not_found', '素材不存在', 404)
        return self.assets[aid]


@unittest.skipUnless(shutil.which('ffmpeg'), 'needs ffmpeg')
class SmartEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.file = cls.root / 'fixture.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                        '-f', 'lavfi', '-i', 'color=c=black:s=96x96:r=15:d=4',
                        '-f', 'lavfi', '-i', 'color=c=white:s=96x96:r=15:d=4',
                        '-f', 'lavfi', '-i', 'testsrc2=s=96x96:r=15:d=4',
                        '-f', 'lavfi', '-i', 'sine=f=800:d=12', '-filter_complex',
                        '[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]', '-map', '[v]', '-map', '3:a:0',
                        '-c:v', 'mpeg4', '-c:a', 'aac', str(cls.file)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.runtime = Runtime(Path(self.work.name), self.file)
        smart_api.setup(self.runtime)

    def test_real_pixels_audio_and_only_known_evidence_can_be_labeled(self):
        def visual(sheets):
            self.assertTrue(sheets[0][0].read_bytes().startswith(b'\xff\xd8'))
            rows = sheets[0][1]
            return [{'id': rows[0]['candidate_id'], 'tag': 'hook', 'reason': '画面切换', 'score': .9,
                     'category': '对照', 'start_ms': -987, 'end_ms': 24000, 'transcript': ''},
                    {'id': 'made_up', 'tag': 'hook', 'reason': 'no', 'score': .9}]
        real = smart_media.analyze_file(self.file, 12000, True, self.work.name, visual)
        self.assertEqual(real['engine'], 'multimodal')
        self.assertEqual(real['visual_candidates'], 1)
        self.assertTrue(any('RMS' in e['evidence'] for e in real['candidates']))
        self.assertTrue(any('画面变化' == e['category'] or e['anchor_ms'] in (4000, 8000)
                            for e in real['candidates']))
        self.assertTrue(all(0 <= e['start_ms'] < e['end_ms'] <= 12000 for e in real['candidates']))

    def test_recovery_authorization_and_independent_drafts(self):
        with patch.object(smart_api, 'wake'), patch.dict(os.environ, {'AI_VIDEO_EDITING_VISION_BASE_URL': '',
                                                                     'AI_VIDEO_EDITING_CODEX_BIN': ''}):
            _, initial = smart_api.dispatch(self.runtime, ACTOR, 'POST', '/analyses', {'source_ids': ['source_a', 'source_b']})
            _, duplicate = smart_api.dispatch(self.runtime, ACTOR, 'POST', '/analyses', {'source_ids': ['source_a', 'source_b']})
            self.assertEqual(initial['analysis_id'], duplicate['analysis_id'])
            jid = initial['analysis_id']
            with self.assertRaises(SmartError):
                smart_api.dispatch(self.runtime, OTHER, 'GET', '/analyses/' + jid, {})
            self.assertIsNotNone(smart_api.claim(self.runtime))
            self.assertIsNone(smart_api.claim(self.runtime))
            with smart_api.transaction(self.runtime) as conn:
                conn.execute('UPDATE ai_edit_smart_analysis SET expires=0 WHERE id=?', (jid,))
            self.assertTrue(smart_api.execute_one(self.runtime))
            _, done = smart_api.dispatch(self.runtime, ACTOR, 'GET', '/analyses/' + jid, {})
            self.assertEqual(done['status'], 'succeeded', done)
            self.assertEqual(done['sources'][0]['engine'], 'signal')
            self.assertNotIn('path', json.dumps(done))
            for method in smart_plans.METHODS:
                payload = {'analysis_id': jid, 'method': method, 'merge': False, 'script_count': 3, 'variant_count': 5}
                if method != 'hook_first':
                    payload['target_duration_ms'] = 6500
                status, plan = smart_api.dispatch(self.runtime, ACTOR, 'POST', '/api/ai-video-editing/plans', payload)
                self.assertEqual((status, len(plan['outputs'])), (200, 30))
                self.assertEqual([len(d['units']) for d in plan['drafts']], [20, 10])
                for output in plan['outputs']:
                    self.assertEqual(sum(seg['end_ms'] - seg['start_ms'] for seg in output['segments']), output['duration_ms'])
                    self.assertTrue(all(0 <= seg['start_ms'] < seg['end_ms'] <= 12000 for seg in output['segments']))
            _, merged = smart_api.dispatch(self.runtime, ACTOR, 'POST', '/plans',
                                           {'analysis_id': jid, 'method': 'hook_first', 'merge': True})
            self.assertEqual([s['source_id'] for s in merged['outputs'][0]['segments'][1:]], ['source_a', 'source_b'])
            with self.assertRaises(SmartError):
                smart_api.dispatch(self.runtime, ACTOR, 'POST', '/plans', {'analysis_id': jid, 'method': 'golden_three',
                                                                           'start_ms': 100})
            self.runtime.assets['source_a']['sha256'] = '0' * 64
            with self.assertRaises(SmartError) as changed:
                smart_api.dispatch(self.runtime, ACTOR, 'POST', '/plans', {'analysis_id': jid, 'method': 'hook_first'})
            self.assertEqual(changed.exception.status, 409)


if __name__ == '__main__':
    unittest.main()
