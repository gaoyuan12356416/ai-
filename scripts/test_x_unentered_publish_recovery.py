import unittest
import sqlite3

from scripts import recover_x_unentered_publish as recovery
from scripts import recover_x_prepost_failures as base
from scripts.test_x_prepost_recovery import PrepostRecoveryTests


class UnenteredPublishTests(unittest.TestCase):
    def setUp(self):
        self.h=PrepostRecoveryTests('runTest');self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.apply(self.h.plan())
        self.job=self.h.claim('publish')
        self.failure={'job_id':self.job['id'],'generation':self.job['generation'],
                      'error_code':'x_tag_dispatch_failed','unknown_outcome':False}
        self.h.store.fail(self.job,'x_tag_dispatch_failed',canonical_no_write=True)
        self.h.store.save_delivery(self.job,'unentered-failure',self.failure,{'accepted':True,'status':'failed'})
        outbox=self.h.outboxes['cpu-main'];rid=outbox.put('jobs/fail',self.failure)
        with outbox.connect() as c:c.execute('UPDATE receipt_outbox SET acked_at=1 WHERE id=?',(rid,))

    def plan(self):
        return recovery.make_plan(self.h.central,self.h.runtime.POST_DB_PATH,[self.job['id']])

    def evidence(self,p):
        return [base.node_evidence(p,n,self.h.outboxes[n].path) for n in p['nodes']]

    def test_unused_permission_advances_once_without_erasing_attempt(self):
        p=self.plan();old_log=self.h.log()
        result=recovery.apply_plan(p,self.evidence(p),self.h.root/'unentered-backups','operator')
        self.assertEqual(result['restored_job_ids'],[self.job['id']])
        self.assertEqual(old_log,self.h.log())
        recovery.apply_plan(p,self.evidence(p),self.h.root/'unentered-backups','operator')
        with self.h.bridge.connect() as c:
            rows=c.execute('SELECT from_generation,to_generation,state FROM pipeline_x_prepost_recovery ORDER BY from_generation').fetchall()
            self.assertEqual([tuple(r) for r in rows],[(1,2,'superseded_unentered'),(2,3,'armed')])
        next_job=self.h.claim('publish')
        self.assertEqual(next_job['generation'],3)
        self.assertEqual(self.h.bridge.reserve(next_job,next_job),self.h.desc)

    def test_native_entry_blocks_recovery(self):
        self.h.bridge.reserve(self.job,self.job)
        with self.assertRaisesRegex(base.RecoveryRejected,'native_current_claim_entered'):self.plan()

    def test_first_unentered_claim_preserves_unstarted_log(self):
        h=self.h;q=h.queue(identity=3,auto=True)
        h.store.offer(h.adapter.project(h.adapter.read(q['id'])))
        prep=h.claim('prepare');desc=h.bridge.reserve(prep,prep)
        receipt={'source_url':desc['request']['source_url'],'artifact':h.artifact}
        h.bridge.checkpoint(prep,prep,'x_preflight',receipt)
        h.store.checkpoint(prep,'new-preflight','x_preflight',receipt,canonical_verified=True)
        h.bridge.complete(prep,{'artifact':h.artifact},prep)
        h.store.complete(prep,'new-prepared',{'artifact':h.artifact,'canonical_receipt':'native-final'},canonical_verified=True)
        h.store.offer(h.adapter.project(h.adapter.read(q['id'])))
        job=h.claim('publish')
        failure=dict(job_id=job['id'],generation=job['generation'],error_code='x_tag_dispatch_failed',unknown_outcome=False)
        h.store.fail(job,failure['error_code'],canonical_no_write=True)
        h.store.save_delivery(job,'first-unentered',failure,{'accepted':True,'status':'failed'})
        outbox=h.outboxes['cpu-main'];rid=outbox.put('jobs/fail',failure)
        with outbox.connect() as c:c.execute('UPDATE receipt_outbox SET acked_at=1 WHERE id=?',(rid,))
        p=recovery.make_plan(h.central,h.runtime.POST_DB_PATH,[job['id']])
        self.assertIsNone(p['items'][0]['authorization'])
        self.assertEqual(p['items'][0]['log']['attempt_count'],0)
        recovery.apply_plan(p,self.evidence(p),h.root/'unentered-backups','operator')
        with h.bridge.connect() as c:
            self.assertEqual(dict(c.execute('SELECT * FROM x_post_publish_log WHERE queue_id=?',(q['id'],)).fetchone()),p['items'][0]['log'])

    def test_unknown_and_consumed_authorization_block_recovery(self):
        with self.h.bridge.connect() as c:c.execute('UPDATE x_post_publish_log SET unknown_outcome=1')
        with self.assertRaisesRegex(base.RecoveryRejected,'native_not_untouched'):self.plan()
        with self.h.bridge.connect() as c:
            c.execute('UPDATE x_post_publish_log SET unknown_outcome=0')
            c.execute("UPDATE pipeline_x_prepost_recovery SET state='consumed'")
        with self.assertRaisesRegex(base.RecoveryRejected,'unused_authorization_required'):self.plan()

    def test_original_operator_termination_blocks_plan(self):
        path=self.h.root/'auto.sqlite'
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE x_auto_task(run_id,execution_queue_id)')
            c.execute('CREATE TABLE x_auto_run(id,status,error_code)')
            c.execute('INSERT INTO x_auto_task VALUES(1,?)',(self.h.q['id'],))
            c.execute("INSERT INTO x_auto_run VALUES(1,'canceled','operator_terminated')")
        with self.assertRaisesRegex(base.RecoveryRejected,'auto_origin_terminated'):
            recovery.make_plan(self.h.central,self.h.runtime.POST_DB_PATH,[self.job['id']],path)


if __name__=='__main__':unittest.main()
