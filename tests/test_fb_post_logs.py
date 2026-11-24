import json
import threading
import unittest
from contextlib import closing
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from features.fb_post_logs.manager import Manager
from features.fb_post_logs.server import Handler, admin
from features.pipeline_dispatch.core import DispatchStore
from scripts import test_pipeline_fb_source_locks as fixtures


class LogTests(unittest.TestCase):
    def setUp(self):
        # Use the native publisher/dispatcher migrations and claim paths.
        self.fixture = fixtures.FBSourceLockTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = DispatchStore(
            self.fixture.temp.name + "/dispatch.sqlite", clock=lambda: self.fixture.now
        )
        self.manager = Manager(self.fixture.auto.path, self.store.path)
        self.manager.install()
        self.actor = {"user_id": "admin", "tenant_key": "tenant", "name": "Admin"}
        self.video = self.add(self.fixture.video_job(101))
        self.auto = self.add(self.fixture.auto_job(102, "201"))
        self.n = 0

    def add(self, job):
        self.store.offer(
            dict(
                job,
                resource_keys=["fb:page:" + job["source_id"]],
                capabilities=["publish:fb"],
                source_created_at=self.fixture.now.isoformat(),
                scheduled_at=self.fixture.now.isoformat(),
            )
        )
        return job["source_key"]

    def get(self, key):
        return next(x for x in self.manager.query({})["items"] if x["queue_id"] == key)

    def request(self, action, *items):
        self.n += 1
        return {
            "operation_id": "request-%016d" % self.n,
            "action": action,
            "items": [{"queue_id": x["queue_id"], "version": x["action_version"]} for x in items],
        }

    def test_filters_and_stable_pages(self):
        def total(**kw):
            return self.manager.query(kw)["pagination"]["total"]

        self.assertEqual(total(), 2)
        self.assertEqual(total(task_source="template_manual"), 1)
        self.assertEqual(
            total(
                task_source="manual", page_id="100", material_id="101", status="queued", unknown_outcome="0"
            ),
            1,
        )
        first = self.manager.query({"page_size": "1", "page": "1"})["items"][0]
        second = self.manager.query({"page_size": "1", "page": "2"})["items"][0]
        self.assertNotEqual(first["queue_id"], second["queue_id"])
        with self.fixture.video.connect() as c:
            c.execute("UPDATE fb_video_batch SET request_json=?", ('{"excel":{"filename":"x"}}',))
            c.execute(
                "UPDATE fb_video_task SET planned_at_utc='2026-11-03T16:00:00+00:00',status='unknown',unknown_outcome=1"
            )
        self.assertEqual(total(task_source="excel", run_date="2026-11-04", unknown_outcome="1"), 1)
        self.assertEqual(total(task_source="excel", run_date="2026-11-03"), 0)
        self.assertEqual(total(task_source="manual"), 0)
        for source in ("pool", "group_pool"):
            with self.fixture.video.connect() as c:
                c.execute("UPDATE fb_video_task SET source_kind=?", (source,))
            self.assertEqual(total(task_source=source), 1)
        with self.fixture.auto.connect() as c:
            c.execute("""UPDATE fb_auto_task SET selection_json='{"tag_profile":{}}'""")
        self.assertEqual(total(task_source="profile"), 1)
        with self.assertRaises(ValueError):
            total(material_id="invalid")

    def test_template_filter_is_exact_and_combines_with_source(self):
        with self.fixture.auto.connect() as c:
            c.execute("""INSERT INTO fb_auto_template(id,name,status,created_at_utc,updated_at_utc)
                         SELECT 9,name,'disabled',created_at_utc,updated_at_utc FROM fb_auto_template WHERE id=1""")
        result = self.manager.query({"template_id": "1", "task_source": "template_manual"})
        self.assertEqual([self.auto], [row["queue_id"] for row in result["items"]])
        self.assertEqual({1, 9}, {row["id"] for row in result["templates"]})
        for extra in (
            {"template_id": "9"},
            {"template_id": "1", "task_source": "pool"},
            {"template_id": "1", "task_source": "excel"},
        ):
            self.assertEqual(self.manager.query(extra)["pagination"]["total"], 0)
        with self.assertRaises(ValueError):
            self.manager.query({"template_id": "1 OR 1=1"})

    def test_stop_reset_replay_and_actual_claim(self):
        self.store.channel_enabled("fb", True)
        self.store.register_node("cpu-main", ["publish:fb", "backend:cpu"])
        self.store.worker_heartbeat("cpu-main", "w", "publish")
        with self.store.transaction() as c:
            c.execute("DELETE FROM pipeline_job WHERE source_key=?", (self.auto,))
        native_id = self.video.split(":")[-1]
        old = self.get(self.video)
        stop = self.request("stop", old)
        result = self.manager.apply(stop, self.actor)
        self.assertEqual(result["succeeded"], 1, result)
        self.assertEqual(result, self.manager.apply(stop, self.actor))
        self.assertEqual(self.get(self.video)["status"], "stopped")
        self.assertIsNone(self.fixture.video.claim("execute", "worker", task_id=native_id))
        self.assertIsNone(self.store.claim("cpu-main", "w", "publish")["job"])
        self.assertEqual(self.manager.query({"status": "stopped"})["pagination"]["total"], 1)
        reset = self.request("reset", self.get(self.video))
        result = self.manager.apply(reset, self.actor)
        self.assertEqual(result["succeeded"], 1, result)
        self.assertEqual(result, self.manager.apply(reset, self.actor))
        job = self.store.claim("cpu-main", "w", "publish")["job"]
        self.assertEqual(job["source_key"], self.video)
        self.assertIsNone(self.store.claim("cpu-main", "w", "publish")["job"])
        self.assertIsNotNone(self.fixture.video.claim("execute", "worker", task_id=native_id))
        self.assertIsNone(self.fixture.video.claim("execute", "worker2", task_id=native_id))
        with self.fixture.video.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM fb_log_action").fetchone()[0], 2)
        self.assertEqual(self.get(self.video)["short_url"], old["short_url"])

    def test_partial_bulk_stale_and_terminal(self):
        a, b = self.get(self.auto), self.get(self.video)
        request = self.request("reset", a, b)
        with self.fixture.video.connect() as c:
            c.execute("UPDATE fb_video_task SET status='running'")
        result = self.manager.apply(request, self.actor)
        self.assertEqual((result["succeeded"], result["skipped"]), (1, 1), result)
        self.assertEqual(result, self.manager.apply(request, self.actor))
        with self.fixture.auto.connect() as c:
            c.execute("UPDATE fb_auto_task SET error_code='changed'")
        self.assertEqual(self.manager.apply(self.request("stop", a), self.actor)["skipped"], 1)
        for status in ("published", "submitted", "unknown", "failed_without_retry", "skipped"):
            with self.fixture.auto.connect() as c:
                c.execute("UPDATE fb_auto_task SET status=?", (status,))
            item = self.get(self.auto)
            self.assertFalse(item["can_stop"] or item["can_reset"], status)
        with self.assertRaises(ValueError):
            self.manager.apply(dict(request, items=[request["items"][0]]), self.actor)

    def test_failed_ready_task_reuses_frozen_media_and_definite_receipts(self):
        with self.fixture.video.connect() as c:
            c.execute("UPDATE fb_video_task SET status='failed',error_code='source_busy'")
            c.execute("""INSERT INTO fb_video_attempt(task_id,lease_token,credential_id,fb_user_id,result_kind,
                      error_code,created_at_utc) SELECT task_id,'','','','definite_failure','source_busy',created_at_utc FROM fb_video_task""")
        with self.store.transaction() as c:
            c.execute("UPDATE pipeline_job SET status='failed' WHERE source_key=?", (self.video,))
        item = self.get(self.video)
        self.assertEqual(self.manager.apply(self.request("reset", item), self.actor)["succeeded"], 1)
        with self.fixture.video.connect() as c:
            self.assertEqual(
                tuple(c.execute("SELECT status,caption,prepare_state FROM fb_video_task").fetchone()),
                ("queued", "Frozen caption", "ready"),
            )
            self.assertEqual(
                c.execute("SELECT error_code FROM fb_video_attempt").fetchone()[0], "source_busy"
            )
            self.assertIn("source_busy", c.execute("SELECT before_json FROM fb_log_action").fetchone()[0])
            c.execute("UPDATE fb_video_attempt SET result_kind='unknown'")
        self.assertFalse(self.get(self.video)["can_reset"])

    def test_http_cookie_admin_gate(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.manager = self.manager
        server.authenticate = admin
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for method, path in (("GET", "logs"), ("POST", "queue-actions")):
            for role, expected in (("", 401), ("user", 403), ("admin", 200 if method == "GET" else 400)):
                with patch("features.fb_post_logs.server.HTTPConnection") as backend:
                    response = backend.return_value.getresponse.return_value
                    response.status = 200
                    response.read.return_value = json.dumps(
                        {
                            "authenticated": True,
                            "user": {"role": role, "is_admin": role == "admin", "user_id": "u"},
                        }
                    ).encode()
                    with closing(HTTPConnection(*server.server_address)) as client:
                        client.request(
                            method,
                            "/api/admin/fb-posts/" + path,
                            body="{}",
                            headers={"Cookie": role, "Content-Type": "application/json"},
                        )
                        self.assertEqual(client.getresponse().status, expected)
