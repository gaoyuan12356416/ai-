"""Only two audited drama identities can use the one-shot planner entry."""
import contextlib
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import run_x_exact_recovered_schedule as op
from scripts import recover_x_zero_write_schedules as first
from scripts import recover_x_schedule_read_preflight as read
from scripts import recover_x_drama_episode_selection as episode
from scripts import test_x_zero_write_schedule_recovery as base
from scripts import test_x_post_schedule_runner as fixtures
from scripts import x_post_schedule_runner as runner


class ExactPlannerTests(unittest.TestCase):
    create=base.RecoveryTests.create
    row=base.RecoveryTests.row
    update=base.RecoveryTests.update

    def setUp(self):
        base.RecoveryTests.setUp(self)
        for rid,day,minute in ((1029,'2026-11-02','16:28'),(1031,'2026-11-03','08:44')):
            self.create(rid,day)
            self.update("UPDATE x_post_schedule_run SET source_type='drama',publish_time=?,config_version=27,"
                        "material_pool_snapshot_max_id=NULL WHERE id=?",(minute,rid))
        initial=first.plan(self.path,[1029,1031],['2026-11-02','2026-11-03'])
        first.apply(self.path,initial,initial['plan_sha256'],Path(self.temp.name)/'first','operator','original recovery')
        self.update(read.SCHEMA);self.update(episode.SCHEMA)
        anchors={}
        for rid,finished in ((1029,'2026-11-03T11:59:14Z'),(1031,'2026-11-03T10:52:39Z')):
            self.update("UPDATE x_post_schedule_run SET status='failed_preflight',error_code='known_error',"
                        "finished_at=?,updated_at=? WHERE id=?",(finished,finished,rid))
            previous=self.row(rid)
            with contextlib.closing(op.read_connection(self.path)) as c:
                original=op.audit(c,op.FIRST,rid)[0]
            identity_sha=op.fingerprint(op.identity(previous))
            if rid==1029:
                self.update('INSERT INTO '+op.EPISODE+' VALUES(?,?,?,?,?,?,?,?)',
                    (rid,identity_sha,'episode-plan','operator','episode filter recovery','2026-11-03T11:50:49Z','backup','{}'))
                with contextlib.closing(op.read_connection(self.path)) as c:prior_episode=op.audit(c,op.EPISODE,rid)[0]
                proof=dict(previous_run=previous,previous_audits={op.FIRST:original,op.EPISODE:prior_episode})
                self.update('INSERT INTO '+op.READ_AUDIT+' VALUES(?,?,?,?,?,?,?,?,?)',
                    (rid,finished,identity_sha,'read-plan','operator','read recovery','2026-11-03T12:23:47Z','backup',json.dumps(proof)))
            else:
                proof=dict(previous_run=previous,original_recovery_audit=original)
                self.update('INSERT INTO '+op.EPISODE+' VALUES(?,?,?,?,?,?,?,?)',
                    (rid,identity_sha,'episode-plan','operator','episode filter recovery','2026-11-03T12:23:47Z','backup',json.dumps(proof)))
            self.update("UPDATE x_post_schedule_run SET status='claimed',error_code='',error_message='',finished_at='',"
                        "updated_at='2026-11-03T12:23:47Z',lease_heartbeat_at='2026-11-03T12:23:47Z' WHERE id=?",(rid,))
            anchors[rid]=dict(op.EXACT[rid],identity_sha256=identity_sha,previous_failure_sha256=op.fingerprint(previous),
                              first_sha256=op.fingerprint(original))
            if rid==1029:
                with contextlib.closing(op.read_connection(self.path)) as c:anchors[rid]['grant_sha256']=op.fingerprint(op.audit(c,op.READ_AUDIT,rid)[0])
        patch=mock.patch.object(op,'EXACT',anchors);patch.start();self.addCleanup(patch.stop)
        self.owner=dict(pid=op.OLD_PID,invocation=op.OLD_INVOCATION,started_at=op.OLD_START)
        self.repair=dict(commit='a'*40,runner_path=str(Path(runner.__file__)),repository=str(Path(runner.__file__).parent.parent))
        patch=mock.patch.object(op,'verify_deployed',return_value=self.repair);patch.start();self.addCleanup(patch.stop)
        patch=mock.patch.object(op.subprocess,'run',return_value=SimpleNamespace(returncode=0));patch.start();self.addCleanup(patch.stop)
        patch=mock.patch.object(runner,'DEFAULT_WORK_DIR',str(Path(self.temp.name).resolve()));patch.start();self.addCleanup(patch.stop)
        self.config=replace(fixtures.make_config(self.temp.name),lock_path='/run/x-post-daily/schedule.lock')
        self.client=fixtures.FakeSidecar(due=[fixtures.due_item()])
        self.state=Path(self.temp.name)/'operator'

    def owner_check(self,**kwargs):return dict(self.owner)
    def plan(self,rid=1029):return op.plan(self.path,rid,**self.repair,owner_check=self.owner_check)

    def execute(self,saved,**overrides):
        def tick(config,**kwargs):
            return runner.execute_schedule_tick(config,drama_candidate_loader=fixtures.candidate_loader,**kwargs)
        options=dict(config=self.config,client=self.client,owner_check=self.owner_check,
                     lock_factory=lambda _path:contextlib.nullcontext(True),tick=tick,state_root=self.state)
        options.update(overrides)
        with mock.patch.object(runner,'process_lock',side_effect=lambda _path:contextlib.nullcontext(True)):
            return op.run(saved,'reviewer',**options)

    def add_parent_queue(self,rid=1029):
        self.update("INSERT INTO x_post_drama_pool(id,content_id,created_at,updated_at) VALUES(1,'content-1','original','original')")
        payload=base.base_candidate(2,'example',501,'content-1')
        payload.update(idempotency_key='parent-queue',schedule_run_id=rid,source_type='drama',material_key='501',run_date='2026-11-02',
                       drama_pool_item_id=1,episode_number=1,episode_key='content-1:1',drama_replay_generation=1,
                       created_at='2026-11-03T12:40:00Z',updated_at='2026-11-03T12:40:00Z',random_template_enabled=1,duration_policy_version=2)
        self.update('INSERT INTO x_post_queue('+','.join(payload)+') VALUES('+','.join('?' for _ in payload)+')',list(payload.values()))

    def test_normal_tick_only_original_identity_and_no_legacy_or_unrelated_due(self):
        saved=self.plan();before=self.row(1031);result=self.execute(saved)
        self.assertEqual(result['result']['status'],'dispatch_queued')
        self.assertEqual(result['result']['processed_count'],1)
        self.assertFalse(result['legacy_publish_called']);self.assertEqual(self.row(1031),before)
        calls=[c for c in self.client.calls if c[0]=='create'];self.assertEqual(len(calls),1)
        payload=calls[0][2]
        self.assertEqual([payload[k] for k in ('source_type','run_date','publish_time','version','account_ids')],
                         ['drama','2026-11-02','16:28',27,[2,3]])
        self.assertFalse(any(c[0] in ('publish','due','storage') for c in self.client.calls))
        report=json.loads(Path(result['report']).read_text(encoding='utf-8'))
        self.assertEqual(report['plan'],saved);self.assertEqual(report['phase'],'returned')
        with self.assertRaises(FileExistsError):self.execute(saved)
        self.assertEqual(len([c for c in self.client.calls if c[0]=='create']),1)

    def test_plan_is_readonly_and_retains_previous_audit_chain(self):
        def dump():
            with contextlib.closing(op.read_connection(self.path)) as c:return '\n'.join(c.iterdump())
        before=dump();saved=self.plan();self.assertEqual(dump(),before)
        proof=json.loads(saved['item']['grant']['evidence_json'])
        self.assertEqual(set(proof['previous_audits']),{op.FIRST,op.EPISODE})
        self.assertEqual(saved['item']['old_path_proof'],'old_invocation_known_failure_then_return')

    def test_timer_or_owner_change_during_preflight_stops_before_plan_write(self):
        saved=self.plan();original=self.client.verify_account;active=[True]
        def verify(account_id):
            result=original(account_id);active[0]=False;return result
        self.client.verify_account=verify
        def owner(**kwargs):
            op.require(active[0],'schedule_timer_must_be_paused');return self.owner_check(**kwargs)
        result=self.execute(saved,owner_check=owner)
        self.assertNotEqual(result['result']['status'],'dispatch_queued')
        self.assertFalse(any(c[0] in ('create','publish') for c in self.client.calls))

    def test_current_identity_heartbeat_queue_and_plan_fences_reject_before_calls(self):
        for field,value in [('body_template','changed'),('lease_heartbeat_at','later'),('plan_attempted_at','fence'),
                            ('unknown_count',1),('material_pool_snapshot_max_id',4444),('queued_count',1)]:
            saved=self.plan();old=self.row(1029)[field];self.update('UPDATE x_post_schedule_run SET '+field+'=? WHERE id=1029',(value,))
            with self.assertRaises(op.RecoveryRejected):self.execute(saved)
            self.update('UPDATE x_post_schedule_run SET '+field+'=? WHERE id=1029',(old,))
        saved=self.plan();self.add_parent_queue()
        with self.assertRaises(op.RecoveryRejected):self.execute(saved)
        self.assertEqual(self.client.calls,[])

    def test_rechecks_ledger_after_shared_target_mutex_before_first_api(self):
        saved=self.plan()
        @contextlib.contextmanager
        def racing_lock(_path):
            self.update("UPDATE x_post_schedule_run SET lease_heartbeat_at='other-planner' WHERE id=1029")
            yield True
        def tick(config,**kwargs):
            with mock.patch.object(runner,'process_lock',side_effect=racing_lock):
                return runner.execute_schedule_tick(config,drama_candidate_loader=fixtures.candidate_loader,**kwargs)
        result=self.execute(saved,tick=tick)
        self.assertEqual(result['result']['batches'][0]['status'],'needs_review');self.assertEqual(self.client.calls,[])

    def test_1031_needs_real_1029_queues_not_only_summary_and_first_audit(self):
        with self.assertRaisesRegex(op.RecoveryRejected,'1029_must_have_real_queues'):self.plan(1031)
        self.update("UPDATE x_post_schedule_run SET queued_count=1,plan_attempted_at='2026-11-03T12:40:00Z' WHERE id=1029")
        with self.assertRaisesRegex(op.RecoveryRejected,'1029_must_have_real_queues'):self.plan(1031)
        self.add_parent_queue();saved=self.plan(1031)
        self.assertEqual(self.execute(saved)['result']['status'],'dispatch_queued')
        self.assertEqual([c for c in self.client.calls if c[0]=='create'][0][2]['publish_time'],'08:44')

    def test_audit_chain_and_owner_drift_rejected(self):
        saved=self.plan()
        with self.assertRaises(op.RecoveryRejected):self.plan(1034)
        self.update('UPDATE '+op.EPISODE+" SET reason='changed' WHERE run_id=1029")
        with self.assertRaisesRegex(op.RecoveryRejected,'audit_chain_changed'):self.execute(saved)
        self.assertEqual(self.client.calls,[])

    def test_pipeline_off_runtime_drift_and_busy_operator_make_no_calls(self):
        saved=self.plan()
        with mock.patch.object(op,'pipeline_dispatch_enabled',return_value=False),self.assertRaises(op.RecoveryRejected):self.execute(saved)
        with mock.patch.object(op,'verify_deployed',return_value={}),self.assertRaises(op.RecoveryRejected):self.execute(saved)
        with self.assertRaisesRegex(op.RecoveryRejected,'another_targeted_operator'):
            self.execute(saved,lock_factory=lambda _:contextlib.nullcontext(None))
        with self.assertRaisesRegex(op.RecoveryRejected,'old_scanner_changed'):
            self.execute(saved,owner_check=lambda **kwargs:{'pid':1})
        self.assertEqual(self.client.calls,[])

    def test_unknown_plan_response_never_repeats_create_or_calls_publish(self):
        saved=self.plan();self.client.create_error=fixtures.SidecarError('lost','lost response',unknown_outcome=True)
        result=self.execute(saved)
        self.assertNotEqual(result['result']['status'],'dispatch_queued')
        self.assertEqual(len([c for c in self.client.calls if c[0]=='create']),1)
        self.assertFalse(any(c[0]=='publish' for c in self.client.calls))
        with self.assertRaises(FileExistsError):self.execute(saved)


class TargetMutexTests(unittest.TestCase):
    def test_only_exact_original_identities_use_target_mutex(self):
        config=SimpleNamespace(lock_path='/run/x-post-daily/schedule.lock')
        identity=dict(source_type='drama',run_date='2026-11-02',publish_time='16:28',version=27)
        with mock.patch.object(runner,'process_lock',return_value=contextlib.nullcontext(None)) as lock:
            with runner._targeted_schedule_lock(config,identity) as held:self.assertIsNone(held)
            self.assertTrue(lock.call_args.args[0].replace('\\','/').endswith('/schedule-targeted-1029.lock'))
        with mock.patch.object(runner,'process_lock') as lock:
            with runner._targeted_schedule_lock(config,dict(identity,version=28)) as held:self.assertTrue(held)
            lock.assert_not_called()

    def test_busy_target_skips_before_any_ledger_or_account_read(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            config=fixtures.make_config(directory)
            item=dict(source_type='drama',run_date='2026-11-02',publish_time='16:28',version=27,account_ids=[2],frozen=True)
            client=fixtures.FakeSidecar([item])
            with mock.patch.object(runner,'DEFAULT_WORK_DIR',str(Path(directory).resolve())),mock.patch.object(runner,'process_lock',return_value=contextlib.nullcontext(None)):
                result=runner.execute_schedule_tick(config,sidecar=client,drama_candidate_loader=fixtures.candidate_loader)
            self.assertEqual(result['status'],'waiting_targeted_lock');self.assertEqual([c[0] for c in client.calls],['due'])

    def test_owner_requires_pinned_invocation_and_paused_timer_but_allows_natural_exit(self):
        live=dict(MainPID=str(op.OLD_PID),InvocationID=op.OLD_INVOCATION,ActiveState='activating')
        paused=dict(ActiveState='inactive',SubState='dead')
        def check(service,timer=paused,executing=True):
            with mock.patch.object(op,'unit',side_effect=[service,timer]):return op.owner_state(executing=executing)
        self.assertEqual(check(live)['pid'],op.OLD_PID)
        for service,timer in ((dict(live,MainPID='123'),paused),(dict(live,InvocationID='new'),paused),
                              (live,dict(ActiveState='active',SubState='running'))):
            with self.assertRaises(op.RecoveryRejected):check(service,timer)
        ended=dict(MainPID='0',InvocationID='',ActiveState='inactive')
        self.assertEqual(check(ended)['pid'],op.OLD_PID)
        with self.assertRaises(op.RecoveryRejected):check(ended,executing=False)


if __name__=='__main__':unittest.main()
