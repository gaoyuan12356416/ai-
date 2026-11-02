"""Real canonical ledger transitions, fake credential/platform network only."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from features.drama_synthesis.core import DramaSynthesisStore
from features.pipeline_dispatch.youtube_native import YouTubeNativeBridge
from features.pipeline_dispatch.adapters import AdapterError
from features.youtube_auto_publish.service import YouTubeWorkflow


class YouTubeNativeTests(unittest.TestCase):
    def setUp(self):
        gates = patch.dict(os.environ, {"YOUTUBE_LIVE_ENABLED": "1", "YOUTUBE_AUTO_ENABLED": "1"})
        gates.start()
        self.addCleanup(gates.stop)
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = DramaSynthesisStore(root / "jobs.db")
        self.store.ensure_storage()
        cover = root / "cover.jpg"
        stream = io.BytesIO(); Image.new("RGB", (640, 360)).save(stream, format="JPEG")
        cover.write_bytes(stream.getvalue())
        self.cover_sha = hashlib.sha256(cover.read_bytes()).hexdigest()
        schedule = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z") if "scheduled" in self._testMethodName else ""
        legacy = "legacy" in self._testMethodName and "never_reclaimed" not in self._testMethodName
        enqueue = self.store.enqueue_youtube if legacy else self.store.enqueue_reviewed_youtube
        extra = {} if legacy else dict(preparation_id="prep1", source_material_id="material1", approved_cover_path=str(cover), approved_cover_sha256=self.cover_sha, publish_at=schedule)
        self.task = enqueue(**extra,
            operation_id="test-reviewed-1", job_id="a" * 32, content_id="content1", app_id="1479", channel_local_id="1", channel_id="UC" + "A" * 22, youtube_account_id="1", source_kind="concat_video" if legacy else "custom_source", source_url="https://media.example/video.mp4", title="title", description_template="description", description_rendered="description", comment_text="hello", duplicate_confirmed=False, scopes=("https://www.googleapis.com/auth/youtube.force-ssl",), operator_user_id="user1")
        @contextmanager
        def db(write=False):
            conn = self.store._connect()
            try:
                if write: conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback(); raise
            finally:
                conn.close()
        credential = SimpleNamespace(scopes={"https://www.googleapis.com/auth/youtube.force-ssl"}, capabilities={"comment_eligible": True, "upload_eligible": True})
        repo = SimpleNamespace(credential=lambda **_: credential)
        workflow = YouTubeWorkflow(self.store.db_path, root, None, None, None, self.store, channel_directory=SimpleNamespace(repository=repo))
        self.workflow = workflow
        self.bridge = YouTubeNativeBridge(workflow, client=SimpleNamespace(refresh_access_token=lambda _: "short-token", verify_channel_identity=lambda *_: None))
        self.job = {"id": 1, "generation": 1, "source_key": "youtube:publish:" + str(self.task["id"]), "source_id": str(self.task["id"]), "channel": "youtube", "family": "publish", "stage": "publish"}
        self.claim = {"claim_token": "claim-token-that-is-long"}
        self.descriptor = self.bridge.reserve(self.job, self.claim)
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def operation(self, name, details):
        self.counter += 1
        return self.bridge.begin_operation(self.job, self.claim, {"operation_id": "operation-%03d" % self.counter, "operation": name, "account_resource": self.descriptor["account_resource"], "request_sha256": hashlib.sha256(json.dumps(details).encode()).hexdigest(), "details": details})

    def checkpoint(self, phase, receipt):
        return self.bridge.checkpoint(self.job, self.claim, phase, receipt)

    def prepare_upload(self):
        self.checkpoint("youtube_source", {"url": self.task["source_url"], "sha256": "b" * 64, "size": 1024, "duration_ms": 1000})
        self.operation("youtube_begin_resumable", {"title": "title", "description": "description", "size": 1024, "privacy_status": "private"})

    def test_crash_after_intent_is_never_reclaimed_by_legacy(self):
        self.prepare_upload()
        self.assertIsNone(self.store.claim_reviewed_youtube("legacy", "2099-01-01T00:00:00Z"))
        result = self.bridge.fail(self.job, self.claim, {"error_code": "worker_crashed", "unknown_outcome": True})
        self.assertFalse(result["canonical_no_write"])
        task = self.store.youtube_task(self.task["id"])
        self.assertEqual(task["status"], "unknown")
        self.assertEqual(task["video_attempt_count"], 1)
        with self.assertRaises(AdapterError):
            self.bridge.reserve({**self.job, "generation": 2}, self.claim)

    def test_disabled_worker_switches_block_new_claims_without_changing_native_task(self):
        before = self.store.youtube_task(self.task['id'])
        for environment, code in (({'YOUTUBE_LIVE_ENABLED': '0'}, 'youtube_publish_disabled'),
                                  ({'YOUTUBE_AUTO_ENABLED': '0'}, 'youtube_auto_disabled')):
            with self.subTest(environment=environment), patch.dict(os.environ, environment):
                with self.assertRaisesRegex(AdapterError, code):
                    self.bridge.reserve(dict(self.job, generation=2), self.claim)
                self.assertEqual(self.store.youtube_task(self.task['id']), before)
                # Repeated descriptor delivery for the existing in-flight
                # claim does not interrupt its upload/publication sequence.
                self.assertEqual(self.bridge.reserve(self.job, self.claim), self.descriptor)

    def test_legacy_publication_inherits_live_switch_without_auto_switch(self):
        with patch.dict(os.environ, {'YOUTUBE_AUTO_ENABLED': '0'}):
            task = self.store.enqueue_youtube(operation_id='legacy-gates-new', job_id='c' * 32,
                content_id='content-new', app_id='1479', channel_local_id='1', channel_id='UC' + 'A' * 22,
                youtube_account_id='1', source_kind='concat_video', source_url='https://media.example/new.mp4',
                title='new', description_template='new', description_rendered='new', comment_text='hello',
                duplicate_confirmed=False, scopes=('https://www.googleapis.com/auth/youtube.force-ssl',), operator_user_id='user1')
            job = dict(self.job, source_id=str(task['id']), source_key='youtube:publish:' + str(task['id']))
            self.assertEqual(self.bridge.reserve(job, self.claim)['mode'], 'legacy')

    def test_full_receipts_commit_same_video_and_comment_once(self):
        self.prepare_upload()
        session = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=one"
        self.checkpoint("youtube_session", {"session_uri": session, "size": 1024})
        self.operation("youtube_upload", {"session_uri_sha256": hashlib.sha256(session.encode()).hexdigest(), "source_sha256": "b" * 64, "offset": 0})
        uploaded = {"state": "submitted", "video_id": "video_123", "next_byte": 1024}
        self.checkpoint("youtube_upload", uploaded)
        self.checkpoint("youtube_upload", uploaded)
        self.assertIsNone(self.store.claim_reviewed_youtube("legacy", "2099-01-01T00:00:00Z"))
        self.operation("youtube_thumbnail", {"video_id": "video_123", "sha256": self.cover_sha})
        self.checkpoint("youtube_thumbnail", {"video_id": "video_123", "sha256": self.cover_sha, "status": "set"})
        self.checkpoint("youtube_processing", {"video_id": "video_123", "state": "succeeded", "visibility": "private"})
        self.operation("youtube_visibility", {"video_id": "video_123", "publish_at": "", "preserved_status": {"privacyStatus": "private"}})
        self.checkpoint("youtube_visibility", {"video_id": "video_123", "publish_at": "", "confirmed": {"state": "succeeded", "visibility": "public"}})
        self.operation("youtube_comment", {"video_id": "video_123", "channel_id": self.task["channel_id"], "text_sha256": hashlib.sha256(b"hello").hexdigest()})
        self.checkpoint("youtube_comment", {"video_id": "video_123", "comment_id": "comment-1"})
        result = {"status": "published", "video_id": "video_123", "comment_id": "comment-1"}
        self.bridge.complete(self.job, result, self.claim)
        self.bridge.complete(self.job, result, self.claim)
        task = self.store.youtube_task(self.task["id"])
        self.assertEqual((task["video_attempt_count"], task["comment_attempt_count"]), (1, 1))
        self.assertEqual(task["status"], "published")
        self.assertEqual(task["pipeline_dispatch_key"], "")
        late = self.bridge.fail(self.job, self.claim, {"error_code": "receipt_ack_lost", "unknown_outcome": True})
        self.assertTrue(late["canonical_terminal"])
        self.assertEqual(self.store.youtube_task(task["id"])["status"], "published")

    def test_scheduled_continuation_stays_in_pool_and_never_reuploads(self):
        self.prepare_upload()
        self.checkpoint("youtube_session", {"session_uri": "https://www.googleapis.com/upload?x=1", "size": 1024})
        self.checkpoint("youtube_upload", {"state": "submitted", "video_id": "video_123"})
        self.operation("youtube_thumbnail", {"video_id": "video_123", "sha256": self.cover_sha})
        self.checkpoint("youtube_thumbnail", {"video_id": "video_123", "sha256": self.cover_sha, "status": "set"})
        self.checkpoint("youtube_processing", {"video_id": "video_123", "state": "succeeded", "visibility": "private"})
        self.operation("youtube_visibility", {"video_id": "video_123", "publish_at": self.task["publish_at"], "preserved_status": {"privacyStatus": "private"}})
        self.checkpoint("youtube_visibility", {"video_id": "video_123", "publish_at": self.task["publish_at"], "confirmed": {"state": "succeeded", "visibility": "private", "preserved_status": {"publishAt": self.task["publish_at"]}}})
        verified = self.bridge.complete(self.job, {"status": "scheduled", "video_id": "video_123"}, self.claim)
        task = self.store.youtube_task(self.task["id"])
        self.assertEqual((task["status"], task["schedule_status"], task["pipeline_dispatch_key"]), ("scheduled", "armed", self.job["source_key"]))
        self.assertEqual(task["video_attempt_count"], 1)
        self.assertIsNone(self.store.claim_reviewed_youtube("legacy", "2099-01-01T00:00:00Z"))
        self.job.update(generation=2, payload={"continuation": verified["continuation"]})
        self.claim = {"claim_token": "second-generation-token"}
        self.descriptor = self.bridge.reserve(self.job, self.claim)
        self.assertEqual(self.descriptor["mode"], "continuation")
        self.assertEqual(self.descriptor["continuation"], verified["continuation"])
        for name in ("youtube_begin_resumable", "youtube_upload", "youtube_thumbnail", "youtube_visibility"):
            with self.assertRaises(AdapterError):
                self.operation(name, {})
        self.operation("youtube_video_state", {"video_id": "video_123", "channel_id": task["channel_id"]})
        with patch("features.pipeline_dispatch.youtube_native.utc_now", return_value="2099-01-01T00:00:00Z"):
            self.checkpoint("youtube_schedule_readback", {"video_id": "video_123", "confirmed": {"state": "succeeded", "visibility": "public"}})
        details = {"video_id": "video_123", "channel_id": task["channel_id"], "text_sha256": hashlib.sha256(b"hello").hexdigest()}
        self.operation("youtube_comment", details)
        with self.assertRaises(AdapterError):
            self.operation("youtube_comment", details)
        self.checkpoint("youtube_comment", {"video_id": "video_123", "comment_id": "comment-1"})
        self.bridge.complete(self.job, {"status": "published", "video_id": "video_123", "comment_id": "comment-1"}, self.claim)
        task = self.store.youtube_task(task["id"])
        self.assertEqual((task["video_attempt_count"], task["comment_attempt_count"], task["video_state"]), (1, 1, "published"))

    def test_paused_review_preserves_approval_and_skips_due_housekeeping(self):
        identity = "c" * 32
        body = {"id": identity, "status": "review", "current_version": 1, "publish_at": "2020-01-01T00:00:00Z", "versions": [{"approved": True}]}
        with self.workflow.db(True) as conn:
            conn.execute("INSERT INTO youtube_auto_preparation(id,tenant,owner,operation_id,request_sha,state,version,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (identity, "tenant", "user", "operation-held", "sha", "review", 1, json.dumps(body), "2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"))
        held = {"channel": "youtube", "family": "preparation", "source_id": identity, "source_key": "youtube:preparation:" + identity}
        result = self.bridge.pause(held, "2026-11-02T00:00:00Z")
        self.assertTrue(result["held"])
        self.assertEqual(result, self.bridge.pause(held, "2026-11-02T00:00:00Z"))
        with self.assertRaisesRegex(AdapterError, "canonical_hold_cutoff_conflict"):
            self.bridge.pause(held, "2026-11-03T00:00:00Z")
        self.assertIsNone(self.workflow._claim())
        with self.workflow.db() as conn:
            row = conn.execute("SELECT state,body FROM youtube_auto_preparation WHERE id=?", (identity,)).fetchone()
        self.assertEqual(row["state"], "review")
        self.assertTrue(json.loads(row["body"])["versions"][0]["approved"])

    def test_cutover_apply_and_enable_accept_real_youtube_pause_receipt(self):
        from features.pipeline_dispatch.adapters import AdapterRegistry, LedgerAdapter, LedgerSpec
        from features.pipeline_dispatch.core import DispatchStore
        from features.pipeline_dispatch.cutover import Cutover
        from features.pipeline_dispatch.service import Coordinator
        identity = "d" * 32
        body = {"id": identity, "status": "review", "current_version": 1, "publish_at": "2020-01-01T00:00:00Z", "versions": [{"approved": True}]}
        with self.workflow.db(True) as conn:
            conn.execute("INSERT INTO youtube_auto_preparation(id,tenant,owner,operation_id,request_sha,state,version,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (identity, "tenant", "user", "cutover-held", "sha", "review", 1, json.dumps(body), "2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"))
        adapter = LedgerAdapter(LedgerSpec("youtube", "preparation", str(self.store.db_path), "youtube_auto_preparation", state="state", module="youtube_auto_publish"), self.bridge)
        dispatch = DispatchStore(Path(self.temp.name) / "dispatch.sqlite")
        control = Cutover(Coordinator(dispatch, AdapterRegistry([adapter]), internal_token="i" * 32, master_key="m" * 32))
        plan = control.plan("youtube", "2026-11-02T00:00:00Z")
        self.assertEqual(plan["paused_source_keys"], ["youtube:preparation:" + identity])
        for _ in range(2):
            result = control.apply(plan["plan_id"], plan["sha256"])
            self.assertEqual((result["status"], result["paused_count"], result["errors"]), ("held", 1, []))
        self.assertTrue(control.enable(plan["plan_id"], plan["sha256"])["enabled"])
        with self.workflow.db() as conn:
            row = conn.execute("SELECT state,body FROM youtube_auto_preparation WHERE id=?", (identity,)).fetchone()
        self.assertEqual(row["state"], "review")
        self.assertTrue(json.loads(row["body"])["versions"][0]["approved"])

    def test_pause_rechecks_native_creation_and_schedule_cutoff(self):
        for number, (created, scheduled) in enumerate((("2099-01-01T00:00:00Z", "2020-01-01T00:00:00Z"), ("2020-01-01T00:00:00Z", "2099-01-01T00:00:00Z"))):
            identity = (str(number + 4) * 32)
            body = {"id": identity, "status": "review", "current_version": 1, "publish_at": scheduled}
            with self.workflow.db(True) as conn:
                conn.execute("INSERT INTO youtube_auto_preparation(id,tenant,owner,operation_id,request_sha,state,version,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (identity, "tenant", "user", "future-" + identity, "sha", "review", 1, json.dumps(body), created, created))
            job = {"channel": "youtube", "family": "preparation", "source_id": identity, "source_key": "youtube:preparation:" + identity,
                   "scheduled_at": "2020-01-01T00:00:00Z", "source_created_at": "2020-01-01T00:00:00Z"}
            with self.assertRaisesRegex(AdapterError, "canonical_task_outside_cutoff"):
                self.bridge.pause(job, "2026-11-02T00:00:00Z")
        with self.workflow.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_canonical_hold").fetchone()[0], 0)

    def test_future_scheduled_publish_not_held_but_can_prepare_upload_early(self):
        from features.pipeline_dispatch.adapters import AdapterRegistry, LedgerAdapter, LedgerSpec
        from features.pipeline_dispatch.core import DispatchStore, stamp
        from features.pipeline_dispatch.cutover import Cutover
        from features.pipeline_dispatch.service import Coordinator
        task = self.store.enqueue_reviewed_youtube(preparation_id="prep-future", source_material_id="future-material",
            approved_cover_path=str(Path(self.temp.name) / "cover.jpg"), approved_cover_sha256=self.cover_sha,
            publish_at=self.task["publish_at"], operation_id="future-queued", job_id="e" * 32, content_id="future-content",
            app_id="1479", channel_local_id="1", channel_id=self.task["channel_id"], youtube_account_id="1",
            source_kind="custom_source", source_url="https://media.example/future.mp4", title="future", description_template="description",
            description_rendered="description", comment_text="hello", duplicate_confirmed=False,
            scopes=("https://www.googleapis.com/auth/youtube.force-ssl",), operator_user_id="user1")
        adapter = LedgerAdapter(LedgerSpec("youtube", "publish", str(self.store.db_path), "drama_youtube_publish", module="youtube_auto_publish"), self.bridge)
        dispatch = DispatchStore(Path(self.temp.name) / "dispatch-future.sqlite")
        control = Cutover(Coordinator(dispatch, AdapterRegistry([adapter]), internal_token="i" * 32, master_key="m" * 32))
        cutoff = stamp(datetime.now(timezone.utc))
        source_key = "youtube:publish:" + str(task["id"])
        offer = next(item for item in control.snapshot("youtube") if item["source_key"] == source_key)
        self.assertLessEqual(stamp(offer["scheduled_at"]), cutoff)
        self.assertGreater(stamp(offer["cutover_due_at"]), cutoff)
        self.assertNotIn(source_key, control.plan("youtube", cutoff)["paused_source_keys"])
        with self.assertRaisesRegex(AdapterError, "canonical_task_outside_cutoff"):
            self.bridge.pause(offer, cutoff)

    def test_scheduled_cancel_uses_same_video_and_canonical_command_intent(self):
        self.prepare_upload()
        self.checkpoint("youtube_session", {"session_uri": "https://www.googleapis.com/upload?x=1", "size": 1024})
        self.checkpoint("youtube_upload", {"state": "submitted", "video_id": "video_123"})
        self.operation("youtube_thumbnail", {"video_id": "video_123", "sha256": self.cover_sha})
        self.checkpoint("youtube_thumbnail", {"video_id": "video_123", "sha256": self.cover_sha, "status": "set"})
        self.checkpoint("youtube_processing", {"video_id": "video_123", "state": "succeeded", "visibility": "private"})
        self.operation("youtube_visibility", {"video_id": "video_123", "publish_at": self.task["publish_at"], "preserved_status": {"privacyStatus": "private"}})
        self.checkpoint("youtube_visibility", {"video_id": "video_123", "publish_at": self.task["publish_at"], "confirmed": {"state": "succeeded", "visibility": "private", "preserved_status": {"publishAt": self.task["publish_at"]}}})
        verified = self.bridge.complete(self.job, {"status": "scheduled", "video_id": "video_123"}, self.claim)
        self.store.request_reviewed_youtube_schedule(self.task["id"], action="cancel", expected_version=0, operation_id="explicit-cancel-1")
        self.job.update(generation=2, payload={"continuation": verified["continuation"]})
        self.claim = {"claim_token": "second-generation-token"}
        self.descriptor = self.bridge.reserve(self.job, self.claim)
        control = self.descriptor["request"]["schedule_control"]
        self.operation("youtube_schedule_control", {"video_id": "video_123", "control": control, "preserved_status": {"privacyStatus": "private", "publishAt": self.task["publish_at"]}})
        self.assertEqual(self.store.youtube_task(self.task["id"])["schedule_status"], "control_running")
        with self.assertRaises(AdapterError):
            self.operation("youtube_schedule_control", {"video_id": "video_123", "control": control})
        self.checkpoint("youtube_schedule_readback", {"video_id": "video_123", "confirmed": {"state": "succeeded", "visibility": "private", "preserved_status": {"privacyStatus": "private"}}})
        result = self.bridge.complete(self.job, {"status": "cancelled", "video_id": "video_123"}, self.claim)
        self.assertTrue(result["dispatch_completed"])
        task = self.store.youtube_task(self.task["id"])
        self.assertEqual((task["status"], task["video_attempt_count"], task["comment_attempt_count"]), ("cancelled", 1, 0))

    def test_scheduled_reschedule_then_immediate_never_allocates_second_video(self):
        self.prepare_upload()
        self.checkpoint("youtube_session", {"session_uri": "https://www.googleapis.com/upload?x=1", "size": 1024})
        self.checkpoint("youtube_upload", {"state": "submitted", "video_id": "video_123"})
        self.operation("youtube_thumbnail", {"video_id": "video_123", "sha256": self.cover_sha})
        self.checkpoint("youtube_thumbnail", {"video_id": "video_123", "sha256": self.cover_sha, "status": "set"})
        self.checkpoint("youtube_processing", {"video_id": "video_123", "state": "succeeded", "visibility": "private"})
        self.operation("youtube_visibility", {"video_id": "video_123", "publish_at": self.task["publish_at"], "preserved_status": {"privacyStatus": "private"}})
        self.checkpoint("youtube_visibility", {"video_id": "video_123", "publish_at": self.task["publish_at"], "confirmed": {"state": "succeeded", "visibility": "private", "preserved_status": {"publishAt": self.task["publish_at"]}}})
        result = self.bridge.complete(self.job, {"status": "scheduled", "video_id": "video_123"}, self.claim)
        for version, action in enumerate(("reschedule", "immediate")):
            target = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(timespec="seconds").replace("+00:00", "Z") if action == "reschedule" else ""
            self.store.request_reviewed_youtube_schedule(self.task["id"], action=action, publish_at=target, expected_version=version, operation_id="explicit-" + action)
            self.job.update(generation=version + 2, payload={"continuation": result["continuation"]})
            self.claim = {"claim_token": "generation-token-for-" + action}
            self.descriptor = self.bridge.reserve(self.job, self.claim)
            control = self.descriptor["request"]["schedule_control"]
            self.operation("youtube_schedule_control", {"video_id": "video_123", "control": control, "preserved_status": {"privacyStatus": "private"}})
            confirmed = {"state": "succeeded", "visibility": "private", "preserved_status": {"publishAt": target}} if target else {"state": "succeeded", "visibility": "public"}
            self.checkpoint("youtube_schedule_readback", {"video_id": "video_123", "confirmed": confirmed})
            if not target:
                self.operation("youtube_comment", {"video_id": "video_123", "channel_id": self.task["channel_id"], "text_sha256": hashlib.sha256(b"hello").hexdigest()})
                self.checkpoint("youtube_comment", {"video_id": "video_123", "comment_id": "comment-1"})
            result = self.bridge.complete(self.job, {"status": "scheduled" if target else "published", "video_id": "video_123", "comment_id": "" if target else "comment-1"}, self.claim)
        task = self.store.youtube_task(self.task["id"])
        self.assertEqual((task["status"], task["video_attempt_count"], task["comment_attempt_count"]), ("published", 1, 1))

    def test_visibility_requires_independent_readback(self):
        self.prepare_upload()
        self.checkpoint("youtube_session", {"session_uri": "https://www.googleapis.com/upload?x=1", "size": 1024})
        self.checkpoint("youtube_upload", {"state": "submitted", "video_id": "video_123"})
        with self.assertRaises(AdapterError):
            self.checkpoint("youtube_visibility", {"video_id": "video_123", "publish_at": "", "status": "public"})

    def test_legacy_public_upload_keeps_original_rules_and_single_attempt(self):
        self.assertEqual(self.descriptor["mode"], "legacy")
        self.assertNotIn("approved_cover", self.descriptor["request"])
        self.checkpoint("youtube_source", {"url": self.task["source_url"], "sha256": "b" * 64, "size": 1024, "duration_ms": 1000})
        details = {"title": "title", "description": "description", "size": 1024, "privacy_status": "public"}
        self.operation("youtube_begin_resumable", details)
        with self.assertRaises(AdapterError): self.operation("youtube_begin_resumable", details)
        self.assertIsNone(self.store.claim_youtube("legacy", "2099-01-01T00:00:00Z"))
        self.checkpoint("youtube_session", {"session_uri": "https://www.googleapis.com/upload?x=1", "size": 1024})
        self.assertEqual(self.store.youtube_task(self.task["id"])["video_attempt_count"], 1)
        self.checkpoint("youtube_upload", {"state": "submitted", "video_id": "video_123"})
        self.operation("youtube_video_state", {"video_id": "video_123", "channel_id": self.task["channel_id"]})
        self.checkpoint("youtube_legacy_status", {"video_id": "video_123", "state": "published", "visibility": "public"})
        self.operation("youtube_comment", {"video_id": "video_123", "channel_id": self.task["channel_id"], "text_sha256": hashlib.sha256(b"hello").hexdigest()})
        self.checkpoint("youtube_comment", {"video_id": "video_123", "comment_id": "comment-1"})
        result = self.bridge.complete(self.job, {"status": "published", "video_id": "video_123", "comment_id": "comment-1"}, self.claim)
        self.assertEqual(result["status"], "published")
        self.assertEqual(self.store.youtube_task(self.task["id"])["comment_attempt_count"], 1)

    def test_cross_claim_and_changed_cover_rejected(self):
        with self.assertRaises(AdapterError):
            self.bridge.reserve(self.job, {"claim_token": "different-claim-token"})
        with self.assertRaises(AdapterError):
            self.checkpoint("youtube_source", {"url": "https://elsewhere.example/video.mp4", "sha256": "b" * 64, "size": 100, "duration_ms": 1000})


if __name__ == "__main__": unittest.main()
