"""Monitor privacy, canonical counts, cursor and dynamic-node regressions."""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch import monitor, views
from features.pipeline_dispatch.core import DispatchStore, DispatchError


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.responses = [], []
        self.session = {"user_id": "alice", "tenant_key": "tenant", "role": "user", "auth_type": "cookie", "permissions": {"x_accounts": True, "fb_video_publish": True}}
        self.handler = SimpleNamespace(command="GET", _require_auth=lambda: True,
                                       _session=lambda: self.session,
                                       _require_same_origin_json=lambda: True,
                                       _read_json=lambda: {})
        self.app = {"json_response": lambda h, s, b, **kw: self.responses.append((s, b, kw)),
                    "has_module_permission": lambda s, m: bool(s.get("permissions", {}).get(m))}

    def dispatch(self, path, result=None):
        def request(*args):
            self.calls.append(args)
            return result or {"items": []}
        with patch.object(monitor, "coordinator_request", request):
            monitor.dispatch(self.handler, urlparse(path), self.app)
        return self.responses[-1]

    def test_actor_is_server_derived_and_fb_modules_remain_separate(self):
        status, _, headers = self.dispatch("/api/pipeline-monitor/tasks?limit=20")
        self.assertEqual(status, 200)
        actor = self.calls[0][2]["actor"]
        self.assertEqual(actor["user_id"], "alice")
        self.assertEqual(actor["allowed_channels"], ["x", "fb"])
        self.assertNotIn("fb_page_posts", actor["allowed_modules"])
        self.assertTrue(headers["no_store"])

    def test_actor_query_injection_is_rejected(self):
        self.assertEqual(self.dispatch("/api/pipeline-monitor/tasks?user_id=bob")[0], 400)
        self.assertEqual(self.calls, [])

    def test_api_tokens_and_unauthorized_channels_are_denied(self):
        self.assertEqual(self.dispatch("/api/pipeline-monitor/tasks?channel=tt")[0], 403)
        self.session["auth_type"] = "api_token"
        self.assertEqual(self.dispatch("/api/pipeline-monitor/summary")[0], 403)
        self.assertEqual(self.calls, [])

    def test_capacity_is_admin_and_same_origin_only(self):
        self.handler.command = "PUT"
        self.assertEqual(self.dispatch("/api/pipeline-monitor/capacity")[0], 403)
        self.session["role"] = "admin"
        self.handler._require_same_origin_json = lambda: False
        with patch.object(monitor, "coordinator_request") as request:
            monitor.dispatch(self.handler, urlparse("/api/pipeline-monitor/capacity"), self.app)
            request.assert_not_called()

    def test_third_host_capacity_and_enrollment(self):
        self.session["role"] = "admin"
        self.handler.command = "PUT"
        self.handler._read_json = lambda: {"host_id": "gpu-third", "stage": "prepare", "max_workers": 0, "expected_version": 8}
        self.assertEqual(self.dispatch("/api/pipeline-monitor/capacity")[0], 200)
        self.assertEqual(self.calls[-1][2]["host_id"], "gpu-third")
        self.handler.command = "POST"
        self.handler._read_json = lambda: {"label": "G" * 100}
        self.assertEqual(self.dispatch("/api/pipeline-monitor/nodes/enroll")[0], 200)
        self.assertEqual(self.calls[-1][1], "/internal/pipeline-monitor/enroll")
        self.handler._read_json = lambda: {"label": "G" * 101}
        self.assertEqual(self.dispatch("/api/pipeline-monitor/nodes/enroll")[0], 400)
        self.assertEqual(len(self.calls), 2)

    def test_navigation_is_additive_and_idempotent(self):
        original = [{"key": "old", "items": [{"key": "old", "enabled": False}]}]
        result = monitor.ensure_navigation(original)
        self.assertEqual(result, monitor.ensure_navigation(result))
        self.assertEqual(len(original), 1)
        self.assertFalse(result[1]["items"][0]["enabled"])

    def test_validation_rejects_boolean_capacity_and_duplicate_filters(self):
        with self.assertRaises(monitor.MonitorError):
            monitor.capacity_payload({"expected_version": 1, "host_id": "a", "stage": "prepare", "max_workers": True}, {})
        with self.assertRaises(monitor.MonitorError):
            monitor.filters_from_query("channel=x&channel=fb")


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = dt.datetime(2026, 11, 2, 9, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(Path(self.temp.name) / "dispatch.sqlite", clock=lambda: self.now)
        self.admin = {"is_admin": True, "user_id": "admin", "tenant_key": "tenant", "allowed_channels": ["x", "fb", "tt", "youtube"], "allowed_modules": []}
        self.alice = {"is_admin": False, "user_id": "alice", "tenant_key": "tenant", "allowed_channels": ["x", "fb"], "allowed_modules": ["x_accounts", "fb_video_publish"]}

    def tearDown(self):
        views.invalidate_cache(self.store)
        self.temp.cleanup()

    def offer(self, key, owner="alice", tenant="tenant", channel="x", module="x_accounts", status="queued", stage="publish"):
        row = self.store.offer({"source_key": key, "source_id": key, "stage": stage, "channel": channel,
                                "family": "manual", "owner_user_id": owner, "owner_tenant_key": tenant,
                                "required_module": module, "status": status,
                                "payload": {"access_token": "must-not-be-visible"},
                                "detail_url": "/x-post-logs.html?id=" + key})
        return row["id"]

    def test_scope_is_enforced_before_totals_and_pages(self):
        mine = self.offer("mine")
        self.offer("other", owner="bob")
        self.offer("other-tenant", tenant="elsewhere")
        self.offer("fb-auto-denied", channel="fb", module="fb_page_posts")
        self.offer("unknown-owner", owner="")
        self.store.source_health("x", "manual")
        self.store.source_health("fb", "auto")
        tasks = views.query(self.store, "tasks", self.alice, {})
        self.assertEqual([r["id"] for r in tasks["items"]], [mine])
        self.assertNotIn("must-not-be-visible", json.dumps(tasks))
        self.assertEqual(views.query(self.store, "summary", self.alice, {})["totals"]["queued"], 1)

    def test_publish_and_prepare_not_double_counted(self):
        prepared = self.offer("same", status="completed", stage="prepare")
        published = self.offer("same", status="published")
        yesterday = self.offer("yesterday", status="published")
        with self.store.transaction() as c:
            c.execute("UPDATE pipeline_job SET started_at='2026-11-02T08:30:00.000+00:00',finished_at='2026-11-02T08:40:00.000+00:00' WHERE id IN (?,?)", (prepared, published))
            c.execute("UPDATE pipeline_job SET finished_at='2026-11-01T00:00:00.000+00:00' WHERE id=?", (yesterday,))
        self.store.record_publication(published, {"source_key": "same", "publish_id": "platform-same", "published_at": "2026-11-02T08:40:00+00:00"}, canonical_verified=True)
        self.store.record_publication(yesterday, {"source_key": "yesterday", "publish_id": "platform-yesterday", "published_at": "2026-11-01T00:00:00+00:00"}, canonical_verified=True)
        self.store.source_health("x", "manual")
        data = views.query(self.store, "summary", self.alice, {})
        self.assertEqual(data["totals"]["prepared"], 1)
        self.assertEqual(data["totals"]["published"], 1)
        publish = next(r for r in data["channels"] if r["channel"] == "x" and r["stage"] == "publish")
        self.assertEqual(publish["avg_duration_seconds"], 600)
        self.assertEqual(publish["throughput_per_hour"], 1)

    def test_unknown_and_backlog_are_visible_independent_of_window(self):
        self.offer("unknown", status="unknown")
        self.offer("old-pause", status="backlog_paused")
        data = views.query(self.store, "summary", self.alice, {"window": "hour"})
        self.assertEqual(data["totals"]["unknown"], 1)
        self.assertEqual(data["totals"]["backlog_paused"], 1)

    def test_scoped_cache_never_reuses_admin_result_for_personal(self):
        self.offer("a")
        self.offer("b", owner="bob")
        self.assertEqual(views.query(self.store, "tasks", self.admin, {})["total"], 2)
        self.assertEqual(views.query(self.store, "tasks", self.alice, {})["total"], 1)
        self.assertEqual(views.query(self.store, "workers", self.alice, {})["items"], [])

    def test_cursor_pages_no_duplicates(self):
        for n in range(3): self.offer(str(n))
        first = views.query(self.store, "tasks", self.alice, {"limit": 2})
        second = views.query(self.store, "tasks", self.alice, {"limit": 2, "cursor": first["next_cursor"]})
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(len(second["items"]), 1)
        self.assertFalse({r["id"] for r in first["items"]} & {r["id"] for r in second["items"]})

    def test_no_data_is_null_not_zero_and_stale_health_visible(self):
        data = views.query(self.store, "summary", self.admin, {})
        self.assertIsNone(data["totals"]["published"])
        self.assertTrue(data["stale"])
        self.store.source_health("x", "manual")
        self.now += dt.timedelta(minutes=3)
        views.invalidate_cache(self.store)
        self.assertEqual(views.query(self.store, "summary", self.alice, {})["sources"][0]["state"], "stale")

    def test_dynamic_third_node_and_offline_worker(self):
        enrollment = self.store.create_enrollment("GPU Third", self.admin)
        self.store.consume_enrollment(enrollment["token"], "third-gpu")
        self.store.register_node("third-gpu", ["prepare"], {"cpu_count": 12, "gpu_count": 1, "cpu_percent": 25})
        self.store.worker_heartbeat("third-gpu", "prepare-1", "prepare", pid=123)
        data = views.query(self.store, "workers", self.admin, {})
        self.assertIn("third-gpu", [row["host_id"] for row in data["items"]])
        self.assertEqual(data["processes"][0]["pid"], 123)
        self.assertEqual(data["processes"][0]["status"], "idle")
        self.now += dt.timedelta(minutes=2)
        views.invalidate_cache(self.store)
        self.assertEqual(views.query(self.store, "workers", self.admin, {})["processes"][0]["status"], "offline")

    def test_current_worker_phase_is_newer_than_last_checkpoint(self):
        self.store.register_node("cpu-main", ["publish:x"], {"cpu_count": 4})
        self.store.worker_heartbeat("cpu-main", "publish-1", "publish", pid=123,
                                    observation={"phase": "create_post"})
        job_id = self.offer("live")
        with self.store.transaction() as c:
            c.execute("UPDATE pipeline_job SET status='running',node_id='cpu-main',worker_id='publish-1',generation=2,started_at=? WHERE id=?", (self.store.now(), job_id))
            c.execute("INSERT INTO pipeline_checkpoint VALUES('old',?,1,'old_generation','',?)", (job_id, self.store.now()))
            c.execute("INSERT INTO pipeline_checkpoint VALUES('uploaded',?,2,'x_media_uploaded','',?)", (job_id, self.store.now()))
        data = views.query(self.store, "workers", self.admin, {})
        self.assertEqual(data["processes"][0]["phase"], "create_post")
        self.store.worker_heartbeat("cpu-main", "publish-1", "publish", pid=123)
        views.invalidate_cache(self.store)
        data = views.query(self.store, "workers", self.admin, {})
        self.assertEqual(data["processes"][0]["phase"], "x_media_uploaded")


if __name__ == "__main__":
    unittest.main()
