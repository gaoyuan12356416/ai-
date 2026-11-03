"""Exact orphan lease expiry and original queue handoff, using real SQLite."""
import contextlib
import copy
import datetime as dt
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from features.pipeline_dispatch.core import DispatchStore
from features.x_auto_posts.core import XAutoPostStoreError
from scripts import recover_pipeline_x_orphan_planner as recovery
from scripts import test_pipeline_x_native as x_fixture
from scripts import test_x_auto_post_publisher as auto_fixture
from scripts.test_x_post_priority_manual_store import material_candidate, ACTOR


class OrphanTests(unittest.TestCase):
    def setUp(self):
        self.auto = auto_fixture.XAutoPostPublisherTests(); self.auto.setUp(); self.addCleanup(self.auto.doCleanups)
        self.x = x_fixture.NativeTests(); self.x.setUp(); self.addCleanup(self.x.doCleanups)
        self.token, task = self.auto._reserve()
        run = self.x.bridge.store.create_auto_template_run(task.material_id, int(task.account_id),
            'x-auto-task-' + str(task.id), 'x-auto-template-' + str(task.template_id), task.template_version,
            '{{drama_name}}\n{{desc}}\n{{url}}', ACTOR)
        item = material_candidate(run, int(task.account_id), task.material_id)
        item.update(content_id=task.content_id, media_validation_mode='deferred', preflight_sha256='', preflight_size=0, preflight_duration=0)
        queue = self.x.bridge.store.create_manual_plan(run['id'], [item], 'auto_template')['queues'][0]
        self.task = self.auto.store.transition_task(task.id, 'preparing', expected_statuses={'reserved'},
            claim_token=self.token, updates={'execution_run_id': run['id']})
        with self.auto.store._transaction() as c:
            c.execute('UPDATE x_auto_task SET lease_expires_at_utc=?,account_snapshot_json=? WHERE id=?',
                ((self.auto.clock() + dt.timedelta(hours=3)).isoformat(timespec='seconds'), json.dumps({
                    'id': int(task.account_id), 'publish_approved': True, 'status': 'active', 'drama_language': 'en'}), task.id))
        self.auto.clock.value += dt.timedelta(minutes=5)
        self.central = self.x.root / 'dispatch.sqlite'
        self.store = DispatchStore(self.central, self.auto.clock)
        for node in ('cpu-main', 'hk-gpu'): self.store.register_node(node, ['prepare:x'])
        self.ids = []
        for family, source_id in (('auto', task.id), ('queue', queue['id'])):
            offer = dict(source_key='x:%s:%s' % (family, source_id), channel='x', family=family,
                source_id=str(source_id), stage='prepare', status='legacy_running', legacy_inflight=True,
                source_status='preparing' if family == 'auto' else 'queued', scheduled_at=self.auto.clock().isoformat(),
                resource_keys=['x:account:' + task.account_id], capabilities=['prepare:x'])
            self.ids.append(self.store.offer(offer)['id'])
        self.queue_id, self.run_id = queue['id'], run['id']
        self.paths = (self.central, self.auto.store.db_path, self.x.runtime.POST_DB_PATH)
        self.scope = recovery.capture_scope(self.auto.store.db_path, task.id, run['id'], queue['id'], self.ids)
        self.backups = self.x.root / 'backup'

    def plan(self):
        return recovery.make_plan(*self.paths, self.scope, self.scope['scope_sha256'], self.auto.clock())

    def evidence(self, plan):
        return {'plan_sha256': plan['plan_sha256'], 'services': [dict(unit=unit, captured_at=self.auto.clock().isoformat(),
            main_pid=100 + index, threads=1, accepted_connections=0) for index, unit in enumerate(recovery.SERVICE_UNITS)],
            'nodes': [dict(node_id=node, captured_at=self.auto.clock().isoformat(), scope_job_ids=self.ids,
                outbox_matching_count=0, active_scope_job_ids=[], task_file_matches=[]) for node in plan['proof']['node_ids']]}

    def apply(self, plan, evidence=None):
        return recovery.apply_plan(*self.paths, plan, plan['plan_sha256'], evidence or self.evidence(plan),
            self.backups, actor='offline-reviewed-operator', clock=self.auto.clock)

    def dump(self, path):
        with contextlib.closing(recovery.read_connection(path)) as c: return '\n'.join(c.iterdump())

    def test_only_exact_native_lease_and_audit_change(self):
        before = self.dump(self.auto.store.db_path)
        central, x = self.dump(self.central), self.dump(self.x.runtime.POST_DB_PATH)
        with contextlib.closing(recovery.read_connection(self.auto.store.db_path)) as c:
            task_before = dict(c.execute('SELECT * FROM x_auto_task WHERE id=?', (self.task.id,)).fetchone())
            reservation_before = recovery.rows(c, 'SELECT * FROM x_auto_material_ledger')
        plan = self.plan()
        self.assertEqual(self.dump(self.auto.store.db_path), before)
        result = self.apply(plan)
        self.assertEqual(self.dump(result['backup_path']), before)
        self.assertEqual(self.dump(self.central), central)
        self.assertEqual(self.dump(self.x.runtime.POST_DB_PATH), x)
        with contextlib.closing(recovery.read_connection(self.auto.store.db_path)) as c:
            after = dict(c.execute('SELECT * FROM x_auto_task WHERE id=?', (self.task.id,)).fetchone())
            for key, value in task_before.items():
                if key not in ('lease_expires_at_utc', 'updated_at'): self.assertEqual(after[key], value, key)
            self.assertLess(recovery.date(after['lease_expires_at_utc']), self.auto.clock())
            self.assertEqual(recovery.rows(c, 'SELECT * FROM x_auto_material_ledger'), reservation_before)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM x_auto_event WHERE event_type='pipeline_orphan_planning_lease_expired'").fetchone()[0], 1)
        with self.assertRaises(recovery.RecoveryRejected): self.apply(plan)

    def test_existing_planner_claim_confirms_original_queue_without_create_or_publish(self):
        self.assertIsNone(self.auto.store.claim_next_executable_task(worker_id='early', lease_seconds=120, now=self.auto.clock(), publish_delegated=True))
        self.apply(self.plan())
        claim = self.auto.store.claim_next_executable_task(worker_id='resume', lease_seconds=120, now=self.auto.clock(), publish_delegated=True)
        self.assertEqual(claim.claim_phase, 'prepare')
        self.assertEqual(claim.task.execution_run_id, self.run_id)
        with self.assertRaises(XAutoPostStoreError):
            self.auto.store.confirm_material_reservation(self.task.id, self.queue_id, claim_token=self.token)
        forbidden = mock.Mock(side_effect=AssertionError('new queue/render/publication is forbidden'))
        bridge = SimpleNamespace(query_run=lambda rid: self.x.bridge.store.get_manual_run(rid, 'auto_template'),
            create_run=forbidden, create_plan=forbidden, publish_queue=forbidden, storage_preflight=forbidden)
        executor = self.auto._executor(bridge); executor.dispatch_enabled = True; executor.media_preflight = forbidden
        result = executor._prepare(claim.task, claim.reveal_claim_token())
        self.assertEqual((result.status, result.execution_run_id, result.execution_queue_id), ('ready', self.run_id, self.queue_id))
        forbidden.assert_not_called()
        with contextlib.closing(recovery.read_connection(self.x.runtime.POST_DB_PATH)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM x_post_queue').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM x_post_publish_log').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM pipeline_x_operation').fetchone()[0], 0)
        reservation = self.auto.store.get_task_reservation(self.task.id)
        self.assertEqual(reservation.canonical_queue_id, self.queue_id)

    def test_busy_or_stale_services_or_nodes_reject_before_mutation(self):
        plan = self.plan(); before = self.dump(self.auto.store.db_path)
        for changed in ({'threads': 2}, {'accepted_connections': 1}, {'main_pid': 0},
                        {'captured_at': (self.auto.clock() - dt.timedelta(seconds=61)).isoformat()}):
            with self.subTest(changed=changed):
                evidence = self.evidence(plan); evidence['services'][0].update(changed)
                with self.assertRaises(recovery.RecoveryRejected): self.apply(plan, evidence)
        for changed in ({'outbox_matching_count': 1}, {'active_scope_job_ids': [self.ids[1]]}, {'task_file_matches': ['manifest']}):
            with self.subTest(changed=changed):
                evidence = self.evidence(plan); evidence['nodes'][1].update(changed)
                with self.assertRaises(recovery.RecoveryRejected): self.apply(plan, evidence)
        self.assertEqual(self.dump(self.auto.store.db_path), before)

    def test_full_task_hash_detects_replaced_token_or_lease(self):
        with self.auto.store._transaction() as c:
            c.execute("UPDATE x_auto_task SET claim_token='new-owner' WHERE id=?", (self.task.id,))
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'orphan_task_changed'): self.plan()
        with self.auto.store._transaction() as c:
            c.execute("UPDATE x_auto_task SET publish_id='already-published' WHERE id=?", (self.task.id,))
        self.scope = recovery.capture_scope(self.auto.store.db_path, self.task.id, self.run_id, self.queue_id, self.ids)
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'orphan_native_execution_evidence'): self.plan()

    def test_plan_rejects_native_child_execution_and_central_claim(self):
        with self.store.transaction() as c: c.execute('UPDATE pipeline_job SET generation=1 WHERE id=?', (self.ids[1],))
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'orphan_dispatch_execution_evidence'): self.plan()
        with self.store.transaction() as c: c.execute('UPDATE pipeline_job SET generation=0 WHERE id=?', (self.ids[1],))
        self.x.bridge.store.reserve_log(self.queue_id)
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'orphan_child_execution_evidence'): self.plan()

    def test_run_or_reservation_drift_rejects_and_preserves_lease(self):
        plan = self.plan(); before = self.dump(self.auto.store.db_path)
        with self.x.bridge.connect() as c:
            c.execute("UPDATE x_post_manual_run SET status='stopped' WHERE id=?", (self.run_id,))
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'orphan_run_not_untouched'): self.apply(plan)
        self.assertEqual(self.dump(self.auto.store.db_path), before)


if __name__ == '__main__': unittest.main()
