from copy import deepcopy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest import mock
import os

from features.drama_synthesis.async_runtime import AsyncRuntime, render_fingerprint
from features.drama_synthesis.core import DramaSynthesisError
from features.video_editing.gpu_integration import RuntimeAdapter, mixed_validate, mixed_fingerprint, public_result, task_kind
from features.video_editing.recipe import PROFILE, TASK_TYPE, recipe_fingerprint
from features.video_editing.media import upload_private, verify_receipt
from features.video_editing.gpu_integration import Config, EditingService
from scripts.test_drama_synthesis_gpu_cache import FakeMultipartCos
from scripts import test_drama_synthesis_gpu_runtime as http_fixture


def legacy(n="legacy"):
    return {"job_id": n, "episodes": [{"episode_number": 1, "episode_url": "https://source.test/1.mp4"}], "outputs": {"concat_video": True}}


def editing(n="1"):
    return {"job_id": "ve_" + n * 32, "task_type": TASK_TYPE, "operation": "render", "recipe": {
        "version": 1, "profile": PROFILE, "sources": [{"source_id": "s", "kind": "episode", "url": "https://source.test/a.mp4",
            "identity": {"size_bytes": 100, "etag": "v1"}, "duration_ms": 1000, "width": 720, "height": 1280, "has_audio": True}],
        "clips": [{"id": "c", "source_id": "s", "start_ms": 0, "end_ms": 1000}]}}


def result(payload):
    return {"job_id": payload["job_id"], "task_type": TASK_TYPE, "operation": "render",
        "recipe_sha256": recipe_fingerprint(payload["recipe"]), "artifact": {"bucket": "b", "key": "video-editing/out.mp4",
        "sha256": "a" * 64, "size_bytes": 100, "binding": "b" * 32, "etag": "e", "width": 720, "height": 1280,
        "duration_ms": 1000, "fps": 30, "has_audio": True, "video_codec": "h264", "audio_codec": "aac"}}


def wait(fn):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        if fn(): return
        time.sleep(.02)
    raise AssertionError("runtime did not converge")


class RuntimeTests(unittest.TestCase):
    def create(self, root, execute=None):
        service = Mock()
        service.cached.return_value = None; service.can_resume.return_value = True
        service.execute.side_effect = execute or result
        adapter = RuntimeAdapter(service, legacy_execute=lambda p: {"job_id": p["job_id"], "output_video_url": "https://cos.test/legacy.mp4"},
            legacy_cached=lambda p: None, legacy_sync_cached=lambda p: None, legacy_resume=lambda p: True)
        runtime = AsyncRuntime(root, adapter.execute, adapter.cached, sync_cached_result=adapter.sync_cached,
            can_resume=adapter.resume, validate_payload=mixed_validate, fingerprint=mixed_fingerprint,
            public_result=public_result, can_prefetch=lambda p: task_kind(p) == "drama", autostart=False)
        self.addCleanup(runtime.close)
        return runtime, service

    def test_legacy_hash_and_dto_are_unchanged(self):
        payload = legacy(); self.assertEqual(mixed_fingerprint(payload), render_fingerprint(payload))
        with tempfile.TemporaryDirectory() as root:
            runtime, _ = self.create(root); runtime.submit(payload); runtime.start()
            wait(lambda: runtime.get("legacy")["status"] == "completed")
            self.assertEqual(runtime.get("legacy")["result"], {"job_id": "legacy", "output_video_url": "https://cos.test/legacy.mp4"})
            runtime.close()

    def test_mixed_queue_is_single_slot_and_durable(self):
        started, finish, order = threading.Event(), threading.Event(), []
        def execute(p):
            order.append(p["job_id"]); started.set(); finish.wait(4); return result(p)
        with tempfile.TemporaryDirectory() as root:
            runtime, service = self.create(root, execute)
            runtime.submit(editing()); runtime.submit(legacy()); runtime.start()
            self.assertTrue(started.wait(2))
            self.assertEqual(runtime.get("legacy")["status"], "queued")
            self.assertEqual(runtime.submit(editing())["status"], "running")
            finish.set(); wait(lambda: runtime.get("legacy")["status"] == "completed")
            self.assertEqual(service.execute.call_count, 1)
            self.assertEqual(runtime.get(editing()["job_id"])["result"]["artifact"]["key"], "video-editing/out.mp4")
            runtime.close()
            recovered, _ = self.create(root)
            self.assertEqual(recovered.get(editing()["job_id"])["status"], "completed")
            recovered.close()

    def test_frozen_recipe_conflict_and_namespace_gate(self):
        with tempfile.TemporaryDirectory() as root:
            runtime, _ = self.create(root); value = editing(); runtime.submit(value)
            value["recipe"]["clips"][0]["mute"] = True
            with self.assertRaises(DramaSynthesisError) as caught: runtime.submit(value)
            self.assertEqual(caught.exception.code, "gpu_job_input_conflict")
            wrong = editing(); wrong.pop("task_type")
            with self.assertRaises(DramaSynthesisError): mixed_validate(wrong)
            wrong = legacy(); wrong["task_type"] = TASK_TYPE
            with self.assertRaises(DramaSynthesisError): mixed_validate(wrong)
            runtime.close()

    def test_public_result_rejects_recipe_conflict_and_strips_extra(self):
        value = result(editing()); value["secret"] = "hidden"
        self.assertNotIn("secret", public_result(value, editing()))
        value["recipe_sha256"] = "0" * 64
        with self.assertRaises(DramaSynthesisError): public_result(value, editing())

    def test_unknown_process_hold_blocks_both_types(self):
        with tempfile.TemporaryDirectory() as root:
            runtime, service = self.create(root); old = runtime.submit(legacy("blocked"))
            record = runtime._records["blocked"]
            record.update(status="recovery_required", _resource_blocked=True, _children={"1": {"pid": 1}})
            runtime._save(record)
            runtime.submit(editing()); runtime.submit(legacy()); runtime.start()
            time.sleep(.35)
            self.assertEqual(runtime.get(editing()["job_id"])["status"], "queued")
            self.assertEqual(runtime.get("legacy")["status"], "queued")
            service.execute.assert_not_called(); runtime.close()


class MediaReceiptTests(unittest.TestCase):
    def test_private_upload_has_verified_sha_and_survives_lost_complete(self):
        client = FakeMultipartCos(); client.lose_complete_response = True
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "media.mp4"; path.write_bytes(b"fixture-media")
            checkpoint = Path(root) / "upload.json"
            receipt = upload_private(client, bucket="test-12345", key="video-editing/v1/out.mp4", path=path, checkpoint=checkpoint)
            self.assertEqual(client.calls["create"][0]["ACL"], "private")
            self.assertEqual(client.calls["create"][0]["Metadata"]["x-cos-meta-sha256"], receipt["sha256"])
            self.assertTrue(verify_receipt(client, receipt))
            self.assertEqual(receipt, upload_private(client, bucket="test-12345", key="video-editing/v1/out.mp4", path=path, checkpoint=checkpoint))
            self.assertEqual(len(client.calls["create"]), 1)
            client.object_headers["Content-Length"] = "9"
            with self.assertRaises(DramaSynthesisError): verify_receipt(client, receipt)

    def test_uncertain_create_is_fenced_and_receipt_retained(self):
        client = FakeMultipartCos(); client.lose_create_response = True
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "media.mp4"; path.write_bytes(b"fixture-media")
            checkpoint = Path(root) / "upload.json"
            for _ in range(2):
                with self.assertRaises(DramaSynthesisError):
                    upload_private(client, bucket="test-12345", key="video-editing/v1/out.mp4", path=path, checkpoint=checkpoint)
            self.assertEqual(len(client.calls["create"]), 1)
            self.assertTrue(path.exists()); self.assertTrue(checkpoint.exists())

    def test_failed_cleanup_requires_stopped_terminal_job_and_retains_state(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            service = EditingService(Config(root / "work", root / "state", "b", "region", reserve_bytes=0), lambda: None)
            job = editing(); work = root / "work" / job["job_id"]; work.mkdir(parents=True); (work / "media").write_bytes(b"x")
            state = service._state(job["job_id"]); (state / "upload-evidence").write_text("retained")
            record = {"job_id": job["job_id"], "_payload": job, "status": "failed", "heartbeat_at": "2020-01-01T00:00:00+00:00"}
            rt = Mock(); rt._mutex = threading.RLock(); rt._records = {job["job_id"]: record}; rt._process_risk.return_value = "unknown"
            self.assertEqual(service.cleanup_failed(rt), []); self.assertTrue(work.exists())
            rt._process_risk.return_value = None
            self.assertEqual(service.cleanup_failed(rt), [job["job_id"]]); self.assertFalse(work.exists())
            self.assertTrue((state / "upload-evidence").exists()); self.assertFalse(service.can_resume(job))


class EditingHTTPTests(unittest.TestCase):
    setUp = http_fixture.WorkerHTTPTests.setUp
    tearDown = http_fixture.WorkerHTTPTests.tearDown
    request = http_fixture.WorkerHTTPTests.request

    def test_types_auth_and_hot_intake_marker(self):
        service = Mock(); service.cached.return_value = None; service.execute.side_effect = result; service.can_resume.return_value = True
        self.worker.editing_from_app = lambda app: service
        marker = Path(self.directory.name) / "disabled"
        with mock.patch.dict(os.environ, {"VIDEO_EDITING_GPU_ENABLED": "1", "VIDEO_EDITING_DISABLED_FILE": str(marker)}):
            payload = editing()
            url = "/api/gpu-video/video-editing/jobs"
            self.assertEqual(self.request("POST", url, payload, "bad")[0], 401)
            self.assertEqual(self.request("POST", "/api/gpu-video/jobs", payload)[0], 400)
            self.assertEqual(self.request("POST", url, legacy())[0], 400)
            self.assertEqual(self.request("POST", url, payload)[0], 202)
            wait(lambda: self.worker.RUNTIME.get(payload["job_id"])["status"] == "completed")
            marker.write_text("closed")
            self.assertEqual(self.request("POST", url, editing("2"))[0], 503)
            self.assertEqual(self.request("GET", url + "/" + payload["job_id"])[0], 200)
            resumed = dict(payload, expected_generation=1)
            self.assertEqual(self.request("POST", url + "/" + payload["job_id"] + "/resume", resumed)[0], 202)
            self.assertEqual(self.request("GET", "/api/gpu-video/jobs/" + payload["job_id"])[0], 404)
            self.assertFalse(self.request("GET", "/healthz")[1]["video_editing_enabled"])


if __name__ == "__main__": unittest.main()
