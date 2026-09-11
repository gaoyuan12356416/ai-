#!/usr/bin/env python3
"""No-network tests of reviewed upload isolation and irreversible-write fences."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image
from features.drama_synthesis.core import COMMENT_SCOPE, DramaSynthesisError, DramaSynthesisStore
from features.drama_synthesis.youtube import YouTubeCredential, YouTubeHTTPError
from features.youtube_auto_publish.engine import ReviewedYouTubeHTTPClient, ReviewedYouTubePublishEngine
from features.drama_synthesis.unified_youtube import validate_entity_payload
from features.drama_synthesis.unified_youtube_rpc import _video_record

CHANNEL = "UC" + "x" * 22
VIDEO = "reviewed_video_1"
EXPIRY = "2099-01-01T00:00:00Z"


class Credentials:
    value = YouTubeCredential(account_id="11", channel_local_id="12", channel_id=CHANNEL,
        channel_name="Offline test", channel_status=1, scopes=frozenset({COMMENT_SCOPE}),
        refresh_token="OFFLINE", client_id="OFFLINE", client_secret="OFFLINE")

    def credential(self, **request):
        assert request == dict(app_id="1479", channel_local_id="12", account_id="11", expected_channel_id=CHANNEL)
        return self.value


class Media:
    def __init__(self):
        self.prepared, self.uploaded, self.cleaned = [], [], []
        self.error = None
        self.sha = hashlib.sha256(b"offline-mp4").hexdigest()

    def prepare(self, task_id, source_url, *, heartbeat):
        heartbeat()
        self.prepared.append(task_id)
        return {"sha256": self.sha, "size": 42, "duration_ms": 10000}

    def upload(self, task_id, session_uri, offset, *, size, sha256, heartbeat):
        heartbeat()
        self.uploaded.append(task_id)
        if self.error:
            raise self.error
        return {"state": "submitted", "video_id": VIDEO}

    def cleanup(self, task_id):
        self.cleaned.append(task_id)


class Client:
    def __init__(self, store):
        self.store = store
        self.begins, self.thumbnails, self.publications, self.comments = [], [], [], []
        self.visibility, self.processing = "private", "succeeded"
        self.begin_error = self.thumbnail_error = self.public_error = self.comment_error = None
        self.public_takes_effect = True
        self.query_result = {"state": "resume", "next_byte": 0}

    def refresh_access_token(self, credential):
        return "OFFLINE"

    def verify_channel_identity(self, token, channel_id):
        assert channel_id == CHANNEL

    def begin_resumable(self, token, **request):
        assert request["privacy_status"] == "private"
        row = self.store.youtube_task(1)
        assert row["video_attempt_count"] == 1 and not row["resumable_session_uri"]
        self.begins.append(request)
        if self.begin_error:
            raise self.begin_error
        return "https://www.googleapis.com/upload/frozen-session"

    def query_upload(self, session, size):
        if isinstance(self.query_result, Exception):
            raise self.query_result
        return self.query_result

    def read_reviewed_video_state(self, token, video_id, *, expected_channel_id):
        assert video_id == VIDEO and expected_channel_id == CHANNEL
        return {"state": self.processing, "visibility": self.visibility,
            "processing_status": self.processing, "preserved_status": {"privacyStatus": self.visibility, "embeddable": False}}

    def set_thumbnail(self, token, **request):
        assert request["video_id"] == VIDEO
        self.thumbnails.append(request)
        if self.thumbnail_error:
            raise self.thumbnail_error

    def make_video_public(self, token, **request):
        assert request["video_id"] == VIDEO and self.processing == "succeeded"
        row = self.store.youtube_task(1)
        assert row["thumbnail_status"] == row["processing_status"] == "succeeded"
        assert row["public_status"] == "running" and not row["video_published_at_utc"]
        self.publications.append(request)
        if self.public_takes_effect:
            self.visibility = "public"
        if self.public_error:
            raise self.public_error

    def publish_comment(self, token, **request):
        assert request["video_id"] == VIDEO and self.visibility == "public"
        row = self.store.youtube_task(1)
        assert row["public_status"] == "succeeded" and row["video_state"] == "published"
        self.comments.append(request)
        if self.comment_error:
            raise self.comment_error
        return "offline_comment_1"


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.sleep = mock.patch('features.youtube_auto_publish.engine.time.sleep').start()
        self.addCleanup(mock.patch.stopall)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.covers = self.root / "covers"
        self.covers.mkdir()
        self.cover = self.covers / "approved.png"
        Image.new("RGB", (640, 360), "navy").save(self.cover)
        self.store = DramaSynthesisStore(self.root / "ledger.sqlite")
        self.store.ensure_storage()
        self.client, self.media = Client(self.store), Media()
        self.engine = ReviewedYouTubePublishEngine(self.store, Credentials(), self.client,
            work_root=self.root / "work", approved_cover_root=self.covers,
            allowed_source_hosts=["media.example.test"], media_executor=self.media)

    def request(self, **changes):
        value = dict(operation_id="reviewed-operation-1", preparation_id="prep1", source_material_id="90001",
            approved_cover_path=str(self.cover), approved_cover_sha256=hashlib.sha256(self.cover.read_bytes()).hexdigest(),
            job_id="a" * 32, content_id="drama1", app_id="1479", channel_local_id="12", channel_id=CHANNEL,
            youtube_account_id="11", source_kind="custom_source", source_url="https://media.example.test/video.mp4",
            title="Test", description_template="Test description", description_rendered="Test description",
            comment_text="Test comment", duplicate_confirmed=False, scopes=[COMMENT_SCOPE],
            operator_user_id="offline_actor", operator_name="Offline")
        value.update(changes)
        return value

    def enqueue(self, **changes):
        return self.store.enqueue_reviewed_youtube(**self.request(**changes))

    def tick(self):
        return self.engine.run_once("offline-worker")

    def row(self):
        return self.store.youtube_task(1)

    def sql(self, query, args=()):
        conn = sqlite3.connect(self.store.db_path)
        try:
            result = conn.execute(query, args).fetchall()
            conn.commit()
            return result
        finally:
            conn.close()

    def test_private_thumbnail_processing_public_then_comment(self):
        self.enqueue()
        self.assertEqual(self.tick()["status"], "submitted")
        self.assertEqual(self.client.begins[0]["privacy_status"], "private")
        self.assertFalse(self.client.publications)
        self.assertFalse(self.sql("SELECT * FROM drama_youtube_sync_outbox"))
        self.assertEqual(self.tick()["status"], "published")
        self.assertEqual(self.row()["reviewed_phase"], "complete")
        self.assertEqual(self.row()["comment_status"], "published")
        self.assertEqual(len(self.sql("SELECT * FROM drama_youtube_sync_outbox")), 3)
        self.assertEqual(self.media.uploaded, [1])

    def test_empty_comment_skips_external_call(self):
        self.enqueue(comment_text="")
        self.tick()
        self.tick()
        self.assertEqual(self.row()["comment_status"], "skipped")
        self.assertEqual(self.row()["reviewed_phase"], "complete")
        self.assertFalse(self.client.comments)

    def test_reviewed_sync_provenance_json_without_schema_expansion(self):
        self.enqueue(comment_text="")
        self.tick()
        self.tick()
        payload = json.loads(self.sql("SELECT payload_json FROM drama_youtube_sync_outbox WHERE entity_kind='video'")[0][0])
        self.assertEqual(validate_entity_payload("video", VIDEO, payload), payload)
        record = _video_record(payload)
        self.assertNotIn("workflow", record)
        self.assertEqual(json.loads(record["payload_json"])["source_material_id"], "90001")
        for changes in (dict(workflow="legacy"), dict(source_material_id=""), dict(source_kind="concat_video"), dict(privacy_status="unlisted")):
            with self.subTest(changes=changes), self.assertRaises(DramaSynthesisError):
                validate_entity_payload("video", VIDEO, dict(payload, **changes))
        unmarked = {key: value for key, value in payload.items() if key not in {"workflow", "source_material_id", "preparation_id"}}
        with self.assertRaises(DramaSynthesisError):
            validate_entity_payload("video", VIDEO, unmarked)

    def test_processing_waits_and_does_not_repeat_thumbnail(self):
        self.enqueue()
        self.tick()
        self.client.processing = "processing"
        self.assertEqual(self.tick()["status"], "processing")
        self.assertFalse(self.client.publications)
        self.client.processing = "succeeded"
        self.assertEqual(self.tick()["status"], "published")
        self.assertEqual(len(self.client.thumbnails), 1)

    def test_processing_failure_retains_private_video(self):
        self.enqueue()
        self.tick()
        self.client.processing = "failed"
        self.assertEqual(self.tick()["status"], "failed")
        self.assertEqual(self.row()["reviewed_phase"], "processing")
        self.assertEqual(self.row()["video_id"], VIDEO)
        self.assertEqual(self.client.visibility, "private")
        self.assertFalse(self.client.publications or self.client.comments)

    def test_thumbnail_failure_retries_only_same_thumbnail(self):
        self.enqueue()
        self.tick()
        self.client.thumbnail_error = YouTubeHTTPError("thumbnail_denied", "denied", status=403)
        self.assertEqual(self.tick()["status"], "failed")
        self.assertEqual(self.row()["reviewed_phase"], "thumbnail")
        self.assertFalse(self.client.publications or self.client.comments)
        self.client.thumbnail_error = None
        self.store.retry_reviewed_youtube(1)
        self.assertEqual(self.tick()["status"], "published")
        self.assertEqual(len(self.client.begins), 1)
        self.assertEqual(self.media.uploaded, [1])

    def test_public_failure_retries_only_original_video(self):
        self.enqueue()
        self.tick()
        self.client.public_error = YouTubeHTTPError("youtube_public_update_failed", "denied", status=403)
        self.client.public_takes_effect = False
        self.assertEqual(self.tick()["status"], "failed")
        self.assertEqual(self.row()["reviewed_phase"], "public")
        self.assertFalse(self.client.comments)
        self.client.public_error = None
        self.client.public_takes_effect = True
        self.store.retry_reviewed_youtube(1)
        self.assertEqual(self.tick()["status"], "published")
        self.assertEqual(len(self.client.begins), 1)
        self.assertEqual(len(self.client.thumbnails), 1)

    def test_public_unknown_reconciles_by_read(self):
        self.enqueue()
        self.tick()
        self.client.public_error = YouTubeHTTPError("youtube_public_update_unknown", "timeout", unknown=True)
        self.assertEqual(self.tick()["status"], "published")
        self.assertEqual(len(self.client.publications), 1)

    def test_public_unknown_private_holds_until_explicit_retry(self):
        self.enqueue()
        self.tick()
        self.client.public_error = YouTubeHTTPError("youtube_public_update_unknown", "timeout", unknown=True)
        self.client.public_takes_effect = False
        self.assertEqual(self.tick()["status"], "unknown")
        self.assertEqual(self.tick()["status"], "no_pending")
        self.assertFalse(self.client.comments)
        self.client.visibility = "public"
        self.store.retry_reviewed_youtube(1)
        self.assertEqual(self.tick()["status"], "published")
        self.assertEqual(len(self.client.publications), 1)

    def test_comment_definite_failure_retry_never_reuploads(self):
        self.enqueue()
        self.tick()
        self.client.comment_error = YouTubeHTTPError("youtube_comment_failed", "denied", status=403)
        self.assertEqual(self.tick()["status"], "failed")
        self.assertEqual(self.row()["video_state"], "published")
        self.client.comment_error = None
        self.store.retry_reviewed_youtube(1)
        self.assertEqual(self.tick()["status"], "published")
        self.assertEqual(len(self.client.begins), 1)
        self.assertEqual(len(self.client.thumbnails), 1)
        self.assertEqual(len(self.client.publications), 1)

    def test_unknown_comment_cannot_retry(self):
        self.enqueue()
        self.tick()
        self.client.comment_error = YouTubeHTTPError("youtube_comment_unknown", "timeout", unknown=True)
        self.assertEqual(self.tick()["status"], "unknown")
        with self.assertRaises(DramaSynthesisError):
            self.store.retry_reviewed_youtube(1)
        self.assertEqual(self.tick()["status"], "no_pending")
        self.assertEqual(len(self.client.comments), 1)

    def test_insert_unknown_cannot_create_replacement(self):
        self.enqueue()
        self.client.begin_error = YouTubeHTTPError("youtube_resumable_create_unknown", "timeout", unknown=True)
        self.assertEqual(self.tick()["status"], "unknown")
        with self.assertRaises(DramaSynthesisError):
            self.store.retry_reviewed_youtube(1)
        self.assertEqual(self.tick()["status"], "no_pending")
        self.assertEqual(len(self.client.begins), 1)

    def test_definite_insert_rejection_can_retry_after_fix(self):
        self.enqueue()
        self.client.begin_error = YouTubeHTTPError("youtube_resumable_create_failed", "denied", status=403)
        self.assertEqual(self.tick()["status"], "failed")
        self.store.retry_reviewed_youtube(1)
        self.client.begin_error = None
        self.assertEqual(self.tick()["status"], "submitted")
        self.assertEqual(self.media.uploaded, [1])

    def test_definite_insert_retry_cannot_change_frozen_source(self):
        self.enqueue()
        self.client.begin_error = YouTubeHTTPError("youtube_resumable_create_failed", "denied", status=403)
        self.tick()
        self.store.retry_reviewed_youtube(1)
        self.client.begin_error = None
        self.media.sha = "a" * 64
        self.assertEqual(self.tick()["status"], "unknown")
        self.assertEqual(len(self.client.begins), 1)

    def test_unknown_upload_query_denial_stays_unknown(self):
        self.enqueue()
        self.media.error = YouTubeHTTPError("youtube_upload_unknown", "timeout", unknown=True)
        self.client.query_result = YouTubeHTTPError("youtube_upload_failed", "query denied", status=403)
        self.assertEqual(self.tick()["status"], "unknown")
        self.assertEqual(self.row()["unknown_outcome"], 1)
        with self.assertRaises(DramaSynthesisError):
            self.enqueue(preparation_id="replacement", operation_id="replacement-operation")

    def test_known_upload_failure_with_session_blocks_replacement(self):
        self.enqueue()
        self.media.error = YouTubeHTTPError("youtube_upload_failed", "denied", status=403)
        self.assertEqual(self.tick()["status"], "failed")
        with self.assertRaises(DramaSynthesisError):
            self.enqueue(preparation_id="replacement", operation_id="replacement-operation", duplicate_confirmed=True)

    def test_interrupted_comment_is_not_sent_twice(self):
        self.enqueue()
        self.tick()
        self.client.comment_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.tick()
        self.sql("UPDATE drama_youtube_publish SET lease_expires_at_utc='2000-01-01T00:00:00Z'")
        self.client.comment_error = None
        self.assertEqual(self.tick()["status"], "no_pending")
        self.assertEqual(self.row()["comment_status"], "unknown")
        self.assertEqual(len(self.client.comments), 1)

    def test_crash_after_comment_receipt_stays_comment_unknown(self):
        self.enqueue()
        self.tick()
        with mock.patch.object(self.store, "comment_published", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.tick()
        self.sql("UPDATE drama_youtube_publish SET lease_expires_at_utc='2000-01-01T00:00:00Z'")
        self.assertEqual(self.tick()["status"], "no_pending")
        self.assertEqual(self.row()["comment_status"], "unknown")
        self.assertEqual(self.row()["reviewed_phase"], "comment")
        self.assertEqual(len(self.client.comments), 1)

    def test_early_public_is_fenced_without_thumbnail_or_comment(self):
        self.enqueue()
        self.tick()
        self.client.visibility = "public"
        self.assertEqual(self.tick()["status"], "unknown")
        self.assertFalse(self.client.thumbnails or self.client.comments or self.client.publications)

    def test_public_success_response_still_requires_readback(self):
        self.enqueue()
        self.tick()
        self.client.public_takes_effect = False
        self.assertEqual(self.tick()["status"], "unknown")
        self.assertEqual(self.row()["error_code"], "youtube_public_readback_unknown")
        self.assertEqual(self.sleep.call_args_list, [mock.call(2), mock.call(5), mock.call(10)])
        self.assertEqual(self.tick()["status"], "no_pending")
        self.assertFalse(self.client.comments)
        self.client.visibility = "public"
        self.store.retry_reviewed_youtube(1)
        self.assertEqual(self.tick()["status"], "published")
        self.assertEqual(len(self.client.publications), 1)

    def test_public_propagation_delay_recovers_without_duplicate_writes(self):
        self.enqueue()
        self.tick()
        original = self.client.read_reviewed_video_state
        reads_after_write = []
        def delayed(*args, **kwargs):
            state = original(*args, **kwargs)
            if self.client.publications:
                reads_after_write.append(1)
                if len(reads_after_write) <= 2:
                    state['visibility'] = 'private'
            return state
        with mock.patch.object(self.client, 'read_reviewed_video_state', side_effect=delayed):
            self.assertEqual(self.tick()['status'], 'published')
        self.assertEqual(self.sleep.call_args_list, [mock.call(2), mock.call(5)])
        self.assertEqual(len(self.client.publications), 1)
        self.assertEqual(len(self.client.comments), 1)
        self.assertEqual(self.media.uploaded, [1])

    def test_transient_read_failure_after_public_update_is_read_only_retried(self):
        self.enqueue()
        self.tick()
        original = self.client.read_reviewed_video_state
        failures = []
        def transient(*args, **kwargs):
            if self.client.publications and not failures:
                failures.append(1)
                raise YouTubeHTTPError('youtube_processing_check_failed', 'temporary', retryable=True)
            return original(*args, **kwargs)
        with mock.patch.object(self.client, 'read_reviewed_video_state', side_effect=transient):
            self.assertEqual(self.tick()['status'], 'published')
        self.assertEqual(len(self.client.publications), 1)
        self.assertEqual(len(self.client.comments), 1)

    def test_missing_video_after_update_stays_fenced_without_polling_or_comment(self):
        self.enqueue()
        self.tick()
        original = self.client.read_reviewed_video_state
        def missing(*args, **kwargs):
            if self.client.publications:
                raise YouTubeHTTPError('youtube_video_reconcile_unknown', 'missing', unknown=True)
            return original(*args, **kwargs)
        with mock.patch.object(self.client, 'read_reviewed_video_state', side_effect=missing):
            self.assertEqual(self.tick()['status'], 'unknown')
        self.assertEqual(self.tick()['status'], 'no_pending')
        self.assertFalse(self.client.comments)
        self.sleep.assert_not_called()
        self.assertEqual(self.media.uploaded, [1])

    def test_stale_worker_cannot_commit_thumbnail_receipt(self):
        self.enqueue()
        self.tick()
        def steal_lease(*args, **kwargs):
            self.sql("UPDATE drama_youtube_publish SET lease_owner='new-owner',lease_generation=lease_generation+1")
        self.client.set_thumbnail = steal_lease
        self.assertEqual(self.tick()["status"], "stale_claim")
        self.assertEqual(self.row()["thumbnail_status"], "running")
        self.assertFalse(self.client.publications)

    def test_interrupted_thumbnail_repeats_same_approved_bytes_only(self):
        self.enqueue()
        self.tick()
        self.client.thumbnail_error = RuntimeError("offline crash")
        self.assertEqual(self.tick()["status"], "failed")
        self.assertEqual(self.row()["unknown_outcome"], 0)
        self.store.retry_reviewed_youtube(1)
        self.client.thumbnail_error = None
        self.assertEqual(self.tick()["status"], "published")
        self.assertEqual(self.client.thumbnails[0], self.client.thumbnails[1])
        self.assertEqual(len(self.client.begins), 1)

    def test_crash_after_insert_intent_fences_claim(self):
        self.enqueue()
        task = self.store.claim_reviewed_youtube("crashed", "2000-01-01T00:00:00Z")
        self.store.advance_youtube(1, "uploading", worker_id="crashed", lease_generation=task["lease_generation"])
        self.store.mark_reviewed_upload_intent(1, worker_id="crashed", lease_generation=task["lease_generation"])
        self.assertEqual(self.tick()["status"], "no_pending")
        self.assertEqual(self.row()["status"], "unknown")
        self.assertFalse(self.client.begins)

    def test_changed_media_cannot_resume_original_session(self):
        self.enqueue()
        self.media.error = YouTubeHTTPError("temporary", "retry", retryable=True)
        self.assertEqual(self.tick()["status"], "queued")
        self.media.error = None
        self.media.sha = "a" * 64
        self.assertEqual(self.tick()["status"], "unknown")
        self.assertEqual(len(self.client.begins), 1)
        self.assertEqual(self.row()["error_code"], "youtube_media_source_changed")

    def test_expired_session_never_creates_another(self):
        self.enqueue()
        self.media.error = YouTubeHTTPError("temporary", "retry", retryable=True)
        self.tick()
        self.media.error = None
        self.client.query_result = {"state": "expired"}
        self.assertEqual(self.tick()["status"], "unknown")
        self.assertEqual(len(self.client.begins), 1)

    def test_changed_approval_fails_before_video_upload(self):
        self.enqueue()
        self.cover.write_bytes(b"modified")
        self.assertEqual(self.tick()["status"], "failed")
        self.assertFalse(self.client.begins or self.media.uploaded)

    def test_approved_local_cover_preserves_non_wide_composition(self):
        Image.new("RGB", (640, 640), "navy").save(self.cover)
        self.enqueue(comment_text="")
        self.tick()
        self.assertEqual(self.tick()["status"], "published")
        self.assertEqual(self.client.thumbnails[0]["content"], self.cover.read_bytes())

    def test_outside_private_cover_root_fails_before_upload(self):
        outside = self.root / "outside.png"
        outside.write_bytes(self.cover.read_bytes())
        self.enqueue(approved_cover_path=str(outside))
        self.assertEqual(self.tick()["status"], "failed")
        self.assertFalse(self.client.begins)

    def test_scope_missing_and_wrong_source_rejected(self):
        for changes in (dict(scopes=["youtube.upload", "youtube.readonly"]), dict(source_kind="concat_video")):
            with self.subTest(changes=changes), self.assertRaises(DramaSynthesisError):
                self.enqueue(**changes)

    def test_reauthorization_resumes_same_private_video(self):
        self.enqueue()
        self.tick()
        with mock.patch.object(self.engine.credentials, "credential", side_effect=DramaSynthesisError("youtube_channel_not_eligible", "re-authorize", 409)):
            self.assertEqual(self.tick()["status"], "failed")
        self.assertEqual(self.row()["unknown_outcome"], 0)
        self.store.retry_reviewed_youtube(1)
        self.assertEqual(self.tick()["status"], "published")
        self.assertEqual(len(self.client.begins), 1)

    def test_approval_immutable_replay_and_new_cover_conflict(self):
        row = self.enqueue()
        self.assertEqual(row["id"], self.enqueue()["id"])
        with self.assertRaises(DramaSynthesisError):
            self.enqueue(approved_cover_sha256="b" * 64)

    def test_shared_id_space_and_both_claims_isolated(self):
        reviewed = self.enqueue()
        legacy_request = {key: value for key, value in self.request().items() if key not in
                          {"preparation_id", "source_material_id", "approved_cover_path", "approved_cover_sha256"}}
        legacy_request.update(operation_id="legacy-operation-1", source_kind="concat_video")
        legacy = self.store.enqueue_youtube(**legacy_request)
        self.assertNotEqual(reviewed["id"], legacy["id"])
        self.assertEqual(self.store.claim_youtube("old", EXPIRY)["id"], legacy["id"])
        self.assertEqual(self.store.claim_reviewed_youtube("new", EXPIRY)["id"], reviewed["id"])
        self.assertFalse(self.store.claim_youtube("old2", EXPIRY))
        self.assertEqual([r["id"] for r in self.store.youtube_tasks_for_job("a" * 32)], [legacy["id"]])

    def test_concurrent_enqueue_allocates_distinct_ids(self):
        def enqueue(index):
            return self.enqueue(preparation_id="p" + str(index), source_material_id=str(index),
                                operation_id="concurrent-operation-" + str(index))["id"]
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(enqueue, range(12)))
        self.assertEqual(len(set(ids)), 12)

    def test_concurrent_store_migrations_are_atomic(self):
        path = self.root / "concurrent-schema.sqlite"
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: DramaSynthesisStore(path).ensure_storage(), range(12)))
        store = DramaSynthesisStore(path)
        self.assertEqual(store.enqueue_reviewed_youtube(**self.request())["workflow"], "reviewed_thumbnail")

    def test_same_material_pending_cannot_be_replaced(self):
        self.enqueue()
        with self.assertRaises(DramaSynthesisError):
            self.enqueue(preparation_id="prep2", operation_id="other-operation-2", duplicate_confirmed=True)

    def test_legacy_retry_cannot_modify_reviewed(self):
        self.enqueue()
        with self.assertRaises(DramaSynthesisError):
            self.store.retry_youtube_comment(1)

    def test_core_publish_guard_and_stale_reviewed_lease(self):
        self.enqueue()
        task = self.store.claim_reviewed_youtube("new", EXPIRY)
        with self.assertRaises(DramaSynthesisError):
            self.store.video_published(1, VIDEO, worker_id="new", lease_generation=task["lease_generation"])
        with self.assertRaises(DramaSynthesisError):
            self.store.advance_reviewed_youtube(1, "public", worker_id="stale", lease_generation=0, public_status="succeeded")


class HTTPTests(unittest.TestCase):
    def client(self, payload=None, status=200):
        response = mock.Mock(status_code=status, headers={"Location": "https://www.googleapis.com/upload/session"})
        response.json.return_value = payload or {}
        session = mock.Mock()
        session.get.return_value = session.put.return_value = session.post.return_value = response
        return ReviewedYouTubeHTTPClient(session_factory=lambda: session), session

    def test_only_private_upload_and_no_subscriber_notification(self):
        client, session = self.client()
        client.begin_resumable("OFFLINE", title="t", description="d", size=3, privacy_status="private")
        args, kwargs = session.post.call_args
        self.assertEqual(kwargs["json"]["status"], {"privacyStatus": "private"})
        self.assertEqual(parse_qs(urlsplit(args[0]).query)["notifySubscribers"], ["false"])
        for privacy in ("public", "unlisted"):
            with self.assertRaises(YouTubeHTTPError):
                client.begin_resumable("OFFLINE", title="t", description="d", size=3, privacy_status=privacy)
        self.assertEqual(session.post.call_count, 1)

    def test_success_without_session_identity_is_unknown(self):
        client, session = self.client()
        session.post.return_value.headers = {}
        with self.assertRaises(YouTubeHTTPError) as result:
            client.begin_resumable("OFFLINE", title="t", description="d", size=3, privacy_status="private")
        self.assertEqual(result.exception.code, "youtube_resumable_create_unknown")
        self.assertTrue(result.exception.unknown)

    def test_definite_initiation_denial_retains_retryable_identity(self):
        client, _ = self.client(status=403)
        with self.assertRaises(YouTubeHTTPError) as result:
            client.begin_resumable("OFFLINE", title="t", description="d", size=3, privacy_status="private")
        self.assertEqual(result.exception.code, "youtube_resumable_create_failed")
        self.assertFalse(result.exception.unknown)

    def test_public_update_preserves_writable_owner_status(self):
        client, session = self.client()
        original = dict(privacyStatus="private", uploadStatus="processed", license="creativeCommon", embeddable=False,
            publicStatsViewable=False, selfDeclaredMadeForKids=False, containsSyntheticMedia=True, publishAt="tomorrow", madeForKids=False)
        client.make_video_public("OFFLINE", video_id=VIDEO, preserved_status=original)
        args, kwargs = session.put.call_args
        self.assertEqual(args[0], "https://www.googleapis.com/youtube/v3/videos?part=status")
        self.assertEqual(kwargs["json"], {"id": VIDEO, "status": dict(privacyStatus="public", license="creativeCommon",
            embeddable=False, publicStatsViewable=False, selfDeclaredMadeForKids=False, containsSyntheticMedia=True)})
        self.assertFalse(kwargs["allow_redirects"])

    def test_read_requires_matching_owner_video_processing(self):
        item = {"id": VIDEO, "snippet": {"channelId": CHANNEL}, "status": {"privacyStatus": "private", "uploadStatus": "processed"},
                "processingDetails": {"processingStatus": "succeeded"}}
        client, session = self.client({"items": [item]})
        state = client.read_reviewed_video_state("OFFLINE", VIDEO, expected_channel_id=CHANNEL)
        self.assertEqual(state["state"], "succeeded")
        self.assertEqual(state["visibility"], "private")
        self.assertEqual(parse_qs(urlsplit(session.get.call_args.args[0]).query)["part"], ["snippet,status,processingDetails"])
        for change in ({"id": "mismatch"}, {"snippet": {"channelId": "other"}}, {"processingDetails": None}):
            client, _ = self.client({"items": [dict(item, **change)]})
            with self.subTest(change=change), self.assertRaises(YouTubeHTTPError):
                client.read_reviewed_video_state("OFFLINE", VIDEO, expected_channel_id=CHANNEL)

    def test_thumbnail_endpoint_raw_approved_bytes(self):
        client, session = self.client({"items": [{"high": {"url": "https://i.ytimg.com/example"}}]})
        client.set_thumbnail("OFFLINE", video_id=VIDEO, content=b"exact-approved-bytes", mime_type="image/png")
        args, kwargs = session.post.call_args
        self.assertEqual(urlsplit(args[0]).path, "/upload/youtube/v3/thumbnails/set")
        self.assertEqual(parse_qs(urlsplit(args[0]).query), {"videoId": [VIDEO], "uploadType": ["media"]})
        self.assertEqual(kwargs["data"], b"exact-approved-bytes")
        self.assertEqual(kwargs["headers"]["Content-Type"], "image/png")


if __name__ == "__main__":
    unittest.main()
