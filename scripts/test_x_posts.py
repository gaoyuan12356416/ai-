#!/usr/bin/env python3
"""Offline unit tests for the X Post canary module."""

import contextlib
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
    }


def valid_probe_payload(width=720, height=1280, fps="30000/1001", duration="45.25"):
    return {
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
            self.assertNotIn("source_queue_id", columns)

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
        with self.assertRaises(service.XPostError):
            service.build_w2a_url({"username": "bad name"})

    def test_real_candidate_material_name_with_brackets_builds_exact_af_ad(self):
        material_name = "【推荐】2M_TROTLQ_(14_15超爽卡点)_EN_精剪_zhouliwei_3_episode[15].mp4"
        url = service.build_w2a_url(
            {
                "username": "ShortsDramhx", "timestamp": 1784736000, "material_language": "en",
                "drama_name": "The Rise of the Lycan Queen", "tag": "Fantasy", "log_id": 17,
                "page_name": "Short Drama", "page_id": "2076951197916037120",
                "material_name": material_name, "material_id": "5221348", "queue_id": 4,
                "content_id": "3CRScaBEY0",
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

    def test_post_text_preserves_url_and_conservatively_truncates(self):
        short = "https://ai.yingliangads.com/s2l/12.html"
        text = service.build_post_text(short, "剧" * 300)
        first, second = text.split("\n", 1)
        self.assertEqual(first, short)
        self.assertTrue(second)
        self.assertTrue(second.endswith("…"))
        self.assertLessEqual(23 + 1 + sum(1 if ord(char) <= 0x10FF else 2 for char in second), 280)

    def test_short_redirect_is_atomic_immutable_and_public_readable(self):
        long_url = service.build_w2a_url(
            {
                "username": "ShortsDramhx", "timestamp": 1784736000, "material_language": "en",
                "drama_name": "Drama", "tag": "safe", "log_id": 1, "page_name": "Page",
                "page_id": "123", "material_name": "asset", "material_id": "9", "queue_id": 2,
                "content_id": "3",
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

    def test_ffprobe_accepts_720x1280_and_rejects_bad_codec_or_fps(self):
        media = self.root / "video.mp4"
        media.write_bytes(b"video")

        def runner_for(payload):
            return lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

        with mock.patch.dict(os.environ, {"X_POST_FFPROBE_BIN": "/safe/bin/ffprobe"}):
            captured = {}

            def capturing_runner(command, **_kwargs):
                captured["command"] = command
                return SimpleNamespace(returncode=0, stdout=json.dumps(valid_probe_payload()), stderr="")

            result = service.probe_media(media, runner=capturing_runner)
        self.assertEqual((result["width"], result["height"]), (720, 1280))
        self.assertEqual(captured["command"][0], "/safe/bin/ffprobe")
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
        self.assertEqual(caught.exception.code, "invalid_media_duration")
        with self.assertRaises(service.XPostError) as caught:
            service.probe_media(media, runner=runner_for(valid_probe_payload(width=1080, height=1920)))
        self.assertEqual(caught.exception.code, "invalid_media_dimensions")
        non_lc = valid_probe_payload()
        non_lc["streams"][1]["profile"] = "HE-AAC"
        with self.assertRaises(service.XPostError) as caught:
            service.probe_media(media, runner=runner_for(non_lc))
        self.assertEqual(caught.exception.code, "invalid_media_codec")

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
                public_root=public_root, short_base_url="https://ai.yingliangads.com/s2l",
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
            public_root=public_root, short_base_url="https://ai.yingliangads.com/s2l",
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
        self.assertIn("yingliang_post_CLV_VL_ShortsDramhx*", c_value)
        self.assertEqual(af_c_id, str(queue["id"]))
        self.assertNotIn("secret-token", dump)

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
            short_base_url="https://ai.yingliangads.com/s2l", allowed_media_hosts=["media.example.com"],
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

    def test_account_username_mismatch_fails_before_reserving_or_network(self):
        queue = self.enqueue()
        client = ScriptedHttpClient([])
        with self.assertRaises(service.XPostError) as caught:
            service.publish_canary(
                db_path=self.db_path, queue_id=queue["id"],
                account={"id": 2, "username": "OtherAccount"}, access_token="token",
                public_root=self.root / "s2l", short_base_url="https://ai.yingliangads.com/s2l",
                allowed_media_hosts=["media.example.com"], http_client=client,
            )
        self.assertEqual(caught.exception.code, "x_post_account_mismatch")
        self.assertEqual(client.requests, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
