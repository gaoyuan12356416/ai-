#!/usr/bin/env python3
"""Offline tests for the minute-based X Post schedule runner."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.x_post_daily_runner import (  # noqa: E402
    CandidatePreflightError,
    SidecarError,
)
from features.x_posts.service import (  # noqa: E402
    XPostError,
    _material_fifo_selection_matches,
)
from features.x_posts.drama_selector import DramaPoolRejection  # noqa: E402
from scripts.x_post_schedule_runner import (  # noqa: E402
    ScheduleConfig,
    ScheduleRunError,
    ScheduleSidecarClient,
    _drama_candidates,
    _material_candidates,
    _preflight_material_candidates,
    _retrying_media_downloader,
    execute_schedule_tick,
)
from scripts.x_post_schedule_claim_runner import execute_claim_tick  # noqa: E402


BEIJING = timezone(timedelta(hours=8))


def make_config(work_dir):
    return ScheduleConfig(
        internal_url="http://127.0.0.1:8810",
        internal_token="daily-test-bearer",
        start_date="2026-07-01",
        mysql_host="readonly.example.test",
        mysql_port=63350,
        mysql_user="reader",
        mysql_password="secret-placeholder",
        mysql_database="kunlunads_dev",
        mysql_connect_timeout=5,
        mysql_read_timeout=30,
        scan_limit=100,
        candidate_pool_limit=50,
        media_allowed_hosts=("media.example.test",),
        max_media_bytes=512 * 1024 * 1024,
        media_timeout=30,
        internal_timeout=30,
        lock_path=str(Path(work_dir) / "runner.lock"),
        work_dir=str(Path(work_dir).resolve()),
        grace_seconds=90,
        max_due_batches=10,
        due_path="/internal/posts/schedules/due",
        plan_query_path="/internal/posts/schedule-plan/query",
        plan_path="/internal/posts/schedule-plan",
        failure_path="/internal/posts/schedule-runs/record-failure",
        material_pool_path="/internal/posts/material-pool/available",
        material_check_path="/internal/posts/material-pool/check",
        drama_pool_path="/internal/posts/drama-pool/available",
        drama_check_path="/internal/posts/drama-pool/check",
        storage_preflight_path="/internal/posts/storage/preflight",
        publish_path_template="/internal/posts/queue/{queue_id}/publish",
    )


class ScheduleConfigTest(unittest.TestCase):
    def test_schedule_repair_budget_overrides_legacy_daily_budget(self):
        with mock.patch.dict(
            "os.environ",
            {
                "X_POST_SCHEDULE_MAX_REPAIRS_PER_RUN": "17",
                "X_POST_DAILY_MAX_REPAIRS_PER_RUN": "14",
                "X_POST_SCHEDULE_MEDIA_ALLOWED_HOSTS": "media.example.test",
            },
            clear=True,
        ):
            config = ScheduleConfig.from_env()

        self.assertEqual(config.max_repairs_per_run, 17)

    def test_schedule_env_example_covers_full_batch_repair_budget(self):
        example = (ROOT / "deploy" / "x-post-schedule.env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("X_POST_SCHEDULE_MAX_REPAIRS_PER_RUN=17", example)


def due_item(
    source_type="material",
    publish_time="10:00",
    accounts=None,
    frozen=False,
):
    return {
        "source_type": source_type,
        "run_date": "2026-07-27",
        "publish_time": publish_time,
        "version": 3,
        "account_ids": list(accounts or [11, 12]),
        "frozen": bool(frozen),
    }


def queue(
    queue_id,
    account_id,
    rank,
    status="queued",
    unknown=False,
    error_code="",
):
    return {
        "id": queue_id,
        "account_id": account_id,
        "candidate_rank": rank,
        "status": status,
        "unknown_outcome": unknown,
        "error_code": error_code,
    }


def frozen_run(source_type, account_count):
    return {
        "id": 7,
        "source_type": source_type,
        "run_date": "2026-07-27",
        "publish_time": "10:00",
        "config_version": 3,
        "account_ids": list(range(11, 11 + account_count)),
        "expected_count": account_count,
    }


class FakeSidecar:
    def __init__(self, due=None):
        self.due = list(due or [])
        self.existing = {}
        self.calls = []
        self.publish_errors = {}
        self.created_queues = None
        self.next_queue_id = 101

    def due_schedules(self, path, *, current, grace_seconds, limit):
        self.calls.append(("due", path, current, grace_seconds, limit))
        return list(self.due)

    def query_schedule_plan(self, path, identity):
        self.calls.append(("query", path, identity["source_type"]))
        return self.existing.get(
            (
                identity["source_type"],
                identity["run_date"],
                identity["publish_time"],
                identity["version"],
            ),
            {"found": False, "run": None, "queues": []},
        )

    def preflight_storage(self, path):
        self.calls.append(("storage", path))
        return {"ready": True, "mounted": True, "atomic_write": True}

    def verify_account(self, account_id):
        self.calls.append(("verify", account_id))
        return {
            "id": account_id,
            "username": "account%s" % account_id,
            "x_user_id": "x%s" % account_id,
            "display_name": "Account %s" % account_id,
            "status": "active",
            "publish_eligible": True,
        }

    def create_schedule_plan(self, path, payload):
        self.calls.append(("create", path, payload))
        if self.created_queues is not None:
            return list(self.created_queues)
        queues = []
        for rank, account_id in enumerate(payload["account_ids"], 1):
            queues.append(
                queue(self.next_queue_id, account_id, rank)
            )
            self.next_queue_id += 1
        return queues

    def record_schedule_failure(self, path, identity, code, message):
        self.calls.append(
            (
                "failure",
                path,
                identity["source_type"],
                code,
                message,
                dict(identity),
            )
        )
        return {
            **identity,
            "config_version": identity["version"],
            "status": "failed_preflight",
            "recorded": True,
        }

    def publish_queue(self, path_template, queue_id):
        self.calls.append(("publish", queue_id))
        if queue_id in self.publish_errors:
            raise self.publish_errors[queue_id]
        return {
            "status": "published",
            "log_id": 1000 + queue_id,
            "preview_url": "https://x.com/example/status/%s" % queue_id,
        }


class StubScheduleClient(ScheduleSidecarClient):
    def __init__(self, responses):
        super().__init__("http://127.0.0.1:8810", "test-token", timeout=5)
        self.responses = list(responses)
        self.requests = []

    def post(self, path, payload, write_may_have_happened=False):
        self.requests.append((path, payload, write_may_have_happened))
        if not self.responses:
            raise AssertionError("unexpected sidecar request")
        return self.responses.pop(0)


def candidate_loader(
    _config,
    _sidecar,
    accounts,
    *,
    source_date,
    connection_factory,
    downloader,
    prober,
    repair_client,
    timestamp,
):
    del connection_factory, downloader, prober, repair_client, timestamp
    return [
        {
            "account_id": account["id"],
            "source_date": source_date,
            "material_id": str(9000 + account["id"]),
        }
        for account in accounts
    ]


class ScheduleRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config = make_config(self.temporary.name)
        self.now = datetime(2026, 7, 27, 10, 0, 30, tzinfo=BEIJING)

    def test_material_language_without_target_is_current_fifo_skip(self):
        candidates = [
            {
                "pool_item_id": 2,
                "material_id": "ja-newest",
                "material_language": "ja",
                "media_kind": "video",
                "source_duration": 30,
            },
            {
                "pool_item_id": 1,
                "material_id": "en-next",
                "material_language": "en",
                "media_kind": "video",
                "source_duration": 30,
                "content_id": "EN1",
                "material_url": "https://media.example.test/en.mp4",
                "material_name": "Episode",
                "drama_name": "Drama",
                "tag": "Drama",
                "description": "A complete episode description.",
            },
        ]
        accounts = [
            {
                "id": 11,
                "username": "english11",
                "x_user_id": "x11",
                "display_name": "English 11",
                "drama_language": "en",
                "long_video_eligible": False,
            }
        ]
        planned, failures = _preflight_material_candidates(
            self.config,
            mock.Mock(),
            candidates,
            accounts,
            source_date="2026-07-27",
            timestamp=1,
            downloader=lambda *_args, **_kwargs: self.fail("download called"),
            prober=lambda *_args, **_kwargs: self.fail("probe called"),
            repair_client=mock.Mock(side_effect=AssertionError("repair called")),
            assignment_identity={
                "source_type": "material",
                "run_date": "2026-07-27",
                "publish_time": "10:00",
                "version": 3,
            },
        )
        self.assertEqual([item["pool_item_id"] for item in planned], [1])
        self.assertEqual(
            failures,
            [
                {
                    "pool_item_id": 2,
                    "material_id": "ja-newest",
                    "error_code": "material_language_not_scheduled",
                    "error_message": "当前发布账号不包含该素材语言",
                }
            ],
        )
        self.assertEqual(planned[0]["media_validation_mode"], "deferred")
        self.assertEqual(planned[0]["preflight_sha256"], "")
        self.assertEqual(planned[0]["preflight_size"], 0)

    def test_material_scan_continues_after_first_hydration_batch(self):
        pool_items = [
            {
                "id": position,
                "material_id": str(9000 + position),
                "created_at": "2026-07-27T00:%02d:00+00:00" % position,
            }
            for position in range(1, 52)
        ]
        selector_batches = []
        preflighted = []

        class Connection:
            def close(self):
                return None

        class Sidecar(FakeSidecar):
            def __init__(self):
                super().__init__([due_item(accounts=[11])])
                self.checks = []

            def available_pool_items(self, path, limit):
                self.request = (path, limit)
                return list(pool_items)

            def record_pool_checks(self, path, checks):
                self.checks.extend((path, dict(item)) for item in checks)
                return {"updated_count": len(checks)}

            def verify_account(self, account_id):
                item = super().verify_account(account_id)
                item["drama_language"] = "en"
                return item

            def premium_relay_accounts(self, run_date, drama_language):
                self.calls.append(
                    ("relay_accounts", run_date, drama_language)
                )
                return []

        def hydrate(_connection, items, _source_date, *, limit, schema):
            del schema
            selector_batches.append(
                [str(item["material_id"]) for item in items]
            )
            self.assertEqual(limit, 50)
            return [
                {
                    "pool_item_id": int(item["id"]),
                    "pool_created_at": str(item["created_at"]),
                    "material_id": str(item["material_id"]),
                    "material_language": (
                        "en" if int(item["id"]) >= 50 else "ja"
                    ),
                    "media_kind": "video",
                    "source_duration": (
                        200 if int(item["id"]) == 50 else 30
                    ),
                    "content_id": "CONTENT-%s" % item["id"],
                    "material_url": "https://media.example.test/%s.mp4"
                    % item["id"],
                    "material_name": "Episode %s" % item["id"],
                    "drama_name": "Drama",
                    "tag": "Drama",
                    "description": "A complete episode description.",
                }
                for item in items
            ], []

        sidecar = Sidecar()
        with mock.patch(
            "scripts.x_post_schedule_runner.select_pool_candidates",
            side_effect=hydrate,
        ):
            result = self.execute(
                sidecar,
                material_candidate_loader=_material_candidates,
                connection_factory=lambda _config: Connection(),
                downloader=object(),
                prober=object(),
                repair_client=None,
            )

        self.assertEqual(sidecar.request[1], self.config.scan_limit)
        self.assertEqual(
            [len(batch) for batch in selector_batches],
            [50, 1],
        )
        self.assertEqual(
            [item for batch in selector_batches for item in batch],
            [str(item["material_id"]) for item in pool_items],
        )
        create_call = next(call for call in sidecar.calls if call[0] == "create")
        self.assertEqual(result["status"], "published")
        self.assertEqual(
            [
                (item["pool_item_id"], item["material_id"])
                for item in create_call[2]["candidates"]
            ],
            [(51, "9051")],
        )
        self.assertEqual(preflighted, [])
        self.assertEqual(
            create_call[2]["candidates"][0]["media_validation_mode"],
            "deferred",
        )

    def test_fifo_replay_accepts_current_language_skip(self):
        self.assertTrue(
            _material_fifo_selection_matches(
                [
                    {
                        "id": 2,
                        "last_error_code": "material_language_not_scheduled",
                        "last_checked_at": "2026-07-27T01:00:01Z",
                    },
                    {
                        "id": 1,
                        "last_error_code": "",
                        "last_checked_at": "",
                    },
                ],
                [
                    {
                        "pool_item_id": 1,
                        "account_id": 11,
                        "preflight_duration": 100.0,
                    }
                ],
                [11],
                [],
                validation_cutoff="2026-07-27T01:00:00Z",
            )
        )

    def test_premium_relay_client_requires_exact_current_entitlement(self):
        eligible = {
            "id": 10,
            "username": "premium10",
            "x_user_id": "x10",
            "display_name": "Premium 10",
            "subscription_type": "premium",
            "premium_subscriber": True,
            "long_video_eligible": True,
            "long_video_publish_eligible": True,
            "publish_eligible": True,
            "protected": False,
            "relay_assignment_count": 3,
        }
        client = StubScheduleClient([{"items": [eligible]}])
        result = client.premium_relay_accounts("2026-08-12")
        self.assertEqual(result[0]["id"], 10)
        self.assertEqual(result[0]["relay_assignment_count"], 3)
        self.assertEqual(
            client.requests,
            [
                (
                    "/internal/posts/premium-relay/accounts",
                    {
                        "run_date": "2026-08-12",
                        "drama_language": "en",
                    },
                    False,
                )
            ],
        )

        for override in (
            {"long_video_publish_eligible": False},
            {"publish_eligible": 1},
            {"protected": None},
        ):
            with self.subTest(override=override):
                client = StubScheduleClient(
                    [{"items": [{**eligible, **override}]}]
                )
                with self.assertRaises(SidecarError):
                    client.premium_relay_accounts("2026-08-12")

    def test_media_download_retry_is_bounded_and_code_specific(self):
        attempts = []

        def flaky(*_args, **_kwargs):
            attempts.append("download")
            if len(attempts) < 3:
                raise XPostError(
                    "media_download_failed",
                    "source read timed out",
                    502,
                )
            return {"size": 42}

        result = _retrying_media_downloader(flaky)("https://example.test")
        self.assertEqual(result, {"size": 42})
        self.assertEqual(len(attempts), 3)

        deterministic_attempts = []

        def deterministic(*_args, **_kwargs):
            deterministic_attempts.append("download")
            raise XPostError("media_too_large", "too large", 413)

        with self.assertRaises(XPostError) as rejected:
            _retrying_media_downloader(deterministic)(
                "https://example.test"
            )
        self.assertEqual(rejected.exception.code, "media_too_large")
        self.assertEqual(len(deterministic_attempts), 1)

    def tearDown(self):
        self.temporary.cleanup()

    def execute(self, sidecar, **overrides):
        options = {
            "sidecar": sidecar,
            "material_candidate_loader": candidate_loader,
            "drama_candidate_loader": candidate_loader,
            "now": self.now,
        }
        options.update(overrides)
        return execute_schedule_tick(self.config, **options)

    def test_no_due_schedule_does_not_touch_accounts_or_sources(self):
        sidecar = FakeSidecar([])
        result = self.execute(sidecar)
        self.assertEqual(result["status"], "no_due")
        self.assertEqual([call[0] for call in sidecar.calls], ["due"])

    def test_due_client_contract_is_bounded_and_identity_checked(self):
        client = StubScheduleClient(
            [{"items": [due_item(accounts=[11])]}]
        )
        items = client.due_schedules(
            "/internal/posts/schedules/due",
            current=self.now,
            grace_seconds=90,
            limit=10,
        )
        self.assertEqual(items, [due_item(accounts=[11])])
        path, payload, write_flag = client.requests[0]
        self.assertEqual(path, "/internal/posts/schedules/due")
        self.assertEqual(payload["run_date"], "2026-07-27")
        self.assertEqual(payload["grace_seconds"], 90)
        self.assertFalse(write_flag)

    def test_due_client_rejects_two_versions_for_one_source_time(self):
        one = due_item(accounts=[11])
        two = dict(one)
        two["version"] = 4
        client = StubScheduleClient([{"items": [one, two]}])
        with self.assertRaises(SidecarError):
            client.due_schedules(
                "/internal/posts/schedules/due",
                current=self.now,
                grace_seconds=90,
                limit=10,
            )

    def test_drama_pool_client_forwards_frozen_account_order(self):
        items = [
            {
                "id": 41,
                "content_id": "DRAMA-A",
                "candidate_account_id": 11,
            },
            {
                "id": 42,
                "content_id": "DRAMA-B",
                "candidate_account_id": 12,
            },
        ]
        client = StubScheduleClient([{"items": items}])

        selected = client.available_drama_pool(
            "/internal/posts/drama-pool/available",
            100,
            [11, 12],
        )

        self.assertEqual(selected, items)
        path, payload, write_flag = client.requests[0]
        self.assertEqual(
            path,
            "/internal/posts/drama-pool/available",
        )
        self.assertEqual(payload["limit"], 100)
        self.assertEqual(payload["account_ids"], [11, 12])
        self.assertFalse(write_flag)

    def test_drama_pool_check_client_records_exact_rejection(self):
        client = StubScheduleClient(
            [{"item": {"updated_count": 1}}]
        )

        result = client.record_drama_pool_checks(
            "/internal/posts/drama-pool/check",
            [
                {
                    "pool_item_id": 53,
                    "content_id": "DRAMA-BAD",
                    "error_code": "source_not_repairable",
                    "error_message": "duration is outside the X contract",
                }
            ],
        )

        self.assertEqual(result["updated_count"], 1)
        path, payload, write_flag = client.requests[0]
        self.assertEqual(path, "/internal/posts/drama-pool/check")
        self.assertEqual(payload["checks"][0]["pool_item_id"], 53)
        self.assertTrue(write_flag)

    def test_drama_pool_check_client_dry_guards_exact_success_revalidation(self):
        client = StubScheduleClient(
            [
                {
                    "item": {
                        "updated_count": 0,
                        "validated_count": 1,
                        "validate_only": True,
                    }
                }
            ]
        )
        result = client.record_drama_pool_checks(
            "/internal/posts/drama-pool/check",
            [
                {
                    "pool_item_id": 53,
                    "content_id": "DRAMA-BAD",
                    "error_code": "",
                    "error_message": "",
                    "expected_error_code": "source_not_repairable",
                    "expected_episode_number": 1,
                }
            ],
            validate_only=True,
        )
        self.assertEqual(result["updated_count"], 0)
        self.assertEqual(result["validated_count"], 1)
        path, payload, write_flag = client.requests[0]
        self.assertEqual(path, "/internal/posts/drama-pool/check")
        self.assertTrue(payload["validate_only"])
        self.assertEqual(
            payload["checks"][0]["expected_error_code"],
            "source_not_repairable",
        )
        self.assertEqual(
            payload["checks"][0]["expected_episode_number"],
            1,
        )
        self.assertFalse(write_flag)

    def test_unassigned_drama_preflight_failure_falls_forward_fifo(self):
        class Connection:
            def close(self):
                return None

        class DramaSidecar:
            def __init__(self):
                self.available_calls = 0
                self.checks = []
                self.verify_calls = []

            def verify_account(self, account_id):
                self.verify_calls.append(account_id)
                return {
                    "id": account_id,
                    "username": "account%s" % account_id,
                    "x_user_id": "x%s" % account_id,
                    "display_name": "Account %s" % account_id,
                    "status": "active",
                    "publish_eligible": True,
                    "subscription_type": "none",
                    "long_video_eligible": False,
                }

            def available_drama_pool(self, _path, _limit, _account_ids):
                self.available_calls += 1
                second = (
                    {
                        "id": 53,
                        "content_id": "BAD",
                        "created_at": "2026-07-28T01:00:00+00:00",
                        "next_sub_number": 1,
                        "assigned_account_id": 0,
                        "candidate_account_id": 9,
                    }
                    if self.available_calls == 1
                    else {
                        "id": 54,
                        "content_id": "NEXT",
                        "created_at": "2026-07-28T02:00:00+00:00",
                        "next_sub_number": 1,
                        "assigned_account_id": 0,
                        "candidate_account_id": 9,
                    }
                )
                return [
                    {
                        "id": 2,
                        "content_id": "OWNER",
                        "created_at": "2026-07-28T00:00:00+00:00",
                        "next_sub_number": 8,
                        "assigned_account_id": 10,
                        "candidate_account_id": 10,
                    },
                    second,
                ]

            def record_drama_pool_checks(self, _path, checks):
                self.checks.extend(checks)
                return {"updated_count": len(checks)}

        def selected(_connection, pool_items, **_kwargs):
            return [
                {
                    "drama_pool_item_id": item["id"],
                    "drama_pool_created_at": item["created_at"],
                    "episode_number": item["next_sub_number"],
                    "sub_num": item["next_sub_number"],
                    "episode_key": "%s:%s"
                    % (item["content_id"], item["next_sub_number"]),
                    "material_key": "",
                    "material_id": str(item["id"]),
                    "content_id": item["content_id"],
                    "material_url": "https://media.example.test/%s.mp4"
                    % item["id"],
                    "material_name": "Episode",
                    "material_language": "en",
                    "drama_name": "Drama",
                    "tag": "Drama",
                    "name_tag": "#Drama",
                    "description": "A complete episode description.",
                    "free_episode_count": 20,
                    "assigned_account_id": item["assigned_account_id"],
                    "candidate_account_id": item["candidate_account_id"],
                    "spend": 0,
                    "facebook_violation_count": 0,
                    "tiktok_violation_count": 0,
                    "twitter_violation_count": 0,
                    "resource_audit_count": 0,
                    "dangerous_tag_count": 0,
                }
                for item in pool_items
            ]

        preflight_calls = []

        def preflight(
            _config,
            candidate,
            account,
            _rank,
            _timestamp,
            _destination,
            _downloader,
            _prober,
            **_kwargs,
        ):
            preflight_calls.append(candidate["content_id"])
            if candidate["content_id"] == "BAD":
                raise CandidatePreflightError(
                    "source duration is outside the X contract",
                    code="source_not_repairable",
                )
            return {
                "account_id": account["id"],
                "content_id": candidate["content_id"],
            }

        sidecar = DramaSidecar()
        accounts = [
            {
                "id": 10,
                "username": "owner",
                "x_user_id": "x10",
                "display_name": "Owner",
                "long_video_eligible": True,
            },
            {
                "id": 9,
                "username": "new",
                "x_user_id": "x9",
                "display_name": "New",
                "long_video_eligible": True,
            },
        ]
        with mock.patch(
            "scripts.x_post_schedule_runner.select_drama_pool_episodes",
            side_effect=selected,
        ), mock.patch(
            "scripts.x_post_schedule_runner._preflight_candidate",
            side_effect=preflight,
        ):
            planned = _drama_candidates(
                self.config,
                sidecar,
                accounts,
                source_date="2026-07-28",
                connection_factory=lambda _config: Connection(),
                downloader=object(),
                prober=object(),
                repair_client=None,
                timestamp=1,
            )

        self.assertEqual(sidecar.available_calls, 1)
        self.assertEqual(sidecar.verify_calls, [])
        self.assertEqual(sidecar.checks, [])
        self.assertEqual(
            [(item["account_id"], item["content_id"]) for item in planned],
            [(10, "OWNER"), (9, "BAD")],
        )
        self.assertEqual(preflight_calls, [])
        self.assertTrue(
            all(item["media_validation_mode"] == "deferred" for item in planned)
        )

        transient_sidecar = DramaSidecar()

        def transient_preflight(
            _config,
            candidate,
            account,
            _rank,
            _timestamp,
            _destination,
            _downloader,
            _prober,
            **_kwargs,
        ):
            if candidate["content_id"] == "BAD":
                raise OSError("temporary COS connection failure")
            return {
                "account_id": account["id"],
                "content_id": candidate["content_id"],
            }

        with mock.patch(
            "scripts.x_post_schedule_runner.select_drama_pool_episodes",
            side_effect=selected,
        ), mock.patch(
            "scripts.x_post_schedule_runner._preflight_candidate",
            side_effect=transient_preflight,
        ):
            transient_planned = _drama_candidates(
                self.config,
                transient_sidecar,
                accounts,
                source_date="2026-07-28",
                connection_factory=lambda _config: Connection(),
                downloader=object(),
                prober=object(),
                repair_client=None,
                timestamp=1,
            )
        self.assertEqual(
            [(item["account_id"], item["content_id"]) for item in transient_planned],
            [(10, "OWNER"), (9, "BAD")],
        )
        self.assertEqual(transient_sidecar.checks, [])
        self.assertEqual(transient_sidecar.available_calls, 1)

    def test_bad_bound_drama_does_not_block_healthy_sibling_queues(self):
        pool_items = [
            {
                "id": 61,
                "content_id": "BAD-BOUND",
                "created_at": "2026-07-27T00:00:00Z",
                "next_sub_number": 4,
                "assigned_account_id": 10,
                "candidate_account_id": 10,
            },
            {
                "id": 62,
                "content_id": "GOOD-BOUND",
                "created_at": "2026-07-27T00:01:00Z",
                "next_sub_number": 2,
                "assigned_account_id": 11,
                "candidate_account_id": 11,
            },
            {
                "id": 63,
                "content_id": "GOOD-FREE",
                "created_at": "2026-07-27T00:02:00Z",
                "next_sub_number": 1,
                "assigned_account_id": 0,
                "candidate_account_id": 12,
            },
        ]
        frozen_pool = [dict(item) for item in pool_items]

        class Connection:
            def close(self):
                return None

        class DramaSidecar(FakeSidecar):
            def __init__(self):
                super().__init__(
                    [due_item(source_type="drama", accounts=[10, 11, 12])]
                )
                self.checks = []

            def verify_account(self, account_id):
                item = super().verify_account(account_id)
                item.update(
                    {
                        "drama_language": "en",
                        "subscription_type": "premium",
                        "premium_subscriber": True,
                        "long_video_eligible": True,
                    }
                )
                return item

            def available_drama_pool(self, _path, _limit, _account_ids):
                return [dict(item) for item in pool_items]

            def record_drama_pool_checks(self, _path, checks):
                self.checks.extend(dict(item) for item in checks)
                return {"updated_count": len(checks)}

        def selected(_connection, items, **_kwargs):
            item = items[0]
            if item["id"] == 61:
                raise DramaPoolRejection(
                    "drama_mapping_missing",
                    "bound drama metadata is incomplete",
                    61,
                    "BAD-BOUND",
                )
            return [
                {
                    "drama_pool_item_id": item["id"],
                    "drama_pool_created_at": item["created_at"],
                    "episode_number": item["next_sub_number"],
                    "sub_num": item["next_sub_number"],
                    "episode_key": "%s:%s"
                    % (item["content_id"], item["next_sub_number"]),
                    "material_key": "",
                    "material_id": str(item["id"]),
                    "content_id": item["content_id"],
                    "material_url": "https://media.example.test/%s.mp4"
                    % item["id"],
                    "material_name": "Episode",
                    "material_language": "en",
                    "drama_name": "Drama",
                    "tag": "Drama",
                    "name_tag": "#Drama",
                    "description": "A complete episode description.",
                    "free_episode_count": 20,
                    "assigned_account_id": item["assigned_account_id"],
                    "candidate_account_id": item["candidate_account_id"],
                    "spend": 0,
                    "facebook_violation_count": 0,
                    "tiktok_violation_count": 0,
                    "twitter_violation_count": 0,
                    "resource_audit_count": 0,
                    "dangerous_tag_count": 0,
                }
            ]

        sidecar = DramaSidecar()
        with mock.patch(
            "scripts.x_post_schedule_runner.select_drama_pool_episodes",
            side_effect=selected,
        ):
            result = self.execute(
                sidecar,
                drama_candidate_loader=_drama_candidates,
                connection_factory=lambda _config: Connection(),
                downloader=mock.Mock(
                    side_effect=AssertionError("download called")
                ),
                prober=mock.Mock(side_effect=AssertionError("probe called")),
                repair_client=mock.Mock(
                    side_effect=AssertionError("repair called")
                ),
            )

        self.assertIn("create", [call[0] for call in sidecar.calls], result)
        create_payload = next(
            call[2] for call in sidecar.calls if call[0] == "create"
        )
        self.assertEqual(create_payload["account_ids"], [11, 12])
        self.assertEqual(
            [item["content_id"] for item in create_payload["candidates"]],
            ["GOOD-BOUND", "GOOD-FREE"],
        )
        self.assertEqual(
            [call[1] for call in sidecar.calls if call[0] == "publish"],
            [101, 102],
        )
        self.assertEqual(result["status"], "published")
        self.assertEqual(sidecar.checks, [])
        self.assertEqual(pool_items, frozen_pool)

    def test_unassigned_long_drama_routes_to_confirmed_premium_account(self):
        class Connection:
            def close(self):
                return None

        class DramaSidecar:
            def __init__(self):
                self.checks = []

            def available_drama_pool(self, _path, _limit, _account_ids):
                return [
                    {
                        "id": 138,
                        "content_id": "LONG",
                        "created_at": "2026-08-11T01:00:00+00:00",
                        "next_sub_number": 1,
                        "assigned_account_id": 0,
                        "candidate_account_id": 10,
                    },
                    {
                        "id": 137,
                        "content_id": "SHORT",
                        "created_at": "2026-08-11T00:00:00+00:00",
                        "next_sub_number": 1,
                        "assigned_account_id": 0,
                        "candidate_account_id": 9,
                    },
                ]

            def record_drama_pool_checks(self, _path, checks):
                self.checks.extend(checks)
                return {"updated_count": len(checks)}

            def premium_relay_accounts(self, _run_date):
                return [
                    {
                        "id": 9,
                        "username": "premium",
                        "x_user_id": "x9",
                        "display_name": "Premium",
                        "long_video_eligible": True,
                    }
                ]

        def selected(_connection, pool_items, **_kwargs):
            return [
                {
                    "drama_pool_item_id": item["id"],
                    "drama_pool_created_at": item["created_at"],
                    "episode_number": item["next_sub_number"],
                    "sub_num": item["next_sub_number"],
                    "episode_key": "%s:1" % item["content_id"],
                    "material_key": "",
                    "material_id": str(item["id"]),
                    "content_id": item["content_id"],
                    "material_url": "https://media.example.test/%s.mp4"
                    % item["id"],
                    "material_name": "Episode",
                    "material_language": "en",
                    "drama_name": "Drama",
                    "tag": "Drama",
                    "name_tag": "#Drama",
                    "description": "A complete episode description.",
                    "free_episode_count": 20,
                    "assigned_account_id": item["assigned_account_id"],
                    "candidate_account_id": item["candidate_account_id"],
                    "spend": 0,
                    "facebook_violation_count": 0,
                    "tiktok_violation_count": 0,
                    "twitter_violation_count": 0,
                    "resource_audit_count": 0,
                    "dangerous_tag_count": 0,
                }
                for item in pool_items
            ]

        preflight_calls = []

        def preflight(
            _config,
            candidate,
            account,
            _rank,
            _timestamp,
            _destination,
            _downloader,
            _prober,
            **_kwargs,
        ):
            preflight_calls.append((candidate["content_id"], account["id"]))
            if candidate["content_id"] == "LONG" and account["id"] == 10:
                raise CandidatePreflightError(
                    "Videos longer than 140 seconds require Premium",
                    code="x_long_video_requires_premium",
                )
            return {
                "account_id": account["id"],
                "content_id": candidate["content_id"],
            }

        sidecar = DramaSidecar()
        accounts = [
            {
                "id": 10,
                "username": "standard",
                "x_user_id": "x10",
                "display_name": "Standard",
                "long_video_eligible": False,
            },
            {
                "id": 9,
                "username": "premium",
                "x_user_id": "x9",
                "display_name": "Premium",
                "long_video_eligible": True,
            },
        ]
        with mock.patch(
            "scripts.x_post_schedule_runner.select_drama_pool_episodes",
            side_effect=selected,
        ), mock.patch(
            "scripts.x_post_schedule_runner._preflight_candidate",
            side_effect=preflight,
        ):
            planned = _drama_candidates(
                self.config,
                sidecar,
                accounts,
                source_date="2026-08-11",
                connection_factory=lambda _config: Connection(),
                downloader=object(),
                prober=object(),
                repair_client=None,
                timestamp=1,
            )

        self.assertEqual(
            [(item["account_id"], item["content_id"]) for item in planned],
            [(10, "LONG"), (9, "SHORT")],
        )
        self.assertEqual(planned[0]["delivery_mode"], "premium_relay_repost")
        self.assertEqual(planned[0]["relay_account_id"], 9)
        self.assertEqual(planned[0]["preflight_duration"], 141.0)
        self.assertEqual(planned[0]["media_validation_mode"], "deferred")
        self.assertEqual(
            preflight_calls,
            [],
        )
        self.assertEqual(sidecar.checks, [])

    def test_owned_premium_account_can_relay_for_another_target(self):
        class Connection:
            def close(self):
                return None

        class DramaSidecar:
            def __init__(self):
                self.checks = []
                self.available_calls = 0
                self.verify_calls = []

            def verify_account(self, account_id):
                self.verify_calls.append(account_id)
                return {
                    "id": account_id,
                    "username": "account%s" % account_id,
                    "x_user_id": "x%s" % account_id,
                    "display_name": "Account %s" % account_id,
                    "status": "active",
                    "publish_eligible": True,
                    "subscription_type": (
                        "premium" if account_id == 9 else "none"
                    ),
                    "long_video_eligible": account_id == 9,
                }

            def available_drama_pool(self, _path, _limit, _account_ids):
                self.available_calls += 1
                unassigned = (
                    {
                        "id": 138,
                        "content_id": "LONG",
                        "created_at": "2026-08-11T01:00:00+00:00",
                        "next_sub_number": 1,
                        "assigned_account_id": 0,
                        "candidate_account_id": 10,
                    }
                    if not self.checks
                    else {
                        "id": 136,
                        "content_id": "SHORT",
                        "created_at": "2026-08-10T23:00:00+00:00",
                        "next_sub_number": 1,
                        "assigned_account_id": 0,
                        "candidate_account_id": 10,
                    }
                )
                return [
                    unassigned,
                    {
                        "id": 137,
                        "content_id": "OWNER",
                        "created_at": "2026-08-11T00:00:00+00:00",
                        "next_sub_number": 2,
                        "assigned_account_id": 9,
                        "candidate_account_id": 9,
                    },
                ]

            def record_drama_pool_checks(self, _path, checks):
                self.checks.extend(checks)
                return {"updated_count": len(checks)}

            def premium_relay_accounts(self, _run_date):
                return [
                    {
                        "id": 9,
                        "username": "ownedpremium",
                        "x_user_id": "x9",
                        "display_name": "Owned Premium",
                        "long_video_eligible": True,
                    }
                ]

        def selected(_connection, pool_items, **_kwargs):
            return [
                {
                    "drama_pool_item_id": item["id"],
                    "drama_pool_created_at": item["created_at"],
                    "episode_number": item["next_sub_number"],
                    "sub_num": item["next_sub_number"],
                    "episode_key": "%s:%s"
                    % (item["content_id"], item["next_sub_number"]),
                    "material_key": "",
                    "material_id": str(item["id"]),
                    "content_id": item["content_id"],
                    "material_url": "https://media.example.test/%s.mp4"
                    % item["id"],
                    "material_name": "Episode",
                    "material_language": "en",
                    "drama_name": "Drama",
                    "tag": "Drama",
                    "name_tag": "#Drama",
                    "description": "A complete episode description.",
                    "free_episode_count": 20,
                    "assigned_account_id": item["assigned_account_id"],
                    "candidate_account_id": item["candidate_account_id"],
                    "spend": 0,
                    "facebook_violation_count": 0,
                    "tiktok_violation_count": 0,
                    "twitter_violation_count": 0,
                    "resource_audit_count": 0,
                    "dangerous_tag_count": 0,
                }
                for item in pool_items
            ]

        preflight_calls = []

        def preflight(
            _config,
            candidate,
            account,
            _rank,
            _timestamp,
            _destination,
            _downloader,
            _prober,
            **_kwargs,
        ):
            preflight_calls.append((candidate["content_id"], account["id"]))
            if candidate["content_id"] == "LONG" and account["id"] == 10:
                raise CandidatePreflightError(
                    "Videos longer than 140 seconds require Premium",
                    code="x_long_video_requires_premium",
                )
            return {
                "account_id": account["id"],
                "content_id": candidate["content_id"],
            }

        sidecar = DramaSidecar()
        accounts = [
            {
                "id": 10,
                "username": "standard",
                "x_user_id": "x10",
                "display_name": "Standard",
                "long_video_eligible": False,
            },
            {
                "id": 9,
                "username": "ownedpremium",
                "x_user_id": "x9",
                "display_name": "Owned Premium",
                "long_video_eligible": True,
            },
        ]
        with mock.patch(
            "scripts.x_post_schedule_runner.select_drama_pool_episodes",
            side_effect=selected,
        ), mock.patch(
            "scripts.x_post_schedule_runner._preflight_candidate",
            side_effect=preflight,
        ):
            planned = _drama_candidates(
                self.config,
                sidecar,
                accounts,
                source_date="2026-08-11",
                connection_factory=lambda _config: Connection(),
                downloader=object(),
                prober=object(),
                repair_client=None,
                timestamp=1,
            )

        self.assertEqual(sidecar.available_calls, 1)
        self.assertEqual(sidecar.verify_calls, [])
        self.assertEqual(sidecar.checks, [])
        self.assertEqual(
            [(item["account_id"], item["content_id"]) for item in planned],
            [(10, "LONG"), (9, "OWNER")],
        )
        self.assertEqual(planned[0]["relay_account_id"], 9)
        self.assertEqual(
            preflight_calls,
            [],
        )

    def test_plan_query_requires_exact_frozen_identity_and_account_order(self):
        identity = due_item(accounts=[11, 12])
        client = StubScheduleClient(
            [
                {
                    "item": {
                        "found": True,
                        "run": frozen_run("material", 2),
                        "queues": [
                            queue(1, 12, 1),
                            queue(2, 11, 2),
                        ],
                    }
                }
            ]
        )
        with self.assertRaises(SidecarError):
            client.query_schedule_plan(
                "/internal/posts/schedule-plan/query", identity
            )

    def test_plan_query_accepts_ordered_partial_frozen_scope(self):
        identity = due_item(accounts=[11, 12, 13])
        client = StubScheduleClient(
            [
                {
                    "item": {
                        "found": True,
                        "run": {
                            **frozen_run("material", 3),
                            "expected_count": 2,
                        },
                        "queues": [
                            queue(1, 11, 1),
                            queue(2, 13, 2),
                        ],
                    }
                }
            ]
        )

        plan = client.query_schedule_plan(
            "/internal/posts/schedule-plan/query", identity
        )

        self.assertEqual(plan["run"]["expected_count"], 2)
        self.assertEqual(
            [item["account_id"] for item in plan["queues"]],
            [11, 13],
        )

    def test_plan_create_uses_wrapped_http_response(self):
        identity = due_item(accounts=[11])
        payload = {
            **identity,
            "source_date": "2026-07-26",
            "candidates": [{"account_id": 11}],
        }
        client = StubScheduleClient(
            [
                {
                    "item": {
                        "run": frozen_run("material", 1),
                        "queues": [queue(1, 11, 1)],
                    }
                }
            ]
        )
        queues = client.create_schedule_plan(
            "/internal/posts/schedule-plan", payload
        )
        self.assertEqual(queues, [queue(1, 11, 1)])
        self.assertTrue(client.requests[0][2])

    def test_failure_audit_uses_frozen_schedule_scope(self):
        identity = due_item(accounts=[11])
        client = StubScheduleClient(
            [
                {
                    "item": {
                        "id": 8,
                        "source_type": "material",
                        "run_date": "2026-07-27",
                        "publish_time": "10:00",
                        "config_version": 3,
                        "account_ids": [11],
                        "status": "failed_preflight",
                        "error_code": "test_failure",
                        "error_message": "test",
                        "recorded": True,
                    }
                }
            ]
        )
        item = client.record_schedule_failure(
            "/internal/posts/schedule-runs/record-failure",
            identity,
            "test_failure",
            "test",
        )
        self.assertTrue(item["recorded"])
        path, payload, write_flag = client.requests[0]
        self.assertEqual(
            path, "/internal/posts/schedule-runs/record-failure"
        )
        self.assertEqual(payload["account_ids"], [11])
        self.assertEqual(payload["version"], 3)
        self.assertTrue(write_flag)

    def test_drama_failure_audit_carries_exact_pool_binding(self):
        identity = due_item(source_type="drama", accounts=[11])
        identity["drama_pool_item_id"] = 41
        identity["content_id"] = "DRAMA_41"
        client = StubScheduleClient(
            [
                {
                    "item": {
                        "id": 8,
                        "source_type": "drama",
                        "run_date": "2026-07-27",
                        "publish_time": "10:00",
                        "config_version": 3,
                        "account_ids": [11],
                        "status": "failed_preflight",
                        "error_code": "media_preflight_failed",
                        "error_message": "invalid episode media",
                        "recorded": True,
                    }
                }
            ]
        )

        item = client.record_schedule_failure(
            "/internal/posts/schedule-runs/record-failure",
            identity,
            "media_preflight_failed",
            "invalid episode media",
        )

        self.assertTrue(item["recorded"])
        _path, payload, write_flag = client.requests[0]
        self.assertEqual(payload["drama_pool_item_id"], 41)
        self.assertEqual(payload["content_id"], "DRAMA_41")
        self.assertTrue(write_flag)

    def test_stale_schedule_is_ignored_and_never_queried(self):
        sidecar = FakeSidecar([due_item(publish_time="09:58")])
        result = self.execute(sidecar)
        self.assertEqual(result["status"], "no_due")
        self.assertEqual(result["stale_ignored_count"], 1)
        self.assertEqual([call[0] for call in sidecar.calls], ["due"])

    def test_frozen_plan_is_used_before_verify_or_source_reads(self):
        identity = due_item(accounts=[11])
        sidecar = FakeSidecar([identity])
        sidecar.existing[
            ("material", "2026-07-27", "10:00", 3)
        ] = {
            "found": True,
            "run": frozen_run("material", 1),
            "queues": [queue(91, 11, 1)],
        }

        def forbidden_loader(*_args, **_kwargs):
            raise AssertionError("source loader must not run for a frozen plan")

        result = self.execute(
            sidecar,
            material_candidate_loader=forbidden_loader,
        )
        self.assertEqual(result["status"], "published")
        self.assertEqual(
            [call[0] for call in sidecar.calls],
            ["due", "query", "publish"],
        )
        self.assertTrue(result["batches"][0]["resumed_existing_plan"])

    def test_claimed_run_without_queues_is_planned_and_published(self):
        identity = due_item(accounts=[11], frozen=True)
        sidecar = FakeSidecar([identity])
        sidecar.existing[
            ("material", "2026-07-27", "10:00", 3)
        ] = {
            "found": True,
            "run": {
                **frozen_run("material", 1),
                "status": "claimed",
            },
            "queues": [],
        }

        result = self.execute(sidecar)

        self.assertEqual(result["status"], "published")
        self.assertEqual(
            [call for call in sidecar.calls if call[0] == "verify"],
            [("verify", 11), ("verify", 11)],
        )
        self.assertIn("create", [call[0] for call in sidecar.calls])
        self.assertIn("publish", [call[0] for call in sidecar.calls])
        self.assertFalse(
            result["batches"][0]["resumed_existing_plan"]
        )

    def test_old_frozen_claim_is_recovered_outside_grace(self):
        identity = due_item(
            accounts=[11],
            publish_time="09:00",
            frozen=True,
        )
        sidecar = FakeSidecar([identity])
        sidecar.existing[
            ("material", "2026-07-27", "09:00", 3)
        ] = {
            "found": True,
            "run": {
                **frozen_run("material", 1),
                "publish_time": "09:00",
            },
            "queues": [queue(91, 11, 1)],
        }

        result = self.execute(sidecar)

        self.assertEqual(result["status"], "published")
        self.assertEqual(result["stale_ignored_count"], 0)
        self.assertEqual(
            [call[0] for call in sidecar.calls],
            ["due", "query", "publish"],
        )

    def test_claim_tick_does_not_take_the_media_worker_lock(self):
        sidecar = FakeSidecar(
            [due_item(accounts=[11], frozen=True)]
        )

        result = execute_claim_tick(
            self.config,
            sidecar=sidecar,
            now=self.now,
        )

        self.assertEqual(result["status"], "claimed")
        self.assertEqual(result["claimed_or_pending_count"], 1)
        self.assertEqual([call[0] for call in sidecar.calls], ["due"])

    def test_new_plan_verifies_all_accounts_then_freezes_and_publishes(self):
        sidecar = FakeSidecar([due_item()])
        result = self.execute(sidecar)
        call_names = [call[0] for call in sidecar.calls]
        self.assertEqual(
            call_names,
            [
                "due",
                "query",
                "storage",
                "verify",
                "verify",
                "verify",
                "verify",
                "storage",
                "create",
                "publish",
                "publish",
            ],
        )
        create_payload = next(
            call[2] for call in sidecar.calls if call[0] == "create"
        )
        self.assertEqual(create_payload["source_type"], "material")
        self.assertEqual(create_payload["publish_time"], "10:00")
        self.assertEqual(create_payload["version"], 3)
        self.assertEqual(create_payload["account_ids"], [11, 12])
        self.assertEqual(
            [item["account_id"] for item in create_payload["candidates"]],
            [11, 12],
        )
        self.assertEqual(result["batches"][0]["published_count"], 2)

    def test_available_subset_is_frozen_and_published_without_waiting(self):
        sidecar = FakeSidecar(
            [due_item(accounts=[11, 12, 13])]
        )

        def partial_loader(
            _config,
            _sidecar,
            accounts,
            *,
            source_date,
            **_kwargs,
        ):
            return [
                {
                    "account_id": account["id"],
                    "source_date": source_date,
                    "material_id": str(9000 + account["id"]),
                }
                for account in (accounts[0], accounts[2])
            ]

        result = self.execute(
            sidecar,
            material_candidate_loader=partial_loader,
        )

        create_payload = next(
            call[2] for call in sidecar.calls if call[0] == "create"
        )
        self.assertEqual(create_payload["account_ids"], [11, 13])
        self.assertEqual(
            [call[1] for call in sidecar.calls if call[0] == "publish"],
            [101, 102],
        )
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["batches"][0]["planned_count"], 2)

    def test_drama_known_failure_continues_later_episode_queue(self):
        sidecar = FakeSidecar([due_item(source_type="drama")])
        sidecar.publish_errors[101] = SidecarError(
            "x_upstream_error", "known X rejection", 400, False
        )
        result = self.execute(sidecar)
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(
            [call[1] for call in sidecar.calls if call[0] == "publish"],
            [101, 102],
        )

    def test_stopped_drama_batch_does_not_skip_independent_due_batch(self):
        sidecar = FakeSidecar(
            [
                due_item(source_type="drama", accounts=[11, 12]),
                due_item(source_type="material", accounts=[13]),
            ]
        )
        sidecar.publish_errors[101] = SidecarError(
            "x_upstream_error", "known X rejection", 400, False
        )
        result = self.execute(sidecar)
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(result["processed_count"], 2)
        self.assertEqual(
            [call[1] for call in sidecar.calls if call[0] == "publish"],
            [101, 102, 103],
        )
        self.assertEqual(result["batches"][1]["status"], "published")

    def test_preflight_failure_is_recorded_and_next_batch_continues(self):
        sidecar = FakeSidecar(
            [
                due_item(source_type="drama", accounts=[11]),
                due_item(source_type="material", accounts=[12]),
            ]
        )

        def rejected_drama(*_args, **_kwargs):
            raise RuntimeError("read-only drama source unavailable")

        result = self.execute(
            sidecar,
            drama_candidate_loader=rejected_drama,
        )
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(result["processed_count"], 2)
        self.assertEqual(
            result["batches"][0]["status"], "failed_preflight"
        )
        self.assertTrue(result["batches"][0]["failure_recorded"])
        self.assertEqual(result["batches"][1]["status"], "published")
        self.assertIn(
            "failure", [call[0] for call in sidecar.calls]
        )

    def test_drama_preflight_failure_preserves_pool_binding_for_audit(self):
        sidecar = FakeSidecar(
            [due_item(source_type="drama", accounts=[11])]
        )

        def rejected_drama(*_args, **_kwargs):
            raise ScheduleRunError(
                "episode media is invalid",
                "media_preflight_failed",
                drama_pool_item_id=41,
                content_id="DRAMA_41",
            )

        result = self.execute(
            sidecar,
            drama_candidate_loader=rejected_drama,
        )

        self.assertEqual(
            result["batches"][0]["status"],
            "failed_preflight",
        )
        failure_call = next(
            call for call in sidecar.calls if call[0] == "failure"
        )
        self.assertEqual(
            failure_call[5]["drama_pool_item_id"],
            41,
        )
        self.assertEqual(
            failure_call[5]["content_id"],
            "DRAMA_41",
        )

    def test_material_known_failure_does_not_hide_later_queue_result(self):
        sidecar = FakeSidecar([due_item(source_type="material")])
        sidecar.publish_errors[101] = SidecarError(
            "x_upstream_error", "known X rejection", 400, False
        )
        result = self.execute(sidecar)
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(
            [call[1] for call in sidecar.calls if call[0] == "publish"],
            [101, 102],
        )
        self.assertEqual(result["batches"][0]["published_count"], 1)

    def test_rate_limit_still_stops_later_queue(self):
        sidecar = FakeSidecar([due_item(source_type="drama")])
        sidecar.publish_errors[101] = SidecarError(
            "x_post_rate_limited", "X rate limit", 429, False
        )

        result = self.execute(sidecar)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(
            [call[1] for call in sidecar.calls if call[0] == "publish"],
            [101],
        )

    def test_unknown_frozen_queue_stops_without_another_publish_call(self):
        identity = due_item(source_type="drama", accounts=[11, 12])
        sidecar = FakeSidecar([identity])
        sidecar.existing[
            ("drama", "2026-07-27", "10:00", 3)
        ] = {
            "found": True,
            "run": frozen_run("drama", 2),
            "queues": [
                queue(201, 11, 1, status="failed", unknown=True),
                queue(202, 12, 2),
            ],
        }
        result = self.execute(sidecar)
        self.assertEqual(result["status"], "stopped")
        self.assertFalse(any(call[0] == "publish" for call in sidecar.calls))
        self.assertTrue(
            result["batches"][0]["results"][0]["unknown_outcome"]
        )

    def test_known_failed_frozen_drama_queue_does_not_block_later_queue(self):
        identity = due_item(source_type="drama", accounts=[11, 12])
        sidecar = FakeSidecar([identity])
        sidecar.existing[("drama", "2026-07-27", "10:00", 3)] = {
            "found": True,
            "run": frozen_run("drama", 2),
            "queues": [
                queue(201, 11, 1, status="failed", unknown=False),
                queue(202, 12, 2),
            ],
        }

        result = self.execute(sidecar)

        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(
            [call[1] for call in sidecar.calls if call[0] == "publish"],
            [202],
        )

    def test_frozen_rate_limited_queue_still_stops_later_queue(self):
        identity = due_item(source_type="drama", accounts=[11, 12])
        sidecar = FakeSidecar([identity])
        sidecar.existing[("drama", "2026-07-27", "10:00", 3)] = {
            "found": True,
            "run": frozen_run("drama", 2),
            "queues": [
                queue(
                    201,
                    11,
                    1,
                    status="failed",
                    error_code="x_post_rate_limited",
                ),
                queue(202, 12, 2),
            ],
        }

        result = self.execute(sidecar)

        self.assertEqual(result["status"], "stopped")
        self.assertFalse(any(call[0] == "publish" for call in sidecar.calls))

    def test_before_start_date_is_a_noop(self):
        config = make_config(self.temporary.name)
        config = ScheduleConfig(
            **{
                **config.__dict__,
                "start_date": "2026-07-28",
            }
        )
        sidecar = FakeSidecar([due_item()])
        result = execute_schedule_tick(
            config,
            sidecar=sidecar,
            material_candidate_loader=candidate_loader,
            drama_candidate_loader=candidate_loader,
            now=self.now,
        )
        self.assertEqual(result["status"], "skipped_before_start_date")
        self.assertEqual(sidecar.calls, [])


if __name__ == "__main__":
    unittest.main()
