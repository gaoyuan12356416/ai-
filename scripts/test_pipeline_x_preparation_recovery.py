"""Real SQLite, frozen X claims and live unrelated work; no platform network."""
import contextlib
import copy
import datetime as dt
import json
import unittest

from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.core import DispatchStore, DispatchError
from scripts import recover_pipeline_x_preparation as recovery
from scripts import test_pipeline_x_native as native_fixture


class RecoveryTests(unittest.TestCase):
    verify = native_fixture.NativeTests.verify
    queue = native_fixture.NativeTests.queue

    def setUp(self):
        native_fixture.NativeTests.setUp(self)
        self.now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)
        self.central = self.root / 'dispatch.sqlite'
        self.store = DispatchStore(self.central, lambda: self.now)
        self.store.channel_enabled('x', True)
        self.caps = ['prepare:x', 'backend:cpu']
        for node in ('cpu-main', 'hk-gpu'):
            self.store.register_node(node, self.caps)
        self.adapter = LedgerAdapter(LedgerSpec('x', 'queue', str(self.runtime.POST_DB_PATH), 'x_post_queue'), self.bridge)
        self.claims, self.descriptors, items = [], [], []
        for index in range(3):
            queue = self.queue(identity=index + 2, auto=True, random=True)
            offer = self.adapter.project(self.adapter.read(queue['id']))
            self.store.offer(offer)
            for generation in range(1, 2 + (index == 1)):
                self.store.register_node('cpu-main', self.caps)
                self.store.worker_heartbeat('cpu-main', 'fixture-worker', 'prepare')
                job = self.store.claim('cpu-main', 'fixture-worker', 'prepare')['job']
                self.assertIsNotNone(job)
                if index < 2:
                    job['executor_backend'] = 'cpu'
                    descriptor = self.bridge.reserve(job, job)
                    with self.store.transaction() as c:
                        c.execute('UPDATE pipeline_job SET payload_json=? WHERE id=?', (recovery.dumps(descriptor), job['id']))
                else:
                    descriptor = None  # A connection failure before native reserve.
                if generation < 1 + (index == 1):
                    self.now += dt.timedelta(seconds=121); self.store.expire()
            self.store.fail(job, 'canonical_bridge_unavailable')
            self.claims.append(job); self.descriptors.append(descriptor)
            items.append({'job_id': job['id'], 'source_key': job['source_key'], 'generation': job['generation'],
                          'canonical_fingerprint': offer['canonical_fingerprint']})
        self.now += dt.timedelta(seconds=130)
        self.scope = {'kind': 'x_unstarted_prepare_recovery_scope', 'version': 1, 'items': items,
                      'source_keys': [x['source_key'] for x in items]}
        self.scope['scope_sha256'] = recovery.document_hash(self.scope)
        self.backups = self.root / 'backups'

    def plan(self):
        return recovery.make_plan(self.central, self.runtime.POST_DB_PATH, self.scope, self.scope['scope_sha256'], self.now)

    def evidence(self, plan):
        return {'plan_sha256': plan['plan_sha256'], 'nodes': [dict(node_id=node, captured_at=self.now.isoformat(),
            scope_job_ids=sorted(x['job_id'] for x in plan['items']), outbox_pending_for_scope=0,
            active_scope_job_ids=[], task_file_matches=[]) for node in plan['node_ids']]}

    def apply(self, plan, evidence=None):
        return recovery.apply_plan(self.central, self.runtime.POST_DB_PATH, plan, plan['plan_sha256'],
            evidence or self.evidence(plan), self.backups, actor='offline-reviewer', clock=lambda: self.now)

    def dump(self, path):
        with contextlib.closing(recovery.read_connection(path)) as c:
            return '\n'.join(c.iterdump())

    def test_readonly_plan_and_apply_preserve_identity_native_history_and_unrelated_active(self):
        # Other live work and enabled channels are deliberately allowed.
        q = self.queue(identity=99, auto=True)
        self.store.offer(self.adapter.project(self.adapter.read(q['id'])))
        self.store.register_node('cpu-main', self.caps)
        self.store.worker_heartbeat('cpu-main', 'unrelated-live', 'prepare')
        active = self.store.claim('cpu-main', 'unrelated-live', 'prepare')['job']
        self.assertEqual(active['source_id'], str(q['id']))
        active_before = self.store.get_job(active['id'])
        before = self.dump(self.central); native = self.dump(self.runtime.POST_DB_PATH)
        originals = [self.store.get_job(j['id']) for j in self.claims]
        plan = self.plan()
        self.assertEqual(self.dump(self.central), before)
        result = self.apply(plan)
        self.assertEqual(self.dump(result['backup_path']), before)
        self.assertEqual(self.dump(self.runtime.POST_DB_PATH), native)
        self.assertEqual(self.store.get_job(active['id']), active_before)
        for previous in originals:
            current = self.store.get_job(previous['id'])
            for key in ('source_key', 'source_id', 'generation', 'payload', 'artifact', 'capabilities', 'resource_keys',
                        'canonical_fingerprint', 'scheduled_at', 'source_created_at', 'error_code'):
                self.assertEqual(current[key], previous[key], key)
            self.assertEqual(current['status'], 'queued')
            for key in ('node_id', 'worker_id', 'claim_hash', 'started_at', 'finished_at', 'lease_expires_at'):
                self.assertEqual(current.get(key, ''), '')
        with contextlib.closing(recovery.read_connection(self.central)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pipeline_event WHERE event_type='failed'").fetchone()[0], 3)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pipeline_event WHERE event_type='audited_x_preparation_recovery'").fetchone()[0], 3)
            audit = json.loads(c.execute("SELECT details_json FROM pipeline_event WHERE event_type='audited_x_preparation_recovery' ORDER BY id LIMIT 1").fetchone()[0])
            self.assertEqual(audit['previous_execution']['finished_at'], originals[0]['finished_at'])
        with self.assertRaises(recovery.RecoveryRejected): self.apply(plan)

    def test_old_claim_rejected_new_generation_reuses_frozen_descriptor_and_new_times(self):
        plan = self.plan(); self.apply(plan)
        for old in self.claims:
            with self.assertRaises(DispatchError): self.store.fail(old, 'late_old_failure')
        self.now += dt.timedelta(seconds=5)
        self.store.register_node('cpu-main', self.caps)
        self.store.worker_heartbeat('cpu-main', 'new-worker', 'prepare')
        claim = self.store.claim('cpu-main', 'new-worker', 'prepare')['job']
        self.assertEqual(claim['id'], self.claims[0]['id'])
        self.assertEqual(claim['generation'], self.claims[0]['generation'] + 1)
        self.assertEqual(claim['started_at'], self.store.now())
        self.assertEqual(claim['finished_at'], '')
        claim['executor_backend'] = 'gpu'  # Frozen CPU profile must survive.
        self.assertEqual(self.bridge.reserve(claim, claim), self.descriptors[0])
        self.now += dt.timedelta(seconds=9)
        self.store.complete(claim, 'offline-qualified-completion', {'artifact': {'sha256': 'a' * 64,
            'size': 100, 'url': 'https://example.test/qualified.mp4'}}, canonical_verified=True)
        self.assertEqual(self.store.get_job(claim['id'])['finished_at'], self.store.now())

    def test_scope_tamper_and_wrong_stage_or_failure_are_rejected(self):
        modified = copy.deepcopy(self.scope); modified['items'][0]['source_key'] = 'x:queue:999'
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'scope_hash_mismatch'):
            recovery.make_plan(self.central, self.runtime.POST_DB_PATH, modified, self.scope['scope_sha256'], self.now)
        for field, value in (('stage', 'publish'), ('status', 'unknown'), ('error_code', 'media_transcode_failed'),
                             ('external_intent', 1), ('hold_reason', 'operator_pause')):
            with self.subTest(field=field):
                with self.store.transaction() as c:
                    old = c.execute('SELECT ' + field + ' FROM pipeline_job WHERE id=?', (self.claims[0]['id'],)).fetchone()[0]
                    c.execute('UPDATE pipeline_job SET ' + field + '=? WHERE id=?', (value, self.claims[0]['id']))
                with self.assertRaises(recovery.RecoveryRejected): self.plan()
                with self.store.transaction() as c:
                    c.execute('UPDATE pipeline_job SET ' + field + '=? WHERE id=?', (old, self.claims[0]['id']))

    def test_platform_attempt_unknown_media_or_native_operation_rejected(self):
        qid = int(self.claims[0]['source_id'])
        for field, value in (('attempt_count', 1), ('unknown_outcome', 1), ('x_media_id', 'media-id'),
                             ('x_post_id', 'post-id'), ('status', 'failed')):
            with self.subTest(field=field):
                with self.bridge.connect() as c:
                    old = c.execute('SELECT ' + field + ' FROM x_post_publish_log WHERE queue_id=?', (qid,)).fetchone()[0]
                    c.execute('UPDATE x_post_publish_log SET ' + field + '=? WHERE queue_id=?', (value, qid))
                with self.assertRaisesRegex(recovery.RecoveryRejected, 'native_publish_evidence'): self.plan()
                with self.bridge.connect() as c:
                    c.execute('UPDATE x_post_publish_log SET ' + field + '=? WHERE queue_id=?', (old, qid))
        with self.bridge.connect() as c:
            c.execute('INSERT INTO pipeline_x_operation VALUES(?,?,?,?,?,?)', ('write', self.claims[0]['source_key'], 1, 'initialize', 'b' * 64, self.now.isoformat()))
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'native_operation_evidence'): self.plan()

    def test_frozen_binding_mismatch_and_preexisting_checkpoint_rejected(self):
        with self.bridge.connect() as c:
            c.execute("UPDATE pipeline_x_claim SET claim_hash='mismatch' WHERE source_key=?", (self.claims[0]['source_key'],))
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'native_binding_identity_changed'): self.plan()
        with self.bridge.connect() as c:
            c.execute('UPDATE pipeline_x_claim SET claim_hash=? WHERE source_key=?',
                      (self.bridge.claim_hash(self.claims[0]), self.claims[0]['source_key']))
            c.execute('INSERT INTO pipeline_x_checkpoint VALUES(?,?,?,?)', (self.claims[0]['source_key'], 1, 'x_preflight', '{}'))
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'native_checkpoint_present'): self.plan()

    def test_existing_qualified_or_partial_artifacts_never_enter_unstarted_scope(self):
        with self.bridge.connect() as c:
            c.execute('INSERT INTO pipeline_x_artifact VALUES(?,?,?)', (int(self.claims[0]['source_id']), '{}', 'https://example.test/source'))
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'native_preparation_present'): self.plan()

    def test_native_identity_drift_before_plan_is_rejected(self):
        qid = int(self.claims[0]['source_id'])
        for field, value in (('account_id', 999), ('material_id', 'changed-material')):
            with self.subTest(field=field):
                with self.bridge.connect() as c:
                    old = c.execute('SELECT ' + field + ' FROM x_post_queue WHERE id=?', (qid,)).fetchone()[0]
                    c.execute('UPDATE x_post_queue SET ' + field + '=? WHERE id=?', (value, qid))
                with self.assertRaisesRegex(recovery.RecoveryRejected, 'native_frozen_identity_changed'): self.plan()
                with self.bridge.connect() as c:
                    c.execute('UPDATE x_post_queue SET ' + field + '=? WHERE id=?', (old, qid))

    def test_fresh_scoped_node_evidence_and_plan_cas_are_required(self):
        plan = self.plan(); before = self.dump(self.central)
        for changed in ({'outbox_pending_for_scope': 1}, {'active_scope_job_ids': [self.claims[0]['id']]},
                        {'task_file_matches': ['pending-render']}, {'scope_job_ids': []},
                        {'captured_at': (self.now - dt.timedelta(seconds=61)).isoformat()}):
            with self.subTest(changed=changed):
                evidence = self.evidence(plan); evidence['nodes'][0].update(changed)
                with self.assertRaises(recovery.RecoveryRejected): self.apply(plan, evidence)
                self.assertEqual(self.dump(self.central), before)
        with self.store.transaction() as c:
            c.execute("UPDATE pipeline_job SET capabilities_json='[\"prepare:x\",\"backend:gpu\"]' WHERE id=?", (self.claims[0]['id'],))
        changed = self.dump(self.central)
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'plan_state_drift'): self.apply(plan)
        self.assertEqual(self.dump(self.central), changed)

    def test_other_publish_execution_and_central_delivery_are_not_reset(self):
        original = self.store.get_job(self.claims[0]['id'])
        offer = self.adapter.project(self.adapter.read(original['source_id']))
        offer['stage'] = 'publish'; offer['status'] = 'failed'
        published = self.store.offer(offer)
        with self.store.transaction() as c:
            c.execute("UPDATE pipeline_job SET generation=1,error_code='canonical_bridge_unavailable' WHERE id=?", (published['id'],))
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'other_stage_execution_evidence'): self.plan()
        with self.store.transaction() as c:
            c.execute('DELETE FROM pipeline_job WHERE id=?', (published['id'],))
            c.execute('INSERT INTO pipeline_delivery VALUES(?,?,?,?,?,?)', ('receipt', original['id'], 1, 'a', 'b', '{}'))
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'dispatch_execution_evidence'): self.plan()


if __name__ == '__main__':
    unittest.main()
