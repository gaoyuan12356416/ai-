#!/usr/bin/env python3
"""Offline unit tests for the X Post canary module."""

import contextlib
import http.client
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
            self.assertNotIn("source_queue_id", columns)

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
        with self.assertRaises(service.XPostError) as caught:
            service.download_media("http://media.example.com/a.mp4", target, ["media.example.com"], http_client=client)
        self.assertEqual(caught.exception.code, "invalid_media_url")
        with self.assertRaises(service.XPostError) as caught:
            service.download_media("https://evil.example/a.mp4", target, ["media.example.com"], http_client=client)
        self.assertEqual(caught.exception.code, "media_host_not_allowed")

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
        self.assertEqual(
            caught.exception.code, "x_long_video_requires_premium"
        )
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
