#!/usr/bin/env python3
"""Offline regression tests for the one-off X Post catch-up runner."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from scripts import x_post_catchup_runner as catchup  # noqa: E402
from scripts import x_post_daily_runner as daily  # noqa: E402


SHANGHAI = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 27, 11, 30, tzinfo=SHANGHAI)
CONFIGURED_IDS = tuple(range(2, 11))
PARENT_IDS = (2, 3, 4)
MISSING_IDS = tuple(range(5, 11))


def test_config(account_ids=CONFIGURED_IDS):
    return daily.DailyConfig(
        internal_url="http://127.0.0.1:8810",
        internal_token="unit-test-secret",
        account_ids=tuple(account_ids),
        start_date="2026-07-28",
        mysql_host="read-only.example.test",
        mysql_port=63350,
        mysql_user="reader",
        mysql_password="password",
        mysql_database="kunlunads_dev",
        mysql_connect_timeout=5,
        mysql_read_timeout=30,
        scan_limit=100,
        candidate_pool_limit=50,
        media_allowed_hosts=("media.example.test",),
        max_media_bytes=1024 * 1024,
        media_timeout=30,
        internal_timeout=30,
        lock_path="/run/x-post-daily/unit-test.lock",
        work_dir=str(Path(tempfile.gettempdir()).resolve()),
        material_keys_path="/internal/posts/material-keys/query",
        storage_preflight_path="/internal/posts/storage/preflight",
        failure_path="/internal/posts/runs/record-failure",
        plan_path="/internal/posts/daily-plan",
        publish_path_template="/internal/posts/queue/{queue_id}/publish",
    )


def parent_plan(
    *,
    status="completed",
    account_ids=PARENT_IDS,
    published_count=None,
    failed_count=0,
    unknown_count=0,
):
    expected_count = len(account_ids)
    if published_count is None:
        published_count = expected_count
    run = {
        "id": 4,
        "run_date": catchup.AUTHORIZED_RUN_DATE,
        "source_date": "2026-07-26",
        "status": status,
        "expected_count": expected_count,
        "queued_count": expected_count,
        "published_count": published_count,
        "failed_count": failed_count,
        "unknown_count": unknown_count,
        "started_at": "2026-07-27T02:00:00Z",
        "finished_at": "2026-07-27T02:03:00Z",
        "created_at": "2026-07-27T02:00:00Z",
        "updated_at": "2026-07-27T02:03:00Z",
    }
    queues = [
        {
            "id": 41 + index,
            "run_id": 4,
            "run_date": catchup.AUTHORIZED_RUN_DATE,
            "source_date": "2026-07-26",
            "account_id": account_id,
            "candidate_rank": index + 1,
            "status": (
                "published"
                if status == "completed"
                else ("failed" if index == expected_count - 1 else "published")
            ),
            "created_at": "2026-07-27T02:00:00Z",
            "updated_at": "2026-07-27T02:03:00Z",
        }
        for index, account_id in enumerate(account_ids)
    ]
    return {"found": True, "run": run, "queues": queues}


def child_run(
    *,
    status="queued",
    account_ids=MISSING_IDS,
    queued_count=None,
    published_count=0,
    failed_count=0,
    unknown_count=0,
):
    expected_count = len(account_ids)
    if queued_count is None:
        queued_count = 0 if status == "failed_preflight" else expected_count
    return {
        "id": 71,
        "parent_run_id": 4,
        "batch_kind": "catchup",
        "run_date": catchup.AUTHORIZED_RUN_DATE,
        "source_date": "2026-07-26",
        "reason": catchup.AUTHORIZED_REASON,
        "account_ids": list(account_ids),
        "status": status,
        "expected_count": expected_count,
        "queued_count": queued_count,
        "published_count": published_count,
        "failed_count": failed_count,
        "unknown_count": unknown_count,
        "error_code": (
            "x_post_catchup_pool_shortage"
            if status == "failed_preflight"
            else ""
        ),
        "error_message": (
            "not enough candidates"
            if status == "failed_preflight"
            else ""
        ),
        "started_at": "2026-07-27T03:00:00Z",
        "finished_at": "",
        "created_at": "2026-07-27T03:00:00Z",
        "updated_at": "2026-07-27T03:00:00Z",
    }


def child_queues(account_ids=MISSING_IDS, *, status="queued"):
    return [
        {
            "id": 201 + index,
            "run_id": None,
            "catchup_run_id": 71,
            "run_date": catchup.AUTHORIZED_RUN_DATE,
            "source_date": "2026-07-26",
            "account_id": account_id,
            "candidate_rank": index + 1,
            "status": status,
            "created_at": "2026-07-27T03:00:00Z",
            "updated_at": "2026-07-27T03:00:00Z",
        }
        for index, account_id in enumerate(account_ids)
    ]


def child_plan(*, status="queued", account_ids=MISSING_IDS):
    queues = [] if status == "failed_preflight" else child_queues(account_ids)
    return {
        "found": True,
        "run": child_run(status=status, account_ids=account_ids),
        "queues": queues,
    }


def candidate(material_id):
    return {
        "source_date": "2026-07-26",
        "material_key": str(material_id),
        "material_id": str(material_id),
        "pool_item_id": int(material_id),
        "pool_created_at": "2026-07-24T00:00:%02dZ" % (material_id % 60),
        "content_id": "C%s" % material_id,
        "material_url": "https://media.example.test/%s.mp4" % material_id,
        "material_name": "material-%s.mp4" % material_id,
        "material_language": "en",
        "drama_name": "Drama %s" % material_id,
        "tag": "Fantasy",
        "description": "A safe description.",
        "spend": 0.0,
        "facebook_violation_count": 0,
        "tiktok_violation_count": 0,
        "twitter_violation_count": 0,
        "resource_audit_count": 0,
    }


class FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeSidecar:
    def __init__(self, *, parent=None, child=None, pool_count=100):
        self.parent = parent if parent is not None else parent_plan()
        self.child = (
            child
            if child is not None
            else {"found": False, "run": None, "queues": []}
        )
        self.pool_count = pool_count
        self.events = []
        self.create_error = None
        self.rate_limit_queue_id = None

    def query_daily_plan(self, path, run_date):
        self.events.append(("parent_query", path, run_date))
        return self.parent

    def query_catchup_plan(self, run_date, parent_run_id, reason):
        self.events.append(
            ("child_query", run_date, parent_run_id, reason)
        )
        return self.child

    def preflight_storage(self, path):
        self.events.append(("storage", path))
        return {"ready": True, "mounted": True, "atomic_write": True}

    def verify_account(self, account_id):
        self.events.append(("verify", account_id))
        return {
            "id": account_id,
            "username": "account%s" % account_id,
            "x_user_id": "200%s" % account_id,
            "display_name": "Account %s" % account_id,
            "status": "active",
            "publish_eligible": True,
        }

    def available_pool_items(self, path, limit):
        self.events.append(("pool", path, limit))
        return [
            {
                "id": material_id,
                "material_id": str(material_id),
                "material_key": str(material_id),
                "created_at": "2026-07-24T00:00:%02dZ"
                % (material_id % 60),
            }
            for material_id in range(10, 10 + min(limit, self.pool_count))
        ]

    def record_pool_checks(self, path, checks):
        self.events.append(("pool_checks", path, list(checks)))
        return {"updated_count": len(checks)}

    def create_catchup_plan(
        self,
        run_date,
        source_date,
        parent_run_id,
        reason,
        candidates,
    ):
        self.events.append(
            (
                "create",
                run_date,
                source_date,
                parent_run_id,
                reason,
                [item["account_id"] for item in candidates],
                [item["material_id"] for item in candidates],
            )
        )
        if self.create_error is not None:
            raise self.create_error
        account_ids = tuple(item["account_id"] for item in candidates)
        return child_plan(account_ids=account_ids)

    def record_catchup_failure(
        self,
        run_date,
        source_date,
        parent_run_id,
        reason,
        expected_missing_count,
        error_code,
        error_message,
    ):
        self.events.append(
            (
                "failure",
                run_date,
                source_date,
                parent_run_id,
                reason,
                expected_missing_count,
                error_code,
                error_message,
            )
        )
        return {
            "recorded": True,
            "run": child_run(status="failed_preflight"),
        }

    def publish_queue(self, path_template, queue_id):
        self.events.append(("publish", queue_id))
        if queue_id == self.rate_limit_queue_id:
            raise daily.SidecarError(
                "rate_limit_exceeded",
                "rate limited",
                429,
            )
        return {
            "status": "published",
            "log_id": queue_id + 1000,
            "short_url": "https://ai.yingliangads.com/s2l/%s.html"
            % (queue_id + 1000),
            "post_id": str(queue_id),
            "preview_url": "https://x.com/account/status/%s" % queue_id,
        }


def run_new_catchup(sidecar, *, config=None, loaded_candidates=None):
    config = config or test_config()
    connection = FakeConnection()
    preflight_events = []

    def loader(
        _connection,
        pool_items,
        source_date,
        *,
        limit,
        schema,
    ):
        preflight_events.append(
            (
                "selector",
                [item["material_id"] for item in pool_items],
                source_date,
                limit,
                schema,
            )
        )
        return (
            loaded_candidates
            if loaded_candidates is not None
            else [candidate(value) for value in range(10, 16)]
        ), []

    def downloader(url, destination, allowed_hosts, max_bytes, timeout):
        preflight_events.append(("download", url))
        Path(destination).write_bytes(b"video")
        return {
            "size": 5,
            "sha256": "a" * 64,
            "media_type": "video/mp4",
        }

    def prober(path, max_bytes, timeout):
        preflight_events.append(("probe", Path(path).name))
        return {"duration": 30.0, "width": 720, "height": 1280}

    result = catchup.execute_catchup_run(
        config,
        run_date=catchup.AUTHORIZED_RUN_DATE,
        expected_missing_count=catchup.AUTHORIZED_EXPECTED_MISSING_COUNT,
        reason=catchup.AUTHORIZED_REASON,
        sidecar=sidecar,
        connection_factory=lambda _config: connection,
        pool_candidate_loader=loader,
        downloader=downloader,
        prober=prober,
        now=NOW,
    )
    return result, connection, preflight_events


class CatchupOrchestrationTests(unittest.TestCase):
    def test_new_child_uses_exact_difference_fifo_and_sequential_publish(self):
        sidecar = FakeSidecar()
        result, connection, preflight_events = run_new_catchup(sidecar)

        self.assertEqual(result["status"], "published")
        self.assertEqual(result["workflow"], "catchup")
        self.assertEqual(result["parent_run_id"], 4)
        self.assertEqual(result["planned_count"], 6)
        self.assertEqual(result["published_count"], 6)
        self.assertFalse(result["resumed_existing_plan"])
        self.assertEqual(
            sidecar.events[:10],
            [
                (
                    "parent_query",
                    "/internal/posts/daily-plan/query",
                    "2026-07-27",
                ),
                (
                    "child_query",
                    "2026-07-27",
                    4,
                    "scope_expansion_v1",
                ),
                ("storage", "/internal/posts/storage/preflight"),
                ("verify", 5),
                ("verify", 6),
                ("verify", 7),
                ("verify", 8),
                ("verify", 9),
                ("verify", 10),
                (
                    "pool",
                    "/internal/posts/material-pool/available",
                    100,
                ),
            ],
        )
        create_event = next(
            event for event in sidecar.events if event[0] == "create"
        )
        self.assertEqual(create_event[5], list(MISSING_IDS))
        self.assertEqual(create_event[6], [str(value) for value in range(10, 16)])
        self.assertEqual(
            [event[1] for event in sidecar.events if event[0] == "publish"],
            list(range(201, 207)),
        )
        self.assertEqual(
            len([event for event in preflight_events if event[0] == "download"]),
            6,
        )
        self.assertEqual(
            preflight_events[0][1][:6],
            [str(value) for value in range(10, 16)],
        )
        self.assertEqual(preflight_events[0][2], "2026-07-26")
        self.assertTrue(connection.closed)

    def test_existing_child_recovers_without_reselection_or_account_refresh(self):
        class ExistingSidecar(FakeSidecar):
            def preflight_storage(self, _path):
                raise AssertionError("frozen child must skip storage preflight")

            def verify_account(self, _account_id):
                raise AssertionError("frozen child must skip account refresh")

            def available_pool_items(self, _path, _limit):
                raise AssertionError("frozen child must skip pool selection")

            def create_catchup_plan(self, *_args):
                raise AssertionError("frozen child must not be recreated")

        sidecar = ExistingSidecar(child=child_plan())
        result = catchup.execute_catchup_run(
            test_config(),
            run_date=catchup.AUTHORIZED_RUN_DATE,
            expected_missing_count=6,
            reason=catchup.AUTHORIZED_REASON,
            sidecar=sidecar,
            connection_factory=lambda _config: self.fail(
                "source database must not be opened for frozen child"
            ),
            downloader=lambda *_args, **_kwargs: self.fail(
                "media must not be downloaded for frozen child"
            ),
            now=NOW,
        )

        self.assertEqual(result["status"], "published")
        self.assertTrue(result["resumed_existing_plan"])
        self.assertEqual(
            sidecar.events,
            [
                (
                    "parent_query",
                    "/internal/posts/daily-plan/query",
                    "2026-07-27",
                ),
                (
                    "child_query",
                    "2026-07-27",
                    4,
                    "scope_expansion_v1",
                ),
                ("publish", 201),
                ("publish", 202),
                ("publish", 203),
                ("publish", 204),
                ("publish", 205),
                ("publish", 206),
            ],
        )

    def test_existing_failed_preflight_child_never_reselects(self):
        sidecar = FakeSidecar(child=child_plan(status="failed_preflight"))
        result = catchup.execute_catchup_run(
            test_config(),
            run_date=catchup.AUTHORIZED_RUN_DATE,
            expected_missing_count=6,
            reason=catchup.AUTHORIZED_REASON,
            sidecar=sidecar,
            connection_factory=lambda _config: self.fail(
                "failed frozen child must not be rebuilt"
            ),
            now=NOW,
        )

        self.assertEqual(result["status"], "failed_preflight")
        self.assertTrue(result["resumed_existing_plan"])
        self.assertEqual(
            [event[0] for event in sidecar.events],
            ["parent_query", "child_query"],
        )

    def test_missing_scope_must_equal_exactly_six_and_is_audited(self):
        sidecar = FakeSidecar()
        with self.assertRaises(daily.DailyRunError) as captured:
            run_new_catchup(
                sidecar,
                config=test_config(account_ids=tuple(range(2, 10))),
            )

        self.assertEqual(
            captured.exception.code,
            "x_post_catchup_missing_scope_conflict",
        )
        self.assertEqual(
            [event[0] for event in sidecar.events],
            ["parent_query", "child_query", "failure"],
        )
        self.assertEqual(sidecar.events[-1][5], 6)

    def test_parent_must_be_fully_published_before_new_child(self):
        parent = parent_plan(
            status="completed_with_errors",
            published_count=2,
            failed_count=1,
        )
        sidecar = FakeSidecar(parent=parent)
        with self.assertRaises(daily.DailyRunError) as captured:
            run_new_catchup(sidecar)

        self.assertEqual(
            captured.exception.code,
            "x_post_catchup_parent_not_complete",
        )
        self.assertEqual(
            [event[0] for event in sidecar.events],
            ["parent_query", "child_query", "failure"],
        )

    def test_pool_shortage_never_creates_or_publishes(self):
        sidecar = FakeSidecar(pool_count=5)
        with self.assertRaises(daily.DailyRunError) as captured:
            run_new_catchup(sidecar)

        self.assertEqual(
            captured.exception.code,
            "x_post_catchup_pool_shortage",
        )
        event_names = [event[0] for event in sidecar.events]
        self.assertIn("failure", event_names)
        self.assertNotIn("create", event_names)
        self.assertNotIn("publish", event_names)

    def test_unknown_create_response_is_not_overwritten_by_failure_record(self):
        sidecar = FakeSidecar()
        sidecar.create_error = daily.SidecarError(
            "x_sidecar_unreachable",
            "response lost",
            503,
            unknown_outcome=True,
        )
        with self.assertRaises(daily.SidecarError):
            run_new_catchup(sidecar)

        event_names = [event[0] for event in sidecar.events]
        self.assertIn("create", event_names)
        self.assertNotIn("failure", event_names)
        self.assertNotIn("publish", event_names)

    def test_known_create_rollback_records_catchup_failure(self):
        sidecar = FakeSidecar()
        sidecar.create_error = daily.SidecarError(
            "x_catchup_plan_conflict",
            "known transactional rollback",
            409,
            unknown_outcome=False,
        )
        with self.assertRaises(daily.SidecarError):
            run_new_catchup(sidecar)

        event_names = [event[0] for event in sidecar.events]
        self.assertEqual(event_names[-2:], ["create", "failure"])
        self.assertNotIn("publish", event_names)

    def test_rate_limit_stops_remaining_frozen_child_queues(self):
        sidecar = FakeSidecar(child=child_plan())
        sidecar.rate_limit_queue_id = 203
        result = catchup.execute_catchup_run(
            test_config(),
            run_date=catchup.AUTHORIZED_RUN_DATE,
            expected_missing_count=6,
            reason=catchup.AUTHORIZED_REASON,
            sidecar=sidecar,
            now=NOW,
        )

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(
            [event[1] for event in sidecar.events if event[0] == "publish"],
            [201, 202, 203],
        )


class CatchupScopeAndClientTests(unittest.TestCase):
    def test_cli_requires_all_three_explicit_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                catchup.parse_args([])
        parsed = catchup.parse_args(
            [
                "--run-date",
                "2026-07-27",
                "--expected-missing-count",
                "6",
                "--reason",
                "scope_expansion_v1",
            ]
        )
        self.assertEqual(parsed.run_date, "2026-07-27")
        self.assertEqual(parsed.expected_missing_count, 6)
        self.assertEqual(parsed.reason, "scope_expansion_v1")

    def test_invocation_is_pinned_to_authorized_scope_and_beijing_day(self):
        current = catchup._validate_authorized_invocation(
            "2026-07-27",
            6,
            "scope_expansion_v1",
            now=NOW,
        )
        self.assertEqual(current.date().isoformat(), "2026-07-27")
        for run_date, count, reason, now in (
            ("2026-07-26", 6, "scope_expansion_v1", NOW),
            ("2026-07-27", 5, "scope_expansion_v1", NOW),
            ("2026-07-27", 6, "another_reason", NOW),
            (
                "2026-07-27",
                6,
                "scope_expansion_v1",
                datetime(2026, 7, 28, 0, 1, tzinfo=SHANGHAI),
            ),
        ):
            with self.assertRaises(daily.DailyRunError):
                catchup._validate_authorized_invocation(
                    run_date,
                    count,
                    reason,
                    now=now,
                )

    def test_client_posts_exact_query_create_and_failure_contracts(self):
        class RecordingClient(catchup.CatchupSidecarClient):
            def __init__(self):
                self.calls = []

            def post(self, path, payload, write_may_have_happened=False):
                self.calls.append(
                    (path, payload, write_may_have_happened)
                )
                if path == catchup.CATCHUP_QUERY_PATH:
                    return {
                        "item": {
                            "found": False,
                            "run": None,
                            "queues": [],
                        }
                    }
                if path == catchup.CATCHUP_CREATE_PATH:
                    item = child_run()
                    item["queues"] = child_queues()
                    item["created"] = True
                    item["missing_account_ids"] = list(MISSING_IDS)
                    return {
                        "item": item
                    }
                item = child_run(status="failed_preflight")
                item["recorded"] = True
                item["missing_account_ids"] = list(MISSING_IDS)
                return {
                    "item": item
                }

        client = RecordingClient()
        self.assertFalse(
            client.query_catchup_plan(
                "2026-07-27",
                4,
                "scope_expansion_v1",
            )["found"]
        )
        candidates = [
            {
                "account_id": account_id,
                "material_id": str(10 + index),
                "pool_item_id": 10 + index,
            }
            for index, account_id in enumerate(MISSING_IDS)
        ]
        created = client.create_catchup_plan(
            "2026-07-27",
            "2026-07-26",
            4,
            "scope_expansion_v1",
            candidates,
        )
        self.assertEqual(created["run"]["account_ids"], list(MISSING_IDS))
        client.record_catchup_failure(
            "2026-07-27",
            "2026-07-26",
            4,
            "scope_expansion_v1",
            6,
            "x_post_catchup_pool_shortage",
            "not enough candidates",
        )

        query_path, query_body, query_write = client.calls[0]
        self.assertEqual(query_path, "/internal/posts/catchup-plan/query")
        self.assertEqual(
            query_body,
            {
                "run_date": "2026-07-27",
                "parent_run_id": 4,
                "reason": "scope_expansion_v1",
            },
        )
        self.assertFalse(query_write)

        create_path, create_body, create_write = client.calls[1]
        self.assertEqual(create_path, "/internal/posts/catchup-plan")
        self.assertEqual(
            set(create_body),
            {
                "run_date",
                "source_date",
                "parent_run_id",
                "reason",
                "candidates",
            },
        )
        self.assertNotIn("expected_count", create_body)
        self.assertTrue(create_write)

        failure_path, failure_body, failure_write = client.calls[2]
        self.assertEqual(
            failure_path,
            "/internal/posts/catchup-runs/record-failure",
        )
        self.assertEqual(
            set(failure_body),
            {
                "run_date",
                "source_date",
                "parent_run_id",
                "reason",
                "expected_missing_count",
                "error_code",
                "error_message",
            },
        )
        self.assertTrue(failure_write)

    def test_snapshot_rejects_child_queue_identity_drift(self):
        invalid = child_plan()
        invalid["queues"][0]["account_id"] = 999
        with self.assertRaises(daily.SidecarError):
            catchup._normalize_catchup_snapshot(
                invalid,
                "2026-07-27",
                4,
                "scope_expansion_v1",
            )


if __name__ == "__main__":
    unittest.main()
