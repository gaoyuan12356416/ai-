"""Exact six final-artifact recovery using native and central SQLite ledgers."""
import contextlib
import copy
import datetime as dt
import json
import unittest
from unittest import mock

from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.core import DispatchError, DispatchStore
from features.x_posts.random_template_store import PreparationStore
from scripts import recover_pipeline_x_prepared_publish as recovery
from scripts import test_pipeline_x_native as native_fixture


class PreparedPublishRecoveryTests(unittest.TestCase):
    verify = native_fixture.NativeTests.verify
    queue = native_fixture.NativeTests.queue

    def setUp(self):
        native_fixture.NativeTests.setUp(self)
        self.now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=3)
        self.central = self.root / 'central.sqlite'
        self.store = DispatchStore(self.central, lambda: self.now)
        self.store.channel_enabled('x', True)
        self.caps = ['prepare:x', 'publish:x', 'backend:cpu']
        self.adapter = LedgerAdapter(LedgerSpec('x', 'queue', str(self.runtime.POST_DB_PATH), 'x_post_queue'), self.bridge)
        self.pub_claims, self.prep_claims, self.artifacts = {}, {}, {}
        for index, qid in enumerate(recovery.QUEUE_IDS):
            with self.bridge.connect() as c:
                c.execute("DELETE FROM sqlite_sequence WHERE name='x_post_queue'")
                c.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('x_post_queue',?)", (qid - 1,))
            queue = self.queue(identity=index + 2, auto=True, random=True)
            self.assertEqual(queue['id'], qid)
            self.store.offer(self.adapter.project(self.adapter.read(qid)))
            self.store.register_node('cpu-main', self.caps)
            self.store.register_node('hk-gpu', self.caps)
            self.store.worker_heartbeat('cpu-main', 'prep-worker', 'prepare')
            prep = self.store.claim('cpu-main', 'prep-worker', 'prepare')['job']; prep['executor_backend'] = 'cpu'
            desc = self.bridge.reserve(prep, prep)
            source = {'url': 'https://media.example.test/source-%s.mp4' % qid, 'sha256': 'b' * 64,
                      'size': 2048, 'mime': 'video/mp4', 'duration_seconds': 30, 'profile': 'source'}
            receipt = {'source_url': desc['request']['source_url'], 'artifact': source}
            self.bridge.checkpoint(prep, prep, 'x_preflight', receipt)
            self.store.checkpoint(prep, 'preflight-' + str(qid), 'x_preflight', receipt, canonical_verified=True)
            if qid in recovery.FAILED_PREPARATIONS:
                self.now += dt.timedelta(seconds=121); self.store.expire()
                self.store.register_node('cpu-main', self.caps)
                self.store.worker_heartbeat('cpu-main', 'prep-worker', 'prepare')
                prep = self.store.claim('cpu-main', 'prep-worker', 'prepare')['job']; prep['executor_backend'] = 'cpu'
                self.bridge.reserve(prep, prep)
            request = PreparationStore(self.runtime.POST_DB_PATH).get(qid)['request']
            result = {**request, 'output_url': 'https://media.example.test/final-%s.mp4' % qid,
                      'output_sha256': format(qid, '064x'), 'output_size': 3333 + index,
                      'probe': {'duration': 30}, 'recipe': {'asset_set_sha256': request['asset_manifest_sha256'], 'chosen': qid}}
            artifact = recovery.artifact_from_receipt(request, result)
            self.bridge.complete(prep, {'artifact': artifact, 'preparation': result}, prep)
            if qid in recovery.FAILED_PREPARATIONS:
                self.store.fail(prep, 'canonical_bridge_unavailable')
            else:
                self.store.complete(prep, 'complete-' + str(qid), {'artifact': artifact, 'canonical_receipt': 'native-final-' + str(qid)}, canonical_verified=True)
            self.prep_claims[qid], self.artifacts[qid] = prep, artifact
            pubid = self.store.offer(self.adapter.project(self.adapter.read(qid)))['id']
            self.store.register_node('cpu-main', self.caps)
            self.store.worker_heartbeat('cpu-main', 'pub-worker', 'publish')
            pub = self.store.claim('cpu-main', 'pub-worker', 'publish')['job']
            self.assertEqual(pub['id'], pubid)
            if qid in recovery.FAILED_PREPARATIONS:
                self.bridge.reserve(pub, pub)
            # The historical central projection stored the source, although
            # native reserve always binds the validated final render.
            if qid != 5458:
                with self.store.transaction() as c:
                    c.execute('UPDATE pipeline_job SET artifact_json=? WHERE id=?', (recovery.dumps(source), pubid))
            self.store.fail(pub, 'canonical_bridge_unavailable' if index % 2 == 0 else 'x_dispatch_failed')
            self.pub_claims[qid] = pub
        self.now += dt.timedelta(seconds=180)
        self.backups = self.root / 'backups'

    def plan(self):
        return recovery.make_plan(self.central, self.runtime.POST_DB_PATH, self.now)

    def evidence(self, plan):
        return {'plan_sha256': plan['plan_sha256'], 'nodes': [dict(node_id=node, captured_at=self.now.isoformat(),
            scope_job_ids=plan['evidence_job_ids'], outbox_pending_for_scope=0, active_scope_job_ids=[]) for node in plan['node_ids']]}

    def apply(self, plan, evidence=None):
        return recovery.apply_plan(self.central, self.runtime.POST_DB_PATH, plan, plan['plan_sha256'],
            evidence or self.evidence(plan), self.backups, actor='offline-reviewer', clock=lambda: self.now)

    def dump(self, path):
        with contextlib.closing(recovery.read_connection(path)) as c: return '\n'.join(c.iterdump())

    def test_exact_six_reuses_final_artifacts_corrects_two_outcomes_and_preserves_native(self):
        native_before, central_before = self.dump(self.runtime.POST_DB_PATH), self.dump(self.central)
        before = {qid: self.store.get_job(j['id']) for qid, j in self.pub_claims.items()}
        plan = self.plan()
        self.assertEqual(self.dump(self.central), central_before)
        self.assertEqual(len(plan['evidence_job_ids']), 12)
        self.assertNotIn('https://', recovery.dumps(plan))
        result = self.apply(plan)
        self.assertEqual(self.dump(result['backup_path']), central_before)
        self.assertEqual(self.dump(self.runtime.POST_DB_PATH), native_before)
        for qid in recovery.QUEUE_IDS:
            pub = self.store.get_job(self.pub_claims[qid]['id'])
            self.assertEqual(pub['status'], 'ready'); self.assertEqual(pub['artifact'], self.artifacts[qid])
            for key in ('source_key', 'generation', 'scheduled_at', 'source_created_at', 'payload', 'capabilities', 'resource_keys'):
                self.assertEqual(pub[key], before[qid][key], key)
            for key in ('claim_hash', 'node_id', 'worker_id', 'lease_expires_at', 'started_at', 'finished_at'):
                self.assertEqual(pub.get(key, ''), '')
            prep = self.store.get_job(self.prep_claims[qid]['id'])
            self.assertEqual(prep['status'], 'completed'); self.assertEqual(prep['artifact'], self.artifacts[qid])
            self.assertEqual((prep['node_id'], prep['worker_id']),
                             (self.prep_claims[qid]['node_id'], self.prep_claims[qid]['worker_id']))
        with contextlib.closing(recovery.read_connection(self.central)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM pipeline_event WHERE event_type='audited_x_first_publish_recovery'").fetchone()[0], 6)
            self.assertEqual(c.execute("SELECT count(*) FROM pipeline_event WHERE event_type='audited_native_preparation_confirmed'").fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT count(*) FROM pipeline_event WHERE event_type='failed'").fetchone()[0], 8)
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_publication').fetchone()[0], 0)
        with self.assertRaises(recovery.RecoveryRejected): self.apply(plan)

    def test_next_generation_first_publish_uses_same_final_without_preparation(self):
        self.apply(self.plan()); self.now += dt.timedelta(seconds=1)
        self.store.register_node('cpu-main', self.caps)
        self.store.worker_heartbeat('cpu-main', 'new-publisher', 'publish')
        job = self.store.claim('cpu-main', 'new-publisher', 'publish')['job']
        qid = int(job['source_id']); self.assertEqual(job['generation'], self.pub_claims[qid]['generation'] + 1)
        with mock.patch.object(self.bridge, 'preflight', side_effect=AssertionError('must not prepare')):
            desc = self.bridge.reserve(job, job)
        self.assertEqual(desc['kind'], 'x_publish'); self.assertEqual(desc['artifact'], self.artifacts[qid])
        with self.assertRaises(DispatchError): self.store.fail(self.pub_claims[qid], 'late_old_failure')
        with self.assertRaises(DispatchError): self.store.complete(self.prep_claims[5392], 'late-prepare', {}, canonical_verified=True)
        with self.bridge.connect() as c:
            self.assertEqual(c.execute('SELECT attempt_count FROM x_post_publish_log WHERE queue_id=?', (qid,)).fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_x_operation').fetchone()[0], 0)

    def test_unknown_account_fence_and_unrelated_jobs_remain_in_place(self):
        resource = self.pub_claims[5392]['resource_keys'][0]
        unknown = self.store.offer({'source_key': 'x:queue:99999', 'source_id': '99999', 'channel': 'x',
            'family': 'queue', 'stage': 'publish', 'status': 'unknown', 'scheduled_at': self.now.isoformat(),
            'resource_keys': [resource], 'capabilities': ['publish:x'], 'payload': {}})
        with self.store.transaction() as c:
            c.execute('INSERT OR REPLACE INTO pipeline_resource VALUES(?,?,?,?,?)', (resource, unknown['id'], 4, 1, 1))
            c.execute('INSERT OR REPLACE INTO pipeline_legacy_resource VALUES(?,?)', ('x:account:unrelated', unknown['id']))
        with contextlib.closing(recovery.read_connection(self.central)) as c:
            before = [tuple(r) for r in c.execute('SELECT * FROM pipeline_resource')]
            legacy = [tuple(r) for r in c.execute('SELECT * FROM pipeline_legacy_resource')]
        self.apply(self.plan())
        with contextlib.closing(recovery.read_connection(self.central)) as c:
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM pipeline_resource')], before)
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM pipeline_legacy_resource')], legacy)
        self.store.register_node('cpu-main', self.caps); self.store.worker_heartbeat('cpu-main', 'safe-publisher', 'publish')
        self.assertNotEqual(self.store.claim('cpu-main', 'safe-publisher', 'publish')['job']['source_id'], '5392')
        self.assertEqual(self.store.get_job(self.pub_claims[5392]['id'])['status'], 'ready')

    def test_platform_attempt_unknown_or_receipt_never_restored(self):
        for field, value in (('attempt_count', 1), ('unknown_outcome', 1), ('x_media_id', 'media'), ('x_post_id', 'post')):
            with self.subTest(field=field):
                with self.bridge.connect() as c:
                    old = c.execute('SELECT '+field+' FROM x_post_publish_log WHERE queue_id=5392').fetchone()[0]
                    c.execute('UPDATE x_post_publish_log SET '+field+'=? WHERE queue_id=5392', (value,))
                with self.assertRaisesRegex(recovery.RecoveryRejected, 'native_publish_evidence'): self.plan()
                with self.bridge.connect() as c: c.execute('UPDATE x_post_publish_log SET '+field+'=? WHERE queue_id=5392', (old,))
        with self.bridge.connect() as c:
            c.execute('INSERT INTO pipeline_x_operation VALUES(?,?,?,?,?,?)', ('intent', 'x:queue:5392', 1, 'initialize', 'f'*64, self.now.isoformat()))
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'native_write_or_hold_evidence'): self.plan()

    def test_final_receipt_and_source_identity_must_match(self):
        with self.bridge.connect() as c:
            row = c.execute('SELECT result_json FROM x_post_random_template_preparation WHERE queue_id=5392').fetchone()[0]
            value = json.loads(row); value['source_sha256'] = 'c' * 64
            c.execute('UPDATE x_post_random_template_preparation SET result_json=? WHERE queue_id=5392', (recovery.dumps(value),))
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'native_final_receipt_invalid'): self.plan()
        with self.bridge.connect() as c:
            c.execute('UPDATE x_post_random_template_preparation SET result_json=? WHERE queue_id=5392', (row,))
            c.execute('UPDATE x_post_queue SET material_id=? WHERE id=5392', ('different',))
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'native_frozen_identity_changed'): self.plan()

    def test_fresh_all_twelve_jobs_node_proof_required_and_plan_tamper_rejected(self):
        plan = self.plan(); before = self.dump(self.central)
        for changed in ({'scope_job_ids': [i['job_id'] for i in plan['items']]}, {'outbox_pending_for_scope': 1},
                        {'active_scope_job_ids': [self.prep_claims[5392]['id']]},
                        {'captured_at': (self.now - dt.timedelta(seconds=61)).isoformat()}):
            with self.subTest(changed=changed):
                evidence = self.evidence(plan); evidence['nodes'][0].update(changed)
                with self.assertRaises(recovery.RecoveryRejected): self.apply(plan, evidence)
                self.assertEqual(self.dump(self.central), before)
        changed = copy.deepcopy(plan); changed['items'][0]['final_sha256'] = 'c' * 64
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'plan_hash_mismatch'): self.apply(changed)

    def test_after_plan_frozen_or_platform_drift_fails_without_mutation(self):
        plan = self.plan()
        with self.bridge.connect() as c: c.execute("UPDATE x_post_queue SET description='new text' WHERE id=5392")
        before = self.dump(self.central)
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'plan_state_drift'): self.apply(plan)
        self.assertEqual(self.dump(self.central), before)


if __name__ == '__main__': unittest.main()
