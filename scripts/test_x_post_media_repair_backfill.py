#!/usr/bin/env python3
"""Offline safety tests for the explicit X media-repair backfill."""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts.service import XPostError  # noqa: E402
from scripts.test_x_post_daily import (  # noqa: E402
    candidate,
    repair_response,
    test_config,
)
from scripts.x_post_daily_runner import (  # noqa: E402
    MediaRepairError,
)
from scripts.x_post_media_repair_backfill import (  # noqa: E402
    BackfillError,
    _atomic_write_report,
    _validate_report_path,
    execute_backfill,
    load_environment_files,
    main,
    normalize_material_ids,
)


NOW = datetime(
    2026,
    7,
    24,
    12,
    0,
    tzinfo=timezone(timedelta(hours=8)),
)


class FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeSidecar:
    def __init__(self, material_ids=(10, 11, 12)):
        self.events = []
        self.material_ids = tuple(material_ids)

    def preflight_storage(self, path):
        self.events.append(("storage", path))
        return {"ready": True, "mounted": True, "atomic_write": True}

    def available_pool_items(self, path, limit):
        self.events.append(("available", path, limit))
        return [
            {
                "id": material_id,
                "material_id": str(material_id),
                "material_key": str(material_id),
                "created_at": "2026-07-23T00:00:%02dZ"
                % (material_id % 60),
            }
            for material_id in self.material_ids
        ]

    def record_pool_checks(self, path, checks):
        self.events.append(("checks", path, [dict(item) for item in checks]))
        return {"updated_count": len(checks)}

    def create_plan(self, *_args, **_kwargs):
        raise AssertionError("backfill must never create a plan")

    def publish_queue(self, *_args, **_kwargs):
        raise AssertionError("backfill must never publish")


@contextlib.contextmanager
def acquired_lock(path, events=None):
    if events is not None:
        events.append(path)
    yield object()


def hydrated_loader(
    connection,
    pool_items,
    source_date,
    limit,
    schema,
):
    assert source_date == "2026-07-23"
    assert schema == "kunlunads_dev"
    assert limit == len(pool_items)
    result = []
    for pool_item in pool_items:
        item = candidate(int(pool_item["material_id"]), 1)
        item["pool_item_id"] = pool_item["id"]
        item["pool_created_at"] = pool_item["created_at"]
        result.append(item)
    return result, []


def media_downloader(url, destination, _hosts, max_bytes, timeout):
    repaired = url.startswith("https://cos.example.test/")
    content = b"repaired" if repaired else b"video"
    Path(destination).write_bytes(content)
    return {
        "size": len(content),
        "sha256": ("b" if repaired else "a") * 64,
        "media_type": "video/mp4",
    }


def media_prober(path, max_bytes, timeout):
    if Path(path).read_bytes() == b"video":
        raise XPostError("invalid_media_codec", "bad codec", 422)
    return {
        "codec": "h264",
        "pixel_format": "yuv420p",
        "audio_codec": "aac",
        "duration": 30.0,
        "width": 720,
        "height": 1280,
        "frame_rate": 30.0,
        "size": Path(path).stat().st_size,
    }


class BackfillTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch(
            "scripts.x_post_daily_runner.FIXED_DAILY_WORK_DIR",
            Path(tempfile.gettempdir()).resolve(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, _temporary):
        return replace(
            test_config(),
            work_dir=str(Path(tempfile.gettempdir()).resolve()),
            repair_url=(
                "http://127.0.0.1:18820/internal/x-post-media-repair"
            ),
            repair_token="repair-secret",
        )

    def test_env_files_are_parsed_as_data_without_expansion_or_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            daily = Path(temporary) / "daily.env"
            token = Path(temporary) / "repair.token"
            daily.write_text(
                "\n".join(
                    (
                        "X_POST_DAILY_INTERNAL_TOKEN=$NOT_EXPANDED",
                        "X_POST_DAILY_MYSQL_PASSWORD='$(not-executed)'",
                        "X_POST_DAILY_REPAIR_URL="
                        "http://127.0.0.1:18820/"
                        "internal/x-post-media-repair",
                    )
                ),
                encoding="utf-8",
            )
            token.write_text(
                "X_POST_MEDIA_REPAIR_TOKEN=${TOKEN_NOT_EXPANDED}\n",
                encoding="utf-8",
            )

            values = load_environment_files(daily, token)

            self.assertEqual(
                values["X_POST_DAILY_INTERNAL_TOKEN"], "$NOT_EXPANDED"
            )
            self.assertEqual(
                values["X_POST_DAILY_MYSQL_PASSWORD"],
                "$(not-executed)",
            )
            self.assertEqual(
                values["X_POST_MEDIA_REPAIR_TOKEN"],
                "${TOKEN_NOT_EXPANDED}",
            )

            daily.write_text("source /tmp/unsafe.env\n", encoding="utf-8")
            with self.assertRaises(BackfillError):
                load_environment_files(daily, token)
            daily.write_text(
                "X_POST_DAILY_UNKNOWN=1\n", encoding="utf-8"
            )
            with self.assertRaises(BackfillError):
                load_environment_files(daily, token)

    def test_requires_unique_explicit_material_ids(self):
        self.assertEqual(normalize_material_ids(["10", "11"]), ["10", "11"])
        for values in ([], ["0"], ["01"], ["10", "10"], ["bad"]):
            with self.subTest(values=values):
                with self.assertRaises(BackfillError):
                    normalize_material_ids(values)

    def test_only_explicit_available_material_is_repaired_and_error_is_cleared(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(temporary)
            sidecar = FakeSidecar((10, 11, 12))
            connection = FakeConnection()
            lock_paths = []

            class Repair:
                def __init__(self):
                    self.calls = []

                def repair(self, payload):
                    self.calls.append(dict(payload))
                    return repair_response(payload["job_key"])

            repair = Repair()
            result = execute_backfill(
                config,
                ["10"],
                sidecar=sidecar,
                repair_client=repair,
                connection_factory=lambda _config: connection,
                pool_candidate_loader=hydrated_loader,
                downloader=media_downloader,
                prober=media_prober,
                lock_factory=lambda path: acquired_lock(path, lock_paths),
                now=NOW,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["ready_count"], 1)
        self.assertEqual(result["repair_attempted_count"], 1)
        self.assertEqual([item["material_id"] for item in result["results"]], ["10"])
        self.assertEqual(result["results"][0]["status"], "repaired_ready")
        self.assertEqual(len(repair.calls), 1)
        self.assertEqual(repair.calls[0]["material_id"], "10")
        self.assertEqual(lock_paths, [config.lock_path])
        self.assertTrue(connection.closed)
        check_event = next(item for item in sidecar.events if item[0] == "checks")
        self.assertEqual(
            check_event[2],
            [
                {
                    "pool_item_id": 10,
                    "error_code": "",
                    "error_message": "",
                }
            ],
        )
        self.assertEqual(
            [item[0] for item in sidecar.events],
            ["storage", "available", "checks"],
        )
        serialized = json.dumps(result)
        self.assertNotIn("https://", serialized)
        self.assertNotIn("repair-secret", serialized)

    def test_failed_repair_records_safe_error_and_continues_fifo(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(temporary)
            sidecar = FakeSidecar((10, 11))

            class Repair:
                def repair(self, payload):
                    if payload["material_id"] == "10":
                        raise MediaRepairError(
                            "gpu_repair_failed",
                            "source https://media.example.test/10.mp4 failed",
                            502,
                        )
                    return repair_response(payload["job_key"])

            result = execute_backfill(
                config,
                ["10", "11"],
                sidecar=sidecar,
                repair_client=Repair(),
                connection_factory=lambda _config: FakeConnection(),
                pool_candidate_loader=hydrated_loader,
                downloader=media_downloader,
                prober=media_prober,
                lock_factory=acquired_lock,
                now=NOW,
            )

        self.assertEqual(result["status"], "completed_with_failures")
        self.assertEqual(result["ready_count"], 1)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["repair_attempted_count"], 2)
        failed = result["results"][0]
        self.assertEqual(failed["material_id"], "10")
        self.assertEqual(failed["error_code"], "gpu_repair_failed")
        self.assertNotIn("https://", failed["error_message"])
        checks = next(item[2] for item in sidecar.events if item[0] == "checks")
        self.assertEqual(checks[0]["error_code"], "gpu_repair_failed")
        self.assertEqual(checks[1]["error_code"], "")

    def test_explicit_backfill_does_not_apply_the_daily_batch_repair_cap(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                self.config(temporary),
                max_repairs_per_run=1,
            )
            requested = [str(value) for value in range(10, 19)]
            sidecar = FakeSidecar(tuple(range(10, 19)))

            class Repair:
                def __init__(self):
                    self.calls = []

                def repair(self, payload):
                    self.calls.append(payload["material_id"])
                    return repair_response(payload["job_key"])

            repair = Repair()
            result = execute_backfill(
                config,
                requested,
                sidecar=sidecar,
                repair_client=repair,
                connection_factory=lambda _config: FakeConnection(),
                pool_candidate_loader=hydrated_loader,
                downloader=media_downloader,
                prober=media_prober,
                lock_factory=acquired_lock,
                now=NOW,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["ready_count"], 9)
        self.assertEqual(result["repair_attempted_count"], 9)
        self.assertEqual(repair.calls, requested)

    def test_unavailable_requested_material_is_never_hydrated_or_mutated(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(temporary)
            sidecar = FakeSidecar((10, 11))
            loaded = []

            def loader(connection, pool_items, source_date, limit, schema):
                loaded.extend(item["material_id"] for item in pool_items)
                return hydrated_loader(
                    connection, pool_items, source_date, limit, schema
                )

            class Repair:
                def repair(self, payload):
                    return repair_response(payload["job_key"])

            result = execute_backfill(
                config,
                ["10", "99"],
                sidecar=sidecar,
                repair_client=Repair(),
                connection_factory=lambda _config: FakeConnection(),
                pool_candidate_loader=loader,
                downloader=media_downloader,
                prober=media_prober,
                lock_factory=acquired_lock,
                now=NOW,
            )

        self.assertEqual(loaded, ["10"])
        self.assertEqual(result["failed_count"], 1)
        unavailable = next(
            item for item in result["results"] if item["material_id"] == "99"
        )
        self.assertEqual(
            unavailable["error_code"],
            "x_post_backfill_material_not_available",
        )
        checks = next(item[2] for item in sidecar.events if item[0] == "checks")
        self.assertEqual([item["pool_item_id"] for item in checks], [10])

    def test_same_daily_lock_blocks_all_sidecar_and_database_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(temporary)

            @contextlib.contextmanager
            def locked(path):
                self.assertEqual(path, config.lock_path)
                yield None

            class ForbiddenSidecar:
                def __getattr__(self, name):
                    raise AssertionError("%s must not run while locked" % name)

            result = execute_backfill(
                config,
                ["10"],
                sidecar=ForbiddenSidecar(),
                repair_client=object(),
                connection_factory=lambda _config: self.fail(
                    "database must not be opened while locked"
                ),
                lock_factory=locked,
                now=NOW,
            )

        self.assertEqual(result["status"], "skipped_locked")
        self.assertEqual(result["results"], [])

    def test_report_is_atomic_and_contains_only_sanitized_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "backfill.json"
            report.write_text("old", encoding="utf-8")
            result = {
                "status": "completed",
                "results": [
                    {
                        "material_id": "10",
                        "status": "repaired_ready",
                    }
                ],
            }

            _atomic_write_report(report.resolve(), result)

            loaded = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(loaded, result)
            self.assertEqual(list(Path(temporary).glob("*.tmp")), [])
            with self.assertRaises(BackfillError):
                _atomic_write_report(Path("relative.json"), result)
            self.assertEqual(_validate_report_path(report), report.resolve())

    def test_report_write_failure_preserves_completed_operation_counts(self):
        completed = {
            "status": "completed",
            "requested_count": 9,
            "available_count": 9,
            "ready_count": 9,
            "failed_count": 0,
            "repair_attempted_count": 9,
            "pool_checks_updated_count": 9,
            "results": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            report = str((Path(temporary) / "report.json").resolve())
            with mock.patch(
                "scripts.x_post_media_repair_backfill.load_environment_files",
                return_value={},
            ), mock.patch(
                "scripts.x_post_media_repair_backfill.DailyConfig.from_env",
                return_value=object(),
            ), mock.patch(
                "scripts.x_post_media_repair_backfill.execute_backfill",
                return_value=completed,
            ), mock.patch(
                "scripts.x_post_media_repair_backfill._atomic_write_report",
                side_effect=OSError("disk full"),
            ), mock.patch("builtins.print") as output:
                exit_code = main(
                    ["--material-id", "10", "--report-path", report]
                )

        self.assertEqual(exit_code, 1)
        written = json.loads(output.call_args.args[0])
        self.assertEqual(written["status"], "completed")
        self.assertEqual(written["ready_count"], 9)
        self.assertEqual(written["repair_attempted_count"], 9)
        self.assertEqual(written["report_status"], "failed")


if __name__ == "__main__":
    unittest.main()
