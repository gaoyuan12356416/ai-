"""Offline Excel free-resource preview, reservation, hydration and restart checks."""
import contextlib
import copy
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts import service as posts
from features.x_video_publish.core import PersonalStore
from features.x_video_publish.service import PublishingService
from scripts import x_post_manual_runner as runner
from scripts.test_x_post_schedule_runner import make_config

EPISODE = "a0e543eb91a2afacfc5077a6942c222c"
OTHER_EPISODE = "b0e543eb91a2afacfc5077a6942c222c"
ACTOR = {"tenant_key": "test-tenant", "user_id": "test-user", "name": "Test operator"}
NOW = datetime(2030, 1, 2, 1, 0, tzinfo=timezone.utc)


def episode(mid=EPISODE):
    return {
        "id": mid, "material_id": mid, "material_key": mid,
        "source_kind": "free_episode", "resource_id": mid, "episode_number": 2,
        "content_id": "content-17", "series_code": "series-17",
        "drama_name": "Episode story", "description": "A complete story description.",
        "drama_description": "A complete story description.", "language": "en",
        "material_language": "en", "material_name": "Episode 2",
        "material_url": "https://media.example.test/" + mid + ".mp4",
        "source_duration": 50, "duration_seconds": 50, "duration_status": "verified", "media_kind": "video",
        "source_type": "material", "pool_item_id": None, "pool_created_at": "",
        "tag": "Drama", "name_tag": "#Episode_story", "spend": 0,
        "facebook_violation_count": 0, "tiktok_violation_count": 0,
        "twitter_violation_count": 0, "resource_audit_count": 0, "dangerous_tag_count": 0,
    }


class ExcelEpisodePipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "posts.sqlite3"
        self.store = PersonalStore(self.path)
        self.legacy = posts.XPostStore(self.path)
        self.accounts = {str(i): {
            "id": i, "x_user_id": str(2704526255 + i), "username": "test%d" % i,
            "display_name": "Test %d" % i, "owner_tenant_key": ACTOR["tenant_key"],
            "owner_user_id": ACTOR["user_id"], "status": "active", "publish_approved": True,
            "publish_eligible": True, "drama_language": "en", "subscription_type": "none",
        } for i in (1, 2)}
        self.materials = {EPISODE: episode(), OTHER_EPISODE: episode(OTHER_EPISODE),
                          "501": {"material_id": "501", "drama_name": "Numeric material",
                                  "drama_description": "Description", "language": "en"}}

    def tearDown(self):
        self.tmp.cleanup()

    def preview(self, rows=None, source="excel"):
        return self.store.preview(ACTOR, {"source": source, "rows": rows or [
            {"account_id": "1", "material_id": EPISODE, "caption": "{name} {url}"}]},
            self.accounts, self.materials, now=NOW)

    def commit(self, preview=None, materials=None, key="episode-test-001"):
        return self.store.commit(ACTOR, (preview or self.preview())["preview_token"], key,
                                 self.accounts, materials or self.materials, now=NOW)

    def candidates(self, run, candidate=None):
        connection = mock.Mock()
        resolved = {"items": {EPISODE: candidate or episode()}, "errors": {}}
        with mock.patch.object(runner, "_open_source_connection", return_value=connection), \
                mock.patch("features.x_video_publish.sources.SourceRepository.resolve", return_value=resolved), \
                mock.patch.dict("os.environ", {"PIPELINE_X_DISPATCH_ENABLED": "1"}):
            planned = runner._manual_candidates(make_config(self.tmp.name), run,
                [self.accounts["1"]], now=NOW)
        connection.close.assert_called_once()
        return planned

    def plan(self, task, candidate=None):
        run = runner._manual_identity(self.legacy.get_manual_run(task["manual_run_id"]))
        planned = self.candidates(run, candidate)
        self.assertFalse(planned.preflight_failures)
        with mock.patch.dict("os.environ", {"PIPELINE_X_DISPATCH_ENABLED": "1"}):
            return self.legacy.create_manual_plan(run["id"], planned, preflight_failures=[])

    def test_mixed_rows_normalize_case_skip_bad_row_and_duplicate(self):
        rows = [{"account_id": "1", "material_id": EPISODE.upper()},
                {"account_id": "1", "material_id": "bad-id"},
                {"account_id": "2", "material_id": "501"},
                {"account_id": "1", "material_id": EPISODE}]
        preview = self.preview(rows)
        self.assertEqual(preview["valid_count"], 2)
        self.assertEqual(preview["rows"][0]["material_id"], EPISODE)
        self.assertEqual(preview["rows"][3]["error_code"], "x_video_duplicate_row")
        batch = self.commit(preview)
        self.assertEqual([t["material_id"] for t in batch["tasks"]], [EPISODE, "501"])

    def test_service_preview_resolves_normalized_episode_and_preserves_source_error(self):
        oauth = SimpleNamespace(POST_DB_PATH=self.path, INTERNAL_TOKEN="inert",
            list_accounts=lambda *_: {"items": list(self.accounts.values())})
        source = mock.Mock()
        source.resolve.return_value = {"items": {EPISODE: episode()}, "errors": {
            OTHER_EPISODE: {"code": "episode_not_free", "message": "不是免费集"}}}
        service = PublishingService(oauth, source=source, enabled=True)
        result = service.preview(ACTOR, {"rows": [
            {"row_number": 2, "x_user_id": self.accounts["1"]["x_user_id"], "material_id": EPISODE.upper()},
            {"row_number": 3, "x_user_id": self.accounts["1"]["x_user_id"], "material_id": OTHER_EPISODE},
        ]}, excel=True)
        source.resolve.assert_called_once_with([EPISODE, OTHER_EPISODE])
        self.assertEqual(result["items"][0]["material_id"], EPISODE)
        self.assertEqual(result["skipped"][0]["code"], "episode_not_free")

    def test_preview_commit_runner_queue_and_full_storage_restart(self):
        preview = self.preview()
        batch = self.commit(preview)
        run = self.plan(batch["tasks"][0])
        self.assertEqual(len(run["queues"]), 1)
        queue_id = run["queues"][0]["id"]
        with contextlib.closing(self.store.connect()) as conn:
            queue = dict(conn.execute("SELECT * FROM x_post_queue WHERE id=?", (queue_id,)).fetchone())
            self.assertEqual((queue["material_id"], queue["material_key"]), (EPISODE, EPISODE))
            self.assertEqual(queue["source_type"], "material")
            self.assertIsNone(queue["drama_pool_item_id"])
            self.assertEqual(queue["episode_number"], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM x_post_drama_pool").fetchone()[0], 0)
        # Bypass the process's schema cache: this is the actual restart/migration pass.
        posts.ensure_storage(self.path)
        restarted = posts.XPostStore(self.path).get_manual_run(batch["tasks"][0]["manual_run_id"])
        self.assertEqual(runner._manual_identity(restarted)["material_ids"], [EPISODE])
        self.assertEqual(restarted["queues"][0]["id"], queue_id)
        self.assertEqual(self.commit(preview)["id"], batch["id"])

    def test_changed_resource_identity_or_url_rejected_before_commit(self):
        preview = self.preview()
        for field, value in (("content_id", "other-content"), ("episode_number", 3),
                             ("resource_id", OTHER_EPISODE), ("series_code", "other-series"),
                             ("material_url", "https://media.example.test/changed.mp4")):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.materials)
                changed[EPISODE][field] = value
                with self.assertRaises(posts.XPostError) as exc:
                    self.commit(preview, changed)
                self.assertEqual(exc.exception.code, "x_video_material_changed")
        with contextlib.closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM x_post_manual_run").fetchone()[0], 0)

    def test_source_drift_before_queue_is_rejected(self):
        task = self.commit()["tasks"][0]
        changed = episode()
        changed["episode_number"] = 3
        with self.assertRaises(posts.XPostError) as exc:
            self.plan(task, changed)
        self.assertEqual(exc.exception.code, "x_video_material_changed")

    def test_active_and_unknown_pair_stay_blocked_other_episode_remains_usable(self):
        task = self.commit()["tasks"][0]
        self.assertFalse(self.preview()["rows"][0]["valid"])
        run = self.plan(task)
        log = self.legacy.reserve_log(run["queues"][0]["id"])
        self.legacy.mark_failed(log["id"], "x_post_unknown_outcome", "unknown", unknown_outcome=True)
        self.assertEqual(self.preview()["rows"][0]["error_code"], "x_post_unknown_outcome")
        unrelated = self.preview([{"account_id": "1", "material_id": OTHER_EPISODE}])
        self.assertTrue(unrelated["rows"][0]["valid"])

    def test_drama_queue_and_excel_share_the_same_resource_fence(self):
        from scripts.test_x_posts import deferred_drama_queue
        shared_id = "a" * 32
        self.materials[shared_id] = episode(shared_id)
        with mock.patch.dict("os.environ", {"PIPELINE_X_DISPATCH_ENABLED": "1"}):
            drama = deferred_drama_queue(self.legacy, account_id=1, username="test1")
        row = [{"account_id": "1", "material_id": shared_id.upper()}]
        self.assertEqual(self.preview(row)["rows"][0]["error_code"], "x_video_pair_active")
        log = self.legacy.reserve_log(drama["id"])
        self.legacy.mark_failed(log["id"], "x_post_unknown_outcome", "unknown", unknown_outcome=True)
        self.assertEqual(self.preview(row)["rows"][0]["error_code"], "x_post_unknown_outcome")
        with contextlib.closing(self.store.connect()) as conn:
            target = {"id": -1, "account_id": 1, "relay_account_id": 0,
                      "material_id": shared_id, "material_key": shared_id, "episode_key": ""}
            with self.assertRaises(posts.XPostError):
                self.legacy._assert_account_publish_fence(conn, target)

    def test_drama_publish_also_respects_unknown_excel_resource(self):
        task = self.commit()["tasks"][0]
        run = self.plan(task)
        log = self.legacy.reserve_log(run["queues"][0]["id"])
        self.legacy.mark_failed(log["id"], "x_post_unknown_outcome", "unknown", unknown_outcome=True)
        with contextlib.closing(self.store.connect()) as conn:
            target = {"id": -1, "account_id": 1, "relay_account_id": 0,
                      "material_id": EPISODE, "material_key": "", "episode_key": "content-17:2"}
            with self.assertRaises(posts.XPostError):
                self.legacy._assert_account_publish_fence(conn, target)

    def test_pending_excel_reservation_blocks_drama_resource_queue(self):
        from scripts.test_x_posts import deferred_drama_queue
        shared_id = "a" * 32
        self.materials[shared_id] = episode(shared_id)
        self.commit(self.preview([{"account_id": "1", "material_id": shared_id}]))
        # Restart reinstalls both base and personal triggers; the reservation
        # must remain effective even when a drama planner uses an empty key.
        posts.ensure_storage(self.path)
        with mock.patch.dict("os.environ", {"PIPELINE_X_DISPATCH_ENABLED": "1"}):
            with self.assertRaises((posts.XPostError, sqlite3.IntegrityError)):
                deferred_drama_queue(self.legacy, account_id=2, username="test2")
        with contextlib.closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM x_post_queue").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT state FROM x_post_manual_material_reservation").fetchone()[0], "active")

    def test_free_episode_has_stable_media_repair_item_identity(self):
        from scripts.x_post_daily_runner import _repair_job_key
        task = self.commit()["tasks"][0]
        run = runner._manual_identity(self.legacy.get_manual_run(task["manual_run_id"]))
        planned = self.candidates(run)
        self.assertEqual(planned[0]["manual_item_id"], 1)
        key = _repair_job_key(planned[0], "a" * 64, "standard", "standard_20m")
        self.assertEqual(len(key), 64)

    def test_non_excel_and_legacy_admission_remain_numeric(self):
        self.assertEqual(self.preview(source="manual")["valid_count"], 0)
        with self.assertRaises(posts.XPostError):
            posts.normalize_material_key(EPISODE)
        with self.assertRaises(posts.XPostError):
            self.legacy.create_manual_run([EPISODE], [1], "legacy-test-001", ACTOR)
        from scripts.test_x_post_manual_runner import manual_run
        legacy = manual_run()
        legacy["material_ids"][0] = EPISODE
        with self.assertRaises(runner.SidecarError):
            runner._manual_identity(legacy)

    def test_free_episode_language_and_long_video_rules_are_unchanged(self):
        self.materials[EPISODE]["duration_seconds"] = 1201
        self.assertEqual(self.preview()["rows"][0]["error_code"], "x_long_video_requires_premium")
        self.accounts["1"]["subscription_type"] = "premium"
        self.assertTrue(self.preview()["rows"][0]["valid"])
        self.accounts["1"]["drama_language"] = "de"
        self.assertEqual(self.preview()["rows"][0]["error_code"], "x_post_account_language_mismatch")

    def test_pending_preview_retains_unknown_and_strict_runner_rejects_long_nonmember(self):
        pending = self.materials[EPISODE]
        pending.update(duration_status="pending", duration_seconds=None, source_duration=None)
        preview = self.preview()
        self.assertTrue(preview["rows"][0]["valid"])
        snapshot = preview["rows"][0]["source_snapshot"]
        self.assertEqual(snapshot["duration_status"], "pending")
        self.assertIsNone(snapshot["duration_seconds"])
        dto = PublishingService.preview_dto(preview)
        self.assertEqual(dto["items"][0]["duration_status"], "pending")
        self.assertIsNone(dto["items"][0]["duration_seconds"])
        batch = self.commit(preview)
        run = runner._manual_identity(self.legacy.get_manual_run(batch["tasks"][0]["manual_run_id"]))
        actual = episode()
        actual.update(duration_seconds=1501, source_duration=1501)
        with mock.patch.object(runner, "deferred_candidate") as dispatch:
            planned = self.candidates(run, actual)
        dispatch.assert_not_called()
        self.assertEqual(len(planned), 0)
        self.assertEqual(planned.preflight_failures[0]["error_code"], "x_long_video_requires_premium")
        self.accounts["1"]["subscription_type"] = "premium"
        allowed = self.candidates(run, actual)
        self.assertEqual(len(allowed), 1)
        self.assertFalse(allowed.preflight_failures)

    def test_unresolved_duration_never_advances_to_media_or_pipeline(self):
        task = self.commit()["tasks"][0]
        run = runner._manual_identity(self.legacy.get_manual_run(task["manual_run_id"]))
        for duration, status in ((None, "pending"), (0, "verified"), (float("nan"), "verified")):
            with self.subTest(duration=duration, status=status):
                unresolved = episode()
                unresolved.update(duration_seconds=duration, source_duration=duration, duration_status=status)
                with mock.patch.object(runner, "deferred_candidate") as dispatch:
                    planned = self.candidates(run, unresolved)
                dispatch.assert_not_called()
                self.assertFalse(planned)
                self.assertEqual(planned.preflight_failures[0]["error_code"], "material_duration_missing")

    def test_strict_source_duration_error_stays_on_original_task(self):
        task = self.commit()["tasks"][0]
        run = runner._manual_identity(self.legacy.get_manual_run(task["manual_run_id"]))
        resolved = {"items": {}, "errors": {EPISODE: {
            "code": "material_duration_missing", "message": "视频时长暂不可用"}}}
        with mock.patch.object(runner, "_open_source_connection", return_value=mock.Mock()), \
                mock.patch("features.x_video_publish.sources.SourceRepository.resolve", return_value=resolved), \
                mock.patch.object(runner, "deferred_candidate") as dispatch:
            planned = runner._manual_candidates(make_config(self.tmp.name), run, [self.accounts["1"]], now=NOW)
        dispatch.assert_not_called()
        self.assertFalse(planned)
        self.assertEqual(planned.preflight_failures[0]["material_id"], EPISODE)
        self.assertEqual(planned.preflight_failures[0]["account_id"], 1)
        self.assertEqual(planned.preflight_failures[0]["error_code"], "material_duration_missing")


if __name__ == "__main__":
    unittest.main()
