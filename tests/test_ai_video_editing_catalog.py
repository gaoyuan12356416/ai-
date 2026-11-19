import hashlib
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from features.ai_video_editing.catalog import Catalog, decode_key, safe_url, source_key
from features.ai_video_editing import catalog_imports as imports
from features.ai_video_editing.runtime import Runtime, Fault


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Runtime(self.tmp.name)
        imports.setup(self.rt)
        self.actor = {"tenant_key": "one", "user_id": "test"}
        self.other = {"tenant_key": "two", "user_id": "test"}
        self.body = {
            "product": "dramawave",
            "key": source_key(["42", "dramawave", "US", "en"]),
            "episodes": [1, 2],
        }
        self.catalog = Catalog(
            types.SimpleNamespace(
                DB_NAME="kunlunads_dev", SOURCE_TABLE="ads_drama_resource"
            )
        )
        self.catalog.settings = {"MOBOREELS_INSTITUTION_USER_ID": "institution"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_selection_deduplicates_and_isolates_owner(self):
        first = imports.submit(self.rt, self.actor, self.body)["items"]
        self.assertEqual(first, imports.submit(self.rt, self.actor, self.body)["items"])
        self.assertEqual([], imports.list_imports(self.rt, self.other)["items"])
        other = imports.submit(self.rt, self.other, self.body)["items"]
        self.assertNotEqual(first[0]["id"], other[0]["id"])
        with self.assertRaises(Fault) as exc:
            imports.retry(self.rt, self.other, first[0]["id"])
        self.assertEqual(404, exc.exception.status)

    def test_invalid_selection_and_product_are_rejected(self):
        for change in (
            {"product": "internal"},
            {"episodes": [True]},
            {"episodes": list(range(1, 22))},
            {"episodes": [1, 1]},
            {"key": "bad"},
        ):
            with self.assertRaises(Fault):
                imports.submit(self.rt, self.actor, dict(self.body, **change))
        with self.assertRaises(Fault):
            decode_key(self.body["key"], "moboreels")

    def test_expired_import_recovers_without_new_job(self):
        imports.submit(self.rt, self.actor, dict(self.body, episodes=[1]))
        first = imports.claim(self.rt)
        self.assertIsNone(imports.claim(self.rt))
        with self.rt.db(True) as db:
            db.execute(
                "UPDATE catalog_imports SET expires=? WHERE id=?",
                (time.time() - 1, first["id"]),
            )
        again = imports.claim(self.rt)
        self.assertEqual(first["id"], again["id"])
        self.assertNotEqual(first["lease"], again["lease"])
        with self.assertRaises(Fault):
            imports.progress(self.rt, first, 2, 10)

    def test_success_saves_private_asset_and_provenance(self):
        imports.submit(self.rt, self.actor, dict(self.body, episodes=[1]))
        job = imports.claim(self.rt)

        def download(url, dest, callback):
            dest.write_bytes(b"private-video")
            callback(13, 13)
            return hashlib.sha256(b"private-video").hexdigest()

        source = types.SimpleNamespace(
            episodes=lambda *a, **kw: {
                "items": [
                    {
                        "number": 1,
                        "name": "Story",
                        "language": "en",
                        "url": "https://example.com/video.mp4",
                    }
                ]
            }
        )
        with patch.object(imports, "download", download), patch(
            "features.ai_video_editing.runtime.probe",
            return_value={
                "duration_ms": 5000,
                "has_audio": True,
                "width": 720,
                "height": 1280,
            },
        ):
            imports.process(self.rt, source, job)
        result = imports.list_imports(self.rt, self.actor)["items"][0]
        self.assertEqual("ready", result["status"])
        self.assertEqual(self.body["key"], result["source_key"])
        asset = self.rt.asset(result["asset_id"], self.actor)
        self.assertIn("DramaWave", asset["series_code"])
        with self.assertRaises(Fault):
            self.rt.asset(result["asset_id"], self.other)
        self.assertEqual([], list((self.rt.root / "temp").iterdir()))

    def test_missing_episode_fails_with_retry_and_no_asset(self):
        imports.submit(self.rt, self.actor, dict(self.body, episodes=[1]))
        job = imports.claim(self.rt)
        imports.process(
            self.rt, types.SimpleNamespace(episodes=lambda *a, **k: {"items": []}), job
        )
        row = imports.list_imports(self.rt, self.actor)["items"][0]
        self.assertEqual("failed", row["status"])
        self.assertEqual("", row["asset_id"])
        imports.retry(self.rt, self.actor, row["id"])
        self.assertEqual(
            "queued", imports.list_imports(self.rt, self.actor)["items"][0]["status"]
        )

    def test_catalogs_query_only_their_canonical_source(self):
        with patch.object(self.catalog, "query", return_value=[]) as query:
            self.catalog.search("dramawave", "x' OR 1=1")
            sql = query.call_args[0][0]
            self.assertIn("app_id='1479'", sql)
            self.assertIn("ads_drama_resource", sql)
            self.assertNotIn("x' OR 1=1", sql)
            self.catalog.search("moboreels", "name")
            sql = query.call_args[0][0]
            self.assertIn("ads_ai.cdreader_koc_drama", sql)
            self.assertIn("institution_user_id=", sql)
            self.assertNotIn("ads_drama_resource", sql)

    def test_episode_list_does_not_expose_source_urls(self):
        with patch.object(
            self.catalog,
            "query",
            return_value=[
                {
                    "number": 1,
                    "url": "https://example.com/v?signature=secret",
                    "name": "Story",
                    "language": "en",
                }
            ],
        ):
            result = self.catalog.episodes("dramawave", self.body["key"])
            self.assertNotIn("url", result["items"][0])

    def test_source_download_rejects_private_network(self):
        with patch(
            "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]
        ):
            with self.assertRaises(Fault):
                safe_url("https://example.com/media.mp4")


if __name__ == "__main__":
    unittest.main()
