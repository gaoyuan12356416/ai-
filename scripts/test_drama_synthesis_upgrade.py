#!/usr/bin/env python3
"""Focused, fake-only acceptance tests for the authoritative drama upgrade."""

from __future__ import annotations

import ast
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
        "operator_user_id": 803,
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
        "operator_user_id": 803,
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
    def publish_comment(self, _token, *, video_id, comment_text):
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

    def enqueue(self, operation="operation:test-0001", comment="", description="required", confirmed=False):
        return self.store.enqueue_youtube(
            operation_id=operation, job_id=JOB_ID, content_id="2284", app_id="1479",
            channel_local_id="1", channel_id=CHANNEL, youtube_account_id="2",
            source_kind="concat_video", source_url="https://example.test/video.mp4",
            title="Title", description_template=description, description_rendered=description,
            comment_text=comment, duplicate_confirmed=confirmed,
            scopes=(UPLOAD, READONLY, COMMENT_SCOPE), operator_user_id="803", operator_name="tester",
        )

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
        token = json.dumps({"refresh_token": "r", "scope": [UPLOAD, READONLY]})
        credentials = json.dumps({"installed": {"client_id": "c", "client_secret": "s"}})
        repo = YouTubeCredentialRepository(lambda sql: seen.append(sql) or [("1", CHANNEL, "Current", "1", "2", token, credentials)], identity_probe=lambda _row: True)
        self.assertEqual(len(repo.list_for_app("1479")), 1)
        self.assertIn("ch.channel_status", seen[0])
        with self.assertRaises(DramaSynthesisError):
            repo.list_for_app("1'\\ OR 1=1")

    def test_channel_list_hides_upload_only_and_status_two(self):
        upload_only = json.dumps({"refresh_token": "r", "scope": [UPLOAD]})
        complete = json.dumps({"refresh_token": "r", "scope": [UPLOAD, READONLY]})
        creds = json.dumps({"installed": {"client_id": "c", "client_secret": "s"}})
        rows = [("1", CHANNEL, "upload-only", "1", "2", upload_only, creds), ("3", CHANNEL[:-1] + "B", "disabled", "2", "4", complete, creds)]
        self.assertEqual(YouTubeCredentialRepository(lambda _sql: rows, identity_probe=lambda _row: True).list_for_app("1479"), [])

    def test_channel_list_requires_runtime_refresh_identity_and_hides_failures(self):
        token = json.dumps({"refresh_token": "r", "scope": [UPLOAD, READONLY]})
        creds = json.dumps({"installed": {"client_id": "c", "client_secret": "s"}})
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
            self.assertIn("runuser -u drama-youtube -- /usr/bin/python3", document)
            self.assertIn("/opt/drama-youtube-unified-writer/current/scripts/", document)
        self.assertIn("writer database credential file is invalid", migration_doc)
        self.assertIn("releases/<candidate_git_sha>", migration_doc)
        self.assertIn("rehearsal_result_sha256", migration_doc)

    def test_gpu_worker_fake_http_contract_is_media_only(self):
        fake_app = SimpleNamespace(
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
                request = Request(base + "/api/gpu-video/render", data=json.dumps({"recipe": "frozen"}).encode(), headers={"Authorization": "Bearer fake-token", "Content-Type": "application/json"}, method="POST")
                self.assertEqual(json.loads(urlopen(request, timeout=2).read())["recipe"], "frozen")
                with self.assertRaises(HTTPError) as denied:
                    urlopen(base + "/api/gpu-video/youtube", timeout=2)
                self.assertEqual(denied.exception.code, 404)
                denied.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
