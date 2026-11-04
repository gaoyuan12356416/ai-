"""Node portion of deployed 940aa14f lease fencing regression; coordinator unchanged."""
import datetime as dt
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from features.fb_gpu.job_lock import exclusive_job
from features.fb_gpu.render_process import run_render
from features.pipeline_dispatch.worker import ExecutionContext, LeaseLost


def locked_write(root, events):
    with exclusive_job(Path(root)/'job.lock'):
        events.put(('start',time.monotonic()))
        time.sleep(.2)
        events.put(('end',time.monotonic()))


class LeaseFenceTests(unittest.TestCase):
    def test_process_lock_never_overlaps_and_keeps_inode(self):
        with tempfile.TemporaryDirectory() as root:
            ctx=multiprocessing.get_context('spawn');q=ctx.Queue()
            ps=[ctx.Process(target=locked_write,args=(root,q)) for _ in range(2)]
            for p in ps:p.start()
            events=[q.get(timeout=20) for _ in range(4)]
            for p in ps:p.join(10);self.assertEqual(p.exitcode,0)
            self.assertEqual([x[0] for x in sorted(events,key=lambda x:x[1])],['start','end','start','end'])
            self.assertTrue((Path(root)/'job.lock').is_file())

    def test_cancel_kills_and_reaps_real_child(self):
        with tempfile.TemporaryDirectory() as root:
            output=Path(root)/'output';calls=[0];created=[];real=subprocess.Popen
            def popen(*a,**kw):
                p=real(*a,**kw);created.append(p);return p
            def check():
                calls[0]+=1
                if calls[0]>=4:raise LeaseLost('lease expired')
            with patch('features.fb_gpu.render_process.subprocess.Popen',side_effect=popen):
                with self.assertRaises(LeaseLost):
                    run_render([sys.executable,'-c','import time; time.sleep(60)',str(output)],progress_check=check,poll_seconds=.02)
            self.assertEqual(len(created),1);self.assertIsNotNone(created[0].poll())

    def test_expired_before_launch_never_starts_child(self):
        def reject():raise LeaseLost()
        with patch('features.fb_gpu.render_process.subprocess.Popen') as start:
            with self.assertRaises(LeaseLost):run_render(['fake','output'],progress_check=reject)
            start.assert_not_called()

    def test_render_deadline_does_not_wait_for_heartbeat_rpc(self):
        now=[0.0];job={'id':1,'generation':1,'claim_token':'x','node_id':'n','worker_id':'w'}
        context=ExecutionContext(job,None,None,None,monotonic=lambda:now[0]);context._heartbeat_uncertain=True
        now[0]=119;context.check_render()
        now[0]=120
        with self.assertRaises(LeaseLost):context.check_render()

    def test_cpu_layers_and_filters_are_bounded_without_changing_encoder_quality(self):
        from features.fb_gpu.prepare_worker import software_command
        original=['ffmpeg','-i','source','-loop','1','-i','border','-c:v','libvpx-vp9','-i','opacity','-i','corners','-i','tint','-c:v','h264_nvenc','-preset','p5','-rc','vbr','-cq','21','output.mp4']
        result=software_command(original)
        self.assertEqual(result[result.index('-filter_complex_threads')+1],'2')
        positions=[i for i,x in enumerate(result) if x=='-i']
        self.assertTrue(all(result[i-2:i]==['-threads','1'] for i in positions[1:]))
        self.assertEqual(result[-5:],['-threads','4','-crf','21','output.mp4'])
        self.assertEqual(original[1:3],['-i','source'])


if __name__=='__main__':unittest.main()
