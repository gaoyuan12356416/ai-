#!/usr/bin/env python3
"""Offline safety tests for exact drama media repair and restoration."""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts.service import XPostError  # noqa: E402
from scripts.test_x_post_schedule_runner import make_config  # noqa: E402
from scripts.x_post_drama_media_repair_backfill import (  # noqa: E402
    BackfillError,
    execute_backfill,
    load_drama_environment_files,
    main,
    normalize_items,
)


ITEMS = [
    {
        "pool_item_id": 53,
        "content_id": "3CRScaBEY0",
        "episode_number": 1,
        "expected_error_code": "source_not_repairable",
    },
    {
        "pool_item_id": 54,
        "content_id": "zuMg6fyfSs",
        "episode_number": 1,
        "expected_error_code": "source_not_repairable",
    },
]


@contextlib.contextmanager
def acquired_lock(_path):
    yield object()


class FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeSidecar:
    def __init__(self):
        self.calls = []

    def preflight_storage(self, path):
        self.calls.append(("storage", path))
        return {"ready": True, "mounted": True, "atomic_write": True}

    def record_drama_pool_checks(
        self,
        path,
        checks,
        *,
        validate_only=False,
    ):
        self.calls.append((path, [dict(item) for item in checks], validate_only))
        return {
            "updated_count": 0 if validate_only else len(checks),
            "validated_count": len(checks),
            "validate_only": validate_only,
        }

    def create_schedule_plan(self, *_args, **_kwargs):
        raise AssertionError("drama repair backfill must never create a plan")

    def publish_queue(self, *_args, **_kwargs):
        raise AssertionError("drama repair backfill must never publish")


class DramaMediaRepairBackfillTests(unittest.TestCase):
    def test_loads_strict_schedule_overrides_used_by_the_live_scheduler(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            daily = root / "daily.env"
            token = root / "repair.token"
            schedule = root / "schedule.env"
            daily.write_text(
                "X_POST_DAILY_MEDIA_ALLOWED_HOSTS=daily.example.test\n",
                encoding="utf-8",
            )
            token.write_text(
                "X_POST_MEDIA_REPAIR_TOKEN=repair-secret\n",
                encoding="utf-8",
            )
            schedule.write_text(
                "X_POST_SCHEDULE_MEDIA_ALLOWED_HOSTS="
                "daily.example.test,drama.example.test\n"
                "X_POST_SCHEDULE_DRAMA_APP_ID=1015\n",
                encoding="utf-8",
            )
            values = load_drama_environment_files(
                daily,
                token,
                schedule,
            )
            self.assertEqual(
                values["X_POST_SCHEDULE_MEDIA_ALLOWED_HOSTS"],
                "daily.example.test,drama.example.test",
            )
            self.assertEqual(
                values["X_POST_SCHEDULE_DRAMA_APP_ID"],
                "1015",
            )
            self.assertEqual(
                values["X_POST_MEDIA_REPAIR_TOKEN"],
                "repair-secret",
            )

            schedule.write_text(
                "X_POST_SCHEDULE_UNSUPPORTED=unsafe\n",
                encoding="utf-8",
            )
            with self.assertRaises(BackfillError) as raised:
                load_drama_environment_files(daily, token, schedule)
            self.assertEqual(
                raised.exception.code,
                "x_post_backfill_config_invalid",
            )
            for secret_key in (
                "X_POST_SCHEDULE_INTERNAL_TOKEN",
                "X_POST_SCHEDULE_MYSQL_PASSWORD",
                "X_POST_SCHEDULE_REPAIR_TOKEN",
            ):
                with self.subTest(secret_key=secret_key):
                    schedule.write_text(
                        "%s=must-stay-in-dedicated-config\n" % secret_key,
                        encoding="utf-8",
                    )
                    with self.assertRaises(BackfillError):
                        load_drama_environment_files(
                            daily,
                            token,
                            schedule,
                        )

    def test_unexpected_failure_output_never_echoes_exception_secrets(self):
        sentinel = (
            "https://source.example.test/private.mp4?"
            "token=must-not-leak"
        )
        with mock.patch(
            "scripts.x_post_drama_media_repair_backfill."
            "load_drama_environment_files",
            side_effect=RuntimeError(sentinel),
        ), mock.patch("builtins.print") as output:
            exit_code = main(
                [
                    "--pool-item-id",
                    "53",
                    "--content-id",
                    "3CRScaBEY0",
                    "--episode-number",
                    "1",
                    "--expected-error-code",
                    "source_not_repairable",
                ]
            )
        self.assertEqual(exit_code, 1)
        result = json.loads(output.call_args.args[0])
        self.assertEqual(
            result["error_code"],
            "x_post_drama_backfill_unexpected_error",
        )
        self.assertEqual(result["error_message"], "RuntimeError")
        self.assertNotIn("must-not-leak", output.call_args.args[0])

    def test_exact_item_contract_rejects_mismatch_and_duplicates(self):
        normalized = normalize_items(
            ["53", "54"],
            ["3CRScaBEY0", "zuMg6fyfSs"],
            ["1", "1"],
            ["source_not_repairable", "source_not_repairable"],
        )
        self.assertEqual(normalized, ITEMS)
        invalid = (
            (["53"], [], ["1"], ["source_not_repairable"]),
            (["53", "53"], ["A", "A"], ["1", "1"], ["x", "x"]),
            (["0"], ["A"], ["1"], ["x"]),
            (["53"], ["bad/content"], ["1"], ["x"]),
        )
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(BackfillError):
                    normalize_items(*values)

    def test_repairs_every_item_before_one_guarded_restore_and_never_publishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                make_config(temporary),
                repair_url=(
                    "http://127.0.0.1:18820/internal/x-post-media-repair"
                ),
                repair_token="repair-secret",
            )
            sidecar = FakeSidecar()
            connection = FakeConnection()
            selected = []

            def selector(_connection, pool, *, account_ids, schema, app_id):
                selected.append((dict(pool[0]), list(account_ids), schema, app_id))
                requested = pool[0]
                return [
                    {
                        "episode_number": requested["next_sub_number"],
                        "sub_num": requested["next_sub_number"],
                        "episode_key": "%s:%s"
                        % (
                            requested["content_id"],
                            requested["next_sub_number"],
                        ),
                        "material_id": "a" * 32,
                        "material_url": "https://media.example.test/source.mp4",
                        "material_name": "Episode",
                        "material_language": "en",
                        "drama_name": "Drama",
                        "tag": "Drama",
                        "name_tag": "#Drama",
                        "description": "Description",
                        "content_id": requested["content_id"],
                    }
                ]

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
                self.assertIsNotNone(repair_client)
                repair_state["attempted"] += 1
                return {
                    **candidate,
                    "media_repair_job_key": "b" * 64,
                    "media_repair_profile": config.repair_profile,
                    "preflight_sha256": "c" * 64,
                    "preflight_size": 123,
                    "preflight_duration": 139.0,
                }

            with mock.patch(
                "scripts.x_post_drama_media_repair_backfill."
                "select_drama_pool_episodes",
                side_effect=selector,
            ), mock.patch(
                "scripts.x_post_drama_media_repair_backfill."
                "_preflight_candidate",
                side_effect=preflight,
            ):
                result = execute_backfill(
                    config,
                    ITEMS,
                    sidecar=sidecar,
                    repair_client=object(),
                    connection_factory=lambda _config: connection,
                    downloader=object(),
                    prober=object(),
                    lock_factory=acquired_lock,
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["ready_count"], 2)
        self.assertEqual(result["restored_count"], 2)
        self.assertEqual(result["repair_attempted_count"], 2)
        self.assertTrue(connection.closed)
        self.assertEqual(len(selected), 2)
        check_calls = [
            call for call in sidecar.calls if call[0] != "storage"
        ]
        self.assertEqual([call[2] for call in check_calls], [True, False])
        for check in check_calls[1][1]:
            self.assertEqual(check["error_code"], "")
            self.assertEqual(
                check["expected_error_code"],
                "source_not_repairable",
            )

    def test_failed_preflight_never_clears_validation_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                make_config(temporary),
                repair_url=(
                    "http://127.0.0.1:18820/internal/x-post-media-repair"
                ),
                repair_token="repair-secret",
            )
            sidecar = FakeSidecar()
            connection = FakeConnection()
            candidate = {
                "episode_number": 1,
                "material_id": "a" * 32,
                "material_url": "https://media.example.test/source.mp4",
            }
            with mock.patch(
                "scripts.x_post_drama_media_repair_backfill."
                "select_drama_pool_episodes",
                return_value=[candidate],
            ), mock.patch(
                "scripts.x_post_drama_media_repair_backfill."
                "_preflight_candidate",
                side_effect=XPostError(
                    "source_not_repairable",
                    "source is too short",
                    422,
                ),
            ):
                with self.assertRaises(XPostError):
                    execute_backfill(
                        config,
                        ITEMS[:1],
                        sidecar=sidecar,
                        repair_client=object(),
                        connection_factory=lambda _config: connection,
                        downloader=object(),
                        prober=object(),
                        lock_factory=acquired_lock,
                    )
        check_calls = [
            call for call in sidecar.calls if call[0] != "storage"
        ]
        self.assertEqual(len(check_calls), 1)
        self.assertTrue(check_calls[0][2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
