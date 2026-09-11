#!/usr/bin/env python3
"""Offline contracts for native scheduling and leased, readback-confirmed controls."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.test_youtube_auto_engine as fixtures
from features.drama_synthesis.core import DramaSynthesisError, DramaSynthesisStore, normalize_youtube_publish_at
from features.drama_synthesis.youtube import YouTubeHTTPError
from features.youtube_auto_publish.engine import ReviewedYouTubeHTTPClient, ReviewedYouTubePublishEngine


class Client(fixtures.Client):
    def __init__(self, store):
        super().__init__(store)
        self.remote_time = ""
        self.schedule_writes, self.cancellations, self.reads = [], [], []
        self.schedule_error = self.cancel_error = None
        self.schedule_takes_effect = self.cancel_takes_effect = True

    def read_reviewed_video_state(self, *args, **kwargs):
        state = super().read_reviewed_video_state(*args, **kwargs)
        if self.remote_time:
            state["preserved_status"]["publishAt"] = self.remote_time
        self.reads.append(state)
        return state

    def set_video_schedule(self, token, **request):
        row = self.store.youtube_task(1)
        assert row["thumbnail_status"] == row["processing_status"] == "succeeded"
        assert row["schedule_status"] in {"arming", "control_running"}
        assert row["schedule_command_status"] == "running"
        self.schedule_writes.append(request)
        if self.schedule_takes_effect:
            self.remote_time = request["publish_at"]
        if self.schedule_error:
            raise self.schedule_error

    def cancel_video_schedule(self, token, **request):
        assert self.store.youtube_task(1)["schedule_command_status"] == "running"
        self.cancellations.append(request)
        if self.cancel_takes_effect:
            self.remote_time = ""
        if self.cancel_error:
            raise self.cancel_error

    def make_video_public(self, token, **request):
        super().make_video_public(token, **request)
        if self.public_takes_effect:
            self.remote_time = ""


class ScheduleTests(unittest.TestCase):
    request = fixtures.EngineTests.request
    enqueue = fixtures.EngineTests.enqueue
    tick = fixtures.EngineTests.tick
    row = fixtures.EngineTests.row
    sql = fixtures.EngineTests.sql

    def setUp(self):
        fixtures.EngineTests.setUp(self)
        self.now = "2030-01-02T10:00:00Z"
        self.target = "2030-01-02T11:00:00Z"
        mock.patch("features.drama_synthesis.core.utc_now", side_effect=lambda: self.now).start()
        mock.patch("features.youtube_auto_publish.engine.utc_now", side_effect=lambda: self.now).start()
        self.client = Client(self.store)
        self.engine = ReviewedYouTubePublishEngine(self.store, fixtures.Credentials(), self.client,
            work_root=self.root / "work", approved_cover_root=self.covers,
            allowed_source_hosts=["media.example.test"], media_executor=self.media)

    def due(self, seconds=301):
        self.now = (datetime.fromisoformat(self.now.replace("Z", "+00:00")) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    def arm(self):
        self.enqueue(publish_at=self.target)
        self.assertEqual(self.tick()["status"], "submitted")
        self.assertEqual(self.tick()["status"], "scheduled")

    def command(self, action, target="", **changes):
        values = dict(action=action, publish_at=target, expected_version=self.row()["schedule_version"], operation_id="schedule-control-" + action)
        values.update(changes)
        return self.store.request_reviewed_youtube_schedule(1, **values)

    def test_native_schedule_never_immediate_and_comment_only_after_public(self):
        self.arm()
        self.assertEqual(self.row()["schedule_confirmed_publish_at"], self.target)
        self.assertEqual(len(self.client.schedule_writes), 1)
        self.assertFalse(self.client.publications or self.client.comments)
        self.assertFalse(self.sql("SELECT * FROM drama_youtube_sync_outbox"))
        self.assertEqual(self.tick()["status"], "no_pending")
        self.now = self.target
        self.assertEqual(self.tick()["status"], "scheduled")
        self.assertFalse(self.client.publications or self.client.comments)
        self.due(15)
        self.client.visibility = "public"
        self.assertEqual(self.tick()["status"], "published")
        self.assertEqual(len(self.client.comments), 1)
        self.assertEqual(len(self.client.schedule_writes), 1)
        self.assertEqual(len(self.sql("SELECT * FROM drama_youtube_sync_outbox")), 3)
        self.assertEqual(self.tick()["status"], "no_pending")

    def test_blank_schedule_preserves_immediate_lane(self):
        self.enqueue()
        self.tick()
        self.assertEqual(self.tick()["status"], "published")
        self.assertEqual(len(self.client.publications), 1)
        self.assertFalse(self.client.schedule_writes)

    def test_expired_unarmed_never_uploads(self):
        self.enqueue(publish_at="2030-01-02T09:59:00Z")
        self.assertEqual(self.tick()["status"], "schedule_missed")
        self.assertFalse(self.client.begins or self.media.prepared)
        self.assertEqual(self.tick()["status"], "no_pending")

    def test_expired_processing_keeps_original_private_video(self):
        self.enqueue(publish_at=self.target)
        self.tick()
        self.client.processing = "processing"
        self.assertEqual(self.tick()["status"], "processing")
        self.now = self.target
        self.assertEqual(self.tick()["status"], "schedule_missed")
        self.assertEqual(self.row()["video_id"], fixtures.VIDEO)
        self.assertFalse(self.client.schedule_writes or self.client.publications or self.client.comments)

    def test_last_write_headroom_and_no_late_publication(self):
        self.enqueue(publish_at="2030-01-02T10:00:59Z")
        self.tick()
        self.assertEqual(self.tick()["status"], "schedule_missed")
        self.assertEqual(self.row()["error_code"], "youtube_schedule_too_close")
        self.assertFalse(self.client.schedule_writes or self.client.publications)

    def test_unknown_schedule_response_readback_confirms_without_repeat(self):
        self.client.schedule_error = YouTubeHTTPError("timeout", "unknown", unknown=True)
        self.arm()
        self.due()
        self.tick()
        self.assertEqual(len(self.client.schedule_writes), 1)

    def test_unknown_schedule_response_keeps_readonly_fence_and_stable_event(self):
        self.client.schedule_takes_effect = False
        self.client.schedule_error = YouTubeHTTPError("timeout", "unknown", unknown=True)
        self.enqueue(publish_at=self.target)
        self.tick()
        self.assertEqual(self.tick()["status"], "unknown")
        event_at = self.row()["schedule_event_at"]
        self.assertEqual(self.row()["schedule_status"], "reconciling")
        self.assertEqual(self.tick()["status"], "no_pending")
        self.due(61)
        self.assertEqual(self.tick()["status"], "unknown")
        self.assertEqual(self.row()["schedule_event_at"], event_at)
        self.assertEqual(len(self.client.schedule_writes), 1)
        with self.assertRaises(DramaSynthesisError) as error:
            self.command("cancel")
        self.assertEqual(error.exception.code, "youtube_schedule_busy")
        self.client.remote_time = self.target
        self.due(61)
        self.assertEqual(self.tick()["status"], "scheduled")
        self.assertEqual(len(self.client.schedule_writes), 1)

    def test_crash_after_schedule_intent_only_reads(self):
        self.enqueue(publish_at=self.target)
        self.tick()
        self.sql("UPDATE drama_youtube_publish SET thumbnail_status='succeeded',processing_status='succeeded',reviewed_phase='schedule',schedule_status='arming',schedule_command_status='running'")
        self.assertEqual(self.tick()["status"], "unknown")
        self.assertFalse(self.client.schedule_writes)
        self.client.remote_time = self.target
        self.due(61)
        self.assertEqual(self.tick()["status"], "scheduled")

    def test_reschedule_armed_confirms_on_same_video(self):
        self.arm()
        next_time = "2030-01-02T12:00:00Z"
        accepted = self.command("reschedule", next_time)
        self.assertEqual(accepted["status"], "schedule_pending")
        self.assertEqual(accepted["schedule_command_status"], "pending")
        self.assertEqual(self.tick()["status"], "scheduled")
        self.assertEqual(self.row()["schedule_confirmed_publish_at"], next_time)
        self.assertEqual(len(self.client.schedule_writes), 2)
        self.assertEqual(len(self.client.begins), 1)
        self.assertFalse(self.client.publications or self.client.comments)

    def test_cancel_armed_removes_platform_time_and_keeps_private(self):
        self.arm()
        self.command("cancel")
        self.assertEqual(self.tick()["status"], "cancelled")
        self.assertEqual(self.row()["schedule_command_status"], "confirmed")
        self.assertFalse(self.client.remote_time or self.client.comments or self.client.publications)
        self.assertEqual(self.client.visibility, "private")
        self.assertEqual(len(self.client.cancellations), 1)
        self.assertEqual(self.tick()["status"], "no_pending")

    def test_unknown_cancel_does_not_claim_success_or_repeat(self):
        self.arm()
        self.client.cancel_takes_effect = False
        self.client.cancel_error = YouTubeHTTPError("timeout", "unknown", unknown=True)
        self.command("cancel")
        self.assertEqual(self.tick()["status"], "unknown")
        self.due(61)
        self.assertEqual(self.tick()["status"], "unknown")
        self.assertEqual(len(self.client.cancellations), 1)
        self.client.remote_time = ""
        self.due(61)
        self.assertEqual(self.tick()["status"], "cancelled")

    def test_immediate_armed_releases_then_comments_once(self):
        self.arm()
        self.command("immediate")
        self.assertEqual(self.tick()["status"], "published")
        self.assertEqual(len(self.client.publications), 1)
        self.assertEqual(len(self.client.comments), 1)
        self.assertEqual(len(self.client.begins), 1)

    def test_unknown_immediate_control_never_repeats_put(self):
        self.arm()
        self.client.public_takes_effect = False
        self.client.public_error = YouTubeHTTPError("timeout", "unknown", unknown=True)
        self.command("immediate")
        self.assertEqual(self.tick()["status"], "unknown")
        self.due(61)
        self.assertEqual(self.tick()["status"], "unknown")
        self.assertEqual(len(self.client.publications), 1)
        self.assertFalse(self.client.comments)
        self.client.visibility = "public"
        self.due(61)
        self.assertEqual(self.tick()["status"], "published")
        self.assertEqual(len(self.client.publications), 1)

    def test_definite_reschedule_refusal_keeps_confirmed_original_schedule(self):
        self.arm()
        self.client.schedule_takes_effect = False
        self.client.schedule_error = YouTubeHTTPError("youtube_schedule_update_failed", "refused", status=403)
        self.command("reschedule", "2030-01-02T12:00:00Z")
        self.assertEqual(self.tick()["status"], "scheduled")
        self.assertEqual(self.row()["schedule_command_status"], "failed")
        self.assertEqual(self.row()["publish_at"], self.target)
        self.assertEqual(self.row()["schedule_confirmed_publish_at"], self.target)
        self.command("reschedule", "2030-01-02T12:00:00Z", expected_version=0)
        self.assertEqual(self.row()["schedule_version"], 1)

    def test_command_near_original_deadline_is_rejected(self):
        self.arm()
        self.now = "2030-01-02T10:59:30Z"
        with self.assertRaises(DramaSynthesisError) as error:
            self.command("cancel")
        self.assertEqual(error.exception.code, "youtube_schedule_too_close")
        self.assertFalse(self.client.cancellations)

    def test_delayed_command_does_not_privatize_an_already_public_video(self):
        self.arm()
        self.command("cancel")
        self.now = self.target
        self.client.visibility = "public"
        self.assertEqual(self.tick()["status"], "published")
        self.assertEqual(self.row()["schedule_command_status"], "failed")
        self.assertFalse(self.client.cancellations)

    def test_early_public_is_unknown_and_comments_blocked(self):
        self.arm()
        self.due()
        self.client.visibility = "public"
        self.assertEqual(self.tick()["status"], "unknown")
        self.assertFalse(self.client.comments)
        self.now = self.target
        self.assertEqual(self.tick()["status"], "no_pending")
        self.assertFalse(self.client.comments)

    def test_definite_immediate_refusal_leaves_old_schedule_editable(self):
        self.arm()
        self.client.public_takes_effect = False
        self.client.public_error = YouTubeHTTPError("youtube_public_update_failed", "refused", status=403)
        self.command("immediate")
        self.assertEqual(self.tick()["status"], "scheduled")
        self.assertEqual(self.row()["public_status"], "failed")
        self.assertEqual(self.row()["publish_at"], self.target)
        self.assertEqual(self.command("cancel")["status"], "schedule_pending")

    def test_read_failure_cannot_reinterpret_old_failed_cancel_as_new_intent(self):
        self.arm()
        self.client.cancel_takes_effect = False
        self.client.cancel_error = YouTubeHTTPError("youtube_schedule_update_failed", "refused", status=403)
        self.command("cancel")
        self.assertEqual(self.tick()["status"], "scheduled")
        self.assertEqual(self.row()["schedule_command_status"], "failed")
        self.due()
        with mock.patch.object(self.client, "read_reviewed_video_state", side_effect=YouTubeHTTPError("temporary", "read failed", retryable=True)):
            self.assertEqual(self.tick()["status"], "unknown")
        self.due(61)
        self.assertEqual(self.tick()["status"], "scheduled")
        self.assertEqual(len(self.client.cancellations), 1)

    def test_delayed_reschedule_cannot_send_a_past_publish_at(self):
        self.arm()
        self.command("reschedule", "2030-01-02T10:10:00Z")
        self.now = "2030-01-02T10:11:00Z"
        self.assertEqual(self.tick()["status"], "scheduled")
        self.assertEqual(self.row()["schedule_command_status"], "failed")
        self.assertEqual(self.row()["publish_at"], self.target)
        self.assertEqual(len(self.client.schedule_writes), 1)

    def test_no_video_cancel_is_local_and_idempotent(self):
        self.enqueue(publish_at=self.target)
        self.assertEqual(self.command("cancel")["status"], "cancelled")
        self.assertEqual(self.command("cancel", expected_version=0)["schedule_version"], 1)
        self.assertEqual(self.tick()["status"], "no_pending")
        self.assertFalse(self.client.begins or self.client.cancellations)

    def test_unarmed_reschedule_and_clear_then_immediate(self):
        self.enqueue(publish_at=self.target, schedule_version=4)
        self.command("reschedule", "2030-01-02T12:00:00Z")
        row = self.command("immediate")
        self.assertEqual(row["schedule_version"], 6)
        self.assertEqual(row["schedule_command_status"], "confirmed")
        self.assertEqual(row["publish_at"], "")
        self.tick()
        self.assertEqual(self.tick()["status"], "published")
        self.assertFalse(self.client.schedule_writes)

    def test_missed_known_video_reschedules_without_second_upload(self):
        self.enqueue(publish_at=self.target)
        self.tick()
        self.now = self.target
        self.assertEqual(self.tick()["status"], "schedule_missed")
        self.command("reschedule", "2030-01-02T12:00:00Z")
        self.assertEqual(self.tick()["status"], "scheduled")
        self.assertEqual(len(self.client.begins), 1)

    def test_schedule_cas_and_lease_exclude_racing_publication(self):
        self.enqueue(publish_at=self.target)
        self.store.claim_reviewed_youtube("worker", fixtures.EXPIRY)
        with self.assertRaises(DramaSynthesisError) as error:
            self.command("cancel")
        self.assertEqual(error.exception.code, "youtube_schedule_busy")
        self.sql("UPDATE drama_youtube_publish SET lease_owner='',lease_expires_at_utc=''")
        self.command("reschedule", "2030-01-02T12:00:00Z")
        with self.assertRaises(DramaSynthesisError) as error:
            self.command("cancel", expected_version=0)
        self.assertEqual(error.exception.code, "youtube_schedule_version_conflict")

    def test_timezone_normalization_and_naive_rejection(self):
        self.assertEqual(normalize_youtube_publish_at("2030-01-02T19:00:00+08:00"), self.target)
        for value in ["2030-01-02T11:00", "bad", 3, {}, True]:
            with self.subTest(value=value), self.assertRaises(DramaSynthesisError):
                normalize_youtube_publish_at(value)
        with self.assertRaises(DramaSynthesisError):
            normalize_youtube_publish_at("2030-01-01T00:00:00Z", require_future=True)

    def test_additive_migration_and_handoff_lookup(self):
        task = self.enqueue(publish_at=self.target)
        self.store.ensure_storage()
        recovered = self.store.get_reviewed_youtube_by_preparation(task["preparation_id"])
        self.assertEqual(recovered["id"], task["id"])
        self.assertEqual(recovered["publish_at"], self.target)
        self.assertEqual(self.enqueue(publish_at=self.target)["id"], task["id"])
        with self.assertRaises(DramaSynthesisError):
            self.enqueue(publish_at="2030-01-02T12:00:00Z")

    def test_existing_unscheduled_row_migrates_with_unchanged_video_facts(self):
        task = self.enqueue()
        self.tick()
        before = self.row()
        new_columns = [name for name in before if name.startswith("schedule_") or name in {"publish_at", "initial_publish_at"}]
        for name in new_columns:
            self.sql("ALTER TABLE drama_youtube_publish DROP COLUMN " + name)
        self.store.ensure_storage()
        restored = self.row()
        self.assertEqual(restored["publish_at"], "")
        self.assertEqual(restored["schedule_status"], "none")
        for key in ["id", "video_id", "resumable_session_uri", "video_attempt_count", "status", "video_state"]:
            self.assertEqual(restored[key], before[key])
        self.assertEqual(self.tick()["status"], "published")

    def test_legacy_worker_never_claims_scheduled_lane(self):
        self.arm()
        self.assertIsNone(self.store.claim_youtube("legacy", fixtures.EXPIRY))


class ScheduleHTTPTests(unittest.TestCase):
    def setUp(self):
        self.session = mock.Mock()
        self.session.put.return_value.status_code = 200
        self.client = ReviewedYouTubeHTTPClient(session_factory=lambda: self.session, timeout=120)
        self.clock = mock.patch("features.youtube_auto_publish.engine.utc_now", return_value="2030-01-02T10:00:00Z")
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_schedule_preserves_owner_writable_flags_and_excludes_readonly_fields(self):
        self.client.set_video_schedule("OFFLINE", video_id=fixtures.VIDEO, publish_at="2030-01-02T11:00:00Z",
            preserved_status=dict(privacyStatus="private", uploadStatus="processed", embeddable=False,
                license="creativeCommon", selfDeclaredMadeForKids=False, containsSyntheticMedia=True))
        request = self.session.put.call_args.kwargs
        self.assertEqual(request["timeout"], 30)
        self.assertFalse(request["allow_redirects"])
        self.assertEqual(request["json"]["status"], dict(privacyStatus="private", publishAt="2030-01-02T11:00:00Z",
            embeddable=False, license="creativeCommon", selfDeclaredMadeForKids=False, containsSyntheticMedia=True))

    def test_cancel_omits_publish_at_and_retains_private(self):
        self.client.cancel_video_schedule("OFFLINE", video_id=fixtures.VIDEO,
            preserved_status=dict(privacyStatus="private", publishAt="2030-01-02T11:00:00Z", embeddable=False))
        self.assertEqual(self.session.put.call_args.kwargs["json"]["status"], dict(privacyStatus="private", embeddable=False))

    def test_past_or_too_close_never_puts(self):
        for value in ["", "2030-01-02T09:59:00Z", "2030-01-02T10:00:59Z", "2030-01-02T10:01:00Z"]:
            with self.subTest(value=value), self.assertRaises(YouTubeHTTPError):
                self.client.set_video_schedule("OFFLINE", video_id=fixtures.VIDEO, publish_at=value,
                    preserved_status={"privacyStatus": "private"})
        self.session.put.assert_not_called()

    def test_public_video_cannot_be_privatized_by_schedule_or_cancel(self):
        with self.assertRaises(YouTubeHTTPError):
            self.client.cancel_video_schedule("OFFLINE", video_id=fixtures.VIDEO, preserved_status={"privacyStatus": "public"})
        self.session.put.assert_not_called()

    def test_cancel_rechecks_original_deadline_at_last_http_gate(self):
        with self.assertRaises(YouTubeHTTPError) as error:
            self.client.cancel_video_schedule("OFFLINE", video_id=fixtures.VIDEO,
                preserved_status={"privacyStatus": "private", "publishAt": "2030-01-02T10:00:40Z"})
        self.assertEqual(error.exception.code, "youtube_schedule_too_close")
        self.session.put.assert_not_called()


if __name__ == "__main__":
    unittest.main()
