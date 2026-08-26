#!/usr/bin/env python3
"""Focused, offline contract tests for the drama-synthesis upgrade."""

from __future__ import annotations

import hashlib
import ast
import concurrent.futures
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.drama_synthesis.core import (
    COMMENT_SCOPE,
    DramaSynthesisError,
    DramaSynthesisStore,
    ImmutableFilesystemPublisher,
    RECIPE_CATEGORIES,
    RECIPE_PROFILE,
    build_long_url,
    freeze_random_recipe,
    render_wrapper_html,
)
from features.drama_synthesis.youtube import (
    YouTubeCredential,
    YouTubeCredentialRepository,
    YouTubeHTTPError,
    YouTubePublishEngine,
)
from features.drama_synthesis import gpu as gpu_adapter


JOB_ID = "0123456789abcdef0123456789abcdef"
OTHER_JOB_ID = "fedcba9876543210fedcba9876543210"
CHANNEL_ID = "UCabcdefghijklmnopqrstuv"
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def catalog():
    categories = {}
    for category in RECIPE_CATEGORIES:
        media_type = "video/webm" if category == "opacity_video" else "image/png"
        categories[category] = [
            {
                "name": f"{category}-{index}.{ 'webm' if media_type == 'video/webm' else 'png'}",
                "sha256": hashlib.sha256(f"{category}:{index}".encode()).hexdigest(),
                "media_type": media_type,
                "size": 1000 + index,
            }
            for index in (1, 2)
        ]
    return {
        "version": 1,
        "profile": RECIPE_PROFILE,
        "manifest_sha256": "a" * 64,
        "categories": categories,
    }


class CredentialRepository:
    def __init__(self, scopes=(UPLOAD_SCOPE, COMMENT_SCOPE)):
        self.scopes = frozenset(scopes)

    def credential(self, **_kwargs):
        return YouTubeCredential(
            account_id="11",
            channel_local_id="22",
            channel_id=CHANNEL_ID,
            channel_name="Offline fake",
            channel_status=1,
            scopes=self.scopes,
            refresh_token="server-only-refresh",
            client_id="server-only-client",
            client_secret="server-only-secret",
        )


class SuccessfulClient:
    def __init__(self):
        self.begin_count = 0
        self.upload_count = 0
        self.comment_count = 0

    def refresh_access_token(self, _credential):
        return "not-a-real-token"

    def download(self, _url, target, **_kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"offline-video")
        return target.stat().st_size

    def begin_resumable(self, _token, **_kwargs):
        self.begin_count += 1
        return "https://upload.youtube.test/resumable/session-1"

    def query_upload(self, _session_uri, _size):
        return {"state": "published", "video_id": "video_123456", "next_byte": 13}

    def upload(self, _session_uri, _source, _offset):
        self.upload_count += 1
        return {"state": "published", "video_id": "video_123456", "next_byte": 13}

    def publish_comment(self, _token, **_kwargs):
        self.comment_count += 1
        return "comment-123"


class ResumeClient(SuccessfulClient):
    def upload(self, _session_uri, _source, _offset):
        self.upload_count += 1
        return {"state": "resume", "next_byte": 5}


class UnknownClient(SuccessfulClient):
    def upload(self, _session_uri, _source, _offset):
        self.upload_count += 1
        raise YouTubeHTTPError("youtube_upload_unknown", "unknown", unknown=True)


class ExpiredClient(SuccessfulClient):
    def query_upload(self, _session_uri, _size):
        return {"state": "expired"}


class RefreshRetryClient(SuccessfulClient):
    def refresh_access_token(self, _credential):
        raise YouTubeHTTPError("youtube_token_refresh_unavailable", "temporarily unavailable", retryable=True)


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = DramaSynthesisStore(self.root / "jobs.sqlite3")
        self.store.ensure_storage()

    def tearDown(self):
        self.temp.cleanup()

    def enqueue(self, operation_id="operation:0001", comment="first comment", duplicate=False):
        return self.store.enqueue_youtube(
            operation_id=operation_id,
            job_id=JOB_ID,
            app_id="1479",
            channel_local_id="22",
            channel_id=CHANNEL_ID,
            youtube_account_id="11",
            source_kind="random_template_video",
            source_url="https://media.example.test/material.mp4",
            title="Offline title",
            description="Offline description",
            comment_text=comment,
            duplicate_confirmed=duplicate,
            scopes=(UPLOAD_SCOPE, COMMENT_SCOPE),
        )

    def engine(self, client):
        return YouTubePublishEngine(
            self.store,
            CredentialRepository(),
            client,
            work_root=self.root / "youtube",
            allowed_source_hosts=("media.example.test",),
        )

    def test_auto_recipe_is_deterministic_and_complete(self):
        one = freeze_random_recipe(job_id=JOB_ID, content_id="drama-1", request={"mode": "auto"}, catalog=catalog())
        two = freeze_random_recipe(job_id=JOB_ID, content_id="drama-1", request={"mode": "auto"}, catalog=catalog())
        self.assertEqual(one, two)
        self.assertEqual(set(one["assets"]), set(RECIPE_CATEGORIES))
        self.assertEqual(one["profile"], RECIPE_PROFILE)

    def test_new_python_runtime_files_parse_as_python39(self):
        paths = [
            ROOT / "features" / "drama_synthesis" / name
            for name in ("core.py", "gpu.py", "youtube.py")
        ] + [ROOT / "scripts" / "drama_youtube_publish_worker.py"]
        for path in paths:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9))

    def test_manual_recipe_requires_every_layer(self):
        layers = {category: catalog()["categories"][category][1]["name"] for category in RECIPE_CATEGORIES}
        recipe = freeze_random_recipe(job_id=JOB_ID, content_id="drama-1", request={"mode": "manual", "layers": layers}, catalog=catalog())
        self.assertEqual({key: row["name"] for key, row in recipe["assets"].items()}, layers)
        with self.assertRaises(DramaSynthesisError):
            freeze_random_recipe(job_id=JOB_ID, content_id="drama-1", request={"mode": "manual", "layers": {}}, catalog=catalog())
        invalid_catalog = catalog()
        invalid_catalog["profile"] = "unexpected-profile"
        with self.assertRaises(DramaSynthesisError):
            freeze_random_recipe(job_id=JOB_ID, content_id="drama-1", request={"mode": "auto"}, catalog=invalid_catalog)

    def test_gpu_adapter_returns_frozen_recipe_and_output_identity(self):
        recipe = freeze_random_recipe(job_id=JOB_ID, content_id="drama-1", request={"mode": "auto"}, catalog=catalog())
        source = self.root / "source.mp4"
        output = self.root / "output.mp4"
        source.write_bytes(b"source")
        probe = {
            "duration": 1.0,
            "has_audio": True,
            "video": {"codec_name": "h264", "profile": "High", "width": 720, "height": 1280},
            "audio": {"codec_name": "aac"},
        }
        with mock.patch.object(gpu_adapter, "_probe", return_value=probe), \
                mock.patch.object(gpu_adapter, "load_asset_set", return_value={"manifest_sha256": "a" * 64}), \
                mock.patch.object(gpu_adapter, "validate_recipe"), \
                mock.patch.object(gpu_adapter, "selected_asset_paths", return_value={}), \
                mock.patch.object(gpu_adapter, "build_command", return_value=["ffmpeg", "offline"]), \
                mock.patch.object(gpu_adapter, "sha256_file", return_value=("b" * 64, 1234)):
            result = gpu_adapter.render_random_output(
                source=source,
                output=output,
                recipe=recipe,
                asset_root=self.root,
                manifest_sha256="a" * 64,
                runner=mock.Mock(),
            )
        self.assertEqual(result["recipe_sha256"], recipe["recipe_sha256"])
        self.assertEqual((result["output_sha256"], result["profile"]), ("b" * 64, RECIPE_PROFILE))

    def test_recipe_freeze_is_retry_stable_and_conflict_safe(self):
        recipe = freeze_random_recipe(job_id=JOB_ID, content_id="drama-1", request={"mode": "auto"}, catalog=catalog())
        first = self.store.freeze_recipe(JOB_ID, recipe)
        second = self.store.freeze_recipe(JOB_ID, recipe)
        self.assertEqual(first["recipe_sha256"], second["recipe_sha256"])
        changed = freeze_random_recipe(job_id=OTHER_JOB_ID, content_id="drama-1", request={"mode": "auto"}, catalog=catalog())
        with self.assertRaises(DramaSynthesisError):
            self.store.freeze_recipe(JOB_ID, changed)

    def test_random_template_youtube_source_resolves_from_frozen_output(self):
        import app as drama_app

        recipe = freeze_random_recipe(job_id=JOB_ID, content_id="drama-1", request={"mode": "auto"}, catalog=catalog())
        self.store.freeze_recipe(JOB_ID, recipe)
        output_url = "https://media.example.test/random-template.mp4"
        self.store.complete_recipe(
            JOB_ID,
            output_url=output_url,
            output_sha256="b" * 64,
            output_profile=RECIPE_PROFILE,
            recipe_sha256=recipe["recipe_sha256"],
        )
        raw_legacy_job = {
            "job_id": JOB_ID,
            "output_video_url": "",
            "output_video_no_bgm_url": "",
        }
        with mock.patch.object(drama_app, "DRAMA_SYNTHESIS_STORE", self.store):
            kind, resolved = drama_app.drama_youtube_source(raw_legacy_job, "random_template_video")
        self.assertEqual((kind, resolved), ("random_template_video", output_url))

    def test_short_target_and_wrapper_are_exact_and_closed(self):
        target = build_long_url(JOB_ID)
        self.assertEqual(target, "https://www.dramawavew2a.com/ads/101/2284/view?cid=" + JOB_ID + "&af_channel=ai_youtube")
        wrapper = render_wrapper_html(JOB_ID).decode()
        self.assertIn(target.replace("&", "&amp;"), wrapper)
        self.assertNotIn("window.location", wrapper)

    def test_short_link_is_idempotent_and_immutable(self):
        publisher = ImmutableFilesystemPublisher(self.root / "wrappers")
        first = self.store.ensure_short_link(JOB_ID, publisher)
        second = self.store.ensure_short_link(JOB_ID, publisher)
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["short_url"], f"https://page.dramabuzzs.com/s2l/{first['id']}.html")
        self.assertEqual((self.root / "wrappers" / f"{first['id']}.html").read_bytes(), render_wrapper_html(JOB_ID))

    def test_short_link_concurrency_never_overwrites(self):
        publisher = ImmutableFilesystemPublisher(self.root / "concurrent-wrappers")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            rows = list(executor.map(lambda _index: self.store.ensure_short_link(JOB_ID, publisher), range(2)))
        self.assertEqual(len({row["id"] for row in rows}), 1)
        self.assertEqual(sorted(bool(row["reused"]) for row in rows), [False, True])

    def test_short_link_fails_closed_without_publisher(self):
        with self.assertRaisesRegex(DramaSynthesisError, "短链发布器"):
            self.store.ensure_short_link(JOB_ID, None)
        self.assertEqual(self.store.short_link(JOB_ID)["publish_state"], "failed")

    def test_youtube_operation_is_idempotent(self):
        first = self.enqueue()
        second = self.enqueue()
        self.assertEqual(first["id"], second["id"])

    def test_youtube_comment_scope_is_exact_and_fail_closed(self):
        with self.assertRaisesRegex(DramaSynthesisError, "评论权限"):
            self.store.enqueue_youtube(
                operation_id="operation:scope1", job_id=JOB_ID, app_id="1479",
                channel_local_id="22", channel_id=CHANNEL_ID, youtube_account_id="11",
                source_kind="concat_video", source_url="https://media.example.test/material.mp4",
                title="Title", description="", comment_text="comment",
                duplicate_confirmed=False, scopes=(UPLOAD_SCOPE,),
            )

    def test_youtube_channel_eligibility_requires_status_refresh_and_upload_scope(self):
        def token(scopes, refresh="refresh"):
            return json.dumps({"scope": scopes, "refresh_token": refresh})

        credentials = json.dumps({"installed": {"client_id": "client", "client_secret": "secret"}})
        rows = [
            ("1", CHANNEL_ID, "eligible", "1", "101", token([UPLOAD_SCOPE, COMMENT_SCOPE]), credentials),
            ("2", "UCbbbbbbbbbbbbbbbbbbbbbb", "status-2", "2", "102", token([UPLOAD_SCOPE]), credentials),
            ("3", "UCcccccccccccccccccccccc", "missing-scope", "1", "103", token([]), credentials),
            ("4", "UCdddddddddddddddddddddd", "missing-refresh", "1", "104", token([UPLOAD_SCOPE], ""), credentials),
        ]
        def schema_contract_runner(sql):
            self.assertIn("ch.channel_status", sql)
            self.assertNotIn("ch.status", sql)
            return rows

        items = YouTubeCredentialRepository(schema_contract_runner).list_for_app("1479")
        self.assertEqual([item["channel_id"] for item in items], [CHANNEL_ID])
        self.assertTrue(items[0]["comment_eligible"])

    def test_youtube_schema_contract_uses_live_channel_status_column(self):
        captured = []

        def runner(sql):
            captured.append(sql)
            if "ch.status" in sql or "ch.channel_status" not in sql:
                raise AssertionError("query does not match live ads_youtube_channels schema")
            return []

        self.assertEqual(YouTubeCredentialRepository(runner).list_for_app("1479"), [])
        self.assertEqual(len(captured), 1)

    def test_youtube_sql_identifiers_reject_quote_and_backslash_adversaries(self):
        captured = []
        repository = YouTubeCredentialRepository(lambda sql: captured.append(sql) or [])
        adversaries = ("1479' OR 1=1 --", r"1479\' OR 1=1 --", "0", "9223372036854775808")
        for value in adversaries:
            with self.assertRaises(DramaSynthesisError):
                repository.list_for_app(value)
            with self.assertRaises(DramaSynthesisError):
                repository.credential(
                    app_id="1479",
                    channel_local_id=value,
                    account_id="11",
                    expected_channel_id=CHANNEL_ID,
                )
            with self.assertRaises(DramaSynthesisError):
                repository.credential(
                    app_id="1479",
                    channel_local_id="22",
                    account_id=value,
                    expected_channel_id=CHANNEL_ID,
                )
        self.assertEqual(captured, [])

    def test_youtube_video_and_comment_have_separate_success_states(self):
        row = self.enqueue()
        client = SuccessfulClient()
        result = self.engine(client).run_once("worker-1")
        stored = self.store.youtube_task(row["id"])
        self.assertTrue(result["ok"])
        self.assertEqual((stored["video_state"], stored["comment_state"]), ("published", "published"))
        self.assertEqual((client.begin_count, client.upload_count, client.comment_count), (1, 1, 1))

    def test_youtube_retry_queries_session_and_never_duplicates_video(self):
        row = self.enqueue(operation_id="operation:retry1", comment="")
        first_client = ResumeClient()
        first = self.engine(first_client).run_once("worker-1")
        self.assertEqual(first["status"], "video_retry")
        second_client = SuccessfulClient()
        second = self.engine(second_client).run_once("worker-2")
        stored = self.store.youtube_task(row["id"])
        self.assertTrue(second["ok"])
        self.assertEqual(stored["video_state"], "published")
        self.assertEqual(second_client.begin_count, 0)
        self.assertEqual(second_client.upload_count, 0)

    def test_youtube_unknown_outcome_fails_closed(self):
        row = self.enqueue(operation_id="operation:unknown1", comment="")
        client = UnknownClient()
        result = self.engine(client).run_once("worker-1")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.store.youtube_task(row["id"])["unknown_outcome"], 1)
        self.assertFalse(self.engine(SuccessfulClient()).run_once("worker-2")["claimed"])

    def test_youtube_expired_session_is_unknown_and_closed(self):
        row = self.enqueue(operation_id="operation:expired1", comment="")
        self.store.set_upload_session(row["id"], "https://upload.youtube.test/resumable/expired", 13)
        result = self.engine(ExpiredClient()).run_once("worker-1")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.store.youtube_task(row["id"])["unknown_outcome"], 1)

    def test_expired_worker_lease_can_be_reclaimed(self):
        row = self.enqueue(operation_id="operation:lease01", comment="")
        first = self.store.claim_youtube("crashed-worker", "2000-01-01T00:00:00Z")
        self.assertEqual(first["id"], row["id"])
        second = self.store.claim_youtube("recovery-worker", "2099-01-01T00:00:00Z")
        self.assertEqual(second["id"], row["id"])
        self.assertEqual(second["lease_owner"], "recovery-worker")

    def test_interrupted_comment_is_unknown_and_never_duplicated(self):
        row = self.enqueue(operation_id="operation:comment1")
        self.store.video_published(row["id"], "video_123456")
        claimed = self.store.claim_youtube("crashed-comment-worker", "2000-01-01T00:00:00Z")
        self.assertEqual(claimed["status"], "publishing_comment")
        self.store.mark_comment_attempt(row["id"])
        self.assertIsNone(self.store.claim_youtube("recovery-worker", "2099-01-01T00:00:00Z"))
        stored = self.store.youtube_task(row["id"])
        self.assertEqual((stored["status"], stored["comment_state"], stored["unknown_outcome"]), ("unknown", "unknown", 1))

    def test_known_safe_pre_comment_failure_retries_comment_once(self):
        row = self.enqueue(operation_id="operation:comment-retry")
        self.store.video_published(row["id"], "video_123456")
        failed_client = RefreshRetryClient()
        first = self.engine(failed_client).run_once("worker-1")
        self.assertEqual(first["status"], "comment_retry")
        self.assertEqual(failed_client.comment_count, 0)
        retry_client = SuccessfulClient()
        second = self.engine(retry_client).run_once("worker-2")
        stored = self.store.youtube_task(row["id"])
        self.assertTrue(second["ok"])
        self.assertEqual((stored["comment_state"], stored["comment_attempt_count"]), ("published", 1))
        self.assertEqual(retry_client.comment_count, 1)

    def test_prior_success_requires_second_confirmation(self):
        first = self.enqueue(operation_id="operation:first1", comment="")
        self.store.video_published(first["id"], "video_123456")
        with self.assertRaisesRegex(DramaSynthesisError, "二次确认"):
            self.enqueue(operation_id="operation:second", comment="")
        accepted = self.enqueue(operation_id="operation:second", comment="", duplicate=True)
        self.assertEqual(accepted["duplicate_confirmed"], 1)

    def test_ui_and_backend_zero_output_contract_and_removed_fields(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        standalone = (ROOT / "static" / "drama-synthesis.html").read_text(encoding="utf-8")
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertEqual(html, standalone)
        for element_id in ("outVideo", "outNoBgm", "outCover", "outRandomTemplate"):
            marker = f'id="{element_id}" type="checkbox"'
            self.assertIn(marker, html)
            self.assertNotIn(marker + " checked", html)
        self.assertNotIn("coverTemplateInput", html)
        self.assertNotIn("namingRuleInput", html)
        self.assertIn('raise ValueError("至少选择一个输出项")', app_source)
        for route in (
            "/api/drama-material/random-template-catalog",
            "/youtube-channels",
            "short-link|youtube-publish",
            "/api/gpu-video/random-overlay/catalog",
        ):
            self.assertIn(route, app_source)
        self.assertIn("DRAMA_SYNTHESIS_STORE.ensure_storage()", app_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
