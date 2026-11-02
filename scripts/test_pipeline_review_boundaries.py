"""Independent SQLite acceptance checks for monitoring and dispatch boundaries."""
import datetime as dt
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.core import DispatchStore, DispatchError, stamp
from features.pipeline_dispatch.service import Coordinator
from features.pipeline_dispatch.owners import OwnerResolver
from features.pipeline_dispatch import views
from scripts import test_pipeline_coordinator as coordinator_fixtures
from scripts import test_pipeline_adapters as adapter_fixtures


class ReviewBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = dt.datetime(2026, 11, 2, 10, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(Path(self.temp.name) / "dispatch.sqlite", clock=lambda: self.now)
        self.addCleanup(views.invalidate_cache, self.store)
        self.admin = {"is_admin": True, "allowed_channels": ["x", "fb", "tt", "youtube"]}
        self.store.register_node("cpu-main", ["publish:x", "prepare:x"])
        self.store.worker_heartbeat("cpu-main", "pub", "publish")
        self.store.channel_enabled("x", True)

    def offer(self, key, **patch):
        value = {"source_key": key, "source_id": key, "channel": "x", "family": "queue", "stage": "publish",
            "owner_user_id": "alice", "owner_tenant_key": "tenant", "required_module": "x_accounts",
            "resource_keys": ["x:account:1"], "capabilities": ["publish:x"], "scheduled_at": stamp(self.now),
            "source_created_at": stamp(self.now)}
        value.update(patch)
        return self.store.offer(value)

    def test_worker_publish_totals_exclude_appointments(self):
        appointment = self.offer("appointment", status="completed")
        published = self.offer("published", status="published")
        with self.store.transaction() as conn:
            conn.execute("UPDATE pipeline_job SET node_id='cpu-main',started_at=?,finished_at=? WHERE id IN (?,?)",
                (stamp(self.now-dt.timedelta(seconds=5)), stamp(self.now), appointment["id"], published["id"]))
        self.store.record_publication(published["id"], {"source_key": "published", "publish_id": "post-1", "published_at": stamp(self.now)}, canonical_verified=True)
        result = views.query(self.store, "workers", self.admin, {})
        host = next(row for row in result["items"] if row["host_id"] == "cpu-main")
        publish = next(row for row in host["stages"] if row["stage"] == "publish")
        self.assertEqual((publish["completed_today"], publish["throughput_per_hour"]), (1, 1))
        summary = views.query(self.store, "summary", self.admin, {})
        self.assertEqual(summary["totals"]["published"], 1)

    def test_video_publication_and_unknown_comment_are_independent_facts(self):
        video = self.offer("youtube:publish:1", channel="youtube", status="unknown", required_module="youtube_auto_publish")
        failed_comment = self.offer("youtube:publish:2", channel="youtube", status="failed", required_module="youtube_auto_publish", finished_at=stamp(self.now))
        self.offer("youtube:publish:3", channel="youtube", status="failed", required_module="youtube_auto_publish", finished_at=stamp(self.now))
        for row in (video, failed_comment):
            proof = {"source_key": row["source_key"], "publish_id": "video-"+str(row["id"]), "video_published_at": stamp(self.now)}
            self.store.record_publication(row["id"], proof, canonical_verified=True)
            self.store.record_publication(row["id"], proof, canonical_verified=True)
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO pipeline_phase_metric VALUES(?,0,'platform_upload',11,NULL,?)", (video["id"], stamp(self.now)))
        data = views.query(self.store, "summary", self.admin, {})
        self.assertEqual(data["totals"]["published"], 2)
        self.assertEqual(data["totals"]["unknown"], 1)
        self.assertEqual(data["totals"]["failed"], 2)
        channel = next(row for row in data["channels"] if row["channel"] == "youtube" and row["stage"] == "publish")
        self.assertEqual((channel["completed"], channel["throughput_per_hour"], channel["failed_without_publication"]), (2, 2, 1))
        self.assertAlmostEqual(channel["success_rate"], 2/3)
        phase = next(row for row in data["efficiency"] if row["channel"] == "youtube" and row["stage"] == "publish" and row["phase"] == "platform_upload")
        self.assertEqual((phase["samples"], phase["p50_seconds"]), (1, 11))

    def test_prepare_requires_portable_artifact_and_keeps_stage_metrics_separate(self):
        self.offer("stages", stage="prepare", capabilities=["prepare:x"])
        self.store.worker_heartbeat("cpu-main", "prep", "prepare")
        claim = self.store.claim("cpu-main", "prep", "prepare")["job"]
        with self.assertRaises(DispatchError): self.store.complete(claim, "missing-artifact", {}, canonical_verified=True)
        self.now += dt.timedelta(seconds=20)
        result = {"artifact": {"url": "https://media.example.test/output.mp4", "sha256": "a"*64, "size": 123}, "phase_metrics": {"download": 3, "render": 12}}
        self.store.complete(claim, "prepared", result, canonical_verified=True)
        self.store.complete(claim, "prepared", result, canonical_verified=True)
        self.offer("stages")
        data = views.query(self.store, "summary", self.admin, {})
        self.assertEqual((data["totals"]["prepared"], data["totals"]["published"]), (1, 0))
        render = next(row for row in data["efficiency"] if row["channel"] == "x" and row["stage"] == "prepare" and row["phase"] == "render")
        self.assertEqual((render["samples"], render["p50_seconds"], render["p95_seconds"]), (1, 12, 12))

    def test_historical_publication_time_can_be_filled_without_fabricated_success(self):
        row = self.offer("historical", status="published")
        with self.assertRaises(DispatchError): self.store.record_publication(row["id"], {"source_key": "historical"}, canonical_verified=True)
        proof = {"source_key": "historical", "publish_id": "confirmed-post", "historical": True}
        self.store.record_publication(row["id"], proof, canonical_verified=True)
        self.store.source_health("x", "queue")
        self.assertEqual(views.query(self.store, "summary", self.admin, {})["totals"]["published"], 0)
        with self.store.transaction() as conn:
            conn.execute("UPDATE pipeline_job SET node_id='cpu-main' WHERE id=?", (row["id"],))
        proof["published_at"] = stamp(self.now-dt.timedelta(minutes=2))
        self.store.record_publication(row["id"], proof, canonical_verified=True)
        views.invalidate_cache(self.store)
        self.assertEqual(views.query(self.store, "summary", self.admin, {})["totals"]["published"], 1)
        with self.assertRaises(DispatchError): self.store.record_publication(row["id"], {**proof, "publish_id": "other-post"}, canonical_verified=True)

    def test_snapshot_install_rolls_back_and_recovers_missing_trigger(self):
        from features.pipeline_dispatch import snapshots
        self.offer("snapshot")
        conn = self.store.connect()
        self.addCleanup(conn.close)
        for trigger in snapshots.TRIGGERS: conn.execute("DROP TRIGGER " + trigger)
        conn.execute("DELETE FROM pipeline_monitor_counts")
        class InterruptedConnection:
            def execute(self, sql, *args):
                if sql.startswith("CREATE TRIGGER " + snapshots.TRIGGERS[1]): raise RuntimeError("interrupted install")
                return conn.execute(sql, *args)
        with self.assertRaisesRegex(RuntimeError, "interrupted install"):
            snapshots.install(InterruptedConnection())
        self.assertFalse(conn.in_transaction)
        snapshots.install(conn)
        self.assertEqual(conn.execute("SELECT SUM(n) FROM pipeline_monitor_counts").fetchone()[0], 1)
        with self.store.transaction() as write:
            write.execute("UPDATE pipeline_job SET hold_reason='metadata_only'")
        self.assertEqual(conn.execute("SELECT COALESCE(SUM(n),0) FROM pipeline_monitor_counts").fetchone()[0], 0)
        with self.store.transaction() as write:
            write.execute("UPDATE pipeline_job SET status='completed',finished_at=?,hold_reason=''", (stamp(self.now),))
        self.assertEqual(conn.execute("SELECT SUM(n) FROM pipeline_monitor_counts").fetchone()[0], 1)
        snapshots.install(conn)
        self.assertEqual(conn.execute("SELECT SUM(n) FROM pipeline_monitor_counts").fetchone()[0], 1)

    def test_metadata_only_is_excluded_from_counts_and_timing(self):
        row = self.offer("planning", stage="prepare", status="completed", started_at=stamp(self.now-dt.timedelta(seconds=10)), finished_at=stamp(self.now))
        with self.store.transaction() as conn:
            conn.execute("UPDATE pipeline_job SET hold_reason='metadata_only' WHERE id=?", (row["id"],))
            conn.execute("INSERT INTO pipeline_phase_metric VALUES(?,0,'render',10,NULL,?)", (row["id"], stamp(self.now)))
        self.store.source_health("x", "queue")
        data = views.query(self.store, "summary", self.admin, {})
        self.assertEqual(data["totals"]["prepared"], 0)
        channel = next(row for row in data["channels"] if row["channel"] == "x" and row["stage"] == "prepare")
        self.assertEqual(channel["duration_samples"], 0)
        self.assertFalse(any(row["samples"] for row in data["efficiency"]))

    def test_fb_numeric_owner_does_not_bypass_manual_tenant_scope(self):
        self.offer("auto-mine", channel="fb", required_module="fb_page_posts", owner_user_id="42", owner_tenant_key="", owner_namespace="fb_page_owner")
        self.offer("auto-other", channel="fb", required_module="fb_page_posts", owner_user_id="43", owner_tenant_key="", owner_namespace="fb_page_owner")
        self.offer("manual-mine", channel="fb", required_module="fb_video_publish")
        self.offer("manual-other-tenant", channel="fb", required_module="fb_video_publish", owner_tenant_key="other")
        self.offer("manual-numeric-collision", channel="fb", required_module="fb_video_publish", owner_user_id="42", owner_tenant_key="other")
        actor = {"is_admin": False, "user_id": "alice", "tenant_key": "tenant", "fb_owner_user_id": "42",
            "allowed_channels": ["fb"], "allowed_modules": ["fb_page_posts", "fb_video_publish"]}
        result = views.query(self.store, "tasks", actor, {})
        self.assertEqual({row["source_key"] for row in result["items"]}, {"auto-mine", "manual-mine"})
        actor["allowed_modules"] = ["fb_video_publish"]
        self.assertEqual({row["source_key"] for row in views.query(self.store, "tasks", actor, {})["items"]}, {"manual-mine"})

    def test_owner_resolution_only_unambiguous_session_identity(self):
        path = Path(self.temp.name) / "owners.sqlite"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE drama_admin_user(user_id TEXT,tenant_key TEXT,name TEXT,status TEXT)")
            conn.executemany("INSERT INTO drama_admin_user VALUES(?,?,?,'active')", [("alice", "tenant", "Alice"), ("duplicate", "t1", "One"), ("duplicate", "t2", "Two"), ("42", "tenant", "Numeric")])
        conn.close()
        offers = [{"owner_user_id": key, "owner_namespace": namespace} for key, namespace in (("alice", "session_user"), ("duplicate", "session_user"), ("42", "fb_page_owner"))]
        OwnerResolver(path).resolve(offers)
        self.assertEqual(offers[0]["owner_tenant_key"], "tenant")
        self.assertNotIn("owner_tenant_key", offers[1])
        self.assertNotIn("owner_tenant_key", offers[2])

    def test_later_legacy_observation_installs_and_releases_resource_fence(self):
        first = self.offer("old")
        self.offer("old", status="legacy_running", legacy_inflight=True)
        self.assertEqual(self.store.get_job(first["id"])["status"], "legacy_running")
        self.offer("new")
        self.assertIsNone(self.store.claim("cpu-main", "pub", "publish")["job"])
        self.offer("old", status="unknown")
        self.assertIsNone(self.store.claim("cpu-main", "pub", "publish")["job"])
        self.offer("old", status="published", finished_at=stamp(self.now))
        self.assertEqual(self.store.claim("cpu-main", "pub", "publish")["job"]["source_key"], "new")

    def test_existing_child_inherits_only_its_exact_inflight_ancestor(self):
        ancestor = self.offer("x:auto:ancestor", stage="prepare", capabilities=["prepare:x"])
        child = self.offer("x:queue:child", ancestor_source_keys=[ancestor["source_key"]])
        unrelated = self.offer("x:queue:unrelated", batch_key="same-batch")
        self.offer(ancestor["source_key"], stage="prepare", status="legacy_running", legacy_inflight=True, capabilities=["prepare:x"])
        self.store.cutover("x", stamp(self.now), [ancestor["source_key"]])
        self.assertTrue(self.store.get_job(child["id"])["grandfathered"])
        self.assertEqual(self.store.get_job(child["id"])["status"], "queued")
        self.assertEqual(self.store.get_job(unrelated["id"])["status"], "backlog_paused")

    def test_pending_canonical_receipt_keeps_account_unknown(self):
        native = coordinator_fixtures.NativeLedger()
        service = Coordinator(self.store, native, internal_token="i"*32, master_key="m"*32)
        self.offer("pending")
        job = service.route("/internal/pipeline/jobs/claim", {"node_id": "cpu-main", "worker_id": "pub", "stage": "publish"}, {"internal": True})["job"]
        result = service.route("/internal/pipeline/jobs/complete", {**job, "job_id": job["id"], "receipt_id": "pending-receipt", "result": {}}, {"internal": True})
        self.assertEqual(result["status"], "unknown")
        self.offer("next")
        self.assertIsNone(self.store.claim("cpu-main", "pub", "publish")["job"])
        self.assertEqual(views.query(self.store, "summary", self.admin, {})["totals"]["published"], 0)

    def test_collector_exposes_partial_history_then_converges_without_double_count(self):
        fixture = adapter_fixtures.ProjectionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for identity in range(1, 206): fixture.add(identity, "published")
        service = Coordinator(self.store, fixture.registry, internal_token="i"*32, master_key="m"*32)
        service.scan()
        partial = views.query(self.store, "summary", self.admin, {})
        self.assertTrue(any(row["state"] in ("collecting", "backfilling") for row in partial["sources"]))
        self.assertTrue(partial["warnings"])
        service.scan()
        service.scan()
        with self.store.transaction() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_job WHERE channel='tt'").fetchone()[0], 205)
            self.assertEqual(conn.execute("SELECT SUM(n) FROM pipeline_monitor_counts WHERE channel='tt'").fetchone()[0], 205)
        views.invalidate_cache(self.store)
        final = views.query(self.store, "summary", self.admin, {})
        self.assertEqual(next(row for row in final["sources"] if row["channel"] == "tt")["state"], "healthy")


if __name__ == "__main__":
    unittest.main()
