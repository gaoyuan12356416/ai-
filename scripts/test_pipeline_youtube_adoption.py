"""Adopt a pre-dispatch scheduled upload through real ledgers and coordinator."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from features.drama_synthesis.core import DramaSynthesisStore
from features.youtube_auto_publish.service import YouTubeWorkflow
from features.pipeline_dispatch.adapters import AdapterRegistry, AdapterError, LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.core import DispatchStore, dumps, stamp
from features.pipeline_dispatch.credentials import open_credentials
from features.pipeline_dispatch.executors import NodeExecutors
from features.pipeline_dispatch.service import Coordinator
from features.pipeline_dispatch.youtube_native import YouTubeNativeBridge


class ScheduledAdoptionTests(unittest.TestCase):
    def setUp(self):
        gates = patch.dict(os.environ, {"YOUTUBE_LIVE_ENABLED": "1", "YOUTUBE_AUTO_ENABLED": "1"})
        gates.start(); self.addCleanup(gates.stop)
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.due = (self.now + timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        self.store = DramaSynthesisStore(self.root / "native.db"); self.store.ensure_storage()
        image = io.BytesIO(); Image.new("RGB", (640, 360)).save(image, format="JPEG")
        cover = self.root / "cover.jpg"; cover.write_bytes(image.getvalue())
        self.task = self.store.enqueue_reviewed_youtube(
            preparation_id="old-preparation", source_material_id="old-material", approved_cover_path=str(cover),
            approved_cover_sha256=hashlib.sha256(cover.read_bytes()).hexdigest(), publish_at=self.due,
            operation_id="old-approved-upload", job_id="a"*32, content_id="drama", app_id="1479", channel_local_id="1",
            channel_id="UC"+"A"*22, youtube_account_id="1", source_kind="custom_source", source_url="https://media.example/video.mp4",
            title="title", description_template="description", description_rendered="description", comment_text="hello",
            duplicate_confirmed=False, scopes=("https://www.googleapis.com/auth/youtube.force-ssl",), operator_user_id="owner")
        self.source_key = "youtube:publish:" + str(self.task["id"])
        credential = SimpleNamespace(scopes={"https://www.googleapis.com/auth/youtube.force-ssl"}, capabilities={"comment_eligible": True, "upload_eligible": True})
        workflow = YouTubeWorkflow(self.store.db_path, self.root, None, None, None, self.store,
            channel_directory=SimpleNamespace(repository=SimpleNamespace(credential=lambda **_: credential)))
        self.workflow = workflow
        self.bridge = YouTubeNativeBridge(workflow, client=SimpleNamespace(refresh_access_token=lambda _: "ephemeral-token", verify_channel_identity=lambda *_: None))
        # Exact production-shaped imported state: an old worker uploaded and
        # confirmed the future appointment. There are no pipeline claim rows.
        self.change(status="scheduled", video_state="submitted", video_id="akR3Ipdx1X4", video_attempt_count=1,
                    schedule_status="armed", schedule_command_status="confirmed", schedule_confirmed_publish_at=self.due,
                    thumbnail_status="succeeded", processing_status="succeeded", lease_owner="", lease_expires_at_utc="", pipeline_dispatch_key="")
        driver = SimpleNamespace(reserve=lambda row, job, claim: self.bridge.reserve(job, claim),
            begin_operation=self.bridge.begin_operation, checkpoint=self.bridge.checkpoint,
            complete=self.bridge.complete, reconcile=self.bridge.reconcile, fail=self.bridge.fail)
        self.adapter = LedgerAdapter(LedgerSpec("youtube", "publish", str(self.store.db_path), "drama_youtube_publish", module="youtube_auto_publish"), driver)
        self.registry = AdapterRegistry([self.adapter])
        self.dispatch = DispatchStore(self.root / "dispatch.db", clock=lambda: self.now)
        self.service = Coordinator(self.dispatch, self.registry, internal_token="i"*32, master_key="m"*32)
        self.dispatch.channel_enabled("youtube", True)
        self.refresh_node()

    def change(self, **updates):
        with self.workflow.db(True) as conn:
            conn.execute("UPDATE drama_youtube_publish SET " + ",".join(name+"=?" for name in updates) + " WHERE id=?", (*updates.values(), self.task["id"]))

    def refresh_node(self):
        self.dispatch.register_node("cpu-main", ["publish:youtube", "backend:cpu"])
        self.dispatch.worker_heartbeat("cpu-main", "one", "publish")

    def offer(self):
        return self.adapter.project(self.adapter.read(self.task["id"]), now=self.now)

    def claim(self):
        self.refresh_node()
        return self.service.route("/internal/pipeline/jobs/claim", {"node_id":"cpu-main", "worker_id":"one", "stage":"publish"}, {"internal":True})["job"]

    def test_existing_scheduled_video_adopts_at_due_and_posts_only_one_comment(self):
        offer = self.offer()
        self.assertEqual((offer["status"], offer["scheduled_at"], offer["execution_available"]), ("ready", self.due, True))
        # Simulate the already-running old collector's unknown generation-0 row.
        old = dict(offer, status="unknown", execution_available=False, payload={"canonical_fingerprint":offer["canonical_fingerprint"]})
        observed = self.dispatch.offer(old)
        self.service.scan()
        self.assertEqual(self.dispatch.get_job(observed["id"])["status"], "ready")
        self.assertIsNone(self.claim())
        with self.workflow.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_youtube_claim").fetchone()[0], 0)
        self.now = datetime.fromisoformat(self.due.replace("Z", "+00:00")) + timedelta(seconds=1)
        job = self.claim()
        self.assertEqual(job["payload"]["mode"], "continuation")
        self.assertTrue(job["payload"]["adopted_existing_schedule"])
        self.assertNotIn("approved_cover", job["payload"]["request"])
        identity = {"job_id":job["id"], **{key:job[key] for key in ("generation","node_id","worker_id","claim_token")}}
        for operation in ("youtube_begin_resumable", "youtube_upload", "youtube_thumbnail", "youtube_visibility"):
            with self.assertRaisesRegex(AdapterError, "youtube_operation_invalid"):
                self.bridge.begin_operation(job, identity, {"operation_id":"forbidden-"+operation, "operation":operation,
                    "account_resource":job["payload"]["account_resource"], "details":{}})
        self.assertIsNone(self.store.claim_reviewed_youtube("legacy-worker", "2099-01-01T00:00:00Z"))
        calls, counter = [], [0]
        service = self.service
        class Context:
            def phase(self, name): pass
            def check(self): pass
            @contextmanager
            def operation(self, name, details, **kwargs):
                counter[0] += 1
                request = {**identity, "operation_id":"actual-operation-%s"%counter[0], "operation":name,
                    "account_resource":job["payload"]["account_resource"], "write":kwargs.get("write",True),
                    "details":details, "request_sha256":hashlib.sha256(dumps(details).encode()).hexdigest()}
                response = service.route("/internal/pipeline/jobs/begin-operation", request, {"internal":True})
                with open_credentials(response["credential_envelope"],service.node_key("cpu-main"),response["binding"]) as credential:
                    yield credential
            def checkpoint(self, phase, receipt):
                service.route("/internal/pipeline/jobs/checkpoint",{**identity,"checkpoint_id":"checkpoint-"+phase,"phase":phase,"receipt":receipt},{"internal":True})
        def state(token, video_id, **kwargs):
            calls.append(("GET",video_id));return {"state":"succeeded","visibility":"public"}
        def comment(token, video_id, comment_text, **kwargs):
            calls.append(("comment",video_id));return "original-video-comment"
        api = SimpleNamespace(read_reviewed_video_state=state, publish_comment=comment)
        executor = NodeExecutors({"state_root":str(self.root/"node"),"backend":"cpu"},platform_clients={"youtube_publish":api})
        with patch("features.pipeline_dispatch.youtube_native.utc_now",return_value=stamp(self.now)):
            result = executor.execute(job,Context())
        completed = self.service.route("/internal/pipeline/jobs/complete",{**identity,"receipt_id":"final-receipt","result":result},{"internal":True})
        self.assertTrue(completed["accepted"])
        native = self.store.youtube_task(self.task["id"])
        self.assertEqual((native["video_id"],native["video_attempt_count"],native["comment_attempt_count"],native["status"]), ("akR3Ipdx1X4",1,1,"published"))
        self.assertEqual(calls,[("GET","akR3Ipdx1X4"),("comment","akR3Ipdx1X4")])
        self.assertEqual(self.dispatch.get_job(job["id"])["status"],"published")
        self.assertIsNone(self.claim())

    def test_adoption_rejects_active_unknown_changed_receipt_or_prior_native_intent(self):
        original = self.store.youtube_task(self.task["id"])
        for fields in ({"lease_owner":"legacy-active"},{"unknown_outcome":1},{"schedule_confirmed_publish_at":"2099-01-01T00:00:00Z"},
                       {"schedule_command_status":"unknown"},{"pipeline_dispatch_key":"different-owner"},{"comment_attempt_count":1}):
            with self.subTest(fields=fields):
                self.change(**fields)
                self.assertFalse(self.offer()["execution_available"])
                self.change(**{key:original[key] for key in fields})
        projected = self.offer()
        job = {**projected,"id":1,"generation":1}
        claim = {"claim_token":"long-adoption-token"}
        changed = {**job,"payload":{**job["payload"],"continuation":{**job["payload"]["continuation"],"video_id":"other-video"}}}
        with self.assertRaisesRegex(AdapterError,"youtube_existing_schedule_not_claimable"):
            self.bridge.reserve(changed,claim)
        with self.workflow.db(True) as conn:
            conn.execute("INSERT INTO pipeline_youtube_operation VALUES(?,?,?,?,?,?)",("uncertain-op",self.source_key,1,"youtube_comment","a"*64,stamp(self.now)))
        self.assertFalse(self.offer()["execution_available"])
        with self.assertRaisesRegex(AdapterError,"youtube_existing_schedule_not_claimable"):
            self.bridge.reserve(job,claim)

    def test_existing_dispatch_unknown_intent_or_conflicting_final_receipt_stays_fenced(self):
        offer = self.offer()
        job = self.dispatch.offer(dict(offer,status="unknown",execution_available=False,payload={}))
        with self.dispatch.transaction() as conn:
            conn.execute("INSERT INTO pipeline_operation(operation_id,job_id,generation,node_id,operation,request_sha256,account_resource,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",("old-unknown",job["id"],0,"old-node","youtube_comment","a"*64,"youtube:channel:"+self.task["channel_id"],stamp(self.now),stamp(self.now)))
        self.dispatch.offer(offer)
        self.assertEqual(self.dispatch.get_job(job["id"])["status"],"unknown")
        with self.dispatch.transaction() as conn:
            conn.execute("DELETE FROM pipeline_operation WHERE job_id=?",(job["id"],))
            conn.execute("UPDATE pipeline_job SET status='completed',canonical_receipt=? WHERE id=?",(dumps({"status":"published","source_key":self.source_key,"publish_id":"other-video"}),job["id"]))
        self.dispatch.offer(offer)
        self.assertEqual(self.dispatch.get_job(job["id"])["status"],"completed")
        # A collector may already have reconciled the old observation as an
        # armed schedule. Only that exact video proof can be parked for due work.
        with self.dispatch.transaction() as conn:
            conn.execute("UPDATE pipeline_job SET canonical_receipt=? WHERE id=?",(dumps({"status":"scheduled","source_key":self.source_key,"publish_id":"akR3Ipdx1X4"}),job["id"]))
        self.dispatch.offer(offer)
        self.assertEqual(self.dispatch.get_job(job["id"])["status"],"ready")


if __name__ == "__main__": unittest.main()
