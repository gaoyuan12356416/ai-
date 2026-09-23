"""Offline tests for missing source duration recovery and its fences."""

import math
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.x_posts import service, source_duration
from features.x_posts.selector import select_manual_candidates, select_pool_candidates
from scripts.test_x_post_material_pool_selector import PoolConnection, pool_item


class DurationSelectorTests(unittest.TestCase):
    def test_missing_values_use_real_duration_in_pool_and_manual(self):
        for value in (None, "", 0, "0", 0.0):
            with self.subTest(value=value), patch(
                "features.x_posts.selector.probe_source_duration", return_value=843.25,
            ) as probe:
                conn = PoolConnection([11])
                conn.materials["11"]["video_duration"] = value
                for selected, rejected in (
                    select_manual_candidates(conn, ["11"], "2026-09-22"),
                    select_pool_candidates(conn, [pool_item(1, 11, "2026-09-23T00:00:00Z")], "2026-09-22"),
                ):
                    self.assertEqual(rejected, [])
                    self.assertEqual(selected[0]["source_duration"], 843.25)
                self.assertEqual(probe.call_count, 2)

    def test_measured_over_limit_and_invalid_results_still_fail(self):
        for duration, error in ((14401, "material_duration_exceeds_limit"), (0, "material_duration_missing"), (math.nan, "material_duration_missing")):
            with self.subTest(duration=duration), patch(
                "features.x_posts.selector.probe_source_duration", return_value=duration,
            ):
                conn = PoolConnection([11])
                conn.materials["11"]["video_duration"] = 0
                selected, rejected = select_manual_candidates(conn, ["11"], "2026-09-22")
                self.assertEqual(selected, [])
                self.assertEqual(rejected[0]["error_code"], error)

    def test_failed_probe_is_redacted_and_does_not_block_normal_material(self):
        conn = PoolConnection([11, 12])
        conn.materials["11"]["video_duration"] = 0
        with patch("features.x_posts.selector.probe_source_duration", side_effect=service.XPostError("media_probe_failed", "https://private/?token=secret", 422)):
            selected, rejected = select_manual_candidates(conn, ["11", "12"], "2026-09-22", limit=2)
        self.assertEqual([r["material_id"] for r in selected], ["12"])
        self.assertNotIn("secret", str(rejected))
        self.assertEqual(rejected[0]["error_code"], "material_duration_missing")

    def test_existing_duration_images_and_invalid_metadata_do_not_probe(self):
        conn = PoolConnection([11, 12, 13])
        conn.materials["12"].update(material_type=1, video_duration=0)
        conn.materials["13"]["video_duration"] = "bad"
        with patch("features.x_posts.selector.probe_source_duration") as probe:
            selected, rejected = select_manual_candidates(conn, ["11", "12", "13"], "2026-09-22", limit=3)
        probe.assert_not_called()
        self.assertEqual(len(selected), 2)
        self.assertEqual(rejected[0]["error_code"], "material_duration_invalid")


class DurationMediaTests(unittest.TestCase):
    def setUp(self):
        source_duration._CACHE.clear()
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        (Path(self.root.name) / "media-work").mkdir()
        for p in (
            patch.object(source_duration, "_mp4_metadata_duration", side_effect=ValueError("not MP4")),
            patch.object(service, "DEFAULT_STORAGE_ROOT", self.root.name),
            patch.object(service, "preflight_post_storage"),
            patch.dict(os.environ, {"X_POST_SOURCE_DURATION_ALLOWED_HOSTS": "media.example.test"}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_temp_media_removed_and_only_success_cached_by_exact_url(self):
        paths = []
        def download(url, path, allowed, **kwargs):
            self.assertEqual(allowed, ("media.example.test",))
            self.assertEqual(kwargs["max_bytes"], service.DEFAULT_MAX_MEDIA_BYTES)
            path.write_bytes(b"video")
            paths.append(path)
            return {"media_type": "video/mp4"}
        with patch.object(service, "download_media", side_effect=download) as get, patch.object(source_duration, "_probe_raw_video_duration", return_value=78.4):
            for url in ("https://media.example.test/a.mp4",) * 2 + ("https://media.example.test/b.mp4",):
                self.assertEqual(source_duration.probe_source_duration(url), 78.4)
            self.assertEqual(get.call_count, 2)
        self.assertTrue(all(not p.exists() for p in paths))

    def test_failure_not_cached_and_temp_removed(self):
        def download(url, path, allowed, **kwargs):
            path.write_bytes(b"video")
            return {"media_type": "video/mp4"}
        with patch.object(service, "download_media", side_effect=download) as get, patch.object(source_duration, "_probe_raw_video_duration", side_effect=service.XPostError("media_probe_failed", "bad", 422)):
            for _ in range(2):
                with self.assertRaises(service.XPostError):
                    source_duration.probe_source_duration("https://media.example.test/fail.mp4")
            self.assertEqual(get.call_count, 2)
        self.assertEqual(list((Path(self.root.name) / "media-work").iterdir()), [])

    def test_bad_storage_prevents_download(self):
        with patch.object(service, "preflight_post_storage", side_effect=service.XPostError("x_post_storage_unavailable", "unmounted", 503)), patch.object(service, "download_media") as get:
            with self.assertRaises(service.XPostError):
                source_duration.probe_source_duration("https://media.example.test/storage.mp4")
        get.assert_not_called()

    def test_disallowed_url_rejected_by_existing_downloader(self):
        with self.assertRaises(service.XPostError) as raised:
            source_duration.probe_source_duration("https://127.0.0.1/source.mp4")
        self.assertEqual(raised.exception.code, "media_host_not_allowed")


class MP4MetadataTests(unittest.TestCase):
    @staticmethod
    def box(kind, data):
        return struct.pack(">I4s", len(data) + 8, kind) + data

    def movie(self, video=True, version=0):
        mvhd = (bytes(12) + struct.pack(">II", 1000, 843098)) if version == 0 else (b"\x01" + bytes(19) + struct.pack(">IQ", 1000, 843098))
        hdlr = bytes(8) + (b"vide" if video else b"soun")
        return self.box(b"mvhd", mvhd) + self.box(b"trak", self.box(b"mdia", self.box(b"hdlr", hdlr)))

    def test_movie_versions_and_video_identity(self):
        for version in (0, 1):
            self.assertAlmostEqual(source_duration._movie_duration(self.movie(version=version)), 843.098)
        with self.assertRaises(ValueError):
            source_duration._movie_duration(self.movie(video=False))

    def test_range_seek_skips_media_body_and_checks_etag(self):
        from unittest.mock import MagicMock
        data = self.box(b"ftyp", b"isom0000") + self.box(b"mdat", bytes(256000)) + self.box(b"moov", self.movie())
        requests = []
        def request(method, url, *, headers, **kwargs):
            first, last = map(int, headers["Range"].removeprefix("bytes=").split("-"))
            last = min(last, len(data)-1)
            requests.append((first, last))
            response = MagicMock()
            response.status = 206
            response.headers = {"content-range": "bytes %d-%d/%d" % (first, last, len(data)), "etag": '"source1"'}
            response.iter_bytes.return_value = [data[first:last+1]]
            if len(requests) > 1:
                self.assertEqual(headers["If-Match"], '"source1"')
            return response
        with patch.object(service.UrllibHttpClient, "request", side_effect=request):
            duration = source_duration._mp4_metadata_duration("https://media.example.test/x.mp4", ("media.example.test",))
        self.assertAlmostEqual(duration, 843.098)
        self.assertEqual(len(requests), 2)
        self.assertLess(sum(last-first+1 for first,last in requests), 66000)


if __name__ == "__main__":
    unittest.main()
