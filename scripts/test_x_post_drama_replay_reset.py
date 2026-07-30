#!/usr/bin/env python3
"""Offline safety tests for the X short-drama replay operator command."""

import contextlib
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts import service  # noqa: E402
from scripts import x_post_drama_replay_reset as replay  # noqa: E402


@contextlib.contextmanager
def acquired_lock(_path):
    yield object()


@contextlib.contextmanager
def unavailable_lock(_path):
    yield None


class DramaReplayResetCommandTests(unittest.TestCase):
    def test_apply_requires_exact_policy_confirmation(self):
        with self.assertRaises(service.XPostError) as rejected:
            replay.execute_replay_reset(
                [2],
                [],
                actor_user_id="admin-1",
                actor_name="Admin",
                apply=True,
                confirmation="yes",
                db_path=Path("unused.sqlite3"),
                lock_factory=acquired_lock,
            )
        self.assertEqual(
            rejected.exception.code,
            "x_post_drama_replay_confirmation_required",
        )

    def test_dry_run_calls_store_with_validate_only(self):
        store = mock.Mock()
        store.reset_drama_pool_for_replay.return_value = {
            "validate_only": True,
            "validated_count": 1,
            "reset_count": 0,
        }
        with mock.patch.object(replay, "XPostStore", return_value=store):
            result = replay.execute_replay_reset(
                [2],
                [{"pool_item_id": 2}],
                actor_user_id="admin-1",
                actor_name="Admin",
                db_path=Path("unused.sqlite3"),
                lock_factory=acquired_lock,
            )
        self.assertEqual(result["status"], "validated")
        store.reset_drama_pool_for_replay.assert_called_once_with(
            [2],
            actor={"user_id": "admin-1", "name": "Admin"},
            reason=service.DRAMA_REPLAY_REASON,
            expected_snapshots=[{"pool_item_id": 2}],
            validate_only=True,
        )

    def test_apply_calls_store_only_after_confirmation(self):
        store = mock.Mock()
        store.reset_drama_pool_for_replay.return_value = {
            "validate_only": False,
            "validated_count": 1,
            "reset_count": 1,
        }
        with mock.patch.object(replay, "XPostStore", return_value=store):
            result = replay.execute_replay_reset(
                [2],
                [{"pool_item_id": 2}],
                actor_user_id="admin-1",
                actor_name="Admin",
                apply=True,
                confirmation=service.DRAMA_REPLAY_REASON,
                db_path=Path("unused.sqlite3"),
                lock_factory=acquired_lock,
            )
        self.assertEqual(result["status"], "reset")
        self.assertFalse(
            store.reset_drama_pool_for_replay.call_args.kwargs[
                "validate_only"
            ]
        )

    def test_unavailable_publish_lock_causes_no_write(self):
        with mock.patch.object(replay, "XPostStore") as store_class:
            result = replay.execute_replay_reset(
                [2],
                [],
                actor_user_id="admin-1",
                actor_name="Admin",
                db_path=Path("unused.sqlite3"),
                lock_factory=unavailable_lock,
            )
        self.assertEqual(result["status"], "skipped_locked")
        store_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
