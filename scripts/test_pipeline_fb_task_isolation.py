"""Offline regressions for FB material quarantine and stale native ownership."""
import hashlib
import json
import threading
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import test_pipeline_fb_source_locks as fixtures
from features.fb_auto_posts.publisher import GraphResult
from features.fb_video_publish.worker import Worker
from features.pipeline_dispatch.adapters import AdapterError, LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.core import DispatchStore, DispatchError, dumps, stamp
from features.pipeline_dispatch.service import Coordinator


class FBTaskIsolationTests(unittest.TestCase):
    video_job = fixtures.FBSourceLockTests.video_job
    auto_job = fixtures.FBSourceLockTests.auto_job
    operation = fixtures.FBSourceLockTests.operation

    def setUp(self):
        fixtures.FBSourceLockTests.setUp(self)
        self.store = DispatchStore(Path(self.temp.name) / "dispatch.db", lambda: self.now)
        self.refresh_node()
        self.store.channel_enabled("fb", True)
        self.adapter = LedgerAdapter(LedgerSpec("fb", "video", self.video.path, "fb_video_task", identity="task_id"), driver=object())

    def refresh_node(self):
        self.store.register_node("cpu-main", ["publish:fb", "prepare:fb", "backend:cpu"])
        for worker in ("one", "two"):
            self.store.worker_heartbeat("cpu-main", worker, "publish")
        self.store.worker_heartbeat("cpu-main", "prep", "prepare")

    def offer(self, job, **changes):
        offer = self.adapter.project(self.adapter.read(job["source_id"]), now=self.now)
        offer.update(changes)
        return self.store.offer(offer)

    def claim_next(self, worker="one", stage="publish"):
        return self.store.claim("cpu-main", worker, stage)["job"]

    def set_state(self, job, state, **fields):
        fields = dict(fields, status=state)
        with self.video.connect() as conn:
            conn.execute("UPDATE fb_video_task SET " + ",".join(key + "=?" for key in fields) + " WHERE task_id=?",
                         (*fields.values(), job["source_id"]))

    def resources(self, table="pipeline_resource"):
        with closing(self.store.connect()) as conn:
            return [tuple(row) for row in conn.execute("SELECT resource_key,job_id FROM " + table + " ORDER BY resource_key,job_id")]

    def auto_skipped_observation(self, identity):
        job = self.auto_job(identity, str(identity + 1000))
        adapter = LedgerAdapter(LedgerSpec("fb", "auto", self.auto.path, "fb_auto_task"), driver=object())
        with self.auto.connect() as conn:
            conn.execute("UPDATE fb_auto_task SET status='planned',media_url='',prepared_media_url='',prepared_at_utc='' WHERE id=?", (identity,))
        existing = self.store.offer(adapter.project(adapter.read(str(identity)), now=self.now))
        with self.auto.connect() as conn:
            conn.execute("UPDATE fb_auto_task SET status='skipped',skip_reason='fb_auto_page_unknown_block' WHERE id=?", (identity,))
        skipped = self.store.offer(adapter.project(adapter.read(str(identity)), now=self.now))
        self.assertEqual((skipped['id'], skipped['status'], skipped['hold_reason']), (existing['id'], 'completed', 'metadata_only'))
        return job, adapter, skipped

    def restore_auto_skip(self, adapter, identity, *, marker=True):
        recovery = dict(operation_id='a' * 32, original_skip_reason='fb_auto_page_unknown_block',
                        original_planned_publish_at_utc=self.now.isoformat())
        with self.auto.connect() as conn:
            conn.execute("UPDATE fb_auto_task SET status='planned',skip_reason='',selection_json=? WHERE id=?",
                         (json.dumps({'recovery': recovery}) if marker else '{}', identity))
        return adapter.project(adapter.read(str(identity)), now=self.now)

    def test_explicit_never_executed_auto_skip_rearms_original_preparation_once(self):
        job, adapter, old = self.auto_skipped_observation(80)
        offer = self.restore_auto_skip(adapter, 80)
        self.assertEqual(offer['fb_skipped_recovery'], 'a' * 32)
        restored = self.store.offer(offer)
        self.assertEqual((restored['id'], restored['status'], restored['generation']), (old['id'], 'queued', 0))
        self.assertEqual(restored['hold_reason'], '')
        self.assertEqual(self.store.offer(offer)['id'], old['id'])
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_event WHERE event_type='fb_skipped_recovery_offered' AND job_id=?", (old['id'],)).fetchone()[0], 1)
        claimed = self.claim_next('prep', 'prepare')
        self.assertEqual(claimed['source_key'], job['source_key'])
        self.assertEqual(self.store.offer(offer)['status'], 'running')

    def test_plain_native_status_reset_cannot_rearm_skipped_observation(self):
        _, adapter, old = self.auto_skipped_observation(81)
        offer = self.restore_auto_skip(adapter, 81, marker=False)
        self.assertEqual(offer['fb_skipped_recovery'], '')
        self.assertEqual(self.store.offer(offer)['status'], 'completed')
        self.assertIsNone(self.claim_next('prep', 'prepare'))

    def test_recovery_does_not_rearm_terminal_unknown_or_claimed_central_facts(self):
        changes = ({'status': 'unknown'}, {'status': 'published'}, {'status': 'failed'},
                   {'generation': 1}, {'external_intent': 1}, {'claim_hash': 'prior'},
                   {'canonical_receipt': '{}'}, {'hold_reason': 'cutover_overdue'},
                   {'lease_expires_at': stamp(self.now + timedelta(minutes=1))})
        for identity, fields in enumerate(changes, 90):
            with self.subTest(fields=fields):
                _, adapter, old = self.auto_skipped_observation(identity)
                with self.store.transaction() as conn:
                    conn.execute("UPDATE pipeline_job SET " + ','.join(k+'=?' for k in fields) + " WHERE id=?", (*fields.values(), old['id']))
                result = self.store.offer(self.restore_auto_skip(adapter, identity))
                self.assertNotEqual(result['status'], 'queued')
                with closing(self.store.connect()) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_event WHERE event_type='fb_skipped_recovery_offered' AND job_id=?", (old['id'],)).fetchone()[0], 0)

    def test_recovery_preserves_central_operation_and_material_quarantine(self):
        for identity, table in ((110, 'pipeline_operation'), (111, 'pipeline_legacy_resource')):
            with self.subTest(table=table):
                _, adapter, old = self.auto_skipped_observation(identity)
                with self.store.transaction() as conn:
                    if table == 'pipeline_operation':
                        conn.execute("INSERT INTO pipeline_operation VALUES(?,?,0,'node','fb_video_publish',?,'fb:page:100','intent',?,?)",
                                     ('op-prior', old['id'], 'a'*64, stamp(self.now), stamp(self.now)))
                    else:
                        conn.execute("INSERT INTO pipeline_legacy_resource VALUES(?,?)", ('fb:material:100:111', old['id']))
                result = self.store.offer(self.restore_auto_skip(adapter, identity))
                self.assertEqual(result['status'], 'completed')
                with closing(self.store.connect()) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM " + table + " WHERE job_id=?", (old['id'],)).fetchone()[0], 1)

    def test_native_recovery_proof_excludes_publishing_evidence_and_holds(self):
        cases = ('ledger', 'operation', 'claim', 'hold', 'attempt_count', 'unknown', 'graph', 'lease', 'started')
        for identity, case in enumerate(cases, 120):
            with self.subTest(case=case):
                _, adapter, _ = self.auto_skipped_observation(identity)
                self.restore_auto_skip(adapter, identity)
                key = 'fb:auto:' + str(identity)
                with self.auto.connect() as conn:
                    if case == 'ledger':
                        conn.execute("INSERT INTO fb_auto_publish_ledger VALUES(?, '100', ?, 'submitted', 'post', 1, 0, '', ?, ?)",
                                     (identity, str(identity), self.now.isoformat(), self.now.isoformat()))
                    elif case == 'operation':
                        conn.execute("INSERT INTO pipeline_fb_operation(operation_id,source_key,stage,generation,request_sha256,credential_id,fb_user_id,native_attempt_id,sequence) VALUES(?,?,'publish',1,'hash','cred','user','attempt',1)",
                                     ('native-op-'+str(identity), key))
                    elif case == 'claim':
                        conn.execute("INSERT INTO pipeline_fb_claim(source_key,stage,generation,claim_hash,task_json,descriptor_json) VALUES(?,'prepare',1,'hash','{}','{}')", (key,))
                    elif case == 'hold':
                        conn.execute("INSERT INTO pipeline_canonical_hold VALUES(?,?,?)", (key, self.now.isoformat(), 'cutover'))
                    else:
                        field, value = {'attempt_count': ('attempt_count', 1), 'unknown': ('unknown_outcome', 1),
                                        'graph': ('graph_post_id', '1234'), 'lease': ('lease_owner', 'owner'),
                                        'started': ('started_at_utc', self.now.isoformat())}[case]
                        conn.execute('UPDATE fb_auto_task SET '+field+'=? WHERE id=?', (value, identity))
                self.assertEqual(adapter.project(adapter.read(str(identity)), now=self.now)['fb_skipped_recovery'], '')

    def test_legacy_page_quarantine_migrates_and_duplicate_cannot_fifo_block_other_material(self):
        old = self.video_job(1)
        self.set_state(old, "unknown", unknown_outcome=1)
        original = self.adapter.project(self.adapter.read(old["source_id"]), now=self.now)
        # Simulate a job collected by the previously deployed Page-only adapter.
        legacy = {key: value for key, value in original.items() if not key.startswith("fb_")}
        legacy["resource_keys"] = ["fb:page:100"]
        saved = self.store.offer(legacy)
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO pipeline_resource VALUES(?,?,0,1,1)", ("fb:page:100", saved["id"]))
        duplicate, later = self.video_job(2), self.video_job(3)
        # A historical central queued duplicate must not impose Page FIFO.
        self.offer(duplicate, fb_material_id="1", resource_keys=["fb:page:100", "fb:material:100:1"])
        later_saved = self.offer(later)
        self.assertIsNone(self.claim_next())
        self.offer(old); self.offer(old)
        self.assertEqual(self.resources(), [("fb:material:100:1", saved["id"])])
        self.assertEqual(self.resources("pipeline_legacy_resource"), [("fb:material:100:1", saved["id"])])
        self.assertEqual(self.claim_next()["id"], later_saved["id"])
        with closing(self.store.connect()) as conn:
            keys = json.loads(conn.execute("SELECT resource_keys_json FROM pipeline_job WHERE id=?", (saved["id"],)).fetchone()[0])
            events = conn.execute("SELECT COUNT(*) FROM pipeline_event WHERE job_id=? AND event_type='fb_material_isolation_refreshed'", (saved["id"],)).fetchone()[0]
        self.assertEqual(keys, ["fb:material:100:1", "fb:page:100"])
        self.assertEqual(events, 1)

    def test_two_unknowns_keep_separate_material_fences_when_one_resolves(self):
        old, second, later = self.video_job(4), self.video_job(5), self.video_job(6)
        for job in (old, second): self.set_state(job, "unknown", unknown_outcome=1)
        first = self.offer(old); other = self.offer(second); self.offer(later)
        self.set_state(old, "failed", unknown_outcome=0)
        self.offer(old)
        self.assertEqual(self.resources("pipeline_legacy_resource"), [("fb:material:100:5", other["id"])])
        self.assertNotEqual(self.claim_next()["id"], first["id"])

    def test_two_historical_unknowns_of_same_material_both_need_resolution(self):
        old, duplicate = self.video_job(40), self.video_job(41)
        self.set_state(old, "unknown", unknown_outcome=1)
        first = self.offer(old)
        second = self.offer(duplicate, status="unknown", source_status="unknown", unknown_outcome=True,
                            fb_material_id="40", resource_keys=["fb:page:100", "fb:material:100:40"])
        queued = self.video_job(42)
        self.offer(queued, fb_material_id="40", resource_keys=["fb:page:100", "fb:material:100:40"])
        self.assertIsNone(self.claim_next())
        self.store.reconcile_native(first["id"], {"definitive_failure": True, "canonical_receipt": {"source_key": old["source_key"]}})
        self.assertIsNone(self.claim_next())
        self.assertEqual(self.resources("pipeline_legacy_resource"), [("fb:material:100:40", second["id"])])
        self.store.reconcile_native(second["id"], {"definitive_failure": True, "canonical_receipt": {"source_key": duplicate["source_key"]}})
        self.assertEqual(self.claim_next()["source_key"], queued["source_key"])

    def test_active_native_lease_survives_short_central_expiry_then_detaches(self):
        old, later = self.video_job(7), self.video_job(8)
        self.offer(old)
        claim = self.claim_next()
        native_job = dict(old, generation=claim["generation"])
        descriptor = self.bridge.reserve(native_job, claim)
        self.bridge.begin_operation(native_job, claim, self.operation(descriptor))
        self.store.begin_operation(claim, dict(self.operation(descriptor), operation="fb_video_publish"))
        later_saved = self.offer(later)
        self.now += timedelta(seconds=121); self.refresh_node(); self.store.expire()
        self.offer(old)
        self.assertIsNone(self.claim_next("two"))
        self.assertIn(("fb:page:100", claim["id"]), self.resources())
        # Native timeout changes ownership, not evidence: old write stays unknown.
        self.now += timedelta(seconds=1200); self.refresh_node(); self.bridge._next_tick = 0
        self.assertEqual(self.bridge.reconcile(native_job)["status"], "unknown")
        self.offer(old)
        successor = self.claim_next("two")
        self.assertEqual(successor["id"], later_saved["id"])
        with self.assertRaisesRegex(DispatchError, "claim_expired"):
            self.store.begin_operation(claim, self.operation(descriptor, "stale"))
        with self.assertRaisesRegex(AdapterError, "canonical_claim_stale|previous_operation_unresolved"):
            self.bridge.begin_operation(native_job, claim, self.operation(descriptor, "stale"))

    def test_late_exact_receipt_does_not_release_successor_page(self):
        old, later = self.video_job(9), self.video_job(10)
        self.offer(old); claim = self.claim_next()
        descriptor = self.bridge.reserve(old, claim)
        operation = self.operation(descriptor)
        self.bridge.begin_operation(old, claim, operation)
        self.store.begin_operation(claim, operation)
        self.now += timedelta(seconds=1300); self.refresh_node(); self.store.expire()
        self.bridge.reconcile(old); self.offer(old); later_saved = self.offer(later)
        successor = self.claim_next("two")
        self.assertEqual(successor["id"], later_saved["id"])
        receipt = {"operation_id": operation["operation_id"], "kind": "success", "post_id": "123456"}
        self.bridge.checkpoint(old, claim, "fb_publish_attempt", receipt)
        self.bridge.complete(old, receipt, claim); self.offer(old)
        self.assertIn(("fb:page:100", successor["id"]), self.resources())
        self.assertIn(("fb:material:100:9", claim["id"]), self.resources("pipeline_legacy_resource"))
        self.set_state(old, "published", graph_post_id="123456", unknown_outcome=0)
        self.offer(old)
        self.assertNotIn(("fb:material:100:9", claim["id"]), self.resources("pipeline_legacy_resource"))
        self.assertIn(("fb:page:100", successor["id"]), self.resources())

    def test_prepare_does_not_hold_page_while_another_material_is_unknown(self):
        old, prep = self.video_job(11), self.video_job(12)
        self.set_state(old, "unknown", unknown_outcome=1)
        self.set_state(prep, "queued", prepare_state="pending", processing_mode="random_overlay", prepared_json="{}")
        self.offer(old); self.offer(prep)
        claimed = self.claim_next("prep", "prepare")
        self.assertEqual(claimed["source_key"], prep["source_key"])
        self.assertEqual(claimed["resource_keys"], ["fb:material:100:12"])

    def test_unidentified_legacy_unknown_isolates_original_task_only(self):
        old, later = self.video_job(13), self.video_job(14)
        self.set_state(old, "unknown", material_id="invalid", unknown_outcome=1)
        saved = self.offer(old); self.offer(later)
        self.assertEqual(self.resources("pipeline_legacy_resource"), [("task:" + saved["source_key"], saved["id"])])
        self.assertIsNotNone(self.claim_next())

    def test_empty_credential_preflight_failure_is_not_platform_attempt(self):
        job = self.video_job(15)
        with patch.object(self.bridge, "_video_preflight", side_effect=AdapterError("source_mysql_failed")):
            with self.assertRaisesRegex(AdapterError, "source_mysql_failed"):
                self.bridge.reserve(job, self.claim)
        self.set_state(job, "queued")  # Only explicit, independently verified recovery.
        row = self.adapter.read(job["source_id"])
        self.assertEqual(row["publish_attempt_count"], 0)
        projected = self.adapter.project(row)
        self.assertTrue(projected["execution_available"])
        self.assertFalse(projected["reconcile_only"])
        with self.video.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_video_attempt").fetchone()[0], 1)

    def test_pipeline_operation_alone_is_still_unknown_even_if_native_attempt_missing(self):
        job = self.video_job(16)
        descriptor = self.bridge.reserve(job, self.claim)
        self.bridge.begin_operation(job, self.claim, self.operation(descriptor))
        with self.video.connect() as conn:
            conn.execute("DELETE FROM fb_video_attempt WHERE task_id=?", (job["source_id"],))
        self.set_state(job, "queued", execution_lease="", execution_until="")
        projected = self.adapter.project(self.adapter.read(job["source_id"]))
        self.assertEqual(projected["source_attempt_count"], 1)
        self.assertTrue(projected["reconcile_only"])

    def test_expired_descriptor_without_platform_intent_fails_and_keeps_claim_history(self):
        job = self.video_job(17)
        descriptor = self.bridge.reserve(job, self.claim)
        self.now += timedelta(seconds=1201)
        self.assertEqual(self.bridge.reconcile(job)["status"], "failed")
        with self.video.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_fb_claim").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_fb_operation").fetchone()[0], 0)
        with self.assertRaisesRegex(AdapterError, "canonical_claim_stale"):
            self.bridge.begin_operation(job, self.claim, self.operation(descriptor))

    def test_pending_source_native_busy_is_deferred_not_failed(self):
        first, later = self.video_job(18), self.video_job(19)
        self.bridge.reserve(first, self.claim)
        with self.assertRaisesRegex(AdapterError, "canonical_account_busy"):
            self.bridge.reserve(later, self.claim)
        self.assertEqual(self.adapter.read(later["source_id"])["status"], "queued")

    def test_old_submitted_get_readback_converges_without_post_or_collector_wait(self):
        job = self.video_job(20)
        self.set_state(job, "submitted", graph_post_id="234567", reconcile_at="")
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        graph = Mock()
        def readback(object_id, credential):
            self.assertEqual(object_id, "234567")
            entered.set()
            if not release.wait(3): raise RuntimeError("fixture timeout")
            return GraphResult("success", post_id=object_id)
        graph.reconcile_video.side_effect = readback
        worker = Worker(self.video, self.runtime.pages, None, graph, live_enabled=True)
        worker._task_credentials = self.runtime.manual_worker._task_credentials
        original = worker.reconcile_next
        def reconcile(*args, **kwargs):
            try: return original(*args, **kwargs)
            finally: finished.set()
        worker.reconcile_next = reconcile
        self.runtime.manual_worker = worker
        with patch("features.pipeline_dispatch.fb_native.RequestsGraphTransport", return_value=graph) as transport:
            try:
                self.assertEqual(self.bridge.reconcile(job)["status"], "submitted")
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.bridge.reconcile(job)["status"], "submitted")
                self.assertEqual(graph.reconcile_video.call_count, 1)
                transport.assert_called_once_with(api_version=graph.api_version, timeout_seconds=10)
            finally:
                release.set(); self.assertTrue(finished.wait(2))
        self.assertEqual(self.bridge.reconcile(job)["status"], "published")
        graph.publish_video.assert_not_called()

    def test_slow_credential_lookup_never_returns_token_after_central_lease_expiry(self):
        native = SimpleNamespace()
        service = Coordinator(self.store, native, internal_token="i" * 32, master_key="m" * 32)
        job = self.video_job(21); self.offer(job); claim = self.claim_next()
        claim["job_id"] = claim["id"]
        def issue(*args):
            self.now += timedelta(seconds=121)
            return {"credentials": {"access_token": "fixture-private"}}
        native.begin_operation = issue
        details = {"method": "POST"}
        operation = dict(claim, operation_id="operation-slow-lookup", operation="fb_video_publish",
                         account_resource="fb:page:100", details=details,
                         request_sha256=hashlib.sha256(dumps(details).encode()).hexdigest())
        with self.assertRaisesRegex(DispatchError, "claim_expired"):
            service.route("/internal/pipeline/jobs/begin-operation", operation, {"internal": True})
        self.assertEqual(self.store.get_job(claim["id"])["status"], "unknown")

    def test_known_id_unknown_waits_for_live_write_then_gets_readback(self):
        job = self.video_job(22)
        self.set_state(job, "unknown", unknown_outcome=1, graph_post_id="345678", reconcile_at="",
                       execution_lease="prior-write", execution_until=stamp(self.now + timedelta(seconds=100)))
        graph, finished = Mock(), threading.Event()
        graph.reconcile_video.return_value = GraphResult("success", post_id="345678")
        worker = Worker(self.video, self.runtime.pages, None, graph, live_enabled=True)
        worker._task_credentials = self.runtime.manual_worker._task_credentials
        reconcile = worker.reconcile_next
        def finish(*args, **kwargs):
            try: return reconcile(*args, **kwargs)
            finally: finished.set()
        worker.reconcile_next = finish
        self.runtime.manual_worker = worker
        with patch("features.pipeline_dispatch.fb_native.RequestsGraphTransport", return_value=graph):
            self.assertEqual(self.bridge.reconcile(job)["status"], "unknown")
            graph.reconcile_video.assert_not_called()
            self.now += timedelta(seconds=101)
            self.bridge.reconcile(job)
            self.assertTrue(finished.wait(2))
        self.assertEqual(self.bridge.reconcile(job), {"status": "published", "graph_post_id": "345678", "unknown_outcome": 0})
        graph.publish_video.assert_not_called()

    def test_readback_exception_does_not_block_other_source(self):
        first, other = self.video_job(23), self.video_job(24)
        for job in (first, other): self.set_state(job, "submitted", graph_post_id="post" + job["source_id"])
        completed = threading.Event()
        def reconcile(*args, task_id=None, **kwargs):
            if task_id == first["source_id"]:
                raise RuntimeError("fixture unavailable")
            completed.set()
        self.runtime.manual_worker.reconcile_next = reconcile
        with patch("features.pipeline_dispatch.fb_native.logging.getLogger") as logger:
            self.bridge.reconcile(first)
            self.bridge.reconcile(other)
            self.assertTrue(completed.wait(2))
            self.bridge._reconcile_queue.join()
            logger.return_value.warning.assert_called_once()
        self.assertEqual(self.adapter.read(first["source_id"])["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
