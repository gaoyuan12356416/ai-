from copy import deepcopy
from contextlib import ExitStack, contextmanager
from datetime import datetime
import hashlib
import json
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
from features.video_editing.recipe import PROFILE, TASK_TYPE, recipe_fingerprint, payload_fingerprint
from features.video_editing import gpu_integration as editing_gpu
from features.drama_synthesis.local_checkpoint import atomic_write_record, read_record, save_completed
from features.video_editing.media import upload_private, verify_receipt
from features.video_editing.gpu_integration import Config, EditingService
from scripts.test_drama_synthesis_gpu_cache import FakeMultipartCos, FakeCosError
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
            record = {"job_id": job["job_id"], "_payload": job, "fingerprint": payload_fingerprint(job), "generation": 1,
                      "status": "failed", "heartbeat_at": "2020-01-01T00:00:00+00:00"}
            rt = Mock(); rt._mutex = threading.RLock(); rt._records = {job["job_id"]: record}; rt._process_risk.return_value = "unknown"
            self.assertEqual(service.cleanup_failed(rt), []); self.assertTrue(work.exists())
            rt._process_risk.return_value = None
            self.assertEqual(service.cleanup_failed(rt), [job["job_id"]]); self.assertFalse(work.exists())
            self.assertTrue((state / "upload-evidence").exists()); self.assertFalse(service.can_resume(job))


class ExpiredScratchTests(unittest.TestCase):
    def setUp(self):
        self.runtimes = []

    @contextmanager
    def temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            first = len(self.runtimes)
            try:
                yield folder
            finally:
                for rt in reversed(self.runtimes[first:]): rt.close()

    def service(self, root, client=None, reserve=0):
        client = client or FakeMultipartCos()
        value = EditingService(Config(root / "work", root / "state", "b", "region", reserve_bytes=reserve), lambda: client)
        return value, client

    def runtime(self, root, service):
        adapter = RuntimeAdapter(service, legacy_execute=lambda p: {"job_id": p["job_id"], "output_video_url": "https://cos.test/done.mp4"},
            legacy_cached=lambda p: None, legacy_sync_cached=lambda p: None, legacy_resume=lambda p: True)
        rt = AsyncRuntime(root / "runtime", adapter.execute, adapter.cached, sync_cached_result=adapter.sync_cached,
            can_resume=adapter.resume, validate_payload=mixed_validate, fingerprint=mixed_fingerprint,
            public_result=public_result, can_prefetch=lambda p: False, autostart=False)
        self.addCleanup(rt.close)
        self.runtimes.append(rt)
        return rt

    def failed(self, root, payload=None):
        payload = payload or editing()
        service, client = self.service(root)
        rt = self.runtime(root, service); rt.submit(payload)
        record = rt._records[payload["job_id"]]
        record.update(status="failed", stage="failed", heartbeat_at="2020-01-01T00:00:00+00:00")
        rt._save(record)
        work = service.config.root / payload["job_id"]
        work.mkdir(parents=True); (work / "old.mp4").write_bytes(b"old media")
        return service, client, rt, payload, service._state(payload["job_id"])

    def expire(self, service, rt, payload, extra=0):
        stamp = datetime.fromisoformat(rt._records[payload["job_id"]]["heartbeat_at"]).timestamp()
        self.assertEqual(service.cleanup_failed(rt, now=stamp + 86401 + extra), [payload["job_id"]])

    def media(self, payload):
        stack = ExitStack()
        def download(source, path, **kwargs):
            path.write_bytes(b"x" * 100)
            return {"size_bytes": 100, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "etag": "v1"}
        def render(command, _duration, **kwargs):
            Path(command[-1]).write_bytes(b"new rendered output")
        metadata = {"duration_ms": 1000, "width": 720, "height": 1280, "fps": 30, "has_audio": True,
            "video_codec": "h264", "audio_codec": "aac", "audio_duration_ms": 1000,
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2", "container_brand": "isom", "pixel_format": "yuv420p", "sample_aspect_ratio": "1:1"}
        stack.download = stack.enter_context(mock.patch.object(editing_gpu, "verified_source_download", side_effect=download))
        stack.render = stack.enter_context(mock.patch.object(editing_gpu, "run_ffmpeg", side_effect=render))
        stack.enter_context(mock.patch.object(editing_gpu, "probe_media", return_value=metadata))
        return stack

    def test_01_real_runtime_expired_preupload_failure_rebuilds_original_id_once(self):
        with self.temporary() as folder:
            root = Path(folder); service, client = self.service(root, reserve=10**30)
            rt = self.runtime(root, service); payload = editing()
            rt.submit(legacy("already-completed")); rt.start()
            wait(lambda: rt.get("already-completed")["status"] == "completed")
            old = rt._path("already-completed").read_bytes()
            rt.submit(payload)
            wait(lambda: rt.get(payload["job_id"])["status"] == "failed")
            self.assertEqual(rt.get(payload["job_id"])["error"]["code"], "video_editing_disk_budget")
            self.expire(service, rt, payload)
            state = service._state(payload["job_id"]); marker = (state / "expired.json").read_bytes()
            self.assertEqual(read_record(state / "expired.json")["expired_generation"], 1)
            service.config = Config(root / "work", root / "state", "b", "region", reserve_bytes=0)
            with self.media(payload) as media:
                self.assertTrue(service.can_resume(payload))
                self.assertEqual(rt.resume(payload, 1)["generation"], 2)
                self.assertEqual(rt.resume(payload, 1)["generation"], 2)
                wait(lambda: rt.get(payload["job_id"])["status"] == "completed")
                receipt = rt.get(payload["job_id"])["result"]
                self.assertEqual(service.execute(payload), receipt)
                self.assertEqual(rt.submit(payload)["result"], receipt)
                self.assertEqual(rt.resume(payload, 2)["result"], receipt)
                self.assertEqual(media.render.call_count, 1)
            self.assertEqual(len(client.calls["create"]), 1); self.assertEqual(client.aborts, 0)
            self.assertEqual((state / "expired.json").read_bytes(), marker)
            self.assertEqual(read_record(state / "scratch_epoch.json")["admitted_generation"], 2)
            self.assertEqual(rt._path("already-completed").read_bytes(), old)
            self.assertFalse((service.config.root / payload["job_id"]).exists())

    def test_02_existing_generation_payload_and_process_guards_remain_authoritative(self):
        with self.temporary() as folder:
            service, client, rt, payload, state = self.failed(Path(folder))
            self.expire(service, rt, payload)
            with mock.patch.object(rt, "_process_risk", return_value="gpu_process_state_unknown"):
                with self.assertRaises(DramaSynthesisError): rt.resume(payload, 1)
            changed = deepcopy(payload); changed["recipe"]["clips"][0]["mute"] = True
            with self.assertRaises(DramaSynthesisError): rt.resume(changed, 1)
            with self.assertRaises(DramaSynthesisError): rt.resume(payload, 4)
            self.assertEqual(rt.get(payload["job_id"])["generation"], 1)
            self.assertEqual(rt.resume(payload, 1)["generation"], 2)
            self.assertEqual(rt.resume(payload, 1)["generation"], 2)
            self.assertFalse((state / "scratch_epoch.json").exists())
            with self.assertRaises(DramaSynthesisError): service.execute(payload)
            self.assertFalse((service.config.root / payload["job_id"]).exists())
            with rt._mutex: rt._begin(rt._records[payload["job_id"]])
            for job_id, generation, owner in ((payload["job_id"], 1, rt.instance),
                    (payload["job_id"], 2, "stale-owner"), (editing("2")["job_id"], 2, rt.instance)):
                context = editing_gpu.runtime._ExecutionContext(rt, job_id, generation, owner)
                with editing_gpu.runtime.use_context(context), self.assertRaises(DramaSynthesisError):
                    service.execute(payload)
            with rt._mutex:
                rt._records[payload["job_id"]].update(status="failed", stage="failed", _owner=None)
                rt._save(rt._records[payload["job_id"]])
            self.assertFalse((state / "scratch_epoch.json").exists())
            self.assertEqual(client.calls["create"], [])

    def test_03_every_upload_intent_and_unknown_sidecar_remains_fenced(self):
        cases = [("material.mp4.upload.json", json.dumps({"phase": p})) for p in ("creating", "uploading", "completing", "completed")]
        cases += [("material.mp4.upload.json", "broken"), (".material.mp4.upload.json.unfinished", "{}"),
                  ("upload-evidence", "unknown"), ("material.mp4.upload.json.lock", "nonempty")]
        for name, content in cases:
            with self.subTest(name=name, content=content), self.temporary() as folder:
                service, client, rt, payload, state = self.failed(Path(folder))
                self.expire(service, rt, payload); (state / name).write_text(content)
                self.assertFalse(service.can_resume(payload))
                with self.assertRaises(DramaSynthesisError): rt.resume(payload, 1)
                self.assertEqual(client.calls["create"], []); self.assertEqual(client.calls["part"], [])
                self.assertEqual(client.aborts, 0)
        with self.temporary() as folder:
            service, _, rt, payload, state = self.failed(Path(folder)); self.expire(service, rt, payload)
            (state / "material.mp4.upload.json.lock").touch()
            self.assertTrue(service.can_resume(payload))

    def test_04_only_authenticated_404_for_every_expected_key_allows_admission(self):
        for response in ({}, {"Content-Length": "100"}, FakeCosError(403), FakeCosError(503), TimeoutError()):
            with self.subTest(response=type(response).__name__), self.temporary() as folder:
                service, client, rt, payload, _ = self.failed(Path(folder)); self.expire(service, rt, payload)
                with mock.patch.object(client, "head_object", **({"side_effect": response} if isinstance(response, Exception) else {"return_value": response})):
                    self.assertFalse(service.can_resume(payload))
                self.assertEqual(client.calls["create"], [])
        with self.temporary() as folder:
            probe = editing(); probe["operation"] = "probe"; probe["recipe"]["clips"] = []
            service, client, rt, payload, _ = self.failed(Path(folder), probe); self.expire(service, rt, payload)
            self.assertTrue(service.can_resume(payload))
            self.assertEqual([c["Key"].rsplit("/", 1)[1] for c in client.calls["head"]], ["thumbnail.jpg", "preview.mp4"])
            with mock.patch.object(client, "head_object", side_effect=[FakeCosError(404), {}]):
                self.assertFalse(service.can_resume(payload))
        for changed in ("remote_object", "upload_intent"):
            with self.subTest(changed_after_admission=changed), self.temporary() as folder:
                service, client, rt, payload, state = self.failed(Path(folder)); self.expire(service, rt, payload)
                self.assertEqual(rt.resume(payload, 1)["generation"], 2)
                if changed == "remote_object": client.object_headers = {}
                else: (state / "material.mp4.upload.json").write_text('{"phase":"creating"}')
                with self.media(payload) as media:
                    rt.start(); wait(lambda: rt.get(payload["job_id"])["status"] == "failed")
                    media.download.assert_not_called(); media.render.assert_not_called()
                self.assertFalse((state / "scratch_epoch.json").exists())
                self.assertEqual(client.calls["create"], [])

    def test_05_valid_result_precedes_even_an_unbound_expiry_and_never_renders(self):
        with self.temporary() as folder:
            service, client, rt, payload, state = self.failed(Path(folder))
            work = service.config.root / payload["job_id"]
            path = work / "result.mp4"; path.write_bytes(b"finished already")
            artifact = upload_private(client, bucket="b", key="video-editing/v1/outputs/" + payload["job_id"] + "/material.mp4",
                path=path, checkpoint=state / "material.mp4.upload.json")
            completed = result(payload); completed["artifact"].update(artifact)
            atomic_write_record(state / "result.json", {"version": 1, "fingerprint": payload_fingerprint(payload), "result": completed})
            atomic_write_record(state / "expired.json", {"expired_at": 1, "reason": "failed_media_retention"})
            before = deepcopy(client.object_headers)
            with mock.patch.object(editing_gpu, "run_ffmpeg") as render, mock.patch.object(editing_gpu, "verified_source_download") as download:
                self.assertEqual(service.execute(payload), completed)
                self.assertEqual(rt.resume(payload, 1)["status"], "completed")
                render.assert_not_called(); download.assert_not_called()
            self.assertEqual(client.object_headers, before); self.assertEqual(len(client.calls["create"]), 1)
            self.assertEqual(rt.get(payload["job_id"])["generation"], 1)

    def test_06_epoch_crashes_and_restart_preserve_old_evidence_and_reuse_same_epoch(self):
        with self.temporary() as folder:
            root = Path(folder); service, client, rt, payload, state = self.failed(root)
            old_media = service.config.root / payload["job_id"] / "old.mp4"
            old_checkpoint = state / "clip-0000.local.json"
            save_completed(old_checkpoint, old_media, {"fingerprint": payload_fingerprint(payload), "clip_id": "c"}, {})
            old_bytes = old_checkpoint.read_bytes(); self.expire(service, rt, payload)
            def fail_epoch(path, value):
                if path.name == "scratch_epoch.json": raise OSError("before atomic epoch")
                return atomic_write_record(path, value)
            rt.start()
            with mock.patch.object(editing_gpu, "atomic_write_record", side_effect=fail_epoch), self.media(payload) as media:
                rt.resume(payload, 1); wait(lambda: rt.get(payload["job_id"])["status"] == "failed")
                media.download.assert_not_called(); media.render.assert_not_called()
                self.assertFalse((state / "scratch_epoch.json").exists())
            def fail_after_epoch(path, value):
                atomic_write_record(path, value)
                if path.name == "scratch_epoch.json": raise OSError("after atomic epoch")
            with mock.patch.object(editing_gpu, "atomic_write_record", side_effect=fail_after_epoch):
                rt.resume(payload, 2); wait(lambda: rt.get(payload["job_id"])["status"] == "failed")
            epoch = (state / "scratch_epoch.json").read_bytes()
            self.assertEqual(read_record(state / "scratch_epoch.json")["admitted_generation"], 3)
            rt.close(); rt = self.runtime(root, service); rt.start()
            with self.media(payload) as media:
                with mock.patch.object(editing_gpu, "upload_private", side_effect=RuntimeError("known before upload invocation")):
                    rt.resume(payload, 3); wait(lambda: rt.get(payload["job_id"])["status"] == "failed")
                self.assertEqual(media.render.call_count, 1)
                rt.close(); rt = self.runtime(root, service); rt.start()
                rt.resume(payload, 4); wait(lambda: rt.get(payload["job_id"])["status"] == "completed")
                self.assertEqual(media.render.call_count, 1)  # Generation 5 uses epoch 3's completed clip.
            self.assertEqual((state / "scratch_epoch.json").read_bytes(), epoch)
            self.assertEqual(old_checkpoint.read_bytes(), old_bytes)
            self.assertTrue((state / "local-attempts/g3/clip-0000.local.json").exists())
            self.assertEqual(len(client.calls["create"]), 1)

    def test_07_repeated_retention_preserves_same_generation_and_refences_new_epoch(self):
        with self.temporary() as folder:
            root = Path(folder); service, client, rt, payload, state = self.failed(root)
            self.expire(service, rt, payload); old = (state / "expired.json").read_bytes()
            self.expire(service, rt, payload, extra=60)
            self.assertEqual((state / "expired.json").read_bytes(), old)
            rt.start()
            with mock.patch.object(editing_gpu, "verified_source_download", side_effect=RuntimeError("before upload")):
                rt.resume(payload, 1); wait(lambda: rt.get(payload["job_id"])["status"] == "failed")
            self.expire(service, rt, payload)
            current = (state / "expired.json").read_bytes()
            self.assertNotEqual(current, old)
            self.assertEqual(read_record(state / "expired.json")["expired_generation"], 2)
            self.assertEqual((state / "expiry-history" / (hashlib.sha256(old).hexdigest() + ".json")).read_bytes(), old)
            self.expire(service, rt, payload, extra=60); self.assertEqual((state / "expired.json").read_bytes(), current)
            upload = state / "material.mp4.upload.json"; upload.write_text('{"phase":"creating"}')
            self.assertFalse(service.can_resume(payload)); upload.unlink()  # Local test fixture only.
            with self.media(payload):
                rt.resume(payload, 2); wait(lambda: rt.get(payload["job_id"])["status"] == "completed")
            self.assertEqual(read_record(state / "scratch_epoch.json")["admitted_generation"], 3)
            self.assertEqual(len(client.calls["create"]), 1)

    def test_08_legacy_markers_bad_evidence_and_unsafe_epoch_paths_are_fenced(self):
        with self.temporary() as folder:
            service, client, rt, payload, state = self.failed(Path(folder)); self.expire(service, rt, payload)
            marker = (state / "expired.json").read_bytes(); fingerprint = payload_fingerprint(payload)
            for bad in ({"expired_at": 1, "reason": "failed_media_retention"}, {"version": 1}):
                atomic_write_record(state / "expired.json", bad)
                self.assertFalse(service.can_resume(payload))
            (state / "expired.json").write_bytes(marker)
            valid = {"version": 1, "fingerprint": fingerprint, "admitted_generation": 2,
                     "consumed_expired_sha256": hashlib.sha256(marker).hexdigest(), "local_checkpoint_dir": "local-attempts/g2"}
            for bad in (dict(valid, local_checkpoint_dir="../outside"), dict(valid, fingerprint="f" * 64),
                        dict(valid, consumed_expired_sha256="f" * 64), dict(valid, extra=True)):
                atomic_write_record(state / "scratch_epoch.json", bad); self.assertFalse(service.can_resume(payload))
            (state / "scratch_epoch.json").unlink()
            (state / "clip-0000.local.json").write_text("broken")
            self.assertFalse(service.can_resume(payload)); (state / "clip-0000.local.json").unlink()
            future = state / "local-attempts/g2"; future.mkdir(parents=True)
            self.assertFalse(service.can_resume(payload)); future.rmdir()
            actual = Path.is_symlink
            for unsafe in (state / "expired.json", state.parent.parent):
                with mock.patch.object(Path, "is_symlink", lambda p: p == unsafe or actual(p)):
                    try: allowed = service.can_resume(payload)
                    except DramaSynthesisError: allowed = False
                    self.assertFalse(allowed)
            atomic_write_record(state / "scratch_epoch.json", valid)
            with mock.patch.object(Path, "is_symlink", lambda p: p == state / "local-attempts" or actual(p)):
                self.assertFalse(service.can_resume(payload))
            self.assertEqual(client.calls["create"], []); self.assertEqual(client.aborts, 0)

    def test_09_slow_expiry_head_does_not_block_legacy_get_or_runtime_mutex(self):
        with self.temporary() as folder:
            service, client, rt, payload, _ = self.failed(Path(folder)); self.expire(service, rt, payload)
            rt.submit(legacy("unrelated")); rt.start()
            wait(lambda: rt.get("unrelated")["status"] == "completed")
            original_head = client.head_object
            entered, release, observed = threading.Event(), threading.Event(), threading.Event()
            calls = []
            def head(**kwargs):
                calls.append(kwargs)
                if len(calls) == 2:
                    entered.set(); release.wait(5)
                return original_head(**kwargs)
            with mock.patch.object(client, "head_object", side_effect=head), self.media(payload):
                rt.resume(payload, 1)
                self.assertTrue(entered.wait(2))
                reader = threading.Thread(target=lambda: (rt.get("unrelated"), observed.set()))
                reader.start()
                try:
                    self.assertTrue(observed.wait(.5), "COS HEAD held the shared runtime mutex")
                finally:
                    release.set(); reader.join(2)
                wait(lambda: rt.get(payload["job_id"])["status"] == "completed")


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
