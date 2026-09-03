#!/usr/bin/env python3
"""Focused, fake-only acceptance tests for the authoritative drama upgrade."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qsl, urlsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.drama_synthesis.core import (  # noqa: E402
    COMMENT_SCOPE, DramaSynthesisError, DramaSynthesisStore,
    ImmutableFilesystemPublisher, RECIPE_CATEGORIES, RECIPE_PROFILE,
    build_long_url, freeze_random_recipe, render_wrapper_html,
    _video_sync_payload, _comment_sync_payload,
)
from features.drama_synthesis.unified_youtube import (  # noqa: E402
    ControlledRPCExecutor, UnifiedYouTubeWriter, build_unified_youtube_writer_from_env,
    run_sync_outbox_once, validate_controlled_operation, validate_entity_payload,
)
from features.drama_synthesis.youtube import (  # noqa: E402
    YouTubeCredential, YouTubeCredentialRepository, YouTubeHTTPClient,
    YouTubeHTTPError, YouTubePublishEngine,
)
from scripts.migrate_drama_synthesis_outputs import migrate  # noqa: E402

JOB_ID = "a" * 32
UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
READONLY = "https://www.googleapis.com/auth/youtube.readonly"
CHANNEL = "UC" + "A" * 22


def unified_video_payload(*, publish_id=1, video_id="video_1"):
    return {
        "publish_id": publish_id,
        "video_id": video_id,
        "app_id": 1479,
        "channel_local_id": 1,
        "operator_user_id": "c31ggb2g",
        "job_id": JOB_ID,
        "content_id": "2284",
        "source_kind": "concat_video",
        "source_url": "https://example.test/video.mp4",
        "title": "Title",
        "description_rendered": "required",
        "privacy_status": "public",
        "published_at_utc": "2026-08-26T00:00:00Z",
    }


def unified_comment_payload(*, publish_id=1, video_id="video_1", comment_id="comment_1"):
    return {
        "publish_id": publish_id,
        "video_id": video_id,
        "comment_id": comment_id,
        "channel_local_id": 1,
        "operator_user_id": "c31ggb2g",
        "comment_text": "hello",
        "published_at_utc": "2026-08-26T00:00:00Z",
    }


def catalog():
    counts = {"border": 3, "corners": 3, "opacity_video": 5, "tint": 7}
    categories = {}
    for category, count in counts.items():
        suffix = ".webm" if category in {"corners", "opacity_video"} else ".png"
        media = "video/webm" if suffix == ".webm" else "image/png"
        categories[category] = [
            {"name": f"{category}-{index:02d}{suffix}", "sha256": f"{index:064x}", "size": 100 + index, "media_type": media}
            for index in range(1, count + 1)
        ]
    return {"version": 1, "profile": RECIPE_PROFILE, "manifest_sha256": "f" * 64, "categories": categories}


def recipe(source="concat_video", mode="auto", layers=None):
    request = {"mode": mode, "source": source, "layers": layers}
    return freeze_random_recipe(job_id=JOB_ID, content_id="剧 1/+", request=request, catalog=catalog())


class FakeCredentialRepo:
    def credential(self, **_kwargs):
        return YouTubeCredential(
            account_id="2", channel_local_id="1", channel_id=CHANNEL,
            channel_name="Current Product", channel_status=1,
            scopes=frozenset({UPLOAD, READONLY, COMMENT_SCOPE}),
            refresh_token="refresh", client_id="client", client_secret="secret",
        )


class FakePublishClient:
    def __init__(self, video_states):
        self.video_states = list(video_states)
        self.comments = 0
        self.identity_checks = 0

    def refresh_access_token(self, _credential): return "access"
    def verify_channel_identity(self, _token, expected):
        self.identity_checks += 1
        if expected != CHANNEL: raise AssertionError("wrong channel")
    def video_status(self, _token, _video_id): return {"state": self.video_states.pop(0), "visibility": "public"}
    def publish_comment(self, _token, *, video_id, comment_text, channel_id):
        if channel_id != CHANNEL: raise AssertionError("wrong comment channel")
        self.comments += 1
        self.last_comment = (video_id, comment_text)
        return "comment_123"


class FakeRecoveringUploadClient(FakePublishClient):
    def __init__(self, query_state="submitted"):
        super().__init__([])
        self.query_state = query_state
        self.uploads = 0
        self.queries = 0
        self.begins = 0

    def begin_resumable(self, *_args, **_kwargs):
        self.begins += 1
        return "https://upload.youtube.test/session"

    def upload(self, *_args, **_kwargs):
        self.uploads += 1
        raise YouTubeHTTPError("youtube_upload_response_unknown", "response lost", unknown=True)

    def query_upload(self, *_args, **_kwargs):
        self.queries += 1
        if self.query_state == "submitted":
            return {"state": "submitted", "video_id": "video_recovered"}
        return {"state": self.query_state}


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = {}
    def json(self): return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses); self.trust_env = True; self.gets = 0
    def get(self, *_args, **_kwargs): self.gets += 1; return self.responses.pop(0)
    def close(self): pass


class FakeRPCSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []
        self.trust_env = True

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.responses.pop(0)

    def close(self): pass


class FakeSyncStore:
    def __init__(self, payload_json, *, entity_kind="video", external_id="video_fake"):
        self.item = {
            "id": 91, "lease_generation": 4, "entity_kind": entity_kind,
            "external_id": external_id, "payload_json": payload_json,
        }
        self.finish_calls = []

    def claim_youtube_sync(self, _worker_id, _expiry):
        item, self.item = self.item, None
        return item

    def finish_youtube_sync(self, outbox_id, **kwargs):
        self.finish_calls.append((outbox_id, kwargs))
        return {"id": outbox_id, "status": "synced" if kwargs["success"] else "failed"}


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = DramaSynthesisStore(self.root / "jobs.sqlite3")
        self.store.ensure_storage()

    def tearDown(self):
        self.tmp.cleanup()

    def enqueue(self, operation="operation:test-0001", comment="", description="required", confirmed=False, operator="c31ggb2g"):
        return self.store.enqueue_youtube(
            operation_id=operation, job_id=JOB_ID, content_id="2284", app_id="1479",
            channel_local_id="1", channel_id=CHANNEL, youtube_account_id="2",
            source_kind="concat_video", source_url="https://example.test/video.mp4",
            title="Title", description_template=description, description_rendered=description,
            comment_text=comment, duplicate_confirmed=confirmed,
            scopes=(UPLOAD, READONLY, COMMENT_SCOPE), operator_user_id=operator, operator_name="tester",
        )

    def test_operator_string_is_preserved_in_all_outbox_payloads(self):
        for index, actor in enumerate(("c31ggb2g", "892fd2e8", "", "原始身份_é", "Actor_ID-1")):
            with self.subTest(actor=actor):
                row = self.enqueue(operation="operation:actor-%08d" % index, operator=actor)
                self.assertEqual(row["operator_user_id"], actor)
                row["video_id"] = "video_actor1"
                video = _video_sync_payload(row, "video_actor1", "2026-08-27T00:00:00Z")
                row["comment_text"] = "actor audit"
                comment = _comment_sync_payload(row, "comment_actor1", "2026-08-27T00:00:00Z")
                self.assertEqual(video["operator_user_id"], actor)
                self.assertEqual(comment["operator_user_id"], actor)
                validate_entity_payload("video", "video_actor1", video)
                validate_entity_payload("publish_log", str(row["id"]), video)
                validate_entity_payload("comment", "comment_actor1", comment)

    def test_operator_is_not_silently_truncated_or_control_normalized(self):
        for value in (803, "a" * 129, "line\nactor", "actor\u202e", "actor\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(DramaSynthesisError):
                self.enqueue(operator=value)
        connection = sqlite3.connect(self.store.db_path)
        try:
            self.assertEqual(connection.execute("SELECT count(*) FROM drama_youtube_publish").fetchone()[0], 0)
        finally:
            connection.close()

    def test_random_source_is_required_and_exact(self):
        with self.assertRaises(DramaSynthesisError):
            freeze_random_recipe(job_id=JOB_ID, content_id="1", request={"mode": "auto"}, catalog=catalog())
        self.assertEqual(recipe("no_bgm_video")["source"], "no_bgm_video")

    def test_fb_v3_catalog_is_315_and_excludes_light(self):
        data = catalog()
        self.assertEqual(set(data["categories"]), set(RECIPE_CATEGORIES))
        product = 1
        for rows in data["categories"].values():
            product *= len(rows)
        self.assertEqual(product, 315)

    def test_auto_recipe_is_deterministic_and_source_frozen(self):
        self.assertEqual(recipe(), recipe())
        self.assertNotEqual(recipe("concat_video")["recipe_sha256"], recipe("no_bgm_video")["recipe_sha256"])
        value = recipe()
        self.assertTrue(-2000 <= value["rotation_millidegrees"] <= 2000)
        self.assertTrue(9800 <= value["scale_bp"] <= 10200)
        self.assertTrue(100 <= value["tint_opacity_bp"] <= 1000)

    def test_manual_recipe_requires_all_four_layers(self):
        layers = {key: catalog()["categories"][key][0]["name"] for key in RECIPE_CATEGORIES}
        self.assertEqual(set(recipe(mode="manual", layers=layers)["assets"]), set(RECIPE_CATEGORIES))
        layers.pop("tint")
        with self.assertRaises(DramaSynthesisError):
            recipe(mode="manual", layers=layers)

    def test_recipe_freeze_and_output_are_immutable(self):
        value = recipe()
        first = self.store.freeze_recipe(JOB_ID, value)
        self.assertEqual(first["recipe_sha256"], value["recipe_sha256"])
        self.store.complete_recipe(JOB_ID, output_url="https://example.test/random.mp4", output_sha256="1" * 64, output_profile=RECIPE_PROFILE, recipe_sha256=value["recipe_sha256"])
        with self.assertRaises(DramaSynthesisError):
            self.store.complete_recipe(JOB_ID, output_url="https://example.test/other.mp4", output_sha256="2" * 64, output_profile=RECIPE_PROFILE, recipe_sha256=value["recipe_sha256"])

    def test_short_target_has_exact_order_and_encoding(self):
        target = build_long_url(JOB_ID, "剧 1/+")
        parsed = urlsplit(target)
        self.assertEqual(parsed.scheme + "://" + parsed.netloc + parsed.path, "https://www.dramawavew2a.com/ads/101/2284/view")
        self.assertEqual(parse_qsl(parsed.query), [("af_dp", "剧 1/+"), ("c", "ai_youtube"), ("af_channel", "ai_youtube"), ("af_c_id", JOB_ID)])

    def test_wrapper_only_reads_fbclid_and_has_no_open_redirect(self):
        body = render_wrapper_html(JOB_ID, "content").decode()
        self.assertIn("p.get('fbclid')", body)
        self.assertNotIn("redirect_uri", body)
        self.assertEqual(body.count("p.get("), 1)
        self.assertNotIn("p.get('af_dp')", body)

    def test_short_link_identity_is_job_and_material(self):
        publisher = ImmutableFilesystemPublisher(self.root / "public")
        one = self.store.ensure_short_link(JOB_ID, "concat_video", "c1", publisher)
        again = self.store.ensure_short_link(JOB_ID, "concat_video", "c1", publisher)
        other = self.store.ensure_short_link(JOB_ID, "no_bgm_video", "c1", publisher)
        self.assertEqual(one["id"], again["id"])
        self.assertNotEqual(one["id"], other["id"])
        self.assertTrue(one["short_url"].startswith("https://gy.g2flow.com/s2l/youtube/"))

    def test_short_link_conflict_and_concurrency(self):
        publisher = ImmutableFilesystemPublisher(self.root / "public")
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda _: self.store.ensure_short_link(JOB_ID, "concat_video", "c1", publisher), range(4)))
        self.assertEqual(len({row["id"] for row in rows}), 1)
        with self.assertRaises(DramaSynthesisError):
            self.store.ensure_short_link(JOB_ID, "concat_video", "changed", publisher)

    def test_short_link_stale_temp_name_cannot_block_publish(self):
        public = self.root / "public"
        public.mkdir()
        (public / "1.html.tmp.stale-process").write_bytes(b"stale")
        row = self.store.ensure_short_link(
            JOB_ID,
            "concat_video",
            "c1",
            ImmutableFilesystemPublisher(public),
        )
        self.assertEqual(row["id"], 1)
        self.assertTrue((public / "1.html").is_file())

    def test_exact_additive_table_names_exist(self):
        conn = sqlite3.connect(self.store.db_path)
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("drama_material_short_link", names)
        self.assertIn("drama_youtube_publish", names)
        self.assertIn("drama_youtube_sync_outbox", names)
        self.assertNotIn("drama_youtube_publish_task", names)
        conn.close()

    def test_python_runtime_sources_parse_as_39(self):
        files = [
            ROOT / "features/drama_synthesis/core.py",
            ROOT / "features/drama_synthesis/youtube.py",
            ROOT / "features/drama_synthesis/unified_youtube.py",
            ROOT / "features/drama_synthesis/unified_youtube_rpc.py",
            ROOT / "scripts/drama_synthesis_gpu_worker.py",
            ROOT / "scripts/drama_youtube_unified_writer_rpc.py",
            ROOT / "scripts/migrate_drama_synthesis_outputs.py",
            ROOT / "scripts/migrate_drama_youtube_unified_schema.py",
            ROOT / "scripts/bootstrap_drama_youtube_ads_ai.py",
        ]
        for path in files:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9))

    def _legacy_db(self, outputs='{"concat_video": true}'):
        path = self.root / "legacy.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE drama_material_job(id INTEGER PRIMARY KEY,outputs_json TEXT,output_video_url TEXT,output_video_no_bgm_url TEXT,cover_16x9_url TEXT)")
        conn.execute("INSERT INTO drama_material_job VALUES(1,?,?,?,?)", (outputs, "https://a/video.mp4", "https://a/no.mp4", "https://a/cover.jpg"))
        conn.commit(); conn.close()
        return path

    def test_outputs_migration_dry_run_backup_apply_and_idempotency(self):
        db = self._legacy_db()
        self.assertEqual(migrate(db)["changes"], 1)
        backup = self.root / "backup.sqlite3"
        applied = migrate(db, apply=True, backup_path=backup)
        self.assertEqual(applied["changes"], 1)
        self.assertTrue(backup.is_file())
        backup_conn = sqlite3.connect(backup)
        self.assertEqual(backup_conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(backup_conn.execute("SELECT outputs_json FROM drama_material_job").fetchone()[0], '{"concat_video": true}')
        backup_conn.close()
        self.assertEqual(migrate(db)["changes"], 0)
        conn = sqlite3.connect(db)
        value = json.loads(conn.execute("SELECT outputs_json FROM drama_material_job").fetchone()[0])
        conn.close()
        self.assertEqual(value, {"concat_video": True, "no_bgm_video": True, "cover_16x9": True, "random_template_video": False})

    def test_outputs_migration_uses_authoritative_random_template_video_key(self):
        db = self._legacy_db('{"concat_video": false,"random_template_video": true}')
        migrate(db, apply=True, backup_path=self.root / "authoritative.backup.sqlite3")
        conn = sqlite3.connect(db)
        value = json.loads(conn.execute("SELECT outputs_json FROM drama_material_job").fetchone()[0])
        conn.close()
        self.assertTrue(value["random_template_video"])
        self.assertNotIn("random_template", value)

    def test_outputs_migration_invalid_json_rolls_back(self):
        db = self._legacy_db("not-json")
        with self.assertRaises(ValueError):
            migrate(db, apply=True, backup_path=self.root / "invalid.backup.sqlite3")
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute("SELECT outputs_json FROM drama_material_job").fetchone()[0], "not-json")
        conn.close()

    def test_outputs_migration_concurrent_dry_runs_are_read_only(self):
        db = self._legacy_db()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: migrate(db)["changes"], range(100)))
        self.assertEqual(results, [1] * 100)

    def test_outputs_migration_cli_exposes_explicit_dry_run(self):
        text = (ROOT / "scripts/migrate_drama_synthesis_outputs.py").read_text(encoding="utf-8")
        self.assertIn('mode.add_argument("--dry-run"', text)
        self.assertIn("add_mutually_exclusive_group", text)

    def test_youtube_metadata_uses_required_utf8_byte_limit(self):
        with self.assertRaises(DramaSynthesisError):
            self.enqueue(operation="operation:empty-desc", description="")
        self.enqueue(operation="operation:bytes-ok", description="中" * 1666)
        with self.assertRaises(DramaSynthesisError):
            self.enqueue(operation="operation:bytes-bad", description="中" * 1667)

    def test_youtube_operation_idempotent_and_conflict_safe(self):
        one = self.enqueue()
        two = self.enqueue()
        self.assertEqual(one["id"], two["id"])
        with self.assertRaises(DramaSynthesisError):
            self.enqueue(description="changed")

    def test_processing_and_unknown_block_replacement(self):
        row = self.enqueue(operation="operation:processing")
        conn = sqlite3.connect(self.store.db_path)
        conn.execute("UPDATE drama_youtube_publish SET status='processing',video_state='processing',video_id='video_123' WHERE id=?", (row["id"],))
        conn.commit(); conn.close()
        with self.assertRaises(DramaSynthesisError):
            self.enqueue(operation="operation:replacement")

    def test_lease_generation_rejects_stale_worker(self):
        row = self.enqueue(operation="operation:fence")
        first = self.store.claim_youtube("worker-a", "2000-01-01T00:00:00Z")
        second = self.store.claim_youtube("worker-b", "2999-01-01T00:00:00Z")
        self.assertEqual(second["id"], row["id"])
        with self.assertRaises(DramaSynthesisError):
            self.store.set_upload_session(row["id"], "https://upload.youtube.test/session", 10, worker_id="worker-a", lease_generation=first["lease_generation"])

    def test_engine_requires_processing_and_visibility_before_comment(self):
        row = self.enqueue(operation="operation:engine", comment="hello")
        conn = sqlite3.connect(self.store.db_path)
        conn.execute("UPDATE drama_youtube_publish SET status='submitted',video_state='submitted',video_id='video_123' WHERE id=?", (row["id"],))
        conn.commit(); conn.close()
        client = FakePublishClient(["processing", "published"])
        engine = YouTubePublishEngine(self.store, FakeCredentialRepo(), client, work_root=self.root / "work", allowed_source_hosts=("example.test",))
        first = engine.run_once("worker")
        self.assertEqual(first["status"], "processing")
        self.assertEqual(client.comments, 0)
        second = engine.run_once("worker")
        self.assertEqual(second["status"], "published")
        self.assertEqual(client.comments, 1)
        final = self.store.youtube_task(row["id"])
        self.assertEqual((final["video_state"], final["comment_status"]), ("published", "published"))

    def test_lost_upload_response_is_status_queried_before_state_change(self):
        row = self.enqueue(operation="operation:recover-upload")
        work = self.root / "work" / ("task-%d" % row["id"])
        work.mkdir(parents=True)
        (work / "source.mp4").write_bytes(b"fake-video")
        client = FakeRecoveringUploadClient()
        engine = YouTubePublishEngine(self.store, FakeCredentialRepo(), client, work_root=self.root / "work", allowed_source_hosts=("example.test",))
        probe = SimpleNamespace(stdout='{"format":{"duration":"1.25"}}')
        with mock.patch("features.drama_synthesis.youtube.subprocess.run", return_value=probe):
            result = engine.run_once("worker")
        self.assertEqual(result["status"], "submitted")
        self.assertEqual((client.begins, client.uploads, client.queries), (1, 1, 1))
        task = self.store.youtube_task(row["id"])
        self.assertEqual((task["video_state"], task["video_id"]), ("submitted", "video_recovered"))

    def test_upload_query_expired_is_unknown_and_cleans_terminal_source(self):
        row = self.enqueue(operation="operation:expired-upload")
        work = self.root / "work" / ("task-%d" % row["id"])
        work.mkdir(parents=True)
        (work / "source.mp4").write_bytes(b"fake-video")
        client = FakeRecoveringUploadClient("expired")
        engine = YouTubePublishEngine(self.store, FakeCredentialRepo(), client, work_root=self.root / "work", allowed_source_hosts=("example.test",))
        probe = SimpleNamespace(stdout='{"format":{"duration":"1.25"}}')
        with mock.patch("features.drama_synthesis.youtube.subprocess.run", return_value=probe):
            result = engine.run_once("worker")
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(work.exists())
        self.assertEqual(client.queries, 1)

    def test_published_video_enqueues_idempotent_sync_outbox(self):
        row = self.enqueue(operation="operation:outbox", comment="")
        claim = self.store.claim_youtube("worker", "2999-01-01T00:00:00Z")
        self.store.video_submitted(row["id"], "video_456", worker_id="worker", lease_generation=claim["lease_generation"])
        claim = self.store.claim_youtube("worker", "2999-01-01T00:00:00Z")
        self.store.video_published(row["id"], "video_456", worker_id="worker", lease_generation=claim["lease_generation"])
        conn = sqlite3.connect(self.store.db_path)
        rows = list(conn.execute("SELECT entity_kind,external_id,payload_json FROM drama_youtube_sync_outbox ORDER BY entity_kind"))
        conn.close()
        self.assertEqual([item[0] for item in rows], ["publish_log", "video"])
        self.assertEqual(rows[0][1], str(row["id"]))
        self.assertEqual(rows[1][1], "video_456")
        publish_payload = json.loads(rows[0][2])
        video_payload = json.loads(rows[1][2])
        self.assertEqual(publish_payload, video_payload)
        self.assertEqual((publish_payload["publish_id"], publish_payload["video_id"]), (row["id"], "video_456"))
        validate_entity_payload("publish_log", str(row["id"]), publish_payload)
        validate_entity_payload("video", "video_456", video_payload)

    def test_late_comment_outbox_reopens_independent_sync_status(self):
        row = self.enqueue(operation="operation:late-comment", comment="hello")
        claim = self.store.claim_youtube("worker", "2999-01-01T00:00:00Z")
        self.store.video_submitted(row["id"], "video_late", worker_id="worker", lease_generation=claim["lease_generation"])
        claim = self.store.claim_youtube("worker", "2999-01-01T00:00:00Z")
        published = self.store.video_published(row["id"], "video_late", worker_id="worker", lease_generation=claim["lease_generation"])
        writer = UnifiedYouTubeWriter(lambda action, *_args: {"found": False} if action == "select" else {"idempotent_success": True})
        self.assertEqual(run_sync_outbox_once(self.store, writer, "sync-1")["status"], "synced")
        self.assertEqual(run_sync_outbox_once(self.store, writer, "sync-2")["status"], "synced")
        self.assertEqual(self.store.youtube_task(row["id"])["sync_status"], "synced")
        self.store.mark_comment_attempt(row["id"], worker_id="worker", lease_generation=published["lease_generation"])
        self.store.comment_published(row["id"], "comment_late", worker_id="worker", lease_generation=published["lease_generation"])
        self.assertEqual(self.store.youtube_task(row["id"])["sync_status"], "pending")

    def test_outbox_finish_is_fenced_and_updates_parent(self):
        self.test_published_video_enqueues_idempotent_sync_outbox()
        item = self.store.claim_youtube_sync("sync-a", "2000-01-01T00:00:00Z")
        reclaimed = self.store.claim_youtube_sync("sync-b", "2999-01-01T00:00:00Z")
        with self.assertRaises(DramaSynthesisError):
            self.store.finish_youtube_sync(item["id"], worker_id="sync-a", lease_generation=item["lease_generation"], success=True)
        self.store.finish_youtube_sync(reclaimed["id"], worker_id="sync-b", lease_generation=reclaimed["lease_generation"], success=True)

    def test_unified_writer_is_whitelisted_idempotent_and_fail_closed(self):
        video = unified_video_payload()
        with self.assertRaises(DramaSynthesisError):
            UnifiedYouTubeWriter(None).sync("video", "video_1", video)
        calls = []
        writer = UnifiedYouTubeWriter(lambda action, table, external_id, payload: calls.append((action, table, external_id, payload)) or ({"found": False} if action == "select" else {"idempotent_success": True}))
        writer.sync("video", "video_1", video)
        self.assertEqual([call[0] for call in calls], ["select", "insert"])
        self.assertEqual(calls[1][1:3], ("ads_youtube_videos", "video_1"))
        rejected = (
            ("video", "video_1", dict(video, secret="reject")),
            ("video", "video_1", {"video_id": "video_1"}),
            ("video", "video_1", dict(video, publish_id="1")),
            ("video", "video_other", video),
            ("comment", "comment_other", unified_comment_payload()),
            ("publish_log", "2", video),
        )
        for entity_kind, external_id, payload in rejected:
            with self.assertRaises(DramaSynthesisError):
                writer.sync(entity_kind, external_id, payload)
        self.assertEqual(len(calls), 2)
        writer.sync("comment", "comment_1", unified_comment_payload())
        writer.sync("publish_log", "1", video)
        self.assertEqual(
            [(calls[index][1], calls[index][2]) for index in (3, 5)],
            [("ads_youtube_comments", "comment_1"), ("ads_youtube_publish_log", "1")],
        )
        with self.assertRaises(DramaSynthesisError):
            validate_controlled_operation("delete", "ads_youtube_videos")

    def test_unified_rpc_factory_config_auth_redirect_and_unknown_fail_closed(self):
        credential = self.root / "unified.token"
        credential.write_text("fake-server-credential-" + "x" * 32, encoding="utf-8")
        if os.name != "nt": credential.chmod(0o600)
        env = {
            "DRAMA_YOUTUBE_UNIFIED_RPC_URL": "http://127.0.0.1:18837/v1/youtube-sync",
            "DRAMA_YOUTUBE_UNIFIED_RPC_CREDENTIAL_FILE": str(credential),
            "DRAMA_YOUTUBE_UNIFIED_RPC_TIMEOUT": "5",
        }
        session = FakeRPCSession([FakeResponse(payload={"found": False}), FakeResponse(payload={"idempotent_success": True})])
        writer = build_unified_youtube_writer_from_env(env, session_factory=lambda: session)
        writer.sync("video", "video_rpc", unified_video_payload(video_id="video_rpc"))
        self.assertEqual([call[1]["json"]["action"] for call in session.posts], ["select", "insert"])
        self.assertTrue(all(call[1]["allow_redirects"] is False for call in session.posts))
        self.assertTrue(all(call[1]["headers"]["Authorization"].startswith("Bearer ") for call in session.posts))
        with self.assertRaises(DramaSynthesisError):
            build_unified_youtube_writer_from_env({}).sync("video", "video_missing", unified_video_payload(video_id="video_missing"))
        with self.assertRaises(DramaSynthesisError) as partial:
            build_unified_youtube_writer_from_env({"DRAMA_YOUTUBE_UNIFIED_RPC_URL": env["DRAMA_YOUTUBE_UNIFIED_RPC_URL"]})
        self.assertEqual(partial.exception.code, "youtube_sync_not_configured")
        credential.write_text("too-short-token", encoding="utf-8")
        with self.assertRaises(DramaSynthesisError):
            build_unified_youtube_writer_from_env(env)
        credential.write_text("fake-server-credential-" + "x" * 32, encoding="utf-8")
        for response, expected in ((FakeResponse(status=401), "youtube_sync_auth_failed"), (FakeResponse(status=302), "youtube_sync_redirect_denied"), (FakeResponse(payload={}), "youtube_sync_response_invalid")):
            failed = build_unified_youtube_writer_from_env(env, session_factory=lambda response=response: FakeRPCSession([response]))
            with self.assertRaises(DramaSynthesisError) as raised:
                failed.sync("video", "video_failed", unified_video_payload(video_id="video_failed"))
            self.assertEqual(raised.exception.code, expected)
        with self.assertRaises(DramaSynthesisError):
            ControlledRPCExecutor("http://127.0.0.1:18837/v1/youtube-sync", str(credential))("delete", "ads_youtube_videos", "video", {})
        with self.assertRaises(DramaSynthesisError):
            ControlledRPCExecutor("http://127.0.0.1:18836/v1/youtube-sync", str(credential))

    def test_sync_outbox_invalid_payload_is_fenced_failed_without_raw_data(self):
        writer_calls = []
        writer = UnifiedYouTubeWriter(lambda *args: writer_calls.append(args) or {"found": False})
        cases = (
            ('{"secret":', "youtube_sync_payload_invalid"),
            ('["not-an-object"]', "youtube_sync_payload_invalid"),
            (json.dumps(dict(unified_video_payload(video_id="video_fake"), secret="must-reject")), "youtube_sync_contract_invalid"),
        )
        for payload_json, expected_code in cases:
            store = FakeSyncStore(payload_json)
            result = run_sync_outbox_once(store, writer, "sync-worker")
            self.assertEqual((result["status"], result["code"]), ("failed", expected_code))
            self.assertEqual(len(store.finish_calls), 1)
            outbox_id, finish = store.finish_calls[0]
            self.assertEqual(outbox_id, 91)
            self.assertEqual((finish["worker_id"], finish["lease_generation"], finish["success"]), ("sync-worker", 4, False))
            self.assertEqual(finish["code"], expected_code)
            self.assertNotIn("secret", finish["message"].lower())
        self.assertEqual(writer_calls, [])

    def test_channel_repository_uses_channel_status_and_decimal_ids(self):
        seen = []
        token = json.dumps({"refresh_token": "r", "scope": [UPLOAD, READONLY]}).encode("utf-8").hex()
        credentials = json.dumps({"installed": {"client_id": "c", "client_secret": "s"}}).encode("utf-8").hex()
        repo = YouTubeCredentialRepository(lambda sql: seen.append(sql) or [("1", CHANNEL, "Current", "1", "2", token, credentials)], identity_probe=lambda _row: True)
        self.assertEqual(len(repo.list_for_app("1479")), 1)
        self.assertIn("ch.channel_status", seen[0])
        self.assertIn("HEX(COALESCE(a.account_token,''))", seen[0])
        self.assertIn("HEX(COALESCE(a.account_credentials,''))", seen[0])
        with self.assertRaises(DramaSynthesisError):
            repo.list_for_app("1'\\ OR 1=1")

    def test_channel_list_hides_upload_only_and_status_two(self):
        upload_only = json.dumps({"refresh_token": "r", "scope": [UPLOAD]}).encode("utf-8").hex()
        complete = json.dumps({"refresh_token": "r", "scope": [UPLOAD, READONLY]}).encode("utf-8").hex()
        creds = json.dumps({"installed": {"client_id": "c", "client_secret": "s"}}).encode("utf-8").hex()
        rows = [("1", CHANNEL, "upload-only", "1", "2", upload_only, creds), ("3", CHANNEL[:-1] + "B", "disabled", "2", "4", complete, creds)]
        self.assertEqual(YouTubeCredentialRepository(lambda _sql: rows, identity_probe=lambda _row: True).list_for_app("1479"), [])

    def test_channel_list_requires_runtime_refresh_identity_and_hides_failures(self):
        token = json.dumps({"refresh_token": "r", "scope": [UPLOAD, READONLY]}).encode("utf-8").hex()
        creds = json.dumps({"installed": {"client_id": "c", "client_secret": "s"}}).encode("utf-8").hex()
        other = CHANNEL[:-1] + "B"
        rows = [("1", CHANNEL, "verified", "1", "2", token, creds), ("3", other, "hidden", "1", "4", token, creds)]
        refreshes = []
        identity_reads = []
        mutations = []
        def probe(row):
            refreshes.append(row.account_id)
            identity_reads.append(row.channel_id)
            return row.channel_id == CHANNEL
        items = YouTubeCredentialRepository(lambda _sql: rows, identity_probe=probe).list_for_app("1479")
        self.assertEqual([item["channel_id"] for item in items], [CHANNEL])
        self.assertEqual(refreshes, ["2", "4"])
        self.assertEqual(identity_reads, [CHANNEL, other])
        self.assertEqual(mutations, [])
        self.assertEqual(YouTubeCredentialRepository(lambda _sql: rows).list_for_app("1479"), [])

    def test_identity_probe_empty_multiple_and_mismatch_fail_closed(self):
        payloads = [{"items": []}, {"items": [{"id": CHANNEL}, {"id": CHANNEL}]}, {"items": [{"id": CHANNEL[:-1] + "B"}]}]
        for payload in payloads:
            session = FakeSession([FakeResponse(payload=payload)])
            with self.assertRaises(Exception):
                YouTubeHTTPClient(session_factory=lambda: session).verify_channel_identity("token", CHANNEL)
            self.assertEqual(session.gets, 1)

    def test_identity_probe_transient_is_retryable_before_any_mutation(self):
        session = FakeSession([FakeResponse(status=503)])
        with self.assertRaises(YouTubeHTTPError) as raised:
            YouTubeHTTPClient(session_factory=lambda: session).verify_channel_identity("token", CHANNEL)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(session.gets, 1)

    def test_video_status_maps_processing_published_and_failure(self):
        payloads = [
            {"items": [{"status": {"uploadStatus": "uploaded", "privacyStatus": "public"}}]},
            {"items": [{"status": {"uploadStatus": "processed", "privacyStatus": "public"}}]},
            {"items": [{"status": {"uploadStatus": "rejected", "privacyStatus": "public"}}]},
        ]
        session = FakeSession([FakeResponse(payload=item) for item in payloads])
        client = YouTubeHTTPClient(session_factory=lambda: session)
        self.assertEqual(client.video_status("token", "video_123")["state"], "processing")
        self.assertEqual(client.video_status("token", "video_123")["state"], "published")
        self.assertEqual(client.video_status("token", "video_123")["state"], "failed")

    def test_retry_comment_never_reuploads_video(self):
        row = self.enqueue(operation="operation:comment-retry", comment="hello")
        conn = sqlite3.connect(self.store.db_path)
        conn.execute("UPDATE drama_youtube_publish SET status='published',video_state='published',video_id='video_123',comment_status='failed' WHERE id=?", (row["id"],))
        conn.commit(); conn.close()
        self.store.retry_youtube_comment(row["id"])
        client = FakePublishClient([])
        result = YouTubePublishEngine(self.store, FakeCredentialRepo(), client, work_root=self.root / "work", allowed_source_hosts=("example.test",)).run_once("worker")
        self.assertEqual(result["status"], "published")
        self.assertEqual(client.comments, 1)

    def test_unknown_outcome_blocks_replacement(self):
        row = self.enqueue(operation="operation:unknown")
        claim = self.store.claim_youtube("worker", "2999-01-01T00:00:00Z")
        self.store.fail_youtube(row["id"], worker_id="worker", lease_generation=claim["lease_generation"], phase="video", code="unknown", message="unknown", unknown=True)
        with self.assertRaises(DramaSynthesisError):
            self.enqueue(operation="operation:unknown-replacement")

    def test_published_duplicate_requires_second_confirmation(self):
        row = self.enqueue(operation="operation:published")
        conn = sqlite3.connect(self.store.db_path)
        conn.execute("UPDATE drama_youtube_publish SET status='published',video_state='published',video_id='video_123' WHERE id=?", (row["id"],))
        conn.commit(); conn.close()
        with self.assertRaises(DramaSynthesisError):
            self.enqueue(operation="operation:duplicate")
        duplicate = self.enqueue(operation="operation:duplicate-confirmed", confirmed=True)
        self.assertEqual(duplicate["status"], "queued")

    def test_fake_outbox_worker_success_and_missing_adapter_failure(self):
        self.test_published_video_enqueues_idempotent_sync_outbox()
        ok = UnifiedYouTubeWriter(lambda action, *_args: {"found": False} if action == "select" else {"idempotent_success": True})
        self.assertEqual(run_sync_outbox_once(self.store, ok, "sync-ok")["status"], "synced")
        self.assertEqual(run_sync_outbox_once(self.store, UnifiedYouTubeWriter(None), "sync-fail")["status"], "failed")

    def test_ui_contract_has_source_selection_bytes_macro_and_clipboard_fallback(self):
        for name in ("drama-synthesis.html", "index.html"):
            text = (ROOT / "static" / name).read_text(encoding="utf-8")
            self.assertIn('id="randomTemplateSource"', text)
            self.assertIn("source: els.randomTemplateSource.value", text)
            self.assertIn("random_template_video: els.outRandomTemplate.checked", text)
            self.assertNotRegex(text, r"(?m)^\s*random_template:\s*els\.outRandomTemplate\.checked\s*,?\s*$")
            self.assertIn("new TextEncoder().encode(description).length > 5000", text)
            self.assertIn("document.execCommand(\"copy\")", text)
            self.assertIn('id="materialPickerModal"', text)
            self.assertNotIn("window.prompt", text)
            self.assertIn("retryYoutubeComment", text)
            self.assertIn("chooseMaterial(job, false)", text)
            self.assertIn("无可用视频产物", text)
            self.assertIn("随机模板配方审计", text)
            self.assertIn('card.dataset.recipeAudit = "true"', text)
            self.assertIn("line.textContent =", text)
            self.assertNotIn('join("<br />")', text)
            self.assertNotIn("const [job, channels] = await Promise.all", text)
            self.assertNotIn("random_template_video_url", text)
        browser_spec = (ROOT / "scripts/drama_synthesis_browser.spec.js").read_text(encoding="utf-8")
        self.assertIn('require.resolve("@playwright/test"', browser_spec)
        self.assertIn("recipe audit renders hostile values", browser_spec)

    def test_live_feature_guard_requires_shared_browser_runtime(self):
        manifest = json.loads((ROOT / "deploy/live_feature_guard.json").read_text(encoding="utf-8"))
        feature = next(item for item in manifest["features"] if item["id"] == "drama_synthesis")
        runtime = next(item for item in feature["required"] if item["path"] == "static/drama-job-runtime.js")
        self.assertEqual(runtime["public_path"], "drama-job-runtime.js")
        self.assertIn("root.DramaJobRuntime", runtime["contains"])

    def test_app_exposes_exact_six_routes_and_macro_precondition(self):
        text = (ROOT / "app.py").read_text(encoding="utf-8")
        for fragment in ("/api/drama-material/random-template-catalog", "short-links", "/api/drama-material/youtube/channels", "youtube-publishes", "retry-comment"):
            self.assertIn(fragment, text)
        self.assertIn('if "{{url}}" in description_template:', text)
        self.assertIn("description_template.replace", text)
        self.assertIn("YOUTUBE_LIVE_ENABLED", text)
        self.assertIn("token = client.refresh_access_token(credential)", text)
        self.assertIn("client.verify_channel_identity(token, credential.channel_id)", text)
        self.assertIn("material_kind, _source_url = drama_youtube_source", text)
        self.assertNotIn("page.dramabuzzs.com/s2l", text)
        self.assertNotIn("/youtube-channels", text)

    def test_hk_units_release_ports_namespace_and_gpu_surface(self):
        worker = (ROOT / "deploy/drama-synthesis-gpu-worker.service").read_text(encoding="utf-8")
        tunnel = (ROOT / "deploy/drama-synthesis-gpu-tunnel.service").read_text(encoding="utf-8")
        nginx = (ROOT / "deploy/drama-youtube-s2l.nginx.conf").read_text(encoding="utf-8")
        gpu = (ROOT / "scripts/drama_synthesis_gpu_worker.py").read_text(encoding="utf-8")
        self.assertIn("/data/drama-synthesis-gpu/current", worker)
        self.assertIn("DRAMA_GPU_PORT=8787", worker)
        self.assertIn("127.0.0.1:18788:127.0.0.1:8787", tunnel)
        self.assertIn("/s2l/youtube/", nginx)
        self.assertNotIn("youtube-publishes", gpu)
        self.assertNotIn("refresh_token", gpu)
        publish_worker = (ROOT / "scripts/drama_youtube_publish_worker.py").read_text(encoding="utf-8")
        self.assertIn("build_unified_youtube_writer_from_env()", publish_worker)
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("bool(DRAMA_SHORT_LINK_ROOT) != bool(DRAMA_SHORT_LINK_OWNER)", app_source)
        writer_service = (ROOT / "deploy/drama-youtube-unified-writer.service").read_text(encoding="utf-8")
        writer_env = (ROOT / "deploy/drama-youtube-unified-writer.env.example").read_text(encoding="utf-8")
        short_root = (ROOT / "deploy/configure_drama_youtube_short_link_root.sh").read_text(encoding="utf-8")
        self.assertIn("DRAMA_YOUTUBE_UNIFIED_RPC_PORT=18837", writer_env)
        self.assertNotIn("18836", writer_service + writer_env)
        self.assertIn("drama-youtube:drama-youtube", writer_env)
        self.assertIn("default:user:nginx:r--", short_root)
        deploy_doc = (ROOT / "doc/049.drama-synthesis-upgrade/deploy.md").read_text(encoding="utf-8")
        migration_doc = (ROOT / "doc/049.drama-synthesis-upgrade/migration.md").read_text(encoding="utf-8")
        for document in (deploy_doc, migration_doc):
            self.assertIn("ads-ai-new-tables-20260827.md", document)
        current = (ROOT / "doc/049.drama-synthesis-upgrade/ads-ai-new-tables-20260827.md").read_text(encoding="utf-8")
        for fragment in ("ads_ai", "bootstrap_drama_youtube_ads_ai.py", "drama-youtube-writer-preflight-v3",
                         "shared-existing-account", "application-table-allowlist", "db_least_privilege=false",
                         "63350", "63353", "SELECT/INSERT/UPDATE", "不复制", "原表"):
            self.assertIn(fragment, current)

    def test_gpu_worker_fake_http_contract_is_media_only(self):
        fake_app = SimpleNamespace(
            WORK_ROOT=str(self.root / "gpu-work"),
            cached_gpu_video_result=lambda _payload: None,
            strict_cached_gpu_video_result=lambda _payload: None,
            drama_random_template_catalog=lambda: {"version": 1, "count": 315},
            handle_gpu_video_render=lambda payload: {"ok": True, "recipe": payload["recipe"]},
            handle_gpu_video_cover=lambda payload: {"ok": True, "cover": payload["source_url"]},
        )
        original_app = sys.modules.get("app")
        sys.modules["app"] = fake_app
        try:
            path = ROOT / "scripts/drama_synthesis_gpu_worker.py"
            spec = importlib.util.spec_from_file_location("drama_gpu_contract_test", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            if original_app is None:
                sys.modules.pop("app", None)
            else:
                sys.modules["app"] = original_app
        server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            with mock.patch.dict(os.environ, {"GPU_VIDEO_WORKER_TOKEN": "fake-token"}, clear=False):
                health = json.loads(urlopen(base + "/healthz", timeout=2).read())
                self.assertEqual(health, {"ok": True, "role": "media-only"})
                request = Request(base + "/api/gpu-video/render", data=json.dumps({"job_id": JOB_ID, "recipe": "frozen"}).encode(), headers={"Authorization": "Bearer fake-token", "Content-Type": "application/json"}, method="POST")
                self.assertEqual(json.loads(urlopen(request, timeout=2).read())["recipe"], "frozen")
                with self.assertRaises(HTTPError) as denied:
                    urlopen(base + "/api/gpu-video/youtube", timeout=2)
                self.assertEqual(denied.exception.code, 404)
                denied.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            if module.RUNTIME is not None:
                self.assertTrue(module.RUNTIME.close(timeout=3))

    def test_gpu_uses_frozen_source_and_hides_internal_intermediate(self):
        text = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('random_source_kind = str((random_recipe or {}).get("source")', text)
        self.assertIn('source=no_bgm_output_path if random_source_kind == "no_bgm_video" else output_path', text)
        self.assertIn('if publish_no_bgm else ""', text)

    def test_new_outputs_default_unchecked_and_legacy_fields_normalize_default(self):
        text = (ROOT / "app.py").read_text(encoding="utf-8")
        for key in ("concat_video", "no_bgm_video", "cover_16x9"):
            self.assertIn(f'outputs.get("{key}", False)', text)
        self.assertIn('"random_template_video": bool(outputs.get("random_template_video", outputs.get("random_template", False)))', text)
        self.assertIn('"cover_template": "default"', text)
        self.assertIn('"naming_rule": "default"', text)
        self.assertIn('item["output_random_template_url"]', text)
        self.assertIn('item["random_template_recipe"]', text)


class AsyncAppIntegrationTests(unittest.TestCase):
    """Exercise the real app glue without importing its production side effects."""

    @classmethod
    def setUpClass(cls):
        cls.app_tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))

    def load(self, *names, **values):
        from features.drama_synthesis import remote_client
        from features.drama_synthesis.app_support import ObservationStop, remote_display
        import logging
        import requests
        env = dict(threading=threading, os=os, json=json, logging=logging,
                   DramaObservationStop=ObservationStop, drama_remote_display=remote_display,
                   drama_remote_client=remote_client, requests=requests,
                   DRAMA_GPU_ASYNC_ENABLED=True, JOB_DB_PATH="unused.sqlite3",
                   GPU_VIDEO_WORKER_URL="http://127.0.0.1:8787", GPU_VIDEO_WORKER_TOKEN="test-only",
                   gpu_video_worker_enabled=lambda: True, normalize_outputs=lambda value: value)
        env.update(values)
        nodes = [n for n in self.app_tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        self.assertEqual(len(nodes), len(names))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), env)
        return env

    def test_native_render_stage_has_media_time_ratio_and_no_global_weight(self):
        from features.drama_synthesis.app_support import remote_display
        actual = remote_display({"status": "running", "stage": "rendering_random",
                                 "metrics": {"out_time_seconds": 60, "duration_seconds": 120}})
        self.assertEqual(actual["stage_percent"], 50.0)
        self.assertIn("1.0/2.0", actual["detail"])
        self.assertEqual(remote_display({"stage": "concatenating"})["stage_percent"], None)

    def test_normalizing_stage_keeps_live_download_metrics_visible(self):
        from features.drama_synthesis.app_support import remote_display
        view = remote_display({
            "status": "running", "stage": "normalizing",
            "metrics": {
                "completed_episodes": 3, "total_episodes": 8,
                "downloaded_bytes": 12_000_000, "total_bytes": 40_000_000,
                "bytes_per_second": 2_500_000,
                "normalized_episodes": 2, "total_segments": 8,
            },
        })
        self.assertEqual(view["stage_percent"], 25.0)
        self.assertIn("已下载 3/8 集", view["detail"])
        self.assertIn("12.0 MB / 40.0 MB", view["detail"])
        self.assertIn("2.50 MB/s", view["detail"])
        self.assertIn("已处理 2/8 段", view["detail"])

    def test_bad_metrics_and_reconnection_never_create_fake_progress(self):
        from features.drama_synthesis.app_support import remote_display
        view = remote_display({"status": "running", "stage": "rendering",
                               "metrics": {"out_time_seconds": float("nan"), "duration_seconds": 120},
                               "connection_state": "reconnecting"})
        self.assertIsNone(view["stage_percent"])
        self.assertEqual(view["stage_label"], "连接恢复中")

    def test_observer_parent_stop_is_not_lost(self):
        from features.drama_synthesis.app_support import ObservationStop
        parent = threading.Event()
        observer = ObservationStop(parent)
        self.assertFalse(observer.wait(0))
        parent.set()
        self.assertTrue(observer.wait(0))

    def test_cover_failure_joins_old_observer_before_retry_can_start(self):
        job = {"job_id": JOB_ID}
        stopped = []
        executor = mock.Mock()
        def run(current):
            current["_gpu_observer_executor"] = executor
            executor.shutdown.side_effect = lambda **kw: stopped.append(current["_remote_stop_event"].is_set())
            raise ValueError("cover service unavailable")
        env = self.load("process_job", _process_job_observed=run)
        with self.assertRaisesRegex(ValueError, "cover service"):
            env["process_job"](job)
        self.assertEqual(stopped, [True])
        executor.shutdown.assert_called_once_with(wait=True)
        self.assertNotIn("_remote_stop_event", job)
        self.assertNotIn("_gpu_observer_executor", job)

    def gpu_env(self, *, completed=False):
        payload = {"job_id": JOB_ID, "await_cover_16x9": True, "outputs": {"concat_video": True},
                   "episodes": [{"episode_number": 1, "episode_url": "https://example.test/one.mp4"}]}
        runtime = SimpleNamespace(
            get_remote_payload=mock.Mock(return_value=payload),
            remember_remote_submission=mock.Mock(side_effect=lambda db, job, saved, lease: saved),
            get_remote_status=mock.Mock(return_value={"job_id": JOB_ID, "generation": 1}),
            get_remote_resume_intent=mock.Mock(return_value=None), record_remote_status=mock.Mock())
        env = self.load("call_gpu_video_worker", drama_cpu_runtime=runtime,
                        submit_gpu_video_cover=mock.Mock(), set_job_progress=mock.Mock())
        job = {"job_id": JOB_ID, "cover_16x9_url": "https://example.test/cover.jpg"}
        return env, job, runtime

    def test_lost_cover_callback_retries_without_failing_or_resubmitting_media(self):
        import requests
        env, job, runtime = self.gpu_env()
        env["submit_gpu_video_cover"].side_effect = [requests.Timeout(), {"ok": True}]
        snapshots = []
        runtime.record_remote_status.side_effect = lambda db, jid, status, lease: snapshots.append(status)
        remote = env["drama_remote_client"]
        def wait(*args, **kwargs):
            kwargs["on_status"]({"status": "running", "stage": "waiting_cover", "connection_state": "connected"})
            kwargs["on_status"]({"status": "running", "stage": "waiting_cover", "connection_state": "connected"})
            return {"job_id": JOB_ID}
        with mock.patch.object(remote, "wait_for_gpu_job", side_effect=wait) as poll:
            self.assertEqual(env["call_gpu_video_worker"](job, [], {}), {"job_id": JOB_ID})
        self.assertEqual(poll.call_count, 1)
        self.assertEqual(env["submit_gpu_video_cover"].call_count, 2)
        self.assertEqual(snapshots[0]["connection_state"], "reconnecting")
        self.assertEqual(snapshots[1]["connection_state"], "connected")

    def test_completed_result_does_not_depend_on_cover_callback_availability(self):
        env, job, _ = self.gpu_env()
        def wait(*args, **kwargs):
            kwargs["on_status"]({"status": "completed", "stage": "completed", "connection_state": "connected"})
            return {"job_id": JOB_ID}
        with mock.patch.object(env["drama_remote_client"], "wait_for_gpu_job", side_effect=wait):
            env["call_gpu_video_worker"](job, [], {})
        env["submit_gpu_video_cover"].assert_not_called()

    def test_new_submission_freezes_cdn_route_before_persisting_payload(self):
        from features.drama_synthesis.media_pipeline import freeze_episode_download_route
        env, job, runtime = self.gpu_env()
        runtime.get_remote_payload.return_value = None
        env["freeze_episode_download_route"] = freeze_episode_download_route
        source = "https://img.tianmai.cn/resource/13218/19_example.mp4"
        with mock.patch.dict(os.environ, {"DRAMA_GPU_TIANMAI_CDN": "international"}), \
                mock.patch.object(env["drama_remote_client"], "wait_for_gpu_job", return_value={"job_id": JOB_ID}):
            env["call_gpu_video_worker"](job, [{"episode_number": 19, "episode_url": source}], {"concat_video": True})
        frozen = runtime.remember_remote_submission.call_args.args[2]["episodes"][0]
        self.assertEqual(frozen["episode_url"], source)
        self.assertEqual(frozen["download_route"]["primary_url"], source.replace("img.tianmai.cn", "accelerate.tianmai.cn"))
        self.assertEqual(frozen["download_route"]["fallback_url"], source)

    def test_saved_submission_does_not_change_when_cdn_configuration_changes(self):
        env, job, runtime = self.gpu_env()
        old_payload = runtime.get_remote_payload.return_value
        env["freeze_episode_download_route"] = mock.Mock(side_effect=AssertionError("must use frozen payload"))
        with mock.patch.dict(os.environ, {"DRAMA_GPU_TIANMAI_CDN": "international"}), \
                mock.patch.object(env["drama_remote_client"], "wait_for_gpu_job", return_value={"job_id": JOB_ID}) as wait:
            env["call_gpu_video_worker"](job, [{"episode_number": 99, "episode_url": "https://img.tianmai.cn/resource/1/other.mp4"}], {})
        self.assertIs(wait.call_args.args[2], old_payload)
        self.assertNotIn("download_route", old_payload["episodes"][0])
        env["freeze_episode_download_route"].assert_not_called()

    def test_async_upload_uses_durable_checkpoint_before_public_head(self):
        import contextlib
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "video.mp4"
            path.write_bytes(b"complete media fixture")
            runtime = SimpleNamespace(capture_context=lambda: SimpleNamespace(job_id=JOB_ID),
                                      use_context=lambda context: contextlib.nullcontext(), emit_progress=mock.Mock())
            http = SimpleNamespace(head=mock.Mock(side_effect=AssertionError("unverified public HEAD must not bypass ledger")))
            client = object()
            client_factory = mock.Mock(return_value=client)
            env = self.load("upload_file_to_cos", cos_enabled=lambda: True, file_ready=lambda _: True,
                            build_cos_object_key=lambda _: "isolated/video.mp4", build_cos_url=lambda _: "https://example.test/video.mp4",
                            drama_async_runtime=runtime, requests=http, hashlib=hashlib, WORK_ROOT=root,
                            PUBLIC_ROOT=str(Path(root) / "public"), re=__import__("re"),
                            drama_gpu_cache=SimpleNamespace(ARTIFACT_FILENAMES={}),
                            get_cos_client=client_factory, COS_UPLOAD_TIMEOUT=120, COS_MULTIPART_TIMEOUT=900,
                            COS_BUCKET="test-bucket", guess_content_type=lambda _: "video/mp4")
            with mock.patch("features.drama_synthesis.cos_upload.resume_upload") as upload:
                self.assertEqual(env["upload_file_to_cos"](str(path)), "https://example.test/video.mp4")
            call = upload.call_args
            self.assertIs(call.args[0], client)
            self.assertEqual(call.kwargs["path"], str(path))
            self.assertEqual(Path(call.kwargs["checkpoint_path"]).parent, Path(root) / ".runtime" / "uploads" / JOB_ID)
            self.assertEqual(call.kwargs["key"], "isolated/video.mp4")
            self.assertEqual(call.kwargs["acl"], "public-read")
            client_factory.assert_called_once_with(timeout=900, retry=0)
            http.head.assert_not_called()

    def test_async_upload_may_return_verified_receipt_without_changing_default_contract(self):
        import contextlib
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "video.mp4"
            path.write_bytes(b"complete media fixture")
            runtime = SimpleNamespace(capture_context=lambda: SimpleNamespace(job_id=JOB_ID),
                                      use_context=lambda context: contextlib.nullcontext(), emit_progress=mock.Mock())
            receipt = {
                "bucket": "test-bucket", "key": "isolated/video.mp4", "sha256": "a" * 64,
                "size_bytes": path.stat().st_size, "etag": '"etag"', "binding": "b" * 32,
            }
            env = self.load(
                "upload_file_to_cos", cos_enabled=lambda: True, file_ready=lambda _: True,
                build_cos_object_key=lambda _: "isolated/video.mp4",
                build_cos_url=lambda _: "https://example.test/video.mp4",
                drama_async_runtime=runtime, requests=SimpleNamespace(head=mock.Mock()),
                hashlib=hashlib, WORK_ROOT=root, PUBLIC_ROOT=str(Path(root) / "public"),
                re=__import__("re"), drama_gpu_cache=SimpleNamespace(ARTIFACT_FILENAMES={}),
                get_cos_client=mock.Mock(return_value=object()), COS_UPLOAD_TIMEOUT=120,
                COS_MULTIPART_TIMEOUT=900, COS_BUCKET="test-bucket",
                guess_content_type=lambda _: "video/mp4",
            )
            with mock.patch("features.drama_synthesis.cos_upload.resume_upload", return_value=receipt):
                self.assertEqual(env["upload_file_to_cos"](str(path)), "https://example.test/video.mp4")
                self.assertEqual(
                    env["upload_file_to_cos"](
                        str(path), return_receipt=True, checkpoint_job_id=JOB_ID,
                    ),
                    ("https://example.test/video.mp4", receipt),
                )

    def test_only_explicit_async_client_disables_sdk_internal_post_retries(self):
        config = object()
        constructor = mock.Mock()
        env = self.load("get_cos_client", cos_enabled=lambda: True, COS_UPLOAD_TIMEOUT=120,
                        COS_REGION="test", COS_SECRET_ID="fixture", COS_SECRET_KEY="fixture",
                        CosConfig=mock.Mock(return_value=config), CosS3Client=constructor)
        env["get_cos_client"]()
        constructor.assert_called_once_with(config)
        constructor.reset_mock()
        env["get_cos_client"](timeout=900, retry=0)
        constructor.assert_called_once_with(config, retry=0)

    def test_other_upload_callers_keep_existing_object_reuse(self):
        response = SimpleNamespace(status_code=200, headers={"Content-Length": "12"})
        http = SimpleNamespace(head=mock.Mock(return_value=response))
        env = self.load("upload_file_to_cos", cos_enabled=lambda: True, file_ready=lambda _: True,
                         build_cos_object_key=lambda _: "legacy.mp4", build_cos_url=lambda _: "https://example.test/legacy.mp4",
                         drama_async_runtime=SimpleNamespace(capture_context=lambda: None), requests=http,
                         PUBLIC_ROOT=str(ROOT / "unused-public"), re=__import__("re"),
                         drama_gpu_cache=SimpleNamespace(ARTIFACT_FILENAMES={}), hashlib=hashlib,
                         WORK_ROOT=str(ROOT / "unused-work"), COS_UPLOAD_TIMEOUT=120,
                         COS_MULTIPART_TIMEOUT=900, get_cos_client=mock.Mock(return_value=object()),
                         COS_BUCKET="test-bucket", guess_content_type=lambda _: "video/mp4")
        with mock.patch.object(os.path, "getsize", return_value=12), \
                mock.patch("features.drama_synthesis.cos_upload.resume_upload", return_value={"verified": True}) as upload:
            self.assertEqual(env["upload_file_to_cos"]("legacy.mp4"), "https://example.test/legacy.mp4")
            upload.assert_not_called()
            self.assertEqual(
                env["upload_file_to_cos"]("legacy.mp4", checkpoint_job_id=JOB_ID),
                "https://example.test/legacy.mp4",
            )
        upload.assert_called_once()
        http.head.assert_called_once()

    def test_stopped_observer_cannot_write_a_late_status(self):
        env, job, runtime = self.gpu_env()
        stop = threading.Event(); stop.set(); job["_remote_stop_event"] = stop
        def wait(*args, **kwargs):
            kwargs["on_status"]({"status": "running", "connection_state": "connected"})
        with mock.patch.object(env["drama_remote_client"], "wait_for_gpu_job", side_effect=wait):
            with self.assertRaises(env["drama_remote_client"].RemotePollingInterrupted):
                env["call_gpu_video_worker"](job, [], {})
        runtime.record_remote_status.assert_not_called()

    def test_async_retry_is_immediately_queued_without_url_based_completion(self):
        job = {"job_id": JOB_ID, "cover_16x9_url": "https://example.test/cover.jpg", "status": "failed"}
        env = self.load("resume_job_from_checkpoint",
                        drama_cpu_runtime=SimpleNamespace(get_remote_payload=lambda *a: {"job_id": JOB_ID}),
                        clear_job_deleted_marker=mock.Mock(), upsert_job_record=mock.Mock(), run_job_async=mock.Mock(),
                        reconcile_job_outputs_from_public_artifacts=mock.Mock(side_effect=AssertionError("unsafe reconciliation")),
                        selected_job_outputs_ready=mock.Mock(side_effect=AssertionError("unsafe URL shortcut")))
        env["resume_job_from_checkpoint"](job)
        self.assertEqual(job["status"], "queued")
        env["run_job_async"].assert_called_once_with(job)

    def test_async_records_cannot_bypass_manifest_verification_via_legacy_reconcile(self):
        env = self.load("reconcile_job_outputs_from_public_artifacts",
                        drama_cpu_runtime=SimpleNamespace(get_remote_payload=lambda *a: {"job_id": JOB_ID}),
                        public_artifact_ready=mock.Mock(side_effect=AssertionError("must not HEAD")))
        self.assertFalse(env["reconcile_job_outputs_from_public_artifacts"]({"job_id": JOB_ID}))

    def test_master_failure_is_not_hidden_by_last_known_gpu_stage(self):
        store = SimpleNamespace(recipe=lambda *a: None, short_links_for_job=lambda *a: [], youtube_tasks_for_job=lambda *a, **kw: [])
        env = self.load("decorate_drama_synthesis_job", DRAMA_SYNTHESIS_STORE=store,
                        drama_cpu_runtime=SimpleNamespace(get_remote_status=lambda *a: {"status": "running", "stage": "rendering"}))
        row = env["decorate_drama_synthesis_job"]({"job_id": JOB_ID, "status": "failed", "error_message": "记录消失，需要核对"})
        self.assertEqual(row["status_label"], "执行状态待核查")
        self.assertEqual(row["remote_progress"]["detail"], "记录消失，需要核对")
        self.assertIsNone(row["remote_progress"]["stage_percent"])

    def test_child_is_killed_and_reaped_if_pid_record_write_fails(self):
        import contextlib
        import subprocess
        import signal
        child = mock.Mock(pid=123456, returncode=None)
        child.poll.side_effect = lambda: child.returncode
        child.kill.side_effect = lambda: setattr(child, "returncode", -9)
        child.wait.side_effect = lambda: setattr(child, "returncode", -9)
        runtime = SimpleNamespace(capture_context=lambda: object(), process_launch=contextlib.nullcontext,
                                  record_process=mock.Mock(side_effect=OSError("ledger write failed")), clear_process=mock.Mock())
        fake_os = SimpleNamespace(name="nt", environ={})
        env = self.load("run_cmd", drama_async_runtime=runtime, subprocess=subprocess, signal=signal, os=fake_os)
        with mock.patch.object(subprocess, "Popen", return_value=child):
            with self.assertRaisesRegex(OSError, "ledger write"):
                env["run_cmd"](["fake", "argument"])
        child.kill.assert_called_once()
        child.wait.assert_called_once()
        runtime.clear_process.assert_called_once_with(child.pid)

    def test_notification_exception_does_not_undo_atomic_done(self):
        job = {"job_id": JOB_ID, "outputs": {"concat_video": True}}
        runtime = SimpleNamespace(atomic_complete_job=mock.Mock(return_value={"status": "done", "active_finished_at": "first"}))
        env = self.load("complete_async_gpu_job", drama_cpu_runtime=runtime,
                        notify_job_creator_on_completion=mock.Mock(side_effect=RuntimeError("notification unavailable")))
        with self.assertLogs(level="ERROR"):
            env["complete_async_gpu_job"](job, {"job_id": JOB_ID})
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["active_finished_at"], "first")

    def test_cover_callback_is_idempotent_and_cannot_replace_frozen_intro(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "cover.url"
            env = self.load("write_gpu_cover_url", tempfile=tempfile, DramaSynthesisError=DramaSynthesisError,
                            ensure_dir=lambda path: Path(path).mkdir(parents=True, exist_ok=True),
                            gpu_cover_url_marker_path=lambda path: str(Path(path) / "cover.url"))
            write = env["write_gpu_cover_url"]
            write(root, "https://example.test/first.jpg")
            initial = marker.stat().st_mtime_ns
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: write(root, "https://example.test/first.jpg"), range(8)))
            self.assertEqual(marker.stat().st_mtime_ns, initial)
            with self.assertRaises(DramaSynthesisError):
                write(root, "https://example.test/different.jpg")
            self.assertEqual(marker.read_text(), "https://example.test/first.jpg")
            self.assertEqual(list(Path(root).glob(".cover-binding-*")), [])

    def test_legacy_retry_keeps_its_existing_success_notification_contract(self):
        job = {"job_id": JOB_ID, "status": "failed", "completion_notified_at": "prior_failure", "completion_notification_error": "prior error"}
        env = self.load("resume_job_from_checkpoint", drama_cpu_runtime=SimpleNamespace(get_remote_payload=lambda *a: None),
                        clear_job_deleted_marker=mock.Mock(), reconcile_job_outputs_from_public_artifacts=lambda *a, **kw: True)
        env["resume_job_from_checkpoint"](job)
        self.assertEqual(job["completion_notified_at"], "")
        self.assertEqual(job["completion_notification_error"], "")


class MediaAcceptanceHardeningTests(unittest.TestCase):
    SHA = "a" * 40

    def setUp(self):
        from scripts import run_drama_media_acceptance as launcher
        from scripts import check_drama_media_resource_guard as guard
        self.launcher = launcher
        self.guard = guard

    def spec(self, operation="render", sample_kind="short", config="2c2t", trial="r1"):
        return self.launcher.build_spec(
            self.SHA, "accept01", sample_kind, config, operation, trial
        )

    def test_decode_rehashes_after_full_decode_before_writing_success(self):
        spec = self.spec("decode", "short", "4c2t", "r2")
        artifact = {"sha256": "e" * 64, "size_bytes": 999}
        identity = {"device": 1, "inode": 2, "size_bytes": 999,
                    "mtime_ns": 3, "nlink": 1}
        frozen = {"artifact": artifact, "artifact_identity": identity,
                  "benchmark_evidence_sha256": "f" * 64}
        outcome = {"elapsed_seconds": 12.5, "exit_code": 0,
                   "minimum_mem_available_bytes": 16 * 1024 ** 3}
        with mock.patch.object(self.launcher, "validate_render_result",
                               side_effect=[frozen, frozen]) as verify, \
             mock.patch.object(self.launcher, "run_fixed_child", return_value=outcome), \
             mock.patch.object(self.launcher, "write_exclusive_json") as write:
            result = self.launcher.run_decode(spec, 1009, 1010, 9, object())
        self.assertTrue(result["ok"])
        self.assertEqual(verify.call_count, 2)
        evidence = write.call_args.args[1]
        self.assertTrue(evidence["result_reverified_after_decode"])
        self.assertEqual(evidence["exit_code"], 0)
        self.assertEqual(evidence["render_unit"], self.spec(
            "render", "short", "4c2t", "r2").unit)
        self.assertEqual(evidence["result_identity_before"], identity)
        self.assertEqual(evidence["result_identity_after"], identity)

    def test_decode_same_size_replacement_never_writes_ok_evidence(self):
        spec = self.spec("decode", "short", "4c2t", "r2")
        before = {
            "artifact": {"sha256": "1" * 64, "size_bytes": 999},
            "artifact_identity": {"device": 1, "inode": 2, "size_bytes": 999,
                                  "mtime_ns": 3, "nlink": 1},
            "benchmark_evidence_sha256": "f" * 64,
        }
        after = {
            **before,
            "artifact": {"sha256": "2" * 64, "size_bytes": 999},
            "artifact_identity": {"device": 1, "inode": 4, "size_bytes": 999,
                                  "mtime_ns": 5, "nlink": 1},
        }
        with mock.patch.object(self.launcher, "validate_render_result",
                               side_effect=[before, after]), \
             mock.patch.object(self.launcher, "run_fixed_child", return_value={
                 "elapsed_seconds": 1, "exit_code": 0,
                 "minimum_mem_available_bytes": 16 * 1024 ** 3,
             }), mock.patch.object(self.launcher, "write_exclusive_json") as write, \
             self.assertRaises(self.launcher.LaunchFailure) as caught:
            self.launcher.run_decode(spec, 1009, 1010, 9, object())
        self.assertEqual(str(caught.exception), "decode_result_changed_during_decode")
        write.assert_not_called()

    def test_run_source_manifest_is_shared_and_rejects_same_size_replacement(self):
        first = self.spec("prepare-short")
        other = self.spec("render", "long", "4c4t", "r2")
        self.assertEqual(first.run_source_manifest_path, other.run_source_manifest_path)
        before = {"sha256": "1" * 64, "size_bytes": self.launcher.LONG_SOURCE_SIZE,
                  "device": 1, "inode": 2, "mtime_ns": 3, "nlink": 1}
        after = dict(before, sha256="2" * 64, inode=4, mtime_ns=5)
        frozen = self.launcher.run_source_record(first, before)
        with mock.patch.object(self.launcher, "fingerprint_fixed_input", return_value=before), \
             mock.patch.object(self.launcher.os, "lstat", side_effect=FileNotFoundError), \
             mock.patch.object(self.launcher, "write_exclusive_json") as write, \
             mock.patch.object(self.launcher, "read_run_source_record", return_value=frozen):
            self.assertEqual(self.launcher.ensure_run_source_frozen(first, 1009, 1010), frozen)
        write.assert_called_once_with(
            first.run_source_manifest_path, frozen, code="run_source_manifest_write_failed"
        )
        renderer = mock.Mock()
        with mock.patch.object(self.launcher, "fingerprint_fixed_input", return_value=after), \
             mock.patch.object(self.launcher.os, "lstat", return_value=SimpleNamespace()), \
             mock.patch.object(self.launcher, "read_run_source_record", return_value=frozen), \
             self.assertRaises(self.launcher.LaunchFailure) as caught:
            self.launcher.ensure_run_source_frozen(other, 1009, 1010)
            renderer()
        self.assertEqual(str(caught.exception), "fixed_long_source_changed")
        renderer.assert_not_called()

    def test_submit_timeout_is_durable_unknown_and_same_action_cannot_replay(self):
        import subprocess
        spec = self.spec("render")
        events = []
        with mock.patch.object(self.launcher, "existing_submission_guard",
                               side_effect=lambda _: events.append("replay-check")), \
             mock.patch.object(self.launcher, "ensure_public_apply_preflight",
                               side_effect=lambda _: events.append("preflight")), \
             mock.patch.object(self.launcher, "build_systemd_command", return_value=["fixed"]), \
             mock.patch.object(self.launcher, "write_submission_record",
                               side_effect=lambda _spec, state: events.append(state)), \
             mock.patch.object(self.launcher.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("fixed", 30)), \
             self.assertRaises(self.launcher.SubmissionUncertain) as caught:
            self.launcher.submit(spec)
        self.assertEqual(str(caught.exception), "media_acceptance_submit_outcome_unknown")
        self.assertEqual(events, ["replay-check", "preflight", "submitting"])
        with mock.patch.object(self.launcher, "existing_submission_guard",
                               side_effect=self.launcher.SubmissionUncertain(
                                   "media_acceptance_submission_already_recorded")), \
             mock.patch.object(self.launcher, "ensure_public_apply_preflight") as preflight, \
             self.assertRaises(self.launcher.SubmissionUncertain):
            self.launcher.submit(spec)
        preflight.assert_not_called()

    def test_successful_submit_output_binds_complete_action_identity(self):
        spec = self.spec("render", "long", "4c4t", "r2")
        completed = SimpleNamespace(returncode=0, stdout="Running as unit fixed.service.\n")
        with mock.patch.object(self.launcher, "existing_submission_guard"), \
             mock.patch.object(self.launcher, "ensure_public_apply_preflight"), \
             mock.patch.object(self.launcher, "build_systemd_command", return_value=["fixed"]), \
             mock.patch.object(self.launcher, "write_submission_record") as write, \
             mock.patch.object(self.launcher.subprocess, "run", return_value=completed):
            value = self.launcher.submit(spec)
        self.assertEqual([call.args[1] for call in write.call_args_list],
                         ["submitting", "accepted"])
        self.assertEqual({key: value[key] for key in (
            "candidate_sha", "run_id", "operation", "sample_kind",
            "configuration", "trial", "unit",
        )}, {
            "candidate_sha": self.SHA, "run_id": "accept01",
            "operation": "render", "sample_kind": "long",
            "configuration": "4c4t", "trial": "r2", "unit": spec.unit,
        })
        self.assertTrue(value["submitted"])
        self.assertFalse(value["completion_unknown"])
        self.assertTrue(value["replay_forbidden"])

    def test_public_unknown_submit_output_never_claims_media_not_started(self):
        output = __import__("io").StringIO()
        arguments = [
            "--candidate-sha", self.SHA, "--run-id", "accept01",
            "--sample-kind", "short", "--config", "2c2t", "--trial", "r1",
            "--apply",
        ]
        with mock.patch.object(self.launcher, "submit", side_effect=
                               self.launcher.SubmissionUncertain(
                                   "media_acceptance_submit_outcome_unknown")), \
             mock.patch("sys.stdout", new=output):
            self.assertEqual(self.launcher.main(arguments), 78)
        value = json.loads(output.getvalue())
        self.assertIsNone(value["media_started"])
        self.assertTrue(value["completion_unknown"])
        self.assertTrue(value["replay_forbidden"])
        self.assertEqual(value["unit"], self.spec().unit)
        self.assertEqual({key: value[key] for key in (
            "candidate_sha", "run_id", "operation", "sample_kind",
            "configuration", "trial", "unit",
        )}, {
            "candidate_sha": self.SHA, "run_id": "accept01",
            "operation": "render", "sample_kind": "short",
            "configuration": "2c2t", "trial": "r1",
            "unit": self.spec().unit,
        })

    def test_pressure_delta_rejects_recovered_burst_and_oom_kill(self):
        pressure = {
            "memory_failcnt": 0, "memsw_failcnt": 0, "swap_bytes": 0,
            "oom_control": {"oom_kill_disable": 0, "under_oom": 0,
                            "oom_kill": 0, "oom_kill_available": True},
        }
        before = {"resources_sha256": "a" * 64, "pressure": pressure}
        for after_pressure in (
                {**pressure, "memory_failcnt": 1},
                {**pressure, "oom_control": {**pressure["oom_control"], "oom_kill": 1}},
        ):
            with self.subTest(after=after_pressure), self.assertRaises(self.guard.GuardFailure):
                self.guard.verify_pressure_transition(before, {
                    "resources_sha256": "a" * 64, "pressure": after_pressure
                })

    def test_base_exception_still_runs_post_pressure_audit_before_propagating(self):
        spec = self.spec("render", "long", "2c2t", "r1")
        pressure = {
            "memory_failcnt": 0, "memsw_failcnt": 0, "swap_bytes": 0,
            "oom_control": {"oom_kill_disable": 0, "under_oom": 0,
                            "oom_kill": 0, "oom_kill_available": True},
        }
        snapshot = {"resources_sha256": "a" * 64, "pressure": pressure}
        fake_guard = SimpleNamespace(
            GuardFailure=self.guard.GuardFailure,
            MEDIA_16_GIB_PROFILE=object(), LinuxFiles=mock.Mock(return_value=object()),
            LinuxProcess=mock.Mock(return_value=object()),
            capture_pressure=mock.Mock(return_value=snapshot),
            verify_pressure_transition=mock.Mock(return_value={"verified": True}),
        )
        verified = {"proof": {"resources_sha256": "a" * 64}, "pressure": snapshot}
        source = {"version": 1, "candidate_sha": self.SHA, "run_id": "accept01",
                  "source": self.launcher.path_text(self.launcher.LONG_SOURCE),
                  "source_sha256": "b" * 64, "source_size": self.launcher.LONG_SOURCE_SIZE,
                  "source_device": 1, "source_inode": 2, "source_mtime_ns": 3,
                  "source_nlink": 1}
        with mock.patch.object(self.launcher, "write_exclusive_json") as write, \
             self.assertRaises(KeyboardInterrupt):
            self.launcher.finalize_resource_evidence(
                spec, 1009, 1010, fake_guard, verified, None, source, source,
                operation_error=KeyboardInterrupt(),
            )
        fake_guard.capture_pressure.assert_called_once()
        self.assertEqual(write.call_count, 1)
        self.assertFalse(write.call_args.args[1]["operation_succeeded"])

    def test_candidate_gate_checks_ignored_flags_and_each_worktree_blob(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            critical = (
                "scripts/run_drama_media_acceptance.py",
                "scripts/benchmark_drama_synthesis_media.py",
                "scripts/check_drama_media_resource_guard.py",
            )
            for name in critical:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name, encoding="utf-8")
                path.chmod(0o444)
            (root / ".git").mkdir()
            (root / ".git" / "info").mkdir()
            (root / ".git" / "info" / "attributes").write_text(
                "*.py filter=hostile\n", encoding="utf-8"
            )
            (root / ".git" / "config").write_text(
                "[filter \"hostile\"]\n\tprocess = never-run\n", encoding="utf-8"
            )
            spec = replace(self.spec(), candidate_root=root,
                           script_path=root / critical[0])
            blobs = {name: str(index + 1) * 40
                     for index, name in enumerate(critical)}
            calls = []

            def git(args, _root, **_kwargs):
                calls.append(tuple(args))
                if args == ["config", "--includes", "--show-origin", "--show-scope",
                            "--get-all", "core.fsmonitor"]:
                    return "command\tcommand line:\t\n"
                if args == ["rev-parse", "--show-toplevel"]:
                    return self.launcher.path_text(root) + "\n"
                if args == ["rev-parse", "--is-bare-repository"]:
                    return "false\n"
                if args == ["rev-parse", "--absolute-git-dir"]:
                    return self.launcher.path_text(root / ".git") + "\n"
                if args[:2] == ["rev-parse", "--verify"]:
                    return self.SHA + "\n" if args[2] == "HEAD^{commit}" else "b" * 40 + "\n"
                if args[0] == "for-each-ref":
                    return ""
                if args[:2] in (["ls-files", "-v"], ["ls-files", "-f"]):
                    return "".join("H " + name + "\x00" for name in critical)
                if args[:2] == ["ls-files", "--stage"]:
                    return "".join("100644 %s 0\t%s\x00" % (blobs[name], name)
                                   for name in critical)
                if args[0] == "ls-tree":
                    return "".join("100644 blob %s\t%s\x00" % (blobs[name], name)
                                   for name in critical)
                if args[:2] == ["hash-object", "--no-filters"]:
                    paths = _kwargs["input_bytes"].decode().splitlines()
                    return "".join(blobs[name] + "\n" for name in paths)
                raise AssertionError(args)

            def trusted_stat(path, _kind, _code):
                return os.stat(path)

            with mock.patch.object(self.launcher, "require_secure_git_binary"), \
                 mock.patch.object(self.launcher, "verify_git_directory_security",
                                   return_value=root / ".git"), \
                 mock.patch.object(self.launcher, "require_secure_directory_ancestors"), \
                 mock.patch.object(self.launcher, "require_root_owned_secure_path",
                                   side_effect=trusted_stat), \
                 mock.patch.object(self.launcher, "bounded_git", side_effect=git):
                result = self.launcher.verify_candidate(spec)
            self.assertEqual(set(result["critical"]), set(critical))
            self.assertEqual(set(result["tracked"]), set(critical))
            self.assertEqual(len([call for call in calls if call[0] == "hash-object"]), 1)
            self.assertFalse(any(call[0] == "status" for call in calls))

            ignored = root / "scripts" / "__pycache__" / "unsafe.pyc"
            ignored.parent.mkdir()
            ignored.write_bytes(b"untrusted bytecode")
            with mock.patch.object(self.launcher, "require_secure_git_binary"), \
                 mock.patch.object(self.launcher, "verify_git_directory_security",
                                   return_value=root / ".git"), \
                 mock.patch.object(self.launcher, "require_secure_directory_ancestors"), \
                 mock.patch.object(self.launcher, "require_root_owned_secure_path",
                                   side_effect=trusted_stat), \
                 mock.patch.object(self.launcher, "bounded_git", side_effect=git), \
                 self.assertRaises(self.launcher.LaunchFailure) as caught:
                self.launcher.verify_candidate(spec)
            self.assertEqual(str(caught.exception), "candidate_checkout_not_clean_exact_sha")
            ignored.unlink()
            ignored.parent.rmdir()

            def flagged_git(args, root, **kwargs):
                if args[:2] == ["ls-files", "-v"]:
                    return "h " + critical[0] + "\x00" + "".join(
                        "H " + name + "\x00" for name in critical[1:]
                    )
                return git(args, root, **kwargs)

            with mock.patch.object(self.launcher, "require_secure_git_binary"), \
                 mock.patch.object(self.launcher, "verify_git_directory_security",
                                   return_value=root / ".git"), \
                 mock.patch.object(self.launcher, "require_secure_directory_ancestors"), \
                 mock.patch.object(self.launcher, "require_root_owned_secure_path",
                                   side_effect=trusted_stat), \
                 mock.patch.object(self.launcher, "bounded_git", side_effect=flagged_git), \
                 self.assertRaises(self.launcher.LaunchFailure) as caught:
                self.launcher.verify_candidate(spec)
            self.assertEqual(str(caught.exception), "candidate_index_flags_unsafe")

            def fsmonitor_flagged_git(args, root, **kwargs):
                if args[:2] == ["ls-files", "-f"]:
                    return "h " + critical[0] + "\x00" + "".join(
                        "H " + name + "\x00" for name in critical[1:]
                    )
                return git(args, root, **kwargs)

            with mock.patch.object(self.launcher, "require_secure_git_binary"), \
                 mock.patch.object(self.launcher, "verify_git_directory_security",
                                   return_value=root / ".git"), \
                 mock.patch.object(self.launcher, "require_secure_directory_ancestors"), \
                 mock.patch.object(self.launcher, "require_root_owned_secure_path",
                                   side_effect=trusted_stat), \
                 mock.patch.object(self.launcher, "bounded_git",
                                   side_effect=fsmonitor_flagged_git), \
                 self.assertRaises(self.launcher.LaunchFailure) as caught:
                self.launcher.verify_candidate(spec)
            self.assertEqual(str(caught.exception), "candidate_index_flags_unsafe")

            def changed_blob(args, root, **kwargs):
                if args[:2] == ["hash-object", "--no-filters"]:
                    values = git(args, root, **kwargs).splitlines()
                    values[0] = "9" * 40
                    return "\n".join(values) + "\n"
                return git(args, root, **kwargs)

            with mock.patch.object(self.launcher, "require_secure_git_binary"), \
                 mock.patch.object(self.launcher, "verify_git_directory_security",
                                   return_value=root / ".git"), \
                 mock.patch.object(self.launcher, "require_secure_directory_ancestors"), \
                 mock.patch.object(self.launcher, "require_root_owned_secure_path",
                                   side_effect=trusted_stat), \
                 mock.patch.object(self.launcher, "bounded_git", side_effect=changed_blob), \
                 self.assertRaises(self.launcher.LaunchFailure) as caught:
                self.launcher.verify_candidate(spec)
            self.assertEqual(str(caught.exception), "candidate_worktree_blob_mismatch")

            with mock.patch.object(self.launcher, "require_secure_git_binary"), \
                 mock.patch.object(self.launcher, "verify_git_directory_security",
                                   return_value=root / ".git"), \
                 mock.patch.object(self.launcher, "require_secure_directory_ancestors"), \
                 mock.patch.object(self.launcher, "require_root_owned_secure_path",
                                   side_effect=trusted_stat), \
                 mock.patch.object(self.launcher, "bounded_git", side_effect=lambda args, base, **kw:
                                   self.launcher.path_text(root.parent) + "\n"
                                   if args == ["rev-parse", "--show-toplevel"]
                                   else git(args, base, **kw)), \
                 self.assertRaises(self.launcher.LaunchFailure) as caught:
                self.launcher.verify_candidate(spec)
            self.assertEqual(str(caught.exception), "candidate_worktree_binding_invalid")

    def test_candidate_filesystem_limit_is_enforced_while_scandir_streams(self):
        root = Path("/fixed/candidate")
        entries = [
            SimpleNamespace(path=str(root / ".git"), name=".git"),
            SimpleNamespace(path=str(root / "tracked.py"), name="tracked.py"),
            SimpleNamespace(path=str(root / "overflow.py"), name="overflow.py"),
        ]

        class StreamingEntries:
            def __enter__(self):
                return iter(entries)

            def __exit__(self, *_args):
                return False

        directory_stat = SimpleNamespace(st_mode=self.launcher.stat.S_IFDIR | 0o755)
        file_stat = SimpleNamespace(st_mode=self.launcher.stat.S_IFREG | 0o644)

        def lstat(path):
            return directory_stat if Path(path).name == ".git" else file_stat

        with mock.patch.object(self.launcher, "MAX_CANDIDATE_FILESYSTEM_ENTRIES", 2), \
             mock.patch.object(self.launcher, "require_secure_directory_ancestors"), \
             mock.patch.object(self.launcher, "require_root_owned_secure_path"), \
             mock.patch.object(self.launcher.os, "scandir", return_value=StreamingEntries()), \
             mock.patch.object(self.launcher.os, "lstat", side_effect=lstat) as inspected, \
             self.assertRaises(self.launcher.LaunchFailure) as caught:
            self.launcher.verify_candidate_filesystem_permissions(
                root, {"tracked.py": ("100644", "a" * 40)})
        self.assertEqual(str(caught.exception), "candidate_filesystem_too_large")
        self.assertEqual(inspected.call_count, 2)

    def test_bounded_git_uses_fixed_no_hook_no_replace_environment(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured.update(command=command, kwargs=kwargs)
            kwargs["stdout"].write(b"a" * 40 + b"\n")
            return SimpleNamespace(returncode=0)

        with mock.patch.object(self.launcher.subprocess, "run", side_effect=fake_run):
            result = self.launcher.bounded_git(
                ["rev-parse", "--verify", "HEAD^{commit}"], Path("/fixed/candidate")
            )
        self.assertEqual(result, "a" * 40 + "\n")
        command = captured["command"]
        self.assertIn("--no-replace-objects", command)
        self.assertIn("core.fsmonitor=", command)
        self.assertNotIn("core.fsmonitor=false", command)
        self.assertIn("core.untrackedCache=false", command)
        self.assertIn("core.hooksPath=/dev/null", command)
        self.assertIn("core.bare=false", command)
        self.assertIn("--work-tree=/fixed/candidate", command)
        environment = captured["kwargs"]["env"]
        self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertNotIn("HOME", environment)
        self.assertNotIn("XDG_CONFIG_HOME", environment)

    def test_fsmonitor_gate_accepts_only_command_line_empty_value(self):
        root = Path("/fixed/candidate")
        with mock.patch.object(
                self.launcher, "bounded_git", return_value="command\tcommand line:\t\n") as git:
            self.launcher.verify_fsmonitor_namespace(root)
        self.assertEqual(git.call_args.args[0], [
            "config", "--includes", "--show-origin", "--show-scope", "--get-all",
            "core.fsmonitor",
        ])
        for configured in (
                "local\tfile:.git/config\t/tmp/execute\ncommand\tcommand line:\t\n",
                "worktree\tfile:.git/config.worktree\t/tmp/execute\n"
                "command\tcommand line:\t\n",
                "local\tfile:/tmp/included.gitconfig\t/tmp/execute\n"
                "command\tcommand line:\t\n",
                "command\tcommand line:\tfalse\n",
        ):
            with self.subTest(configured=configured), \
                    mock.patch.object(self.launcher, "bounded_git", return_value=configured), \
                    self.assertRaises(self.launcher.LaunchFailure) as caught:
                self.launcher.verify_fsmonitor_namespace(root)
            self.assertEqual(str(caught.exception), "candidate_fsmonitor_config_unsafe")

    def test_git_binary_must_be_fixed_root_owned_and_not_group_writable(self):
        import stat
        safe = SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_dev=1, st_ino=2,
                               st_uid=0)
        with mock.patch.object(self.launcher.os, "lstat", return_value=safe), \
             mock.patch.object(self.launcher.os, "stat", return_value=safe), \
             mock.patch.object(self.launcher.os, "access", return_value=True):
            self.launcher.require_secure_git_binary()
        unsafe = SimpleNamespace(**{**safe.__dict__, "st_mode": stat.S_IFREG | 0o775})
        with mock.patch.object(self.launcher.os, "lstat", return_value=unsafe), \
             mock.patch.object(self.launcher.os, "stat", return_value=unsafe), \
             mock.patch.object(self.launcher.os, "access", return_value=True), \
             self.assertRaises(self.launcher.LaunchFailure) as caught:
            self.launcher.require_secure_git_binary()
        self.assertEqual(str(caught.exception), "candidate_git_binary_unsafe")

    def test_candidate_permission_primitives_require_root_no_external_write_and_git_mode(self):
        import stat

        def value(kind, permissions, uid=0):
            return SimpleNamespace(
                st_mode=kind | permissions, st_dev=1, st_ino=2, st_uid=uid,
                st_size=10, st_mtime_ns=3, st_nlink=1,
            )

        safe_directory = value(stat.S_IFDIR, 0o755)
        with mock.patch.object(self.launcher.os, "lstat", return_value=safe_directory), \
             mock.patch.object(self.launcher.os, "stat", return_value=safe_directory):
            self.assertIs(
                self.launcher.require_root_owned_secure_path(
                    Path("/candidate"), "directory", "unsafe"
                ),
                safe_directory,
            )

        writable_directory = value(stat.S_IFDIR, 0o775)
        with mock.patch.object(self.launcher.os, "lstat", return_value=writable_directory), \
             mock.patch.object(self.launcher.os, "stat", return_value=writable_directory), \
             self.assertRaises(self.launcher.LaunchFailure) as caught:
            self.launcher.require_root_owned_secure_path(
                Path("/candidate"), "directory", "unsafe"
            )
        self.assertEqual(str(caught.exception), "unsafe")

        non_root_file = value(stat.S_IFREG, 0o644, uid=1009)
        with mock.patch.object(self.launcher.os, "lstat", return_value=non_root_file), \
             mock.patch.object(self.launcher.os, "stat", return_value=non_root_file), \
             self.assertRaises(self.launcher.LaunchFailure) as caught:
            self.launcher.require_secure_tracked_file(Path("/candidate/file.py"), "100644")
        self.assertEqual(str(caught.exception),
                         "candidate_worktree_file_permissions_unsafe")

        executable_file = value(stat.S_IFREG, 0o755)
        with mock.patch.object(self.launcher.os, "lstat", return_value=executable_file), \
             mock.patch.object(self.launcher.os, "stat", return_value=executable_file), \
             self.assertRaises(self.launcher.LaunchFailure) as caught:
            self.launcher.require_secure_tracked_file(Path("/candidate/file.py"), "100644")
        self.assertEqual(str(caught.exception), "candidate_worktree_file_mode_mismatch")

    def test_internal_media_action_rechecks_long_source_at_start_and_end(self):
        spec = self.spec("render", "long", "2c2t", "r1")
        pressure = {
            "memory_failcnt": 0, "memsw_failcnt": 0, "swap_bytes": 0,
            "oom_control": {"oom_kill_disable": 0, "under_oom": 0,
                            "oom_kill": 0, "oom_kill_available": True},
        }
        snapshot = {"resources_sha256": "a" * 64, "pressure": pressure}
        source = {"version": 1, "candidate_sha": self.SHA, "run_id": "accept01",
                  "source": self.launcher.path_text(self.launcher.LONG_SOURCE),
                  "source_sha256": "b" * 64, "source_size": self.launcher.LONG_SOURCE_SIZE,
                  "source_device": 1, "source_inode": 2, "source_mtime_ns": 3,
                  "source_nlink": 1}
        guard = SimpleNamespace(
            GuardFailure=self.guard.GuardFailure,
            MEDIA_16_GIB_PROFILE=self.guard.MEDIA_16_GIB_PROFILE,
            verify_inherited_guard=mock.Mock(return_value={
                "proof": {"pid": 123, "profile": self.guard.MEDIA_16_GIB_PROFILE.name,
                          "resources_sha256": "a" * 64},
                "pressure": snapshot,
            }),
            LinuxFiles=mock.Mock(return_value=object()),
            LinuxProcess=mock.Mock(return_value=object()),
            capture_pressure=mock.Mock(return_value=snapshot),
            verify_pressure_transition=mock.Mock(return_value={"verified": True}),
        )
        events = []

        class Process:
            returncode = None

            def poll(self):
                events.append("poll")
                return self.returncode

            def kill(self):
                events.append("kill")
                self.returncode = -9

            def wait(self, timeout):
                events.append(("wait", timeout))
                if sum(isinstance(item, tuple) and item[0] == "wait"
                       for item in events) == 1:
                    raise KeyboardInterrupt()
                return self.returncode

        benchmark = SimpleNamespace(
            MEDIA_ACCEPTANCE_LOCK_PATH=self.launcher.LOCK_PATH,
            launch_renderer_process=mock.Mock(return_value=Process()),
        )
        candidate = {"head": self.SHA, "tree": "b" * 40,
                     "snapshot_sha256": "c" * 64, "tracked": {}, "critical": {
                         "scripts/check_drama_media_resource_guard.py": {},
                         "scripts/benchmark_drama_synthesis_media.py": {},
                     }}
        def interrupted_render(_spec, _uid, _gid, inherited_lock_fd, loaded_benchmark):
            self.launcher.run_fixed_child(
                loaded_benchmark, ["fixed"], inherited_lock_fd, timeout=1,
                failure_code="fixed_failed", timeout_code="fixed_timeout",
                cleanup_code="fixed_cleanup_failed",
            )

        with mock.patch.object(self.launcher, "require_linux"), \
             mock.patch.object(self.launcher, "ensure_python_stage"), \
             mock.patch.object(self.launcher, "target_identity", return_value=(1009, 1010)), \
             mock.patch.object(self.launcher.os, "geteuid", return_value=1009, create=True), \
             mock.patch.object(self.launcher.os, "getegid", return_value=1010, create=True), \
             mock.patch.object(self.launcher.os, "getgroups", return_value=[], create=True), \
             mock.patch.object(self.launcher.os, "getpid", return_value=123), \
             mock.patch.object(self.launcher, "verify_candidate", return_value=candidate), \
             mock.patch.object(self.launcher, "validate_fixed_inputs"), \
             mock.patch.object(self.launcher, "read_host_memory", return_value={
                 "MemTotal": 32 * 1024 ** 3, "MemAvailable": 32 * 1024 ** 3,
             }), \
             mock.patch.object(self.launcher, "verify_private_run_root"), \
             mock.patch.object(self.launcher, "validate_existing_action_inputs"), \
             mock.patch.object(self.launcher, "verify_inherited_media_lock",
                               return_value=(7, 8)), \
             mock.patch.object(self.launcher, "load_candidate_module",
                               side_effect=[guard, benchmark]), \
              mock.patch.object(self.launcher, "ensure_run_source_frozen",
                                side_effect=[source, source]) as freeze, \
              mock.patch.object(self.launcher, "run_render",
                                side_effect=interrupted_render), \
              mock.patch.object(self.launcher, "write_exclusive_json") as write, \
              mock.patch.object(self.launcher, "fingerprint_regular",
                                return_value={"sha256": "c" * 64, "size_bytes": 100}), \
              mock.patch.object(self.launcher, "validate_action_completion"), \
              self.assertRaises(KeyboardInterrupt):
            self.launcher.internal_verified_stage(spec, 6, 9)
        self.assertEqual(freeze.call_count, 2)
        self.assertIn("kill", events)
        self.assertEqual(sum(isinstance(item, tuple) and item[0] == "wait"
                             for item in events), 2)
        guard.capture_pressure.assert_called_once()
        guard.verify_pressure_transition.assert_called_once()
        written_paths = [call.args[0] for call in write.call_args_list]
        self.assertIn(spec.resource_evidence_path, written_paths)
        self.assertNotIn(spec.completion_evidence_path, written_paths)
        resource_value = next(
            call.args[1] for call in write.call_args_list
            if call.args[0] == spec.resource_evidence_path
        )
        self.assertEqual(resource_value["minimum_mem_available_bytes"], 32 * 1024 ** 3)
        self.assertEqual(resource_value["host_memory_stop_threshold_bytes"],
                         8 * 1024 ** 3)

    def test_submission_intent_lstat_errors_are_unknown_except_definite_absence(self):
        import errno

        spec = self.spec("render", "long", "2c2t", "r1")
        write_error = self.launcher.LaunchFailure("submission_guard_write_failed")
        with mock.patch.object(self.launcher, "write_exclusive_json",
                               side_effect=write_error), \
             mock.patch.object(self.launcher.os, "lstat",
                               side_effect=OSError(errno.EIO, "simulated")), \
             self.assertRaises(self.launcher.SubmissionUncertain) as caught:
            self.launcher.write_submission_record(spec, "submitting")
        self.assertEqual(str(caught.exception),
                         "media_acceptance_submission_state_unknown")

        write_error = self.launcher.LaunchFailure("submission_guard_write_failed")
        with mock.patch.object(self.launcher, "write_exclusive_json",
                               side_effect=write_error), \
             mock.patch.object(self.launcher.os, "lstat",
                               side_effect=FileNotFoundError()), \
             self.assertRaises(self.launcher.LaunchFailure) as caught:
            self.launcher.write_submission_record(spec, "submitting")
        self.assertIs(caught.exception, write_error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
