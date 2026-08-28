#!/usr/bin/env python3
"""Offline tests for exact bound-drama media recovery orchestration."""

import contextlib
import copy
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.x_post_media_repair_backfill import BackfillError  # noqa: E402
from scripts.x_post_bound_drama_media_recovery import (  # noqa: E402
    execute_recovery,
    main,
    normalize_manifest,
)


BEIJING = timezone(timedelta(hours=8))


def manifest(count=2):
    return {
        "run_id": 274,
        "queues": [
            {
                "queue_id": 530 + index,
                "pool_item_id": 151 - index,
                "content_id": "CONTENT%s" % index,
                "episode_number": 5,
                "expected_error_code": "invalid_media_dimensions",
            }
            for index in range(count)
        ],
    }


class FakeStore:
    def __init__(self):
        self.calls = []

    def get_queue(self, queue_id):
        index = queue_id - 530
        return {
            "schedule_run_id": 274, "source_type": "drama",
            "drama_pool_item_id": 151 - index, "content_id": "CONTENT%s" % index,
            "episode_number": 5, "material_id": str(151 - index),
            "material_url": "https://media.example.test/%s.mp4" % (151 - index),
        }

    def recover_failed_drama_schedule_queues(
        self, run_id, prepared, **kwargs
    ):
        self.calls.append((run_id, [dict(item) for item in prepared], kwargs))
        return {
            "validated_queue_count": len(prepared),
            "updated_count": 0 if kwargs["validate_only"] else len(prepared),
            "next_status": "running",
        }


class BoundDramaRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.temp.name)
        self.config = SimpleNamespace(
            repair_url="http://127.0.0.1:18820/internal/media/repair",
            repair_token="repair-token",
            repair_timeout=30,
            max_media_bytes=512 * 1024 * 1024,
            work_dir=str(self.work_dir),
            lock_path=str(self.work_dir / "runner.lock"),
            mysql_database="kunlunads_dev",
            drama_app_id=2116,
            validate=lambda: None,
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    @contextlib.contextmanager
    def lock(_path):
        yield object()

    @staticmethod
    def candidate_loader(_connection, pools, **_kwargs):
        pool = pools[0]
        return [
            {
                "content_id": pool["content_id"],
                "episode_number": pool["next_sub_number"],
                "candidate_account_id": pool["candidate_account_id"],
                "material_id": str(pool["id"]),
                "material_language": "en",
                "material_url": "https://media.example.test/%s.mp4"
                % pool["id"],
            }
        ]

    @staticmethod
    def preflight(
        _config,
        candidate,
        _account,
        _rank,
        _timestamp,
        _destination,
        _downloader,
        _prober,
        *,
        repair_client,
        repair_state,
    ):
        del repair_client
        repair_state["attempted"] += 1
        pool_id = int(candidate["pool_item_id"])
        return {
            **candidate,
            "material_url": "https://cos.example.test/repaired-%s.mp4"
            % pool_id,
            "preflight_sha256": "%064x" % pool_id,
            "preflight_size": 1024 + pool_id,
            "preflight_duration": 30.0,
            "media_repair_trigger_code": "invalid_media_dimensions",
            "media_repair_job_key": "%064x" % (pool_id + 1000),
            "media_repair_profile": "x-h264-v5",
            "media_repair_source_sha256": "%064x" % (pool_id + 2000),
        }

    def execute(self, raw_manifest, *, apply=False, preflight=None):
        store = FakeStore()
        connection = SimpleNamespace(close=lambda: None)
        result = execute_recovery(
            self.config,
            self.work_dir / "ledger.sqlite3",
            raw_manifest,
            deployed_commit="a" * 40,
            apply=apply,
            store=store,
            repair_client=object(),
            connection_factory=lambda _config: connection,
            candidate_loader=self.candidate_loader,
            preflight_candidate=preflight or self.preflight,
            downloader=object(),
            prober=object(),
            lock_factory=self.lock,
            now=datetime(2026, 8, 26, 12, 0, tzinfo=BEIJING),
        )
        return result, store

    def test_validate_only_repairs_all_media_but_performs_no_ledger_apply_or_x(self):
        result, store = self.execute(manifest())

        self.assertEqual(result["status"], "validated")
        self.assertFalse(result["x_write_attempted"])
        self.assertEqual(result["repair_attempted_count"], 2)
        self.assertEqual(len(store.calls), 1)
        self.assertTrue(store.calls[0][2]["validate_only"])
        self.assertEqual(result["updated_count"], 0)

    def test_apply_runs_full_validate_before_one_store_apply(self):
        result, store = self.execute(manifest(), apply=True)

        self.assertEqual(result["status"], "applied")
        self.assertFalse(result["x_write_attempted"])
        self.assertEqual(
            [call[2]["validate_only"] for call in store.calls],
            [True, False],
        )
        self.assertEqual(result["updated_count"], 2)
        self.assertEqual(
            [item["queue_id"] for item in store.calls[1][1]],
            [530, 531],
        )

    def test_one_missing_repair_proof_aborts_before_any_store_write(self):
        calls = []

        def incomplete(*args, **kwargs):
            item = self.preflight(*args, **kwargs)
            calls.append(item["pool_item_id"])
            if len(calls) == 2:
                item["media_repair_job_key"] = ""
            return item

        with self.assertRaises(BackfillError) as rejected:
            self.execute(manifest(), apply=True, preflight=incomplete)

        self.assertEqual(
            rejected.exception.code,
            "x_post_bound_drama_repair_proof_invalid",
        )

    def test_manifest_rejects_duplicate_queue_or_pool_identity(self):
        raw = manifest()
        raw["queues"][1]["queue_id"] = raw["queues"][0]["queue_id"]
        with self.assertRaises(BackfillError):
            normalize_manifest(raw)

    def test_source_resource_or_url_drift_stops_before_repair(self):
        loader = self.candidate_loader
        for field, replacement in (
            ("material_id", "changed-resource"),
            ("material_url", "https://media.example.test/changed.mp4"),
        ):
            with self.subTest(field=field):
                def changed(*args, **kwargs):
                    items = loader(*args, **kwargs)
                    items[0][field] = replacement
                    return items
                preflight = mock.Mock()
                with mock.patch.object(self, "candidate_loader", changed):
                    with self.assertRaises(BackfillError) as rejected:
                        self.execute(manifest(), apply=True, preflight=preflight)
                self.assertEqual(rejected.exception.code, "x_post_bound_drama_source_changed")
                preflight.assert_not_called()

    def test_main_redacts_sqlite_failure_and_records_zero_x_report(self):
        report_path = self.work_dir / "store-failed.json"
        stdout = io.StringIO()
        with (
            mock.patch(
                "scripts.x_post_bound_drama_media_recovery.load_manifest",
                return_value=manifest(),
            ),
            mock.patch(
                "scripts.x_post_bound_drama_media_recovery."
                "load_drama_environment_files",
                return_value={},
            ),
            mock.patch(
                "scripts.x_post_bound_drama_media_recovery."
                "ScheduleConfig.from_env",
                return_value=self.config,
            ),
            mock.patch(
                "scripts.x_post_bound_drama_media_recovery.execute_recovery",
                side_effect=sqlite3.IntegrityError(
                    "x_post_queue relay binding invalid"
                ),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "--db-path",
                    str(self.work_dir / "ledger.sqlite3"),
                    "--manifest",
                    str(self.work_dir / "manifest.json"),
                    "--deployed-commit",
                    "a" * 40,
                    "--report-path",
                    str(report_path),
                    "--apply",
                ]
            )

        result = json.loads(stdout.getvalue())
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 1)
        self.assertEqual(result, report)
        self.assertEqual(
            result["error_code"],
            "x_post_bound_drama_recovery_store_failed",
        )
        self.assertFalse(result["x_write_attempted"])
        self.assertNotIn("relay binding invalid", result["error_message"])


class VerifiedIncidentReportTests(unittest.TestCase):
    SOURCE_BYTES = b"synthetic-original-video" * 16
    OUTPUT_BYTES = b"synthetic-repaired-video" * 24

    def setUp(self):
        from deploy.recovery import x_post_verified_drama_report as helper

        self.helper = helper
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name) / "verified.mp4"
        self.config = SimpleNamespace(
            repair_profile=helper.INCIDENT_PROFILE,
            max_media_bytes=512 * 1024 * 1024,
            media_allowed_hosts=(
                "advertising-1306474899.cos.ap-hongkong.myqcloud.com",
            ),
            media_timeout=30,
            max_repairs_per_run=16,
        )
        self.account = {
            "id": 1, "username": "repair_only", "display_name": "Repair only",
            "x_user_id": "1", "drama_language": "en",
            "long_video_eligible": True, "long_video_publish_eligible": True,
        }
        self.timestamp = 1787907600
        self.downloader = mock.Mock(side_effect=self.download)
        self.prober = mock.Mock(side_effect=self.probe)

    def fixture(self, queue_id=635, run_id=348):
        import hashlib
        from features.x_posts.media_repair import output_rate_control
        from scripts.x_post_daily_runner import _repair_job_key

        expected = {
            "queue_id": queue_id, "pool_item_id": queue_id - 400,
            "content_id": "CONTENT%s" % queue_id, "episode_number": 2,
            "expected_error_code": "invalid_media_dimensions",
        }
        material_id = "%032x" % queue_id
        source_url = "https://media.example.test/%s.mp4" % material_id
        candidate = {
            "source_type": "drama", "material_id": material_id,
            "material_url": source_url, "content_id": expected["content_id"],
            "episode_number": 2, "pool_item_id": expected["pool_item_id"],
            "drama_pool_item_id": expected["pool_item_id"],
            "material_language": "en", "drama_name": "Example Drama",
            "description": "An example description.", "tag": "Fantasy",
            "material_name": "Episode 2", "candidate_account_id": 1,
        }
        queue = {
            "id": queue_id, "schedule_run_id": run_id, "source_type": "drama",
            "status": "failed", "media_validation_mode": "deferred",
            "material_id": material_id, "material_url": source_url,
            "drama_pool_item_id": expected["pool_item_id"],
            "content_id": expected["content_id"], "episode_number": 2,
            "preflight_size": 0,
        }
        source_sha = hashlib.sha256(self.SOURCE_BYTES).hexdigest()
        output_sha = hashlib.sha256(self.OUTPUT_BYTES).hexdigest()
        job_key = _repair_job_key(
            candidate, source_sha, self.helper.INCIDENT_PROFILE, "premium",
        )
        cos_key = self.helper.COS_PREFIX + "drama-resource-%s/source-%s/output-%s.mp4" % (
            material_id, source_sha, output_sha,
        )
        gpu_manifest = {
            "version": 4, "status": "ready", "completed_at": "2026-08-28T08:00:00Z",
            "request": {
                "job_key": job_key, "material_id": material_id,
                "pool_item_id": str(expected["pool_item_id"]),
                "source_url": source_url, "source_sha256": source_sha,
                "source_size": len(self.SOURCE_BYTES),
                "trigger_code": "invalid_media_dimensions",
                "profile": self.helper.INCIDENT_PROFILE, "duration_policy": "premium",
            },
            "cos_key": cos_key,
            "result": {
                "job_key": job_key, "profile": self.helper.INCIDENT_PROFILE,
                "output_url": self.helper.OUTPUT_ORIGIN + "/" + cos_key,
                "output_sha256": output_sha, "output_size": len(self.OUTPUT_BYTES),
                "probe": {
                    "codec": "h264", "profile": "high", "pixel_format": "yuv420p",
                    "field_order": "progressive", "width": 720, "height": 1280,
                    "frame_rate": 30.0, "gop": 60, "duration": 180.0,
                    "size": len(self.OUTPUT_BYTES), "audio_codec": "aac",
                    "audio_profile": "lc", "audio_sample_rate": 48000,
                    "audio_channels": 2, "audio_channel_layout": "stereo",
                },
            },
            "repair": {
                "source_duration": 180.0, "target_duration": 180.0,
                "trim_applied": False,
                "rate_control": output_rate_control(self.config.max_media_bytes, 180.0),
            },
        }
        return {
            "candidate": candidate, "expected": expected,
            "frozen_queue": queue, "manifest": gpu_manifest,
        }

    def download(self, url, destination, _allowed_hosts, **_kwargs):
        import hashlib

        self.assertTrue(url.startswith(self.helper.OUTPUT_PREFIX))
        Path(destination).write_bytes(self.OUTPUT_BYTES)
        return {
            "sha256": hashlib.sha256(self.OUTPUT_BYTES).hexdigest(),
            "size": len(self.OUTPUT_BYTES), "media_kind": "video",
            "media_type": "video/mp4",
        }

    def probe(self, destination, **kwargs):
        self.assertEqual(kwargs["max_duration_seconds"], 14400.0)
        self.assertTrue(Path(destination).is_file())
        return {
            "codec": "h264", "pixel_format": "yuv420p", "audio_codec": "aac",
            "width": 720, "height": 1280, "frame_rate": 30.0,
            "duration": 180.0, "size": Path(destination).stat().st_size,
        }

    def prepare(self, data=None, account=None):
        data = data or self.fixture()
        return self.helper.prepare_from_gpu_manifest(
            self.config, data["candidate"], account or self.account,
            1, self.timestamp, self.destination, self.downloader, self.prober,
            expected=data["expected"], frozen_queue=data["frozen_queue"],
            manifest=data["manifest"],
        )

    def assert_rejected_before_download(self, data):
        from scripts.x_post_daily_runner import DailyRunError

        self.downloader.reset_mock()
        self.prober.reset_mock()
        with self.assertRaises(DailyRunError):
            self.prepare(data)
        self.downloader.assert_not_called()
        self.prober.assert_not_called()

    def test_cached_source_is_not_downloaded_and_item_matches_original_preflight(self):
        from features.x_posts.service import XPostError
        from scripts.x_post_daily_runner import (
            _preflight_candidate, _validate_repair_probe,
        )

        data = self.fixture()
        untouched = copy.deepcopy(data)
        with mock.patch(
            "scripts.x_post_daily_runner.MediaRepairClient.repair",
            side_effect=AssertionError("No repair POST is allowed for cached proof"),
        ):
            actual = self.prepare(data)

        self.downloader.assert_called_once()
        self.assertEqual(
            self.downloader.call_args.args[0], data["manifest"]["result"]["output_url"],
        )
        self.prober.assert_called_once()
        self.assertFalse(self.destination.exists())
        self.assertEqual(data, untouched)

        # Compare the entire returned item with the existing successful preflight
        # contract, using synthetic bytes and a synthetic repair client only.
        def original_download(url, destination, allowed_hosts, **kwargs):
            if url == data["candidate"]["material_url"]:
                Path(destination).write_bytes(self.SOURCE_BYTES)
                return {
                    "sha256": data["manifest"]["request"]["source_sha256"],
                    "size": len(self.SOURCE_BYTES),
                }
            return self.download(url, destination, allowed_hosts, **kwargs)

        def original_probe(destination, **kwargs):
            if Path(destination).read_bytes() == self.SOURCE_BYTES:
                raise XPostError("invalid_media_dimensions", "Synthetic bad dimensions", 422)
            return self.probe(destination, **kwargs)

        repaired = copy.deepcopy(data["manifest"]["result"])
        repaired["probe"] = _validate_repair_probe(
            repaired["probe"], repaired["output_size"], max_duration_seconds=14400.0,
        )
        expected = _preflight_candidate(
            self.config, data["candidate"], self.account, 1, self.timestamp,
            self.destination, original_download, original_probe,
            repair_client=SimpleNamespace(repair=lambda _payload: repaired),
            repair_state={"attempted": 0},
        )
        self.assertEqual(actual, expected)
        self.assertEqual(actual["preflight_duration"], 180.0)

    def test_only_the_exact_incident_queue_run_pairs_are_supported(self):
        for run_id, queue_ids in self.helper.EXPECTED_QUEUES.items():
            for queue_id in queue_ids:
                with self.subTest(run_id=run_id, queue_id=queue_id):
                    self.prepare(self.fixture(queue_id, run_id))
        for queue_id, run_id in ((533, 348), (634, 348), (648, 348),
                                 (635, 350), (667, 348), (667, 351),
                                 (666, 350), (670, 350)):
            with self.subTest(queue_id=queue_id, run_id=run_id):
                self.assert_rejected_before_download(self.fixture(queue_id, run_id))
        data = self.fixture()
        data["expected"]["queue_id"] = True
        data["frozen_queue"]["id"] = True
        self.assert_rejected_before_download(data)

    def test_candidate_or_frozen_identity_drift_is_rejected_before_download(self):
        changes = (
            ("candidate", "material_id", "f" * 32),
            ("candidate", "material_url", "https://media.example.test/changed.mp4"),
            ("candidate", "content_id", "OTHER"),
            ("candidate", "episode_number", 3),
            ("candidate", "pool_item_id", 999),
            ("candidate", "drama_pool_item_id", 999),
            ("candidate", "source_type", "material"),
            ("candidate", "media_kind", "image"),
            ("expected", "pool_item_id", 999),
            ("expected", "expected_error_code", "invalid_media_duration"),
            ("frozen_queue", "id", 636),
            ("frozen_queue", "material_id", "e" * 32),
            ("frozen_queue", "material_url", "https://media.example.test/changed.mp4"),
            ("frozen_queue", "content_id", "OTHER"),
            ("frozen_queue", "episode_number", 3),
            ("frozen_queue", "status", "published"),
            ("frozen_queue", "media_validation_mode", "preflight"),
            ("frozen_queue", "media_repair_job_key", "a" * 64),
            ("frozen_queue", "original_material_url", "https://media.example.test/old.mp4"),
        )
        for section, field, value in changes:
            with self.subTest(section=section, field=field):
                data = self.fixture()
                data[section][field] = value
                self.assert_rejected_before_download(data)

    def test_partial_cpu_reports_and_incomplete_or_nonready_manifests_are_rejected(self):
        for field, value in (("version", 3), ("version", True),
                             ("status", "failed"), ("status", "running"),
                             ("request", {}), ("result", {}), ("repair", {}),
                             ("completed_at", "not-a-completion-time")):
            with self.subTest(field=field, value=value):
                data = self.fixture()
                data["manifest"][field] = value
                self.assert_rejected_before_download(data)
        for field in ("request", "result", "cos_key", "repair", "completed_at"):
            data = self.fixture()
            del data["manifest"][field]
            self.assert_rejected_before_download(data)
        data = self.fixture()
        data["manifest"] = {
            "run_id": 348, "status": "failed", "x_write_attempted": False,
            "results": [],
        }
        self.assert_rejected_before_download(data)

    def test_request_must_be_canonical_complete_and_match_the_frozen_source(self):
        changes = (
            ("pool_item_id", 235), ("pool_item_id", "999"),
            ("material_id", "f" * 32),
            ("source_url", "https://media.example.test/other.mp4"),
            ("source_sha256", "A" * 64),
            ("source_size", 0), ("source_size", True), ("source_size", "368"),
            ("source_size", float(len(self.SOURCE_BYTES))),
            ("source_size", 536870913),
            ("trigger_code", "invalid_media_duration"),
            ("profile", "x-h264-old"), ("duration_policy", "standard"),
            ("unrecognized_field", "not-in-worker-contract"),
        )
        for field, value in changes:
            with self.subTest(field=field, value=value):
                data = self.fixture()
                data["manifest"]["request"][field] = value
                self.assert_rejected_before_download(data)

    def test_request_and_result_cannot_self_assert_a_different_job_key(self):
        data = self.fixture()
        data["manifest"]["request"]["job_key"] = "c" * 64
        data["manifest"]["result"]["job_key"] = "c" * 64
        self.assert_rejected_before_download(data)
        data = self.fixture()
        data["manifest"]["request"]["source_sha256"] = "d" * 64
        self.assert_rejected_before_download(data)

    def test_result_probe_and_duration_provenance_must_match_v5_contract(self):
        changes = (
            ("result", "job_key", "d" * 64), ("result", "profile", "other"),
            ("result", "output_sha256", "not-a-hash"),
            ("result", "output_size", 0), ("result", "output_size", True),
            ("result", "output_size", 536870913),
            ("probe", "size", 999), ("probe", "duration", float("nan")),
            ("probe", "duration", 14401), ("probe", "width", 1920),
            ("probe", "frame_rate", 61), ("probe", "codec", "hevc"),
            ("probe", "audio_codec", "mp3"), ("probe", "gop", 30),
            ("repair", "trim_applied", True), ("repair", "source_duration", float("nan")),
            ("repair", "target_duration", 179), ("repair", "rate_control", {}),
        )
        for section, field, value in changes:
            with self.subTest(section=section, field=field, value=value):
                data = self.fixture()
                target = data["manifest"][section] if section != "probe" else data["manifest"]["result"]["probe"]
                target[field] = value
                self.assert_rejected_before_download(data)

    def test_noncanonical_foreign_and_crosswired_cos_outputs_are_rejected(self):
        changes = (
            lambda url: url + "?download=1",
            lambda url: url + "#fragment",
            lambda url: url.replace("https://", "http://"),
            lambda url: url.replace(self.helper.OUTPUT_ORIGIN, "https://media.example.test"),
            lambda url: url.replace("/drama-resource-", "/%64rama-resource-"),
            lambda url: url.replace("/x-post-media-repair/", "/different-prefix/"),
            lambda url: url.replace("/source-", "/extra/source-"),
            lambda url: url.replace("output-", "output-" + "c" * 64),
        )
        for change in changes:
            data = self.fixture()
            data["manifest"]["result"]["output_url"] = change(data["manifest"]["result"]["output_url"])
            self.assert_rejected_before_download(data)
        data = self.fixture()
        data["manifest"]["cos_key"] = data["manifest"]["cos_key"].replace("/source-", "/other/source-")
        self.assert_rejected_before_download(data)
        data = self.fixture()
        data["manifest"]["result"]["output_sha256"] = "e" * 64
        self.assert_rejected_before_download(data)

    def test_cpu_fingerprint_mismatch_returns_no_proof_and_removes_download(self):
        from scripts.x_post_daily_runner import MediaRepairError

        for field, value in (("sha256", "f" * 64), ("size", len(self.OUTPUT_BYTES) + 1)):
            with self.subTest(field=field):
                def bad_download(*args, **kwargs):
                    media = self.download(*args, **kwargs)
                    media[field] = value
                    return media
                self.downloader.side_effect = bad_download
                with self.assertRaises(MediaRepairError) as rejected:
                    self.prepare()
                self.assertEqual(rejected.exception.code, "x_post_media_repair_fingerprint_mismatch")
                self.assertFalse(self.destination.exists())

    def test_cpu_probe_disagreement_or_failure_returns_no_proof_and_cleans_up(self):
        from features.x_posts.service import XPostError
        from scripts.x_post_daily_runner import MediaRepairError

        for field, value in (("height", 720), ("duration", 180.1), ("frame_rate", 29.0)):
            with self.subTest(field=field):
                def bad_probe(*args, **kwargs):
                    result = self.probe(*args, **kwargs)
                    result[field] = value
                    return result
                self.prober.side_effect = bad_probe
                with self.assertRaises(MediaRepairError) as rejected:
                    self.prepare()
                self.assertEqual(rejected.exception.code, "x_post_media_repair_probe_mismatch")
                self.assertFalse(self.destination.exists())
        self.prober.side_effect = XPostError("media_probe_failed", "Synthetic probe failure", 422)
        with self.assertRaises(XPostError):
            self.prepare()
        self.assertFalse(self.destination.exists())

    def test_output_download_failure_has_no_source_fallback_or_repair_post(self):
        from features.x_posts.service import XPostError

        def interrupted_download(_url, destination, *_args, **_kwargs):
            Path(destination).write_bytes(b"partial-output")
            raise XPostError("media_download_incomplete", "Synthetic interrupted GET", 502)

        self.downloader.side_effect = interrupted_download
        with self.assertRaises(XPostError):
            self.prepare()
        self.downloader.assert_called_once()
        self.prober.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_nonpremium_repair_account_or_image_output_cannot_create_proof(self):
        from features.x_posts.service import XPostError
        from scripts.x_post_daily_runner import CandidatePreflightError

        with self.assertRaises(CandidatePreflightError):
            self.prepare(account={**self.account, "long_video_eligible": False})
        self.downloader.assert_not_called()

        def image_download(*args, **kwargs):
            media = self.download(*args, **kwargs)
            media["media_kind"] = "image"
            media["media_type"] = "image/png"
            return media

        self.downloader.side_effect = image_download
        with self.assertRaises(XPostError):
            self.prepare()
        self.prober.assert_not_called()
        self.assertFalse(self.destination.exists())


class IncidentCheckpointTests(unittest.TestCase):
    SOURCE_BYTES = VerifiedIncidentReportTests.SOURCE_BYTES
    OUTPUT_BYTES = VerifiedIncidentReportTests.OUTPUT_BYTES
    fixture = VerifiedIncidentReportTests.fixture
    download = VerifiedIncidentReportTests.download
    probe = VerifiedIncidentReportTests.probe

    def setUp(self):
        from deploy.recovery import x_post_incident_preflight_20260828 as wrapper
        from deploy.recovery import x_post_verified_drama_report as helper

        self.wrapper, self.helper = wrapper, helper
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.artifact = Path(self.temp.name).resolve() / "artifact"
        self.artifact.mkdir(mode=0o700)
        self.commit = "a" * 40
        self.current = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        self.timestamp = int(self.current.timestamp())
        self.config = SimpleNamespace(
            repair_profile=helper.INCIDENT_PROFILE,
            max_media_bytes=512 * 1024 * 1024,
            media_allowed_hosts=("media.example.test", "advertising-1306474899.cos.ap-hongkong.myqcloud.com"),
            media_timeout=30, max_repairs_per_run=16,
            lock_path=str(Path(self.temp.name) / "runner.lock"),
        )
        self.account = {
            "id": 1, "username": "repair_only", "display_name": "Repair only",
            "x_user_id": "1", "drama_language": "en",
            "long_video_eligible": True, "long_video_publish_eligible": True,
        }
        self.fixtures = {
            queue_id: self.fixture(queue_id, run_id)
            for run_id, queue_ids in helper.EXPECTED_QUEUES.items()
            for queue_id in queue_ids
        }
        queues = [item["frozen_queue"] for item in self.fixtures.values()]
        logs = [{
            "id": item["id"], "queue_id": item["id"], "status": "failed",
            "attempt_count": 0, "unknown_outcome": 0,
            "error_code": "invalid_media_dimensions", "x_media_id": "",
            "x_post_id": "", "x_post_url": "", "started_at": "", "published_at": "",
        } for item in queues]
        frozen = {
            "queues": queues, "logs": logs, "relays": [],
            "pools": [{"id": item["drama_pool_item_id"], "content_id": item["content_id"]} for item in queues],
            "runs": [{"id": run_id, "status": "completed_with_errors"} for run_id in helper.EXPECTED_QUEUES],
            "protected": {
                "queues": [{**queues[0], "id": key} for key in (533, 719, 726)],
                "logs": [{**logs[0], "id": key, "queue_id": key, "attempt_count": 1,
                          "unknown_outcome": 1 if key == 726 else 0} for key in (533, 719, 726)],
                "relays": [{"id": 77, "queue_id": 533, "status": "failed"}],
            },
        }
        snapshot = {"captured_at": "2026-08-28T07:00:00Z", "frozen": frozen,
                    "base_deployed_commit": "b" * 40, "new_deployed_commit": "c" * 40}
        bundle = {
            "origin_host": wrapper.GPU_HOST, "origin_release": wrapper.GPU_RELEASE,
            "captured_at": "2026-08-28T09:00:00Z", "records": [],
        }
        for key in range(635, 642):
            manifest = self.fixtures[key]["manifest"]
            bundle["records"].append({
                "path": wrapper.GPU_MANIFEST_ROOT + manifest["request"]["job_key"] + ".json",
                "uid": 0, "mode": "0o600", "manifest": manifest,
                "sha256": wrapper._sha(wrapper._encoded(manifest)),
            })
        frozen_bytes, bundle_bytes = wrapper._encoded(snapshot), wrapper._encoded(bundle)
        (self.artifact / "frozen-inputs.json").write_bytes(frozen_bytes)
        (self.artifact / "gpu-ready-manifests.json").write_bytes(bundle_bytes)
        with contextlib.closing(sqlite3.connect(self.artifact / "rehearsal.sqlite3")) as conn:
            for key, table in wrapper.TABLES.items():
                rows = frozen[key] + frozen["protected"].get(key, [])
                columns = list(rows[0])
                definitions = []
                for name in columns:
                    value = next((row[name] for row in rows if row[name] is not None), None)
                    kind = "INTEGER" if isinstance(value, int) else "REAL" if isinstance(value, float) else "TEXT"
                    definitions.append('"%s" %s' % (name, kind))
                conn.execute("CREATE TABLE %s(%s)" % (table, ",".join(definitions)))
                conn.executemany("INSERT INTO %s VALUES(%s)" % (table, ",".join("?" for _ in columns)),
                                 [tuple(row[name] for name in columns) for row in rows])
            conn.commit()
        self.private_owner = wrapper._private_owner
        self.trusted_ancestor = wrapper._trusted_ancestor
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        # Windows cannot represent Linux uid/mode. Guard functions themselves
        # are tested below; media bytes, SQLite comparisons and hashes stay real.
        for name in ("_require_root", "_private_owner", "_trusted_ancestor"):
            stack.enter_context(mock.patch.object(wrapper, name, return_value=None))
        stack.enter_context(mock.patch.object(wrapper, "_runtime_commit", return_value=self.commit))
        stack.enter_context(mock.patch.object(wrapper, "_now", return_value=self.current))
        stack.enter_context(mock.patch.object(wrapper, "FROZEN_SHA256", wrapper._sha(frozen_bytes)))
        stack.enter_context(mock.patch.object(wrapper, "GPU_BUNDLE_SHA256", wrapper._sha(bundle_bytes)))
        self.preflight = self.make_preflight()

    def make_preflight(self, **kwargs):
        return self.wrapper.IncidentPreflight(self.artifact, 348, deployed_commit=self.commit, **kwargs)

    def call(self, preflight=None, queue_id=635, downloader=None, prober=None, repair_client=None, candidate=None, state=None):
        return (preflight or self.preflight)(
            self.config, candidate or self.fixtures[queue_id]["candidate"], self.account,
            queue_id - 634, self.timestamp, Path(self.temp.name) / ("%s.mp4" % queue_id),
            downloader or self.download, prober or self.probe,
            repair_client=repair_client, repair_state=state if state is not None else {"attempted": 0},
        )

    def test_gpu_output_is_cpu_verified_before_checkpoint_then_reused_without_media(self):
        state = {"attempted": 0}
        repair_client = SimpleNamespace(repair=mock.Mock(side_effect=AssertionError("No repair POST")))
        downloaded, probed = mock.Mock(side_effect=self.download), mock.Mock(side_effect=self.probe)
        item = self.call(downloader=downloaded, prober=probed, repair_client=repair_client, state=state)
        self.assertEqual(downloaded.call_count, 1)
        self.assertEqual(probed.call_count, 1)
        repair_client.repair.assert_not_called()
        self.assertEqual(state, {"attempted": 0})
        report = self.preflight.report()
        self.assertEqual(report["gpu_manifest_verified_count"], 1)
        self.assertEqual(report["prepared"][0]["item"], item)
        checkpoint, _ = self.wrapper._read_private(self.preflight.checkpoint_dir / "queue-635.json")
        self.assertEqual(checkpoint["status"], "cpu_verified")
        self.assertEqual(checkpoint["cpu_verification"]["output_download"]["sha256"], item["preflight_sha256"])
        self.assertEqual(checkpoint["tool_commit"], self.commit)
        reuse = self.make_preflight(reuse_only=True, expected_index_sha256=report["checkpoint_index_sha256"])
        forbidden = mock.Mock(side_effect=AssertionError("No media work on checkpoint reuse"))
        reused_state = {"attempted": 0}
        self.assertEqual(self.call(reuse, downloader=forbidden, prober=forbidden, state=reused_state), item)
        forbidden.assert_not_called()
        self.assertEqual(reused_state, {"attempted": 0})
        self.assertEqual(reuse.report()["checkpoint_reused_count"], 1)

    def test_partial_failure_retains_completed_checkpoint_and_index(self):
        from features.x_posts.service import XPostError

        self.call()
        index_before = self.preflight.index_path.read_bytes()
        item_before = (self.preflight.checkpoint_dir / "queue-635.json").read_bytes()
        failed = mock.Mock(side_effect=XPostError("media_download_incomplete", "Synthetic failure", 422))
        with self.assertRaises(XPostError):
            self.call(queue_id=636, downloader=failed)
        self.assertEqual(self.preflight.index_path.read_bytes(), index_before)
        self.assertEqual((self.preflight.checkpoint_dir / "queue-635.json").read_bytes(), item_before)
        self.assertFalse((self.preflight.checkpoint_dir / "queue-636.json").exists())
        self.assertEqual(self.preflight.report()["prepared_count"], 1)

    def test_missing_gpu_manifest_uses_original_preflight_and_honest_repair_count(self):
        from features.x_posts.service import XPostError
        from scripts.x_post_daily_runner import _validate_repair_probe

        data = self.fixtures[642]
        repaired = copy.deepcopy(data["manifest"]["result"])
        repaired["probe"] = _validate_repair_probe(repaired["probe"], repaired["output_size"], max_duration_seconds=14400.0)

        def download(url, path, hosts, **kwargs):
            if url == data["candidate"]["material_url"]:
                Path(path).write_bytes(self.SOURCE_BYTES)
                return {"sha256": data["manifest"]["request"]["source_sha256"], "size": len(self.SOURCE_BYTES)}
            return self.download(url, path, hosts, **kwargs)

        def probe(path, **kwargs):
            if Path(path).read_bytes() == self.SOURCE_BYTES:
                raise XPostError("invalid_media_dimensions", "Synthetic dimensions", 422)
            return self.probe(path, **kwargs)

        repair_client = SimpleNamespace(repair=mock.Mock(return_value=repaired))
        state = {"attempted": 0}
        with mock.patch.object(self.wrapper, "_preflight_candidate", wraps=self.wrapper._preflight_candidate) as original:
            self.call(queue_id=642, downloader=download, prober=probe, repair_client=repair_client, state=state)
        original.assert_called_once()
        repair_client.repair.assert_called_once()
        self.assertEqual(state["attempted"], 1)
        report = self.preflight.report()
        self.assertEqual(report["repair_attempted_count"], 1)
        self.assertEqual(report["normal_preflight_verified_count"], 1)
        self.assertEqual(report["gpu_manifest_verified_count"], 0)

    def test_reuse_only_rejects_missing_wrong_hash_source_drift_and_expiry_without_get(self):
        self.call()
        digest = self.preflight.report()["checkpoint_index_sha256"]
        forbidden = mock.Mock(side_effect=AssertionError("No GET or repair for rejected checkpoint"))
        with self.assertRaises(BackfillError):
            self.make_preflight(reuse_only=True)
        for queue_id, expected_sha, changes in (
            (636, digest, {}), (635, "0" * 64, {}),
            (635, digest, {"material_id": "f" * 32}),
        ):
            with self.subTest(queue_id=queue_id, sha=expected_sha, changes=changes):
                reuse = self.make_preflight(reuse_only=True, expected_index_sha256=expected_sha)
                candidate = {**self.fixtures[queue_id]["candidate"], **changes}
                with self.assertRaises(BackfillError):
                    self.call(reuse, queue_id=queue_id, candidate=candidate, downloader=forbidden, prober=forbidden)
        reuse = self.make_preflight(reuse_only=True, expected_index_sha256=digest)
        with mock.patch.object(self.wrapper, "_now", return_value=self.current + timedelta(hours=4, seconds=1)):
            with self.assertRaises(BackfillError) as expired:
                self.call(reuse, downloader=forbidden, prober=forbidden)
        self.assertEqual(expired.exception.code, "x_post_incident_checkpoint_expired")
        forbidden.assert_not_called()

    def test_reuse_rejects_failed_unknown_incomplete_or_different_commit_even_with_new_index_hash(self):
        self.call()
        path = self.preflight.checkpoint_dir / "queue-635.json"
        original, _ = self.wrapper._read_private(path)
        index, _ = self.wrapper._read_private(self.preflight.index_path)
        forbidden = mock.Mock(side_effect=AssertionError("Bad proof must not trigger a GET"))
        for changes in (
            {"status": "failed"}, {"unknown_outcome": True},
            {"deployed_commit": "d" * 40}, {"tool_commit": "d" * 40},
            {"cpu_verification": {}}, {"item": {}},
        ):
            with self.subTest(changes=changes):
                changed = {**original, **changes}
                changed["item_sha256"] = self.wrapper._sha(self.wrapper._encoded(changed["item"]))
                item_digest = self.wrapper._write_private(path, changed)
                index["checkpoints"]["635"]["sha256"] = item_digest
                digest = self.wrapper._write_private(self.preflight.index_path, index)
                reuse = self.make_preflight(reuse_only=True, expected_index_sha256=digest)
                with self.assertRaises(BackfillError):
                    self.call(reuse, downloader=forbidden, prober=forbidden)
        forbidden.assert_not_called()

    def test_protected_unknown_ledger_drift_rejects_reuse_before_media(self):
        self.call()
        reuse = self.make_preflight(reuse_only=True, expected_index_sha256=self.preflight.report()["checkpoint_index_sha256"])
        with contextlib.closing(sqlite3.connect(self.preflight.db_path)) as conn:
            conn.execute("UPDATE x_post_publish_log SET unknown_outcome=0 WHERE id=726")
            conn.commit()
        forbidden = mock.Mock(side_effect=AssertionError("Protected ledger drift must stop before GET"))
        with self.assertRaises(BackfillError) as changed:
            self.call(reuse, downloader=forbidden, prober=forbidden)
        self.assertEqual(changed.exception.code, "x_post_incident_frozen_ledger_changed")
        forbidden.assert_not_called()

    def test_private_file_and_ancestor_permissions_fail_closed(self):
        import os
        import stat

        self.private_owner(SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600), 0o600)
        for uid, mode in ((1, 0o600), (0, 0o644), (0, 0o400)):
            with self.subTest(uid=uid, mode=mode), self.assertRaises(BackfillError):
                self.private_owner(SimpleNamespace(st_uid=uid, st_mode=stat.S_IFREG | mode), 0o600)
        self.trusted_ancestor(SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o1777))
        with self.assertRaises(BackfillError):
            self.trusted_ancestor(SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o777))
        with mock.patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_nlink=1)):
            with self.assertRaises(BackfillError):
                self.wrapper._private_file(self.artifact / "link.json")
        original = self.artifact / "frozen-inputs.json"
        linked = self.artifact / "hardlink.json"
        os.link(original, linked)
        with self.assertRaises(BackfillError):
            self.wrapper._private_file(original)
        linked.unlink()

    def test_prepare_cli_boundary_is_fixed_rehearsal_validate_only_with_original_lock(self):
        lock_calls = []

        @contextlib.contextmanager
        def lock(path):
            lock_calls.append(path)
            yield object()

        def execute(config, db_path, manifest, **kwargs):
            self.assertEqual(Path(db_path), self.artifact / "rehearsal.sqlite3")
            self.assertIs(kwargs["apply"], False)
            self.assertEqual(manifest["run_id"], 348)
            self.assertEqual([item["queue_id"] for item in manifest["queues"]], list(range(635, 648)))
            with kwargs["lock_factory"](config.lock_path):
                return {"status": "validated", "updated_count": 0, "x_write_attempted": False}

        with mock.patch.object(self.wrapper, "XPostStore") as store, mock.patch.object(
            self.wrapper, "process_lock", side_effect=lock,
        ), mock.patch.object(self.wrapper, "execute_recovery", side_effect=execute):
            result = self.wrapper.prepare(self.config, self.artifact, 348, deployed_commit=self.commit)
        store.assert_called_once_with(self.artifact / "rehearsal.sqlite3")
        self.assertEqual(lock_calls, [self.config.lock_path])
        self.assertEqual(result["status"], "validated")
        self.assertFalse(result["x_write_attempted"])
        self.assertTrue((self.preflight.checkpoint_dir / "prepare-348-report.json").exists())
        for extra in (("--phase", "apply"), ("--db-path", "production.sqlite3"), ("--apply",)):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.wrapper.main(["--artifact-dir", str(self.artifact), "--run-id", "348", "--phase", "prepare",
                                   "--deployed-commit", self.commit, *extra])


if __name__ == "__main__":
    unittest.main()
