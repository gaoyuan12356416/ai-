#!/usr/bin/env python3
"""Offline tests for exact bound-drama media recovery orchestration."""

import contextlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.x_post_media_repair_backfill import BackfillError  # noqa: E402
from scripts.x_post_bound_drama_media_recovery import (  # noqa: E402
    execute_recovery,
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


if __name__ == "__main__":
    unittest.main()
