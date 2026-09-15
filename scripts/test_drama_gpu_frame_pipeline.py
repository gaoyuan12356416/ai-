"""Cache corruption and timestamp regression tests; real GPU QA is separate."""
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import sys
from types import SimpleNamespace

from features.drama_synthesis import asset_cache
from features.drama_synthesis.native_gpu import Timeline, fps_slot, container_origin
from features.drama_synthesis.h264_headers import sps_dimensions, packet_dimensions
from features.drama_synthesis.gpu import run_render_with_progress


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

    def test_audio_video_start_offset_keeps_container_clock(self):
        stream = SimpleNamespace(start_time=269, time_base=Fraction(1, 12800))
        self.assertEqual(container_origin(SimpleNamespace(start_time=0), stream), 0)
        self.assertEqual(container_origin(SimpleNamespace(start_time=None), stream), Fraction(269, 12800))

    def test_nonzero_pts_25_to_30_duplicate_pattern(self):
        # FFmpeg fps=30,setpts=PTS-STARTPTS for video starting 21 ms after audio.
        timeline = Timeline((Fraction(21, 1000) + Fraction(i, 25), i) for i in range(50))
        self.assertEqual([timeline.at(i) for i in range(10)], [0, 1, 2, 3, 4, 4, 5, 6, 7, 8])


class NativeBoundaryTests(unittest.TestCase):
    def test_real_nvenc_sps_dimensions(self):
        landscape = bytes.fromhex("6764001fac2b200a00b7602d4080805000003e80000c350e00000300aae6000006acfc2ef2e0a0")
        portrait = bytes.fromhex("67640029ac2b20168143602d4040405000003e80000c350e000003002625a000001dcd652ef2e0a0")
        self.assertEqual(sps_dimensions(landscape), (1280, 720))
        self.assertEqual(sps_dimensions(portrait), (720, 1280))
        self.assertEqual(packet_dimensions(b"\0\0\0\1"+landscape+b"\0\0\1"+portrait), (720, 1280))
        self.assertIsNone(packet_dimensions(b"\0\0\1\x65\x88\x80"))
        with self.assertRaises(ValueError):
            sps_dimensions(portrait[:8])

    def test_python_child_uses_same_tracked_progress_protocol(self):
        updates = []
        command = [sys.executable, "-c", "print('frame=30\\nout_time_us=1000000\\nprogress=end', flush=True)"]
        run_render_with_progress(command, timeout=30, duration_seconds=1,
                                 ffmpeg_progress=False, progress_callback=updates.append)
        self.assertTrue(any(row.get("frame") == 30 for row in updates))


if __name__ == "__main__":
    unittest.main()
