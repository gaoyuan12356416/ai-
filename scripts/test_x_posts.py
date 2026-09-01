#!/usr/bin/env python3
"""Offline unit tests for the X Post canary module."""

import contextlib
import hashlib
import http.client
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts import service


class ScriptedHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, headers=None, body=None, timeout=None, stream=False, max_response_bytes=None):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "body": body,
                "timeout": timeout,
                "stream": stream,
                "max_response_bytes": max_response_bytes,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected HTTP request: %s %s" % (method, url))
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def response(status, payload=None, headers=None, body=None):
    if body is None:
        body = json.dumps(payload or {}, separators=(",", ":")).encode("utf-8")
    return service.HttpResponse(status, headers=headers or {"content-type": "application/json"}, body=body)


def candidate(account_id=2, username="ShortsDramhx"):
    return {
        "account_id": account_id,
        "account_username": username,
        "source_date": "2026-07-22",
        "material_id": "88001",
        "content_id": "32001",
        "material_url": "https://media.example.com/source/video.mp4",
        "material_name": "best_video",
        "material_language": "en",
        "drama_name": "The Contract Bride",
        "tag": "romance",
        "description": "A contract marriage becomes an unexpected romance.",
        "page_name": "Short Drama",
        "page_id": "2076951197916037120",
        "preflight_duration": 45.25,
    }


def valid_probe_payload(width=720, height=1280, fps="30000/1001", duration="45.25"):
    return {
        "duration": duration,
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "width": width,
                "height": height,
                "avg_frame_rate": fps,
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": duration},
    }


def deferred_drama_queue(store, account_id=2, username="ShortsDramhx", relay=False):
    """Build a real frozen deferred episode; shared with Sidecar integration tests."""
    config = store.save_schedule_config(
        "drama",
        {
            "enabled": True, "timezone": "Asia/Shanghai", "account_ids": [account_id],
            "publish_times": ["09:00"], "version": 1,
        },
        actor={"user_id": "admin-1", "name": "Admin"},
        eligible_account_ids=[account_id],
        now=datetime(2026, 8, 12, 8, 0, tzinfo=service.BEIJING_TZ),
    )
    pool = store.add_drama_pool_items(
        ["32001"],
        [{
            "content_id": "32001", "drama_name": "The Contract Bride",
            "description": "A contract marriage becomes an unexpected romance.",
            "language": "en", "labels": "romance", "name_tag": "#Contract_Bride",
            "free_episode_count": 2,
        }],
        actor={"user_id": "admin-1", "name": "Admin"},
    )["items"][0]
    item = candidate(account_id, username)
    item.update({
        "source_date": "2026-08-11", "source_type": "drama", "material_id": "a" * 32,
        "candidate_rank": 1, "drama_pool_item_id": pool["id"],
        "drama_pool_created_at": pool["created_at"], "episode_number": 1,
        "episode_key": "32001:1", "drama_replay_generation": 1,
        "name_tag": "#Contract_Bride", "account_drama_language": "en",
        "media_validation_mode": "deferred", "preflight_sha256": "", "preflight_size": 0,
        "preflight_duration": 141.0 if relay else 0.0,
        "delivery_mode": "premium_relay_repost" if relay else "direct",
        "relay_account_id": account_id + 10 if relay else 0,
        "relay_account_username": "premium10" if relay else "",
        "facebook_violation_count": 0, "tiktok_violation_count": 0,
        "twitter_violation_count": 0, "resource_audit_count": 0, "dangerous_tag_count": 0,
    })
    return store.create_schedule_plan(
        "drama", "2026-08-12", "09:00", config["version"], [item],
        premium_account_ids=[] if relay else [account_id],
        premium_relay_accounts=[{"id": account_id + 10, "username": "premium10", "drama_language": "en"}]
        if relay else [],
    )["queues"][0]


class XPostsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "accounts.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def enqueue(self, **changes):
        payload = candidate()
        payload.update(changes)
        return service.XPostStore(self.db_path).enqueue(payload)

    def test_storage_is_additive_idempotent_and_store_self_initializes(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
            conn.execute("INSERT INTO sentinel(value) VALUES('keep')")
            conn.commit()
        service.ensure_storage(self.db_path)
        service.ensure_storage(self.db_path)
        service.XPostStore(self.db_path)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("x_post_queue", tables)
            self.assertIn("x_post_publish_log", tables)
            self.assertEqual(conn.execute("SELECT value FROM sentinel").fetchone()[0], "keep")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(x_post_queue)")}
            self.assertIn("account_username", columns)
            self.assertIn("preflight_duration", columns)
            self.assertIn("media_validation_mode", columns)
            self.assertNotIn("source_queue_id", columns)

    def test_log_task_source_labels_and_filters_all_three_origins(self):
        store = service.XPostStore(self.db_path)
        store.enqueue(candidate(2, "MaterialAccount"))
        deferred_drama_queue(store, 3, "DramaAccount")

        auto_run = store.create_auto_template_run(
            "99001",
            4,
            "task-source-auto-1",
            "template-1",
            1,
            "{{drama_name}}\n{{desc}}",
            {"user_id": "admin-1", "name": "Admin"},
        )
        auto_candidate = candidate(4, "AutoAccount")
        auto_candidate.update(
            {
                "material_id": "99001",
                "source_date": auto_run["source_date"],
                "preflight_sha256": service.hashlib.sha256(b"video").hexdigest(),
                "preflight_size": 5,
                "facebook_violation_count": 0,
                "tiktok_violation_count": 0,
                "twitter_violation_count": 0,
                "resource_audit_count": 0,
                "dangerous_tag_count": 0,
            }
        )
        store.create_manual_plan(
            auto_run["id"],
            [auto_candidate],
            "auto_template",
        )

        all_items = store.query_logs({"page_size": 10})["items"]
        self.assertEqual(
            {item["task_source"] for item in all_items},
            {"drama_pool", "material_pool", "auto_publish"},
        )
        for task_source in ("drama_pool", "material_pool", "auto_publish"):
            with self.subTest(task_source=task_source):
                result = store.query_logs({"task_source": task_source})
                self.assertEqual(result["pagination"]["total"], 1)
                self.assertEqual(result["items"][0]["task_source"], task_source)

        with self.assertRaises(service.XPostError) as caught:
            store.query_logs({"task_source": "other"})
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_daily_plan_accepts_nine_candidates_with_dynamic_expected_count(self):
        candidates = []
        for rank, account_id in enumerate(range(2, 11), 1):
            item = candidate(account_id, "DailyAccount%02d" % account_id)
            material_id = str(89000 + rank)
            item.update(
                {
                    "material_id": material_id,
                    "content_id": "content-" + material_id,
                    "candidate_rank": rank,
                    "spend": 100 - rank,
                    "preflight_sha256": (
                        "%064x" % int(material_id)
                    )[-64:],
                    "preflight_size": 5,
                    "compliance_counts": {
                        "facebook_violation_count": 0,
                        "tiktok_violation_count": 0,
                        "twitter_violation_count": 0,
                        "resource_audit_count": 0,
                        "dangerous_tag_count": 0,
                    },
                }
            )
            candidates.append(item)

        plan = service.XPostStore(self.db_path).create_daily_plan(
            "2026-07-23",
            "2026-07-22",
            candidates,
        )

        self.assertEqual(plan["expected_count"], 9)
        self.assertEqual(plan["queued_count"], 9)
        self.assertEqual(len(plan["queues"]), 9)
        self.assertEqual(
            [queue["account_id"] for queue in plan["queues"]],
            list(range(2, 11)),
        )

    def test_daily_plan_batch_size_accepts_one_and_fifty_but_rejects_fifty_one(self):
        def planned(account_id, rank, material_base, source_date):
            item = candidate(account_id, "DailyBound%02d" % account_id)
            material_id = str(material_base + rank)
            item.update(
                {
                    "source_date": source_date,
                    "material_id": material_id,
                    "content_id": "content-" + material_id,
                    "candidate_rank": rank,
                    "spend": 1000 - rank,
                    "preflight_sha256": ("%064x" % int(material_id))[-64:],
                    "preflight_size": 5,
                    "compliance_counts": {
                        "facebook_violation_count": 0,
                        "tiktok_violation_count": 0,
                        "twitter_violation_count": 0,
                        "resource_audit_count": 0,
                        "dangerous_tag_count": 0,
                    },
                }
            )
            return item

        store = service.XPostStore(self.db_path)
        one = store.create_daily_plan(
            "2026-07-23",
            "2026-07-22",
            [planned(1, 1, 90000, "2026-07-22")],
        )
        fifty_candidates = [
            planned(account_id, rank, 91000, "2026-07-23")
            for rank, account_id in enumerate(range(1, 51), 1)
        ]
        fifty = store.create_daily_plan(
            "2026-07-24",
            "2026-07-23",
            fifty_candidates,
        )

        self.assertEqual(one["expected_count"], 1)
        self.assertEqual(len(one["queues"]), 1)
        self.assertEqual(fifty["expected_count"], 50)
        self.assertEqual(len(fifty["queues"]), 50)
        with self.assertRaises(service.XPostError) as rejected:
            store.create_daily_plan(
                "2026-07-25",
                "2026-07-24",
                [{} for _index in range(51)],
            )
        self.assertEqual(
            rejected.exception.code,
            "x_post_daily_candidate_shortage",
        )

    def test_enqueue_is_idempotent_and_conflicts_fail_closed(self):
        store = service.XPostStore(self.db_path)
        first = store.enqueue(candidate())
        second = store.enqueue(candidate())
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"])
        changed = candidate()
        changed["description"] = "different"
        with self.assertRaises(service.XPostError) as caught:
            store.enqueue(changed)
        self.assertEqual(caught.exception.code, "x_post_idempotency_conflict")

    def test_w2a_builder_has_exact_order_and_local_queue_id(self):
        url = service.build_w2a_url(
            {
                "username": "ShortsDramhx",
                "timestamp": 1784736000,
                "material_language": "en",
                "drama_name": "The Contract Bride",
                "tag": "romance",
                "log_id": 9,
                "page_name": "Short Drama",
                "page_id": "2076951197916037120",
                "material_name": "best_video",
                "material_id": "88001",
                "queue_id": 7,
                "content_id": "32001",
                "video_duration_seconds": 140.0,
            }
        )
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "www.dramawavew2a.com")
        self.assertEqual(parsed.path, "/ads/101/2116/view")
        pairs = urllib.parse.parse_qsl(parsed.query)
        self.assertEqual(
            [key for key, _value in pairs],
            ["c", "af_adset", "af_adset_id", "af_ad", "af_ad_id", "af_channel", "af_c_id", "af_dp"],
        )
        values = dict(pairs)
        self.assertEqual(
            values["c"],
            "yingliang_post_CLV_VL_ShortsDramhx*1784736000noneen*The Contract Bride*romance*9",
        )
        self.assertEqual(values["af_ad"], "best_video_contentid[32001]")
        self.assertEqual(values["af_c_id"], "7")
        self.assertEqual(values["af_dp"], "32001")
        self.assertEqual(values["af_channel"], "short")
        with self.assertRaises(service.XPostError):
            service.build_w2a_url({"username": "bad name"})

    def test_w2a_channel_uses_exact_published_video_duration_boundary(self):
        params = {
            "username": "ShortsDramhx",
            "timestamp": 1784736000,
            "material_language": "en",
            "drama_name": "The Contract Bride",
            "tag": "romance",
            "log_id": 9,
            "page_name": "Short Drama",
            "page_id": "2076951197916037120",
            "material_name": "best_video",
            "material_id": "88001",
            "queue_id": 7,
            "content_id": "32001",
        }
        for duration, expected_channel in ((140.0, "short"), (140.000001, "long")):
            with self.subTest(duration=duration):
                url = service.build_w2a_url(
                    {**params, "video_duration_seconds": duration}
                )
                query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
                self.assertEqual(query["af_channel"], expected_channel)
                self.assertEqual(service._validate_w2a_url(url), url)

        historical_url = service.build_w2a_url(
            {**params, "video_duration_seconds": 140.0}
        ).replace("af_channel=short", "af_channel=AIpost")
        self.assertEqual(service._validate_w2a_url(historical_url), historical_url)
        with self.assertRaises(service.XPostError):
            service._validate_w2a_url(
                historical_url.replace("af_channel=AIpost", "af_channel=other")
            )

    def test_w2a_image_zero_duration_uses_short_but_tiny_video_stays_invalid(self):
        params = {
            "username": "ShortsDramhx",
            "timestamp": 1784736000,
            "material_language": "en",
            "drama_name": "Drama",
            "tag": "safe",
            "log_id": 1,
            "page_name": "Page",
            "page_id": "123",
            "material_name": "image.jpg",
            "material_id": "9",
            "queue_id": 2,
            "content_id": "3",
            "video_duration_seconds": 0,
        }
        image_url = service.build_w2a_url(params)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(image_url).query))
        self.assertEqual(query["af_channel"], "short")
        params["video_duration_seconds"] = 0.1
        with self.assertRaises(service.XPostError):
            service.build_w2a_url(params)

    def test_short_link_base_is_fixed_to_g2flow_host(self):
        self.assertEqual(
            service._build_short_url("https://gy.g2flow.com/s2l", 7),
            "https://gy.g2flow.com/s2l/7.html",
        )
        for value in (
            "https://example.com/s2l",
            "https://ai.yingliangads.com/s2l",
            "https://ai.yingliangads.com/other",
            "https://ai.yingliangads.com:443/s2l",
        ):
            with self.subTest(value=value):
                with self.assertRaises(service.XPostError):
                    service._build_short_url(value, 7)

    def test_real_candidate_material_name_with_brackets_builds_exact_af_ad(self):
        material_name = "【推荐】2M_TROTLQ_(14_15超爽卡点)_EN_精剪_zhouliwei_3_episode[15].mp4"
        url = service.build_w2a_url(
            {
                "username": "ShortsDramhx", "timestamp": 1784736000, "material_language": "en",
                "drama_name": "The Rise of the Lycan Queen", "tag": "Fantasy", "log_id": 17,
                "page_name": "Short Drama", "page_id": "2076951197916037120",
                "material_name": material_name, "material_id": "5221348", "queue_id": 4,
                "content_id": "3CRScaBEY0",
                "video_duration_seconds": 140.0,
            }
        )
        values = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertEqual(values["af_ad"], material_name + "_contentid[3CRScaBEY0]")
        self.assertIn("%5B15%5D", url)
        self.assertIn("contentid%5B3CRScaBEY0%5D", url)

    def test_success_status_with_errors_array_fails_closed(self):
        client = ScriptedHttpClient([response(201, {"data": {"id": "190003"}, "errors": [{"detail": "partial failure"}]})])
        with self.assertRaises(service.XPostError) as caught:
            service.XApiClient(http_client=client).create_post("token", "url\ndesc", "media1")
        self.assertTrue(caught.exception.unknown_outcome)

    def test_malformed_create_post_5xx_is_always_unknown(self):
        for status in (500, 503):
            for body in (b"<html>oops</html>", b"{"):
                with self.subTest(status=status, body=body):
                    client = ScriptedHttpClient(
                        [
                            service.HttpResponse(
                                status,
                                {"content-type": "text/html"},
                                body=body,
                            )
                        ]
                    )
                    with self.assertRaises(service.XPostError) as caught:
                        service.XApiClient(http_client=client).create_post(
                            "token",
                            "url\ndesc",
                            "media1",
                        )
                    self.assertTrue(caught.exception.unknown_outcome)

    def test_x_401_has_stable_token_invalid_login_message(self):
        client = ScriptedHttpClient(
            [response(401, {"title": "Unauthorized", "detail": "expired"})]
        )
        with self.assertRaises(service.XPostError) as caught:
            service.XApiClient(http_client=client).create_post(
                "expired-token", "url\ndesc", "media1"
            )
        self.assertEqual(caught.exception.code, "x_token_invalid")
        self.assertEqual(str(caught.exception), "Token失效，请重新登陆")
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse(caught.exception.unknown_outcome)

    def test_x_rate_limit_has_stable_429_code_and_is_not_retried(self):
        for upstream in (
            response(429, {"title": "Too Many Requests"}),
            service.HttpResponse(429, {"content-type": "text/plain"}, body=b"rate limited"),
            response(
                403,
                {
                    "type": "https://api.x.com/2/problems/usage-capped",
                    "title": "wording is deliberately ignored",
                },
            ),
            response(
                201,
                {
                    "errors": [
                        {
                            "type": "https://api.x.com/2/problems/rate-limit-exceeded",
                        }
                    ]
                },
            ),
            response(201, {"errors": [{"code": 88}]}),
        ):
            client = ScriptedHttpClient([upstream])
            with self.assertRaises(service.XPostError) as caught:
                service.XApiClient(http_client=client).create_post(
                    "token", "url\ndesc", "media1"
                )
            self.assertEqual(caught.exception.code, "x_post_rate_limited")
            self.assertEqual(caught.exception.status, 429)
            self.assertFalse(caught.exception.unknown_outcome)
            self.assertEqual(len(client.requests), 1)

    def test_post_text_omits_url_and_conservatively_truncates(self):
        short = "https://gy.g2flow.com/s2l/12.html"
        text = service.build_post_text(short, "My Drama", "剧" * 300)
        self.assertTrue(text.startswith("🎬 My Drama\n"))
        self.assertNotIn(short, text)
        self.assertNotIn("Watch now", text)
        self.assertTrue(
            text.endswith(
                "…\n\n#shortdrama #shortfilms #tvdrama #aidrama #dramawave"
            )
        )
        self.assertLessEqual(service._tweet_text_weight(text), 280)

    def test_material_post_template_matches_the_requested_copy(self):
        short = "https://gy.g2flow.com/s2l/12.html"
        self.assertEqual(
            service.build_post_text(
                short,
                "My Stepmom and Her Secret Besties",
                "A complete drama description.",
            ),
            "🎬 My Stepmom and Her Secret Besties\n"
            "A complete drama description.\n\n"
            "#shortdrama #shortfilms #tvdrama #aidrama #dramawave",
        )

    def test_url_macro_renders_the_frozen_tracked_short_link(self):
        short = "https://gy.g2flow.com/s2l/12.html"
        rendered = service.build_post_text(
            short,
            "My Drama",
            "A complete drama description.",
            "{{drama_name}}\n{{desc}}\n{{url}}",
        )
        self.assertEqual(
            rendered,
            "My Drama\nA complete drama description.\n" + short,
        )

    def test_post_template_rejects_unknown_missing_or_repeated_macros(self):
        invalid_templates = (
            "{{drama_name}} {{unknown}} {{desc}}",
            "{{drama_name}} {{URL}} {{desc}}",
            "{{drama_name}} only",
            "{{drama_name}} {{desc}} {{desc}}",
        )
        for template in invalid_templates:
            with self.subTest(template=template):
                with self.assertRaises(service.XPostError) as caught:
                    service.build_post_text(
                        "https://gy.g2flow.com/s2l/12.html",
                        "My Drama",
                        "A complete drama description.",
                        template,
                    )
                self.assertEqual(caught.exception.code, "invalid_post_template")

    def test_short_redirect_is_atomic_immutable_and_public_readable(self):
        long_url = service.build_w2a_url(
            {
                "username": "ShortsDramhx", "timestamp": 1784736000, "material_language": "en",
                "drama_name": "Drama", "tag": "safe", "log_id": 1, "page_name": "Page",
                "page_id": "123", "material_name": "asset", "material_id": "9", "queue_id": 2,
                "content_id": "3", "video_duration_seconds": 140.0,
            }
        )
        short_root = self.root / "s2l"
        path = service.write_short_redirect(short_root, 1, long_url)
        self.assertEqual(path, short_root / "1.html")
        body = path.read_text(encoding="utf-8")
        self.assertIn("location.replace", body)
        self.assertIn("no-referrer", body)
        self.assertEqual(service.write_short_redirect(short_root, 1, long_url), path)
        other = long_url.replace("af_ad_id=9", "af_ad_id=10")
        with self.assertRaises(service.XPostError) as caught:
            service.write_short_redirect(short_root, 1, other)
        self.assertEqual(caught.exception.code, "short_link_conflict")
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual(short_root.stat().st_mode & 0o777, 0o755)

    def test_short_redirect_persists_mode_file_and_directory_in_order(self):
        long_url = service.build_w2a_url(
            {
                "username": "ShortsDramhx",
                "timestamp": 1784736000,
                "material_language": "en",
                "drama_name": "Drama",
                "tag": "safe",
                "log_id": 21,
                "page_name": "Page",
                "page_id": "123",
                "material_name": "asset",
                "material_id": "9",
                "queue_id": 2,
                "content_id": "3",
                "video_duration_seconds": 140.0,
            }
        )
        tracker = mock.Mock()
        with mock.patch.object(
            service.os, "fchmod", wraps=os.fchmod
        ) as fchmod_mock, mock.patch.object(
            service.os, "fsync", wraps=os.fsync
        ) as fsync_mock, mock.patch.object(
            service.os, "replace", wraps=os.replace
        ) as replace_mock, mock.patch.object(
            service, "_fsync_directory"
        ) as directory_sync_mock:
            tracker.attach_mock(fchmod_mock, "fchmod")
            tracker.attach_mock(fsync_mock, "fsync")
            tracker.attach_mock(replace_mock, "replace")
            tracker.attach_mock(directory_sync_mock, "directory_sync")
            path = service.write_short_redirect(self.root / "durable-s2l", 21, long_url)
        names = [call[0] for call in tracker.mock_calls]
        self.assertLess(names.index("fchmod"), names.index("fsync"))
        self.assertLess(names.index("fsync"), names.index("replace"))
        self.assertLess(names.index("replace"), names.index("directory_sync"))

        tracker.reset_mock()
        with mock.patch.object(
            service.os, "fchmod", wraps=os.fchmod
        ) as fchmod_mock, mock.patch.object(
            service.os, "fsync", wraps=os.fsync
        ) as fsync_mock, mock.patch.object(
            service, "_fsync_directory"
        ) as directory_sync_mock:
            tracker.attach_mock(fchmod_mock, "fchmod")
            tracker.attach_mock(fsync_mock, "fsync")
            tracker.attach_mock(directory_sync_mock, "directory_sync")
            self.assertEqual(
                service.write_short_redirect(self.root / "durable-s2l", 21, long_url),
                path,
            )
        names = [call[0] for call in tracker.mock_calls]
        self.assertLess(names.index("fchmod"), names.index("fsync"))
        self.assertLess(names.index("fsync"), names.index("directory_sync"))

    def test_short_redirect_directory_sync_failure_is_known_before_create_post(self):
        queue = self.enqueue(material_id="88040")
        with mock.patch.object(
            service,
            "_fsync_directory",
            side_effect=OSError("simulated directory sync failure"),
        ):
            with self.assertRaises(service.XPostError) as caught:
                service.publish_canary(
                    db_path=self.db_path,
                    queue_id=queue["id"],
                    account={"id": 2, "username": "ShortsDramhx"},
                    access_token="secret-token",
                    public_root=self.root / "public" / "s2l",
                    short_base_url="https://gy.g2flow.com/s2l",
                    allowed_media_hosts=["media.example.com"],
                    http_client=ScriptedHttpClient([]),
                    timeout=5,
                )
        self.assertEqual(caught.exception.code, "short_link_write_failed")
        self.assertFalse(caught.exception.unknown_outcome)
        log = service.XPostStore(self.db_path).get_log(1)
        self.assertEqual(log["status"], "failed")
        self.assertEqual(log["error_code"], "short_link_write_failed")

    def test_download_requires_https_allowlist_video_and_size_cap(self):
        client = ScriptedHttpClient(
            [service.HttpResponse(200, {"content-type": "video/mp4", "content-length": "5"}, body=b"video")]
        )
        target = self.root / "media-work" / "video.mp4"
        result = service.download_media(
            "https://media.example.com/a.mp4", target, ["media.example.com"], max_bytes=10,
            timeout=5, http_client=client,
        )
        self.assertEqual(result["size"], 5)
        self.assertEqual(target.read_bytes(), b"video")
        self.assertEqual(len(client.requests), 1)
        with self.assertRaises(service.XPostError) as caught:
            service.download_media("http://media.example.com/a.mp4", target, ["media.example.com"], http_client=client)
        self.assertEqual(caught.exception.code, "invalid_media_url")
        with self.assertRaises(service.XPostError) as caught:
            service.download_media("https://evil.example/a.mp4", target, ["media.example.com"], http_client=client)
        self.assertEqual(caught.exception.code, "media_host_not_allowed")
        self.assertEqual(len(client.requests), 1)

    def test_download_retries_clean_eof_using_fresh_full_get_and_atomic_target(self):
        target = self.root / "media-work" / "retry.mp4"
        target.parent.mkdir()
        target.write_bytes(b"existing-video")
        temporary_names = []
        observed_targets = []

        class RecordingResponse(service.HttpResponse):
            def iter_bytes(self, chunk_size=64 * 1024):
                temporary_names.extend(
                    path.name for path in target.parent.glob(".retry.mp4.*.part")
                )
                observed_targets.append(target.read_bytes())
                yield from super().iter_bytes(chunk_size)

        client = ScriptedHttpClient([
            RecordingResponse(200, {"content-type": "video/mp4", "content-length": "5"}, body=b"bad"),
            RecordingResponse(200, {"content-type": "video/mp4", "content-length": "5"}, body=b"video"),
        ])
        result = service.download_media(
            "https://media.example.com/retry.mp4", target, ["media.example.com"],
            max_bytes=32, timeout=5, http_client=client,
        )
        self.assertEqual(target.read_bytes(), b"video")
        self.assertEqual(result["sha256"], hashlib.sha256(b"video").hexdigest())
        self.assertEqual(result["size"], 5)
        self.assertEqual(observed_targets, [b"existing-video", b"existing-video"])
        self.assertEqual(len(temporary_names), 2)
        self.assertEqual(len(set(temporary_names)), 2)
        self.assertEqual(list(target.parent.glob("*.part")), [])
        self.assertEqual(len(client.requests), 2)
        for request in client.requests:
            self.assertEqual((request["method"], request["url"]), ("GET", "https://media.example.com/retry.mp4"))
            self.assertNotIn("range", {name.lower() for name in request["headers"]})
            self.assertEqual(request["max_response_bytes"], 32)

    def test_download_clean_eof_exhausts_three_attempts_without_replacing_target(self):
        for index, partial in enumerate((b"", b"vi")):
            with self.subTest(partial=partial):
                target = self.root / ("exhausted-%s.mp4" % index)
                target.write_bytes(b"existing-video")
                client = ScriptedHttpClient([
                    service.HttpResponse(200, {"content-type": "video/mp4", "content-length": "5"}, body=partial)
                    for _ in range(4)
                ])
                with self.assertRaises(service.XPostError) as caught:
                    service.download_media(
                        "https://media.example.com/truncated.mp4", target, ["media.example.com"],
                        max_bytes=32, timeout=5, http_client=client,
                    )
                self.assertEqual(caught.exception.code, "media_download_incomplete")
                self.assertEqual(caught.exception.status, 502)
                self.assertFalse(caught.exception.unknown_outcome)
                self.assertIn("已尝试3次", str(caught.exception))
                self.assertEqual(len(client.requests), 3)
                self.assertEqual(len(client.responses), 1)
                self.assertEqual(target.read_bytes(), b"existing-video")
                self.assertEqual(list(target.parent.glob("*.part")), [])

    def test_download_rejects_surplus_bytes_without_retry_or_target_replacement(self):
        target = self.root / "surplus.mp4"
        target.write_bytes(b"existing-video")
        client = ScriptedHttpClient([
            service.HttpResponse(200, {"content-type": "video/mp4", "content-length": "5"}, body=b"video-extra"),
            service.HttpResponse(200, {"content-type": "video/mp4", "content-length": "5"}, body=b"video"),
        ])
        with self.assertRaises(service.XPostError) as caught:
            service.download_media(
                "https://media.example.com/surplus.mp4", target, ["media.example.com"],
                max_bytes=32, timeout=5, http_client=client,
            )
        self.assertEqual(caught.exception.code, "media_download_length_mismatch")
        self.assertFalse(caught.exception.unknown_outcome)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(target.read_bytes(), b"existing-video")
        self.assertEqual(list(target.parent.glob("*.part")), [])

    def test_download_truncation_retry_does_not_retry_other_failures_or_relax_gates(self):
        failures = [
            (service.HttpResponse(302, {"content-type": "video/mp4", "location": "https://other.example/a.mp4"}, body=b"video"), "media_download_failed"),
            (service.HttpResponse(200, {"content-type": "text/html"}, body=b"html"), "invalid_media_type"),
            (service.HttpResponse(200, {"content-type": "video/mp4", "content-length": "33"}, body=b"video"), "media_too_large"),
            (service.HttpResponse(200, {"content-type": "video/mp4", "content-length": "bad"}, body=b"video"), "invalid_media_response"),
            (TimeoutError("network timeout"), "media_download_failed"),
        ]
        for index, (failure, code) in enumerate(failures):
            with self.subTest(code=code, index=index):
                target = self.root / ("other-failure-%s.mp4" % index)
                target.write_bytes(b"existing-video")
                client = ScriptedHttpClient([
                    service.HttpResponse(200, {"content-type": "video/mp4", "content-length": "5"}, body=b"vi"),
                    failure,
                    service.HttpResponse(200, {"content-type": "video/mp4", "content-length": "5"}, body=b"video"),
                ])
                with self.assertRaises(service.XPostError) as caught:
                    service.download_media(
                        "https://media.example.com/failure.mp4", target, ["media.example.com"],
                        max_bytes=32, timeout=5, http_client=client,
                    )
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(client.requests), 2)
                self.assertEqual(target.read_bytes(), b"existing-video")
                self.assertEqual(list(target.parent.glob("*.part")), [])

    def test_download_accepts_image_and_enforces_x_image_size_cap(self):
        target = self.root / "media-work" / "image.bin"
        client = ScriptedHttpClient(
            [
                service.HttpResponse(
                    200,
                    {"content-type": "image/jpeg", "content-length": "4"},
                    body=b"jpeg",
                )
            ]
        )
        result = service.download_media(
            "https://media.example.com/a.jpg",
            target,
            ["media.example.com"],
            max_bytes=service.DEFAULT_MAX_MEDIA_BYTES,
            timeout=5,
            http_client=client,
        )
        self.assertEqual(result["media_kind"], "image")
        self.assertEqual(result["media_type"], "image/jpeg")
        self.assertEqual(target.read_bytes(), b"jpeg")

        oversized = ScriptedHttpClient(
            [
                service.HttpResponse(
                    200,
                    {
                        "content-type": "image/png",
                        "content-length": str(service.DEFAULT_MAX_IMAGE_BYTES + 1),
                    },
                    body=b"x",
                )
            ]
        )
        with self.assertRaises(service.XPostError) as caught:
            service.download_media(
                "https://media.example.com/a.png",
                target,
                ["media.example.com"],
                max_bytes=service.DEFAULT_MAX_MEDIA_BYTES,
                timeout=5,
                http_client=oversized,
            )
        self.assertEqual(caught.exception.code, "media_too_large")

    def test_truncated_media_stream_is_a_known_download_failure(self):
        class TruncatedStream:
            def __init__(self):
                self.read_count = 0

            def read(self, _size):
                self.read_count += 1
                if self.read_count == 1:
                    return b"partial"
                raise http.client.IncompleteRead(b"", 100)

            def close(self):
                return None

        client = ScriptedHttpClient(
            [
                service.HttpResponse(
                    200,
                    {"content-type": "video/mp4"},
                    stream=TruncatedStream(),
                )
            ]
        )
        target = self.root / "media-work" / "truncated.mp4"
        with self.assertRaises(service.XPostError) as caught:
            service.download_media(
                "https://media.example.com/truncated.mp4",
                target,
                ["media.example.com"],
                max_bytes=1024,
                timeout=5,
                http_client=client,
            )
        self.assertEqual(caught.exception.code, "media_download_failed")
        self.assertFalse(caught.exception.unknown_outcome)
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob("*.part")), [])
        self.assertEqual(len(client.requests), 1)

    def test_ffprobe_accepts_720x1280_and_rejects_bad_codec_or_fps(self):
        media = self.root / "video.mp4"
        media.write_bytes(b"video")

        def runner_for(payload):
            return lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

        fake_ffprobe = str((self.root / "ffprobe.exe").resolve())
        with mock.patch.dict(os.environ, {"X_POST_FFPROBE_BIN": fake_ffprobe}):
            captured = {}

            def capturing_runner(command, **_kwargs):
                captured["command"] = command
                captured["kwargs"] = dict(_kwargs)
                return SimpleNamespace(returncode=0, stdout=json.dumps(valid_probe_payload()), stderr="")

            result = service.probe_media(media, runner=capturing_runner)
        self.assertEqual((result["width"], result["height"]), (720, 1280))
        self.assertEqual(captured["command"][0], fake_ffprobe)
        self.assertIs(captured["kwargs"]["stdin"], service.subprocess.DEVNULL)
        self.assertTrue(captured["kwargs"]["close_fds"])
        self.assertEqual(
            captured["kwargs"]["env"],
            {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        )
        self.assertNotIn("X_CLIENT_SECRET", captured["kwargs"]["env"])
        self.assertNotIn("X_POST_DAILY_MYSQL_PASSWORD", captured["kwargs"]["env"])
        bad_codec = valid_probe_payload()
        bad_codec["streams"][0]["codec_name"] = "hevc"
        with self.assertRaises(service.XPostError) as caught:
            service.probe_media(media, runner=runner_for(bad_codec))
        self.assertEqual(caught.exception.code, "invalid_media_codec")
        with self.assertRaises(service.XPostError) as caught:
            service.probe_media(media, runner=runner_for(valid_probe_payload(fps="61/1")))
        self.assertEqual(caught.exception.code, "invalid_media_frame_rate")
        with self.assertRaises(service.XPostError) as caught:
            service.probe_media(media, runner=runner_for(valid_probe_payload(duration="140.1")))
        self.assertEqual(caught.exception.code, "x_long_video_requires_premium")
        premium_result = service.probe_media(
            media,
            runner=runner_for(valid_probe_payload(duration="763.938005")),
            max_duration_seconds=service.PREMIUM_MAX_DURATION_SECONDS,
        )
        self.assertEqual(premium_result["duration"], 763.938005)
        self.assertEqual(service.PREMIUM_MAX_DURATION_SECONDS, 14400.0)
        with self.assertRaises(service.XPostError) as caught:
            service.probe_media(
                media,
                runner=runner_for(valid_probe_payload(duration="14400.1")),
                max_duration_seconds=service.PREMIUM_MAX_DURATION_SECONDS,
            )
        self.assertEqual(caught.exception.code, "invalid_media_duration")
        with self.assertRaises(service.XPostError) as caught:
            service.probe_media(media, runner=runner_for(valid_probe_payload(width=1080, height=1920)))
        self.assertEqual(caught.exception.code, "invalid_media_dimensions")
        non_lc = valid_probe_payload()
        non_lc["streams"][1]["profile"] = "HE-AAC"
        with self.assertRaises(service.XPostError) as caught:
            service.probe_media(media, runner=runner_for(non_lc))
        self.assertEqual(caught.exception.code, "invalid_media_codec")

    def test_image_probe_accepts_claimed_format_and_rejects_mismatch(self):
        media = self.root / "image.jpg"
        media.write_bytes(b"jpeg")

        def runner_for(codec):
            payload = {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": codec,
                        "width": 1200,
                        "height": 1600,
                    }
                ]
            }
            return lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )

        result = service.probe_image(
            media,
            "image/jpeg",
            runner=runner_for("mjpeg"),
        )
        self.assertEqual(result["media_category"], "tweet_image")
        self.assertEqual((result["width"], result["height"]), (1200, 1600))
        self.assertEqual(service.image_media_category("image/gif"), "tweet_gif")
        with self.assertRaises(service.XPostError) as caught:
            service.probe_image(
                media,
                "image/jpeg",
                runner=runner_for("png"),
            )
        self.assertEqual(caught.exception.code, "invalid_image")

    def test_storage_preflight_requires_mount_and_atomic_write(self):
        mount = self.root / "mnt"
        storage = mount / "x-post-automation"
        public = storage / "s2l"
        public.mkdir(parents=True)
        (storage / "media-work").mkdir()
        with mock.patch.object(service.os.path, "ismount", return_value=True):
            result = service.preflight_post_storage(
                public,
                mount_root=mount,
                storage_root=storage,
                minimum_free_bytes=1,
            )
        self.assertEqual(
            result,
            {"ready": True, "mounted": True, "atomic_write": True},
        )
        self.assertEqual(list(public.iterdir()), [])
        self.assertEqual(list((storage / "media-work").iterdir()), [])
        (storage / "media-work").rmdir()
        with mock.patch.object(service.os.path, "ismount", return_value=True):
            with self.assertRaises(service.XPostError) as missing_media:
                service.preflight_post_storage(
                    public,
                    mount_root=mount,
                    storage_root=storage,
                    minimum_free_bytes=1,
                )
        self.assertEqual(missing_media.exception.code, "x_post_storage_unavailable")
        with mock.patch.object(service.os.path, "ismount", return_value=False):
            with self.assertRaises(service.XPostError) as caught:
                service.preflight_post_storage(
                    public,
                    mount_root=mount,
                    storage_root=storage,
                    minimum_free_bytes=1,
                )
        self.assertEqual(caught.exception.code, "x_post_storage_unavailable")

    def test_durable_short_link_never_creates_missing_mount_layout(self):
        mount = self.root / "missing-mount"
        storage = mount / "x-post-automation"
        public = storage / "s2l"
        long_url = service.build_w2a_url(
            {
                "username": "ShortsDramhx",
                "timestamp": 1784736000,
                "material_language": "en",
                "drama_name": "Drama",
                "tag": "safe",
                "log_id": 1,
                "page_name": "Page",
                "page_id": "123",
                "material_name": "asset",
                "material_id": "9",
                "queue_id": 2,
                "content_id": "3",
                "video_duration_seconds": 140.0,
            }
        )
        with mock.patch.object(service.os.path, "ismount", return_value=False):
            with self.assertRaises(service.XPostError) as caught:
                service.write_short_redirect(
                    public,
                    1,
                    long_url,
                    durable_storage={
                        "mount_root": mount,
                        "storage_root": storage,
                    },
                )
        self.assertEqual(caught.exception.code, "x_post_storage_unavailable")
        self.assertFalse(mount.exists())

    def test_x_v2_chunked_upload_status_and_create_post(self):
        media = self.root / "video.mp4"
        media.write_bytes(b"abcde")
        client = ScriptedHttpClient(
            [
                response(200, {"data": {"id": "media123"}}),
                response(200, {"data": {"expires_at": 1}}),
                response(200, {"data": {"expires_at": 1}}),
                response(200, {"data": {"id": "media123", "processing_info": {"state": "pending", "check_after_secs": 0}}}),
                response(200, {"data": {"processing_info": {"state": "succeeded", "progress_percent": 100}}}),
                response(201, {"data": {"id": "190001", "text": "ok"}}),
            ]
        )
        api = service.XApiClient(http_client=client, sleeper=lambda _seconds: None, chunk_bytes=4)
        uploaded = api.upload_media("secret-token", media)
        created = api.create_post("secret-token", "url\ndesc", uploaded["media_id"])
        self.assertEqual(created["post_id"], "190001")
        self.assertTrue(client.requests[0]["url"].endswith("/2/media/upload/initialize"))
        append_requests = client.requests[1:3]
        self.assertEqual(len(append_requests), 2)
        self.assertIn(b'name="segment_index"\r\n\r\n0', append_requests[0]["body"])
        self.assertIn(b'name="segment_index"\r\n\r\n1', append_requests[1]["body"])
        self.assertIn("/2/media/upload?media_id=media123&command=STATUS", client.requests[4]["url"])
        post_payload = json.loads(client.requests[5]["body"].decode("utf-8"))
        self.assertEqual(post_payload["media"]["media_ids"], ["media123"])

    def test_x_v2_image_upload_uses_tweet_image_category(self):
        media = self.root / "image.png"
        media.write_bytes(b"image")
        client = ScriptedHttpClient(
            [
                response(200, {"data": {"id": "image123"}}),
                response(200, {"data": {"expires_at": 1}}),
                response(200, {"data": {"id": "image123"}}),
            ]
        )
        api = service.XApiClient(http_client=client, sleeper=lambda _seconds: None)
        uploaded = api.upload_media(
            "secret-token",
            media,
            media_type="image/png",
            media_category="tweet_image",
        )
        self.assertEqual(uploaded["media_id"], "image123")
        initialize = json.loads(client.requests[0]["body"].decode("utf-8"))
        self.assertEqual(initialize["media_type"], "image/png")
        self.assertEqual(initialize["media_category"], "tweet_image")

    def test_create_post_transport_failure_is_unknown_outcome(self):
        client = ScriptedHttpClient([OSError("connection reset access_token=do-not-log")])
        api = service.XApiClient(http_client=client)
        with self.assertRaises(service.XPostError) as caught:
            api.create_post("secret-token", "url\ndesc", "media1")
        self.assertTrue(caught.exception.unknown_outcome)
        self.assertNotIn("do-not-log", str(caught.exception))

    def test_truncated_create_post_response_is_unknown_and_run_needs_review(self):
        store = service.XPostStore(self.db_path)
        candidates = []
        for rank, (account_id, username, material_id) in enumerate(
            (
                (2, "ShortsDramhx", "88901"),
                (3, "NextShortsy1", "88902"),
                (4, "GrapeShortlzod", "88903"),
            ),
            1,
        ):
            item = candidate(account_id, username)
            item.update(
                {
                    "material_id": material_id,
                    "content_id": "content-" + material_id,
                    "candidate_rank": rank,
                    "spend": 100 - rank,
                    "preflight_sha256": service.hashlib.sha256(b"video").hexdigest(),
                    "preflight_size": 5,
                    "compliance_counts": {
                        "facebook_violation_count": 0,
                        "tiktok_violation_count": 0,
                        "twitter_violation_count": 0,
                        "resource_audit_count": 0,
                        "dangerous_tag_count": 0,
                    },
                }
            )
            candidates.append(item)
        plan = store.create_daily_plan("2026-07-23", "2026-07-22", candidates)
        client = ScriptedHttpClient(
            [
                service.HttpResponse(
                    200,
                    {"content-type": "video/mp4", "content-length": "5"},
                    body=b"video",
                ),
                response(200, {"data": {"id": "media-truncated"}}),
                response(200, {"data": {"expires_at": 1}}),
                response(200, {"data": {"id": "media-truncated"}}),
                http.client.IncompleteRead(b'{"data":{"id":"190', 20),
            ]
        )
        with mock.patch.object(service, "probe_media", return_value=valid_probe_payload()):
            with self.assertRaises(service.XPostError) as caught:
                service.publish_canary(
                    db_path=self.db_path,
                    queue_id=plan["queues"][0]["id"],
                    account={"id": 2, "username": "ShortsDramhx"},
                    access_token="secret-token",
                    public_root=self.root / "public" / "s2l",
                    short_base_url="https://gy.g2flow.com/s2l",
                    allowed_media_hosts=["media.example.com"],
                    http_client=client,
                    timeout=5,
                )
        self.assertTrue(caught.exception.unknown_outcome)
        log = store.query_logs(
            {"account_id": 2, "run_date": "2026-07-23", "page": 1, "page_size": 10}
        )["items"][0]
        self.assertEqual(log["status"], "failed")
        self.assertTrue(log["unknown_outcome"])
        self.assertEqual(store.get_run(plan["id"])["status"], "needs_review")

    def test_post_id_is_preserved_as_unknown_when_final_ledger_commit_fails(self):
        store = service.XPostStore(self.db_path)
        candidates = []
        for rank, (account_id, username, material_id) in enumerate(
            (
                (2, "ShortsDramhx", "88911"),
                (3, "NextShortsy1", "88912"),
                (4, "GrapeShortlzod", "88913"),
            ),
            1,
        ):
            item = candidate(account_id, username)
            item.update(
                {
                    "material_id": material_id,
                    "content_id": "content-" + material_id,
                    "candidate_rank": rank,
                    "spend": 100 - rank,
                    "preflight_sha256": service.hashlib.sha256(b"video").hexdigest(),
                    "preflight_size": 5,
                    "compliance_counts": {
                        "facebook_violation_count": 0,
                        "tiktok_violation_count": 0,
                        "twitter_violation_count": 0,
                        "resource_audit_count": 0,
                        "dangerous_tag_count": 0,
                    },
                }
            )
            candidates.append(item)
        plan = store.create_daily_plan("2026-07-23", "2026-07-22", candidates)
        client = ScriptedHttpClient(
            [
                service.HttpResponse(
                    200,
                    {"content-type": "video/mp4", "content-length": "5"},
                    body=b"video",
                ),
                response(200, {"data": {"id": "media-ledger-failure"}}),
                response(200, {"data": {"expires_at": 1}}),
                response(200, {"data": {"id": "media-ledger-failure"}}),
                response(201, {"data": {"id": "19000991", "text": "ok"}}),
            ]
        )
        with mock.patch.object(service, "probe_media", return_value=valid_probe_payload()), mock.patch.object(
            service.XPostStore,
            "mark_published",
            side_effect=service.XPostError(
                "x_post_state_conflict", "simulated final ledger failure", 409
            ),
        ):
            with self.assertRaises(service.XPostError) as caught:
                service.publish_canary(
                    db_path=self.db_path,
                    queue_id=plan["queues"][0]["id"],
                    account={"id": 2, "username": "ShortsDramhx"},
                    access_token="secret-token",
                    public_root=self.root / "public" / "s2l",
                    short_base_url="https://gy.g2flow.com/s2l",
                    allowed_media_hosts=["media.example.com"],
                    http_client=client,
                    timeout=5,
                )
        self.assertTrue(caught.exception.unknown_outcome)
        log = store.query_logs(
            {"account_id": 2, "run_date": "2026-07-23", "page": 1, "page_size": 10}
        )["items"][0]
        self.assertEqual(log["status"], "failed")
        self.assertTrue(log["unknown_outcome"])
        raw_log = store.get_log(log["log_id"])
        self.assertEqual(raw_log["x_post_id"], "19000991")
        self.assertEqual(
            raw_log["x_post_url"],
            "https://x.com/ShortsDramhx/status/19000991",
        )
        self.assertEqual(store.get_run(plan["id"])["status"], "needs_review")

    def test_publish_canary_records_log_short_link_and_is_idempotent(self):
        queue = self.enqueue()
        client = ScriptedHttpClient(
            [
                service.HttpResponse(200, {"content-type": "video/mp4", "content-length": "5"}, body=b"video"),
                response(200, {"data": {"id": "media1"}}),
                response(200, {"data": {"expires_at": 1}}),
                response(200, {"data": {"id": "media1"}}),
                response(201, {"data": {"id": "190002", "text": "ok"}}),
            ]
        )
        public_root = self.root / "public" / "s2l"
        with mock.patch.object(service, "probe_media", return_value=valid_probe_payload()):
            result = service.publish_canary(
                db_path=self.db_path, queue_id=queue["id"],
                account={"id": 2, "username": "ShortsDramhx"}, access_token="secret-token",
                public_root=public_root, short_base_url="https://gy.g2flow.com/s2l",
                allowed_media_hosts=["media.example.com"], http_client=client,
                sleeper=lambda _seconds: None, timeout=5,
            )
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["preview_url"], "https://x.com/ShortsDramhx/status/190002")
        self.assertTrue((public_root / (str(result["log_id"]) + ".html")).exists())
        self.assertTrue((public_root.parent / "media-work").is_dir())
        self.assertEqual(list((public_root.parent / "media-work").iterdir()), [])
        requests_before = len(client.requests)
        repeated = service.publish_canary(
            db_path=self.db_path, queue_id=queue["id"],
            account={"id": 2, "username": "ShortsDramhx"}, access_token="secret-token",
            public_root=public_root, short_base_url="https://gy.g2flow.com/s2l",
            allowed_media_hosts=["media.example.com"], http_client=client, timeout=5,
        )
        self.assertEqual(repeated["post_id"], "190002")
        self.assertEqual(len(client.requests), requests_before)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            dump = "\n".join(conn.iterdump())
            c_value = dict(
                urllib.parse.parse_qsl(
                    urllib.parse.urlsplit(conn.execute("SELECT long_url FROM x_post_publish_log").fetchone()[0]).query
                )
            )["c"]
            af_c_id = dict(
                urllib.parse.parse_qsl(
                    urllib.parse.urlsplit(conn.execute("SELECT long_url FROM x_post_publish_log").fetchone()[0]).query
                )
            )["af_c_id"]
            af_channel = dict(
                urllib.parse.parse_qsl(
                    urllib.parse.urlsplit(conn.execute("SELECT long_url FROM x_post_publish_log").fetchone()[0]).query
                )
            )["af_channel"]
        self.assertIn("yingliang_post_CLV_VL_ShortsDramhx*", c_value)
        self.assertEqual(af_c_id, str(queue["id"]))
        self.assertEqual(af_channel, "short")
        self.assertNotIn("secret-token", dump)

    def test_publish_image_uses_image_probe_category_and_short_attribution(self):
        queue = self.enqueue(
            material_id="88008",
            material_url="https://media.example.com/image.jpg",
            material_name="image.jpg",
            preflight_duration=0,
        )
        client = ScriptedHttpClient(
            [
                service.HttpResponse(
                    200,
                    {"content-type": "image/jpeg", "content-length": "4"},
                    body=b"jpeg",
                ),
                response(200, {"data": {"id": "image-media"}}),
                response(200, {"data": {"expires_at": 1}}),
                response(200, {"data": {"id": "image-media"}}),
                response(201, {"data": {"id": "1900028", "text": "ok"}}),
            ]
        )
        with mock.patch.object(
            service,
            "probe_image",
            return_value={
                "media_category": "tweet_image",
                "width": 1200,
                "height": 1600,
            },
        ) as image_probe, mock.patch.object(
            service,
            "probe_media",
            side_effect=AssertionError("image publish must not use video probe"),
        ):
            result = service.publish_canary(
                db_path=self.db_path,
                queue_id=queue["id"],
                account={"id": 2, "username": "ShortsDramhx"},
                access_token="secret-token",
                public_root=self.root / "image-public" / "s2l",
                short_base_url="https://gy.g2flow.com/s2l",
                allowed_media_hosts=["media.example.com"],
                http_client=client,
                sleeper=lambda _seconds: None,
                timeout=5,
            )
        self.assertEqual(result["status"], "published")
        self.assertEqual(image_probe.call_args.args[1], "image/jpeg")
        initialize = next(
            request
            for request in client.requests
            if request["url"].endswith("/2/media/upload/initialize")
        )
        initialize_payload = json.loads(initialize["body"].decode("utf-8"))
        self.assertEqual(initialize_payload["media_type"], "image/jpeg")
        self.assertEqual(initialize_payload["media_category"], "tweet_image")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            long_url = conn.execute(
                "SELECT long_url FROM x_post_publish_log WHERE queue_id=?",
                (queue["id"],),
            ).fetchone()[0]
        self.assertEqual(
            dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(long_url).query))[
                "af_channel"
            ],
            "short",
        )

    def test_premium_long_publish_uses_amplify_video_category(self):
        queue = self.enqueue(preflight_duration=763.938005)
        client = ScriptedHttpClient(
            [
                service.HttpResponse(
                    200,
                    {"content-type": "video/mp4", "content-length": "5"},
                    body=b"video",
                ),
                response(200, {"data": {"id": "long-media"}}),
                response(200, {"data": {"expires_at": 1}}),
                response(200, {"data": {"id": "long-media"}}),
                response(201, {"data": {"id": "190003", "text": "ok"}}),
            ]
        )
        with mock.patch.object(
            service,
            "probe_media",
            return_value={"duration": 763.938005},
        ) as probe:
            result = service.publish_canary(
                db_path=self.db_path,
                queue_id=queue["id"],
                account={
                    "id": 2,
                    "username": "ShortsDramhx",
                    "subscription_type": "premium",
                },
                access_token="secret-token",
                public_root=self.root / "long-public" / "s2l",
                short_base_url="https://gy.g2flow.com/s2l",
                allowed_media_hosts=["media.example.com"],
                http_client=client,
                sleeper=lambda _seconds: None,
                timeout=5,
            )
        self.assertEqual(result["status"], "published")
        self.assertEqual(
            probe.call_args.kwargs["max_duration_seconds"],
            service.PREMIUM_MAX_DURATION_SECONDS,
        )
        initialize = next(
            request
            for request in client.requests
            if request["url"].endswith("/2/media/upload/initialize")
        )
        self.assertEqual(
            json.loads(initialize["body"].decode("utf-8"))["media_category"],
            "amplify_video",
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            long_url = conn.execute(
                "SELECT long_url FROM x_post_publish_log WHERE queue_id=?",
                (queue["id"],),
            ).fetchone()[0]
        self.assertEqual(
            dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(long_url).query))[
                "af_channel"
            ],
            "long",
        )

    def test_deferred_publish_uses_final_probe_duration_for_attribution(self):
        store = service.XPostStore(self.db_path)
        config = store.save_schedule_config(
            "material",
            {
                "enabled": True,
                "timezone": "Asia/Shanghai",
                "account_ids": [2],
                "publish_times": ["10:00"],
                "version": 1,
            },
            actor={"user_id": "admin-1", "name": "Admin"},
            eligible_account_ids=[2],
        )
        pool = store.add_pool_materials(
            ["88010"],
            actor={"user_id": "admin-1", "name": "Admin"},
            validation_checks=[{"material_id": "88010", "error_code": ""}],
        )["items"][0]
        item = candidate()
        item.update(
            {
                "material_id": "88010",
                "content_id": "32010",
                "pool_item_id": pool["id"],
                "pool_created_at": pool["created_at"],
                "account_drama_language": "en",
                "media_validation_mode": "deferred",
                "preflight_sha256": "",
                "preflight_size": 0,
                # Routing metadata is intentionally stale. Deferred mode must
                # attribute from the single final probe, not this value.
                "preflight_duration": 141.0,
                "facebook_violation_count": 0,
                "tiktok_violation_count": 0,
                "twitter_violation_count": 0,
                "resource_audit_count": 0,
                "dangerous_tag_count": 0,
            }
        )
        plan = store.create_schedule_plan(
            "material",
            "2026-07-22",
            "10:00",
            config["version"],
            [item],
            premium_account_ids=[2],
        )
        queue = plan["queues"][0]
        client = ScriptedHttpClient(
            [
                service.HttpResponse(
                    200,
                    {"content-type": "video/mp4", "content-length": "5"},
                    body=b"video",
                ),
                response(200, {"data": {"id": "deferred-media"}}),
                response(200, {"data": {"expires_at": 1}}),
                response(200, {"data": {"id": "deferred-media"}}),
                response(201, {"data": {"id": "1900031", "text": "ok"}}),
            ]
        )
        with mock.patch.object(
            service, "probe_media", return_value={"duration": 45.25}
        ):
            result = service.publish_canary(
                db_path=self.db_path,
                queue_id=queue["id"],
                account={
                    "id": 2,
                    "username": "ShortsDramhx",
                    "subscription_type": "premium",
                },
                access_token="secret-token",
                public_root=self.root / "deferred-public" / "s2l",
                short_base_url="https://gy.g2flow.com/s2l",
                allowed_media_hosts=["media.example.com"],
                http_client=client,
                sleeper=lambda _seconds: None,
                timeout=5,
            )
        self.assertEqual(result["status"], "published")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            long_url = conn.execute(
                "SELECT long_url FROM x_post_publish_log WHERE queue_id=?",
                (queue["id"],),
            ).fetchone()[0]
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(long_url).query))
        self.assertEqual(query["af_channel"], "short")
        initialize = next(
            request
            for request in client.requests
            if request["url"].endswith("/2/media/upload/initialize")
        )
        self.assertEqual(
            json.loads(initialize["body"].decode("utf-8"))["media_category"],
            "tweet_video",
        )

    @staticmethod
    def _deferred_repair_result(payload, duration=45.25):
        return {
            "status": "ready", "job_key": payload["job_key"], "profile": payload["profile"],
            "reused": False, "output_url": "https://media.example.com/repaired.mp4",
            "output_sha256": hashlib.sha256(b"fixed").hexdigest(), "output_size": 5,
            "probe": {
                "codec": "h264", "pixel_format": "yuv420p", "audio_codec": "aac",
                "width": 720, "height": 1280, "frame_rate": 30.0, "duration": duration, "size": 5,
            },
        }

    def test_deferred_drama_repair_reuses_download_and_measured_duration_direct_and_relay(self):
        from features.x_posts import publish_media_repair as repair
        from scripts.x_post_daily_runner import DEFAULT_REPAIR_PROFILE

        for relay in (False, True):
            for duration in (45.25, 763.938):
                with self.subTest(relay=relay, duration=duration):
                    db_path = self.root / ("repair-%s-%s.sqlite3" % (relay, duration))
                    store = service.XPostStore(db_path)
                    queue = deferred_drama_queue(store, relay=relay)
                    log = store.reserve_log(queue["id"])
                    account = {
                        "id": queue["relay_account_id"] if relay else queue["account_id"],
                        "username": queue["relay_account_username"] if relay else queue["account_username"],
                        "subscription_type": "none" if not relay and duration <= 140 else "premium",
                    }
                    client = ScriptedHttpClient([
                        service.HttpResponse(200, {"content-type": "video/mp4"}, body=b"video"),
                        service.HttpResponse(200, {"content-type": "video/mp4"}, body=b"fixed"),
                        response(200, {"data": {"id": "repaired-media"}}),
                        response(200, {"data": {"expires_at": 1}}),
                        response(200, {"data": {"id": "repaired-media"}}),
                        response(201, {"data": {"id": "190008281", "text": "ok"}}),
                    ])
                    repairer = SimpleNamespace(repair=mock.Mock(
                        side_effect=lambda payload: self._deferred_repair_result(payload, duration),
                    ))
                    probes = [
                        valid_probe_payload(width=1920, height=1080, duration=str(duration), fps="30/1"),
                        valid_probe_payload(duration=str(duration), fps="30/1"),
                        valid_probe_payload(duration=str(duration), fps="30/1"),
                    ]
                    public_root = self.root / ("repaired-%s-%s" % (relay, duration)) / "s2l"
                    audit = io.StringIO()
                    with mock.patch.object(repair, "_repair_client_from_env", return_value=(repairer, DEFAULT_REPAIR_PROFILE)), mock.patch.object(
                        service.subprocess, "run", side_effect=[SimpleNamespace(returncode=0, stdout=json.dumps(item)) for item in probes],
                    ), contextlib.redirect_stdout(audit):
                        with repair.prepare_deferred_drama_media(
                            queue=queue, log=log, account=account, public_root=public_root,
                            allowed_media_hosts=["media.example.com"], http_client=client,
                        ) as prepared:
                            prepared_path = prepared.media["path"]
                            result = service.publish_canary(
                                db_path=db_path, queue_id=queue["id"], account=account, access_token="offline-test-token",
                                public_root=public_root, short_base_url="https://gy.g2flow.com/s2l",
                                allowed_media_hosts=["media.example.com"], http_client=client,
                                sleeper=lambda _seconds: None, prepared_media=prepared,
                            )
                    self.assertEqual(result["status"], "source_published" if relay else "published")
                    self.assertFalse(prepared_path.exists())
                    self.assertEqual([item["url"] for item in client.requests if item["method"] == "GET"], [queue["material_url"], "https://media.example.com/repaired.mp4"])
                    repairer.repair.assert_called_once()
                    self.assertEqual(repairer.repair.call_args.args[0]["duration_policy"], "standard" if account["subscription_type"] == "none" else "premium")
                    self.assertEqual(repairer.repair.call_args.args[0]["source_sha256"], hashlib.sha256(b"video").hexdigest())
                    frozen = store.get_queue(queue["id"])
                    for field in repair._QUEUE_IDENTITY_FIELDS + ("preflight_duration", "preflight_sha256", "preflight_size"):
                        self.assertEqual(frozen[field], queue[field], field)
                    final_log = store.get_log(log["id"])
                    self.assertEqual(final_log["attempt_count"], 1)
                    self.assertFalse(final_log["unknown_outcome"])
                    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(final_log["long_url"]).query))
                    self.assertEqual(query["af_channel"], "long" if duration > 140 else "short")
                    initialize = next(item for item in client.requests if item["url"].endswith("/2/media/upload/initialize"))
                    self.assertEqual(json.loads(initialize["body"])["media_category"], "amplify_video" if duration > 140 else "tweet_video")
                    event = json.loads(audit.getvalue())
                    self.assertEqual(event["queue_id"], queue["id"])
                    self.assertEqual(event["duration"], duration)
                    self.assertNotIn("url", audit.getvalue())
                    self.assertNotIn("offline-test-token", audit.getvalue())

    def test_deferred_drama_healthy_media_does_not_call_repair_or_download_twice(self):
        from features.x_posts import publish_media_repair as repair

        store = service.XPostStore(self.db_path)
        queue = deferred_drama_queue(store)
        log = store.reserve_log(queue["id"])
        account = {"id": 2, "username": "ShortsDramhx", "subscription_type": "none"}
        client = ScriptedHttpClient([
            service.HttpResponse(200, {"content-type": "video/mp4"}, body=b"video"),
            response(200, {"data": {"id": "original-media"}}),
            response(200, {"data": {"expires_at": 1}}),
            response(200, {"data": {"id": "original-media"}}),
            response(201, {"data": {"id": "190008282"}}),
        ])
        with mock.patch.object(repair, "_repair_client_from_env", side_effect=AssertionError("healthy media must not repair")), mock.patch.object(
            service, "probe_media", return_value={"duration": 45.25},
        ) as probe:
            with repair.prepare_deferred_drama_media(
                queue=queue, log=log, account=account, public_root=self.root / "s2l",
                allowed_media_hosts=["media.example.com"], http_client=client,
            ) as prepared:
                result = service.publish_canary(
                    db_path=self.db_path, queue_id=queue["id"], account=account, access_token="offline-test-token",
                    public_root=self.root / "s2l", short_base_url="https://gy.g2flow.com/s2l",
                    allowed_media_hosts=["media.example.com"], http_client=client,
                    prepared_media=prepared, sleeper=lambda _seconds: None,
                )
        self.assertEqual(result["status"], "published")
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(sum(item["method"] == "GET" for item in client.requests), 1)

    def test_deferred_repair_rejects_gpu_errors_and_mismatched_evidence_before_x(self):
        from features.x_posts import publish_media_repair as repair
        from scripts.x_post_daily_runner import DEFAULT_REPAIR_PROFILE, MediaRepairError

        cases = [
            ({"profile": "wrong"}, "x_post_media_repair_invalid_response"),
            ({"job_key": "0" * 64}, "x_post_media_repair_invalid_response"),
            ({"duration_policy": "standard"}, "x_post_media_repair_invalid_response"),
            ({"output_sha256": "0" * 64}, "x_post_media_repair_fingerprint_mismatch"),
            ({"output_size": 6}, "x_post_media_repair_fingerprint_mismatch"),
            ({"probe": {**self._deferred_repair_result({"job_key": "", "profile": ""})["probe"], "width": 640}}, "x_post_media_repair_probe_mismatch"),
            (MediaRepairError("x_post_media_repair_unreachable", "private token=do-not-expose"), "x_post_media_repair_unreachable"),
        ]
        for index, (change, expected_code) in enumerate(cases):
            with self.subTest(code=expected_code, change=index):
                store = service.XPostStore(self.root / ("repair-failure-%s.sqlite3" % index))
                queue = deferred_drama_queue(store)
                log = store.reserve_log(queue["id"])
                account = {"id": 2, "username": "ShortsDramhx", "subscription_type": "premium"}
                client = ScriptedHttpClient([
                    service.HttpResponse(200, {"content-type": "video/mp4"}, body=b"video"),
                    service.HttpResponse(200, {"content-type": "video/mp4"}, body=b"fixed"),
                ])
                def rejected(payload):
                    if isinstance(change, Exception):
                        raise change
                    return {**self._deferred_repair_result(payload), **change}
                repairer = SimpleNamespace(repair=mock.Mock(side_effect=rejected))
                with mock.patch.object(repair, "_repair_client_from_env", return_value=(repairer, DEFAULT_REPAIR_PROFILE)), mock.patch.object(
                    service, "probe_media", side_effect=[service.XPostError("invalid_media_dimensions", "invalid dimensions", 422), self._deferred_repair_result({"job_key": "", "profile": ""})["probe"]],
                ), self.assertRaises(service.XPostError) as caught:
                    with repair.prepare_deferred_drama_media(
                        queue=queue, log=log, account=account, public_root=self.root / ("failure-%s" % index) / "s2l",
                        allowed_media_hosts=["media.example.com"], http_client=client,
                    ):
                        self.fail("invalid repair must not reach publication")
                self.assertEqual(caught.exception.code, expected_code)
                self.assertNotIn("do-not-expose", str(caught.exception))
                self.assertEqual(store.get_log(log["id"])["attempt_count"], 0)
                self.assertFalse(store.get_log(log["id"])["unknown_outcome"])
                self.assertTrue(all(item["method"] == "GET" for item in client.requests))
                repairer.repair.assert_called_once()

    def test_deferred_repair_disabled_or_nonrepairable_errors_never_call_gpu(self):
        from features.x_posts import publish_media_repair as repair

        for index, code in enumerate(("invalid_media_dimensions", "invalid_media_frame_rate", "x_long_video_requires_premium")):
            with self.subTest(code=code):
                store = service.XPostStore(self.root / ("repair-disabled-%s.sqlite3" % index))
                queue = deferred_drama_queue(store)
                log = store.reserve_log(queue["id"])
                client = ScriptedHttpClient([service.HttpResponse(200, {"content-type": "video/mp4"}, body=b"video")])
                with mock.patch.object(repair, "_repair_client_from_env", return_value=(None, "")) as factory, mock.patch.object(
                    service, "probe_media", side_effect=service.XPostError(code, "source validation failed", 422),
                ), self.assertRaises(service.XPostError) as caught:
                    with repair.prepare_deferred_drama_media(
                        queue=queue, log=log, account={"id": 2, "username": "ShortsDramhx", "subscription_type": "none"},
                        public_root=self.root / ("disabled-%s" % index) / "s2l",
                        allowed_media_hosts=["media.example.com"], http_client=client,
                    ):
                        self.fail("unrepaired source must not publish")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(factory.call_count, 1 if code == "invalid_media_dimensions" else 0)
                self.assertEqual(len(client.requests), 1)
                self.assertEqual(store.get_log(log["id"])["attempt_count"], 0)

    def test_deferred_repair_output_is_not_repaired_recursively(self):
        from features.x_posts import publish_media_repair as repair
        from scripts.x_post_daily_runner import DEFAULT_REPAIR_PROFILE

        store = service.XPostStore(self.db_path)
        queue = deferred_drama_queue(store)
        log = store.reserve_log(queue["id"])
        client = ScriptedHttpClient([
            service.HttpResponse(200, {"content-type": "video/mp4"}, body=b"video"),
            service.HttpResponse(200, {"content-type": "video/mp4"}, body=b"fixed"),
        ])
        repairer = SimpleNamespace(repair=mock.Mock(side_effect=self._deferred_repair_result))
        with mock.patch.object(repair, "_repair_client_from_env", return_value=(repairer, DEFAULT_REPAIR_PROFILE)), mock.patch.object(
            service, "probe_media", side_effect=service.XPostError("invalid_media_dimensions", "still invalid", 422),
        ), self.assertRaises(service.XPostError) as caught:
            with repair.prepare_deferred_drama_media(
                queue=queue, log=log, account={"id": 2, "username": "ShortsDramhx", "subscription_type": "premium"},
                public_root=self.root / "s2l", allowed_media_hosts=["media.example.com"], http_client=client,
            ):
                self.fail("invalid repaired output must not publish")
        self.assertEqual(caught.exception.code, "invalid_media_dimensions")
        repairer.repair.assert_called_once()
        self.assertEqual(len(client.requests), 2)
        self.assertTrue(all(item["method"] == "GET" for item in client.requests))
        self.assertEqual(store.get_log(log["id"])["attempt_count"], 0)

    def test_deferred_prepared_media_rechecks_fingerprint_and_current_entitlement(self):
        from features.x_posts import publish_media_repair as repair

        for change_bytes in (False, True):
            with self.subTest(change_bytes=change_bytes):
                db_path = self.root / ("prepared-recheck-%s.sqlite3" % change_bytes)
                store = service.XPostStore(db_path)
                queue = deferred_drama_queue(store)
                log = store.reserve_log(queue["id"])
                account = {"id": 2, "username": "ShortsDramhx", "subscription_type": "premium"}
                client = ScriptedHttpClient([service.HttpResponse(200, {"content-type": "video/mp4"}, body=b"video")])
                with mock.patch.object(service.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(valid_probe_payload(duration="763.938")),
                )), mock.patch.object(repair, "_repair_client_from_env", side_effect=AssertionError("valid source must not repair")):
                    with repair.prepare_deferred_drama_media(
                        queue=queue, log=log, account=account, public_root=self.root / ("recheck-%s" % change_bytes) / "s2l",
                        allowed_media_hosts=["media.example.com"], http_client=client,
                    ) as prepared:
                        if change_bytes:
                            prepared.media["path"].write_bytes(b"other")
                        with self.assertRaises(service.XPostError) as caught:
                            service.publish_canary(
                                db_path=db_path, queue_id=queue["id"], account={**account, "subscription_type": "none"},
                                access_token="offline-test-token", public_root=self.root / ("recheck-%s" % change_bytes) / "s2l",
                                short_base_url="https://gy.g2flow.com/s2l", allowed_media_hosts=["media.example.com"],
                                http_client=client, prepared_media=prepared,
                            )
                self.assertEqual(caught.exception.code, "media_preflight_changed" if change_bytes else "x_long_video_requires_premium")
                final_log = store.get_log(log["id"])
                self.assertEqual(final_log["status"], "failed")
                self.assertEqual(final_log["attempt_count"], 0)
                self.assertFalse(final_log["unknown_outcome"])
                self.assertEqual(len(client.requests), 1)

    def test_deferred_preparation_refuses_any_attempt_or_unknown_and_material_is_unchanged(self):
        from features.x_posts import publish_media_repair as repair

        store = service.XPostStore(self.db_path)
        queue = deferred_drama_queue(store)
        log = store.reserve_log(queue["id"])
        account = {"id": 2, "username": "ShortsDramhx", "subscription_type": "premium"}
        with mock.patch.object(service, "download_media", side_effect=AssertionError("must not download")), mock.patch.object(
            repair, "_repair_client_from_env", side_effect=AssertionError("must not repair"),
        ):
            for changed in ({"status": "published"}, {"unknown_outcome": True}, {"attempt_count": 1}):
                with self.subTest(changed=changed), self.assertRaises(service.XPostError):
                    with repair.prepare_deferred_drama_media(
                        queue=queue, log={**log, **changed}, account=account,
                        public_root=self.root / "s2l", allowed_media_hosts=["media.example.com"],
                    ):
                        self.fail("attempted log must be fenced")
            for changed in ({"source_type": "material"}, {"media_validation_mode": "preflight"}):
                with self.subTest(unchanged_path=changed):
                    with repair.prepare_deferred_drama_media(
                        queue={**queue, **changed}, log=log, account=account,
                        public_root=self.root / "s2l", allowed_media_hosts=["media.example.com"],
                    ) as prepared:
                        self.assertIsNone(prepared)

    def test_deferred_repair_config_is_disabled_by_default_and_fails_closed(self):
        from features.x_posts import publish_media_repair as repair

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(repair._repair_client_from_env(512 * 1024 * 1024), (None, ""))
        base = {
            "X_POST_DEFERRED_DRAMA_REPAIR_ENABLED": "true",
            "X_POST_DEFERRED_DRAMA_REPAIR_URL": "http://127.0.0.1:18820/internal/x-post-media-repair",
            "X_POST_DEFERRED_DRAMA_REPAIR_TOKEN": "private-repair-token",
        }
        for change in (
            {"X_POST_DEFERRED_DRAMA_REPAIR_URL": ""},
            {"X_POST_DEFERRED_DRAMA_REPAIR_URL": "http://external.example.com:18820/internal/x-post-media-repair"},
            {"X_POST_DEFERRED_DRAMA_REPAIR_TOKEN": ""},
            {"X_POST_DEFERRED_DRAMA_REPAIR_PROFILE": "old-profile"},
            {"X_POST_DEFERRED_DRAMA_REPAIR_TIMEOUT": "4000"},
            {"X_INTERNAL_TOKEN": "private-repair-token"},
        ):
            with self.subTest(change=tuple(change)), mock.patch.dict(os.environ, {**base, **change}, clear=True), self.assertRaises(service.XPostError) as caught:
                repair._repair_client_from_env(512 * 1024 * 1024)
            self.assertEqual(caught.exception.code, "x_post_media_repair_config_invalid")
            self.assertNotIn("private-repair-token", str(caught.exception))

    def test_publish_fails_if_final_probe_crosses_140_second_channel_boundary(self):
        queue = self.enqueue(preflight_duration=140.0)
        client = ScriptedHttpClient(
            [
                service.HttpResponse(
                    200,
                    {"content-type": "video/mp4", "content-length": "5"},
                    body=b"video",
                )
            ]
        )
        with mock.patch.object(
            service, "probe_media", return_value={"duration": 140.01}
        ):
            with self.assertRaises(service.XPostError) as caught:
                service.publish_canary(
                    db_path=self.db_path,
                    queue_id=queue["id"],
                    account={
                        "id": 2,
                        "username": "ShortsDramhx",
                        "subscription_type": "premium",
                    },
                    access_token="secret-token",
                    public_root=self.root / "boundary-public" / "s2l",
                    short_base_url="https://gy.g2flow.com/s2l",
                    allowed_media_hosts=["media.example.com"],
                    http_client=client,
                    timeout=5,
                )
        self.assertEqual(caught.exception.code, "media_preflight_changed")
        self.assertFalse(
            any(
                request["url"].endswith("/2/media/upload/initialize")
                for request in client.requests
            )
        )

    def test_nonpremium_long_publish_fails_before_media_upload(self):
        queue = self.enqueue(material_id="88009", preflight_duration=180.0)
        client = ScriptedHttpClient(
            [
                service.HttpResponse(
                    200,
                    {"content-type": "video/mp4", "content-length": "5"},
                    body=b"video",
                )
            ]
        )

        def fail_closed_probe(*_args, **kwargs):
            self.assertEqual(
                kwargs["max_duration_seconds"],
                service.STANDARD_MAX_DURATION_SECONDS,
            )
            raise service.XPostError(
                "x_long_video_requires_premium",
                "premium required",
                422,
            )

        with mock.patch.object(
            service, "probe_media", side_effect=fail_closed_probe
        ):
            with self.assertRaises(service.XPostError) as caught:
                service.publish_canary(
                    db_path=self.db_path,
                    queue_id=queue["id"],
                    account={
                        "id": 2,
                        "username": "ShortsDramhx",
                        "subscription_type": "none",
                    },
                    access_token="secret-token",
                    public_root=self.root / "standard-public" / "s2l",
                    short_base_url="https://gy.g2flow.com/s2l",
                    allowed_media_hosts=["media.example.com"],
                    http_client=client,
                    timeout=5,
                )
        self.assertEqual(
            caught.exception.code, "x_long_video_requires_premium"
        )
        self.assertEqual(len(client.requests), 1)
        self.assertFalse(
            any(
                request["url"].endswith("/2/media/upload/initialize")
                for request in client.requests
            )
        )

    def test_long_video_validation_code_keeps_material_retryable(self):
        self.assertFalse(
            service._material_validation_is_blocking(
                "x_long_video_requires_premium"
            )
        )

    def test_short_link_preparation_failure_does_not_leave_reserved_log(self):
        queue = self.enqueue(material_id="88004")
        with mock.patch.object(
            service,
            "write_short_redirect",
            side_effect=service.XPostError(
                "short_link_write_failed", "simulated disk failure", 500
            ),
        ):
            with self.assertRaises(service.XPostError) as caught:
                service.publish_canary(
                    db_path=self.db_path,
                    queue_id=queue["id"],
                    account={"id": 2, "username": "ShortsDramhx"},
                    access_token="secret-token",
                    public_root=self.root / "public" / "s2l",
                    short_base_url="https://gy.g2flow.com/s2l",
                    allowed_media_hosts=["media.example.com"],
                    http_client=ScriptedHttpClient([]),
                    timeout=5,
                )
        self.assertEqual(caught.exception.code, "short_link_write_failed")
        log = service.XPostStore(self.db_path).get_log(1)
        self.assertEqual(log["status"], "failed")
        self.assertEqual(log["error_code"], "short_link_write_failed")
        self.assertFalse(log["unknown_outcome"])

    def test_unknown_create_outcome_is_logged_redacted_and_never_retried(self):
        queue = self.enqueue(material_id="88002")
        client = ScriptedHttpClient(
            [
                service.HttpResponse(200, {"content-type": "video/mp4", "content-length": "5"}, body=b"video"),
                response(200, {"data": {"id": "media2"}}),
                response(200, {"data": {"expires_at": 1}}),
                response(200, {"data": {"id": "media2"}}),
                OSError("reset Authorization=supersecret"),
            ]
        )
        kwargs = dict(
            db_path=self.db_path, queue_id=queue["id"], account={"id": 2, "username": "ShortsDramhx"},
            access_token="access-secret", public_root=self.root / "public" / "s2l",
            short_base_url="https://gy.g2flow.com/s2l", allowed_media_hosts=["media.example.com"],
            http_client=client, timeout=5,
        )
        with mock.patch.object(service, "probe_media", return_value=valid_probe_payload()):
            with self.assertRaises(service.XPostError) as caught:
                service.publish_canary(**kwargs)
        self.assertTrue(caught.exception.unknown_outcome)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            log = conn.execute("SELECT * FROM x_post_publish_log WHERE queue_id=?", (queue["id"],)).fetchone()
        self.assertEqual(log["status"], "failed")
        self.assertEqual(log["unknown_outcome"], 1)
        self.assertNotIn("supersecret", log["error_message"])
        request_count = len(client.requests)
        with self.assertRaises(service.XPostError) as repeated:
            service.publish_canary(**kwargs)
        self.assertEqual(repeated.exception.code, "x_post_unknown_outcome")
        self.assertEqual(len(client.requests), request_count)

    def test_explicit_create_post_429_is_known_and_stops_daily_run(self):
        store = service.XPostStore(self.db_path)
        candidates = []
        for rank, (account_id, username, material_id) in enumerate(
            (
                (2, "ShortsDramhx", "88101"),
                (3, "NextShortsy1", "88102"),
                (4, "GrapeShortlzod", "88103"),
            ),
            1,
        ):
            item = candidate(account_id, username)
            item.update(
                {
                    "material_id": material_id,
                    "content_id": "content-" + material_id,
                    "candidate_rank": rank,
                    "spend": 100 - rank,
                    "preflight_sha256": service.hashlib.sha256(b"video").hexdigest(),
                    "preflight_size": 5,
                    "compliance_counts": {
                        "facebook_violation_count": 0,
                        "tiktok_violation_count": 0,
                        "twitter_violation_count": 0,
                        "resource_audit_count": 0,
                        "dangerous_tag_count": 0,
                    },
                }
            )
            candidates.append(item)
        plan = store.create_daily_plan("2026-07-23", "2026-07-22", candidates)
        queue = plan["queues"][0]
        client = ScriptedHttpClient(
            [
                service.HttpResponse(
                    200,
                    {"content-type": "video/mp4", "content-length": "5"},
                    body=b"video",
                ),
                response(200, {"data": {"id": "media429"}}),
                response(200, {"data": {"expires_at": 1}}),
                response(200, {"data": {"id": "media429"}}),
                response(429, {"message": "rate limited"}),
            ]
        )
        with mock.patch.object(service, "probe_media", return_value=valid_probe_payload()):
            with self.assertRaises(service.XPostError) as caught:
                service.publish_canary(
                    db_path=self.db_path,
                    queue_id=queue["id"],
                    account={"id": 2, "username": "ShortsDramhx"},
                    access_token="secret-token",
                    public_root=self.root / "public" / "s2l",
                    short_base_url="https://gy.g2flow.com/s2l",
                    allowed_media_hosts=["media.example.com"],
                    http_client=client,
                    sleeper=lambda _seconds: None,
                    timeout=5,
                )
        self.assertEqual(caught.exception.code, "x_post_rate_limited")
        self.assertFalse(caught.exception.unknown_outcome)
        log = store.query_logs(
            {"account_id": 2, "run_date": "2026-07-23", "page": 1, "page_size": 10}
        )["items"][0]
        self.assertEqual(log["error_code"], "x_post_rate_limited")
        self.assertFalse(log["unknown_outcome"])
        self.assertEqual(store.get_run(plan["id"])["status"], "stopped")
        self.assertEqual(
            store.query_logs(
                {"run_date": "2026-07-23", "page": 1, "page_size": 10}
            )["pagination"]["total"],
            3,
        )

    def test_daily_publish_refuses_media_changed_after_preflight(self):
        store = service.XPostStore(self.db_path)
        candidates = []
        for rank, (account_id, username, material_id) in enumerate(
            (
                (2, "ShortsDramhx", "88201"),
                (3, "NextShortsy1", "88202"),
                (4, "GrapeShortlzod", "88203"),
            ),
            1,
        ):
            item = candidate(account_id, username)
            item.update(
                {
                    "material_id": material_id,
                    "content_id": "content-" + material_id,
                    "candidate_rank": rank,
                    "spend": 100 - rank,
                    "preflight_sha256": "a" * 64,
                    "preflight_size": 5,
                    "compliance_counts": {
                        "facebook_violation_count": 0,
                        "tiktok_violation_count": 0,
                        "twitter_violation_count": 0,
                        "resource_audit_count": 0,
                        "dangerous_tag_count": 0,
                    },
                }
            )
            candidates.append(item)
        plan = store.create_daily_plan("2026-07-23", "2026-07-22", candidates)
        client = ScriptedHttpClient(
            [
                service.HttpResponse(
                    200,
                    {"content-type": "video/mp4", "content-length": "5"},
                    body=b"video",
                )
            ]
        )
        with self.assertRaises(service.XPostError) as caught:
            service.publish_canary(
                db_path=self.db_path,
                queue_id=plan["queues"][0]["id"],
                account={"id": 2, "username": "ShortsDramhx"},
                access_token="secret-token",
                public_root=self.root / "public" / "s2l",
                short_base_url="https://gy.g2flow.com/s2l",
                allowed_media_hosts=["media.example.com"],
                http_client=client,
                timeout=5,
            )
        self.assertEqual(caught.exception.code, "media_preflight_changed")
        self.assertFalse(caught.exception.unknown_outcome)
        self.assertEqual(len(client.requests), 1)

    def test_account_username_mismatch_fails_before_reserving_or_network(self):
        queue = self.enqueue()
        client = ScriptedHttpClient([])
        with self.assertRaises(service.XPostError) as caught:
            service.publish_canary(
                db_path=self.db_path, queue_id=queue["id"],
                account={"id": 2, "username": "OtherAccount"}, access_token="token",
                public_root=self.root / "s2l", short_base_url="https://gy.g2flow.com/s2l",
                allowed_media_hosts=["media.example.com"], http_client=client,
            )
        self.assertEqual(caught.exception.code, "x_post_account_mismatch")
        self.assertEqual(client.requests, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
