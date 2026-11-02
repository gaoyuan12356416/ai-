"""Real canonical SQLite X transitions, using offline auth/network boundaries."""
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.x_native import XNativeBridge
from features.pipeline_dispatch.x_share_native import XShareBridge
from features.pipeline_dispatch.adapters import AdapterError
from features.x_posts import service as posts
from scripts.test_x_post_priority_manual_store import material_candidate, ACTOR


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.accounts = {}
        self.policy = mock.patch.dict(os.environ, {"PIPELINE_X_DISPATCH_ENABLED": "1", "X_POST_ATTRIBUTION_V2_ENABLED": "0", "X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256": "a" * 64})
        self.policy.start()
        self.addCleanup(self.policy.stop)
        self.redirect = mock.patch.object(posts, "write_short_redirect")
        self.redirect.start()
        self.addCleanup(self.redirect.stop)
        @contextlib.contextmanager
        def credentials(identity, actor, scope):
            yield self.verify(identity), "offline-token"
        self.runtime = SimpleNamespace(POST_DB_PATH=self.root / "posts.sqlite", POST_SHORT_BASE_URL=posts.DEFAULT_SHORT_BASE_URL,
            POST_PUBLIC_ROOT=self.root / "public", POST_STORAGE_ROOT=self.root, POST_STORAGE_MOUNT_ROOT=self.root,
            POST_MAX_MEDIA_BYTES=16 * 1024**3, POST_MEDIA_ALLOWED_HOSTS=("media.example.test",), AUTO_TEMPLATE_MAX_DURATION_SECONDS=14400,
            AUTO_TEMPLATE_ACTOR=ACTOR, CANARY_ACTOR=ACTOR, verify_account=self.verify, publish_credentials=credentials,
            same_drama_language=lambda a, b: a == b, _personal_owner_guard=lambda *_: None,
            _personal_run_guard=lambda *_: None, preflight_post_storage_request=lambda *_: None)
        self.bridge = XNativeBridge(self.runtime)
        self.serial = 0

    def verify(self, identity, *_args, **_kwargs):
        return dict(self.accounts.get(identity, {"id": identity, "status": "active", "publish_approved": True,
            "x_user_id": str(identity), "username": "account" + str(identity), "drama_language": "en",
            "long_video_publish_eligible": False, "protected": False}))

    def queue(self, identity=2, auto=False, random=False):
        self.serial += 1
        material = str(500 + self.serial)
        if auto:
            run = self.bridge.store.create_auto_template_run(material, identity, "task-" + material, "template-1", 1,
                "{{drama_name}}\n{{desc}}\n{{url}}", ACTOR, random_template_enabled=random)
            trigger = "auto_template"
        else:
            run = self.bridge.store.create_manual_run([material], [identity], "test-" + material, ACTOR, random_template_enabled=random)
            self.bridge.store.claim_manual_run()
            trigger = "manual"
        item = material_candidate(run, identity, material)
        item.update(media_validation_mode="deferred", preflight_sha256="", preflight_size=0, preflight_duration=0)
        return self.bridge.store.create_manual_plan(run["id"], [item], trigger)["queues"][0]

    def job(self, queue, stage, generation=1, backend="cpu"):
        return {"channel": "x", "family": "queue", "source_id": str(queue["id"]), "source_key": "x:queue:" + str(queue["id"]),
                "stage": stage, "generation": generation, "executor_backend": backend, "claim_token": "c" * 32}

    def preflight(self, queue, *, mime="video/mp4", duration=30):
        job = self.job(queue, "prepare")
        descriptor = self.bridge.reserve(job, job)
        artifact = {"url": "https://media.example.test/final.mp4", "sha256": "b" * 64, "size": 2048,
                    "mime": mime, "duration_seconds": duration, "width": 720, "height": 1280, "profile": "source"}
        receipt = {"source_url": descriptor["request"]["source_url"], "artifact": artifact}
        self.bridge.checkpoint(job, job, "x_preflight", receipt)
        return job, descriptor, artifact

    def operation(self, job, method, path, body=b"", name="operation"):
        return {"operation_id": name, "operation": "x:" + method + ":" + path.split("?")[0],
                "request_sha256": "a" * 64, "account_resource": "x:account:2",
                "details": {"method": method, "path": path, "body_sha256": hashlib.sha256(body).hexdigest()}}

    def publish(self, queue, late=False):
        job = self.job(queue, "publish")
        descriptor = self.bridge.reserve(job, job)
        initial = json.dumps({"media_type": descriptor["artifact"]["mime"], "total_bytes": descriptor["artifact"]["size"], "media_category": descriptor["request"]["media_category"]}, separators=(",", ":")).encode()
        op = self.operation(job, "POST", "/2/media/upload/initialize", initial, "initialize-" + job["source_id"])
        self.bridge.begin_operation(job, job, op)
        self.bridge.checkpoint(job, job, "x_media_initialized", {"media_id": "media123", "operation_id": op["operation_id"]})
        self.bridge.checkpoint(job, job, "x_media_uploaded", {"media_id": "media123", "artifact_sha256": descriptor["artifact"]["sha256"]})
        body = json.dumps({"text": descriptor["request"]["body"], "media": {"media_ids": ["media123"]}}, ensure_ascii=False, separators=(",", ":")).encode()
        self.bridge.begin_operation(job, job, self.operation(job, "POST", "/2/tweets", body, "post-" + job["source_id"]))
        if late:
            self.assertFalse(self.bridge.fail(job, job, {"unknown_outcome": True})["canonical_no_write"])
        self.bridge.checkpoint(job, job, "x_source_published", {"media_id": "media123", "post_id": "123456"})
        result = self.bridge.complete(job, {"post_id": "123456"}, job)
        self.assertEqual(result["status"], "published")
        return job, descriptor

    def test_real_auto_queue_preflight_to_publication_no_second_attempt(self):
        queue = self.queue(auto=True)
        prep, _, artifact = self.preflight(queue)
        self.assertEqual(self.bridge.complete(prep, {"artifact": artifact}, prep)["status"], "ready")
        job, descriptor = self.publish(queue)
        with self.assertRaises(Exception):
            self.bridge.begin_operation(job, job, self.operation(job, "POST", "/2/media/upload/initialize", name="retry"))
        log = self.bridge.store.get_log_by_queue_id(queue["id"]) if hasattr(self.bridge.store, "get_log_by_queue_id") else None
        with self.bridge.connect() as c:
            row = c.execute("SELECT status,attempt_count FROM x_post_publish_log WHERE queue_id=?", (queue["id"],)).fetchone()
        self.assertEqual(tuple(row), ("published", 1))

    def test_native_account_fence_blocks_same_account_during_upload(self):
        first, second = self.queue(), self.queue()
        for queue in (first, second):
            prep, _, artifact = self.preflight(queue)
            self.bridge.complete(prep, {"artifact": artifact}, prep)
        a, b = self.job(first, "publish"), self.job(second, "publish")
        desc = self.bridge.reserve(a, a)
        body = json.dumps({"media_type": "video/mp4", "total_bytes": 2048, "media_category": "tweet_video"}, separators=(",", ":")).encode()
        self.bridge.begin_operation(a, a, self.operation(a, "POST", "/2/media/upload/initialize", body, "first-upload"))
        self.bridge.reserve(b, b)
        with self.assertRaises(Exception):
            self.bridge.begin_operation(b, b, self.operation(b, "POST", "/2/media/upload/initialize", body, "second-upload"))

    def test_image_preflight_and_native_category(self):
        queue = self.queue()
        prep, _, artifact = self.preflight(queue, mime="image/png", duration=0)
        self.bridge.complete(prep, {"artifact": artifact}, prep)
        job = self.job(queue, "publish")
        self.assertEqual(self.bridge.reserve(job, job)["request"]["media_category"], "tweet_image")

    def test_account_approval_and_duration_cannot_be_bypassed(self):
        queue = self.queue()
        self.accounts[2] = {**self.verify(2), "publish_approved": False}
        with self.assertRaises(AdapterError): self.bridge.reserve(self.job(queue, "prepare"), self.job(queue, "prepare"))
        self.accounts.clear()
        with self.assertRaises(AdapterError): self.preflight(queue, duration=1201)

    def test_direct_long_video_keeps_original_public_account_requirement(self):
        queue = self.queue()
        self.accounts[2] = {**self.verify(2), "long_video_publish_eligible": True, "protected": True}
        prep, _, artifact = self.preflight(queue, duration=1300)
        self.bridge.complete(prep, {"artifact": artifact}, prep)
        with self.assertRaises(AdapterError): self.bridge.reserve(self.job(queue, "publish"), self.job(queue, "publish"))

    def test_manual_sibling_post_creating_does_not_block_other_account(self):
        run = self.bridge.store.create_manual_run(["601", "602"], [2, 3], "two-accounts", ACTOR)
        self.bridge.store.claim_manual_run()
        candidates = []
        for identity, material in ((2, "601"), (3, "602")):
            candidate = material_candidate(run, identity, material)
            candidate.update(media_validation_mode="deferred", preflight_sha256="", preflight_size=0, preflight_duration=0)
            candidates.append(candidate)
        queues = self.bridge.store.create_manual_plan(run["id"], candidates, "manual")["queues"]
        for queue in queues:
            prep, _, artifact = self.preflight(queue)
            self.bridge.complete(prep, {"artifact": artifact}, prep)
        for queue in queues:
            job = self.job(queue, "publish")
            descriptor = self.bridge.reserve(job, job)
            initial = json.dumps({"media_type": "video/mp4", "total_bytes": 2048, "media_category": "tweet_video"}, separators=(",", ":")).encode()
            op = self.operation(job, "POST", "/2/media/upload/initialize", initial, "sibling-" + job["source_id"])
            op["account_resource"] = "x:account:" + str(queue["account_id"])
            self.bridge.begin_operation(job, job, op)
            self.bridge.checkpoint(job, job, "x_media_initialized", {"media_id": "media" + job["source_id"], "operation_id": op["operation_id"]})
            self.bridge.checkpoint(job, job, "x_media_uploaded", {"media_id": "media" + job["source_id"], "artifact_sha256": descriptor["artifact"]["sha256"]})
        with self.bridge.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM x_post_publish_log WHERE status='post_creating'").fetchone()[0], 2)

    def test_repaired_source_checkpoint_survives_generation_and_artifact_mime_is_frozen(self):
        queue = self.queue()
        job = self.job(queue, "prepare")
        descriptor = self.bridge.reserve(job, job)
        artifact = {"url": "https://media.example.test/repaired.mp4", "sha256": "c" * 64, "size": 2048,
            "mime": "video/mp4", "duration_seconds": 30, "profile": descriptor["request"]["repair_request"]["profile"]}
        repair = {**descriptor["request"]["repair_request"], "output_url": artifact["url"], "output_sha256": artifact["sha256"],
            "output_size": artifact["size"], "probe": {"duration": 30}}
        receipt = {"source_url": descriptor["request"]["source_url"], "source_sha256": "b" * 64, "source_size": 4096,
            "artifact": artifact, "repair": repair}
        self.bridge.checkpoint(job, job, "x_preflight", receipt)
        with self.assertRaises(AdapterError): self.bridge.complete(job, {"artifact": {**artifact, "mime": "image/png"}}, job)
        retry = self.job(queue, "prepare", generation=2)
        self.assertEqual(self.bridge.reserve(retry, retry)["request"]["prepared_artifact"], artifact)
        self.assertEqual(self.bridge.complete(retry, {"artifact": artifact}, retry)["status"], "ready")

    def test_frozen_random_request_valid_on_cpu_and_cannot_move_to_gpu(self):
        from features.x_posts.random_template_gpu import validate_request, CPU_PROFILE
        from features.x_posts.random_template_store import PreparationStore
        queue = self.queue(random=True)
        job, descriptor, artifact = self.preflight(queue)
        request = PreparationStore(self.runtime.POST_DB_PATH).get(queue["id"])["request"]
        config = SimpleNamespace(profile=CPU_PROFILE, max_source_bytes=16 * 1024**3, allowed_hosts=("media.example.test",))
        validate_request(request, config)
        retry = self.job(queue, "prepare", generation=2, backend="gpu")
        self.assertEqual(self.bridge.reserve(retry, retry)["request"]["profile"], CPU_PROFILE)
        with self.assertRaises(AdapterError): self.bridge.complete(job, {"artifact": artifact}, job)

    def test_random_complete_binds_full_portable_artifact(self):
        from features.x_posts.random_template_store import PreparationStore
        queue = self.queue(random=True)
        job, _, _ = self.preflight(queue)
        request = PreparationStore(self.runtime.POST_DB_PATH).get(queue["id"])["request"]
        result = {**request, "output_url": "https://media.example.test/rendered.mp4", "output_sha256": "d" * 64,
            "output_size": 3333, "probe": {"duration": 30}, "recipe": {"asset_set_sha256": request["asset_manifest_sha256"]}}
        artifact = {"url": result["output_url"], "sha256": result["output_sha256"], "size": result["output_size"], "mime": "video/mp4",
            "duration_seconds": 30, "profile": result["profile"], "recipe": result["recipe"]}
        with self.assertRaises(AdapterError): self.bridge.complete(job, {"artifact": {**artifact, "url": "https://media.example.test/wrong.mp4"}, "preparation": result}, job)
        self.assertEqual(self.bridge.complete(job, {"artifact": artifact, "preparation": result}, job)["status"], "ready")
        self.assertEqual(self.bridge.reserve(self.job(queue, "publish"), self.job(queue, "publish"))["artifact"], artifact)

    def test_late_post_receipt_resolves_exact_quarantined_attempt(self):
        queue = self.queue(auto=True)
        prep, _, artifact = self.preflight(queue)
        self.bridge.complete(prep, {"artifact": artifact}, prep)
        self.publish(queue, late=True)

    def test_pause_keeps_queue_and_material_reservation_but_blocks_claim(self):
        queue = self.queue()
        job = self.job(queue, "prepare")
        self.assertTrue(self.bridge.pause(job, "2026-11-02T01:00:00Z")["held"])
        self.assertEqual(self.bridge.store.get_queue(queue["id"])["status"], "queued")
        with self.assertRaises(AdapterError): self.bridge.reserve(job, job)

    def waiting_relay_queue(self):
        from scripts.test_x_post_multi_schedule_store import XPostMultiScheduleStoreTests
        fixture = XPostMultiScheduleStoreTests()
        fixture.store, fixture.db_path = self.bridge.store, self.runtime.POST_DB_PATH
        fixture.save_schedule("drama", [2], ["09:00"])
        pool = fixture.add_drama(content_id="WAIT-RELAY")
        candidate = fixture.duration_pending_drama_candidate(pool, 2)
        fixture.claim_schedule_slot("2026-07-27", "09:00")
        plan = fixture.store.create_schedule_plan("drama", "2026-07-27", "09:00", 2, [candidate])
        queue_id = plan["queues"][0]["id"]
        fixture.store.resolve_drama_duration_route(queue_id,
            fixture.duration_media_evidence(candidate, 1201.0, 77), False, [])
        return fixture.store.get_queue(queue_id)

    def test_waiting_relay_pause_retains_native_route_and_material_assignment(self):
        queue = self.waiting_relay_queue()
        job = self.job(queue, "publish")
        job.pop("generation")
        with self.bridge.connect() as conn:
            before = [tuple(r) for r in conn.execute("SELECT * FROM x_post_drama_pool")]
            route = [tuple(r) for r in conn.execute("SELECT * FROM x_post_drama_delivery_route")]
        for _ in range(2):
            self.assertTrue(self.bridge.pause(job, "2026-11-02T11:17:02Z")["held"])
        self.assertEqual(self.bridge.store.get_queue(queue["id"]), queue)
        with self.bridge.connect() as conn:
            self.assertEqual([tuple(r) for r in conn.execute("SELECT * FROM x_post_drama_pool")], before)
            self.assertEqual([tuple(r) for r in conn.execute("SELECT * FROM x_post_drama_delivery_route")], route)
            for table in ("x_post_publish_log", "x_post_repost_ledger", "pipeline_x_claim", "pipeline_x_operation", "pipeline_x_checkpoint"):
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_canonical_hold").fetchone()[0], 1)

    def test_waiting_relay_pause_rejects_log_repost_claim_operation_and_missing_route(self):
        queue = self.waiting_relay_queue()
        job = self.job(queue, "publish")
        # Inject inconsistent historical evidence in this isolated SQLite fixture;
        # production route triggers also reject these combinations.
        with self.bridge.connect() as conn:
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                conn.execute('DROP TRIGGER "' + row[0].replace('"', '""') + '"')
        cases = (
            ("INSERT INTO x_post_publish_log(queue_id,account_id,status,attempt_count,created_at,updated_at) VALUES(?,2,'reserved',0,'now','now')", (queue["id"],)),
            ("INSERT INTO x_post_repost_ledger(queue_id,run_date,target_account_id,relay_account_id,status,source_attempt_count,source_post_id,created_at,updated_at) VALUES(?,'2026-07-27',2,3,'source_published',1,'123456','now','now')", (queue["id"],)),
            ("INSERT INTO pipeline_x_claim VALUES(?,'prepare',1,'hash',?,0,'{}')", (job["source_key"], queue["id"])),
            ("INSERT INTO pipeline_x_operation VALUES('op',?,1,'post','hash','now')", (job["source_key"],)),
            ("INSERT INTO pipeline_x_checkpoint VALUES(?,1,'x_source_published','{}')", (job["source_key"],)),
            ("DELETE FROM x_post_drama_delivery_route WHERE queue_id=?", (queue["id"],)),
        )
        for sql, params in cases:
            with self.subTest(sql=sql), self.bridge.connect() as conn:
                conn.execute(sql, params)
                conn.commit()
                with self.assertRaisesRegex(AdapterError, "canonical_task_not_untouched"):
                    self.bridge.pause(job, "2026-11-02T11:17:02Z")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_canonical_hold").fetchone()[0], 0)
                for table in ("x_post_publish_log", "x_post_repost_ledger", "pipeline_x_claim", "pipeline_x_operation", "pipeline_x_checkpoint"):
                    conn.execute("DELETE FROM " + table)

    def test_cutover_projection_without_generation_reaches_http_pause_and_persists_hold(self):
        from features.pipeline_dispatch import x_native
        from features.pipeline_dispatch.adapters import AdapterRegistry, LedgerAdapter, LedgerSpec
        from features.pipeline_dispatch.core import DispatchStore
        from features.pipeline_dispatch.cutover import Cutover
        from features.pipeline_dispatch.service import Coordinator

        queue = self.queue()
        before = self.bridge.store.get_queue(queue["id"])
        received = []
        token = "t" * 32
        runtime = self.runtime

        class HTTPPauseDriver:
            def pause(self, job, cutoff):
                received.append(dict(job))
                replies = []
                handler = SimpleNamespace(headers={"Authorization": "Bearer " + token},
                    read_json=lambda _limit: {"job": job, "cutoff": cutoff},
                    send_json=lambda status, body: replies.append((status, body)))
                x_native.handle(handler, runtime, "/internal/pipeline-dispatch/x/pause")
                status, body = replies[0]
                if status != 200:
                    raise AdapterError(body["code"])
                return body

        adapter = LedgerAdapter(LedgerSpec("x", "queue", str(self.runtime.POST_DB_PATH), "x_post_queue", module="x_accounts"), HTTPPauseDriver())
        central = DispatchStore(self.root / "cutover.sqlite")
        control = Cutover(Coordinator(central, AdapterRegistry([adapter]), internal_token="i" * 32, master_key="m" * 32))
        plan = control.plan("x", "2099-01-01T00:00:00Z")
        self.assertEqual(plan["paused_source_keys"], ["x:queue:" + str(queue["id"])])
        with mock.patch.object(x_native, "_BRIDGE", self.bridge), mock.patch.dict(os.environ, {
                "PIPELINE_X_CANONICAL_BRIDGE_ENABLED": "1", "PIPELINE_X_CANONICAL_TOKEN": token}):
            for _ in range(2):
                held = control.apply(plan["plan_id"], plan["sha256"])
                self.assertEqual((held["status"], held["paused_count"], held["errors"]), ("held", 1, []))
        self.assertNotIn("generation", received[0])
        self.assertNotIn("claim_token", received[0])
        self.assertEqual(self.bridge.store.get_queue(queue["id"]), before)
        with self.bridge.connect() as conn:
            hold = conn.execute("SELECT source_key,cutoff,reason FROM pipeline_canonical_hold").fetchall()
            self.assertEqual([tuple(row) for row in hold], [(plan["paused_source_keys"][0], plan["cutoff"], "cutover_overdue")])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_x_claim").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_x_operation").fetchone()[0], 0)
        self.assertEqual(self.bridge.reconcile(received[0])["status"], "queued")
        with self.assertRaises(AdapterError):
            self.bridge.pause({**received[0], "source_key": "x:queue:wrong"}, plan["cutoff"])
        with self.assertRaises(KeyError):
            self.bridge.reserve(received[0], {})

    def test_coordinator_expired_lease_accepts_late_native_receipt_without_reclaim(self):
        from features.pipeline_dispatch.core import DispatchStore
        queue = self.queue(auto=True)
        prep, _, artifact = self.preflight(queue)
        self.bridge.complete(prep, {"artifact": artifact}, prep)
        now = [dt.datetime(2026, 11, 2, 9, tzinfo=dt.timezone.utc)]
        store = DispatchStore(self.root / "dispatch.sqlite", clock=lambda: now[0])
        store.register_node("cpu-main", ["publish:x"])
        store.worker_heartbeat("cpu-main", "p1", "publish")
        store.channel_enabled("x", True)
        offer = {"source_key": "x:queue:" + str(queue["id"]), "source_id": str(queue["id"]),
                 "channel": "x", "family": "queue", "stage": "publish", "resource_keys": ["x:account:2"], "capabilities": ["publish:x"]}
        store.offer(offer)
        job = store.claim("cpu-main", "p1", "publish")["job"]
        descriptor = self.bridge.reserve(job, job)
        init_body = json.dumps({"media_type": "video/mp4", "total_bytes": 2048, "media_category": "tweet_video"}, separators=(",", ":")).encode()
        init = self.operation(job, "POST", "/2/media/upload/initialize", init_body, "lease-init")
        store.begin_operation(job, init)
        self.bridge.begin_operation(job, job, init)
        self.bridge.checkpoint(job, job, "x_media_initialized", {"media_id": "media123", "operation_id": "lease-init"})
        self.bridge.checkpoint(job, job, "x_media_uploaded", {"media_id": "media123", "artifact_sha256": artifact["sha256"]})
        body = json.dumps({"text": descriptor["request"]["body"], "media": {"media_ids": ["media123"]}}, ensure_ascii=False, separators=(",", ":")).encode()
        post = self.operation(job, "POST", "/2/tweets", body, "lease-post")
        store.begin_operation(job, post)
        self.bridge.begin_operation(job, job, post)
        now[0] += dt.timedelta(seconds=121)
        store.expire()
        self.assertEqual(store.get_job(job["id"])["status"], "unknown")
        store.worker_heartbeat("cpu-main", "p1", "publish")
        store.register_node("cpu-main", ["publish:x"])
        self.assertIsNone(store.claim("cpu-main", "p1", "publish")["job"])
        receipt = {"media_id": "media123", "post_id": "999999"}
        store.validate_receipt_claim(job)
        self.bridge.checkpoint(job, job, "x_source_published", receipt)
        store.checkpoint(job, "late", "x_source_published", receipt, canonical_verified=True)
        native = self.bridge.complete(job, receipt, job)
        store.complete(job, "receipt", {"canonical_receipt": native["post_id"]}, canonical_verified=True)
        self.assertEqual(store.get_job(job["id"])["status"], "published")
        self.assertTrue(store.complete(job, "receipt", {"canonical_receipt": native["post_id"]}, canonical_verified=True)["duplicate"])


class ShareNativeTests(unittest.TestCase):
    def setUp(self):
        from features.x_accounts.youtube_shares import YouTubeShares
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1793581200.0
        self.actor = {"tenant_key": "tenant", "user_id": "alice", "role": "user"}
        def account(identity, *_args, **_kwargs):
            return {"id": identity, "username": "account" + str(identity), "status": "active", "publish_approved": True}
        @contextlib.contextmanager
        def credentials(*_args):
            yield account(2), "offline-share-token"
        self.service = YouTubeShares(Path(self.temp.name) / "share.sqlite", normalize_scope=lambda a, s: (a, s),
            get_account=account, verify_account=account, publish_credentials=credentials, clock=lambda: self.now)
        runtime = SimpleNamespace(youtube_shares_service=lambda: self.service, verify_account=account, publish_credentials=credentials)
        self.bridge = XShareBridge(runtime)
        self.payload = {"actor": self.actor, "scope": "mine", "task_id": "task1", "video_id": "abcdefghijk",
            "youtube_url": "https://www.youtube.com/watch?v=abcdefghijk", "text": "Watch https://www.youtube.com/watch?v=abcdefghijk",
            "title": "Video", "description_template": "{{url}}", "account_ids": [2, 3], "operation_id": str(uuid.uuid4()), "source_verified_at": self.now}
        self.service.create(self.payload)

    def job(self, identity=2):
        return {"channel": "x", "family": "youtube_share", "stage": "publish", "source_key": "x:youtube_share:" + str(identity),
                "source_id": str(identity), "generation": 1, "claim_token": "s" * 32}

    def test_exact_item_receipt_and_late_unknown_resolution(self):
        job = self.job(2)
        descriptor = self.bridge.reserve(job, job)
        self.assertEqual(self.bridge.item(1)["status"], "queued")
        self.assertEqual(descriptor["account_resource"], "x:account:3")
        body = json.dumps({"text": descriptor["request"]["text"]}, ensure_ascii=False, separators=(",", ":")).encode()
        operation = {"account_resource": "x:account:3", "details": {"method": "POST", "path": "/2/tweets", "body_sha256": hashlib.sha256(body).hexdigest()}}
        self.bridge.begin(job, job, operation)
        with self.assertRaises(AdapterError): self.bridge.begin(job, job, operation)
        self.bridge.dispatch("fail", {"job": job, "claim": job})
        self.assertEqual(self.bridge.item(2)["status"], "unknown_outcome")
        self.bridge.checkpoint(job, job, "x_share_published", {"post_id": "12345"})
        self.assertEqual(self.bridge.dispatch("complete", {"job": job, "claim": job})["status"], "published")
        self.bridge.checkpoint(job, job, "x_share_published", {"post_id": "12345"})
        with self.assertRaises(AdapterError): self.bridge.checkpoint(job, job, "x_share_published", {"post_id": "99999"})
        self.assertEqual(self.bridge.item(1)["status"], "queued")

    def test_share_identity_and_missing_attempt_are_rejected(self):
        job = self.job(1)
        self.bridge.reserve(job, job)
        with self.assertRaises(AdapterError): self.bridge.owned(dict(job, source_id="2"), job)
        with self.assertRaises(AdapterError): self.bridge.checkpoint(job, job, "x_share_published", {"post_id": "123"})

    def test_share_pause_retains_native_duplicate_fence(self):
        job = self.job(1)
        self.assertTrue(self.bridge.pause(job, "2026-11-02T01:00:00Z")["held"])
        with self.assertRaises(AdapterError): self.bridge.reserve(job, job)
        result = self.service.create({**self.payload, "operation_id": str(uuid.uuid4())})
        self.assertTrue(result["run"]["items"][0]["duplicate"])


if __name__ == "__main__":
    unittest.main()
