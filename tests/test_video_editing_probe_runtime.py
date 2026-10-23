import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from features.drama_synthesis.async_runtime import AsyncRuntime, emit_progress
from features.video_editing.gpu_integration import mixed_validate, mixed_fingerprint, EditingService
from test_video_editing_runtime import legacy, editing, wait

class RuntimeProbeConcurrencyTests(unittest.TestCase):
    def test_three_downloads_overlap_render_stays_single_and_restart_reuses(self):
        finish=threading.Event();lock=threading.Lock();active={'probe':0,'render':0};peak=dict(active);calls=[]
        def execute(p):
            kind='probe' if p.get('operation')=='probe' else 'render'
            with lock:active[kind]+=1;peak[kind]=max(peak[kind],active[kind]);calls.append(p['job_id'])
            emit_progress('downloading',download_bytes=30,total_bytes=100)
            finish.wait(5)
            with lock:active[kind]-=1
            return {'job_id':p['job_id'],'ok':True}
        kwargs=dict(validate_payload=mixed_validate,fingerprint=mixed_fingerprint,parallel_workers=3,
                    parallel_predicate=lambda p:p.get('operation')=='probe',public_result=lambda r,p:r)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            runtime=AsyncRuntime(root,execute,lambda p:None,autostart=False,**kwargs)
            self.addCleanup(runtime.close)
            for n in '1234':
                p=editing(n);p['operation']='probe';p['recipe']['clips']=[];runtime.submit(p)
            runtime.submit(legacy('render1'));runtime.submit(legacy('render2'));runtime.start()
            wait(lambda:len(calls)==4)
            self.assertEqual(peak,{'probe':3,'render':1})
            self.assertEqual(runtime.get('ve_'+'4'*32)['status'],'queued')
            self.assertEqual(runtime.get('ve_'+'1'*32)['progress']['download_bytes'],30)
            finish.set();wait(lambda:all(r['status']=='completed' for r in runtime._records.values()))
            self.assertEqual(peak,{'probe':3,'render':1});self.assertEqual(len(calls),6)
            runtime.close()
            again=AsyncRuntime(root,execute,lambda p:None,**kwargs)
            self.assertEqual(again.get('ve_'+'1'*32)['generation'],1)
            again.submit(editing_probe('1'));self.assertEqual(len(calls),6);again.close()

    def test_probe_ffmpeg_waits_for_shared_render_slot(self):
        slot=threading.BoundedSemaphore(1);slot.acquire();called=threading.Event()
        service=EditingService(None,None,render_slots=slot)
        with tempfile.TemporaryDirectory() as root, patch('features.video_editing.gpu_integration.load_completed',return_value=None), patch('features.video_editing.gpu_integration.save_completed'), patch('features.video_editing.gpu_integration.run_ffmpeg',side_effect=lambda *a,**k:called.set()):
            t=threading.Thread(target=lambda:service._local_render(Path(root)/'a',Path(root)/'s',{},[],1,probe=True))
            t.start();self.assertFalse(called.wait(.1));slot.release();t.join(2);self.assertTrue(called.is_set())


def editing_probe(n):
    p=editing(n);p['operation']='probe';p['recipe']['clips']=[];return p


if __name__=='__main__':unittest.main()

