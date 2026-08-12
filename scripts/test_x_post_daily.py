#!/usr/bin/env python3
"""Offline regression tests for daily X selection and orchestration."""

from __future__ import annotations

import io
import http.client
import json
import os
import re
import sys
import tempfile
import urllib.error
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts.selector import (  # noqa: E402
    CandidateQueryError,
    CandidateSelectionError,
    contains_dangerous_tag,
    material_key,
    previous_source_date,
    ranked_material_ids,
    select_candidates,
)
from features.x_posts.service import XPostError  # noqa: E402
from scripts.x_post_daily_runner import (  # noqa: E402
    DailyConfig,
    DailyRunError,
    DEFAULT_REPAIR_PROFILE,
    MAX_ERROR_BODY_BYTES,
    MAX_SIDECAR_RESPONSE_BYTES,
    MediaRepairClient,
    MediaRepairError,
    SidecarClient,
    SidecarError,
    _preflight_candidates,
    _parse_account_ids,
    _record_pool_checks_best_effort,
    execute_daily_run,
)
from scripts import wait_x_post_sidecar  # noqa: E402


def base_row(material_id, spend):
    return {
        "material_id": str(material_id),
        "spend": spend,
        "series_count": 1,
        "series_code": "S%s" % material_id,
        "drama_language_count": 1,
        "drama_language": "en",
        "insight_content_id_count": 1,
        "insight_content_id": "C%s" % material_id,
        "material_url": "https://media.example.test/%s.mp4" % material_id,
        "material_name": "material-%s.mp4" % material_id,
        "material_language": "en",
        "content_id": "C%s" % material_id,
        "source_tag_name": "high_quality",
        "video_duration": 30,
    }


def drama_row(material_id, name=None):
    return {
        "content_id": "C%s" % material_id,
        "series_code": "S%s" % material_id,
        "language": "en",
        "drama_name": name or "Drama %s" % material_id,
        "drama_labels": "Fantasy,Counterattack",
        "drama_description": "A complete and safe drama description.",
    }


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, sql, params):
        self.connection.calls.append((sql, tuple(params)))
        if "ads_custom_source_insight" in sql:
            self.rows = self.connection.base_rows
        elif "resource_tags" in sql:
            material_id = str(params[0])
            if material_id in self.connection.query_error_material_ids:
                raise RuntimeError("simulated read-only connection loss")
            self.rows = [
                {"tag_name": value}
                for value in self.connection.material_tags.get(material_id, [])
            ]
        elif "ads_drama_resource" in sql:
            material_id = str(params[0]).lstrip("C")
            self.rows = self.connection.drama_rows.get(
                material_id, [drama_row(material_id)]
            )
        else:
            raise AssertionError("unexpected SQL")

    def fetchall(self):
        return list(self.rows)

    def close(self):
        return None


class FakeConnection:
    def __init__(self, rows):
        self.base_rows = rows
        self.material_tags = {}
        self.drama_rows = {}
        self.query_error_material_ids = set()
        self.calls = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class SelectorTests(unittest.TestCase):
    def test_previous_day_uses_shanghai_calendar(self):
        instant = datetime(2026, 7, 22, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(previous_source_date(instant), "2026-07-22")

    def test_material_key_is_canonical_global_decimal(self):
        self.assertEqual(material_key("000123"), "123")
        with self.assertRaises(CandidateSelectionError):
            material_key("dramawave:123")

    def test_danger_lexicon_handles_english_and_chinese(self):
        self.assertTrue(contains_dangerous_tag("Graphic Violence"))
        self.assertTrue(contains_dangerous_tag("NSFW adult"))
        for value in (
            "sexual_content",
            "violent_content",
            "porn_video",
            "blood_gore",
            "nsfw_adult",
            "weapons",
            "nudes",
            "murders",
            "suicidal",
            "gun",
            "guns",
            "shooting",
            "torture",
            "knife",
            "R18",
            "18+",
            "枪支",
            "枪械",
            "涉黄",
            "成人内容",
            "殴打",
            "流血",
        ):
            with self.subTest(value=value):
                self.assertTrue(contains_dangerous_tag(value))
        self.assertTrue(contains_dangerous_tag("血腥复仇"))
        self.assertFalse(contains_dangerous_tag("Fantasy"))
        with self.assertRaises(CandidateSelectionError):
            contains_dangerous_tag(b"\xff")

    def test_selector_excludes_common_derived_violence_tag(self):
        unsafe = base_row(21, 200)
        unsafe["source_tag_name"] = "weapons"
        connection = FakeConnection([unsafe, base_row(22, 100)])
        selected = select_candidates(
            connection,
            "2026-07-22",
            limit=1,
            scan_limit=10,
        )
        self.assertEqual([item["material_id"] for item in selected], ["22"])

    def test_selector_ignores_violation_history_but_excludes_unsafe_and_ambiguous_rows(self):
        connection = FakeConnection(
            [
                base_row(1, 600),
                base_row(2, 500),
                base_row(3, 400),
                base_row(4, 300),
                base_row(5, 200),
                base_row(6, 100),
            ]
        )
        connection.material_tags["3"] = ["violent"]
        connection.drama_rows["4"] = [
            drama_row(4, "One mapping"),
            drama_row(4, "Another mapping"),
        ]

        selected = select_candidates(
            connection,
            "2026-07-22",
            excluded_material_keys={"1"},
            limit=2,
            scan_limit=100,
        )
        self.assertEqual([item["material_id"] for item in selected], ["2", "5"])
        self.assertEqual([item["spend"] for item in selected], [500.0, 200.0])
        self.assertTrue(
            all(
                item["facebook_violation_count"] == 0
                and item["tiktok_violation_count"] == 0
                and item["twitter_violation_count"] == 0
                and item["resource_audit_count"] == 0
                for item in selected
            )
        )
        self.assertTrue(all(item["material_key"] == item["material_id"] for item in selected))
        statements = [sql for sql, _params in connection.calls]
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql in statements))
        self.assertTrue(
            all("violations" not in sql and "resource_audit" not in sql for sql in statements)
        )
        base_sql, base_params = connection.calls[0]
        self.assertNotIn("2026-07-22", base_sql)
        self.assertIn("2026-07-22", base_params)
        self.assertIn("SUM(COALESCE(s.spend, 0)) DESC", base_sql)
        self.assertIn("CAST(s.resource_id AS UNSIGNED) ASC", base_sql)

    def test_selector_fails_closed_on_incomplete_mapping(self):
        connection = FakeConnection([base_row(7, 100)])
        connection.drama_rows["7"] = []
        self.assertEqual(
            select_candidates(connection, "2026-07-22", limit=1, scan_limit=3),
            [],
        )

    def test_selector_does_not_downgrade_query_failure_to_candidate_rejection(self):
        connection = FakeConnection([base_row(7, 100), base_row(8, 90)])
        connection.query_error_material_ids.add("7")
        with self.assertRaises(CandidateQueryError):
            select_candidates(connection, "2026-07-22", limit=1, scan_limit=3)

    def test_ranked_material_ids_is_bounded_parameterized_and_stable(self):
        connection = FakeConnection([base_row(7, 100), base_row(8, 90)])
        values = ranked_material_ids(
            connection, "2026-07-22", scan_limit=100, schema="kunlunads_dev"
        )
        self.assertEqual(values, ["7", "8"])
        sql, params = connection.calls[0]
        self.assertNotIn("2026-07-22", sql)
        self.assertIn("2026-07-22", params)
        self.assertIn("SUM(COALESCE(s.spend, 0)) DESC", sql)


def candidate(material_id, spend):
    return {
        "source_date": "2026-07-22",
        "material_key": str(material_id),
        "material_id": str(material_id),
        "pool_item_id": int(material_id),
        "pool_created_at": "2026-07-22T00:00:%02dZ" % (int(material_id) % 60),
        "content_id": "C%s" % material_id,
        "material_url": "https://media.example.test/%s.mp4" % material_id,
        "material_name": "material-%s.mp4" % material_id,
        "material_language": "en",
        "drama_name": "Drama %s" % material_id,
        "tag": "Fantasy",
        "description": "A safe description.",
        "spend": float(spend),
        "facebook_violation_count": 0,
        "tiktok_violation_count": 0,
        "twitter_violation_count": 0,
        "resource_audit_count": 0,
    }


def repair_response(job_key, *, sha256="b" * 64, size=8):
    return {
        "status": "ready",
        "job_key": job_key,
        "profile": DEFAULT_REPAIR_PROFILE,
        "reused": False,
        "output_url": "https://cos.example.test/repaired.mp4",
        "output_sha256": sha256,
        "output_size": size,
        "probe": {
            "codec": "h264",
            "pixel_format": "yuv420p",
            "audio_codec": "aac",
            "width": 720,
            "height": 1280,
            "frame_rate": 30.0,
            "duration": 30.0,
            "size": size,
        },
    }


def frozen_plan_snapshot(
    status="running",
    source_date="2026-07-22",
    account_ids=(2, 3, 4),
):
    expected_count = len(account_ids)
    run = {
        "id": 51,
        "run_date": "2026-07-23",
        "source_date": source_date,
        "status": status,
        "expected_count": expected_count,
        "queued_count": expected_count,
        "published_count": 0,
        "failed_count": 0,
        "unknown_count": 0,
        "started_at": "2026-07-23T02:00:00Z",
        "finished_at": "",
        "created_at": "2026-07-23T02:00:00Z",
        "updated_at": "2026-07-23T02:00:00Z",
    }
    queues = [
        {
            "id": 101 + index,
            "run_id": 51,
            "run_date": "2026-07-23",
            "source_date": source_date,
            "account_id": account_id,
            "candidate_rank": index + 1,
            "status": "queued",
            "created_at": "2026-07-23T02:00:00Z",
            "updated_at": "2026-07-23T02:00:00Z",
        }
        for index, account_id in enumerate(account_ids)
    ]
    return {"found": True, "run": run, "queues": queues}


def test_config(start_date="2026-07-23", account_ids=(2, 3, 4)):
    return DailyConfig(
        internal_url="http://127.0.0.1:8810",
        internal_token="unit-test-secret",
        account_ids=tuple(account_ids),
        start_date=start_date,
        mysql_host="read-only.example.test",
        mysql_port=63350,
        mysql_user="reader",
        mysql_password="password",
        mysql_database="kunlunads_dev",
        mysql_connect_timeout=5,
        mysql_read_timeout=30,
        scan_limit=100,
        candidate_pool_limit=10,
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


class FakeSidecar:
    def __init__(self, rate_limit_second=False):
        self.events = []
        self.rate_limit_second = rate_limit_second

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

    def preflight_storage(self, path):
        self.events.append(("storage", path))
        return {"ready": True, "mounted": True, "atomic_write": True}

    def query_daily_plan(self, path, run_date):
        self.events.append(("plan_query", path, run_date))
        return {"found": False, "run": None, "queues": []}

    def used_material_keys(self, path, material_ids):
        self.events.append(("used", path, list(material_ids)))
        return {"99"}

    def available_pool_items(self, path, limit):
        self.events.append(("pool", path, limit))
        return [
            {
                "id": material_id,
                "material_id": str(material_id),
                "material_key": str(material_id),
                "created_at": "2026-07-22T00:00:%02dZ" % (material_id % 60),
            }
            for material_id in range(10, 10 + limit)
        ]

    def record_pool_checks(self, path, checks):
        self.events.append(("pool_checks", path, list(checks)))
        return {"updated_count": len(checks)}

    def create_plan(self, path, payload):
        self.events.append(
            (
                "plan",
                path,
                [item["account_id"] for item in payload["candidates"]],
                [item["material_id"] for item in payload["candidates"]],
            )
        )
        return [
            {"id": 101 + index, "account_id": item["account_id"]}
            for index, item in enumerate(payload["candidates"])
        ]

    def record_run_failure(
        self,
        path,
        run_date,
        source_date,
        error_code,
        error_message,
        expected_count,
    ):
        self.events.append(
            (
                "failure",
                path,
                run_date,
                source_date,
                error_code,
                error_message,
                expected_count,
            )
        )
        return {
            "id": 1,
            "run_date": run_date,
            "source_date": source_date,
            "status": "failed_preflight",
            "expected_count": expected_count,
            "recorded": True,
        }

    def publish_queue(self, path_template, queue_id):
        self.events.append(("publish", queue_id))
        if self.rate_limit_second and queue_id == 102:
            raise SidecarError("rate_limit_exceeded", "rate limited", 429)
        return {
            "status": "published",
            "log_id": queue_id + 1000,
            "short_url": "https://ai.yingliangads.com/s2l/%s.html"
            % (queue_id + 1000),
            "post_id": str(queue_id),
            "preview_url": "https://x.com/account/status/%s" % queue_id,
        }


class RunnerTests(unittest.TestCase):
    def test_account_id_parser_accepts_one_and_fifty_but_rejects_invalid_scope(self):
        self.assertEqual(_parse_account_ids("7"), (7,))
        fifty = tuple(range(1, 51))
        self.assertEqual(
            _parse_account_ids(",".join(str(value) for value in fifty)),
            fifty,
        )
        for raw in (
            "",
            "1,1",
            "0",
            "-1",
            "not-an-id",
            ",".join(str(value) for value in range(1, 52)),
        ):
            with self.subTest(raw=raw), self.assertRaises(DailyRunError):
                _parse_account_ids(raw)

    def setUp(self):
        # Keep unit-test media under the OS temp root while preserving the
        # production invariant that work_dir must equal its fixed root.
        patcher = mock.patch(
            "scripts.x_post_daily_runner.FIXED_DAILY_WORK_DIR",
            Path(tempfile.gettempdir()).resolve(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pool_check_audit_is_chunked_to_sidecar_batch_limit(self):
        sidecar = FakeSidecar()
        checks = [
            {
                "pool_item_id": index,
                "error_code": "material_invalid",
                "error_message": "invalid material %s" % index,
            }
            for index in range(1, 206)
        ]

        _record_pool_checks_best_effort(sidecar, test_config(), checks)

        batches = [
            event[2] for event in sidecar.events if event[0] == "pool_checks"
        ]
        self.assertEqual([len(batch) for batch in batches], [100, 100, 5])
        self.assertEqual(
            [item["pool_item_id"] for batch in batches for item in batch],
            list(range(1, 206)),
        )

    def test_loopback_daily_and_health_clients_disable_environment_proxies(self):
        with mock.patch.dict(
            os.environ,
            {
                "http_proxy": "http://proxy.example:8080",
                "https_proxy": "http://proxy.example:8080",
            },
            clear=False,
        ):
            client = SidecarClient(
                "http://127.0.0.1:8810",
                "secret",
                timeout=30,
            )
            repair_client = MediaRepairClient(
                "http://127.0.0.1:18799/internal/x-post-media-repair",
                "repair-secret",
                timeout=30,
            )
        for opener in (
            client.opener,
            repair_client.opener,
            wait_x_post_sidecar._DIRECT_OPENER,
        ):
            proxy_handlers = [
                handler
                for handler in opener.handlers
                if isinstance(handler, urllib.request.ProxyHandler)
            ]
            self.assertFalse(
                any(handler.proxies for handler in proxy_handlers)
            )

    def test_repair_configuration_is_optional_but_independently_authenticated(self):
        disabled = test_config()
        disabled.validate()
        self.assertEqual(disabled.repair_url, "")
        self.assertEqual(disabled.max_repairs_per_run, 6)
        self.assertEqual(disabled.repair_profile, DEFAULT_REPAIR_PROFILE)

        enabled = replace(
            disabled,
            repair_url=(
                "http://127.0.0.1:18799/internal/x-post-media-repair"
            ),
            repair_token="repair-secret",
        )
        enabled.validate()

        with self.assertRaises(DailyRunError):
            replace(
                enabled, repair_token=enabled.internal_token
            ).validate()
        with self.assertRaises(DailyRunError):
            replace(
                enabled,
                repair_url=(
                    "http://gpu.example.test:8799/internal/x-post-media-repair"
                ),
            ).validate()

    def test_daily_repair_token_falls_back_to_dedicated_worker_secret(self):
        environment = {
            "X_POST_DAILY_ACCOUNT_IDS": "2,3,4",
            "X_POST_DAILY_INTERNAL_TOKEN": "daily-secret",
            "X_POST_DAILY_MYSQL_HOST": "read-only.example",
            "X_POST_DAILY_MYSQL_USER": "reader",
            "X_POST_DAILY_MYSQL_PASSWORD": "password",
            "X_POST_DAILY_MEDIA_ALLOWED_HOSTS": "media.example.com",
            "X_POST_DAILY_REPAIR_URL": (
                "http://127.0.0.1/internal/x-post-media-repair"
            ),
            "X_POST_MEDIA_REPAIR_TOKEN": "dedicated-repair-secret",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            config = DailyConfig.from_env()
        self.assertEqual(config.repair_token, "dedicated-repair-secret")
        self.assertNotEqual(config.repair_token, config.internal_token)

    def test_repair_client_sends_exact_contract_and_validates_ready_response(self):
        class Response:
            status = 200

            def __init__(self, payload):
                self.raw = json.dumps(payload).encode("utf-8")

            def read(self, _limit):
                return self.raw

            def getcode(self):
                return self.status

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class CaptureOpener:
            def __init__(self):
                self.payload = None
                self.authorization = ""

            def open(self, request, timeout):
                self.payload = json.loads(request.data.decode("utf-8"))
                self.authorization = request.headers["Authorization"]
                self.timeout = timeout
                return Response(repair_response(self.payload["job_key"]))

        request_payload = {
            "job_key": "c" * 64,
            "material_id": "10",
            "pool_item_id": 10,
            "source_url": "https://media.example.test/10.mp4",
            "source_sha256": "a" * 64,
            "source_size": 5,
            "trigger_code": "invalid_media_codec",
            "profile": DEFAULT_REPAIR_PROFILE,
            "duration_policy": "standard",
        }
        opener = CaptureOpener()
        client = MediaRepairClient(
            "http://127.0.0.1:18799/internal/x-post-media-repair",
            "repair-secret",
            timeout=45,
            max_output_bytes=1024,
            opener=opener,
        )

        result = client.repair(request_payload)

        self.assertEqual(opener.payload, request_payload)
        self.assertEqual(opener.authorization, "Bearer repair-secret")
        self.assertEqual(opener.timeout, 45)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["output_sha256"], "b" * 64)

        resource_id = "21c09b915223a0695f0a4cf85386cabd"
        resource_payload = dict(
            request_payload,
            material_id=resource_id,
            job_key="d" * 64,
        )
        result = client.repair(resource_payload)
        self.assertEqual(
            opener.payload["material_id"],
            resource_id,
        )
        self.assertEqual(result["job_key"], "d" * 64)

    def test_repair_client_rejects_unsafe_material_id_before_network(self):
        class RejectNetwork:
            def __init__(self):
                self.calls = 0

            def open(self, _request, timeout):
                self.calls += 1
                raise AssertionError("invalid requests must not reach the worker")

        opener = RejectNetwork()
        client = MediaRepairClient(
            "http://127.0.0.1:18799/internal/x-post-media-repair",
            "repair-secret",
            opener=opener,
        )
        payload = {
            "job_key": "c" * 64,
            "material_id": "../21c09b915223a0695f0a4cf85386cabd",
            "pool_item_id": 10,
            "source_url": "https://media.example.test/10.mp4",
            "source_sha256": "a" * 64,
            "source_size": 5,
            "trigger_code": "invalid_media_codec",
            "profile": DEFAULT_REPAIR_PROFILE,
            "duration_policy": "standard",
        }

        with self.assertRaises(MediaRepairError) as caught:
            client.repair(payload)

        self.assertEqual(
            caught.exception.code,
            "x_post_media_repair_invalid_request",
        )
        self.assertEqual(opener.calls, 0)

        payload["material_id"] = "21C09B915223A0695F0A4CF85386CABD"
        with self.assertRaises(MediaRepairError) as caught:
            client.repair(payload)
        self.assertEqual(
            caught.exception.code,
            "x_post_media_repair_invalid_request",
        )
        self.assertEqual(opener.calls, 0)

    def test_repair_client_rejects_oversized_or_non_https_output(self):
        class Response:
            status = 200

            def __init__(self, raw):
                self.raw = raw

            def read(self, _limit):
                return self.raw

            def getcode(self):
                return self.status

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class StaticOpener:
            def __init__(self, raw):
                self.raw = raw

            def open(self, _request, timeout):
                return Response(self.raw)

        payload = {
            "job_key": "c" * 64,
            "material_id": "10",
            "pool_item_id": 10,
            "source_url": "https://media.example.test/10.mp4",
            "source_sha256": "a" * 64,
            "source_size": 5,
            "trigger_code": "invalid_media_codec",
            "profile": DEFAULT_REPAIR_PROFILE,
            "duration_policy": "standard",
        }
        oversized = MediaRepairClient(
            "http://127.0.0.1:18799/internal/x-post-media-repair",
            "repair-secret",
            opener=StaticOpener(b"x" * (64 * 1024 + 1)),
        )
        with self.assertRaises(MediaRepairError):
            oversized.repair(payload)

        invalid = repair_response(payload["job_key"])
        invalid["output_url"] = "http://cos.example.test/repaired.mp4"
        non_https = MediaRepairClient(
            "http://127.0.0.1:18799/internal/x-post-media-repair",
            "repair-secret",
            opener=StaticOpener(json.dumps(invalid).encode("utf-8")),
        )
        with self.assertRaises(MediaRepairError):
            non_https.repair(payload)

    def test_material_occupancy_request_is_bounded_to_supplied_keys(self):
        class Response:
            status = 200

            def read(self, _limit):
                return b'{"item":{"material_keys":["10"]}}'

            def getcode(self):
                return self.status

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class CaptureOpener:
            def __init__(self):
                self.payload = None

            def open(self, request, timeout):
                self.payload = json.loads(request.data.decode("utf-8"))
                return Response()

        opener = CaptureOpener()
        client = SidecarClient(
            "http://127.0.0.1:8810", "secret", timeout=30, opener=opener
        )
        self.assertEqual(
            client.used_material_keys(
                "/internal/posts/material-keys/query", ["10", "11"]
            ),
            {"10"},
        )
        self.assertEqual(opener.payload, {"material_keys": ["10", "11"]})

    def test_material_pool_response_uses_newest_first_order(self):
        class Response:
            status = 200

            def __init__(self, items):
                self.raw = json.dumps({"items": items}).encode("utf-8")

            def read(self, _limit):
                return self.raw

            def getcode(self):
                return self.status

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class PoolOpener:
            def __init__(self, items):
                self.items = items

            def open(self, _request, timeout):
                self.timeout = timeout
                return Response(self.items)

        newest_first = [
            {
                "id": 12,
                "material_id": "120",
                "material_key": "120",
                "created_at": "2026-08-10T10:57:08Z",
            },
            {
                "id": 11,
                "material_id": "110",
                "material_key": "110",
                "created_at": "2026-08-10T10:57:08Z",
            },
            {
                "id": 10,
                "material_id": "100",
                "material_key": "100",
                "created_at": "2026-08-10T10:11:28Z",
            },
        ]
        accepted = SidecarClient(
            "http://127.0.0.1:8810",
            "secret",
            timeout=30,
            opener=PoolOpener(newest_first),
        ).available_pool_items("/internal/posts/material-pool/available", 10)
        self.assertEqual([item["id"] for item in accepted], [12, 11, 10])

        with self.assertRaises(SidecarError) as caught:
            SidecarClient(
                "http://127.0.0.1:8810",
                "secret",
                timeout=30,
                opener=PoolOpener(list(reversed(newest_first))),
            ).available_pool_items(
                "/internal/posts/material-pool/available",
                10,
            )
        self.assertEqual(
            caught.exception.code,
            "x_post_pool_invalid_response",
        )

    def test_sidecar_parses_flat_unknown_publish_error(self):
        class ErrorOpener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(
                    request.full_url,
                    503,
                    "failed",
                    {},
                    io.BytesIO(
                        b'{"error":"x_publish_unknown","message":"outcome unknown"}'
                    ),
                )

        client = SidecarClient(
            "http://127.0.0.1:8810",
            "secret",
            timeout=30,
            opener=ErrorOpener(),
        )
        with self.assertRaises(SidecarError) as captured:
            client.post(
                "/internal/posts/queue/1/publish",
                {},
                write_may_have_happened=True,
            )
        self.assertEqual(captured.exception.code, "x_publish_unknown")
        self.assertTrue(captured.exception.unknown_outcome)

    def test_sidecar_keeps_structured_known_5xx_as_known(self):
        class ErrorOpener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(
                    request.full_url,
                    502,
                    "failed",
                    {},
                    io.BytesIO(
                        b'{"error":"media_download_failed","message":"cdn unavailable",'
                        b'"outcome_known":true,"unknown_outcome":false}'
                    ),
                )

        client = SidecarClient(
            "http://127.0.0.1:8810",
            "secret",
            timeout=30,
            opener=ErrorOpener(),
        )
        with self.assertRaises(SidecarError) as captured:
            client.post(
                "/internal/posts/queue/1/publish",
                {},
                write_may_have_happened=True,
            )
        self.assertEqual(captured.exception.code, "media_download_failed")
        self.assertFalse(captured.exception.unknown_outcome)

    def test_pre_create_media_errors_remain_known(self):
        for code, status in (
            ("x_media_processing_failed", 502),
            ("x_media_processing_timeout", 504),
            ("http_response_too_large", 502),
        ):
            with self.subTest(code=code):
                class ErrorOpener:
                    def open(self, request, timeout):
                        raise urllib.error.HTTPError(
                            request.full_url,
                            status,
                            "failed",
                            {},
                            io.BytesIO(
                                json.dumps(
                                    {
                                        "error": code,
                                        "message": "pre-create failure",
                                        "outcome_known": True,
                                        "unknown_outcome": False,
                                    }
                                ).encode("utf-8")
                            ),
                        )

                client = SidecarClient(
                    "http://127.0.0.1:8810",
                    "secret",
                    timeout=30,
                    opener=ErrorOpener(),
                )
                with self.assertRaises(SidecarError) as captured:
                    client.publish_queue(
                        "/internal/posts/queue/{queue_id}/publish", 101
                    )
                self.assertEqual(captured.exception.code, code)
                self.assertFalse(captured.exception.unknown_outcome)

    def test_publish_http_error_without_explicit_outcome_is_unknown(self):
        for body in (
            b'{"error":',
            b'{"error":"future_code"}',
            b'{"error":"future_code","outcome_known":true,"unknown_outcome":"false"}',
            b'{"error":"future_code","outcome_known":true,"unknown_outcome":true}',
        ):
            with self.subTest(body=body):
                class ErrorOpener:
                    def open(self, request, timeout):
                        raise urllib.error.HTTPError(
                            request.full_url,
                            409,
                            "failed",
                            {},
                            io.BytesIO(body),
                        )

                client = SidecarClient(
                    "http://127.0.0.1:8810",
                    "secret",
                    timeout=30,
                    opener=ErrorOpener(),
                )
                with self.assertRaises(SidecarError) as captured:
                    client.publish_queue(
                        "/internal/posts/queue/{queue_id}/publish",
                        101,
                    )
                self.assertTrue(captured.exception.unknown_outcome)

    def test_http_200_empty_publish_response_is_unknown(self):
        class Response:
            status = 200

            def read(self, _limit):
                return b"{}"

            def getcode(self):
                return self.status

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class EmptyOpener:
            def open(self, _request, timeout):
                self.timeout = timeout
                return Response()

        client = SidecarClient(
            "http://127.0.0.1:8810",
            "secret",
            timeout=30,
            opener=EmptyOpener(),
        )
        with self.assertRaises(SidecarError) as captured:
            client.publish_queue("/internal/posts/queue/{queue_id}/publish", 101)
        self.assertEqual(captured.exception.code, "x_publish_invalid_response")
        self.assertTrue(captured.exception.unknown_outcome)

    def test_publish_response_accepts_current_g2flow_short_url(self):
        class Response:
            status = 200

            def read(self, _limit):
                return json.dumps(
                    {
                        "item": {
                            "status": "published",
                            "log_id": 101,
                            "short_url": "https://gy.g2flow.com/s2l/101.html",
                            "post_id": "123456789",
                            "preview_url": "https://x.com/account/status/123456789",
                        }
                    }
                ).encode("utf-8")

            def getcode(self):
                return self.status

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class SuccessOpener:
            def open(self, _request, timeout):
                self.timeout = timeout
                return Response()

        client = SidecarClient(
            "http://127.0.0.1:8810",
            "secret",
            timeout=30,
            opener=SuccessOpener(),
        )
        result = client.publish_queue(
            "/internal/posts/queue/{queue_id}/publish",
            101,
        )
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["short_url"], "https://gy.g2flow.com/s2l/101.html")

    def test_publish_response_rejects_legacy_short_host_as_unknown(self):
        class Response:
            status = 200

            def read(self, _limit):
                return json.dumps(
                    {
                        "item": {
                            "status": "published",
                            "log_id": 101,
                            "short_url": "https://ai.yingliangads.com/s2l/101.html",
                            "post_id": "123456789",
                            "preview_url": "https://x.com/account/status/123456789",
                        }
                    }
                ).encode("utf-8")

            def getcode(self):
                return self.status

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class LegacyOpener:
            def open(self, _request, timeout):
                self.timeout = timeout
                return Response()

        client = SidecarClient(
            "http://127.0.0.1:8810",
            "secret",
            timeout=30,
            opener=LegacyOpener(),
        )
        with self.assertRaises(SidecarError) as captured:
            client.publish_queue(
                "/internal/posts/queue/{queue_id}/publish",
                101,
            )
        self.assertEqual(captured.exception.code, "x_publish_invalid_response")
        self.assertTrue(captured.exception.unknown_outcome)

    def test_truncated_sidecar_publish_response_is_unknown(self):
        class TruncatedOpener:
            def open(self, _request, timeout):
                self.timeout = timeout
                raise http.client.IncompleteRead(b'{"item":{"status":"published"', 100)

        client = SidecarClient(
            "http://127.0.0.1:8810",
            "secret",
            timeout=30,
            opener=TruncatedOpener(),
        )
        with self.assertRaises(SidecarError) as captured:
            client.publish_queue("/internal/posts/queue/{queue_id}/publish", 101)
        self.assertEqual(captured.exception.code, "x_sidecar_unreachable")
        self.assertTrue(captured.exception.unknown_outcome)

    def test_truncated_sidecar_http_error_body_is_unknown(self):
        class BrokenErrorBody:
            def read(self, _limit):
                raise http.client.IncompleteRead(b'{"error":"x_publish_unknown"', 20)

            def close(self):
                return None

        class TruncatedErrorOpener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(
                    request.full_url,
                    503,
                    "failed",
                    {},
                    BrokenErrorBody(),
                )

        client = SidecarClient(
            "http://127.0.0.1:8810",
            "secret",
            timeout=30,
            opener=TruncatedErrorOpener(),
        )
        with self.assertRaises(SidecarError) as captured:
            client.publish_queue("/internal/posts/queue/{queue_id}/publish", 101)
        self.assertEqual(captured.exception.code, "x_sidecar_unreachable")
        self.assertTrue(captured.exception.unknown_outcome)

    def test_oversized_sidecar_publish_response_is_unknown(self):
        class Response:
            status = 200

            def read(self, _limit):
                return b"x" * (MAX_SIDECAR_RESPONSE_BYTES + 1)

            def getcode(self):
                return self.status

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class OversizedOpener:
            def open(self, _request, timeout):
                self.timeout = timeout
                return Response()

        client = SidecarClient(
            "http://127.0.0.1:8810",
            "secret",
            timeout=30,
            opener=OversizedOpener(),
        )
        with self.assertRaises(SidecarError) as captured:
            client.publish_queue("/internal/posts/queue/{queue_id}/publish", 101)
        self.assertEqual(captured.exception.code, "x_sidecar_invalid_response")
        self.assertTrue(captured.exception.unknown_outcome)

    def test_malformed_sidecar_publish_response_is_unknown(self):
        class Response:
            status = 200

            def read(self, _limit):
                return b'{"item":'

            def getcode(self):
                return self.status

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class MalformedOpener:
            def open(self, _request, timeout):
                self.timeout = timeout
                return Response()

        client = SidecarClient(
            "http://127.0.0.1:8810",
            "secret",
            timeout=30,
            opener=MalformedOpener(),
        )
        with self.assertRaises(SidecarError) as captured:
            client.publish_queue("/internal/posts/queue/{queue_id}/publish", 101)
        self.assertEqual(captured.exception.code, "x_publish_invalid_response")
        self.assertTrue(captured.exception.unknown_outcome)

    def test_daily_plan_query_strictly_accepts_only_identity_snapshot(self):
        class Response:
            status = 200

            def __init__(self, payload):
                self.raw = json.dumps(payload).encode("utf-8")

            def read(self, _limit):
                return self.raw

            def getcode(self):
                return self.status

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Opener:
            def __init__(self, payload):
                self.payload = payload
                self.request_payload = None

            def open(self, request, timeout):
                self.request_payload = json.loads(request.data.decode("utf-8"))
                return Response(self.payload)

        snapshot = frozen_plan_snapshot()
        opener = Opener({"item": snapshot})
        client = SidecarClient(
            "http://127.0.0.1:8810",
            "secret",
            timeout=30,
            opener=opener,
        )
        queried = client.query_daily_plan(
            "/internal/posts/daily-plan/query",
            "2026-07-23",
        )
        self.assertEqual(opener.request_payload, {"run_date": "2026-07-23"})
        self.assertEqual(
            [queue["account_id"] for queue in queried["queues"]],
            [2, 3, 4],
        )

        leaked = frozen_plan_snapshot()
        leaked["queues"][0]["material_url"] = (
            "https://media.example.test/private-copy.mp4"
        )
        with self.assertRaises(SidecarError) as rejected:
            SidecarClient(
                "http://127.0.0.1:8810",
                "secret",
                timeout=30,
                opener=Opener({"item": leaked}),
            ).query_daily_plan(
                "/internal/posts/daily-plan/query",
                "2026-07-23",
            )
        self.assertEqual(
            rejected.exception.code,
            "x_daily_plan_query_invalid_response",
        )

    def test_daily_plan_response_requires_unique_queue_and_account_identities(self):
        request_payload = {
            "run_date": "2026-07-23",
            "source_date": "2026-07-22",
            "candidates": [
                {"account_id": 2, "material_id": "10", "pool_item_id": 10},
                {"account_id": 3, "material_id": "11", "pool_item_id": 11},
                {"account_id": 4, "material_id": "12", "pool_item_id": 12},
            ],
        }
        valid_queues = [
            {
                "id": 101 + index,
                "run_id": 51,
                "account_id": account_id,
                "material_id": str(10 + index),
                "pool_item_id": 10 + index,
                "run_date": request_payload["run_date"],
                "source_date": request_payload["source_date"],
            }
            for index, account_id in enumerate((2, 3, 4))
        ]

        class Response:
            status = 200

            def __init__(self, payload):
                self.raw = json.dumps(payload).encode("utf-8")

            def read(self, _limit):
                return self.raw

            def getcode(self):
                return self.status

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Opener:
            def __init__(self, payload):
                self.payload = payload

            def open(self, _request, timeout):
                self.timeout = timeout
                return Response(self.payload)

        def result(queues, **overrides):
            item = {
                "id": 51,
                "status": "queued",
                "run_date": request_payload["run_date"],
                "source_date": request_payload["source_date"],
                "expected_count": len(request_payload["candidates"]),
                "queues": queues,
            }
            item.update(overrides)
            return {"item": item}

        valid_client = SidecarClient(
            "http://127.0.0.1:8810",
            "secret",
            timeout=30,
            opener=Opener(result(valid_queues)),
        )
        self.assertEqual(
            [item["id"] for item in valid_client.create_plan("/plan", request_payload)],
            [101, 102, 103],
        )

        invalid_cases = {}
        duplicate_queue = [dict(item) for item in valid_queues]
        duplicate_queue[1]["id"] = duplicate_queue[0]["id"]
        invalid_cases["duplicate_queue_id"] = duplicate_queue
        duplicate_account = [dict(item) for item in valid_queues]
        duplicate_account[1]["account_id"] = duplicate_account[0]["account_id"]
        invalid_cases["duplicate_account_id"] = duplicate_account
        missing_account = [dict(item) for item in valid_queues]
        missing_account[1].pop("account_id")
        invalid_cases["missing_account_id"] = missing_account
        string_account = [dict(item) for item in valid_queues]
        string_account[1]["account_id"] = "3"
        invalid_cases["non_integer_account_id"] = string_account
        wrong_run = [dict(item) for item in valid_queues]
        wrong_run[1]["run_id"] = 52
        invalid_cases["wrong_run_id"] = wrong_run
        wrong_material = [dict(item) for item in valid_queues]
        wrong_material[1]["material_id"] = "999"
        invalid_cases["wrong_material_id"] = wrong_material
        wrong_pool = [dict(item) for item in valid_queues]
        wrong_pool[1]["pool_item_id"] = 999
        invalid_cases["wrong_pool_item_id"] = wrong_pool

        for name, queues in invalid_cases.items():
            with self.subTest(name=name):
                client = SidecarClient(
                    "http://127.0.0.1:8810",
                    "secret",
                    timeout=30,
                    opener=Opener(result(queues)),
                )
                with self.assertRaises(SidecarError) as captured:
                    client.create_plan("/plan", request_payload)
                self.assertEqual(
                    captured.exception.code,
                    "x_daily_plan_invalid_response",
                )
                self.assertTrue(captured.exception.unknown_outcome)

        wrong_date_client = SidecarClient(
            "http://127.0.0.1:8810",
            "secret",
            timeout=30,
            opener=Opener(result(valid_queues, run_date="2026-07-24")),
        )
        with self.assertRaises(SidecarError) as wrong_date:
            wrong_date_client.create_plan("/plan", request_payload)
        self.assertTrue(wrong_date.exception.unknown_outcome)

    def test_daily_config_never_falls_back_to_backend_internal_token(self):
        environment = {
            "X_POST_AUTOMATION_INTERNAL_TOKEN": "broad-backend-token",
            "X_POST_DAILY_ACCOUNT_IDS": "2,3,4",
            "X_POST_DAILY_MYSQL_HOST": "read-only.example",
            "X_POST_DAILY_MYSQL_USER": "reader",
            "X_POST_DAILY_MYSQL_PASSWORD": "password",
            "X_POST_DAILY_MEDIA_ALLOWED_HOSTS": "media.example.com",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            config = DailyConfig.from_env()
        self.assertEqual(config.internal_token, "")
        with self.assertRaises(DailyRunError) as missing:
            config.validate()
        self.assertIn("X_POST_DAILY_INTERNAL_TOKEN", str(missing.exception))

    def _run(self, sidecar, loaded_candidates=None, config=None):
        config = config or test_config()
        connection = FakeConnection([])
        preflight_events = []

        def loader(
            _connection,
            pool_items,
            source_date,
            limit,
            schema,
        ):
            self.assertEqual(source_date, "2026-07-22")
            self.assertEqual([item["id"] for item in pool_items], list(range(10, 110)))
            self.assertEqual(limit, config.candidate_pool_limit)
            self.assertEqual(schema, config.mysql_database)
            return (
                loaded_candidates
                or [
                    candidate(10, 400),
                    candidate(11, 300),
                    candidate(12, 200),
                ],
                [],
            )

        def downloader(url, destination, allowed_hosts, max_bytes, timeout):
            preflight_events.append(("download", url))
            Path(destination).write_bytes(b"video")
            return {"size": 5, "sha256": "a" * 64, "media_type": "video/mp4"}

        def prober(path, max_bytes, timeout, max_duration_seconds=140.0):
            preflight_events.append(("probe", Path(path).name))
            return {"duration": 30.0, "width": 720, "height": 1280}

        result = execute_daily_run(
            config,
            sidecar=sidecar,
            connection_factory=lambda _config: connection,
            pool_candidate_loader=loader,
            downloader=downloader,
            prober=prober,
            now=datetime(2026, 7, 23, 10, 0, tzinfo=timezone(timedelta(hours=8))),
        )
        return result, sidecar.events, preflight_events, connection

    def test_verifies_all_then_preflights_three_then_plans_and_publishes_in_order(self):
        sidecar = FakeSidecar()
        result, events, preflight_events, connection = self._run(sidecar)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["published_count"], 3)
        self.assertEqual(
            events[:6],
            [
                (
                    "plan_query",
                    "/internal/posts/daily-plan/query",
                    "2026-07-23",
                ),
                ("storage", "/internal/posts/storage/preflight"),
                ("verify", 2),
                ("verify", 3),
                ("verify", 4),
                (
                    "pool",
                    "/internal/posts/material-pool/available",
                    100,
                ),
            ],
        )
        self.assertEqual(
            events[6], ("storage", "/internal/posts/storage/preflight")
        )
        self.assertEqual(events[7][0], "plan")
        self.assertEqual(events[7][2], [2, 3, 4])
        self.assertEqual(events[7][3], ["10", "11", "12"])
        self.assertEqual(events[8:], [("publish", 101), ("publish", 102), ("publish", 103)])
        self.assertEqual(len([item for item in preflight_events if item[0] == "download"]), 3)
        self.assertTrue(connection.closed)

    def test_nine_accounts_all_publish_successfully_in_configured_order(self):
        account_ids = tuple(range(2, 11))
        config = test_config(account_ids=account_ids)
        loaded_candidates = [
            candidate(material_id, 1000 - rank)
            for rank, material_id in enumerate(range(10, 19), 1)
        ]
        sidecar = FakeSidecar()

        result, events, preflight_events, connection = self._run(
            sidecar,
            loaded_candidates=loaded_candidates,
            config=config,
        )

        self.assertEqual(result["status"], "published")
        self.assertEqual(result["planned_count"], 9)
        self.assertEqual(result["published_count"], 9)
        self.assertEqual(
            [event[1] for event in events if event[0] == "verify"],
            list(account_ids),
        )
        plan = next(event for event in events if event[0] == "plan")
        self.assertEqual(plan[2], list(account_ids))
        self.assertEqual(plan[3], [str(value) for value in range(10, 19)])
        self.assertEqual(
            [event[1] for event in events if event[0] == "publish"],
            list(range(101, 110)),
        )
        self.assertEqual(
            len([event for event in preflight_events if event[0] == "download"]),
            9,
        )
        self.assertTrue(connection.closed)

    def test_existing_plan_resumes_before_accounts_pool_or_gpu(self):
        class ExistingPlanSidecar(FakeSidecar):
            def query_daily_plan(self, path, run_date):
                self.events.append(("plan_query", path, run_date))
                return frozen_plan_snapshot()

        class ForbiddenRepair:
            def repair(self, _payload):
                raise AssertionError("GPU repair must not run for a frozen plan")

        sidecar = ExistingPlanSidecar()
        result = execute_daily_run(
            test_config(),
            sidecar=sidecar,
            connection_factory=lambda _config: self.fail(
                "source database must not be queried for a frozen plan"
            ),
            pool_candidate_loader=lambda *_args, **_kwargs: self.fail(
                "material pool selection must not run for a frozen plan"
            ),
            downloader=lambda *_args, **_kwargs: self.fail(
                "media download must not run for a frozen plan"
            ),
            repair_client=ForbiddenRepair(),
            now=datetime(
                2026,
                7,
                23,
                10,
                0,
                tzinfo=timezone(timedelta(hours=8)),
            ),
        )
        self.assertEqual(result["status"], "published")
        self.assertTrue(result["resumed_existing_plan"])
        self.assertEqual(
            sidecar.events,
            [
                (
                    "plan_query",
                    "/internal/posts/daily-plan/query",
                    "2026-07-23",
                ),
                ("publish", 101),
                ("publish", 102),
                ("publish", 103),
            ],
        )

    def test_existing_three_queue_plan_resumes_under_expanded_nine_account_scope(self):
        class ExistingPlanSidecar(FakeSidecar):
            def query_daily_plan(self, path, run_date):
                self.events.append(("plan_query", path, run_date))
                return frozen_plan_snapshot(account_ids=(2, 3, 4))

            def preflight_storage(self, _path):
                raise AssertionError("frozen plan recovery must skip storage preflight")

            def verify_account(self, _account_id):
                raise AssertionError("frozen plan recovery must skip account verification")

            def available_pool_items(self, _path, _limit):
                raise AssertionError("frozen plan recovery must skip material selection")

            def create_plan(self, _path, _payload):
                raise AssertionError("frozen plan recovery must not create six queues")

        sidecar = ExistingPlanSidecar()
        result = execute_daily_run(
            test_config(account_ids=tuple(range(2, 11))),
            sidecar=sidecar,
            connection_factory=lambda _config: self.fail(
                "source database must not be queried for a frozen plan"
            ),
            now=datetime(
                2026,
                7,
                23,
                10,
                0,
                tzinfo=timezone(timedelta(hours=8)),
            ),
        )

        self.assertEqual(result["status"], "published")
        self.assertTrue(result["resumed_existing_plan"])
        self.assertEqual(result["planned_count"], 3)
        self.assertEqual(result["published_count"], 3)
        self.assertEqual(
            sidecar.events,
            [
                (
                    "plan_query",
                    "/internal/posts/daily-plan/query",
                    "2026-07-23",
                ),
                ("publish", 101),
                ("publish", 102),
                ("publish", 103),
            ],
        )

    def test_old_three_account_failed_preflight_conflicts_with_nine_account_scope(self):
        class FailedPreflightSidecar(FakeSidecar):
            def query_daily_plan(self, path, run_date):
                self.events.append(("plan_query", path, run_date))
                snapshot = frozen_plan_snapshot(
                    status="failed_preflight",
                    account_ids=(2, 3, 4),
                )
                snapshot["run"]["queued_count"] = 0
                snapshot["queues"] = []
                return snapshot

        sidecar = FailedPreflightSidecar()
        with self.assertRaises(DailyRunError) as captured:
            execute_daily_run(
                test_config(account_ids=tuple(range(2, 11))),
                sidecar=sidecar,
                now=datetime(
                    2026,
                    7,
                    23,
                    10,
                    0,
                    tzinfo=timezone(timedelta(hours=8)),
                ),
            )
        self.assertEqual(captured.exception.code, "x_post_daily_resume_conflict")
        self.assertEqual(
            sidecar.events,
            [
                (
                    "plan_query",
                    "/internal/posts/daily-plan/query",
                    "2026-07-23",
                )
            ],
        )

    def test_existing_plan_unknown_outcome_still_stops_recovery(self):
        class UnknownPlanSidecar(FakeSidecar):
            def query_daily_plan(self, path, run_date):
                self.events.append(("plan_query", path, run_date))
                return frozen_plan_snapshot(status="needs_review")

            def publish_queue(self, path_template, queue_id):
                self.events.append(("publish", queue_id))
                if queue_id == 102:
                    raise SidecarError(
                        "x_post_unknown_outcome",
                        "requires reconciliation",
                        409,
                        unknown_outcome=True,
                    )
                return {
                    "status": "published",
                    "log_id": queue_id + 1000,
                    "short_url": (
                        "https://ai.yingliangads.com/s2l/%s.html"
                        % (queue_id + 1000)
                    ),
                    "post_id": str(queue_id),
                    "preview_url": (
                        "https://x.com/account/status/%s" % queue_id
                    ),
                }

        sidecar = UnknownPlanSidecar()
        result = execute_daily_run(
            test_config(),
            sidecar=sidecar,
            now=datetime(
                2026,
                7,
                23,
                10,
                0,
                tzinfo=timezone(timedelta(hours=8)),
            ),
        )
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(result["resumed_existing_plan"])
        self.assertEqual(
            [event for event in sidecar.events if event[0] == "publish"],
            [("publish", 101), ("publish", 102)],
        )

    def test_existing_plan_anomaly_fails_before_preflight(self):
        class PartialPlanSidecar(FakeSidecar):
            def query_daily_plan(self, path, run_date):
                self.events.append(("plan_query", path, run_date))
                snapshot = frozen_plan_snapshot()
                snapshot["queues"] = snapshot["queues"][:2]
                return snapshot

        sidecar = PartialPlanSidecar()
        with self.assertRaises(DailyRunError) as captured:
            execute_daily_run(
                test_config(),
                sidecar=sidecar,
                now=datetime(
                    2026,
                    7,
                    23,
                    10,
                    0,
                    tzinfo=timezone(timedelta(hours=8)),
                ),
            )
        self.assertEqual(captured.exception.code, "x_post_daily_resume_conflict")
        self.assertEqual(len(sidecar.events), 1)

    def test_failed_preflight_without_queues_can_build_a_fresh_plan(self):
        class FailedPreflightSidecar(FakeSidecar):
            def query_daily_plan(self, path, run_date):
                self.events.append(("plan_query", path, run_date))
                snapshot = frozen_plan_snapshot(status="failed_preflight")
                snapshot["run"]["queued_count"] = 0
                snapshot["queues"] = []
                return snapshot

        sidecar = FailedPreflightSidecar()
        result, events, _preflight, _connection = self._run(sidecar)
        self.assertEqual(result["status"], "published")
        self.assertFalse(result["resumed_existing_plan"])
        self.assertTrue(any(event[0] == "verify" for event in events))
        self.assertTrue(any(event[0] == "pool" for event in events))
        self.assertTrue(any(event[0] == "plan" for event in events))

    def test_429_stops_remaining_accounts(self):
        sidecar = FakeSidecar(rate_limit_second=True)
        result, events, _preflight, _connection = self._run(sidecar)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(
            [event for event in events if event[0] == "publish"],
            [("publish", 101), ("publish", 102)],
        )

    def test_known_502_continues_remaining_accounts(self):
        class KnownFailureSidecar(FakeSidecar):
            def publish_queue(self, path_template, queue_id):
                self.events.append(("publish", queue_id))
                if queue_id == 101:
                    raise SidecarError(
                        "media_download_failed",
                        "cdn unavailable",
                        502,
                        unknown_outcome=False,
                    )
                return {
                    "status": "published",
                    "log_id": queue_id + 1000,
                    "short_url": "https://ai.yingliangads.com/s2l/%s.html"
                    % (queue_id + 1000),
                    "post_id": str(queue_id),
                    "preview_url": "https://x.com/account/status/%s" % queue_id,
                }

        sidecar = KnownFailureSidecar()
        result, events, _preflight, _connection = self._run(sidecar)
        self.assertEqual(result["status"], "completed_with_failures")
        self.assertEqual(result["published_count"], 2)
        self.assertEqual(
            [event for event in events if event[0] == "publish"],
            [("publish", 101), ("publish", 102), ("publish", 103)],
        )

    def test_first_publish_verify_rate_limit_stops_remaining_accounts(self):
        class FirstRateLimitSidecar(FakeSidecar):
            def publish_queue(self, path_template, queue_id):
                self.events.append(("publish", queue_id))
                raise SidecarError(
                    "x_post_rate_limited",
                    "X API usage cap",
                    429,
                    unknown_outcome=False,
                )

        sidecar = FirstRateLimitSidecar()
        result, events, _preflight, _connection = self._run(sidecar)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["published_count"], 0)
        self.assertEqual(
            [event for event in events if event[0] == "publish"],
            [("publish", 101)],
        )
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(
            result["results"][0]["error_code"],
            "x_post_rate_limited",
        )

    def test_copy_validation_rejects_then_replenishes_before_plan(self):
        sidecar = FakeSidecar()
        invalid = candidate(10, 400)
        invalid["tag"] = "bad*reserved"
        loaded = [
            invalid,
            candidate(11, 300),
            candidate(12, 200),
            candidate(13, 100),
        ]
        result, events, preflight, _connection = self._run(sidecar, loaded)
        self.assertEqual(result["status"], "published")
        plan = next(event for event in events if event[0] == "plan")
        self.assertEqual(plan[3], ["11", "12", "13"])
        downloaded_urls = [event[1] for event in preflight if event[0] == "download"]
        self.assertFalse(any("/10.mp4" in url for url in downloaded_urls))

    def test_long_material_routes_only_to_token_confirmed_premium_account(self):
        config = test_config()
        accounts = [
            {
                "id": 2,
                "username": "premium2",
                "x_user_id": "2002",
                "display_name": "Premium 2",
                "subscription_type": "premium",
                "long_video_eligible": True,
            },
            {
                "id": 3,
                "username": "standard3",
                "x_user_id": "2003",
                "display_name": "Standard 3",
                "subscription_type": "none",
                "long_video_eligible": False,
            },
            {
                "id": 4,
                "username": "standard4",
                "x_user_id": "2004",
                "display_name": "Standard 4",
                "subscription_type": "unknown",
                "long_video_eligible": False,
            },
        ]
        probes = []

        def downloader(_url, destination, _hosts, max_bytes, timeout):
            Path(destination).write_bytes(b"video")
            return {
                "size": 5,
                "sha256": "a" * 64,
                "media_type": "video/mp4",
            }

        def prober(
            path, max_bytes, timeout, max_duration_seconds=140.0
        ):
            material_id = Path(path).stem
            probes.append((material_id, max_duration_seconds))
            if material_id == "10" and max_duration_seconds == 140.0:
                raise XPostError(
                    "x_long_video_requires_premium",
                    "premium required",
                    422,
                )
            return {
                "duration": 180.0 if material_id == "10" else 30.0,
                "width": 720,
                "height": 1280,
            }

        accepted, failures = _preflight_candidates(
            config,
            [candidate(10, 400), candidate(11, 300), candidate(12, 200)],
            accounts,
            1784772000,
            downloader,
            prober,
        )

        self.assertEqual(failures, [])
        self.assertEqual(
            [(item["account_id"], item["material_id"]) for item in accepted],
            [(2, "10"), (3, "11"), (4, "12")],
        )
        self.assertEqual(
            probes,
            [
                ("10", 140.0),
                ("10", 14400.0),
                ("11", 140.0),
                ("12", 140.0),
            ],
        )

    def test_long_material_without_premium_stays_retryable_and_short_items_fill(self):
        config = test_config()
        accounts = [
            {
                "id": account_id,
                "username": "standard%s" % account_id,
                "x_user_id": "200%s" % account_id,
                "display_name": "Standard %s" % account_id,
                "subscription_type": "none",
                "long_video_eligible": False,
            }
            for account_id in (2, 3, 4)
        ]

        def downloader(_url, destination, _hosts, max_bytes, timeout):
            Path(destination).write_bytes(b"video")
            return {
                "size": 5,
                "sha256": "a" * 64,
                "media_type": "video/mp4",
            }

        def prober(
            path, max_bytes, timeout, max_duration_seconds=140.0
        ):
            if Path(path).stem == "10":
                raise XPostError(
                    "x_long_video_requires_premium",
                    "premium required",
                    422,
                )
            return {"duration": 30.0, "width": 720, "height": 1280}

        accepted, failures = _preflight_candidates(
            config,
            [
                candidate(10, 400),
                candidate(11, 300),
                candidate(12, 200),
                candidate(13, 100),
            ],
            accounts,
            1784772000,
            downloader,
            prober,
        )

        self.assertEqual(
            [item["material_id"] for item in accepted],
            ["11", "12", "13"],
        )
        self.assertEqual(len(failures), 1)
        self.assertEqual(
            failures[0]["error_code"],
            "x_long_video_requires_premium",
        )

    def test_rejected_media_is_deleted_before_next_candidate_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(test_config(), work_dir=temporary)
            accounts = [
                {
                    "id": account_id,
                    "username": "account%s" % account_id,
                    "x_user_id": "200%s" % account_id,
                    "display_name": "Account %s" % account_id,
                }
                for account_id in (2, 3, 4)
            ]
            candidates = [
                candidate(material_id, 100 - material_id)
                for material_id in (10, 11, 12, 13, 14)
            ]
            peak_files = [0]

            def downloader(_url, destination, _hosts, max_bytes, timeout):
                self.assertGreater(max_bytes, 0)
                self.assertGreater(timeout, 0)
                root = Path(destination).parent
                self.assertEqual(list(root.glob("*.mp4")), [])
                Path(destination).write_bytes(b"video")
                peak_files[0] = max(peak_files[0], len(list(root.glob("*.mp4"))))
                return {"size": 5, "sha256": "a" * 64, "media_type": "video/mp4"}

            def prober(path, max_bytes, timeout, max_duration_seconds=140.0):
                if Path(path).stem in {"10", "11"}:
                    raise XPostError("invalid_media_codec", "rejected", 422)
                return {"duration": 30.0, "width": 720, "height": 1280}

            accepted, failures = _preflight_candidates(
                config,
                candidates,
                accounts,
                1784772000,
                downloader,
                prober,
            )
        self.assertEqual([item["material_id"] for item in accepted], ["12", "13", "14"])
        self.assertEqual(len(failures), 2)
        self.assertEqual(peak_files[0], 1)

    def test_repairable_media_is_rebuilt_once_then_revalidated_from_cos(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                test_config(),
                work_dir=temporary,
                repair_url=(
                    "http://127.0.0.1:18799/internal/x-post-media-repair"
                ),
                repair_token="repair-secret",
            )
            accounts = [
                {
                    "id": account_id,
                    "username": "account%s" % account_id,
                    "x_user_id": "200%s" % account_id,
                    "display_name": "Account %s" % account_id,
                }
                for account_id in (2, 3, 4)
            ]
            candidates = [
                candidate(material_id, 100 - material_id)
                for material_id in (10, 11, 12)
            ]
            drama_resource_id = "21c09b915223a0695f0a4cf85386cabd"
            candidates[0]["material_id"] = drama_resource_id
            candidates[0]["material_url"] = (
                "https://media.example.test/%s.mp4" % drama_resource_id
            )
            events = []

            class Repair:
                def repair(self, payload):
                    events.append(("repair", dict(payload)))
                    return repair_response(payload["job_key"])

            def downloader(url, destination, _hosts, max_bytes, timeout):
                self.assertFalse(Path(destination).exists())
                if url.startswith("https://cos.example.test/"):
                    Path(destination).write_bytes(b"repaired")
                    events.append(("download_repaired", url))
                    return {
                        "size": 8,
                        "sha256": "b" * 64,
                        "media_type": "video/mp4",
                    }
                Path(destination).write_bytes(b"video")
                events.append(("download_source", url))
                return {
                    "size": 5,
                    "sha256": "a" * 64,
                    "media_type": "video/mp4",
                }

            def prober(path, max_bytes, timeout, max_duration_seconds=140.0):
                if (
                    Path(path).read_bytes() == b"video"
                    and Path(path).stem == drama_resource_id
                ):
                    raise XPostError(
                        "invalid_media_duration", "over 140 seconds", 422
                    )
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

            accepted, failures = _preflight_candidates(
                config,
                candidates,
                accounts,
                1784772000,
                downloader,
                prober,
                Repair(),
            )

        self.assertEqual(
            [item["material_id"] for item in accepted],
            [drama_resource_id, "11", "12"],
        )
        self.assertEqual(failures, [])
        repaired = accepted[0]
        self.assertEqual(
            repaired["original_material_url"],
            "https://media.example.test/%s.mp4" % drama_resource_id,
        )
        self.assertEqual(repaired["material_url"], "https://cos.example.test/repaired.mp4")
        self.assertEqual(
            repaired["media_repair_trigger_code"],
            "invalid_media_duration",
        )
        self.assertEqual(
            repaired["media_repair_profile"], DEFAULT_REPAIR_PROFILE
        )
        self.assertEqual(repaired["media_repair_source_sha256"], "a" * 64)
        self.assertEqual(repaired["preflight_sha256"], "b" * 64)
        self.assertEqual(repaired["preflight_size"], 8)
        repair_payload = next(item[1] for item in events if item[0] == "repair")
        self.assertEqual(
            set(repair_payload),
            {
                "job_key",
                "material_id",
                "pool_item_id",
                "source_url",
                "source_sha256",
                "source_size",
                "trigger_code",
                "profile",
                "duration_policy",
            },
        )
        self.assertEqual(repair_payload["pool_item_id"], 10)
        self.assertEqual(repair_payload["material_id"], drama_resource_id)
        self.assertEqual(repair_payload["source_size"], 5)
        self.assertEqual(repair_payload["duration_policy"], "standard")
        self.assertTrue(
            re.fullmatch(
                r"[a-f0-9]{64}", repaired["media_repair_job_key"]
            )
        )

    def test_material_pool_premium_over_600_source_keeps_original_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(test_config(account_ids=(13,)), work_dir=temporary)
            account = {
                "id": 13,
                "username": "premium13",
                "x_user_id": "2013",
                "display_name": "Premium 13",
                "subscription_type": "premium",
                "long_video_eligible": True,
            }
            item = candidate(5286820, 0)
            item["pool_item_id"] = 1
            item.pop("manual_item_id", None)

            class Repair:
                def repair(self, _payload):
                    raise AssertionError(
                        "a valid 763-second Premium source must not be repaired"
                    )

            def downloader(url, destination, _hosts, max_bytes, timeout):
                Path(destination).write_bytes(b"video")
                return {
                    "size": 5,
                    "sha256": "a" * 64,
                    "media_type": "video/mp4",
                }

            def prober(path, max_bytes, timeout, max_duration_seconds=140.0):
                self.assertEqual(max_duration_seconds, 14400.0)
                self.assertEqual(Path(path).read_bytes(), b"video")
                return {
                    "codec": "h264",
                    "pixel_format": "yuv420p",
                    "audio_codec": "aac",
                    "duration": 763.938005,
                    "width": 720,
                    "height": 1280,
                    "frame_rate": 30.0,
                    "size": Path(path).stat().st_size,
                }

            accepted, failures = _preflight_candidates(
                config,
                [item],
                [account],
                1784772000,
                downloader,
                prober,
                Repair(),
            )

        self.assertEqual(failures, [])
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["material_id"], "5286820")
        self.assertEqual(accepted[0]["preflight_duration"], 763.938005)
        self.assertEqual(accepted[0]["material_url"], item["material_url"])

    def test_premium_repaired_download_keeps_account_duration_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                test_config(account_ids=(13,)),
                work_dir=temporary,
                repair_url=(
                    "http://127.0.0.1:18799/internal/x-post-media-repair"
                ),
                repair_token="repair-secret",
            )
            account = {
                "id": 13,
                "username": "premium13",
                "x_user_id": "2013",
                "display_name": "Premium 13",
                "subscription_type": "premium",
                "long_video_eligible": True,
            }
            item = candidate(137, 0)

            class Repair:
                def repair(self, payload):
                    self_payload = repair_response(payload["job_key"])
                    self_payload["probe"]["duration"] = 148.138
                    return self_payload

            def downloader(url, destination, _hosts, max_bytes, timeout):
                body = (
                    b"repaired"
                    if url.startswith("https://cos.example.test/")
                    else b"video"
                )
                Path(destination).write_bytes(body)
                return {
                    "size": len(body),
                    "sha256": "b" * 64 if body == b"repaired" else "a" * 64,
                    "media_type": "video/mp4",
                }

            probe_limits = []

            def prober(path, max_bytes, timeout, max_duration_seconds=140.0):
                probe_limits.append(max_duration_seconds)
                if Path(path).read_bytes() == b"video":
                    raise XPostError(
                        "invalid_media_dimensions",
                        "source dimensions require repair",
                        422,
                    )
                return {
                    "codec": "h264",
                    "pixel_format": "yuv420p",
                    "audio_codec": "aac",
                    "duration": 148.138,
                    "width": 720,
                    "height": 1280,
                    "frame_rate": 30.0,
                    "size": Path(path).stat().st_size,
                }

            accepted, failures = _preflight_candidates(
                config,
                [item],
                [account],
                1784772000,
                downloader,
                prober,
                Repair(),
            )

        self.assertEqual(failures, [])
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["preflight_duration"], 148.138)
        self.assertEqual(probe_limits, [14400.0, 14400.0])

    def test_repair_quota_is_shared_across_all_repairable_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                test_config(),
                work_dir=temporary,
                max_repairs_per_run=1,
            )
            accounts = [
                {
                    "id": account_id,
                    "username": "account%s" % account_id,
                    "x_user_id": "200%s" % account_id,
                    "display_name": "Account %s" % account_id,
                }
                for account_id in (2, 3, 4)
            ]
            candidates = [
                candidate(material_id, 100 - material_id)
                for material_id in (10, 11, 12, 13, 14)
            ]

            class Repair:
                def __init__(self):
                    self.calls = []

                def repair(self, payload):
                    self.calls.append(dict(payload))
                    return repair_response(payload["job_key"])

            repair = Repair()

            def downloader(url, destination, _hosts, max_bytes, timeout):
                repaired = url.startswith("https://cos.example.test/")
                content = b"repaired" if repaired else b"video"
                Path(destination).write_bytes(content)
                return {
                    "size": len(content),
                    "sha256": ("b" if repaired else "a") * 64,
                    "media_type": "video/mp4",
                }

            def prober(path, max_bytes, timeout, max_duration_seconds=140.0):
                material_id = Path(path).stem
                if Path(path).read_bytes() == b"video" and material_id in {"10", "11"}:
                    raise XPostError(
                        "invalid_media_dimensions", "bad dimensions", 422
                    )
                if Path(path).read_bytes() == b"video" and material_id == "12":
                    raise XPostError(
                        "invalid_media_duration", "bad duration", 422
                    )
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

            accepted, failures = _preflight_candidates(
                config,
                candidates,
                accounts,
                1784772000,
                downloader,
                prober,
                repair,
            )

        self.assertEqual([item["material_id"] for item in accepted], ["10", "13", "14"])
        self.assertEqual(len(repair.calls), 1)
        self.assertEqual(repair.calls[0]["material_id"], "10")
        self.assertEqual(
            [item["error_code"] for item in failures],
            ["invalid_media_dimensions", "invalid_media_duration"],
        )

    def test_repaired_media_fingerprint_mismatch_replenishes_without_recursion(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(test_config(), work_dir=temporary)
            accounts = [
                {
                    "id": account_id,
                    "username": "account%s" % account_id,
                    "x_user_id": "200%s" % account_id,
                    "display_name": "Account %s" % account_id,
                }
                for account_id in (2, 3, 4)
            ]
            candidates = [
                candidate(material_id, 100 - material_id)
                for material_id in (10, 11, 12, 13)
            ]

            class Repair:
                def __init__(self):
                    self.calls = 0

                def repair(self, payload):
                    self.calls += 1
                    return repair_response(payload["job_key"])

            repair = Repair()

            def downloader(url, destination, _hosts, max_bytes, timeout):
                repaired = url.startswith("https://cos.example.test/")
                Path(destination).write_bytes(
                    b"repaired" if repaired else b"video"
                )
                return {
                    "size": 8 if repaired else 5,
                    "sha256": ("c" if repaired else "a") * 64,
                    "media_type": "video/mp4",
                }

            def prober(path, max_bytes, timeout, max_duration_seconds=140.0):
                if Path(path).stem == "10" and Path(path).read_bytes() == b"video":
                    raise XPostError(
                        "invalid_media_codec", "bad codec", 422
                    )
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

            accepted, failures = _preflight_candidates(
                config,
                candidates,
                accounts,
                1784772000,
                downloader,
                prober,
                repair,
            )

        self.assertEqual([item["material_id"] for item in accepted], ["11", "12", "13"])
        self.assertEqual(repair.calls, 1)
        self.assertEqual(
            failures[0]["error_code"],
            "x_post_media_repair_fingerprint_mismatch",
        )

    def test_truncated_candidate_download_replenishes_from_next_material(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(test_config(), work_dir=temporary)
            accounts = [
                {
                    "id": account_id,
                    "username": "account%s" % account_id,
                    "x_user_id": "200%s" % account_id,
                    "display_name": "Account %s" % account_id,
                }
                for account_id in (2, 3, 4)
            ]
            candidates = [
                candidate(material_id, 100 - material_id)
                for material_id in (10, 11, 12, 13)
            ]

            def downloader(_url, destination, _hosts, max_bytes, timeout):
                if Path(destination).stem == "10":
                    raise http.client.IncompleteRead(b"partial", 100)
                Path(destination).write_bytes(b"video")
                return {"size": 5, "sha256": "a" * 64, "media_type": "video/mp4"}

            def prober(_path, max_bytes, timeout, max_duration_seconds=140.0):
                return {"duration": 30.0, "width": 720, "height": 1280}

            accepted, failures = _preflight_candidates(
                config,
                candidates,
                accounts,
                1784772000,
                downloader,
                prober,
            )
        self.assertEqual([item["material_id"] for item in accepted], ["11", "12", "13"])
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["error_code"], "media_preflight_failed")

    def test_start_date_gate_runs_before_account_verification(self):
        sidecar = FakeSidecar()
        result = execute_daily_run(
            test_config(start_date="2026-07-24"),
            sidecar=sidecar,
            now=datetime(2026, 7, 23, 20, 0, tzinfo=timezone(timedelta(hours=8))),
        )
        self.assertEqual(result["status"], "skipped_before_start_date")
        self.assertEqual(sidecar.events, [])

    def test_storage_must_pass_before_account_or_plan(self):
        class BrokenStorageSidecar(FakeSidecar):
            def preflight_storage(self, path):
                self.events.append(("storage", path))
                raise SidecarError(
                    "x_post_storage_unavailable", "data disk is unavailable", 503
                )

        sidecar = BrokenStorageSidecar()
        with self.assertRaises(SidecarError):
            execute_daily_run(
                test_config(),
                sidecar=sidecar,
                now=datetime(2026, 7, 23, 10, 0, tzinfo=timezone(timedelta(hours=8))),
            )
        self.assertEqual(sidecar.events[0][0], "plan_query")
        self.assertEqual(sidecar.events[1][0], "storage")
        self.assertFalse(any(event[0] in {"verify", "plan", "publish"} for event in sidecar.events))

    def test_all_accounts_must_verify_before_plan(self):
        class BrokenSidecar(FakeSidecar):
            def verify_account(self, account_id):
                if account_id == 3:
                    raise SidecarError("x_account_not_publishable", "disabled", 409)
                return super().verify_account(account_id)

        sidecar = BrokenSidecar()
        with self.assertRaises(SidecarError):
            execute_daily_run(
                test_config(),
                sidecar=sidecar,
                now=datetime(2026, 7, 23, 10, 0, tzinfo=timezone(timedelta(hours=8))),
            )
        self.assertFalse(any(event[0] == "plan" for event in sidecar.events))
        failure = next(event for event in sidecar.events if event[0] == "failure")
        self.assertEqual(failure[4], "x_account_not_publishable")
        self.assertEqual(failure[6], 3)

    def test_preflight_shortage_never_creates_plan(self):
        sidecar = FakeSidecar()

        def loader(*_args, **_kwargs):
            return [candidate(10, 400), candidate(11, 300), candidate(12, 200)], []

        def bad_download(*_args, **_kwargs):
            raise XPostError("invalid_media_codec", "bad codec", 422)

        with self.assertRaises(DailyRunError):
            execute_daily_run(
                test_config(),
                sidecar=sidecar,
                connection_factory=lambda _config: FakeConnection([]),
                pool_candidate_loader=loader,
                downloader=bad_download,
                now=datetime(2026, 7, 23, 10, 0, tzinfo=timezone(timedelta(hours=8))),
            )
        self.assertFalse(any(event[0] == "plan" for event in sidecar.events))
        failure = next(event for event in sidecar.events if event[0] == "failure")
        self.assertEqual(
            failure[4], "x_post_daily_candidate_preflight_shortage"
        )
        self.assertEqual(failure[6], 3)

    def test_nine_account_preflight_failure_audit_records_dynamic_expected_count(self):
        sidecar = FakeSidecar()
        config = test_config(account_ids=tuple(range(2, 11)))

        def loader(*_args, **_kwargs):
            return [
                candidate(material_id, 1000 - rank)
                for rank, material_id in enumerate(range(10, 19), 1)
            ], []

        def bad_download(*_args, **_kwargs):
            raise XPostError("invalid_media_codec", "bad codec", 422)

        with self.assertRaises(DailyRunError):
            execute_daily_run(
                config,
                sidecar=sidecar,
                connection_factory=lambda _config: FakeConnection([]),
                pool_candidate_loader=loader,
                downloader=bad_download,
                now=datetime(
                    2026,
                    7,
                    23,
                    10,
                    0,
                    tzinfo=timezone(timedelta(hours=8)),
                ),
            )
        self.assertFalse(any(event[0] == "plan" for event in sidecar.events))
        failure = next(event for event in sidecar.events if event[0] == "failure")
        self.assertEqual(
            failure[4],
            "x_post_daily_candidate_preflight_shortage",
        )
        self.assertEqual(failure[6], 9)

    def test_plan_error_is_not_misrecorded_as_preflight_failure(self):
        class PlanFailureSidecar(FakeSidecar):
            def create_plan(self, path, payload):
                self.events.append(("plan", path))
                raise SidecarError(
                    "x_sidecar_unreachable",
                    "plan response was lost",
                    503,
                    unknown_outcome=True,
                )

        sidecar = PlanFailureSidecar()
        with self.assertRaises(SidecarError):
            self._run(sidecar)
        self.assertTrue(any(event[0] == "plan" for event in sidecar.events))
        self.assertFalse(any(event[0] == "failure" for event in sidecar.events))

    def test_known_plan_rollback_is_recorded_as_failed_preflight(self):
        class KnownPlanFailureSidecar(FakeSidecar):
            def create_plan(self, path, payload):
                self.events.append(("plan", path))
                raise SidecarError(
                    "x_post_material_already_used",
                    "transaction rolled back",
                    409,
                    unknown_outcome=False,
                )

        sidecar = KnownPlanFailureSidecar()
        with self.assertRaises(SidecarError):
            self._run(sidecar)
        failure = next(event for event in sidecar.events if event[0] == "failure")
        self.assertEqual(failure[4], "x_post_material_already_used")
        self.assertEqual(failure[6], 3)


if __name__ == "__main__":
    unittest.main()
