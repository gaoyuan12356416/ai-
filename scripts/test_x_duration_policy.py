"""20-minute policy boundaries, legacy ledger compatibility and GPU contracts."""
import contextlib
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from features.x_posts import duration_policy, media_repair, service
from features.x_auto_posts.publisher import account_duration_limit_seconds
from scripts.test_x_posts import candidate, valid_probe_payload, response, ScriptedHttpClient
from scripts.test_x_post_media_repair import source_probe, make_request, repaired_probe
from scripts.x_post_daily_runner import _repair_job_key


class DurationPolicyTests(unittest.TestCase):
    def test_unbound_old_duration_rejection_is_rechecked_without_clearing_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = service.XPostStore(Path(tmp) / "posts.sqlite3")
            result = store.add_pool_materials(
                ["501"], validation_checks=[{"material_id": "501",
                    "error_code": "material_duration_exceeds_limit", "error_message": "old duration limit"}],
            )
            pool_id = result["items"][0]["id"]
            self.assertEqual([p["id"] for p in store.available_pool_items()], [pool_id])
            self.assertEqual(store.query_pool()["items"][0]["last_error_code"], "material_duration_exceeds_limit")
            store.record_pool_checks([{"pool_item_id": pool_id, "error_code": "", "error_message": ""}])
            self.assertEqual(store.query_pool()["items"][0]["last_error_code"], "")

    def test_migration_marks_existing_only_and_is_idempotent(self):
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE TABLE frozen(id INTEGER PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO frozen VALUES(1,'historical')")
            duration_policy.migrate(conn, ("frozen",))
            conn.execute("INSERT INTO frozen(id,value,duration_policy_version) VALUES(2,'new',2)")
            duration_policy.migrate(conn, ("frozen",))
            self.assertEqual(conn.execute("SELECT * FROM frozen ORDER BY id").fetchall(),
                             [(1, "historical", 1), (2, "new", 2)])
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE frozen SET duration_policy_version=2 WHERE id=1")

    def test_real_probe_boundaries_do_not_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video.mp4"
            path.write_bytes(b"fixture")
            for value in (0.5, 140, 140.001, 1094.871, 1199.999, 1200, 1200.001):
                with self.subTest(duration=value):
                    runner = lambda *a, **k: SimpleNamespace(
                        returncode=0, stdout=json.dumps(valid_probe_payload(duration=str(value))), stderr=""
                    )
                    if value <= 1200:
                        self.assertEqual(service.probe_media(path, runner=runner)["duration"], value)
                    else:
                        with self.assertRaises(service.XPostError) as caught:
                            service.probe_media(path, runner=runner)
                        self.assertEqual(caught.exception.code, "x_long_video_requires_premium")

    def test_membership_unknown_and_badge_do_not_change_limit(self):
        for status in ("none", "unknown", "", "future_tier"):
            self.assertEqual(account_duration_limit_seconds(
                {"subscription_type": status, "verified": True, "long_video_publish_eligible": True}
            ), 1200)
        self.assertEqual(account_duration_limit_seconds({"subscription_type": "none"}, 1), 140)

    def test_repair_keeps_18_minutes_and_separates_old_cache(self):
        for value in (140.001, 1094.871, 1200.0):
            result = media_repair.inspect_source(source_probe(duration=value),
                                                "invalid_media_codec", "standard_20m")
            self.assertFalse(result["trim_applied"])
        current = media_repair.inspect_source(source_probe(duration=1200.001),
                                              "invalid_media_codec", "standard_20m")
        self.assertTrue(current["trim_applied"])
        self.assertEqual(current["output_duration"], 1199.0)
        legacy = media_repair.inspect_source(source_probe(duration=1094.871),
                                             "invalid_media_codec", "standard")
        self.assertTrue(legacy["trim_applied"])
        self.assertEqual(legacy["output_duration"], 139.0)
        request = make_request(duration_policy="standard_20m")
        self.assertEqual(media_repair.validate_request(request)["duration_policy"], "standard_20m")
        item = {"material_id": "501", "pool_item_id": 1, "material_url": request["source_url"]}
        old = _repair_job_key(item, "a" * 64, media_repair.REPAIR_PROFILE, "standard")
        new = _repair_job_key(item, "a" * 64, media_repair.REPAIR_PROFILE, "standard_20m")
        self.assertNotEqual(old, new)

    def test_repaired_output_checks_1200_including_fraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.mp4"
            path.write_bytes(b"fixture")
            media_repair.validate_output(repaired_probe(duration=1200), path, (720, 1280),
                                         1024, expected_duration=1200, max_duration_seconds=1200)
            with self.assertRaises(media_repair.MediaRepairError) as caught:
                media_repair.validate_output(repaired_probe(duration=1200.001), path, (720, 1280),
                                             1024, expected_duration=1200.001, max_duration_seconds=1200)
            self.assertEqual(caught.exception.code, "repaired_media_invalid")

    def test_published_category_and_attribution_follow_frozen_version(self):
        for version, seconds, subscription, category, channel in (
            (2, 140.001, "none", "tweet_video", "short"),
            (2, 1094.871, "none", "tweet_video", "short"),
            (2, 1200.0, "none", "tweet_video", "short"),
            (2, 1200.0, "unknown", "tweet_video", "short"),
            (2, 1200.001, "premium", "amplify_video", "long"),
            (1, 140.0, "none", "tweet_video", "short"),
            (1, 1094.871, "premium", "amplify_video", "long"),
        ):
            with self.subTest(version=version, seconds=seconds), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = service.XPostStore(root / "posts.sqlite3")
                raw = candidate()
                raw["preflight_duration"] = seconds
                # Represent an existing queue with its server-frozen version.
                values = store._queue_payload(raw, duration_policy_version=version)
                columns = ("idempotency_key",) + service.QUEUE_LEDGER_FIELDS + service.QUEUE_FIELDS
                with contextlib.closing(sqlite3.connect(store.db_path)) as conn, conn:
                    conn.execute(
                        "INSERT INTO x_post_queue(" + ",".join(columns)
                        + ",status,created_at,updated_at) VALUES("
                        + ",".join("?" for _ in columns) + ",'queued',?,?)",
                        tuple(values[k] for k in columns) + (service.utc_now(), service.utc_now()),
                    )
                client = ScriptedHttpClient([
                    service.HttpResponse(200, {"content-type": "video/mp4", "content-length": "5"}, body=b"video"),
                    response(200, {"data": {"id": "media-1"}}),
                    response(200, {"data": {"expires_at": 1}}),
                    response(200, {"data": {"id": "media-1"}}),
                    response(201, {"data": {"id": "190003", "text": "ok"}}),
                ])
                with mock.patch.object(service, "probe_media", return_value={"duration": seconds}) as probe:
                    service.publish_canary(
                        db_path=store.db_path, queue_id=1,
                        account={"id": 2, "username": raw["account_username"], "subscription_type": subscription},
                        access_token="offline-fixture", public_root=root / "s2l",
                        short_base_url=service.DEFAULT_SHORT_BASE_URL,
                        allowed_media_hosts=["media.example.com"], http_client=client,
                    )
                expected_limit = 14400 if subscription == "premium" else (140 if version == 1 else 1200)
                self.assertEqual(probe.call_args.kwargs["max_duration_seconds"], expected_limit)
                init = next(r for r in client.requests if r["url"].endswith("/media/upload/initialize"))
                self.assertEqual(json.loads(init["body"])["media_category"], category)
                with contextlib.closing(sqlite3.connect(store.db_path)) as conn, conn:
                    url = conn.execute("SELECT long_url FROM x_post_publish_log").fetchone()[0]
                self.assertEqual(parse_qs(urlsplit(url).query)["af_channel"], [channel])


if __name__ == "__main__":
    unittest.main()
