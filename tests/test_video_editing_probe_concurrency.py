import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from features.drama_synthesis.async_runtime import AsyncRuntime, emit_progress
from features.video_editing.gpu_integration import mixed_validate, mixed_fingerprint, task_kind, EditingService
from features.video_editing.worker import Dispatcher
import test_video_editing_service as fixture
from test_video_editing_service import ACTOR, OTHER
from test_video_editing_runtime import legacy, editing, wait


class DispatcherConcurrencyTests(unittest.TestCase):
    setUp=fixture.ServiceTests.setUp
    add_source=fixture.ServiceTests.add_source
    def probes(self,n=5):
        ids=[]
        for i in range(n):
            sid='vep_'+('%032x' % (i+1));self.add_source(sid,'episode',status='unprobed')
            ids.append(self.service.enqueue_probe(sid,ACTOR)['data']['probe_task_id'])
        return ids

    def test_three_probes_fill_once_poll_together_and_refill(self):
        ids=self.probes();calls=[];barrier=threading.Barrier(3)
        def transport(method,path,payload):
            tid=payload['job_id'] if payload else path.rsplit('/',1)[1]
            calls.append((method,tid))
            if method=='POST':barrier.wait(3)
            return {'job_id':tid,'status':'running','stage':'downloading','progress':{'download_bytes':25,'total_bytes':100}}
        worker=Dispatcher(self.service,transport,probe_concurrency=3)
        worker.tick();self.assertEqual(sum(m=='POST' for m,_ in calls),3)
        worker.tick();self.assertEqual(sum(m=='POST' for m,_ in calls),3)
        self.assertEqual(sum(m=='GET' for m,_ in calls),3)
        with self.store.transaction() as db:self.store.update('task',ids[0],{'status':'completed'},db)
        worker.transport=lambda m,p,b: {'job_id':b['job_id'] if b else p.rsplit('/',1)[1],'status':'running'}
        worker.tick()
        self.assertEqual(sum(r['status']=='running' for r in self.store.list('task')[0]),3)
        self.assertEqual(sum(r['status']=='queued' for r in self.store.list('task')[0]),1)

    def test_unknown_probe_blocks_new_admission_and_shared_dto_is_safe(self):
        ids=self.probes();worker=Dispatcher(self.service,lambda *a:None,probe_concurrency=3)
        worker.tick();worker.tick()
        self.assertEqual(sum(r['status']=='queued' for r in self.store.list('task')[0]),2)
        row=self.store.get('task',ids[0]);d=row['data']
        d.update(stage='downloading',progress={'download_bytes':20,'total_bytes':100,'secret':'hidden','download_bps':float('inf')})
        # Persist only JSON-compatible finite metrics; the projection also rejects booleans.
        d['progress']['download_bps']=True
        with self.store.transaction() as db:self.store.update('task',ids[0],{'status':'running','data':d},db)
        source=self.store.get('source',d['source_id']);dto=self.service.source_dto(source,OTHER)['probe_progress']
        self.assertEqual(dto['download_bytes'],20);self.assertNotIn('download_bps',dto)
        self.assertNotIn('secret',json.dumps(dto));self.assertNotIn('payload',dto)
        self.assertRaises(Exception,self.service.task,ids[0],OTHER)



if __name__=='__main__':unittest.main()
