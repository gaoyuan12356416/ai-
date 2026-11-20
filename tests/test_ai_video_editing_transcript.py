import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import wave
from unittest.mock import patch

from features.ai_video_editing import language_media as media, studio, transcript_jobs
from features.ai_video_editing.runtime import Runtime, Fault, owner, packed
from features.ai_video_editing.smart_common import SmartError


class ChunkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = self.root/'model'
        self.model.touch()

    def asr(self, duration, ranges=None, fail=False):
        windows = []
        def decode(argv, **kwargs):
            windows.append((argv[argv.index('-ss')+1], argv[argv.index('-t')+1]))
            with wave.open(argv[-1], 'wb') as wav:
                wav.setparams((1,2,16000,0,'NONE','not compressed'))
                wav.writeframes(b'\x10\x00'*round(float(windows[-1][1])*16000))
        def recognize(argv, **kwargs):
            result = Path(argv[argv.index('-of')+1]+'.srt')
            if fail and len(windows) == 2:
                return subprocess.CompletedProcess(argv, 1, b'')
            result.write_text('1\n00:00:00,000 --> 00:01:05,000\nHello\n')
            return subprocess.CompletedProcess(argv, 0, b'')
        with patch.object(media, 'probe', return_value=(duration, [{'codec_type':'audio'}])), \
             patch.object(media.shutil, 'which', return_value='whisper'), \
             patch.dict(os.environ, {'AI_VIDEO_EDITING_WHISPER_MODEL':str(self.model)}), \
             patch.object(media, 'command', side_effect=decode), \
             patch.object(media, '_process', side_effect=recognize):
            result = media.transcribe('private.mp4', ranges=ranges, temp_dir=self.root)
        return result, windows

    def test_full_long_asr_bounds_tail_and_deduplicates_boundary(self):
        result, windows = self.asr(1072)
        self.assertEqual(len(windows), 18)
        self.assertEqual(windows[-1], ('1020.000', '52.000'))
        self.assertTrue(all(float(length) <= 60 for _, length in windows))
        self.assertEqual(result['cues'], [{'start':0, 'end':1072000, 'text':'Hello'}])

    def test_late_disjoint_and_overlapping_asr_uses_only_union(self):
        result, windows = self.asr(1072, [(1000000,1002000), (1001000,1004000), (1069000,1072000)])
        self.assertEqual(windows, [('1000.000','4.000'), ('1069.000','3.000')])
        self.assertEqual([(c['start'],c['end']) for c in result['cues']], [(1000000,1004000),(1069000,1072000)])

    def test_failed_middle_chunk_is_not_partial_success(self):
        with self.assertRaises(SmartError) as error:
            self.asr(125, fail=True)
        self.assertIn('60.000–120.000', error.exception.message)

    def test_silent_asr_does_not_trigger_ocr_or_fail(self):
        def empty(argv, **kw):
            Path(argv[argv.index('-of')+1]+'.srt').touch()
            return subprocess.CompletedProcess(argv, 0, b'')
        with patch.object(media, 'probe', return_value=(1072, [{'codec_type':'audio'}])), \
             patch.object(media.shutil, 'which', return_value='whisper'), \
             patch.dict(os.environ, {'AI_VIDEO_EDITING_WHISPER_MODEL':str(self.model)}), \
             patch.object(media, 'command'), patch.object(media, '_audio_samples', return_value=[100]*960000), \
             patch.object(media, '_process', side_effect=empty), \
             patch.object(media, '_hard_captions', side_effect=AssertionError('no fallback')):
            self.assertEqual(media.transcribe('private.mp4', ranges=[(1000000,1002000)], temp_dir=self.root)['cues'], [])

    def test_ocr_gets_short_windows_and_keeps_absolute_times(self):
        with patch.object(media, 'probe', return_value=(1072, [])), \
             patch.object(media.shutil, 'which', return_value=None), \
             patch.object(media, '_hard_captions', side_effect=lambda p,a,b,l,r: [{'start':0,'end':b-a,'text':'Caption'}]) as ocr:
            result = media.transcribe('private.mp4', ranges=[(990000,1052000),(1069000,1072000)], temp_dir=self.root)
        self.assertEqual([(c.args[1],c.args[2]) for c in ocr.call_args_list], [(990000,1050000),(1050000,1052000),(1069000,1072000)])
        self.assertEqual([(c['start'],c['end']) for c in result['cues']], [(990000,1052000),(1069000,1072000)])

    def test_invalid_selection_never_falls_back_to_whole_source(self):
        for ranges in [[], [(1072000,1073000)], [(4,2)], [(1.1,5)]]:
            with self.assertRaises(SmartError):
                media.selected_ranges(ranges, 1072000)

    def test_digital_silence_does_not_invent_captions(self):
        with patch.object(media, 'probe', return_value=(1072, [{'codec_type':'audio'}])), \
             patch.object(media.shutil, 'which', return_value='whisper'), \
             patch.dict(os.environ, {'AI_VIDEO_EDITING_WHISPER_MODEL':str(self.model)}), \
             patch.object(media, 'command'), patch.object(media, '_audio_samples', return_value=[0]*32000), \
             patch.object(media, '_process', side_effect=AssertionError('silent audio needs no model')):
            self.assertEqual(media.transcribe('private.mp4',ranges=[(1000000,1002000)],temp_dir=self.root)['cues'],[])


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires FFmpeg')
class EmbeddedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.path = cls.root/'long.mp4'
        (cls.root/'captions.srt').write_text('1\n00:00:58,000 --> 00:01:02,000\nBoundary\n\n2\n00:16:40,000 --> 00:16:45,000\nLate\n\n3\n00:17:48,000 --> 00:17:52,000\nTail\n')
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=s=320x240:r=1:d=1072',
                        '-i',str(cls.root/'captions.srt'),'-c:v','libx264','-preset','ultrafast',
                        '-c:s','mov_text','-t','1072',str(cls.path)],check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_real_long_embedded_track_boundary_and_tail(self):
        result = media.transcribe(self.path, temp_dir=self.root)
        self.assertEqual(result['chunks'],18)
        self.assertEqual([(c['start'],c['end']) for c in result['cues']], [(58000,62000),(1000000,1005000),(1068000,1072000)])

    def test_real_seek_straddling_cue_disjoint_and_empty(self):
        result = media.transcribe(self.path, ranges=[(1001000,1004000),(1069000,1072000)], temp_dir=self.root)
        self.assertEqual([(c['start'],c['end']) for c in result['cues']], [(1001000,1004000),(1069000,1072000)])
        self.assertEqual(media.transcribe(self.path,ranges=[(900000,901000)],temp_dir=self.root)['cues'],[])

    def test_legacy_language_generate_contract_on_long_media(self):
        from features.ai_video_editing.language_tools import generate
        result=generate({'mode':'transcribe','source_language':'auto'},media_path=self.path)
        self.assertEqual((result['mode'],result['source'],result['chunks']),('transcribe','embedded',18))
        self.assertEqual(len(result['cues']),3)

    def test_studio_plan_union_reuse_and_output_order(self):
        rt = Runtime(self.root/'studio')
        actor = {'tenant_key':'test','user_id':'test'}
        with rt.db(True) as db:
            db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?,?,?,?,?,?)',
                       ('aa_'+'1'*32, owner(actor), 'fixture', '',0,'a'*64,packed({'duration_ms':1072000}),0))
        asset = rt.asset('aa_'+'1'*32, actor)
        shutil.copyfile(self.path,asset['path'])
        sid = asset['id']
        clips = [{'source_id':sid,'start_ms':1069000,'end_ms':1072000},
                 {'source_id':sid,'start_ms':1001000,'end_ms':1004000}]
        plan = {'drafts':[{'units':[{'id':'a','clips':clips},{'id':'b','clips':clips},
                                  {'id':'c','clips':[{'source_id':sid,'start_ms':1002000,'end_ms':1006000}]}]}],'warnings':[]}
        config = {'merge':True,'clip':{'method':'hook_first','script_count':1,'variant_count':2,'seconds':None},
                  'translation':{'source_language':'auto','language':'en','mode':'bilingual','style':{},'speech':False}}
        def translate(payload):
            return {'cues':[{**c,'text':'Translated'} for c in payload['cues']]}
        with patch.object(studio,'build_plans',return_value=plan), patch.object(media,'transcribe',wraps=media.transcribe) as read, \
             patch('features.ai_video_editing.language_tools.generate',side_effect=translate):
            units,_ = studio.prepare(rt,actor,{'sources':[],'analysis_id':'test'},config)
        self.assertEqual(read.call_count,1)
        self.assertEqual([(c['start'],c['end'],c['text']) for c in units[0]['composition']['cues']],
                         [(0,3000,'Tail\nTranslated'),(3000,6000,'Late\nTranslated')])
        self.assertEqual(units[0]['composition'],units[1]['composition'])
        self.assertEqual([(c['start'],c['end']) for c in units[2]['composition']['cues']],[(0,3000)])


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rt = Runtime(self.tmp.name)
        transcript_jobs.setup(self.rt)
        self.actor = {'tenant_key':'test','user_id':'one'}
        self.task = 'at_'+'1'*32
        with self.rt.db(True) as db:
            db.execute("INSERT INTO tasks(id,owner,project,version,unit_id,unit_data,status,created) VALUES(?,?,?,1,'unit',?,'completed',0)",
                       (self.task,owner(self.actor),'project',packed({'title':'fixture','clips':[]})))

    def test_persisted_receipt_progress_and_owner(self):
        body={'task_id':self.task,'source_language':'en'}
        job=transcript_jobs.submit(self.rt,self.actor,body)
        self.assertEqual(job,transcript_jobs.submit(self.rt,self.actor,body))
        def recognize(*args, **kwargs):
            kwargs['progress'](1,2)
            row=transcript_jobs.receipt(self.rt,self.actor,job['id'])
            self.assertEqual((row['status'],row['done'],row['total']),('running',1,2))
            with self.rt.db() as db:
                self.assertGreater(db.execute('select expires from transcript_jobs').fetchone()[0],__import__('time').time())
            return {'cues':[],'chunks':2}
        with patch.object(media,'transcribe',side_effect=recognize):
            self.assertTrue(transcript_jobs.tick(self.rt))
        self.assertEqual(transcript_jobs.receipt(self.rt,self.actor,job['id'])['status'],'completed')
        with self.assertRaises(Fault):
            transcript_jobs.receipt(self.rt,{'user_id':'other'},job['id'])
        self.assertFalse(transcript_jobs.tick(self.rt))

    def test_failure_is_visible_and_no_result_is_returned(self):
        job=transcript_jobs.submit(self.rt,self.actor,{'task_id':self.task})
        with patch.object(media,'transcribe',side_effect=SmartError('failed','Chunk failed')):
            transcript_jobs.tick(self.rt)
        result=transcript_jobs.receipt(self.rt,self.actor,job['id'])
        self.assertEqual((result['status'],result['error'],result['result']),('failed','Chunk failed',None))
