"""Explicit manual admission persists; automatic defaults and in-flight work remain."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest

from features.pipeline_dispatch.core import DispatchStore, DispatchError, stamp
from features.pipeline_dispatch import views


class CapacityModeTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.clock=[dt.datetime(2026,11,3,4,tzinfo=dt.timezone.utc)]
        self.store=DispatchStore(Path(temp.name)/'dispatch.sqlite',clock=lambda:self.clock[0])
        self.admin={'is_admin':True,'user_id':'admin'}
        self.store.channel_enabled('x',True)
        self.refresh()

    def refresh(self, cpu=96, memory=.1):
        for node in ('cpu-main','hk-gpu'):
            self.store.register_node(node,['prepare:x','publish:x','backend:'+('gpu' if node=='hk-gpu' else 'cpu')],
                {'cpu_percent':cpu,'memory_available_fraction':memory,'gpu_percent':99,'encoder_percent':99,'gpu_memory_available_fraction':.05})

    def set(self, stage='prepare', **values):
        return self.store.set_capacity({'host_id':'hk-gpu','stage':stage,'expected_version':self.store.config()['version'],**values},self.admin)

    def test_manual_saved_gpu_capacity_is_not_reduced_under_pressure_and_cpu_remains_auto(self):
        self.set(scaling_mode='manual')
        self.set('publish',scaling_mode='manual')
        expected=self.store.config()
        self.assertEqual(expected['hosts']['hk-gpu'],{'prepare':{'target':2,'ceiling':2,'scaling_mode':'manual'},'publish':{'target':4,'ceiling':4,'scaling_mode':'manual'}})
        for minute in range(6):
            self.clock[0]+=dt.timedelta(minutes=1);self.refresh();self.store.adapt_capacity()
            actual=self.store.config()
            self.assertEqual(actual['hosts']['hk-gpu'],expected['hosts']['hk-gpu'])
            self.assertEqual(actual['channel_limits'],expected['channel_limits'])
            self.assertEqual(actual['global_publish_limit'],expected['global_publish_limit'])
        self.assertEqual(actual['hosts']['cpu-main']['prepare']['target'],1)
        self.assertEqual(actual['hosts']['cpu-main']['prepare'].get('scaling_mode','auto'),'auto')

    def test_existing_ui_ceiling_save_updates_manual_target_without_touching_inflight_claims(self):
        self.set(scaling_mode='manual',max_workers=2)
        claims=[]
        for index in range(3):
            self.store.offer({'source_key':'x:queue:'+str(index),'source_id':str(index),'channel':'x','family':'queue','stage':'prepare','scheduled_at':stamp(self.clock[0]),'capabilities':['prepare:x'],'resource_keys':['x:account:'+str(index)]})
            self.store.worker_heartbeat('hk-gpu',str(index),'prepare')
            if index<2:claims.append(self.store.claim('hk-gpu',str(index),'prepare')['job'])
        before=[self.store.get_job(j['id']) for j in claims]
        self.set(max_workers=1)
        self.assertEqual(self.store.config()['hosts']['hk-gpu']['prepare']['target'],1)
        self.assertIsNone(self.store.claim('hk-gpu','2','prepare')['job'])
        self.assertEqual([self.store.get_job(j['id']) for j in claims],before)
        self.set(max_workers=0);self.assertEqual(self.store.config()['hosts']['hk-gpu']['prepare']['target'],0)
        self.set(max_workers=3);self.assertEqual(self.store.config()['hosts']['hk-gpu']['prepare']['target'],3)
        self.assertIsNotNone(self.store.claim('hk-gpu','2','prepare')['job'])
        self.assertEqual([self.store.get_job(j['id']) for j in claims],before)

    def test_auto_can_be_restored_and_starts_fresh_sustained_pressure_window(self):
        self.refresh(cpu=86,memory=.7)
        self.store.adapt_capacity()
        self.set(scaling_mode='manual',max_workers=2)
        self.clock[0]+=dt.timedelta(minutes=10);self.refresh(cpu=86,memory=.7)
        self.set(scaling_mode='auto')
        for seconds,target in [(0,2),(60,2),(60,1)]:
            self.clock[0]+=dt.timedelta(seconds=seconds);self.refresh(cpu=86,memory=.7);self.store.adapt_capacity()
            self.assertEqual(self.store.config()['hosts']['hk-gpu']['prepare']['target'],target)

    def test_admin_version_and_mode_validation_preserve_config(self):
        before=self.store.config()
        for change,actor in [({'host_id':'hk-gpu','stage':'prepare','scaling_mode':'invalid'},self.admin),
                              ({'scaling_mode':'manual'},self.admin),
                              ({'host_id':'hk-gpu','stage':'prepare','scaling_mode':'manual'},{'is_admin':False}),
                              ({'host_id':'hk-gpu','stage':'prepare','scaling_mode':'manual','expected_version':-1},self.admin)]:
            with self.subTest(change=change),self.assertRaises(DispatchError):
                self.store.set_capacity({'expected_version':before['version'],**change},actor)
            self.assertEqual(self.store.config(),before)

    def test_monitor_dto_reports_manual_and_legacy_auto_without_changing_visibility(self):
        self.set(scaling_mode='manual')
        dto=views.capacity_dto(self.store.config())
        self.assertEqual(next(r for r in dto['hosts'] if r['host_id']=='hk-gpu' and r['stage']=='prepare')['scaling_mode'],'manual')
        self.assertTrue(all(r['scaling_mode']=='auto' for r in dto['hosts'] if r['host_id']=='cpu-main'))
        actor={'is_admin':True,'user_id':'admin','allowed_channels':['x'],'allowed_modules':['x_accounts']}
        result=views.query(self.store,'workers',actor,{})
        gpu=next(h for h in result['items'] if h['host_id']=='hk-gpu')
        self.assertEqual(gpu['stages'][0]['scaling_mode'],'manual')
        personal=views.query(self.store,'workers',{**actor,'is_admin':False},{})
        self.assertIsNone(personal['capacity']);self.assertEqual(personal['items'],[])
        views.invalidate_cache(self.store)


if __name__=='__main__':unittest.main()
