import hashlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from features.fb_auto_posts.core import FBAutoPostStore
from features.pipeline_dispatch import native_media
from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.fb_native import FBNativeBridge
from features.pipeline_dispatch.media_decision import build_context
from features.pipeline_dispatch.original_media import store_stream
from scripts import test_pipeline_tt_bridge, test_pipeline_x_native


class NativeMediaSelection(unittest.TestCase):
    def payload(self, job, url, kind, art=None):
        return {
            "job": job,
            "decision_id": "test-ticket",
            "decision": {
                "kind": kind,
                "source_identity": build_context(url, mode="")["source_identity"],
                "artifact": art or {},
            },
        }

    def test_fb_deadline_retires_only_preparation(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        due = now + timedelta(minutes=4)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.db"
            store = FBAutoPostStore(str(path), now_fn=lambda: now)
            src = "https://cdn.example/source.mp4"
            with store.connect() as c:
                c.execute(
                    "INSERT INTO fb_auto_template(id,name,status,created_at_utc,updated_at_utc) VALUES(1,'t','enabled',?,?)",
                    (now.isoformat(), now.isoformat()),
                )
                c.execute(
                    "INSERT INTO fb_auto_template_version(template_id,version,config_json,config_sha256,created_at_utc) VALUES(1,1,'{}',?,?)",
                    ("a" * 64, now.isoformat()),
                )
                c.execute(
                    "INSERT INTO fb_auto_run(id,template_id,template_version,slot_key,trigger_type,status,config_json,created_at_utc) VALUES(1,1,1,'am','scheduled','queued','{}',?)",
                    (now.isoformat(),),
                )
                c.execute(
                    """INSERT INTO fb_auto_task(id,run_id,template_id,template_version,page_id,group_id,status,material_id,content_id,
                         created_at_utc,source_media_url,planned_publish_at_utc,message_text)
                         VALUES(1,1,1,1,'page','pool','planned','mat','drama',?,?,?,'target caption')""",
                    (now.isoformat(), src, due.isoformat()),
                )
            rt = SimpleNamespace(
                store=store, executor=SimpleNamespace(live_enabled=True)
            )
            bridge = FBNativeBridge(rt)
            job = {
                "channel": "fb",
                "family": "auto",
                "source_id": "1",
                "source_key": "fb:auto:1",
                "stage": "prepare",
                "generation": 0,
                "publish_at": due.isoformat(),
                "observed_publish_at": due.isoformat(),
            }
            payload = self.payload(job, src, "deadline_original")
            with patch.object(native_media, "_headers", return_value=(200, "")) as head:
                result = bridge.dispatch("media-decision", payload)
                self.assertTrue(result["applied"])
                self.assertEqual(bridge.dispatch("media-decision", payload), result)
                head.assert_called_once()
            adapter = LedgerAdapter(LedgerSpec("fb", "auto", str(path), "fb_auto_task"))
            row = adapter.read("1")
            self.assertEqual(
                (row["status"], row["message_text"], row["prepared_media_url"]),
                ("ready", "target caption", src),
            )
            self.assertEqual(adapter.project(row)["stage"], "publish")
            self.assertIsNone(store.claim_next("worker", 1200, task_id=1))
            now = due + timedelta(seconds=1)
            claim = store.claim_next("worker", 1200, task_id=1)
            self.assertEqual(claim["media_url"], src)
            self.assertEqual(claim["media_selection_kind"], "deadline_original")

    def test_tt_selected_file_reaches_native_publish_boundary(self):
        f = test_pipeline_tt_bridge.RoutedTTBridgeTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        bridge, job = f.bridge, f.job
        with bridge.connect() as c:
            row, src, _ = native_media.read_only_snapshot(c, bridge, job)
        self.assertTrue(src)
        art = {
            "url": "https://verified.test/result.mp4",
            "size": 100,
            "sha256": "b" * 64,
            "duration_seconds": 12,
            "mime": "video/mp4",
            "profile": "tt-post-direct-outro-hevc-720x1280-v2",
        }
        payload = self.payload(job, src, "cross_platform_reuse", art)
        with patch.object(native_media, "_artifact", return_value=art):
            self.assertTrue(bridge.dispatch("media-decision", payload)["applied"])
        desc = bridge.reserve(dict(job, stage="publish", generation=2), f.claim)
        self.assertEqual(
            (desc["artifact"]["url"], desc["artifact"]["sha256"]),
            (art["url"], art["sha256"]),
        )
        self.assertEqual(
            desc["account_resource"], "tt:account:" + str(row["account_id"])
        )

    def test_x_target_keeps_its_own_account_and_material(self):
        f = test_pipeline_x_native.NativeTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        row = f.bridge.store.get_queue(f.queue(random=True)["id"])
        src = row["material_url"]
        job = f.job(row, "prepare", generation=0)
        original = {
            "url": src,
            "size": 2048,
            "duration_seconds": 30,
            "mime": "video/mp4",
            "sha256": "b" * 64,
            "profile": "source",
        }
        variant = dict(
            original,
            url="https://media.example.test/variant.mp4",
            sha256="d" * 64,
            profile="tt-post-random-overlay-h264-480x854-v3",
        )
        with patch.object(native_media, "_artifact", side_effect=[variant, original]):
            self.assertTrue(
                f.bridge.dispatch(
                    "media-decision",
                    self.payload(job, src, "cross_platform_reuse", variant),
                )["applied"]
            )
        desc = f.bridge.reserve(f.job(row, "publish"), f.job(row, "publish"))
        self.assertEqual(desc["artifact"]["sha256"], variant["sha256"])
        self.assertEqual(desc["account_resource"], "x:account:2")
        self.assertEqual(
            f.bridge.store.get_queue(row["id"])["material_id"], row["material_id"]
        )

    def test_tt_original_storage_checks_the_received_bytes_before_upload(self):
        data = b"unaltered source"
        sha = hashlib.sha256(data).hexdigest()
        store = Mock()
        store.config.cos_prefix = "tt"

        def upload(key, path, expected_sha, size):
            self.assertEqual(
                (key, expected_sha, size),
                ("tt/original/" + sha + ".mp4", sha, len(data)),
            )
            self.assertEqual(Path(path).read_bytes(), data)

        store.upload.side_effect = upload
        store.url.side_effect = lambda key: "https://verified.test/" + key
        head = (json.dumps({"sha256": sha, "size": len(data)}) + "\n").encode()
        with tempfile.TemporaryDirectory() as folder:
            self.assertIn(sha, store_stream(io.BytesIO(head + data), folder, store))
            with self.assertRaises(ValueError):
                store_stream(io.BytesIO(head + b"z" * len(data)), folder, store)
            store.upload.assert_called_once()
