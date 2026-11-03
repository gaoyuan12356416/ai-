import datetime as dt
import contextlib
import json
import tempfile
import unittest
from pathlib import Path

from features.pipeline_dispatch.backlog_contract import source_fingerprint
from features.pipeline_dispatch.backlog_release import commit_backlog_item, offer_disposition
from features.pipeline_dispatch.core import DispatchStore, stamp


class BacklogReleaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.now = dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(Path(tmp.name)/'dispatch.db', clock=lambda: self.now)
        self.store.channel_enabled('x', True)
        self.identity = dict(source_key='x:auto:1', id='1', channel='x', family='auto', account_id='7',
                             scheduled_at='2026-11-02T01:00:00.000000+00:00', created_at='2026-11-01T00:00:00.000000+00:00')
        self.proof = dict(source_key='x:auto:1', action='resume', plan_sha256='a'*64,
                          source_fingerprint=source_fingerprint(self.identity), identity=self.identity)
        self.offer = dict(source_key='x:auto:1', source_id='1', channel='x', family='auto', batch_key='run1', stage='prepare',
                          scheduled_at='2026-11-02T01:00:00+00:00', source_created_at='2026-11-01T00:00:00+00:00',
                          resource_keys=['x:account:7'], capabilities=['prepare:x'], source_status='pending',
                          execution_available=True, status='queued', payload={'frozen':'value'})
        job = self.store.offer(self.offer)
        with self.store.transaction() as c:
            cfg=self.store.config(c);cfg['cutovers']['x']=stamp(self.now);self.store._save_config(c,cfg,{},'test')
            c.execute("UPDATE pipeline_job SET status='backlog_paused',hold_reason='cutover_overdue' WHERE id=?",(job['id'],))
            frozen=dict(c.execute('SELECT * FROM pipeline_job WHERE id=?',(job['id'],)).fetchone())
        self.item = dict(source_key='x:auto:1', action='resume', ghost=False, identity=self.identity,
                         source_fingerprint=source_fingerprint(self.identity), requested_stage='prepare',
                         current_offer={**self.offer,'backlog_authorization':self.proof}, paused_jobs=[frozen])

    def commit(self, item=None):
        with self.store.transaction() as c:
            return commit_backlog_item(c,item or self.item,'a'*64,stamp(self.now))

    def test_resume_keeps_schedule_generation_and_claims_once_across_nodes(self):
        self.commit()
        for node in ('cpu-main','hk-gpu'):
            self.store.register_node(node,['prepare:x']);self.store.worker_heartbeat(node,'p','prepare')
        a=self.store.claim('cpu-main','p','prepare')['job']; b=self.store.claim('hk-gpu','p','prepare')['job']
        self.assertEqual(a['source_key'],'x:auto:1');self.assertIsNone(b)
        self.assertEqual(a['generation'],1);self.assertEqual(a['scheduled_at'],stamp(self.offer['scheduled_at']))
        self.assertTrue(self.commit()['already_committed'])

    def test_archive_does_not_become_success_or_reappear_in_another_phase(self):
        self.item['action']='archive';self.commit()
        self.assertEqual(self.store.get_job(self.item['paused_jobs'][0]['id'])['status'],'archived')
        self.assertIsNone(self.store.offer({**self.offer,'stage':'publish'}))
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_publication').fetchone()[0],0)
        self.assertEqual(self.store.offer({**self.offer,'status':'completed'})['status'],'archived')

    def test_central_proof_alone_cannot_exempt_a_new_stage(self):
        self.commit()
        unproved=self.store.offer({**self.offer,'stage':'publish','capabilities':['publish:x']})
        self.assertEqual(unproved['status'],'backlog_paused')
        admitted=self.store.offer({**self.offer,'stage':'publish','capabilities':['publish:x'],'backlog_authorization':self.proof})
        self.assertEqual(admitted['status'],'queued');self.assertEqual(admitted['grandfathered'],0)

    def test_exact_child_waits_for_central_commit_and_unrelated_child_stays_paused(self):
        child={**self.offer,'source_key':'x:queue:8','source_id':'8','family':'queue','ancestor_source_keys':['x:auto:1'],
               'backlog_ancestor_authorization':self.proof}
        self.assertEqual(self.store.offer(child)['status'],'backlog_paused')
        self.commit()
        self.assertEqual(self.store.offer(child)['status'],'queued')
        other={**child,'source_key':'x:queue:9','source_id':'9'};other.pop('backlog_ancestor_authorization')
        self.assertEqual(self.store.offer(other)['status'],'backlog_paused')

    def test_native_proof_fingerprint_plan_and_identity_must_match(self):
        self.commit()
        for field,value in [('plan_sha256','b'*64),('source_fingerprint','c'*64),('identity',{})]:
            with self.subTest(field=field), contextlib.closing(self.store.connect()) as c:
                self.assertEqual(offer_disposition(c,{**self.offer,'backlog_authorization':{**self.proof,field:value}}),'blocked')

    def test_real_claim_or_row_change_rejects_without_partial_disposition(self):
        for sql in ("UPDATE pipeline_job SET external_intent=1", "UPDATE pipeline_job SET payload_json='{}'",
                    "UPDATE pipeline_job SET status='running'", "UPDATE pipeline_job SET resource_keys_json='[]'"):
            with self.subTest(sql=sql), contextlib.closing(self.store.connect()) as c:
                c.execute('BEGIN IMMEDIATE');c.execute(sql)
                with self.assertRaises(ValueError):commit_backlog_item(c,self.item,'a'*64,stamp(self.now))
                c.rollback()

    def test_only_collector_observations_can_drift(self):
        with self.store.transaction() as c:c.execute("UPDATE pipeline_job SET updated_at='new-observation',source_status='pending' ")
        self.commit()

    def test_native_hold_and_native_attempt_cannot_be_bypassed(self):
        for key,value in [('status','backlog_paused'),('source_attempt_count',1),('unknown_outcome',True)]:
            with self.subTest(key=key):
                item={**self.item,'current_offer':{**self.item['current_offer'],key:value}}
                with self.assertRaises(ValueError):self.commit(item)

    def test_planner_only_parent_never_becomes_node_executable(self):
        self.item['current_offer']['execution_available']=False
        self.commit()
        job=self.store.get_job(self.item['paused_jobs'][0]['id'])
        self.assertEqual(job['status'],'queued');self.assertEqual(job['hold_reason'],'adapter_unavailable')

    def test_archived_parent_cannot_authorize_exact_descendant(self):
        self.item['action']='archive';self.commit()
        self.assertIsNone(self.store.offer({**self.offer,'source_key':'x:queue:8','source_id':'8','family':'queue',
                         'ancestor_source_keys':['x:auto:1'],'backlog_ancestor_authorization':self.proof}))


if __name__=='__main__':unittest.main()
