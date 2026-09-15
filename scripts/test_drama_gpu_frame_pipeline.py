"""Cache corruption and timestamp regression tests; real GPU QA is separate."""
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from features.drama_synthesis import asset_cache
from features.drama_synthesis.native_gpu import Timeline, fps_slot


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.source_sha = "a" * 64
        self.path = self.root / (self.source_sha + ".nut")
        self.receipt = self.path.with_suffix(".json")
        self.path.write_bytes(b"verified pixels")
        self.info = {"version": asset_cache.VERSION, "source_sha256": self.source_sha,
                     "sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
                     "size": self.path.stat().st_size, "mtime_ns": self.path.stat().st_mtime_ns,
                     "frames": 3, "rgba_frames_verified": True, "timing_basis": "demux"}
        self.save()

    def save(self):
        if self.receipt.exists():
            self.receipt.chmod(0o644)
        self.receipt.write_text(json.dumps(self.info), encoding="utf-8")
        self.path.chmod(0o444)
        self.receipt.chmod(0o444)

    def tearDown(self):
        for item in self.root.iterdir():
            item.chmod(0o644)
        self.tmp.cleanup()
        asset_cache._digest.cache_clear()

    def test_verified_entry(self):
        entry = asset_cache.verified_entry(self.root, self.source_sha)
        self.assertEqual(str(self.path), entry["path"])

    def test_same_size_tamper_with_restored_mtime_is_rehashed(self):
        asset_cache.verified_entry(self.root, self.source_sha)
        stat = self.path.stat()
        self.path.chmod(0o644)
        self.path.write_bytes(b"corrupt! pixels")
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.path.chmod(0o444)
        with self.assertRaises(asset_cache.AssetCacheError):
            asset_cache.verified_entry(self.root, self.source_sha)

    def test_missing_receipt_fails_closed(self):
        self.receipt.chmod(0o644)
        self.receipt.unlink()
        with self.assertRaises(asset_cache.AssetCacheError):
            asset_cache.verified_entry(self.root, self.source_sha)

    def test_unverified_pixels_rejected(self):
        self.info["rgba_frames_verified"] = False
        self.save()
        with self.assertRaises(asset_cache.AssetCacheError):
            asset_cache.verified_entry(self.root, self.source_sha)

    def test_rounded_timestamp_receipt_rejected(self):
        self.info.pop("timing_basis")
        self.save()
        with self.assertRaises(asset_cache.AssetCacheError):
            asset_cache.verified_entry(self.root, self.source_sha)

    def test_other_source_receipt_rejected(self):
        self.info["source_sha256"] = "b" * 64
        self.save()
        with self.assertRaises(asset_cache.AssetCacheError):
            asset_cache.verified_entry(self.root, self.source_sha)


class TimelineTests(unittest.TestCase):
    def test_half_frames_round_away_from_zero(self):
        self.assertEqual(fps_slot(Fraction(1, 60)), 1)
        self.assertEqual(fps_slot(Fraction(-1, 60)), -1)
        self.assertEqual(fps_slot(Fraction(1, 60)-Fraction(1, 1000000)), 0)

    def test_25_to_30_matches_ffmpeg_nearest_pts_slots(self):
        timeline = Timeline((Fraction(i, 25), i) for i in range(50))
        self.assertEqual([timeline.at(i) for i in range(10)], [0, 1, 2, 2, 3, 4, 5, 6, 7, 7])

    def test_source_does_not_fabricate_missing_seconds(self):
        timeline = Timeline([(Fraction(0), 0)])
        self.assertEqual(timeline.at(29), 0)
        with self.assertRaisesRegex(RuntimeError, "ended_early"):
            timeline.at(31)


if __name__ == "__main__":
    unittest.main()
