#!/usr/bin/env python3
"""Offline HTTP/process doubles; no remote downloads, render jobs or COS writes."""
from __future__ import annotations

import ast
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests
from features.drama_synthesis import gpu, media_pipeline as media
from features.drama_synthesis.core import DramaSynthesisError, RECIPE_PROFILE
from features.drama_synthesis.local_checkpoint import checkpoint_error, file_fingerprint


URL = "https://media.example.test/episode.mp4?token=never-print-this"
BODY = b"abcdefghij"
ETAG = '"source-v1"'


class Response:
    def __init__(self, chunks=(), status=200, headers=None):
        self.status_code = status
        self.headers = {"Content-Length": str(len(BODY)), "ETag": ETAG} if headers is None else headers
        self.chunks, self.closed = list(chunks), False

    def iter_content(self, chunk_size):
        for value in self.chunks:
            if isinstance(value, Exception):
                raise value
            yield value

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses, self.calls, self.closed = list(responses), [], False
        self.urls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        self.urls.append(url)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "001.mp4"

    def download(self, responses, **kwargs):
        session = Session(responses)
        self.session = session
        result = media.download_episode(URL, self.path, session_factory=lambda: session, sleep=lambda _: None, **kwargs)
        return result, session

    def partial(self, data=BODY[:4], etag=ETAG):
        with self.assertRaises(DramaSynthesisError):
            self.download([Response([data, requests.ConnectionError("private " + URL)],
                                    headers={"Content-Length": "10", "ETag": etag})], max_attempts=1)

    def test_completed_download_and_restart_replay_have_size_sha_not_url(self):
        result, session = self.download([Response([BODY])])
        self.assertEqual(self.path.read_bytes(), BODY)
        self.assertEqual((result["size_bytes"], result["sha256"]), (10, hashlib.sha256(BODY).hexdigest()))
        self.assertFalse(result["reused"])
        self.assertTrue(session.closed)
        self.assertEqual(session.calls[0]["timeout"], (10, 180))
        self.assertFalse(session.trust_env)
        self.assertEqual(session.calls[0]["headers"]["Accept-Encoding"], "identity")
        again, second = self.download([])
        self.assertTrue(again["reused"])
        self.assertEqual(second.calls, [])
        marker = self.path.with_name("001.mp4.download.json").read_text()
        self.assertNotIn("never-print-this", marker)
        self.assertNotIn("media.example", marker)

    def test_nonempty_legacy_source_is_not_accepted(self):
        self.path.write_bytes(b"half")
        result, session = self.download([Response([BODY])])
        self.assertFalse(result["reused"])
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(self.path.read_bytes(), BODY)

    def test_failed_transfer_preserves_legacy_source_without_completed_record(self):
        self.path.write_bytes(b"legacy")
        self.partial()
        self.assertEqual(self.path.read_bytes(), b"legacy")
        self.assertEqual(self.path.with_name("001.mp4.part").read_bytes(), BODY[:4])
        self.assertFalse(self.path.with_name("001.mp4.download.json").exists())

    def test_disconnect_uses_strong_if_range_and_exact_offset(self):
        first = Response([BODY[:4], requests.ConnectionError(URL)])
        second = Response([BODY[4:]], 206, {"Content-Length": "6", "Content-Range": "bytes 4-9/10", "ETag": ETAG})
        result, session = self.download([first, second])
        self.assertEqual(self.path.read_bytes(), BODY)
        self.assertEqual(session.calls[1]["headers"]["Range"], "bytes=4-")
        self.assertEqual(session.calls[1]["headers"]["If-Range"], ETAG)
        self.assertTrue(first.closed and second.closed)
        self.assertEqual(result["size_bytes"], 10)

    def test_weak_or_missing_etag_restarts_full_instead_of_resuming(self):
        for etag in ('W/"weak"', ""):
            with self.subTest(etag=etag), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "episode.mp4"
                session = Session([Response([BODY[:4], requests.ConnectionError(URL)], headers={"Content-Length": "10", "ETag": etag}),
                                   Response([BODY], headers={"Content-Length": "10", "ETag": etag})])
                media.download_episode(URL, path, session_factory=lambda: session, sleep=lambda _: None)
                self.assertNotIn("Range", session.calls[1]["headers"])
                self.assertEqual(path.read_bytes(), BODY)

    def test_ignored_range_200_replaces_body_never_appends(self):
        self.partial()
        _, session = self.download([Response([BODY])])
        self.assertIn("Range", session.calls[0]["headers"])
        self.assertEqual(self.path.read_bytes(), BODY)

    def test_network_byte_metrics_do_not_count_cached_or_resumed_prefix_as_transfer(self):
        self.partial()
        counts = []
        self.download([Response([BODY[4:]], 206, {"Content-Length": "6", "Content-Range": "bytes 4-9/10", "ETag": ETAG})], transfer_callback=counts.append)
        self.assertEqual(sum(counts), 6)
        counts.clear()
        self.download([], transfer_callback=counts.append)
        self.assertEqual(counts, [])

    def test_changed_validator_is_a_conflict_and_never_promotes(self):
        self.partial()
        for status, headers in ((200, {"Content-Length": "10", "ETag": '"new"'}),
                                (206, {"Content-Length": "6", "Content-Range": "bytes 4-9/10", "ETag": '"new"'})):
            with self.subTest(status=status), self.assertRaises(DramaSynthesisError) as caught:
                self.download([Response([BODY], status, headers)])
            self.assertEqual(caught.exception.code, "drama_episode_source_changed")
            self.assertNotIn("never-print-this", str(caught.exception))
        self.assertFalse(self.path.exists())

    def test_bad_content_ranges_and_unrequested_206_are_rejected(self):
        self.partial()
        for content_range, length in (("bytes 3-9/10", "7"), ("bytes 4-9/11", "6"),
                                      ("bytes 4-8/10", "5"), ("bytes 4-9/*", "6"), ("bytes 4-9/10", "5")):
            with self.subTest(content_range=content_range), self.assertRaises(DramaSynthesisError):
                self.download([Response([BODY[4:]], 206, {"Content-Length": length, "Content-Range": content_range, "ETag": ETAG})])
        with tempfile.TemporaryDirectory() as directory:
            session = Session([Response([BODY], 206, {"Content-Length": "10", "Content-Range": "bytes 0-9/10", "ETag": ETAG})])
            with self.assertRaises(DramaSynthesisError):
                media.download_episode(URL, Path(directory) / "episode.mp4", session_factory=lambda: session)

    def test_416_requires_exact_completed_length_and_same_strong_validator(self):
        self.partial(BODY)
        _, session = self.download([Response([], 416, {"Content-Range": "bytes */10", "ETag": ETAG})])
        self.assertEqual(session.calls[0]["headers"]["Range"], "bytes=10-")
        self.assertEqual(self.path.read_bytes(), BODY)

    def test_unproven_416_falls_back_to_a_full_verified_get(self):
        self.partial()
        _, session = self.download([Response([], 416, {"Content-Range": "bytes */10"}), Response([BODY])])
        self.assertNotIn("Range", session.calls[1]["headers"])
        self.assertEqual(self.path.read_bytes(), BODY)

    def test_short_oversized_encoded_or_unknown_length_transfer_is_not_complete(self):
        cases = [Response([BODY[:4]]), Response([BODY + b"more"]),
                 Response([BODY], headers={"Content-Length": "10", "Content-Encoding": "gzip"}),
                 Response([BODY], headers={}), Response([BODY], headers={"Content-Length": "unknown"})]
        for response in cases:
            with self.subTest(response=response.headers), tempfile.TemporaryDirectory() as directory:
                session = Session([response])
                path = Path(directory) / "video.mp4"
                with self.assertRaises(DramaSynthesisError):
                    media.download_episode(URL, path, session_factory=lambda: session, max_attempts=1)
                self.assertFalse(path.exists())
                self.assertFalse(path.with_name("video.mp4.download.json").exists())

    def test_partial_prefix_hash_detects_corruption_before_http(self):
        self.partial()
        self.path.with_name("001.mp4.part").write_bytes(b"xxxx")
        with self.assertRaises(DramaSynthesisError):
            self.download([])
        self.assertEqual(self.session.calls, [])

    def test_crash_tail_is_discarded_only_after_durable_prefix_verification(self):
        self.partial()
        with self.path.with_name("001.mp4.part").open("ab") as handle:
            handle.write(b"uncommitted-tail")
        _, session = self.download([Response([BODY[4:]], 206, {"Content-Length": "6", "Content-Range": "bytes 4-9/10", "ETag": ETAG})])
        self.assertEqual(session.calls[0]["headers"]["Range"], "bytes=4-")
        self.assertEqual(self.path.read_bytes(), BODY)

    def test_completed_source_corruption_and_identity_change_fail_closed(self):
        self.download([Response([BODY])])
        session = Session([])
        with self.assertRaises(DramaSynthesisError) as caught:
            media.download_episode(URL + "new", self.path, session_factory=lambda: session)
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_conflict")
        self.path.write_bytes(b"xxxxxxxxxx")
        with self.assertRaises(DramaSynthesisError):
            self.download([])
        self.assertEqual(self.session.calls, [])

    def test_validator_rejection_preserves_old_file_and_hides_internal_error(self):
        self.path.write_bytes(b"old")
        with self.assertRaises(DramaSynthesisError) as caught:
            self.download([Response([BODY])], validate=mock.Mock(side_effect=ValueError(URL)))
        self.assertNotIn("never-print-this", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), b"old")

    def test_transient_http_errors_are_bounded_and_do_not_leak_url(self):
        self.download([Response([], 429), Response([], 503), Response([BODY])])
        self.assertEqual(len(self.session.calls), 3)
        with tempfile.TemporaryDirectory() as directory:
            session = Session([requests.ConnectionError(URL)] * 3)
            with self.assertRaises(DramaSynthesisError) as caught:
                media.download_episode(URL, Path(directory) / "new.mp4", session_factory=lambda: session, sleep=lambda _: None)
            self.assertEqual(len(session.calls), 3)
            self.assertNotIn("never-print-this", str(caught.exception))

    def test_worker_count_preserves_default_four_and_rejects_bad_values(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(media.download_worker_count(), 4)
        self.assertEqual(media.download_worker_count(8), 8)
        for value in (0, 9, True, "four", "1.5", ""):
            with self.subTest(value=value), self.assertRaises(DramaSynthesisError):
                media.download_worker_count(value)


class DownloadRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "episode.mp4"
        self.source = "https://img.tianmai.cn/resource/8HehaA3263/episode_001.mp4"
        self.route = media.freeze_episode_download_route(self.source, "international")

    def download(self, responses, **kwargs):
        self.sessions = [Session(rows) for rows in responses]
        sessions = iter(self.sessions)
        return media.download_episode_with_route(self.source, self.path, self.route,
                                                session_factory=lambda: next(sessions), sleep=lambda _: None,
                                                max_attempts=1, **kwargs)

    def test_default_original_and_explicit_international_exact_whitelist(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            original = media.freeze_episode_download_route(self.source)
        self.assertEqual(original, {"version": 1, "source_url": self.source,
                                    "primary_url": self.source, "fallback_url": ""})
        self.assertEqual(self.route["primary_url"], "https://accelerate.tianmai.cn/resource/8HehaA3263/episode_001.mp4")
        self.assertEqual(self.route["fallback_url"], self.source)
        variants = [self.source + "?signature=private", self.source + "?", self.source + "#fragment",
                    self.source.replace("img.tianmai.cn", "img.tianmai.cn:443"),
                    self.source.replace("img.tianmai.cn", "IMG.tianmai.cn"),
                    self.source.replace("img.tianmai.cn", "user:secret@img.tianmai.cn"),
                    self.source.replace("img.tianmai.cn", "other.example.test"),
                    self.source.replace("/resource/", "/other/"), self.source.replace(".mp4", ".webm"),
                    self.source.replace("8HehaA3263", "%38HehaA3263"), self.source + "/extra"]
        for source in variants:
            with self.subTest(source_hash=hashlib.sha256(source.encode()).hexdigest()):
                route = media.freeze_episode_download_route(source, "international")
                self.assertEqual((route["primary_url"], route["fallback_url"]), (source, ""))
        for policy in ("fastest", "", True, 4):
            with self.assertRaises(DramaSynthesisError):
                media.freeze_episode_download_route(self.source, policy)

    def test_frozen_route_is_strict_and_never_reselected_from_current_environment(self):
        with mock.patch.dict(os.environ, {"DRAMA_GPU_TIANMAI_CDN": "invalid-current-config"}):
            self.assertEqual(media.validate_episode_download_route(self.source, self.route), self.route)
        variants = [{**self.route, "version": True}, {**self.route, "extra": 1},
                    {**self.route, "source_url": self.source + "?secret=private"},
                    {**self.route, "primary_url": self.source + "other"},
                    {**self.route, "primary_url": "https://unknown.example.test/x.mp4"},
                    {**self.route, "fallback_url": self.source + "?secret=private"},
                    {**self.route, "fallback_url": None}]
        for route in variants:
            with self.assertRaises(DramaSynthesisError) as caught:
                media.validate_episode_download_route(self.source, route)
            self.assertEqual(caught.exception.code, "drama_episode_download_route_invalid")
            self.assertNotIn("private", str(caught.exception))
        with mock.patch.dict(os.environ, {"DRAMA_GPU_TIANMAI_CDN": "original"}):
            value = self.download([[Response([BODY])]])
        self.assertEqual(self.sessions[0].urls, [self.route["primary_url"]])
        self.assertEqual(value["origin"], "primary")

    def test_network_failure_falls_back_without_mixing_any_primary_prefix(self):
        fallback_body = BODY.upper()
        value = self.download([[Response([BODY[:4], requests.ConnectionError(URL)])],
                               [Response([fallback_body])]])
        self.assertEqual(self.path.read_bytes(), fallback_body)
        self.assertEqual(value["origin"], "fallback")
        self.assertEqual(self.sessions[0].urls, [self.route["primary_url"]])
        self.assertEqual(self.sessions[1].urls, [self.source])
        self.assertNotIn("Range", self.sessions[1].calls[0]["headers"])
        origins = self.path.parent / ".episode.mp4.download-origins"
        self.assertEqual((origins / "primary.mp4.part").read_bytes(), BODY[:4])
        primary_record = json.loads((origins / "primary.mp4.part.json").read_text())
        fallback_record = json.loads((origins / "fallback.mp4.download.json").read_text())
        self.assertNotEqual(primary_record["source_identity"], fallback_record["identity"]["source_identity"])
        for path in self.path.parent.rglob("*.json"):
            self.assertNotIn("tianmai.cn", path.read_text())
            self.assertNotIn("never-print-this", path.read_text())

    def test_resume_after_fallback_failure_stays_on_fallback_and_completed_replay_uses_no_http(self):
        fallback_body = BODY.upper()
        with self.assertRaises(DramaSynthesisError):
            self.download([[Response([BODY[:4], requests.ConnectionError(URL)])],
                           [Response([fallback_body[:5], requests.ConnectionError(URL)])]])
        value = self.download([[Response([fallback_body[5:]], 206,
                                        {"Content-Length": "5", "Content-Range": "bytes 5-9/10", "ETag": ETAG})]])
        self.assertEqual(self.sessions[0].urls, [self.source])
        self.assertEqual(self.sessions[0].calls[0]["headers"]["Range"], "bytes=5-")
        self.assertEqual(self.path.read_bytes(), fallback_body)
        self.assertEqual(value["origin"], "fallback")
        replay = self.download([])
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["sha256"], hashlib.sha256(fallback_body).hexdigest())

    def test_integrity_identity_and_checkpoint_failures_never_trigger_fallback(self):
        codes = ("drama_episode_download_invalid", "drama_episode_source_changed",
                 "drama_media_checkpoint_conflict", "drama_media_checkpoint_unverified",
                 "drama_episode_download_cancelled")
        for code in codes:
            with self.subTest(code=code):
                downloader = mock.Mock(side_effect=DramaSynthesisError(code, "safe", 409))
                with self.assertRaises(DramaSynthesisError):
                    media.download_episode_with_route(self.source, self.path, self.route, downloader=downloader)
                self.assertEqual(downloader.call_count, 1)
                self.assertEqual(downloader.call_args.args[0], self.route["primary_url"])
                state = json.loads(self.path.with_name("episode.mp4.route.json").read_text())
                self.assertEqual(state["active_origin"], "primary")
        with self.assertRaises(DramaSynthesisError) as caught:
            self.download([[Response([BODY[:4]])]])
        self.assertEqual(caught.exception.code, "drama_episode_download_invalid")
        self.assertEqual(len(self.sessions[0].calls), 1)

    def test_verified_origin_survives_crash_after_canonical_rename_without_redownload(self):
        save_completed = media.save_completed

        def crash_canonical(record, *args, **kwargs):
            if Path(record) == self.path.with_name("episode.mp4.download.json"):
                raise OSError("simulated local crash")
            return save_completed(record, *args, **kwargs)

        with mock.patch.object(media, "save_completed", side_effect=crash_canonical):
            with self.assertRaises(OSError):
                self.download([[Response([BODY])]])
        self.assertTrue(self.path.is_file())
        self.assertFalse(self.path.with_name("episode.mp4.download.json").exists())
        replay = self.download([])
        self.assertTrue(replay["reused"])
        self.assertEqual(self.path.read_bytes(), BODY)

    def test_corrupt_completed_artifact_or_route_state_fails_closed_before_http(self):
        self.download([[Response([BODY])]])
        self.path.write_bytes(BODY.upper())
        with self.assertRaises(DramaSynthesisError):
            self.download([])
        self.path.write_bytes(BODY)
        state_path = self.path.with_name("episode.mp4.route.json")
        state = json.loads(state_path.read_text())
        state["route_sha256"] = "0" * 64
        state_path.write_text(json.dumps(state))
        with self.assertRaises(DramaSynthesisError) as caught:
            self.download([])
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_conflict")

    def test_original_route_and_legacy_no_route_use_identical_existing_download_contract(self):
        session = Session([Response([BODY])])
        original = media.freeze_episode_download_route(URL, "original")
        media.download_episode_with_route(URL, self.path, original, session_factory=lambda: session)
        replay = media.download_episode_with_route(URL, self.path, session_factory=mock.Mock(side_effect=AssertionError("no HTTP")))
        self.assertTrue(replay["reused"])
        self.assertFalse(self.path.with_name("episode.mp4.route.json").exists())
        marker = json.loads(self.path.with_name("episode.mp4.download.json").read_text())
        self.assertEqual(marker["identity"]["kind"], "episode_download")

    def test_pending_routed_download_cannot_silently_become_an_original_url_job(self):
        with self.assertRaises(DramaSynthesisError):
            self.download([[Response([BODY[:4], requests.ConnectionError(URL)])], [Response([], 503)]])
        for route in (None, media.freeze_episode_download_route(self.source, "original")):
            with self.assertRaises(DramaSynthesisError) as caught:
                media.download_episode_with_route(self.source, self.path, route,
                                                  session_factory=mock.Mock(side_effect=AssertionError("no HTTP")))
            self.assertEqual(caught.exception.code, "drama_media_checkpoint_conflict")


def stream_info(width=360, height=640, audio=True, sample_aspect_ratio="1:1",
                video_updates=None, audio_updates=None):
    sar_numerator, sar_denominator = (int(value) for value in sample_aspect_ratio.split(":"))
    display_numerator, display_denominator = width * sar_numerator, height * sar_denominator
    divisor = math.gcd(display_numerator, display_denominator)
    video = {
        "codec_type": "video", "codec_name": "h264", "profile": "High", "level": 40,
        "pix_fmt": "yuv420p", "codec_tag_string": "avc1", "codec_tag": "0x31637661",
        "is_avc": "true", "nal_length_size": "4",
        "width": width, "height": height, "coded_width": width, "coded_height": height,
        "sample_aspect_ratio": sample_aspect_ratio,
        "display_aspect_ratio": "%d:%d" % (display_numerator // divisor, display_denominator // divisor),
        "field_order": "progressive",
        "color_range": "tv", "color_space": "bt709", "color_transfer": "bt709",
        "color_primaries": "bt709", "chroma_location": "left", "r_frame_rate": "25/1",
        "avg_frame_rate": "25/1", "time_base": "1/12800",
        "extradata": "00000000: 0164 0028", "extradata_size": 4,
    }
    video.update(video_updates or {})
    streams = [video]
    if audio:
        audio_stream = {
            "codec_type": "audio", "codec_name": "aac", "profile": "LC", "sample_fmt": "fltp",
            "sample_rate": "48000", "channels": 2, "channel_layout": "stereo",
            "codec_tag_string": "mp4a", "codec_tag": "0x6134706d", "bits_per_sample": 0,
            "time_base": "1/48000", "extradata": "00000000: 1190", "extradata_size": 2,
        }
        audio_stream.update(audio_updates or {})
        streams.append(audio_stream)
    return {"streams": streams}


def normalized_stream_info(plan, video_updates=None, audio_updates=None):
    target = plan["target"]
    updates = {
        "profile": target["video_profile"], "level": target["video_level"],
        "codec_tag_string": target["video_tag"], "codec_tag": target["video_tag_hex"],
        "width": target["width"], "height": target["height"],
        "coded_width": target["width"], "coded_height": target["height"],
        "sample_aspect_ratio": target["sample_aspect_ratio"],
        "display_aspect_ratio": target["display_aspect_ratio"],
        "field_order": target["field_order"], "pix_fmt": target["pix_fmt"],
        "color_range": target["color_range"], "color_space": target["color_space"],
        "color_transfer": target["color_transfer"], "color_primaries": target["color_primaries"],
        "chroma_location": target["chroma_location"], "r_frame_rate": target["frame_rate"],
        "avg_frame_rate": target["frame_rate"], "time_base": target["time_base"],
    }
    updates.update(video_updates or {})
    normalized_audio_updates = {
        "profile": plan["audio"]["profile"], "sample_fmt": plan["audio"]["sample_fmt"],
        "sample_rate": str(plan["audio"]["sample_rate"]), "channels": plan["audio"]["channels"],
        "channel_layout": plan["audio"]["channel_layout"], "codec_tag_string": plan["audio"]["tag"],
        "codec_tag": plan["audio"]["tag_hex"],
    }
    normalized_audio_updates.update(audio_updates or {})
    return stream_info(
        width=target["width"], height=target["height"], audio=True,
        sample_aspect_ratio=target["sample_aspect_ratio"], video_updates=updates,
        audio_updates=normalized_audio_updates,
    )


def jpeg_segment(marker, payload):
    return b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def jfif_jpeg(*extra_segments, include_jfif=True, comment_payload=b""):
    parts = [b"\xff\xd8"]
    if include_jfif:
        parts.append(jpeg_segment(0xE0, b"JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00"))
    if comment_payload:
        parts.append(jpeg_segment(0xFE, comment_payload))
    parts.extend(extra_segments)
    parts.append(jpeg_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00"))
    parts.append(b"\x00\xff\xd9")
    return b"".join(parts)


class ConcatSignatureTests(unittest.TestCase):
    def test_ffprobe_contract_requests_show_data_and_all_codec_configuration_fields(self):
        self.assertEqual(media.CONCAT_STREAM_PROBE_ARGS[:2], ("-show_data", "-show_entries"))
        required = {
            "profile", "level", "pix_fmt", "codec_tag_string", "codec_tag", "field_order",
            "is_avc", "nal_length_size",
            "color_range", "color_space", "color_transfer", "color_primaries", "chroma_location",
            "r_frame_rate", "avg_frame_rate", "time_base", "sample_fmt", "sample_rate", "channels",
            "channel_layout", "extradata", "extradata_size",
        }
        self.assertTrue(required.issubset(set(media.CONCAT_STREAM_PROBE_FIELDS)))
        self.assertEqual(media.CONCAT_STREAM_PROBE_ARGS[2], media.CONCAT_STREAM_SHOW_ENTRIES)

    def test_signature_hashes_extradata_and_any_required_field_missing_fails_closed(self):
        info = stream_info()
        signature = media.concat_signature(info)
        self.assertIsNotNone(signature)
        expected = hashlib.sha256(info["streams"][0]["extradata"].encode()).hexdigest()
        self.assertIn(expected, repr(signature))
        self.assertNotIn(info["streams"][0]["extradata"], repr(signature))
        required = (
            (0, "codec_name"), (0, "profile"), (0, "level"), (0, "pix_fmt"),
            (0, "codec_tag_string"), (0, "codec_tag"), (0, "width"), (0, "height"),
            (0, "is_avc"), (0, "nal_length_size"),
            (0, "coded_width"), (0, "coded_height"), (0, "sample_aspect_ratio"),
            (0, "display_aspect_ratio"), (0, "field_order"), (0, "color_range"),
            (0, "color_space"), (0, "color_transfer"), (0, "color_primaries"),
            (0, "chroma_location"), (0, "r_frame_rate"), (0, "avg_frame_rate"),
            (0, "time_base"), (0, "extradata"), (0, "extradata_size"),
            (1, "codec_name"), (1, "profile"), (1, "sample_fmt"), (1, "sample_rate"),
            (1, "channels"), (1, "channel_layout"), (1, "codec_tag_string"),
            (1, "codec_tag"), (1, "bits_per_sample"), (1, "time_base"),
            (1, "extradata"), (1, "extradata_size"),
        )
        for stream_index, field in required:
            with self.subTest(stream=stream_index, field=field):
                changed = json.loads(json.dumps(info))
                del changed["streams"][stream_index][field]
                self.assertIsNone(media.concat_signature(changed))

    def test_frozen_normalization_plan_uses_even_episode0_geometry_and_rejects_missing_color(self):
        reference = stream_info(width=361, height=641)
        source = stream_info(width=720, height=1280, audio=False, sample_aspect_ratio="2:1")
        plan = media.freeze_concat_normalization_plan(reference, source, 1)
        self.assertEqual((plan["target"]["width"], plan["target"]["height"]), (362, 642))
        self.assertEqual(plan["target"]["display_aspect_ratio"], "181:321")
        self.assertEqual(plan["audio"]["mode"], "silence")
        self.assertEqual(plan["source"]["sample_aspect_ratio"], "2:1")
        self.assertEqual(plan["source"]["field_order"], "progressive")
        self.assertEqual(plan["source"]["scan_mode"], "progressive")
        self.assertIn("bwdif", plan["profile"])
        self.assertIn("apad", plan["profile"])
        self.assertIsNotNone(media.validate_normalized_concat_info(normalized_stream_info(plan), plan))
        wrong = normalized_stream_info(plan, video_updates={"width": 360, "coded_width": 360})
        with self.assertRaises(DramaSynthesisError) as caught:
            media.validate_normalized_concat_info(wrong, plan)
        self.assertEqual(caught.exception.code, "drama_concat_normalization_invalid")

        missing = stream_info()
        del missing["streams"][0]["color_transfer"]
        with self.assertRaises(DramaSynthesisError) as caught:
            media.freeze_concat_normalization_plan(reference, missing, 1)
        self.assertEqual(caught.exception.code, "drama_concat_normalization_source_unsupported")

    def test_scan_policy_preserves_progressive_deinterlaces_known_orders_and_rejects_unknown(self):
        reference = stream_info()
        for field_order in ("tt", "bb", "tb", "bt"):
            with self.subTest(field_order=field_order):
                plan = media.freeze_concat_normalization_plan(
                    reference, stream_info(video_updates={"field_order": field_order}), 1,
                )
                self.assertEqual(plan["source"]["field_order"], field_order)
                self.assertEqual(plan["source"]["scan_mode"], "interlaced")
                self.assertEqual(
                    plan["source"]["deinterlace_parity"],
                    "tff" if field_order in ("tt", "bt") else "bff",
                )
                self.assertEqual(plan["target"]["field_order"], "progressive")
        for field_order in ("unknown", "", "N/A", "mixed"):
            with self.subTest(rejected=field_order), self.assertRaises(DramaSynthesisError) as caught:
                media.freeze_concat_normalization_plan(
                    reference, stream_info(video_updates={"field_order": field_order}), 1,
                )
            self.assertEqual(caught.exception.code, "drama_concat_normalization_source_unsupported")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.items = [{"episode_url": str(index), "source_path": str(self.root / (str(index) + ".mp4"))} for index in range(3)]
        self.normalized = []
        self.plans = {}

    def downloader(self, url, path, callback, **kwargs):
        Path(path).write_bytes(("source-" + url).encode())
        value = file_fingerprint(path)
        callback(value["size_bytes"], value["size_bytes"])
        return value

    def normalize(self, source, target, plan):
        self.normalized.append(Path(source).name)
        self.plans[str(target)] = plan
        Path(target).write_bytes(b"normalized-" + Path(source).read_bytes())

    def run_pipeline(self, **kwargs):
        return media.download_and_prepare_segments(self.items, output_dir=self.root / "normalized",
                                                  probe=lambda _: stream_info(), normalize=self.normalize,
                                                  downloader=self.downloader, **kwargs)

    def test_all_compatible_sources_keep_original_fast_path_and_input_order(self):
        metrics = []
        outputs = self.run_pipeline(download_workers=2, progress_callback=lambda stage, **values: metrics.append(values))
        self.assertEqual(outputs, [row["source_path"] for row in self.items])
        self.assertEqual(self.normalized, [])
        self.assertEqual(metrics[0]["total_bytes"], 0)
        self.assertEqual(metrics[-1]["total_bytes"], 24)

    def test_same_dimensions_and_rates_but_codec_configuration_difference_each_normalizes(self):
        cases = {
            "video-profile": stream_info(video_updates={"profile": "Main"}),
            "pixel-format": stream_info(video_updates={"pix_fmt": "yuv422p"}),
            "channel-layout": stream_info(audio_updates={"channel_layout": "2.0"}),
            "video-extradata": stream_info(video_updates={"extradata": "00000000: 0164 0029"}),
            "audio-extradata": stream_info(audio_updates={"extradata": "00000000: 1188"}),
        }
        for name, incompatible_info in cases.items():
            with self.subTest(name=name):
                case_root = self.root / name
                case_root.mkdir()
                output_dir = case_root / "normalized"
                items = [
                    {"episode_url": str(index), "source_path": str(case_root / (str(index) + ".mp4"))}
                    for index in range(2)
                ]
                normalized = []
                plans = {}

                def normalize(source, target, plan):
                    normalized.append(Path(source).name)
                    plans[str(target)] = plan
                    Path(target).write_bytes(b"normalized-" + Path(source).read_bytes())

                def probe(path):
                    if Path(path).parent == output_dir:
                        return normalized_stream_info(plans[str(path)])
                    return incompatible_info if Path(path).stem == "1" else stream_info()

                outputs = media.download_and_prepare_segments(
                    items, output_dir=output_dir, probe=probe, normalize=normalize, downloader=self.downloader,
                )
                self.assertCountEqual(normalized, ["0.mp4", "1.mp4"])
                self.assertTrue(all(Path(path).parent == output_dir for path in outputs))

    def test_missing_source_color_fails_closed_instead_of_relabeling_pixels(self):
        output_dir = self.root / "missing-color"
        bad = stream_info(video_updates={"profile": "Main"})
        del bad["streams"][0]["color_space"]

        def probe(path):
            target = Path(path)
            if target.parent == output_dir:
                return normalized_stream_info(self.plans[str(path)])
            return bad if target.stem == "1" else stream_info()

        with self.assertRaises(DramaSynthesisError) as caught:
            media.download_and_prepare_segments(
                self.items[:2], output_dir=output_dir, probe=probe,
                normalize=self.normalize, downloader=self.downloader,
            )
        self.assertEqual(caught.exception.code, "drama_concat_normalization_source_unsupported")

    def test_normalizer_never_starts_before_episode_zero_has_been_probed(self):
        release_zero = threading.Event()
        one_done = threading.Event()
        normalized = threading.Event()
        output_dir = self.root / "reference-gate"

        def download(url, path, callback, **kwargs):
            if url == "0":
                self.assertTrue(release_zero.wait(3))
            value = self.downloader(url, path, callback, **kwargs)
            if url == "1":
                one_done.set()
            return value

        def normalize(source, target, plan):
            normalized.set()
            self.normalize(source, target, plan)

        def probe(path):
            target = Path(path)
            if target.parent == output_dir:
                return normalized_stream_info(self.plans[str(path)])
            value = stream_info()
            if target.stem == "1":
                del value["streams"][0]["profile"]
            return value

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                media.download_and_prepare_segments,
                self.items[:2], output_dir=output_dir, probe=probe, normalize=normalize,
                downloader=download, download_workers=2,
            )
            self.assertTrue(one_done.wait(3))
            self.assertFalse(normalized.wait(0.1))
            release_zero.set()
            outputs = future.result(timeout=3)
        self.assertTrue(normalized.is_set())
        self.assertEqual(len(outputs), 2)

    def test_no_audio_segments_receive_silence_without_replacing_existing_audio(self):
        cases = (
            ("all-silent", [stream_info(audio=False), stream_info(audio=False)], None,
             ["silence", "silence"]),
            ("partial-silent", [stream_info(), stream_info(audio=False)], None,
             ["resample", "silence"]),
            ("intro-and-silent", [stream_info(audio=False), stream_info(audio=False)], stream_info(audio=False),
             ["silence", "silence", "silence"]),
        )
        for name, source_infos, intro_info, expected_modes in cases:
            with self.subTest(name=name):
                case_root = self.root / name
                case_root.mkdir()
                output_dir = case_root / "normalized"
                items = [
                    {"episode_url": str(index), "source_path": str(case_root / (str(index) + ".mp4"))}
                    for index in range(2)
                ]
                plans = {}
                modes = []
                intro = case_root / "intro.mp4"
                if intro_info is not None:
                    intro.write_bytes(b"intro")

                def normalize(source, target, plan):
                    plans[str(target)] = plan
                    modes.append((plan["segment_index"], plan["audio"]["mode"]))
                    Path(target).write_bytes(b"normalized-" + Path(source).read_bytes())

                def probe(path):
                    target = Path(path)
                    if target.parent == output_dir:
                        return normalized_stream_info(plans[str(path)])
                    if target == intro:
                        return intro_info
                    return source_infos[int(target.stem)]

                outputs = media.download_and_prepare_segments(
                    items, output_dir=output_dir, probe=probe, normalize=normalize,
                    downloader=self.downloader,
                    intro_factory=(lambda _: str(intro)) if intro_info is not None else None,
                )
                self.assertEqual([mode for _, mode in sorted(modes)], expected_modes)
                self.assertEqual(len(outputs), len(expected_modes))

    def test_inconsistent_or_incomplete_normalized_outputs_are_rejected(self):
        incomplete = stream_info()
        del incomplete["streams"][1]["channel_layout"]
        cases = {
            "inconsistent": stream_info(video_updates={"profile": "Main"}),
            "incomplete": incomplete,
        }
        for name, invalid_target in cases.items():
            with self.subTest(name=name):
                case_root = self.root / ("normalized-" + name)
                case_root.mkdir()
                output_dir = case_root / "out"
                items = [
                    {"episode_url": str(index), "source_path": str(case_root / (str(index) + ".mp4"))}
                    for index in range(2)
                ]
                plans = {}

                def normalize(source, target, plan):
                    plans[str(target)] = plan
                    Path(target).write_bytes(b"normalized-" + Path(source).read_bytes())

                def probe(path):
                    target = Path(path)
                    if target.parent == output_dir:
                        if target.stem == "001":
                            if name == "inconsistent":
                                return normalized_stream_info(plans[str(path)], video_updates={"profile": "Main"})
                            value = normalized_stream_info(plans[str(path)])
                            del value["streams"][1]["channel_layout"]
                            return value
                        return normalized_stream_info(plans[str(path)])
                    return stream_info(video_updates={"profile": "Main"}) if target.stem == "1" else stream_info()

                with self.assertRaises(DramaSynthesisError) as caught:
                    media.download_and_prepare_segments(
                        items, output_dir=output_dir, probe=probe, normalize=normalize, downloader=self.downloader,
                    )
                self.assertEqual(caught.exception.code, "drama_concat_normalization_invalid")

    def test_pipeline_passes_frozen_route_to_isolated_downloader_and_returns_canonical_episode_path(self):
        source = "https://img.tianmai.cn/resource/code/001.mp4"
        route = media.freeze_episode_download_route(source, "international")
        self.items = [{"episode_url": source, "source_path": str(self.root / "001.mp4"), "download_route": route}]
        session = Session([Response([BODY])])

        def downloader(url, path, callback, **kwargs):
            return media.download_episode(url, path, callback, session_factory=lambda: session, **kwargs)

        outputs = media.download_and_prepare_segments(self.items, output_dir=self.root / "normalized",
                                                     probe=lambda _: stream_info(), normalize=self.normalize,
                                                     downloader=downloader)
        self.assertEqual(session.urls, [route["primary_url"]])
        self.assertEqual(outputs, [self.items[0]["source_path"]])
        self.assertEqual(Path(outputs[0]).read_bytes(), BODY)
        self.assertEqual(self.normalized, [])

    def test_incompatibility_starts_one_normalizer_before_last_download_completes(self):
        normalized_early = threading.Event()
        active, peak = 0, 0
        guard = threading.Lock()

        def download(url, path, callback, **kwargs):
            if url == "2" and not normalized_early.wait(3):
                raise AssertionError("normalization waited for every download")
            return self.downloader(url, path, callback, **kwargs)

        def normalize(source, target, plan):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            self.normalize(source, target, plan)
            normalized_early.set()
            with guard:
                active -= 1

        def probe(path):
            if Path(path).parent == self.root / "normalized":
                return normalized_stream_info(self.plans[str(path)])
            return stream_info(720 if Path(path).stem == "1" else 360)

        outputs = media.download_and_prepare_segments(
            self.items, output_dir=self.root / "normalized", download_workers=2,
            probe=probe,
            normalize=normalize, downloader=download,
        )
        self.assertEqual(peak, 1)
        self.assertEqual([Path(path).read_bytes() for path in outputs], [b"normalized-source-0", b"normalized-source-1", b"normalized-source-2"])

    def test_intro_is_first_and_uses_the_first_episode_as_reference(self):
        intro = self.root / "intro.mp4"
        intro.write_bytes(b"intro")
        factory = mock.Mock(return_value=str(intro))
        def probe(path):
            if Path(path).parent == self.root / "normalized":
                return normalized_stream_info(self.plans[str(path)])
            return stream_info(1280 if path == str(intro) else 360)

        outputs = media.download_and_prepare_segments(
            self.items, output_dir=self.root / "normalized", probe=probe,
            normalize=self.normalize, downloader=self.downloader, intro_factory=factory,
        )
        factory.assert_called_once_with(self.items[0]["source_path"])
        self.assertEqual([Path(path).name for path in outputs], ["000.mp4", "001.mp4", "002.mp4", "003.mp4"])
        self.assertEqual(Path(outputs[0]).read_bytes(), b"normalized-intro")
        intro_plan = self.plans[outputs[0]]
        self.assertEqual(intro_plan["segment_index"], -1)
        self.assertEqual((intro_plan["target"]["width"], intro_plan["target"]["height"]), (360, 640))
        self.assertEqual(self.plans[outputs[1]]["segment_index"], 0)

    def test_single_segment_preserves_existing_fast_path_even_without_audio(self):
        outputs = media.download_and_prepare_segments(
            self.items[:1], output_dir=self.root / "normalized", probe=lambda _: stream_info(audio=False),
            normalize=self.normalize, downloader=self.downloader,
        )
        self.assertEqual(outputs, [self.items[0]["source_path"]])
        self.assertEqual(self.normalized, [])

    def test_normalized_checkpoints_replay_without_reencoding_and_reject_corruption(self):
        probed = []

        def probe(path):
            probed.append(str(path))
            if Path(path).parent == self.root / "normalized":
                return normalized_stream_info(self.plans[str(path)])
            return stream_info(720 if Path(path).stem == "0" else 360)

        kwargs = dict(output_dir=self.root / "normalized", probe=probe,
                      normalize=self.normalize, downloader=self.downloader)
        original = media.download_and_prepare_segments(self.items, **kwargs)
        self.normalized.clear()
        probed.clear()
        self.assertEqual(media.download_and_prepare_segments(self.items, **kwargs), original)
        self.assertEqual(self.normalized, [])
        self.assertTrue(all(path in probed for path in original))
        Path(original[0]).write_bytes(b"corrupt")
        with self.assertRaises(DramaSynthesisError):
            media.download_and_prepare_segments(self.items, **kwargs)
        self.assertEqual(self.normalized, [])

    def test_normalized_checkpoint_rejects_source_mutation_during_normalization(self):
        source = self.root / "mutable.mp4"
        target = self.root / "mutable-normalized.mp4"
        source.write_bytes(b"before")
        plan_holder = {}
        source_info, source_anchor = media.probe_media_source_with_anchor(
            source, lambda _: stream_info(),
        )

        def normalize(source_path, target_path, plan):
            plan_holder[str(target_path)] = plan
            Path(target_path).write_bytes(b"normalized")
            Path(source_path).write_bytes(b"after-")

        with self.assertRaises(DramaSynthesisError) as caught:
            media.prepare_normalized_concat_segment(
                source, target, source_info=source_info, source_anchor=source_anchor,
                reference_info=source_info, reference_source=source,
                reference_anchor=source_anchor, segment_index=0,
                normalize=normalize, probe=lambda path: normalized_stream_info(plan_holder[str(path)]),
            )
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_conflict")
        self.assertFalse(target.with_name(target.name + ".normalized.json").exists())

    def test_source_replaced_during_probe_fails_before_normalization_or_checkpoint(self):
        output_dir = self.root / "probe-race-normalized"

        def probe(path):
            source = Path(path)
            if source.parent == output_dir:
                raise AssertionError("normalized output must not be probed")
            if source.stem == "0":
                source.write_bytes(b"mutate-0")
            return stream_info(video_updates={"profile": "Main"})

        with self.assertRaises(DramaSynthesisError) as caught:
            media.download_and_prepare_segments(
                self.items[:2], output_dir=output_dir, probe=probe,
                normalize=self.normalize, downloader=self.downloader,
            )
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_conflict")
        self.assertEqual(self.normalized, [])
        self.assertEqual(list(output_dir.glob("*.normalized.json")), [])

    def test_runtime_context_is_propagated_to_download_and_normalizer_threads(self):
        from features.drama_synthesis import async_runtime
        local = threading.local()
        seen = []

        @contextmanager
        def use_context(context):
            local.context = context
            yield

        def download(url, path, callback, **kwargs):
            seen.append(("download", local.context))
            return self.downloader(url, path, callback, **kwargs)

        def normalize(source, target, plan):
            seen.append(("normalize", local.context))
            self.normalize(source, target, plan)

        def probe(path):
            if Path(path).parent == self.root / "normalized":
                return normalized_stream_info(self.plans[str(path)])
            return stream_info(720 if Path(path).stem == "0" else 360)

        with mock.patch.object(async_runtime, "capture_context", return_value="frozen-context"), mock.patch.object(async_runtime, "use_context", use_context), mock.patch.object(async_runtime, "emit_progress"):
            media.download_and_prepare_segments(self.items, output_dir=self.root / "normalized",
                                              probe=probe, normalize=normalize, downloader=download)
        self.assertEqual(len(seen), 6)
        self.assertTrue(all(context == "frozen-context" for _, context in seen))

    def test_download_failure_cancels_running_download_and_does_not_normalize(self):
        started = threading.Event()
        cancelled = threading.Event()

        def download(url, path, callback, **kwargs):
            if url == "0":
                started.set()
                self.assertTrue(kwargs["stop_event"].wait(3))
                cancelled.set()
                raise RuntimeError("download stopped")
            self.assertTrue(started.wait(3))
            raise DramaSynthesisError("download_failed", "下载失败", 502)

        with self.assertRaises(DramaSynthesisError):
            media.download_and_prepare_segments(self.items, output_dir=self.root / "normalized", download_workers=2,
                                              probe=lambda _: stream_info(), normalize=self.normalize, downloader=download)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.normalized, [])


class AppConcatCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def load(self, *names, **values):
        nodes = [node for node in self.app_tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
        self.assertEqual(len(nodes), len(names))
        env = dict(os=os)
        env.update(values)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), env)
        return env

    def test_legacy_concat_helper_uses_shared_complete_signature(self):
        sources = [self.root / "a.mp4", self.root / "b.mp4"]
        for index, source in enumerate(sources):
            source.write_bytes(("source-%d" % index).encode())
        probe = mock.Mock(side_effect=[stream_info(), stream_info()])
        shared = mock.Mock(wraps=media.concat_signature)
        env = self.load(
            "concat_segments_need_normalization",
            probe_media_stream_info=probe,
            probe_media_source_with_anchor=media.probe_media_source_with_anchor,
            verify_media_source_anchor=media.verify_media_source_anchor,
            drama_concat_signature=shared,
            concat_signatures_are_compatible=media.concat_signatures_are_compatible,
        )
        paths = [str(path) for path in sources]
        self.assertFalse(env["concat_segments_need_normalization"](paths))
        self.assertEqual(shared.call_count, 2)

        probe.side_effect = [stream_info(), stream_info(video_updates={"profile": "Main"})]
        self.assertTrue(env["concat_segments_need_normalization"](paths))

    def test_app_probe_passes_the_shared_show_data_contract_to_ffprobe(self):
        run = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout='{"streams": []}', stderr=""))
        process = SimpleNamespace(run=run, PIPE=object())
        env = self.load(
            "probe_media_stream_info",
            file_ready=lambda _: True,
            subprocess=process,
            ffprobe_path=lambda: "/fixed/ffprobe",
            CONCAT_STREAM_PROBE_ARGS=media.CONCAT_STREAM_PROBE_ARGS,
            json=json,
            logging=SimpleNamespace(warning=mock.Mock()),
        )
        self.assertEqual(env["probe_media_stream_info"]("segment.mp4"), {"streams": []})
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["/fixed/ffprobe", "-v"])
        self.assertEqual(command[2:5], ["error", *media.CONCAT_STREAM_PROBE_ARGS[:2]])
        self.assertIn(media.CONCAT_STREAM_SHOW_ENTRIES, command)

    def test_legacy_prepare_uses_durable_identity_reprobes_and_rejects_source_profile_or_order_change(self):
        sources = [self.root / "a.mp4", self.root / "b.mp4"]
        sources[0].write_bytes(b"source-a")
        sources[1].write_bytes(b"source-b")
        output_dir = self.root / "normalized"
        plans = {}
        normalized = []
        probed = []

        def normalize(source, target, plan):
            normalized.append(Path(source).name)
            plans[str(target)] = plan
            Path(target).write_bytes(b"normalized-" + Path(source).read_bytes())

        def probe(path):
            probed.append(str(path))
            target = Path(path)
            if target.parent == output_dir:
                return normalized_stream_info(plans[str(path)])
            return stream_info(video_updates={"profile": "Main"}) if target == sources[1] else stream_info()

        env = self.load(
            "prepare_concat_segments",
            ensure_dir=lambda path: Path(path).mkdir(parents=True, exist_ok=True),
            normalize_concat_segment=normalize,
            probe_media_stream_info=probe,
            drama_concat_signature=media.concat_signature,
            concat_signatures_are_compatible=media.concat_signatures_are_compatible,
            probe_media_source_with_anchor=media.probe_media_source_with_anchor,
            verify_media_source_anchor=media.verify_media_source_anchor,
            prepare_normalized_concat_segment=media.prepare_normalized_concat_segment,
            validate_normalized_concat_signatures=media.validate_normalized_concat_signatures,
            NORMALIZATION_PROFILE=media.NORMALIZATION_PROFILE,
        )
        outputs = env["prepare_concat_segments"]([str(path) for path in sources], str(output_dir))
        self.assertCountEqual(normalized, ["a.mp4", "b.mp4"])
        self.assertEqual(outputs, [str(output_dir / "000.mp4"), str(output_dir / "001.mp4")])
        normalized.clear()
        probed.clear()
        self.assertEqual(env["prepare_concat_segments"]([str(path) for path in sources], str(output_dir)), outputs)
        self.assertEqual(normalized, [])
        self.assertTrue(all(path in probed for path in outputs))

        changes = []
        sources[0].write_bytes(b"changed-source-a")
        changes.append(("source", [str(path) for path in sources], media.NORMALIZATION_PROFILE))
        sources[0].write_bytes(b"source-a")
        changes.append(("profile", [str(path) for path in sources], media.NORMALIZATION_PROFILE + "-next"))
        changes.append(("order", [str(sources[1]), str(sources[0])], media.NORMALIZATION_PROFILE))
        for name, ordered, profile in changes:
            with self.subTest(name=name):
                if name == "source":
                    sources[0].write_bytes(b"changed-source-a")
                else:
                    sources[0].write_bytes(b"source-a")
                env["NORMALIZATION_PROFILE"] = profile
                with self.assertRaises(DramaSynthesisError) as caught:
                    env["prepare_concat_segments"](ordered, str(output_dir))
                self.assertEqual(caught.exception.code, "drama_media_checkpoint_conflict")

    def test_legacy_prepare_rejects_probe_race_without_normalizing_or_persisting(self):
        sources = [self.root / "race-a.mp4", self.root / "race-b.mp4"]
        sources[0].write_bytes(b"source-a")
        sources[1].write_bytes(b"source-b")
        output_dir = self.root / "race-normalized"
        normalize = mock.Mock()

        def probe(path):
            source = Path(path)
            if source == sources[0]:
                source.write_bytes(b"mutate-a")
            return stream_info(video_updates={"profile": "Main"})

        env = self.load(
            "prepare_concat_segments",
            ensure_dir=lambda path: Path(path).mkdir(parents=True, exist_ok=True),
            normalize_concat_segment=normalize,
            probe_media_stream_info=probe,
            drama_concat_signature=media.concat_signature,
            concat_signatures_are_compatible=media.concat_signatures_are_compatible,
            probe_media_source_with_anchor=media.probe_media_source_with_anchor,
            verify_media_source_anchor=media.verify_media_source_anchor,
            prepare_normalized_concat_segment=media.prepare_normalized_concat_segment,
            validate_normalized_concat_signatures=media.validate_normalized_concat_signatures,
            NORMALIZATION_PROFILE=media.NORMALIZATION_PROFILE,
        )
        with self.assertRaises(DramaSynthesisError) as caught:
            env["prepare_concat_segments"]([str(path) for path in sources], str(output_dir))
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_conflict")
        normalize.assert_not_called()
        self.assertFalse(output_dir.exists())

    def test_normalizer_command_converts_colors_scales_pads_and_selects_real_or_silent_audio(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        output = Path(directory.name) / "normalized.mp4"
        runner = mock.Mock(side_effect=lambda command: Path(command[-1]).write_bytes(b"fake-media"))
        env = self.load(
            "normalize_concat_segment",
            FFMPEG="ffmpeg",
            ensure_dir=lambda path: Path(path).mkdir(parents=True, exist_ok=True),
            run_cmd=runner,
            video_encode_args=lambda: ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"],
            valid_video_file=lambda _: True,
            valid_av_duration_alignment=lambda _: True,
            validate_concat_normalization_plan=media.validate_concat_normalization_plan,
        )
        reference = stream_info(width=361, height=641)
        plan = media.freeze_concat_normalization_plan(reference, stream_info(), 0)
        env["normalize_concat_segment"]("source.mp4", str(output), plan)
        command = runner.call_args.args[0]
        joined = " ".join(command)
        self.assertIn("colorspace=ispace=bt709:itrc=bt709:iprimaries=bt709:irange=tv", joined)
        self.assertIn("space=bt709:trc=bt709:primaries=bt709:range=tv:format=yuv420p", joined)
        self.assertNotIn("setparams", joined)
        self.assertIn("scale=w=", joined)
        self.assertIn("pad=362:642:(ow-iw)/2:(oh-ih)/2:color=black", joined)
        self.assertIn("setsar=1", joined)
        video_filter = command[command.index("-vf") + 1]
        self.assertLess(video_filter.index("scale="), video_filter.index("colorspace="))
        self.assertLess(video_filter.index("colorspace="), video_filter.index("pad="))
        self.assertNotIn("bwdif=", joined)
        self.assertNotIn("setfield", joined)
        self.assertEqual(command[command.index("-map", command.index("-map") + 1) + 1], "0:a:0")
        self.assertNotIn("anullsrc", joined)
        self.assertEqual(command[command.index("-af") + 1], "aresample=async=1:first_pts=0,apad")
        self.assertIn("-shortest", command)
        for option, value in (
            ("-color_range", "tv"), ("-colorspace", "bt709"), ("-color_trc", "bt709"),
            ("-color_primaries", "bt709"), ("-chroma_sample_location", "left"),
            ("-profile:v", "high"), ("-level:v", "4.1"), ("-pix_fmt", "yuv420p"),
            ("-tag:v", "avc1"), ("-video_track_timescale", "12800"),
            ("-profile:a", "aac_low"), ("-sample_fmt", "fltp"), ("-tag:a", "mp4a"),
        ):
            index = command.index(option)
            self.assertEqual(command[index + 1], value)

        runner.reset_mock()
        silent_output = self.root / "silent.mp4"
        silent_plan = media.freeze_concat_normalization_plan(reference, stream_info(audio=False), 1)
        env["normalize_concat_segment"]("silent-source.mp4", str(silent_output), silent_plan)
        silent_command = runner.call_args.args[0]
        self.assertIn("anullsrc=r=48000:cl=stereo", silent_command)
        maps = [silent_command[index + 1] for index, value in enumerate(silent_command[:-1]) if value == "-map"]
        self.assertEqual(maps, ["0:v:0", "1:a:0"])
        self.assertEqual(silent_command[silent_command.index("-af") + 1], "aresample=async=1:first_pts=0")
        self.assertIn("-shortest", silent_command)

        for field_order, parity in (("tt", "tff"), ("bt", "tff"), ("bb", "bff"), ("tb", "bff")):
            with self.subTest(field_order=field_order):
                runner.reset_mock()
                interlaced_output = self.root / ("interlaced-%s.mp4" % field_order)
                interlaced_plan = media.freeze_concat_normalization_plan(
                    reference, stream_info(video_updates={"field_order": field_order}), 2,
                )
                env["normalize_concat_segment"](
                    "interlaced-source.mp4", str(interlaced_output), interlaced_plan,
                )
                interlaced_command = runner.call_args.args[0]
                interlaced_filter = interlaced_command[interlaced_command.index("-vf") + 1]
                self.assertTrue(interlaced_filter.startswith(
                    "bwdif=mode=send_frame:parity=%s:deint=all," % parity,
                ))
                self.assertNotIn("setfield", interlaced_filter)

    def test_intro_command_converts_image_pixels_and_writes_canonical_bt709_streams(self):
        output = self.root / "intro.mp4"
        cover = self.root / "cover.jpg"
        original_cover = jfif_jpeg()
        cover.write_bytes(original_cover)
        frozen_sources = []

        def run(command):
            frozen_path = Path(command[command.index("-i") + 1])
            self.assertNotEqual(frozen_path, cover)
            self.assertTrue(frozen_path.is_file())
            frozen_sources.append((frozen_path, frozen_path.read_bytes()))
            cover.write_bytes(jfif_jpeg(comment_payload=b"replacement-after-validation"))
            Path(command[-1]).write_bytes(b"fake-intro")

        runner = mock.Mock(side_effect=run)
        env = self.load(
            "validate_intro_cover_color_contract", "freeze_intro_cover_source", "render_intro",
            FFMPEG="ffmpeg",
            INTRO_SECONDS=5,
            probe_intro_reference_timing=lambda _: {"fps": "25", "audio_rate": "48000"},
            ensure_dir=lambda path: Path(path).mkdir(parents=True, exist_ok=True),
            run_cmd=runner,
            video_encode_args=lambda: ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"],
            valid_video_file=lambda _: True,
            valid_av_duration_alignment=lambda _: True,
            logging=SimpleNamespace(info=mock.Mock()),
            tempfile=tempfile,
            shutil=shutil,
            file_fingerprint=file_fingerprint,
        )
        env["render_intro"](str(cover), str(output), reference_path="episode0.mp4")
        command = runner.call_args.args[0]
        self.assertEqual(len(frozen_sources), 1)
        self.assertEqual(frozen_sources[0][1], original_cover)
        self.assertFalse(frozen_sources[0][0].exists())
        self.assertEqual(list(self.root.glob(".intro-cover-*.jpg")), [])
        video_filter = command[command.index("-vf") + 1]
        self.assertIn("in_range=pc:out_range=tv", video_filter)
        self.assertIn("in_color_matrix=bt470:out_color_matrix=bt709", video_filter)
        self.assertIn("force_divisible_by=2", video_filter)
        self.assertIn("colorspace=ispace=bt709:itrc=iec61966-2-1:iprimaries=bt709:irange=tv", video_filter)
        self.assertNotIn("auto", video_filter)
        self.assertIn("pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black", video_filter)
        self.assertIn("format=yuv420p:fast=0", video_filter)
        self.assertTrue(video_filter.endswith("setsar=1"))
        self.assertNotIn("setparams", video_filter)
        for option, value in (
            ("-profile:v", "high"), ("-level:v", "4.1"), ("-pix_fmt", "yuv420p"),
            ("-tag:v", "avc1"), ("-video_track_timescale", "12800"),
            ("-color_range", "tv"), ("-colorspace", "bt709"), ("-color_trc", "bt709"),
            ("-color_primaries", "bt709"), ("-chroma_sample_location", "left"),
            ("-profile:a", "aac_low"), ("-sample_fmt", "fltp"), ("-tag:a", "mp4a"),
        ):
            index = command.index(option)
            self.assertEqual(command[index + 1], value)

        unsupported = self.root / "cover.png"
        unsupported.write_bytes(b"\x89PNG\r\n\x1a\n")
        runner.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "intro cover color contract unsupported"):
            env["render_intro"](str(unsupported), str(self.root / "unsupported.mp4"))
        runner.assert_not_called()

    def test_intro_jpeg_parser_rejects_fake_comment_icc_and_adobe_markers(self):
        env = self.load("validate_intro_cover_color_contract")
        valid = self.root / "valid.jpg"
        valid.write_bytes(jfif_jpeg())
        self.assertEqual(env["validate_intro_cover_color_contract"](str(valid))["matrix"], "bt470")

        cases = {
            "fake-jfif-comment": jfif_jpeg(
                include_jfif=False, comment_payload=b"JFIF\x00not-an-app0-marker",
            ),
            "icc-profile": jfif_jpeg(jpeg_segment(0xE2, b"ICC_PROFILE\x00\x01\x01fake")),
            "adobe-app14": jfif_jpeg(jpeg_segment(0xEE, b"Adobe\x00d840000000")),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                path = self.root / (name + ".jpg")
                path.write_bytes(payload)
                with self.assertRaisesRegex(RuntimeError, "intro cover color contract unsupported"):
                    env["validate_intro_cover_color_contract"](str(path))

    def test_existing_intro_is_anchored_and_legacy_failure_preserves_bytes(self):
        intro = self.root / "000_intro.mp4"
        reference_path = self.root / "001.mp4"
        intro.write_bytes(b"legacy-intro")
        reference_path.write_bytes(b"reference")
        reference = stream_info()
        legacy = stream_info(video_updates={
            "color_transfer": None, "color_primaries": None,
        })
        for item in legacy["streams"]:
            item["duration"] = "1.0"
        legacy["format"] = {"duration": "1.0"}
        probe = mock.Mock(side_effect=[reference, legacy])
        env = self.load(
            "validate_intro_for_reference",
            INTRO_SECONDS=1,
            file_ready=lambda path: Path(path).is_file() and Path(path).stat().st_size > 0,
            probe_media_stream_info=probe,
            probe_media_source_with_anchor=media.probe_media_source_with_anchor,
            verify_media_source_anchor=media.verify_media_source_anchor,
            freeze_concat_normalization_plan=media.freeze_concat_normalization_plan,
            checkpoint_error=checkpoint_error,
            DramaSynthesisError=DramaSynthesisError,
            math=math,
        )
        with self.assertRaises(DramaSynthesisError) as caught:
            env["validate_intro_for_reference"](str(intro), str(reference_path))
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_unverified")
        self.assertEqual(intro.read_bytes(), b"legacy-intro")

        current = stream_info()
        for item in current["streams"]:
            item["duration"] = "1.0"
        current["format"] = {"duration": "1.0"}
        probe.side_effect = [reference, current]
        anchor = env["validate_intro_for_reference"](str(intro), str(reference_path))
        self.assertEqual(anchor["sha256"], file_fingerprint(intro)["sha256"])
        self.assertEqual(intro.read_bytes(), b"legacy-intro")

        invalid_duration = stream_info()
        for item in invalid_duration["streams"]:
            item["duration"] = "1.0"
        invalid_duration["format"] = {"duration": "nan"}
        probe.side_effect = [reference, invalid_duration]
        with self.assertRaises(DramaSynthesisError) as caught:
            env["validate_intro_for_reference"](str(intro), str(reference_path))
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_unverified")
        self.assertEqual(intro.read_bytes(), b"legacy-intro")

    def test_existing_intro_invalid_or_changed_enters_checkpoint_recovery(self):
        intro = self.root / "000_intro.mp4"
        reference_path = self.root / "001.mp4"
        intro.write_bytes(b"")
        reference_path.write_bytes(b"reference")
        env = self.load(
            "validate_intro_for_reference",
            INTRO_SECONDS=1,
            file_ready=lambda path: Path(path).is_file() and Path(path).stat().st_size > 0,
            probe_media_stream_info=mock.Mock(),
            probe_media_source_with_anchor=media.probe_media_source_with_anchor,
            verify_media_source_anchor=media.verify_media_source_anchor,
            freeze_concat_normalization_plan=media.freeze_concat_normalization_plan,
            checkpoint_error=checkpoint_error,
            DramaSynthesisError=DramaSynthesisError,
            math=math,
        )
        with self.assertRaises(DramaSynthesisError) as caught:
            env["validate_intro_for_reference"](str(intro), str(reference_path))
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_unverified")
        self.assertTrue(intro.exists())
        self.assertEqual(intro.read_bytes(), b"")

        intro.write_bytes(b"intro")
        values = iter([stream_info(), stream_info()])

        def mutate_intro(path):
            value = next(values)
            if Path(path) == intro:
                intro.write_bytes(b"changed-during-probe")
            if Path(path) == intro:
                for item in value["streams"]:
                    item["duration"] = "1.0"
                value["format"] = {"duration": "1.0"}
            return value

        env["probe_media_stream_info"] = mutate_intro
        with self.assertRaises(DramaSynthesisError) as caught:
            env["validate_intro_for_reference"](str(intro), str(reference_path))
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_conflict")
        self.assertEqual(intro.read_bytes(), b"changed-during-probe")

    def test_strict_job_directory_rejects_escape_before_creating_any_path(self):
        root = self.root / "jobs"
        root.mkdir()
        env = self.load(
            "strict_drama_job_directory",
            checkpoint_error=checkpoint_error,
            DramaSynthesisError=DramaSynthesisError,
            drama_async_runtime=SimpleNamespace(
                valid_job_id=lambda value: isinstance(value, str)
                and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value)),
            ),
        )
        self.assertEqual(
            env["strict_drama_job_directory"](str(root), "safe-job_1"),
            str(root / "safe-job_1"),
        )
        with self.assertRaises(ValueError):
            env["strict_drama_job_directory"](str(root), "../outside")
        for invalid in (123, True, " safe-job_1", "safe-job_1 "):
            with self.assertRaises(ValueError):
                env["strict_drama_job_directory"](str(root), invalid)
        self.assertFalse((self.root / "outside").exists())

        realpath = os.path.realpath
        with mock.patch.object(
                os.path, "realpath",
                side_effect=lambda path: str(self.root / "outside")
                if os.path.abspath(path) == os.path.abspath(root) else realpath(path)):
            with self.assertRaises(DramaSynthesisError):
                env["strict_drama_job_directory"](str(root), "safe-job_2")
        self.assertFalse((root / "safe-job_2").exists())


def recipe():
    value = {"profile": RECIPE_PROFILE, "version": 1, "source": "concat_video", "assets": {},
             "asset_set_sha256": "b" * 64, "rotation_millidegrees": 1000, "scale_bp": 10000, "tint_opacity_bp": 1000}
    value["recipe_sha256"] = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return value


class RenderCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source, self.output = self.root / "source.mp4", self.root / "result.mp4"
        self.source.write_bytes(b"source-video")
        self.kwargs = dict(source=self.source, output=self.output, recipe=recipe(), asset_root=self.root,
                           manifest_sha256="b" * 64)
        self.result_info = {"duration": 5.0, "has_audio": True, "audio": {},
                            "video": {"codec_name": "h264", "profile": "High", "width": 720, "height": 1280, "duration": "5.0"}}
        self.runner = mock.Mock(side_effect=lambda command, **_: Path(command[-1]).write_bytes(b"complete-render"))
        self.patches = [mock.patch.object(gpu, "load_asset_set", return_value={}),
                        mock.patch.object(gpu, "validate_recipe"),
                        mock.patch.object(gpu, "selected_asset_paths", return_value={}),
                        mock.patch.object(gpu, "_probe", side_effect=lambda _, path: {**self.result_info, "duration": 5.0} if Path(path) == self.source else self.result_info),
                        mock.patch.object(gpu, "build_command", side_effect=lambda config, source, output, *_: [
                            "ffmpeg", "-y", "-i", str(source), "-filter_complex", "[0:v]setpts=PTS-STARTPTS,fps=30[v]",
                            "-c:v", "h264_nvenc", "-profile:v", "high", "-cq", "21", str(output)])]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def render(self):
        return gpu.render_random_output(**self.kwargs, runner=self.runner)

    def test_upload_retry_reuses_verified_local_render_without_running_encoder(self):
        result = self.render()
        before = self.output.stat().st_mtime_ns
        self.runner.reset_mock()
        self.assertEqual(self.render(), result)
        self.runner.assert_not_called()
        self.assertEqual(self.output.stat().st_mtime_ns, before)
        self.assertEqual(result["output_sha256"], hashlib.sha256(b"complete-render").hexdigest())

    def test_recipe_or_source_change_conflicts_without_deleting_existing_output(self):
        self.render()
        original = self.output.read_bytes()
        self.source.write_bytes(b"changed-source")
        self.runner.reset_mock()
        with self.assertRaises(DramaSynthesisError) as caught:
            self.render()
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_conflict")
        self.assertEqual(self.output.read_bytes(), original)
        self.runner.assert_not_called()

    def test_changed_frozen_recipe_is_not_reinterpreted_as_a_cache_miss(self):
        self.render()
        changed = dict(self.kwargs["recipe"])
        changed.pop("recipe_sha256")
        changed["rotation_millidegrees"] = 2000
        changed["recipe_sha256"] = hashlib.sha256(json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.kwargs["recipe"] = changed
        self.runner.reset_mock()
        with self.assertRaises(DramaSynthesisError) as caught:
            self.render()
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_conflict")
        self.runner.assert_not_called()

    def test_corrupt_manifest_output_or_result_sha_never_rerenders(self):
        self.render()
        marker = self.output.with_name("result.mp4.render.json")
        original_record, original_output = marker.read_bytes(), self.output.read_bytes()
        for kind in ("json", "file", "reported_sha"):
            marker.write_bytes(original_record)
            self.output.write_bytes(original_output)
            if kind == "json":
                marker.write_text("{broken")
            elif kind == "file":
                self.output.write_bytes(b"xxxxxxxxxxxxxxx")
            else:
                value = json.loads(original_record)
                value["result"]["output_sha256"] = "0" * 64
                marker.write_text(json.dumps(value))
            self.runner.reset_mock()
            with self.subTest(kind=kind), self.assertRaises(DramaSynthesisError):
                self.render()
            self.runner.assert_not_called()

    def test_untracked_existing_output_is_preserved_without_adoption_or_rerender(self):
        self.output.write_bytes(b"legacy-unverified")
        with self.assertRaises(DramaSynthesisError) as caught:
            self.render()
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_unverified")
        self.assertEqual(self.output.read_bytes(), b"legacy-unverified")
        self.runner.assert_not_called()
        self.assertFalse(self.output.with_name("result.mp4.render.json").exists())

    def test_bad_duration_or_codec_does_not_promote_a_new_output(self):
        for kind in ("duration", "codec"):
            self.result_info["video"]["duration"] = "3.9" if kind == "duration" else "5.0"
            self.result_info["video"]["codec_name"] = "hevc" if kind == "codec" else "h264"
            with self.subTest(kind=kind), self.assertRaises(DramaSynthesisError):
                self.render()
            self.assertFalse(self.output.exists())
            self.assertFalse(self.output.with_name("result.mp4.render.json").exists())
            self.assertFalse(self.output.with_name("result.mp4.render.prepared.json").exists())
            self.assertEqual(list(self.root.glob(".random-render-*.mp4")), [])

    def test_final_checkpoint_write_failure_recovers_renamed_render_without_encoding(self):
        with mock.patch.object(gpu, "save_completed", side_effect=OSError("simulated checkpoint disk failure")):
            with self.assertRaises(DramaSynthesisError):
                self.render()
        prepared = self.output.with_name("result.mp4.render.prepared.json")
        marker = self.output.with_name("result.mp4.render.json")
        self.assertTrue(prepared.is_file())
        self.assertFalse(marker.exists())
        self.assertEqual(self.output.read_bytes(), b"complete-render")
        before = self.output.stat().st_mtime_ns
        self.runner.reset_mock()
        result = self.render()
        self.runner.assert_not_called()
        self.assertEqual(result["output_sha256"], hashlib.sha256(b"complete-render").hexdigest())
        self.assertEqual(self.output.stat().st_mtime_ns, before)
        self.assertTrue(marker.is_file())
        self.assertFalse(prepared.exists())

    def test_artifact_rename_failure_recovers_durable_temporary_render_without_encoding(self):
        replace = os.replace

        def fail_output_rename(source, target):
            if Path(target) == self.output:
                raise OSError("simulated rename failure")
            return replace(source, target)

        with mock.patch.object(gpu.os, "replace", side_effect=fail_output_rename):
            with self.assertRaises(DramaSynthesisError):
                self.render()
        self.assertFalse(self.output.exists())
        prepared = self.output.with_name("result.mp4.render.prepared.json")
        saved = json.loads(prepared.read_text())
        self.assertEqual((self.root / saved["temporary_name"]).read_bytes(), b"complete-render")
        self.runner.reset_mock()
        self.render()
        self.runner.assert_not_called()
        self.assertEqual(self.output.read_bytes(), b"complete-render")
        self.assertFalse(prepared.exists())
        self.assertEqual(list(self.root.glob(".random-render-*.mp4")), [])

    def test_crash_after_prepared_record_or_after_final_record_is_recoverable(self):
        for stage in ("prepared", "completed"):
            with self.subTest(stage=stage):
                self.output = self.root / (stage + ".mp4")
                self.kwargs["output"] = self.output
                original = gpu.atomic_write_record if stage == "prepared" else gpu.save_completed

                def write_then_crash(*args, **kwargs):
                    original(*args, **kwargs)
                    if stage == "prepared" and args[1].get("result") is None:
                        return
                    raise OSError("simulated post-write crash")

                function = "atomic_write_record" if stage == "prepared" else "save_completed"
                with mock.patch.object(gpu, function, side_effect=write_then_crash):
                    with self.assertRaises(DramaSynthesisError):
                        self.render()
                self.assertTrue(self.output.with_name(self.output.name + ".render.prepared.json").is_file())
                self.runner.reset_mock()
                self.render()
                self.runner.assert_not_called()
                self.assertEqual(self.output.read_bytes(), b"complete-render")
                self.assertFalse(self.output.with_name(self.output.name + ".render.prepared.json").exists())

    def test_failed_validated_prepare_write_preserves_artifact_and_blocks_automatic_rerender(self):
        original = gpu.atomic_write_record

        def fail_validated_prepare(path, value):
            if value["result"] is not None:
                raise OSError("simulated prepared-record disk failure")
            return original(path, value)

        with mock.patch.object(gpu, "atomic_write_record", side_effect=fail_validated_prepare):
            with self.assertRaises(DramaSynthesisError):
                self.render()
        prepared = self.output.with_name("result.mp4.render.prepared.json")
        guard = json.loads(prepared.read_text())
        self.assertIsNone(guard["result"])
        self.assertEqual((self.root / guard["temporary_name"]).read_bytes(), b"complete-render")
        self.runner.reset_mock()
        with self.assertRaises(DramaSynthesisError):
            self.render()
        self.runner.assert_not_called()
        self.assertEqual((self.root / guard["temporary_name"]).read_bytes(), b"complete-render")

    def test_prepared_hash_identity_or_path_corruption_never_adopts_or_reencodes(self):
        with mock.patch.object(gpu, "save_completed", side_effect=OSError("simulated checkpoint failure")):
            with self.assertRaises(DramaSynthesisError):
                self.render()
        prepared = self.output.with_name("result.mp4.render.prepared.json")
        original = prepared.read_text()
        for kind in ("file", "identity", "temporary_path", "reported_sha", "version"):
            value = json.loads(original)
            self.output.write_bytes(b"complete-render")
            if kind == "file":
                self.output.write_bytes(b"xxxxxxxxxxxxxxx")
            elif kind == "identity":
                value["identity"]["source"]["sha256"] = "0" * 64
            elif kind == "temporary_path":
                value["temporary_name"] = "../other.mp4"
            elif kind == "reported_sha":
                value["result"]["output_sha256"] = "0" * 64
            else:
                value["version"] = True
            prepared.write_text(json.dumps(value))
            self.runner.reset_mock()
            with self.subTest(kind=kind), self.assertRaises(DramaSynthesisError):
                self.render()
            self.runner.assert_not_called()
            self.assertFalse(self.output.with_name("result.mp4.render.json").exists())

    def test_render_deadline_default_env_and_explicit_values_above_four_hours_reach_runner(self):
        cases = [(None, None, 43200), ("21600", None, 43200), ("120", 36000, 43200), (None, 86400, 86400)]
        for index, (environment, explicit, expected) in enumerate(cases):
            self.kwargs["output"] = self.root / ("timeout-%s.mp4" % index)
            self.kwargs["timeout"] = explicit
            values = {} if environment is None else {"DRAMA_GPU_RENDER_TIMEOUT": environment}
            with mock.patch.dict(os.environ, values, clear=True):
                self.render()
            self.assertEqual(self.runner.call_args.kwargs["timeout"], expected)
        self.kwargs["output"] = self.root / "tracked-default.mp4"
        self.kwargs["timeout"] = None
        with mock.patch.dict(os.environ, {"DRAMA_GPU_RENDER_TIMEOUT": "28800"}), mock.patch.object(gpu, "run_render_with_progress", side_effect=lambda command, **_: Path(command[-1]).write_bytes(b"complete-render")) as tracked:
            gpu.render_random_output(**self.kwargs)
        self.assertEqual(tracked.call_args.kwargs["timeout"], 43200)
        self.assertEqual(tracked.call_args.kwargs["configured_timeout"], 28800)
        self.assertEqual(tracked.call_args.kwargs["absolute_timeout"], 86400)

    def test_probe_duration_drives_conservative_absolute_budget_with_margin_and_cap(self):
        # The absolute deadline uses a 0.10x planning floor, a 1.25 safety
        # ratio and 30 minutes.  A caller's shorter transport-style timeout cannot
        # truncate a long render, while an explicitly larger safe timeout is
        # retained and the process lifetime remains capped at 24 hours.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gpu.render_budget_seconds(5400, configured_timeout=60), 69300)
            self.assertEqual(gpu.render_budget_seconds(300, configured_timeout=43200), 43200)
            self.assertEqual(gpu.render_budget_seconds(22000, configured_timeout=60), 86400)
            for duration in (True, 0, -1, float("nan"), float("inf"), "5400"):
                with self.subTest(duration=duration), self.assertRaises(DramaSynthesisError):
                    gpu.render_budget_seconds(duration, configured_timeout=60)
            for configured in (True, 0, 59, 86401, -1, 60.5, "auto", float("inf")):
                with self.subTest(configured=configured), self.assertRaises(DramaSynthesisError):
                    gpu.render_budget_seconds(5400, configured_timeout=configured)

    def test_probe_duration_budget_reaches_both_native_and_injected_runners(self):
        long_info = {
            **self.result_info,
            "duration": 5400.0,
            "video": {**self.result_info["video"], "duration": "5400.0"},
        }
        probe = lambda _, path: long_info
        self.kwargs["timeout"] = 60
        self.kwargs["output"] = self.root / "long-injected.mp4"
        with mock.patch.object(gpu, "_probe", side_effect=probe):
            gpu.render_random_output(**self.kwargs, runner=self.runner)
        self.assertEqual(self.runner.call_args.kwargs["timeout"], 69300)

        self.kwargs["output"] = self.root / "long-native.mp4"
        with mock.patch.object(gpu, "_probe", side_effect=probe), mock.patch.object(
            gpu, "run_render_with_progress",
            side_effect=lambda command, **_: Path(command[-1]).write_bytes(b"complete-render"),
        ) as tracked:
            gpu.render_random_output(**self.kwargs)
        self.assertEqual(tracked.call_args.kwargs["timeout"], 69300)
        self.assertEqual(tracked.call_args.kwargs["configured_timeout"], 60)
        self.assertEqual(tracked.call_args.kwargs["absolute_timeout"], 86400)

    def test_timeout_and_process_failure_leave_only_private_bounded_safe_sidecar(self):
        secret_url = URL + "&access_token=credential-never-persist"
        private_stderr = ("Authorization: Bearer credential-never-persist\n" + secret_url + "\n") * 4096
        cases = (
            ("timeout", subprocess.TimeoutExpired(["ffmpeg", "-i", secret_url], 60,
                                                  output="private stdout", stderr=private_stderr),
             "drama_random_render_timeout", None, None),
            ("exit-137", subprocess.CalledProcessError(137, ["ffmpeg", "-i", secret_url],
                                                        output="private stdout", stderr=private_stderr),
             "drama_random_render_failed", 137, None),
            ("signal-9", subprocess.CalledProcessError(-9, ["ffmpeg", "-i", secret_url],
                                                        output="private stdout", stderr=private_stderr),
             "drama_random_render_failed", -9, 9),
        )
        for name, failure, expected_code, expected_returncode, expected_signal in cases:
            with self.subTest(name=name):
                output = self.root / (name + ".mp4")
                self.kwargs["output"] = output
                diagnostic = output.with_name("." + output.name + ".render.failure.json")
                diagnostic.write_text("legacy world-readable diagnostic", encoding="utf-8")
                if os.name == "posix":
                    diagnostic.chmod(0o644)

                def fail(command, **_):
                    Path(command[-1]).write_bytes(b"unverified-partial-media")
                    raise failure

                tracked_atomic = mock.Mock(wraps=gpu.atomic_write_record)
                with mock.patch.object(gpu, "atomic_write_record", tracked_atomic), \
                        self.assertRaises(DramaSynthesisError) as caught:
                    gpu.render_random_output(**self.kwargs, runner=fail)
                self.assertEqual(caught.exception.code, expected_code)
                self.assertTrue(diagnostic.is_file())
                if os.name == "posix":
                    self.assertEqual(stat.S_IMODE(diagnostic.stat().st_mode), 0o600)
                self.assertLessEqual(diagnostic.stat().st_size, 65536)
                payload = json.loads(diagnostic.read_text(encoding="utf-8"))
                self.assertEqual(payload["version"], 1)
                self.assertEqual(payload["public_code"], expected_code)
                self.assertEqual(payload["process"], {
                    "returncode": expected_returncode, "signal": expected_signal,
                })
                stderr_evidence = payload["stderr"]
                self.assertEqual(stderr_evidence["bytes"], len(private_stderr.encode("utf-8")))
                self.assertRegex(stderr_evidence["sha256"], r"^[0-9a-f]{64}$")
                self.assertFalse(stderr_evidence["truncated"])
                self.assertFalse(stderr_evidence["raw_stored"])
                self.assertTrue(stderr_evidence["encoding_transformed"])
                encoded = json.dumps(payload, ensure_ascii=False)
                for private in ("credential-never-persist", "private stdout", "media.example.test",
                                "ffmpeg -i", private_stderr[:80]):
                    self.assertNotIn(private, encoded)
                self.assertTrue(any(Path(call.args[0]) == diagnostic for call in tracked_atomic.call_args_list))
                self.assertEqual(list(self.root.glob("." + diagnostic.name + ".*")), [])
                self.assertFalse(output.exists())
                self.assertFalse(output.with_name(output.name + ".render.json").exists())
                self.assertFalse(output.with_name(output.name + ".render.prepared.json").exists())
                self.assertEqual(list(self.root.glob(".random-render-*.mp4")), [])

    def test_diagnostic_write_failure_preserves_guard_and_partial_and_blocks_reencode(self):
        output = self.root / "diagnostic-io-failure.mp4"
        self.kwargs["output"] = output
        diagnostic = output.with_name("." + output.name + ".render.failure.json")
        real_atomic_write = gpu.atomic_write_record

        def fail_diagnostic(path, value):
            if Path(path) == diagnostic:
                raise OSError("simulated diagnostic disk failure " + URL)
            return real_atomic_write(path, value)

        def fail_render(command, **_):
            Path(command[-1]).write_bytes(b"unverified-partial-media")
            raise subprocess.TimeoutExpired(["ffmpeg", "-i", URL], 60, stderr="private stderr")

        with mock.patch.object(gpu, "atomic_write_record", side_effect=fail_diagnostic):
            with self.assertRaises(DramaSynthesisError) as caught:
                gpu.render_random_output(**self.kwargs, runner=fail_render)
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_unverified")
        guard_path = output.with_name(output.name + ".render.prepared.json")
        guard = json.loads(guard_path.read_text(encoding="utf-8"))
        self.assertIsNone(guard["artifact"])
        self.assertIsNone(guard["result"])
        partial = output.parent / guard["temporary_name"]
        self.assertEqual(partial.read_bytes(), b"unverified-partial-media")
        self.assertFalse(output.exists())
        self.assertFalse(output.with_name(output.name + ".render.json").exists())
        self.assertFalse(diagnostic.exists())

        retry = mock.Mock(side_effect=AssertionError("unverified partial must block re-encoding"))
        with self.assertRaises(DramaSynthesisError) as replay:
            gpu.render_random_output(**self.kwargs, runner=retry)
        self.assertEqual(replay.exception.code, "drama_media_checkpoint_unverified")
        retry.assert_not_called()
        self.assertEqual(partial.read_bytes(), b"unverified-partial-media")

    def test_post_render_validation_failures_are_diagnosed_without_promoting_partial_media(self):
        original_fingerprint = gpu.file_fingerprint
        cases = (
            ("probe", "post_render_probe_failed", "drama_random_probe_failed", False),
            ("duration", "duration_mismatch", "drama_random_duration_mismatch", False),
            ("codec", "output_contract_invalid", "drama_random_output_contract_invalid", False),
            ("fingerprint", "fingerprint_failed", "drama_media_checkpoint_unverified", True),
        )
        for name, reason, public_code, preserve in cases:
            with self.subTest(name=name):
                output = self.root / ("post-" + name + ".mp4")
                self.kwargs["output"] = output

                def probe(_, path):
                    if Path(path) == self.source:
                        return {**self.result_info, "duration": 5.0}
                    if name == "probe":
                        raise DramaSynthesisError("drama_random_probe_failed", "随机模板视频校验失败", 502)
                    info = {**self.result_info, "video": dict(self.result_info["video"])}
                    if name == "duration":
                        info["duration"], info["video"]["duration"] = 3.0, "3.0"
                    elif name == "codec":
                        info["video"]["codec_name"] = "hevc"
                    return info

                def fingerprint(path):
                    if name == "fingerprint" and Path(path).name.startswith(".random-render-"):
                        raise gpu.checkpoint_error()
                    return original_fingerprint(path)

                with mock.patch.object(gpu, "_probe", side_effect=probe), \
                        mock.patch.object(gpu, "file_fingerprint", side_effect=fingerprint), \
                        self.assertRaises(DramaSynthesisError) as caught:
                    gpu.render_random_output(**self.kwargs, runner=self.runner)
                self.assertEqual(caught.exception.code, public_code)
                diagnostic = output.with_name("." + output.name + ".render.failure.json")
                payload = json.loads(diagnostic.read_text(encoding="utf-8"))
                self.assertEqual((payload["reason"], payload["public_code"]), (reason, public_code))
                self.assertNotIn(str(self.root), json.dumps(payload, ensure_ascii=False))
                self.assertFalse(output.exists())
                self.assertFalse(output.with_name(output.name + ".render.json").exists())
                guard_path = output.with_name(output.name + ".render.prepared.json")
                self.assertEqual(guard_path.exists(), preserve)
                partials = list(self.root.glob(".random-render-*.mp4"))
                self.assertEqual(bool(partials), preserve)
                if preserve:
                    guard = json.loads(guard_path.read_text(encoding="utf-8"))
                    self.assertEqual(partials, [self.root / guard["temporary_name"]])
                    partials[0].unlink()
                    guard_path.unlink()

    def test_async_context_uses_private_generation_path_and_invalid_context_never_falls_back(self):
        from features.drama_synthesis import async_runtime
        runtime_root = self.root / ".runtime"
        context = SimpleNamespace(
            runtime=SimpleNamespace(root=runtime_root), job_id="job-safe_01", generation=7, owner="worker",
        )
        self.kwargs["output"] = self.root / "context.mp4"

        def context_timeout(*_, **__):
            raise subprocess.TimeoutExpired(["ffmpeg"], 60, stderr="safe fixture")

        with async_runtime.use_context(context), self.assertRaises(DramaSynthesisError):
            gpu.render_random_output(**self.kwargs, runner=context_timeout)
        diagnostic = runtime_root / "diagnostics" / "job-safe_01" / "generation-00000007.random-render.json"
        self.assertTrue(diagnostic.is_file())
        self.assertFalse((self.root / ".context.mp4.render.failure.json").exists())
        context_payload = json.loads(diagnostic.read_text(encoding="utf-8"))["context"]
        self.assertEqual((context_payload["job_id"], context_payload["generation"]), ("job-safe_01", 7))
        self.assertEqual(set(context_payload), {
            "job_id", "generation", "source_sha256", "source_size_bytes",
            "recipe_sha256", "asset_set_sha256",
        })
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(diagnostic.stat().st_mode), 0o600)

        invalid = SimpleNamespace(
            runtime=SimpleNamespace(root=runtime_root), job_id="../escape", generation=7, owner="worker",
        )
        fallback = self.root / ".invalid-context.mp4.render.failure.json"
        with async_runtime.use_context(invalid), self.assertRaises(DramaSynthesisError) as caught:
            gpu._render_failure_diagnostic_path(self.root / "invalid-context.mp4")
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_unverified")
        self.assertFalse(fallback.exists())

        incomplete_identity = SimpleNamespace(
            runtime=SimpleNamespace(root=runtime_root), job_id="job-safe_01", generation=8, owner="worker",
        )
        invalid_identity_path = (runtime_root / "diagnostics" / "job-safe_01" /
                                 "generation-00000008.random-render.json")
        with async_runtime.use_context(incomplete_identity), self.assertRaises(DramaSynthesisError) as identity_error:
            gpu._write_render_failure_diagnostic(
                invalid_identity_path, reason="render_timeout",
                public_code="drama_random_render_timeout", duration_seconds=5,
                elapsed_seconds=1, configured_floor=60, planned_timeout=43200,
                final_deadline_offset=43200, global_cap=86400, stall_timeout=1800,
                last_progress={"duration_seconds": 5}, returncode=-9,
                stderr_evidence=gpu._new_stderr_evidence(),
                context={"source_sha256": "a" * 64},
            )
        self.assertEqual(identity_error.exception.code, "drama_media_checkpoint_unverified")
        self.assertFalse(invalid_identity_path.exists())

    def test_success_exit_cleanup_failures_are_diagnosed_and_preserve_recovery_evidence(self):
        from features.drama_synthesis import async_runtime

        @contextmanager
        def launch():
            yield

        for failure in ("poll", "join", "close", "clear"):
            with self.subTest(failure=failure):
                output = self.root / ("cleanup-" + failure + ".mp4")
                self.kwargs["output"] = output
                killed = []

                class Stream(io.StringIO):
                    def close(self):
                        if failure == "close":
                            raise OSError("simulated close failure")
                        return super().close()

                class Process:
                    pid = 12345
                    returncode = None
                    stdout, stderr = Stream(""), Stream("")
                    def wait(self, timeout):
                        self.returncode = 0
                        return 0
                    def poll(self):
                        if failure == "poll":
                            raise OSError("simulated poll failure")
                        return self.returncode
                    def kill(self):
                        killed.append(True)

                class Thread:
                    def __init__(self, target, daemon):
                        self.target = target
                    def start(self):
                        self.target()
                    def join(self, timeout=None):
                        if failure == "join":
                            raise OSError("simulated join failure")
                    def is_alive(self):
                        return False

                def popen(command, **_):
                    Path(command[-1]).write_bytes(b"complete-but-cleanup-unverified")
                    return Process()

                def clear(_):
                    if failure == "clear":
                        raise OSError("simulated clear failure")

                with mock.patch.object(gpu.threading, "Thread", Thread), \
                        mock.patch.object(gpu.subprocess, "Popen", side_effect=popen), \
                        mock.patch.object(async_runtime, "process_launch", launch), \
                        mock.patch.object(async_runtime, "record_process"), \
                        mock.patch.object(async_runtime, "clear_process", side_effect=clear), \
                        mock.patch.object(async_runtime, "emit_progress"), \
                        self.assertRaises(DramaSynthesisError) as caught:
                    gpu.render_random_output(**self.kwargs)
                self.assertEqual(caught.exception.code, "drama_media_checkpoint_unverified")
                self.assertEqual(killed, [])
                diagnostic = output.with_name("." + output.name + ".render.failure.json")
                payload = json.loads(diagnostic.read_text(encoding="utf-8"))
                self.assertEqual((payload["reason"], payload["public_code"]),
                                 ("cleanup_unverified", "drama_media_checkpoint_unverified"))
                guard_path = output.with_name(output.name + ".render.prepared.json")
                guard = json.loads(guard_path.read_text(encoding="utf-8"))
                partial = output.parent / guard["temporary_name"]
                self.assertEqual(partial.read_bytes(), b"complete-but-cleanup-unverified")
                self.assertFalse(output.exists())
                self.assertFalse(output.with_name(output.name + ".render.json").exists())
                partial.unlink()
                guard_path.unlink()

    def test_structured_unknown_process_clear_failure_is_checkpoint_unverified(self):
        from features.drama_synthesis import async_runtime

        output = self.root / "clear-process-unknown.mp4"
        self.kwargs["output"] = output

        @contextmanager
        def launch():
            yield

        class Process:
            pid = 12345
            returncode = 0
            stdout, stderr = io.BytesIO(b""), io.BytesIO(b"")

            def wait(self, timeout):
                return 0

            def poll(self):
                return 0

            def kill(self):
                raise AssertionError("exited child must not be killed")

        class Thread:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()

            def join(self, timeout=None):
                return None

            def is_alive(self):
                return False

        def popen(command, **_):
            Path(command[-1]).write_bytes(b"complete-but-process-state-unknown")
            return Process()

        clear = mock.Mock(side_effect=async_runtime.runtime_error("gpu_process_state_unknown"))
        with mock.patch.object(gpu.threading, "Thread", Thread), \
                mock.patch.object(gpu.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(async_runtime, "process_launch", launch), \
                mock.patch.object(async_runtime, "record_process"), \
                mock.patch.object(async_runtime, "clear_process", clear), \
                mock.patch.object(async_runtime, "emit_progress"), \
                self.assertRaises(DramaSynthesisError) as caught:
            gpu.render_random_output(**self.kwargs)

        self.assertEqual(caught.exception.code, "drama_media_checkpoint_unverified")
        clear.assert_called_once_with(12345)
        diagnostic = output.with_name("." + output.name + ".render.failure.json")
        payload = json.loads(diagnostic.read_text(encoding="utf-8"))
        self.assertEqual((payload["reason"], payload["public_code"]),
                         ("cleanup_unverified", "drama_media_checkpoint_unverified"))
        self.assertNotIn("gpu_process_state_unknown", json.dumps(payload, ensure_ascii=False))
        guard_path = output.with_name(output.name + ".render.prepared.json")
        guard = json.loads(guard_path.read_text(encoding="utf-8"))
        self.assertIsNone(guard["artifact"])
        self.assertIsNone(guard["result"])
        partial = output.parent / guard["temporary_name"]
        self.assertEqual(partial.read_bytes(), b"complete-but-process-state-unknown")
        self.assertFalse(output.exists())
        self.assertFalse(output.with_name(output.name + ".render.json").exists())

    def test_exited_child_with_live_reader_never_closes_its_pipe_or_clears_process(self):
        from features.drama_synthesis import async_runtime

        @contextmanager
        def launch():
            yield

        for stuck_reader, stuck_stream in (("read_progress", "stdout"), ("read_errors", "stderr")):
            with self.subTest(stuck_reader=stuck_reader):
                output = self.root / ("reader-" + stuck_stream + ".mp4")
                self.kwargs["output"] = output
                close_calls, join_calls = [], []

                class Stream:
                    def __init__(self, name):
                        self.name = name
                    def __iter__(self):
                        return iter(())
                    def read(self, size=-1):
                        return b""
                    def close(self):
                        close_calls.append((self.name, threading.current_thread().name))

                class Process:
                    pid = 12345
                    returncode = 0
                    stdout, stderr = Stream("stdout"), Stream("stderr")
                    def wait(self, timeout):
                        return 0
                    def poll(self):
                        return 0
                    def kill(self):
                        raise AssertionError("exited child must not be killed")

                class Thread:
                    def __init__(self, target, daemon):
                        self.target, self.name = target, target.__name__
                    def start(self):
                        if self.name != stuck_reader:
                            self.target()
                    def join(self, timeout=None):
                        join_calls.append((self.name, timeout))
                    def is_alive(self):
                        return self.name == stuck_reader

                def popen(command, **_):
                    Path(command[-1]).write_bytes(b"complete-but-reader-live")
                    return Process()

                cleared = mock.Mock()
                with mock.patch.object(gpu.threading, "Thread", Thread), \
                        mock.patch.object(gpu.subprocess, "Popen", side_effect=popen), \
                        mock.patch.object(async_runtime, "process_launch", launch), \
                        mock.patch.object(async_runtime, "record_process"), \
                        mock.patch.object(async_runtime, "clear_process", cleared), \
                        mock.patch.object(async_runtime, "emit_progress"), \
                        self.assertRaises(DramaSynthesisError) as caught:
                    gpu.render_random_output(**self.kwargs)
                self.assertEqual(caught.exception.code, "drama_media_checkpoint_unverified")
                cleared.assert_not_called()
                self.assertFalse(any(name == stuck_stream for name, _ in close_calls))
                self.assertTrue(any(name == stuck_reader and timeout in {2, 5} for name, timeout in join_calls))
                diagnostic = output.with_name("." + output.name + ".render.failure.json")
                payload = json.loads(diagnostic.read_text(encoding="utf-8"))
                self.assertEqual((payload["reason"], payload["public_code"]),
                                 ("cleanup_unverified", "drama_media_checkpoint_unverified"))
                guard_path = output.with_name(output.name + ".render.prepared.json")
                guard = json.loads(guard_path.read_text(encoding="utf-8"))
                partial = output.parent / guard["temporary_name"]
                self.assertEqual(partial.read_bytes(), b"complete-but-reader-live")
                partial.unlink()
                guard_path.unlink()

    def test_none_after_durable_prepare_is_checkpoint_failure_and_keeps_artifact(self):
        original_commit = gpu._commit_prepared_render
        final_commit_seen = []

        def missing_commit_result(prepared_path, *args, **kwargs):
            prepared = gpu.read_record(prepared_path)
            if isinstance(prepared, dict) and prepared.get("result") is not None:
                final_commit_seen.append(True)
                return None
            return original_commit(prepared_path, *args, **kwargs)

        with mock.patch.object(gpu, "_commit_prepared_render", side_effect=missing_commit_result), \
                self.assertRaises(DramaSynthesisError) as caught:
            self.render()
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_unverified")
        self.assertEqual(final_commit_seen, [True])
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_name("result.mp4.render.json").exists())
        guard_path = self.output.with_name("result.mp4.render.prepared.json")
        guard = json.loads(guard_path.read_text(encoding="utf-8"))
        self.assertIsNotNone(guard["artifact"])
        self.assertIsNotNone(guard["result"])
        artifact = self.root / guard["temporary_name"]
        self.assertEqual(artifact.read_bytes(), b"complete-render")

    def test_finally_cleanup_errors_do_not_mask_failure_and_committed_output_is_never_deleted(self):
        for failure in ("read_record", "partial_unlink", "guard_unlink"):
            with self.subTest(failure=failure):
                output = self.root / ("finally-" + failure + ".mp4")
                self.kwargs["output"] = output
                diagnostic = output.with_name("." + output.name + ".render.failure.json")
                guard_path = output.with_name(output.name + ".render.prepared.json")
                real_read, real_unlink = gpu.read_record, Path.unlink
                unlink_order = []

                def read(path):
                    if failure == "read_record" and Path(path) == guard_path and diagnostic.exists():
                        raise OSError("simulated final read failure")
                    return real_read(path)

                def unlink(path, *args, **kwargs):
                    path = Path(path)
                    if diagnostic.exists() and (path == guard_path or path.name.startswith(".random-render-")):
                        unlink_order.append(path.name)
                    if failure == "partial_unlink" and path.name.startswith(".random-render-") and diagnostic.exists():
                        raise OSError("simulated final partial unlink failure")
                    if failure == "guard_unlink" and path == guard_path and diagnostic.exists():
                        raise OSError("simulated final unlink failure")
                    return real_unlink(path, *args, **kwargs)

                def timeout_runner(command, **_):
                    Path(command[-1]).write_bytes(b"known-timeout-partial")
                    raise subprocess.TimeoutExpired(["ffmpeg"], 60, stderr="safe fixture")

                with mock.patch.object(gpu, "read_record", side_effect=read), \
                        mock.patch.object(Path, "unlink", new=unlink), \
                        self.assertRaises(DramaSynthesisError) as caught:
                    gpu.render_random_output(**self.kwargs, runner=timeout_runner)
                self.assertEqual(caught.exception.code, "drama_random_render_timeout")
                self.assertTrue(diagnostic.is_file())
                self.assertTrue(guard_path.is_file())
                guard = json.loads(guard_path.read_text(encoding="utf-8"))
                partial = output.parent / guard["temporary_name"]
                if failure in {"read_record", "partial_unlink"}:
                    self.assertTrue(partial.is_file())
                else:
                    self.assertFalse(partial.exists())
                if failure == "partial_unlink":
                    self.assertEqual(unlink_order, [partial.name])
                elif failure == "guard_unlink":
                    self.assertEqual(unlink_order, [partial.name, guard_path.name])
                partial.unlink(missing_ok=True)
                guard_path.unlink()

        output = self.root / "committed-cleanup.mp4"
        self.kwargs["output"] = output
        checkpoint = output.with_name(output.name + ".render.json")
        prepared = output.with_name(output.name + ".render.prepared.json")
        real_unlink = Path.unlink

        def fail_after_commit(path, *args, **kwargs):
            if Path(path) == prepared and checkpoint.exists():
                raise OSError("simulated committed prepare cleanup failure")
            return real_unlink(path, *args, **kwargs)

        committed_runner = mock.Mock(
            side_effect=lambda command, **_: Path(command[-1]).write_bytes(b"complete-render"),
        )
        with mock.patch.object(Path, "unlink", new=fail_after_commit):
            result = gpu.render_random_output(**self.kwargs, runner=committed_runner)
        self.assertEqual(result["output_sha256"], hashlib.sha256(b"complete-render").hexdigest())
        self.assertEqual(output.read_bytes(), b"complete-render")
        self.assertTrue(checkpoint.is_file())
        self.assertTrue(prepared.is_file())
        before = output.stat().st_mtime_ns
        committed_runner.reset_mock()
        gpu.render_random_output(**self.kwargs, runner=committed_runner)
        committed_runner.assert_not_called()
        self.assertEqual(output.stat().st_mtime_ns, before)

    def test_invalid_render_deadline_fails_before_encoder_launch(self):
        for value in (True, 0, 59, 86401, -1, 60.5, "auto", float("inf")):
            with self.subTest(value=value), self.assertRaises(DramaSynthesisError):
                gpu.render_timeout_seconds(value)
        with mock.patch.dict(os.environ, {"DRAMA_GPU_RENDER_TIMEOUT": "invalid"}):
            with self.assertRaises(DramaSynthesisError) as caught:
                self.render()
        self.assertEqual(caught.exception.code, "drama_random_timeout_configuration_invalid")
        self.runner.assert_not_called()
        self.assertEqual(list(self.root.glob(".random-render-*.mp4")), [])

    def test_native_timeout_has_distinct_safe_public_error(self):
        with mock.patch.object(gpu, "run_render_with_progress", side_effect=TimeoutError(URL)):
            with self.assertRaises(DramaSynthesisError) as caught:
                gpu.render_random_output(**self.kwargs)
        self.assertEqual((caught.exception.code, caught.exception.status), ("drama_random_render_timeout", 504))
        self.assertEqual(str(caught.exception), "随机模板视频制作超时")
        self.assertEqual(caught.exception.details, {})
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertFalse(self.output.exists())

    def test_subprocess_timeout_does_not_expose_command_or_captured_output(self):
        self.runner.side_effect = subprocess.TimeoutExpired(
            ["ffmpeg", "-i", URL], timeout=60, output="private stdout", stderr="private stderr")
        with self.assertRaises(DramaSynthesisError) as caught:
            self.render()
        self.assertEqual((caught.exception.code, caught.exception.status), ("drama_random_render_timeout", 504))
        self.assertEqual(str(caught.exception), "随机模板视频制作超时")
        self.assertEqual(caught.exception.details, {})
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertFalse(self.output.exists())

    def test_thread_budget_changes_only_thread_flag_not_visual_or_encoding_flags(self):
        args = (None, self.source, self.output, {}, {}, {})
        with mock.patch.dict(os.environ, {"DRAMA_GPU_FILTER_THREADS": "2"}):
            baseline = gpu.build_drama_random_command(*args)
        with mock.patch.dict(os.environ, {"DRAMA_GPU_FILTER_THREADS": "4"}):
            candidate = gpu.build_drama_random_command(*args)
        self.assertEqual(candidate[2], "4")
        self.assertEqual(baseline[:2] + baseline[3:], candidate[:2] + candidate[3:])
        self.assertIn("[0:v]setpts=PTS,fps=30[v]", candidate)
        self.assertIn("h264_nvenc", candidate)
        for value in ("0", "5", "auto"):
            with mock.patch.dict(os.environ, {"DRAMA_GPU_FILTER_THREADS": value}), self.assertRaises(DramaSynthesisError):
                gpu.build_drama_random_command(*args)


class ProcessProgressTests(unittest.TestCase):
    def test_progress_parser_rejects_nonfinite_values_and_uses_microseconds(self):
        self.assertEqual(gpu.ffmpeg_progress_metrics({"out_time_us": "1250000", "frame": "30", "speed": "1.25x"}, 5),
                         {"duration_seconds": 5.0, "out_time_seconds": 1.25, "frame": 30, "speed": 1.25})
        self.assertNotIn("speed", gpu.ffmpeg_progress_metrics({"speed": "nan", "frame": "-1"}, 5))
        self.assertEqual(gpu.ffmpeg_progress_metrics({"out_time": "01:02:03.5"}, 4000)["out_time_seconds"], 3723.5)

    def test_popen_tracks_child_identity_and_clears_only_after_wait(self):
        from features.drama_synthesis import async_runtime
        events = []

        class Process:
            pid = 12345
            returncode = None
            stdout = io.StringIO("out_time_us=2500000\nframe=75\nspeed=1.2x\nprogress=end\n")
            def wait(self, timeout):
                events.append("wait")
                self.returncode = 0
                return 0
            def poll(self):
                return self.returncode
            def kill(self):
                events.append("kill")

        @contextmanager
        def launch():
            events.append("launch")
            yield

        popen = mock.Mock(return_value=Process())
        with mock.patch.object(async_runtime, "process_launch", launch), mock.patch.object(async_runtime, "record_process", side_effect=lambda _: events.append("record")), mock.patch.object(async_runtime, "clear_process", side_effect=lambda _: events.append("clear")), mock.patch.object(async_runtime, "emit_progress") as progress:
            gpu.run_render_with_progress(["ffmpeg", "-c:v", "h264_nvenc", "output.mp4"], timeout=5, duration_seconds=5, popen=popen)
        self.assertLess(events.index("launch"), events.index("record"))
        self.assertLess(events.index("wait"), events.index("clear"))
        self.assertNotIn("kill", events)
        self.assertEqual(popen.call_args.args[0], ["ffmpeg", "-progress", "pipe:1", "-nostats", "-c:v", "h264_nvenc", "output.mp4"])
        self.assertTrue(any(call.kwargs.get("out_time_seconds") == 2.5 for call in progress.call_args_list))

    def test_timeout_kills_waits_then_clears_confirmed_child(self):
        from features.drama_synthesis import async_runtime
        events = []
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        diagnostic = Path(directory.name) / "native-timeout.json"

        class Process:
            pid = 12345
            returncode = None
            stdout = io.StringIO("")
            stderr = io.StringIO("No space left on device " + URL)
            def wait(self, timeout):
                events.append("wait")
                self.returncode = -9
                return -9
            def poll(self):
                return self.returncode
            def kill(self):
                events.append("kill")

        @contextmanager
        def launch():
            yield

        with mock.patch.object(async_runtime, "process_launch", launch), mock.patch.object(async_runtime, "record_process"), mock.patch.object(async_runtime, "clear_process", side_effect=lambda _: events.append("clear")), mock.patch.object(async_runtime, "emit_progress"), mock.patch.object(gpu.time, "monotonic", side_effect=[0, 2, 2]):
            with self.assertRaises(TimeoutError):
                gpu.run_render_with_progress(
                    ["ffmpeg", "output.mp4"], timeout=1, duration_seconds=5,
                    diagnostic_path=diagnostic, popen=lambda *_, **__: Process(),
                )
        self.assertEqual(events, ["kill", "wait", "clear"])
        payload = json.loads(diagnostic.read_text(encoding="utf-8"))
        self.assertEqual((payload["reason"], payload["public_code"]),
                         ("render_timeout", "drama_random_render_timeout"))
        self.assertIn("disk_full", payload["stderr"]["tags"])
        self.assertNotIn("media.example.test", diagnostic.read_text(encoding="utf-8"))

    def test_progress_can_extend_but_never_shrink_initial_deadline(self):
        from features.drama_synthesis import async_runtime

        class ControlledThread:
            progress_target = None
            def __init__(self, target, daemon):
                self.target = target
            def start(self):
                if self.target.__name__ == "read_progress":
                    ControlledThread.progress_target = self.target
                else:
                    self.target()
            def join(self, timeout=None):
                pass

        class Clock:
            def __init__(self):
                self.now = 0.0
            def __call__(self):
                return self.now

        class Process:
            pid = 12345
            def __init__(self, clock, progress, advances):
                self.clock, self.stdout = clock, io.StringIO(progress)
                self.advances, self.returncode = list(advances), None
            def wait(self, timeout):
                if self.returncode is not None:
                    return self.returncode
                advance, completed = self.advances.pop(0)
                self.clock.now += advance
                if ControlledThread.progress_target is not None:
                    target, ControlledThread.progress_target = ControlledThread.progress_target, None
                    target()
                if completed:
                    self.returncode = 0
                    return 0
                raise subprocess.TimeoutExpired(["ffmpeg"], timeout)
            def poll(self):
                return self.returncode
            def kill(self):
                self.returncode = -9

        @contextmanager
        def launch():
            yield

        cases = (
            # Very slow observed progress outlives the 12 hour initial deadline.
            # Valid progress extends it toward, but never beyond, the duration
            # based global budget.
            ("extend", 43200, 300, "out_time_us=10000000\nprogress=continue\n",
             [(10000, False), (34000, False), (2000, True)]),
            # The fast sample's estimate is shorter than the 60000 second base.
            # A later loop at t=50000 must still run instead of adopting that
            # shorter estimate as a new deadline.
            ("no-shrink", 60000, 100, "out_time_us=90000000\nspeed=100x\nprogress=continue\n",
             [(10000, False), (40000, False), (9000, True)]),
        )
        for name, base, duration, progress, advances in cases:
            with self.subTest(name=name):
                clock = Clock()
                ControlledThread.progress_target = None
                process = Process(clock, progress, advances)
                with mock.patch.object(gpu.threading, "Thread", ControlledThread), \
                        mock.patch.object(gpu, "_render_stall_seconds", return_value=86400), \
                        mock.patch.object(async_runtime, "process_launch", launch), \
                        mock.patch.object(async_runtime, "record_process"), \
                        mock.patch.object(async_runtime, "clear_process"), \
                        mock.patch.object(async_runtime, "emit_progress"):
                    gpu.run_render_with_progress(
                        ["ffmpeg", "output.mp4"], timeout=base,
                        absolute_timeout=86400,
                        duration_seconds=duration, stall_timeout=None,
                        popen=lambda *_, **__: process, monotonic=clock,
                    )
                self.assertEqual(process.returncode, 0)

    def test_strict_out_time_refreshes_stall_and_deadline_while_frame_only_refreshes_stall_only(self):
        from features.drama_synthesis import async_runtime

        class Clock:
            def __init__(self):
                self.now = 0.0
            def __call__(self):
                return self.now

        class ProgressStream:
            def __init__(self):
                self.lines = []
            def feed(self, packet):
                self.lines = [] if packet is None else packet.splitlines(keepends=True)
            def __iter__(self):
                lines, self.lines = self.lines, []
                return iter(lines)
            def close(self):
                pass

        class ControlledThread:
            progress_target = None
            def __init__(self, target, daemon):
                self.target = target
            def start(self):
                if self.target.__name__ == "read_progress":
                    ControlledThread.progress_target = self.target
                else:
                    self.target()
            def join(self, timeout=None):
                pass
            def is_alive(self):
                return False

        class Process:
            pid = 12345
            def __init__(self, clock, steps):
                self.clock, self.steps, self.returncode = clock, list(steps), None
                self.stdout, self.stderr = ProgressStream(), io.BytesIO(b"")
            def wait(self, timeout):
                if self.returncode is not None:
                    return self.returncode
                advance, packet, result = self.steps.pop(0)
                self.clock.now += advance
                if packet is not None:
                    self.stdout.feed(packet)
                    ControlledThread.progress_target()
                if result == "timeout":
                    raise subprocess.TimeoutExpired(["ffmpeg"], timeout)
                self.returncode = result
                return result
            def poll(self):
                return self.returncode
            def kill(self):
                self.returncode = -9

        @contextmanager
        def launch():
            yield

        first = "out_time_us=1000000\nframe=10\nfps=30\nspeed=1.0x\nprogress=continue\n"
        variants = {
            "strict-0.5ms": "out_time_us=1000500\nframe=10\nfps=31\nspeed=1.1x\nprogress=continue\n",
            "strict-1ms": "out_time_us=1001000\nframe=10\nfps=31\nspeed=1.1x\nprogress=continue\n",
            "frame-only": "frame=11\nfps=31\nspeed=1.1x\nprogress=continue\n",
            "out-time-then-frame-only": "frame=11\nfps=31\nspeed=1.1x\nprogress=continue\n",
            "equal": "out_time_us=1000000\nframe=10\nfps=32\nspeed=1.2x\nprogress=continue\n",
            "metadata-only": "fps=33\nspeed=1.3x\nprogress=continue\n",
        }

        def patches(process, clock):
            return (
                mock.patch.object(gpu.threading, "Thread", ControlledThread),
                mock.patch.object(async_runtime, "process_launch", launch),
                mock.patch.object(async_runtime, "record_process"),
                mock.patch.object(async_runtime, "clear_process"),
                mock.patch.object(async_runtime, "emit_progress"),
            )

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        deadlines = {}
        for name, second in variants.items():
            with self.subTest(deadline=name):
                clock = Clock()
                ControlledThread.progress_target = None
                initial = ("frame=10\nfps=30\nspeed=1.0x\nprogress=continue\n"
                           if name == "frame-only" else first)
                process = Process(clock, [
                    (400, initial, "timeout"),
                    (400, second, 1),
                ])
                diagnostic = Path(directory.name) / (name + ".json")
                contexts = patches(process, clock)
                with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], \
                        self.assertRaises(RuntimeError):
                    gpu.run_render_with_progress(
                        ["ffmpeg", "output.mp4"], timeout=900, absolute_timeout=86400,
                        configured_timeout=900, duration_seconds=100, stall_timeout=900,
                        diagnostic_path=diagnostic, popen=lambda *_, **__: process, monotonic=clock,
                    )
                payload = json.loads(diagnostic.read_text(encoding="utf-8"))
                deadlines[name] = payload["final_deadline_offset_seconds"]
                if name.startswith("strict-"):
                    self.assertEqual(payload["last_progress"]["out_time_seconds"],
                                     1.0005 if name == "strict-0.5ms" else 1.001)
                elif name == "frame-only":
                    self.assertEqual(payload["last_progress"]["frame"], 11)
                    self.assertEqual(payload["last_progress"]["out_time_seconds"], 0.0)
                elif name == "out-time-then-frame-only":
                    self.assertEqual(payload["last_progress"]["frame"], 11)
                    self.assertEqual(payload["last_progress"]["out_time_seconds"], 1.0)
                else:
                    self.assertEqual(payload["last_progress"]["out_time_seconds"], 1.0)
        self.assertGreater(deadlines["strict-0.5ms"], deadlines["equal"])
        self.assertGreater(deadlines["strict-1ms"], deadlines["equal"])
        self.assertEqual(deadlines["frame-only"], 900)
        self.assertEqual(deadlines["out-time-then-frame-only"], deadlines["metadata-only"])
        self.assertEqual(deadlines["equal"], deadlines["metadata-only"])

        # An out_time sample received before the 300 second planning threshold
        # is not a deferred permission to plan later.  A frame-only batch at
        # t=350 refreshes stall, and the following empty batch must still leave
        # the original deadline unchanged.
        with self.subTest(deadline="early-out-time-then-frame-only"):
            clock = Clock()
            ControlledThread.progress_target = None
            process = Process(clock, [
                (100, first, "timeout"),
                (250, variants["out-time-then-frame-only"], "timeout"),
                (100, None, "timeout"),
                (0, None, 1),
            ])
            diagnostic = Path(directory.name) / "early-out-time-then-frame-only.json"
            contexts = patches(process, clock)
            with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], \
                    self.assertRaises(RuntimeError):
                gpu.run_render_with_progress(
                    ["ffmpeg", "output.mp4"], timeout=900, absolute_timeout=86400,
                    configured_timeout=900, duration_seconds=100, stall_timeout=900,
                    diagnostic_path=diagnostic, popen=lambda *_, **__: process, monotonic=clock,
                )
            payload = json.loads(diagnostic.read_text(encoding="utf-8"))
            self.assertEqual(payload["final_deadline_offset_seconds"], 900)
            self.assertEqual(payload["last_progress"]["out_time_seconds"], 1.0)
            self.assertEqual(payload["last_progress"]["frame"], 11)

        for name, second in variants.items():
            with self.subTest(stall=name):
                clock = Clock()
                ControlledThread.progress_target = None
                initial = ("frame=10\nfps=30\nspeed=1.0x\nprogress=continue\n"
                           if name == "frame-only" else first)
                process = Process(clock, [
                    (100, initial, "timeout"),
                    (850, second, "timeout"),
                    (100, None, "timeout"),
                    (100, None, 0),
                ])
                contexts = patches(process, clock)
                with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4]:
                    if (name.startswith("strict-") or
                            name in {"frame-only", "out-time-then-frame-only"}):
                        gpu.run_render_with_progress(
                            ["ffmpeg", "output.mp4"], timeout=43200, absolute_timeout=86400,
                            duration_seconds=100, stall_timeout=900,
                            popen=lambda *_, **__: process, monotonic=clock,
                        )
                        self.assertEqual(process.returncode, 0)
                    else:
                        with self.assertRaisesRegex(TimeoutError, "stalled"):
                            gpu.run_render_with_progress(
                                ["ffmpeg", "output.mp4"], timeout=43200, absolute_timeout=86400,
                                duration_seconds=100, stall_timeout=900,
                                popen=lambda *_, **__: process, monotonic=clock,
                            )

    def test_progress_queue_folds_high_water_and_native_stderr_hashes_exact_bytes(self):
        from features.drama_synthesis import async_runtime
        raw_stderr = b"invalid-utf8:\xff\xfe no space left on device\n"
        packets = [
            (50, 10, 31, 0.1),
            (20, 500, 32, 0.2),
            (3, 30, 33, 0.3),
            (4, 40, 34, 0.4),
            (5, 50, 35, 0.5),
            (6, 60, 36, 0.6),
            (7, 70, 37, 0.7),
            (8, 80, 38, 0.8),
            (9, 90, 39, 0.9),
            (10, 100, 40, 4.0),
        ]
        progress_text = "".join(
            "out_time_us=%d\nframe=%d\nfps=%d\nspeed=%sx\nprogress=%s\n" % (
                seconds * 1000000, frame, fps, speed,
                "end" if index == len(packets) - 1 else "continue",
            )
            for index, (seconds, frame, fps, speed) in enumerate(packets)
        )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        diagnostic = Path(directory.name) / "folded-progress.json"
        queue_instances = []
        queue_type = gpu.queue.Queue

        class TrackingQueue(queue_type):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.full_hits = 0
                queue_instances.append(self)

            def full(self):
                value = super().full()
                if value:
                    self.full_hits += 1
                return value

        class Process:
            pid = 12345
            returncode = 1
            stdout, stderr = io.StringIO(progress_text), io.BytesIO(raw_stderr)
            def wait(self, timeout):
                return self.returncode
            def poll(self):
                return self.returncode
            def kill(self):
                raise AssertionError("exited child must not be killed")

        class Thread:
            def __init__(self, target, daemon):
                self.target = target
            def start(self):
                self.target()
            def join(self, timeout=None):
                pass
            def is_alive(self):
                return False

        @contextmanager
        def launch():
            yield

        with mock.patch.object(gpu.queue, "Queue", TrackingQueue), \
                mock.patch.object(gpu.threading, "Thread", Thread), \
                mock.patch.object(async_runtime, "process_launch", launch), \
                mock.patch.object(async_runtime, "record_process"), \
                mock.patch.object(async_runtime, "clear_process"), \
                mock.patch.object(async_runtime, "emit_progress") as emitted, \
                self.assertRaises(RuntimeError):
            gpu.run_render_with_progress(
                ["ffmpeg", "output.mp4"], timeout=43200, absolute_timeout=86400,
                duration_seconds=100, diagnostic_path=diagnostic,
                popen=lambda *_, **__: Process(),
            )
        payload = json.loads(diagnostic.read_text(encoding="utf-8"))
        self.assertEqual(len(queue_instances), 1)
        self.assertEqual(queue_instances[0].maxsize, 8)
        self.assertEqual(queue_instances[0].full_hits, 2)
        expected = {"out_time_seconds": 50.0, "frame": 500, "fps": 40.0, "speed": 4.0}
        for key, value in expected.items():
            self.assertEqual(payload["last_progress"][key], value)
        self.assertTrue(any(all(call.kwargs.get(key) == value for key, value in expected.items())
                            for call in emitted.call_args_list))
        self.assertTrue(payload["progress_stream_complete"])
        self.assertEqual(payload["stderr"]["bytes"], len(raw_stderr))
        self.assertEqual(payload["stderr"]["sha256"], hashlib.sha256(raw_stderr).hexdigest())
        self.assertFalse(payload["stderr"]["encoding_transformed"])
        self.assertFalse(payload["stderr"]["raw_stored"])

    def test_native_popen_launch_failure_sidecar_is_bounded_and_redacted(self):
        from features.drama_synthesis import async_runtime

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        diagnostic = root / "launch-failed.json"
        secret_url = URL + "&access_token=native-launch-secret"
        secret_output = root / "private-output.mp4"
        launch_error = OSError(
            "cannot launch credential=native-launch-secret for " + secret_url + " at " + str(secret_output)
        )

        @contextmanager
        def launch():
            yield

        failed_popen = mock.Mock(side_effect=launch_error)
        with mock.patch.object(gpu.subprocess, "Popen", failed_popen), \
                mock.patch.object(async_runtime, "process_launch", launch), \
                mock.patch.object(async_runtime, "record_process") as recorded, \
                mock.patch.object(async_runtime, "clear_process") as cleared, \
                mock.patch.object(async_runtime, "emit_progress"), \
                self.assertRaises(OSError) as caught:
            gpu.run_render_with_progress(
                ["/private/bin/ffmpeg-secret", "-i", secret_url, str(secret_output)],
                timeout=43200, absolute_timeout=86400, duration_seconds=100,
                diagnostic_path=diagnostic,
            )

        self.assertIs(caught.exception, launch_error)
        failed_popen.assert_called_once()
        recorded.assert_not_called()
        cleared.assert_not_called()
        payload = json.loads(diagnostic.read_text(encoding="utf-8"))
        self.assertEqual((payload["reason"], payload["public_code"]),
                         ("process_launch_failed", "drama_random_render_failed"))
        self.assertEqual(payload["process"], {"returncode": None, "signal": None})
        encoded = json.dumps(payload, ensure_ascii=False)
        for private in (
            "native-launch-secret", "media.example.test", str(secret_output), str(root),
            "/private/bin/ffmpeg-secret", "cannot launch", "credential=",
        ):
            self.assertNotIn(private, encoded)
        self.assertFalse(secret_output.exists())

    def test_exited_process_wins_at_exact_deadline_and_stall_boundaries(self):
        from features.drama_synthesis import async_runtime

        class Thread:
            def __init__(self, target, daemon):
                self.target = target
            def start(self):
                self.target()
            def join(self, timeout=None):
                pass
            def is_alive(self):
                return False

        @contextmanager
        def launch():
            yield

        for name, timeout, stall, boundary in (("deadline", 900, 1800, 900), ("stall", 43200, 900, 900)):
            with self.subTest(name=name):
                killed = []

                class Clock:
                    calls = 0
                    def __call__(self):
                        self.calls += 1
                        return 0.0 if self.calls == 1 else float(boundary)

                class Process:
                    pid = 12345
                    returncode = 0
                    stdout, stderr = io.StringIO(""), io.BytesIO(b"")
                    def wait(self, timeout):
                        return 0
                    def poll(self):
                        return 0
                    def kill(self):
                        killed.append(True)

                with mock.patch.object(gpu.threading, "Thread", Thread), \
                        mock.patch.object(async_runtime, "process_launch", launch), \
                        mock.patch.object(async_runtime, "record_process"), \
                        mock.patch.object(async_runtime, "clear_process"), \
                        mock.patch.object(async_runtime, "emit_progress"):
                    gpu.run_render_with_progress(
                        ["ffmpeg", "output.mp4"], timeout=timeout, absolute_timeout=86400,
                        configured_timeout=timeout, duration_seconds=100,
                        stall_timeout=stall, popen=lambda *_, **__: Process(), monotonic=Clock(),
                    )
                self.assertEqual(killed, [])

    def test_poll_time_progress_is_drained_before_deadline_and_stall_decisions(self):
        from features.drama_synthesis import async_runtime
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        @contextmanager
        def launch():
            yield

        for name, timeout, stall, boundary in (("deadline", 900, 1800, 900), ("stall", 43200, 900, 900)):
            with self.subTest(name=name):
                class Clock:
                    calls = 0
                    def __call__(self):
                        self.calls += 1
                        return 0.0 if self.calls == 1 else float(boundary)

                class Stream:
                    def __init__(self):
                        self.lines = []
                    def feed(self, value):
                        self.lines = value.splitlines(keepends=True)
                    def __iter__(self):
                        lines, self.lines = self.lines, []
                        return iter(lines)
                    def close(self):
                        pass

                class Thread:
                    progress_target = None
                    def __init__(self, target, daemon):
                        self.target = target
                    def start(self):
                        if self.target.__name__ == "read_progress":
                            Thread.progress_target = self.target
                        else:
                            self.target()
                    def join(self, timeout=None):
                        pass
                    def is_alive(self):
                        return False

                class Process:
                    pid = 12345
                    returncode = None
                    stdout, stderr = Stream(), io.BytesIO(b"")
                    poll_calls = 0
                    def poll(self):
                        self.poll_calls += 1
                        if self.returncode is None and self.poll_calls == 1:
                            self.stdout.feed("out_time_us=500\nfps=30\nspeed=0.1x\nprogress=continue\n")
                            Thread.progress_target()
                        return self.returncode
                    def wait(self, timeout):
                        self.returncode = 1
                        return 1
                    def kill(self):
                        raise AssertionError("poll-time progress must prevent boundary kill")

                clock, process = Clock(), Process()
                diagnostic = Path(directory.name) / ("poll-" + name + ".json")
                callback = mock.Mock()
                with mock.patch.object(gpu.threading, "Thread", Thread), \
                        mock.patch.object(async_runtime, "process_launch", launch), \
                        mock.patch.object(async_runtime, "record_process"), \
                        mock.patch.object(async_runtime, "clear_process"), \
                        mock.patch.object(async_runtime, "emit_progress") as emitted, \
                        self.assertRaises(RuntimeError):
                    gpu.run_render_with_progress(
                        ["ffmpeg", "output.mp4"], timeout=timeout, absolute_timeout=86400,
                        configured_timeout=timeout, duration_seconds=100, stall_timeout=stall,
                        diagnostic_path=diagnostic, progress_callback=callback,
                        popen=lambda *_, **__: process, monotonic=clock,
                    )
                payload = json.loads(diagnostic.read_text(encoding="utf-8"))
                self.assertEqual((payload["reason"], payload["public_code"]),
                                 ("process_exit", "drama_random_render_failed"))
                self.assertEqual(payload["last_progress"]["out_time_seconds"], 0.0005)
                self.assertTrue(any(call.args[0].get("out_time_seconds") == 0.0005
                                    for call in callback.call_args_list))
                self.assertTrue(any(call.kwargs.get("out_time_seconds") == 0.0005
                                    for call in emitted.call_args_list))

    def test_stall_watchdog_is_independent_of_long_absolute_budget(self):
        from features.drama_synthesis import async_runtime
        events = []

        class Clock:
            now = 0.0
            def __call__(self):
                return self.now

        clock = Clock()

        class Process:
            pid = 12345
            returncode = None
            # Repeated rate telemetry is not media advancement and therefore
            # must not keep a wedged encoder alive indefinitely.
            stdout = io.StringIO(
                "fps=30\nspeed=1.0x\nprogress=continue\n"
                "fps=30\nspeed=1.0x\nprogress=continue\n"
            )
            def wait(self, timeout):
                if self.returncode is not None:
                    return self.returncode
                clock.now += 500
                raise subprocess.TimeoutExpired(["ffmpeg"], timeout)
            def poll(self):
                return self.returncode
            def kill(self):
                events.append("kill")
                self.returncode = -9

        @contextmanager
        def launch():
            yield

        with mock.patch.object(async_runtime, "process_launch", launch), \
                mock.patch.object(async_runtime, "record_process"), \
                mock.patch.object(async_runtime, "clear_process", side_effect=lambda _: events.append("clear")), \
                mock.patch.object(async_runtime, "emit_progress"):
            with self.assertRaises(TimeoutError) as caught:
                gpu.run_render_with_progress(
                    ["ffmpeg", "output.mp4"], timeout=43200, absolute_timeout=69300,
                    duration_seconds=5400, stall_timeout=900,
                    popen=lambda *_, **__: Process(), monotonic=clock,
                )
        self.assertIn("stall", str(caught.exception).lower())
        self.assertEqual(events, ["kill", "clear"])

    def test_gpu_runtime_remains_parseable_by_python_39_and_310(self):
        source = (ROOT / "features" / "drama_synthesis" / "gpu.py").read_text(encoding="utf-8")
        for version in ((3, 9), (3, 10)):
            with self.subTest(version=version):
                ast.parse(source, filename="features/drama_synthesis/gpu.py", feature_version=version)


class ResourceGuardTests(unittest.TestCase):
    def setUp(self):
        from scripts import check_drama_media_resource_guard as guard
        self.guard = guard
        self.fixture()

    def fixture(self, profile=None):
        guard = self.guard
        profile = profile or guard.SELF_TEST_PROFILE
        self.profile = profile
        self.unit = ("drama-resource-guard-test-0123456789abcdef.service"
                     if profile is guard.SELF_TEST_PROFILE else
                     "drama-media-accept-0123456789ab-accept01-short-2c2t-r1.service")
        self.pid = 321
        unit_path = "/system.slice/" + self.unit
        self.paths = {name: "/sys/fs/cgroup/" + name for name in ("cpu", "memory", "pids")}
        values = {
            "/proc/321/cgroup": "2:cpu,cpuacct:%s\n3:memory:%s\n4:pids:%s\n1:name=systemd:%s" % ((unit_path,) * 4),
            "/proc/321/mountinfo": "\n".join(
                "%s 10 0:%s / %s rw - cgroup cgroup rw,%s" % (20 + index, 30 + index, path, name)
                for index, (name, path) in enumerate(self.paths.items())),
        }
        directories = {}
        for index, (controller, root) in enumerate(self.paths.items()):
            for offset, suffix in enumerate(("", "/system.slice", unit_path)):
                directories[root + suffix] = [0, 30 + index, 100 + index * 10 + offset]
            values[root + unit_path + "/cgroup.procs"] = str(self.pid)
            values[root + unit_path + "/tasks"] = str(self.pid)
            for suffix in ("/system.slice", unit_path):
                directory = root + suffix
                parent = suffix == "/system.slice"
                if controller == "cpu":
                    values[directory + "/cpu.cfs_quota_us"] = "-1" if parent else "200000"
                    values[directory + "/cpu.cfs_period_us"] = "100000"
                elif controller == "pids":
                    values[directory + "/pids.max"] = "max" if parent else "128"
                    values[directory + "/pids.current"] = "10" if parent else "1"
                else:
                    values.update({directory + "/" + key: value for key, value in {
                        "memory.limit_in_bytes": str(8 * profile.memory_bytes if parent else profile.memory_bytes),
                        "memory.memsw.limit_in_bytes": str(8 * profile.memory_bytes),
                        "memory.usage_in_bytes": str(2 * guard.PROBE_BYTES if parent else guard.PROBE_BYTES),
                        "memory.memsw.usage_in_bytes": str(2 * guard.PROBE_BYTES if parent else guard.PROBE_BYTES),
                        "memory.use_hierarchy": "1", "memory.swappiness": "60", "memory.failcnt": "0",
                        "memory.memsw.failcnt": "0",
                        "memory.oom_control": "oom_kill_disable 0\nunder_oom 0\noom_kill 0",
                        "memory.stat": "hierarchical_memory_limit %s\nhierarchical_memsw_limit %s\ntotal_swap 0" %
                                       (profile.memory_bytes, 8 * profile.memory_bytes),
                    }.items()})
        values[self.paths["memory"] + "/memory.use_hierarchy"] = "1"
        self.events = []
        events = self.events

        class Files:
            def __init__(self):
                self.values, self.directories, self.child_groups = values, directories, set()
                self.writes, self.ignore_writes, self.after_write = [], False, None
            def read(self, path):
                if path not in self.values:
                    raise FileNotFoundError("private control path must not become public")
                return self.values[path]
            def write(self, path, value, *, expected_directory):
                guard.require(self.directory(path.rsplit("/", 1)[0]) == expected_directory, "cgroup_identity_changed")
                events.append("write:" + path.rsplit("/", 1)[-1])
                self.writes.append((path, value))
                if not self.ignore_writes:
                    self.values[path] = str(value)
                    if path.endswith("/memory.memsw.limit_in_bytes"):
                        stats = path.rsplit("/", 1)[0] + "/memory.stat"
                        self.values[stats] = self.values[stats].replace(
                            "hierarchical_memsw_limit " + str(8 * profile.memory_bytes),
                            "hierarchical_memsw_limit " + str(value))
                if self.after_write:
                    self.after_write()
            def directory(self, path):
                return list(self.directories[path])
            def has_child_groups(self, path):
                return path in self.child_groups

        self.files = Files()
        files = self.files

        class Process:
            def __init__(self):
                self.actual = {"uids": [0, 0, 0], "gids": [0, 0, 0], "groups": [0], "nice": 10, "affinity": list(range(8))}
                self.caps, self.ambient, self.no_new_privs, self.after_drop = "c0", "0", "1", None
                self.publish_status()
            def publish_status(self):
                files.values["/proc/321/status"] = "\n".join([
                    "Pid:\t321", "Uid:\t" + " ".join(map(str, self.actual["uids"] + [self.actual["uids"][-1]])),
                    "Gid:\t" + " ".join(map(str, self.actual["gids"] + [self.actual["gids"][-1]])),
                    "Groups:\t" + " ".join(map(str, self.actual["groups"])), "NoNewPrivs:\t" + self.no_new_privs,
                    "CapEff:\t" + self.caps, "CapPrm:\t" + self.caps, "CapAmb:\t" + self.ambient,
                    "CapInh:\t0", "CapBnd:\tc0"])
            def pid(self):
                return 321
            def identity(self):
                return self.actual
            def target_identity(self):
                return 1009, 1010
            def drop_identity(self, uid, gid):
                events.append("drop")
                self.actual.update(uids=[uid] * 3, gids=[gid] * 3, groups=[])
                self.caps = "0"
                self.publish_status()
                if self.after_drop:
                    self.after_drop()

        self.process = Process()
        self.launch = mock.Mock(side_effect=lambda proof: self.events.append("exec"))
        self.report = mock.Mock()
        self.leaf = {name: path + unit_path for name, path in self.paths.items()}

    def run_guard(self, **kwargs):
        return self.guard.run_guard(kwargs.pop("unit", self.unit), kwargs.pop("cpu_cores", 2),
                                    profile=kwargs.pop("profile", self.guard.SELF_TEST_PROFILE),
                                    files=self.files, process=self.process, launch_probe=self.launch,
                                    report=self.report, **kwargs)

    def rejected(self, **kwargs):
        with self.assertRaises((self.guard.GuardFailure, OSError, KeyError, ValueError)):
            self.run_guard(**kwargs)
        self.launch.assert_not_called()
        self.report.assert_not_called()

    def test_guard_reads_back_drops_privileges_then_launches_only_fixed_probe(self):
        self.run_guard()
        self.assertEqual(self.files.writes, [
            (self.leaf["memory"] + "/memory.memsw.limit_in_bytes", self.guard.MEMORY_BYTES),
            (self.leaf["memory"] + "/memory.swappiness", 0)])
        self.assertEqual(self.events, ["write:memory.memsw.limit_in_bytes", "write:memory.swappiness", "drop", "exec"])
        proof = self.launch.call_args.args[0]
        self.assertEqual((proof["unit"], proof["cpu_cores"], proof["pid"]), (self.unit, 2, self.pid))
        self.assertEqual(len(proof["resources_sha256"]), 64)
        evidence = self.report.call_args.args[0]
        self.assertTrue(evidence["resources"]["ancestor_limits_checked"])
        self.assertEqual(evidence["identity"]["groups"], [])
        self.assertEqual(evidence["identity"]["cap_eff"], "0")

    def test_frozen_media_profile_writes_and_reads_exactly_16_gib(self):
        profile = self.guard.MEDIA_16_GIB_PROFILE
        self.fixture(profile)
        self.run_guard(profile=profile)
        self.assertEqual(self.files.writes, [
            (self.leaf["memory"] + "/memory.memsw.limit_in_bytes", 16 * 1024 ** 3),
            (self.leaf["memory"] + "/memory.swappiness", 0),
        ])
        proof = self.launch.call_args.args[0]
        self.assertEqual(proof["profile"], "media-acceptance-16gib-v1")
        self.assertEqual(self.report.call_args.args[0]["resources"]["profile"], proof["profile"])

    def test_media_profile_only_accepts_fixed_action_units_and_trial_names(self):
        pattern = self.guard.MEDIA_16_GIB_PROFILE.unit_pattern
        accepted = (
            "drama-media-accept-0123456789ab-accept01-short-2c2t-r1.service",
            "drama-media-accept-0123456789ab-accept01-long-4c4t-r2.service",
            "drama-media-prepare-0123456789ab-accept01.service",
            "drama-media-decode-0123456789ab-accept01-short-4c2t-r2.service",
            "drama-media-guard-0123456789ab-accept01.service",
        )
        rejected = (
            "drama-media-accept-0123456789ab-accept01-short-2c2t.service",
            "drama-media-accept-0123456789ab-accept01-short-2c2t-r3.service",
            "drama-media-decode-0123456789ab-accept01-short-2c2t.service",
        )
        for unit in accepted:
            with self.subTest(unit=unit):
                self.assertIsNotNone(re.fullmatch(pattern, unit))
        for unit in rejected:
            with self.subTest(unit=unit):
                self.assertIsNone(re.fullmatch(pattern, unit))

    def test_media_proof_revalidates_same_pid_without_running_self_probe(self):
        profile = self.guard.MEDIA_16_GIB_PROFILE
        self.fixture(profile)
        self.run_guard(profile=profile)
        proof = self.launch.call_args.args[0]
        read_fd, write_fd = os.pipe()
        os.write(write_fd, json.dumps(proof).encode())
        os.close(write_fd)
        with mock.patch.object(self.guard, "LinuxFiles", return_value=self.files), \
             mock.patch.object(self.guard, "LinuxProcess", return_value=self.process), \
             mock.patch.object(self.guard.os, "getpid", return_value=self.pid):
            verified = self.guard.verify_inherited_guard(
                self.unit, 2, read_fd, profile=profile
            )
        self.assertEqual(verified["proof"], proof)
        self.assertEqual(verified["identity"]["nice"], 10)

    def test_equal_but_unreviewed_profile_object_is_rejected_before_cgroup_reads(self):
        source = self.guard.SELF_TEST_PROFILE
        clone = self.guard.GuardProfile(
            source.name, source.memory_bytes, source.tasks_max,
            source.unit_pattern, source.media_acceptance
        )
        self.rejected(profile=clone)
        self.assertEqual(self.files.writes, [])

    def test_wrong_unit_or_cpu_expectation_never_writes_or_launches(self):
        for kwargs in ({"unit": "drama-synthesis-gpu.service"}, {"unit": "../system.slice"},
                       {"cpu_cores": 4}, {"cpu_cores": True}, {"cpu_cores": 8}):
            with self.subTest(kwargs=kwargs):
                self.fixture()
                self.rejected(**kwargs)
                self.assertEqual(self.files.writes, [])

    def test_missing_hidden_ambiguous_or_foreign_cgroups_fail_closed(self):
        for kind in ("v2", "foreign_member", "duplicate", "hidden_root", "duplicate_mount", "wrong_device", "extra_pid", "thread", "child"):
            with self.subTest(kind=kind):
                self.fixture()
                membership = "/proc/321/cgroup"
                mounts = "/proc/321/mountinfo"
                if kind == "v2":
                    self.files.values[membership] = "0::/system.slice/" + self.unit
                elif kind == "foreign_member":
                    self.files.values[membership] = self.files.values[membership].replace(self.unit, "production.service", 1)
                elif kind == "duplicate":
                    self.files.values[membership] += "\n5:memory:/system.slice/" + self.unit
                elif kind == "hidden_root":
                    self.files.values[mounts] = self.files.values[mounts].replace(" / /sys/", " /hidden /sys/", 1)
                elif kind == "duplicate_mount":
                    self.files.values[mounts] += "\n" + self.files.values[mounts].splitlines()[0]
                elif kind == "wrong_device":
                    self.files.directories[self.leaf["memory"]][1] = 99
                elif kind in ("extra_pid", "thread"):
                    self.files.values[self.leaf["memory"] + ("/tasks" if kind == "thread" else "/cgroup.procs")] = "321\n322"
                else:
                    self.files.child_groups.add(self.leaf["memory"])
                self.rejected()
                self.assertEqual(self.files.writes, [])

    def test_tighter_or_disabled_parent_limits_and_headroom_block_execution(self):
        cases = [("cpu", "cpu.cfs_quota_us", "100000"), ("memory", "memory.limit_in_bytes", "134217728"),
                 ("memory", "memory.memsw.limit_in_bytes", "134217728"), ("pids", "pids.max", "127"),
                 ("memory", "memory.use_hierarchy", "0"), ("pids", "pids.max", "128"),
                 ("memory", "memory.usage_in_bytes", str(8 * self.guard.MEMORY_BYTES))]
        for controller, name, value in cases:
            with self.subTest(controller=controller, name=name, value=value):
                self.fixture()
                self.files.values[self.paths[controller] + "/system.slice/" + name] = value
                self.rejected()
                self.assertEqual(self.files.writes, [])

    def test_missing_memsw_unsafe_oom_and_swap_pressure_do_not_launch(self):
        for name, value in (("memory.memsw.limit_in_bytes", None), ("memory.swappiness", "101"),
                            ("memory.oom_control", "oom_kill_disable 1\nunder_oom 0"),
                            ("memory.oom_control", "oom_kill_disable 0\nunder_oom 1"),
                            ("memory.failcnt", "1")):
            with self.subTest(name=name, value=value):
                self.fixture()
                path = self.leaf["memory"] + "/" + name
                if value is None:
                    del self.files.values[path]
                else:
                    self.files.values[path] = value
                self.rejected()
                self.assertEqual(self.files.writes, [])

    def test_unapplied_writes_or_changed_membership_fail_before_dropping_identity(self):
        for kind in ("unapplied", "moved", "replaced_directory"):
            with self.subTest(kind=kind):
                self.fixture()
                if kind == "unapplied":
                    self.files.ignore_writes = True
                elif kind == "moved":
                    self.files.after_write = lambda: self.files.values.update({"/proc/321/cgroup": "0::/"})
                else:
                    self.files.after_write = lambda: self.files.directories[self.leaf["memory"]].__setitem__(2, 999)
                self.rejected()
                self.assertNotIn("drop", self.events)

    def test_failed_privilege_drop_or_retained_authority_never_launches(self):
        for kind in ("exception", "groups", "uid", "cap_eff", "cap_amb", "no_new_privs", "nice"):
            with self.subTest(kind=kind):
                self.fixture()
                def corrupt_drop():
                    if kind == "exception":
                        raise PermissionError("private failure")
                    if kind == "groups":
                        self.process.actual["groups"] = [0]
                    elif kind == "uid":
                        self.process.actual["uids"][1] = 0
                    elif kind == "cap_eff":
                        self.process.caps = "c0"
                    elif kind == "cap_amb":
                        self.process.ambient = "1"
                    elif kind == "no_new_privs":
                        self.process.no_new_privs = "0"
                    elif kind == "nice":
                        self.process.actual["nice"] = 0
                    self.process.publish_status()
                self.process.after_drop = corrupt_drop
                self.rejected()

    def test_inode_or_limit_change_after_drop_blocks_exec(self):
        for kind in ("inode", "limit"):
            with self.subTest(kind=kind):
                self.fixture()
                if kind == "inode":
                    self.process.after_drop = lambda: self.files.directories[self.leaf["memory"]].__setitem__(2, 999)
                else:
                    self.process.after_drop = lambda: self.files.values.update({self.leaf["memory"] + "/memory.swappiness": "60"})
                self.rejected()

    def test_native_drop_clears_groups_before_gid_and_uid(self):
        calls = []
        with mock.patch.object(self.guard.os, "setgroups", side_effect=lambda value: calls.append(("groups", value)), create=True), \
             mock.patch.object(self.guard.os, "setgid", side_effect=lambda value: calls.append(("gid", value)), create=True), \
             mock.patch.object(self.guard.os, "setuid", side_effect=lambda value: calls.append(("uid", value)), create=True):
            self.guard.LinuxProcess.drop_identity(1009, 1010)
        self.assertEqual(calls, [("groups", []), ("gid", 1010), ("uid", 1009)])

    def test_cli_rejects_arbitrary_commands_and_public_errors_are_redacted(self):
        with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.guard.main(["--unit", self.unit, "--cpu-cores", "2", "--command", "ffmpeg"])
        self.assertEqual(caught.exception.code, 2)
        output = io.StringIO()
        with mock.patch.object(self.guard.sys, "platform", "linux"), \
             mock.patch.object(self.guard, "run_guard", side_effect=OSError(URL)), mock.patch("sys.stdout", new=output):
            self.assertEqual(self.guard.main(["--unit", self.unit, "--cpu-cores", "2"]), 78)
        self.assertNotIn("never-print-this", output.getvalue())
        self.assertFalse(json.loads(output.getvalue())["child_executed"])

    def test_exec_only_uses_same_probe_script_and_clean_environment(self):
        self.run_guard()
        proof = self.launch.call_args.args[0]
        captured = {}
        def capture_exec(executable, arguments, environment):
            captured.update(executable=executable, arguments=arguments, environment=environment)
            fd = int(arguments[-1])
            captured["fd"] = fd
            captured["proof"] = json.loads(os.read(fd, 1025))
            raise OSError("simulated exec failure")
        with mock.patch.object(self.guard.os, "execve", side_effect=capture_exec), \
             mock.patch.dict(os.environ, {"SECRET_TOKEN": "never-print-this"}), self.assertRaises(OSError):
            self.guard.exec_fixed_probe(proof)
        self.assertEqual(captured["arguments"][4], os.path.realpath(self.guard.__file__))
        self.assertEqual(captured["proof"], proof)
        self.assertEqual(set(captured["environment"]), {"PATH", "LANG", "PYTHONDONTWRITEBYTECODE"})
        with self.assertRaises(OSError):
            os.fstat(captured["fd"])

    def test_fixed_probe_revalidates_inherited_limits_without_media_or_waiting(self):
        self.run_guard()
        proof = self.launch.call_args.args[0]
        read_fd, write_fd = os.pipe()
        os.write(write_fd, json.dumps(proof).encode())
        os.close(write_fd)
        with mock.patch.object(self.guard, "LinuxFiles", return_value=self.files), \
             mock.patch.object(self.guard, "LinuxProcess", return_value=self.process), \
             mock.patch.object(self.guard.os, "getpid", return_value=self.pid), \
             mock.patch.object(self.guard.time, "sleep") as sleep, mock.patch.object(self.guard, "emit") as output:
            self.guard.run_probe(self.unit, 2, read_fd,
                                 profile=self.guard.SELF_TEST_PROFILE)
        self.assertEqual(sleep.call_args_list, [mock.call(1)] * 3)
        final = output.call_args.args[0]
        self.assertEqual(final["allocated_bytes"], 8 * 1024 * 1024)
        self.assertEqual(final["media_tools_started"], 0)
        self.assertFalse(final["media_acceptance"])

    def test_invalid_or_mismatched_probe_proof_never_reaches_allocation_wait(self):
        self.run_guard()
        for kind in ("pid", "fingerprint", "unapplied"):
            with self.subTest(kind=kind):
                proof = dict(self.launch.call_args.args[0])
                if kind == "pid":
                    proof["pid"] += 1
                elif kind == "fingerprint":
                    proof["resources_sha256"] = "0" * 64
                else:
                    self.files.values[self.leaf["memory"] + "/memory.swappiness"] = "60"
                read_fd, write_fd = os.pipe()
                os.write(write_fd, json.dumps(proof).encode())
                os.close(write_fd)
                with mock.patch.object(self.guard, "LinuxFiles", return_value=self.files), \
                     mock.patch.object(self.guard, "LinuxProcess", return_value=self.process), \
                     mock.patch.object(self.guard.os, "getpid", return_value=self.pid), \
                     mock.patch.object(self.guard.time, "sleep") as sleep, self.assertRaises(self.guard.GuardFailure):
                    self.guard.run_probe(self.unit, 2, read_fd,
                                         profile=self.guard.SELF_TEST_PROFILE)
                sleep.assert_not_called()


class MediaLauncherTests(unittest.TestCase):
    SHA = "a" * 40

    def setUp(self):
        from scripts import run_drama_media_acceptance as launcher
        self.launcher = launcher
        self.spec = launcher.build_spec(self.SHA, "accept01", "short", "2c2t")

    def test_default_command_only_previews_fixed_paths_and_never_starts_media(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", new=output):
            code = self.launcher.main([
                "--candidate-sha", self.SHA, "--run-id", "accept01",
                "--sample-kind", "short", "--config", "2c2t", "--trial", "r1",
            ])
        value = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertFalse(value["apply"])
        self.assertFalse(value["media_started"])
        self.assertEqual(value["memory_bytes"], 16 * 1024 ** 3)
        self.assertEqual(value["source"], self.launcher.path_text(self.spec.prepared_short_path))
        self.assertEqual(value["output_dir"], self.launcher.path_text(self.spec.output_dir))
        self.assertEqual(value["trial_configuration_order"], ["2c2t", "4c2t", "4c4t"])
        self.assertEqual(value["trial_position"], 1)
        self.assertEqual(value["render_timeout_seconds"], 43200)
        self.assertEqual(value["render_configured_timeout_seconds"], 43200)
        self.assertEqual(value["render_global_cap_seconds"], 86400)
        self.assertEqual(value["render_unit_runtime_max_seconds"], 90000)
        self.assertEqual(value["operation_timeout_seconds"], 90000)
        self.assertEqual(value["cos_uploads"], 0)
        self.assertEqual(value["production_requests"], 0)

    def test_public_cli_rejects_paths_commands_environment_and_raw_lock_fd(self):
        base = ["--candidate-sha", self.SHA, "--run-id", "accept01",
                "--sample-kind", "short", "--config", "2c2t", "--trial", "r1"]
        for extra in (["--source", URL], ["--command", "ffmpeg"],
                      ["--env-file", "secret.env"]):
            with self.subTest(extra=extra), mock.patch("sys.stderr", new=io.StringIO()), \
                    self.assertRaises(SystemExit):
                self.launcher.main(base + extra)
        output = io.StringIO()
        with mock.patch("sys.stdout", new=output):
            self.assertEqual(self.launcher.main(base + ["--lock-fd", "9"]), 78)
        self.assertEqual(json.loads(output.getvalue())["error_code"],
                         "invalid_internal_arguments")

    def test_systemd_command_is_root_guarded_actual_nice_and_gpu_visible(self):
        with mock.patch.object(self.launcher, "fixed_runtime_python",
                               return_value=Path("/fixed/python")), \
             mock.patch.object(self.launcher, "require_regular_file"):
            command = self.launcher.build_systemd_command(self.spec)
        joined = "\n".join(map(str, command))
        self.assertIn("--property=KillMode=control-group", command)
        self.assertIn("--property=RemainAfterExit=no", command)
        self.assertIn("--property=TimeoutStopSec=90", command)
        self.assertIn("--property=RuntimeMaxSec=90000", command)
        self.assertIn("--property=PrivateDevices=no", command)
        self.assertIn("--property=ReadOnlyPaths=" + " ".join(
            self.launcher.path_text(path) for path in (
                self.spec.candidate_root, self.launcher.INPUT_ROOT,
                self.launcher.ASSET_ROOT, self.launcher.RUNTIME_ROOT,
            )), command)
        self.assertIn("--property=MemoryLimit=17179869184", command)
        self.assertIn("--property=TasksMax=128", command)
        self.assertNotIn("--property=Nice=10", command)
        nice = command.index(self.launcher.path_text(self.launcher.NICE_PATH))
        self.assertEqual(command[nice:nice + 5],
                         [self.launcher.path_text(self.launcher.NICE_PATH), "-n", "10",
                          "/fixed/python", "-I"])
        self.assertEqual(command[nice + 5:nice + 8],
                         ["-S", "-B", self.launcher.path_text(self.spec.script_path)])
        self.assertNotIn("flock", joined)
        self.assertNotIn("EnvironmentFile", joined)

    def test_unit_contract_is_read_back_before_guard_and_timeout_is_bounded(self):
        text = "\n".join([
            "Id=" + self.spec.unit,
            "MainPID=321",
            "KillMode=control-group",
            "RemainAfterExit=no",
            "TimeoutStopUSec=1min 30s",
            "RuntimeMaxUSec=1d 1h",
            "PrivateDevices=no",
            "ReadOnlyPaths=" + " ".join(self.launcher.path_text(path) for path in (
                self.spec.candidate_root, self.launcher.INPUT_ROOT,
                self.launcher.ASSET_ROOT, self.launcher.RUNTIME_ROOT,
            )),
        ]) + "\n"
        result = SimpleNamespace(returncode=0, stdout=text)
        with mock.patch.object(self.launcher, "require_regular_file"), \
             mock.patch.object(self.launcher.subprocess, "run", return_value=result) as run, \
             mock.patch.object(self.launcher.os, "getpid", return_value=321):
            value = self.launcher.verify_media_unit_contract(self.spec)
        self.assertEqual(value["KillMode"], "control-group")
        self.assertIn(self.launcher.path_text(self.launcher.SYSTEMCTL_PATH),
                      run.call_args.args[0])
        for raw in ("0", "91s", "infinity", "1min bad"):
            with self.subTest(raw=raw), self.assertRaises(self.launcher.LaunchFailure):
                self.launcher.parse_systemd_duration(raw)
        self.assertEqual(self.launcher.parse_systemd_duration(
            "1d 1h", maximum_seconds=90000, exact_seconds=90000), 90000)
        with self.assertRaises(self.launcher.LaunchFailure):
            self.launcher.parse_systemd_duration(
                "1d 1h 1s", maximum_seconds=90000, exact_seconds=90000)
        non_render = self.launcher.build_spec(
            self.SHA, "accept01", "short", "2c2t", "decode")
        result.stdout = text.replace(self.spec.unit, non_render.unit).replace(
            "RuntimeMaxUSec=1d 1h", "RuntimeMaxUSec=12h")
        with mock.patch.object(self.launcher, "require_regular_file"), \
             mock.patch.object(self.launcher.subprocess, "run", return_value=result), \
             mock.patch.object(self.launcher.os, "getpid", return_value=321):
            self.launcher.verify_media_unit_contract(non_render)
        for checked_spec, wrong_text in (
            (self.spec, text.replace("RuntimeMaxUSec=1d 1h", "RuntimeMaxUSec=12h")),
            (non_render, text.replace(self.spec.unit, non_render.unit)),
        ):
            result.stdout = wrong_text
            with self.subTest(wrong_runtime=checked_spec.operation), \
                 mock.patch.object(self.launcher, "require_regular_file"), \
                 mock.patch.object(self.launcher.subprocess, "run", return_value=result), \
                 mock.patch.object(self.launcher.os, "getpid", return_value=321), \
                 self.assertRaises(self.launcher.LaunchFailure):
                self.launcher.verify_media_unit_contract(checked_spec)
        result.stdout = text
        result.stdout = text.replace(
            self.launcher.path_text(self.launcher.RUNTIME_ROOT), "/tmp/unreviewed-runtime"
        )
        with mock.patch.object(self.launcher, "require_regular_file"), \
             mock.patch.object(self.launcher.subprocess, "run", return_value=result), \
             mock.patch.object(self.launcher.os, "getpid", return_value=321), \
             self.assertRaises(self.launcher.LaunchFailure) as caught:
            self.launcher.verify_media_unit_contract(self.spec)
        self.assertEqual(str(caught.exception), "media_unit_contract_invalid")

    def test_start_memory_gate_requires_real_24_gib_available(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "meminfo"
            path.write_text("MemTotal: 33554432 kB\nMemAvailable: 25165824 kB\n")
            self.assertEqual(self.launcher.read_host_memory(path)["MemAvailable"],
                             24 * 1024 ** 3)
            path.write_text("MemTotal: 33554432 kB\nMemAvailable: 25165823 kB\n")
            with self.assertRaises(self.launcher.LaunchFailure):
                self.launcher.read_host_memory(path)

    def test_explicit_preflight_checks_launcher_only_without_media_processes(self):
        parent_read, parent_write = os.pipe()
        lock_read, lock_write = os.pipe()
        directory_stat = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
        try:
            with mock.patch.object(self.launcher, "ensure_public_apply_preflight") as base, \
                 mock.patch.object(self.launcher, "read_host_memory") as memory, \
                 mock.patch.object(self.launcher, "fixed_runtime_python"), \
                 mock.patch.object(self.launcher, "require_regular_file"), \
                 mock.patch.object(self.launcher.os, "O_DIRECTORY", 0, create=True), \
                 mock.patch.object(self.launcher.os, "O_CLOEXEC", 0, create=True), \
                 mock.patch.object(self.launcher.os, "open", return_value=parent_read), \
                 mock.patch.object(self.launcher.os, "fstat", return_value=directory_stat), \
                 mock.patch.object(self.launcher, "acquire_media_lock",
                                   return_value=(lock_read, (1, 2))), \
                 mock.patch.object(self.launcher.subprocess, "Popen") as popen:
                value = self.launcher.run_public_preflight(self.spec)
            base.assert_called_once_with(self.spec)
            memory.assert_called_once_with()
            popen.assert_not_called()
            self.assertTrue(value["preflight_passed"])
            self.assertFalse(value["unit_submitted"])
            self.assertEqual((value["ffprobe_processes"], value["ffmpeg_processes"]), (0, 0))
        finally:
            for descriptor in (parent_read, parent_write, lock_read, lock_write):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def test_second_stage_exec_is_same_script_isolated_with_site_suppression(self):
        proof = {"version": 2, "profile": "media-acceptance-16gib-v1",
                 "unit": self.spec.unit, "cpu_cores": 2, "pid": os.getpid(),
                 "resources_sha256": "b" * 64}
        lock_read, lock_write = os.pipe()
        captured = {}

        def capture(executable, arguments, environment):
            captured.update(executable=str(executable), arguments=list(map(str, arguments)),
                            environment=dict(environment))
            proof_fd = int(arguments[-3])
            captured["proof"] = json.loads(os.read(proof_fd, 1025))
            raise OSError("simulated exec")

        try:
            with mock.patch.object(self.launcher.os, "execve", side_effect=capture), \
                 mock.patch.dict(os.environ, {"SECRET_TOKEN": "never-print-this"}), \
                 self.assertRaises(OSError):
                self.launcher.exec_verified_stage(self.spec, proof, lock_read)
        finally:
            os.close(lock_read)
            os.close(lock_write)
        arguments = captured["arguments"]
        self.assertEqual(arguments[1:4], ["-I", "-S", "-B"])
        self.assertEqual(arguments[4], self.launcher.path_text(self.spec.script_path))
        self.assertEqual(captured["proof"], proof)
        self.assertNotIn("SECRET_TOKEN", captured["environment"])
        self.assertEqual(set(captured["environment"]), {
            "PATH", "LANG", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED",
            "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
        })

    def test_renderer_lock_fd_is_only_added_for_guarded_launcher(self):
        from scripts import benchmark_drama_synthesis_media as benchmark

        class Process:
            pid = 444
            stdout = None
            def poll(self): return 0

        ordinary_popen = mock.Mock(return_value=Process())
        benchmark.launch_renderer_process(["ffmpeg"], popen=ordinary_popen, text=True)
        self.assertNotIn("pass_fds", ordinary_popen.call_args.kwargs)
        guarded_popen = mock.Mock(return_value=Process())
        with mock.patch.object(benchmark, "validate_inherited_media_lock_fd",
                               return_value=(1, 2)), \
             mock.patch.object(benchmark, "verify_child_media_lock_fd") as verify:
            benchmark.launch_renderer_process(
                ["ffmpeg"], inherited_lock_fd=9, popen=guarded_popen, text=True
            )
        self.assertEqual(guarded_popen.call_args.kwargs["pass_fds"], (9,))
        verify.assert_called_once_with(444, 9, (1, 2))

    def test_failed_child_lock_readback_kills_waits_and_confirms_exit(self):
        from scripts import benchmark_drama_synthesis_media as benchmark
        events = []

        class Process:
            pid = 445
            stdout = io.StringIO()
            returncode = None
            def poll(self): return self.returncode
            def kill(self):
                events.append("kill")
                self.returncode = -9
            def wait(self, timeout):
                events.append(("wait", timeout))
                return self.returncode

        with mock.patch.object(benchmark, "validate_inherited_media_lock_fd",
                               return_value=(1, 2)), \
             mock.patch.object(benchmark, "verify_child_media_lock_fd",
                               side_effect=benchmark.BenchmarkGuardError(
                                   "benchmark_media_lock_inheritance_failed")), \
             self.assertRaises(benchmark.BenchmarkGuardError) as caught:
            benchmark.launch_renderer_process(
                ["ffmpeg"], inherited_lock_fd=9, popen=mock.Mock(return_value=Process())
            )
        self.assertEqual(caught.exception.code, "benchmark_media_lock_inheritance_failed")
        self.assertEqual(events, ["kill", ("wait", 30)])

    def test_child_lock_keyboard_interrupt_kills_waits_and_reraises(self):
        from scripts import benchmark_drama_synthesis_media as benchmark
        events = []

        class Output:
            def close(self):
                events.append("close")

        class Process:
            pid = 447
            stdout = Output()
            returncode = None

            def poll(self):
                return self.returncode

            def kill(self):
                events.append("kill")
                self.returncode = -9

            def wait(self, timeout):
                events.append(("wait", timeout))
                return self.returncode

        with mock.patch.object(benchmark, "validate_inherited_media_lock_fd",
                               return_value=(1, 2)), \
             mock.patch.object(benchmark, "verify_child_media_lock_fd",
                               side_effect=KeyboardInterrupt()), \
             self.assertRaises(KeyboardInterrupt):
            benchmark.launch_renderer_process(
                ["ffmpeg"], inherited_lock_fd=9,
                popen=mock.Mock(return_value=Process()),
            )
        self.assertEqual(events, ["kill", ("wait", 30), "close"])

    def test_failed_child_cleanup_has_distinct_error_and_is_not_claimed_reaped(self):
        from scripts import benchmark_drama_synthesis_media as benchmark

        class Process:
            pid = 446
            stdout = io.StringIO()
            def poll(self): return None
            def kill(self): raise OSError("private")
            def wait(self, timeout): raise subprocess.TimeoutExpired("ffmpeg", timeout)

        with mock.patch.object(benchmark, "validate_inherited_media_lock_fd",
                               return_value=(1, 2)), \
             mock.patch.object(benchmark, "verify_child_media_lock_fd",
                               side_effect=benchmark.BenchmarkGuardError("bad")), \
             self.assertRaises(benchmark.BenchmarkGuardError) as caught:
            benchmark.launch_renderer_process(
                ["ffmpeg"], inherited_lock_fd=9, popen=mock.Mock(return_value=Process())
            )
        self.assertEqual(caught.exception.code, "benchmark_renderer_cleanup_failed")

    def test_benchmark_evidence_only_labels_explicit_launcher_protocol_guarded(self):
        from scripts import benchmark_drama_synthesis_media as benchmark
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, recipe = root / "source.mp4", root / "recipe.json"
            source.write_bytes(b"source")
            recipe.write_text(json.dumps({"recipe_sha256": "c" * 64}))

            def arguments(name, sample_kind="short"):
                return SimpleNamespace(
                    source=str(source), recipe=str(recipe), output_dir=str(root / name),
                    sample_kind=sample_kind, filter_threads=2, ffmpeg="ffmpeg",
                    ffprobe="ffprobe", timeout=60, asset_root=str(root / "assets"),
                    asset_manifest_sha256="d" * 64,
                )

            original = Path.is_file
            patches = [
                mock.patch.object(Path, "is_file", autospec=True,
                                  side_effect=lambda path: True if path == Path("/proc/self/stat")
                                  else original(path)),
                mock.patch.object(benchmark.gpu, "_probe", return_value={"duration": 5}),
                mock.patch.object(benchmark, "cgroup_limits", return_value={"cgroup_version": 1}),
                mock.patch.object(benchmark.gpu, "render_random_output", return_value={}),
            ]
            invalid_results = []
            with patches[0], patches[1] as probe, patches[2], patches[3] as rendered:
                ordinary = benchmark.benchmark_render(arguments("ordinary"))
                probe.return_value = {"duration": 5400}
                with mock.patch.object(benchmark, "validate_inherited_media_lock_fd",
                                       return_value=(1, 2)):
                    guarded = benchmark.benchmark_render(
                        arguments("guarded", "long"), inherited_lock_fd=9
                    )
                for name, bad_timeout in (("wrong-int", 43200), ("wrong-bool", True)):
                    def run_with_wrong_timeout(**kwargs):
                        kwargs["runner"](
                            ["ffmpeg", str(kwargs["output"])], timeout=bad_timeout,
                        )

                    rendered.side_effect = run_with_wrong_timeout
                    with mock.patch.object(benchmark.subprocess, "Popen") as popen:
                        invalid = benchmark.benchmark_render(arguments(name, "long"))
                    popen.assert_not_called()
                    invalid_results.append(invalid)
        self.assertFalse(ordinary["acceptance_launcher_lock_inherited"])
        self.assertTrue(guarded["acceptance_launcher_lock_inherited"])
        for value in (ordinary, guarded):
            self.assertTrue(value["source_unchanged"])
            self.assertEqual(value["source"], value["source_final"])
            self.assertEqual(value["source_identity"], value["source_final_identity"])
            self.assertEqual(value["render_timeout_seconds"], 60)
            self.assertEqual(value["render_global_cap_seconds"], 86400)
        self.assertEqual(ordinary["render_planned_timeout_seconds"], 43200)
        self.assertEqual(guarded["render_planned_timeout_seconds"], 69300)
        for invalid in invalid_results:
            self.assertFalse(invalid["ok"])
            self.assertEqual(invalid["error_code"],
                             "benchmark_render_timeout_contract_mismatch")
            self.assertEqual(invalid["render_planned_timeout_seconds"], 69300)

    def test_guarded_renderer_rechecks_24_gib_immediately_before_popen(self):
        from scripts import benchmark_drama_synthesis_media as benchmark
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, recipe = root / "source.mp4", root / "recipe.json"
            source.write_bytes(b"source")
            recipe.write_text(json.dumps({"recipe_sha256": "e" * 64}))
            args = SimpleNamespace(
                source=str(source), recipe=str(recipe), output_dir=str(root / "result"),
                sample_kind="short", filter_threads=2, ffmpeg="ffmpeg", ffprobe="ffprobe",
                timeout=60, asset_root=str(root / "assets"), asset_manifest_sha256="f" * 64,
            )
            original = Path.is_file

            def render(**kwargs):
                planned = benchmark.gpu.render_budget_seconds(5, kwargs["timeout"])
                kwargs["runner"](["ffmpeg", str(kwargs["output"])], timeout=planned)

            with mock.patch.object(Path, "is_file", autospec=True,
                                   side_effect=lambda path: True if path == Path("/proc/self/stat")
                                   else original(path)), \
                 mock.patch.object(benchmark, "validate_inherited_media_lock_fd",
                                   return_value=(1, 2)), \
                 mock.patch.object(benchmark.gpu, "_probe", return_value={"duration": 5}), \
                 mock.patch.object(benchmark, "cgroup_limits", return_value={"cgroup_version": 1}), \
                 mock.patch.object(benchmark, "host_memory_sample", return_value={
                     "mem_available_bytes": 24 * 1024 ** 3 - 1,
                     "mem_total_bytes": 32 * 1024 ** 3,
                 }), \
                 mock.patch.object(benchmark.gpu, "render_random_output", side_effect=render), \
                 mock.patch.object(benchmark.subprocess, "Popen") as popen:
                result = benchmark.benchmark_render(args, inherited_lock_fd=9)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "benchmark_launcher_start_memory_low")
        self.assertEqual(result["resource_guard"]["phase"], "before_launch")
        self.assertEqual(
            result["resource_guard"]["thresholds"]["launcher_start_mem_available_below_bytes"],
            24 * 1024 ** 3,
        )
        popen.assert_not_called()

    def test_trial_names_isolate_units_outputs_and_evidence_but_share_one_short(self):
        r1 = self.launcher.build_spec(self.SHA, "accept01", "short", "2c2t",
                                      "render", "r1")
        r2 = self.launcher.build_spec(self.SHA, "accept01", "short", "2c2t",
                                      "render", "r2")
        decode = self.launcher.build_spec(self.SHA, "accept01", "short", "2c2t",
                                          "decode", "r2")
        self.assertEqual(r1.prepared_short_path, r2.prepared_short_path)
        self.assertEqual(r1.prepare_evidence_path, r2.prepare_evidence_path)
        self.assertNotEqual(r1.unit, r2.unit)
        self.assertNotEqual(r1.output_dir, r2.output_dir)
        self.assertNotEqual(r1.launcher_result_path, r2.launcher_result_path)
        self.assertIn("-r2.service", decode.unit)
        self.assertIn("-r2", decode.decode_evidence_path.name)
        self.assertEqual(self.launcher.preview(r2)["trial_configuration_order"],
                         ["4c4t", "4c2t", "2c2t"])
        with self.assertRaises(self.launcher.LaunchFailure):
            self.launcher.build_spec(self.SHA, "accept01", "short", "2c2t",
                                     "prepare-short", "r2")
        with self.assertRaises(self.launcher.LaunchFailure):
            self.launcher.build_spec(self.SHA, "accept01", "short", "4c2t",
                                     "guard-only", "r1")

    def test_same_trial_replay_is_rejected_while_second_trial_remains_new(self):
        r1 = self.launcher.build_spec(self.SHA, "accept01", "short", "2c2t",
                                      "render", "r1")
        r2 = self.launcher.build_spec(self.SHA, "accept01", "short", "2c2t",
                                      "render", "r2")

        def exists(path):
            return path == r1.output_dir

        with mock.patch.object(self.launcher, "validate_prepared_short"), \
             mock.patch.object(self.launcher, "validate_action_completion"), \
             mock.patch.object(Path, "exists", autospec=True, side_effect=exists), \
             mock.patch.object(Path, "is_symlink", autospec=True, return_value=False):
            with self.assertRaises(self.launcher.LaunchFailure) as caught:
                self.launcher.validate_existing_action_inputs(r1, 1009, 1010)
            self.assertEqual(str(caught.exception), "render_output_must_be_new")
            self.launcher.validate_existing_action_inputs(r2, 1009, 1010)

    def test_current_guard_evidence_does_not_block_its_own_render(self):
        spec = self.launcher.build_spec(self.SHA, "accept01", "short", "2c2t")

        def exists(path):
            return path == spec.launcher_guard_path

        source = {"sha256": "9" * 64, "size_bytes": 111}
        identity = {"device": 1, "inode": 2, "mtime_ns": 3, "nlink": 1}
        benchmark = SimpleNamespace(benchmark_render=mock.Mock(return_value={
            "ok": False, "source": source, "source_final": source,
            "source_unchanged": True, "source_identity": identity,
            "source_final_identity": identity,
            "duration_seconds": 120, "render_timeout_seconds": 43200,
            "render_planned_timeout_seconds": 43200,
            "render_global_cap_seconds": 86400,
            "minimum_mem_available_bytes": 16 * 1024 ** 3,
        }))
        with mock.patch.object(self.launcher, "validate_prepared_short", return_value={
                 "prepared_sha256": source["sha256"], "prepared_size": source["size_bytes"]
             }), \
             mock.patch.object(Path, "exists", autospec=True, side_effect=exists), \
             mock.patch.object(Path, "is_symlink", autospec=True, return_value=False), \
             mock.patch.object(self.launcher, "fingerprint_regular",
                               return_value={"sha256": "a" * 64, "size_bytes": 10}), \
             mock.patch.object(self.launcher, "write_exclusive_json") as write:
            result = self.launcher.run_render(spec, 1009, 1010, 9, benchmark)
        self.assertFalse(result["ok"])
        write.assert_called_once()

    def test_short_render_rejects_benchmark_source_that_differs_from_prepare_sha(self):
        spec = self.launcher.build_spec(self.SHA, "accept01", "short", "2c2t")
        identity = {"device": 1, "inode": 2, "mtime_ns": 3, "nlink": 1}
        source = {"sha256": "4" * 64, "size_bytes": 100}
        benchmark = SimpleNamespace(benchmark_render=mock.Mock(return_value={
            "ok": False, "source": source, "source_final": source,
            "source_unchanged": True, "source_identity": identity,
            "source_final_identity": identity,
            "duration_seconds": 120, "render_timeout_seconds": 43200,
            "render_planned_timeout_seconds": 43200,
            "render_global_cap_seconds": 86400,
            "minimum_mem_available_bytes": 16 * 1024 ** 3,
        }))
        with mock.patch.object(self.launcher, "validate_prepared_short", return_value={
                 "prepared_sha256": "5" * 64, "prepared_size": 100
             }), \
             mock.patch.object(Path, "exists", autospec=True, return_value=False), \
             mock.patch.object(Path, "is_symlink", autospec=True, return_value=False), \
             self.assertRaises(self.launcher.LaunchFailure) as caught:
            self.launcher.run_render(spec, 1009, 1010, 9, benchmark)
        self.assertEqual(str(caught.exception), "render_source_fingerprint_mismatch")

    def test_each_operation_defaults_to_preview_and_apply_only_submits(self):
        base = ["--candidate-sha", self.SHA, "--run-id", "accept01",
                "--sample-kind", "short", "--config", "2c2t", "--trial", "r1"]
        for flag, operation in (("--prepare-short", "prepare-short"),
                                ("--decode", "decode"),
                                ("--guard-only", "guard-only")):
            output = io.StringIO()
            with self.subTest(operation=operation), mock.patch("sys.stdout", new=output), \
                    mock.patch.object(self.launcher, "submit") as submit, \
                    mock.patch.object(self.launcher.subprocess, "Popen") as popen:
                self.assertEqual(self.launcher.main(base + [flag]), 0)
                value = json.loads(output.getvalue())
                self.assertEqual(value["operation"], operation)
                self.assertFalse(value["apply"])
                self.assertFalse(value["media_started"])
                self.assertEqual(value["operation_timeout_seconds"], 43200)
                self.assertNotIn("render_unit_runtime_max_seconds", value)
                submit.assert_not_called()
                popen.assert_not_called()
        submitted = {"ok": True, "submitted": True}
        with mock.patch.object(self.launcher, "submit", return_value=submitted) as submit, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(self.launcher.main(base + ["--prepare-short", "--apply"]), 0)
        self.assertEqual(submit.call_args.args[0].operation, "prepare-short")

    def test_systemd_units_are_action_specific_but_share_the_hard_guard(self):
        specs = {
            "render": self.launcher.build_spec(self.SHA, "accept01", "short", "2c2t"),
            "prepare-short": self.launcher.build_spec(
                self.SHA, "accept01", "short", "2c2t", "prepare-short"),
            "decode": self.launcher.build_spec(
                self.SHA, "accept01", "short", "2c2t", "decode"),
            "guard-only": self.launcher.build_spec(
                self.SHA, "accept01", "short", "2c2t", "guard-only"),
        }
        units = set()
        with mock.patch.object(self.launcher, "fixed_runtime_python",
                               return_value=Path("/fixed/python")), \
             mock.patch.object(self.launcher, "require_regular_file"):
            for operation, spec in specs.items():
                command = self.launcher.build_systemd_command(spec)
                units.add(spec.unit)
                self.assertIn("--property=MemoryLimit=17179869184", command)
                self.assertIn("--property=TasksMax=128", command)
                self.assertIn("--property=KillMode=control-group", command)
                self.assertIn("--property=RemainAfterExit=no", command)
                expected_runtime = 90000 if operation == "render" else 43200
                self.assertIn("--property=RuntimeMaxSec=" + str(expected_runtime), command)
                self.assertIn("--property=PrivateDevices=no", command)
                self.assertTrue(any(item.startswith("--property=ReadOnlyPaths=")
                                    for item in command))
                self.assertIn("--trial", command)
                if operation != "render":
                    self.assertIn("--" + operation, command)
        self.assertEqual(len(units), 4)

    def test_guard_only_preflight_does_not_touch_run_root_lock_or_media_inputs(self):
        spec = self.launcher.build_spec(
            self.SHA, "accept01", "short", "2c2t", "guard-only")
        with mock.patch.object(self.launcher, "ensure_public_apply_preflight") as base, \
             mock.patch.object(self.launcher, "read_host_memory"), \
             mock.patch.object(self.launcher, "fixed_runtime_python"), \
             mock.patch.object(self.launcher, "require_regular_file"), \
             mock.patch.object(self.launcher, "acquire_media_lock") as lock, \
             mock.patch.object(self.launcher.os, "open") as opened:
            value = self.launcher.run_public_preflight(spec)
        base.assert_called_once_with(spec)
        lock.assert_not_called()
        opened.assert_not_called()
        self.assertFalse(value["media_started"])
        self.assertEqual((value["ffmpeg_processes"], value["ffprobe_processes"]), (0, 0))
        with mock.patch.object(self.launcher, "require_regular_file") as regular:
            self.launcher.validate_fixed_inputs(spec)
        regular.assert_not_called()

    def test_guard_only_verified_stage_runs_only_fixed_small_probe_and_cannot_claim_media(self):
        spec = self.launcher.build_spec(
            self.SHA, "accept01", "short", "2c2t", "guard-only")

        class GuardFailure(RuntimeError):
            pass

        guard = SimpleNamespace(
            MEDIA_16_GIB_PROFILE=SimpleNamespace(name="media-acceptance-16gib-v1"),
            PROBE_BYTES=8 * 1024 * 1024,
            PROBE_SECONDS=3,
            GuardFailure=GuardFailure,
            run_probe=mock.Mock(),
        )
        output = io.StringIO()
        with mock.patch.object(self.launcher, "require_linux"), \
             mock.patch.object(self.launcher, "ensure_python_stage"), \
             mock.patch.object(self.launcher, "target_identity", return_value=(1009, 1010)), \
             mock.patch.object(self.launcher.os, "geteuid", return_value=1009, create=True), \
             mock.patch.object(self.launcher.os, "getegid", return_value=1010, create=True), \
             mock.patch.object(self.launcher.os, "getgroups", return_value=[], create=True), \
             mock.patch.object(self.launcher, "verify_candidate"), \
             mock.patch.object(self.launcher, "validate_fixed_inputs"), \
             mock.patch.object(self.launcher, "read_host_memory"), \
             mock.patch.object(self.launcher, "load_candidate_module", return_value=guard), \
             mock.patch.object(self.launcher, "verify_private_run_root") as run_root, \
             mock.patch.object(self.launcher, "verify_inherited_media_lock") as lock, \
             mock.patch("sys.stdout", new=output):
            self.assertEqual(self.launcher.internal_verified_stage(spec, 7), 0)
        guard.run_probe.assert_called_once_with(
            spec.unit, 2, 7, profile=guard.MEDIA_16_GIB_PROFILE
        )
        run_root.assert_not_called()
        lock.assert_not_called()
        value = json.loads(output.getvalue())
        self.assertFalse(value["media_started"])
        self.assertFalse(value["media_acceptance"])
        self.assertEqual(value["guard_profile"], "media-acceptance-16gib-v1")
        self.assertEqual(value["memory_bytes"], 16 * 1024 ** 3)
        self.assertEqual((value["ffmpeg_processes"], value["ffprobe_processes"]), (0, 0))

    def test_prepare_and_decode_commands_are_frozen_and_never_accept_paths(self):
        prepare = self.launcher.build_spec(
            self.SHA, "accept01", "short", "2c2t", "prepare-short")
        command = self.launcher.prepare_short_command(prepare)
        self.assertEqual(command[0], self.launcher.path_text(self.launcher.FFMPEG_PATH))
        self.assertEqual(command[command.index("-i") + 1],
                         self.launcher.path_text(self.launcher.LONG_SOURCE))
        self.assertEqual(command[command.index("-t") + 1], "120")
        self.assertEqual(command[command.index("-c") + 1], "copy")
        self.assertEqual(command[command.index("-f") + 1], "mp4")
        self.assertEqual(command[-1], self.launcher.path_text(
            prepare.run_root / self.launcher.PREPARED_SHORT_PART_NAME))
        probe = self.launcher.prepare_short_probe_command(prepare)
        self.assertEqual(probe[0], self.launcher.path_text(self.launcher.FFPROBE_PATH))
        decode = self.launcher.build_spec(
            self.SHA, "accept01", "short", "4c4t", "decode", "r2")
        command = self.launcher.decode_command(decode)
        self.assertIn("-xerror", command)
        self.assertEqual(command[command.index("-i") + 1],
                         self.launcher.path_text(decode.output_dir / "result.mp4"))
        self.assertEqual(command[-3:], ["-f", "null", "-"])
        self.assertNotIn("http", " ".join(command).lower())
        self.assertNotIn("cos", " ".join(command).lower())

    def test_prepare_uses_exclusive_part_and_atomic_no_overwrite_commit(self):
        spec = self.launcher.build_spec(
            self.SHA, "accept01", "short", "2c2t", "prepare-short")
        initial = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1,
                                  st_uid=1009, st_gid=1010, st_dev=11, st_ino=22,
                                  st_size=0)
        completed = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1,
                                    st_uid=1009, st_gid=1010, st_dev=11, st_ino=22,
                                    st_size=123)
        probe = json.dumps({"format": {"duration": "120.0"}, "streams": [
            {"codec_type": "video"}, {"codec_type": "audio"}
        ]}).encode()
        fingerprint = {"sha256": "b" * 64, "size_bytes": 123}
        source_fingerprint = {"sha256": "8" * 64, "size_bytes": 5139047136,
                              "device": 31, "inode": 32, "mtime_ns": 33, "nlink": 1}
        with mock.patch.object(self.launcher, "validate_existing_action_inputs"), \
             mock.patch.object(self.launcher.os, "O_DIRECTORY", 0, create=True), \
             mock.patch.object(self.launcher.os, "O_CLOEXEC", 0, create=True), \
             mock.patch.object(self.launcher.os, "open", side_effect=[50, 51, 52]) as opened, \
             mock.patch.object(self.launcher.os, "fstat", side_effect=[initial, completed]), \
             mock.patch.object(self.launcher.os, "close"), \
             mock.patch.object(self.launcher.os, "fchmod"), \
             mock.patch.object(self.launcher.os, "fsync"), \
             mock.patch.object(self.launcher.os, "link") as link, \
             mock.patch.object(self.launcher.os, "unlink") as unlink, \
             mock.patch.object(self.launcher, "require_owned_regular", return_value=completed), \
             mock.patch.object(self.launcher, "fingerprint_regular", return_value=fingerprint), \
             mock.patch.object(self.launcher, "fingerprint_fixed_input",
                               side_effect=[source_fingerprint, source_fingerprint]), \
             mock.patch.object(self.launcher, "run_fixed_child", side_effect=[
                 {"stdout": b"", "elapsed_seconds": 1,
                  "minimum_mem_available_bytes": 30 * 1024 ** 3},
                 {"stdout": probe, "elapsed_seconds": 1,
                  "minimum_mem_available_bytes": 29 * 1024 ** 3},
             ]) as child, \
             mock.patch.object(self.launcher, "write_exclusive_json") as write, \
             mock.patch.object(self.launcher, "validate_prepared_short"):
            result = self.launcher.run_prepare_short(spec, 1009, 1010, 9, object())
        self.assertTrue(result["ok"])
        part_open = opened.call_args_list[1]
        self.assertEqual(part_open.args[0], self.launcher.PREPARED_SHORT_PART_NAME)
        self.assertTrue(part_open.args[1] & os.O_EXCL)
        self.assertEqual(part_open.kwargs["dir_fd"], 50)
        self.assertEqual(child.call_count, 2)
        link.assert_called_once_with(
            self.launcher.PREPARED_SHORT_PART_NAME, self.launcher.PREPARED_SHORT_NAME,
            src_dir_fd=50, dst_dir_fd=50, follow_symlinks=False,
        )
        unlink.assert_called_once_with(self.launcher.PREPARED_SHORT_PART_NAME, dir_fd=50)
        evidence = write.call_args.args[1]
        self.assertEqual(evidence["prepared_sha256"], "b" * 64)
        self.assertEqual(evidence["source_sha256"], "8" * 64)
        self.assertEqual((evidence["source_device"], evidence["source_inode"]), (31, 32))
        self.assertGreaterEqual(evidence["source_fingerprint_elapsed_seconds"], 0)
        self.assertEqual((evidence["cos_uploads"], evidence["production_requests"]), (0, 0))

    def test_prepare_rejects_long_source_changed_during_stream_copy(self):
        before = {"sha256": "6" * 64, "size_bytes": self.launcher.LONG_SOURCE_SIZE,
                  "device": 1, "inode": 2, "mtime_ns": 3, "nlink": 1}
        after = dict(before, sha256="7" * 64, mtime_ns=4)
        with mock.patch.object(self.launcher, "fingerprint_fixed_input", return_value=after), \
             self.assertRaises(self.launcher.LaunchFailure) as caught:
            self.launcher.verify_fixed_input_unchanged(
                self.launcher.LONG_SOURCE, before,
                "fixed_long_source_fingerprint_failed",
                expected_size=self.launcher.LONG_SOURCE_SIZE,
            )
        self.assertEqual(str(caught.exception), "fixed_long_source_changed")

    def test_benchmark_final_source_recheck_rejects_concurrent_mutation(self):
        from scripts import benchmark_drama_synthesis_media as benchmark
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, recipe = root / "source.mp4", root / "recipe.json"
            source.write_bytes(b"source")
            recipe.write_text(json.dumps({"recipe_sha256": "8" * 64}))
            args = SimpleNamespace(
                source=str(source), recipe=str(recipe), output_dir=str(root / "result"),
                sample_kind="short", filter_threads=2, ffmpeg="ffmpeg", ffprobe="ffprobe",
                timeout=60, asset_root=str(root / "assets"),
                asset_manifest_sha256="9" * 64,
            )
            before = {"sha256": "a" * 64, "size_bytes": 6,
                      "device": 1, "inode": 2, "mtime_ns": 3, "nlink": 1}
            after = dict(before, sha256="b" * 64, mtime_ns=4)
            original = Path.is_file
            with mock.patch.object(Path, "is_file", autospec=True,
                                   side_effect=lambda path: True if path == Path("/proc/self/stat")
                                   else original(path)), \
                 mock.patch.object(benchmark, "stable_source_fingerprint",
                                   side_effect=[before, after]), \
                 mock.patch.object(benchmark.gpu, "_probe", return_value={"duration": 5}), \
                 mock.patch.object(benchmark, "cgroup_limits", return_value={}), \
                 mock.patch.object(benchmark.gpu, "render_random_output", return_value={}), \
                 mock.patch.object(benchmark.subprocess, "Popen") as popen:
                result = benchmark.benchmark_render(args)
        self.assertFalse(result["ok"])
        self.assertFalse(result["source_unchanged"])
        self.assertEqual(result["error_code"], "benchmark_source_changed")
        self.assertEqual(result["resource_guard"]["phase"], "source_recheck")
        popen.assert_not_called()

    def test_all_short_trials_validate_the_same_prepare_evidence_and_sha(self):
        evidence = {
            "version": 1, "ok": True, "operation": "prepare-short",
            "candidate_sha": self.SHA, "run_id": "accept01", "sample_kind": "short",
            "configuration": "2c2t",
            "unit": "drama-media-prepare-%s-accept01.service" % self.SHA[:12],
            "source": self.launcher.path_text(self.launcher.LONG_SOURCE),
            "source_size": self.launcher.LONG_SOURCE_SIZE,
            "source_sha256": "7" * 64, "source_device": 41, "source_inode": 42,
            "source_mtime_ns": 43, "source_nlink": 1,
            "source_fingerprint_elapsed_seconds": 1.5,
            "minimum_mem_available_bytes": 16 * 1024 ** 3,
            "host_memory_stop_threshold_bytes": 8 * 1024 ** 3,
            "host_memory_sampling_interval_seconds": 1,
            "prepared_path": self.launcher.path_text(self.spec.prepared_short_path),
            "prepared_sha256": "c" * 64, "prepared_size": 456,
            "duration_seconds": 120.0, "cos_uploads": 0, "production_requests": 0,
        }
        with mock.patch.object(self.launcher, "read_owned_json", return_value=evidence), \
             mock.patch.object(self.launcher, "fingerprint_regular",
                               return_value={"sha256": "c" * 64, "size_bytes": 456}):
            for trial in ("r1", "r2"):
                spec = self.launcher.build_spec(
                    self.SHA, "accept01", "short", "4c2t", "render", trial)
                self.assertIs(self.launcher.validate_prepared_short(spec, 1009, 1010), evidence)
        with mock.patch.object(self.launcher, "read_owned_json", return_value=evidence), \
             mock.patch.object(self.launcher, "fingerprint_regular",
                               return_value={"sha256": "d" * 64, "size_bytes": 456}), \
             self.assertRaises(self.launcher.LaunchFailure) as caught:
            self.launcher.validate_prepared_short(self.spec, 1009, 1010)
        self.assertEqual(str(caught.exception), "prepared_short_sha256_mismatch")

    def test_action_child_timeout_and_cleanup_failure_are_distinct(self):
        launcher = self.launcher

        class Process:
            def __init__(self, cleanup_fails=False):
                self.returncode, self.cleanup_fails, self.waits = None, cleanup_fails, 0
            def poll(self): return self.returncode
            def kill(self):
                if self.cleanup_fails:
                    raise OSError(URL)
                self.returncode = -9
            def wait(self, timeout):
                self.waits += 1
                if self.waits == 1 or self.cleanup_fails:
                    raise subprocess.TimeoutExpired("fixed", timeout)
                return self.returncode

        safe_memory = {"MemTotal": 32 * 1024 ** 3,
                       "MemAvailable": 32 * 1024 ** 3}
        with mock.patch.object(launcher, "read_host_memory", return_value=safe_memory), \
             mock.patch.object(launcher.time, "monotonic", side_effect=[0, 0, 2]), \
             self.assertRaises(launcher.LaunchFailure) as caught:
            launcher.run_fixed_child(
                SimpleNamespace(launch_renderer_process=mock.Mock(return_value=Process())),
                ["fixed"], 9, timeout=1, failure_code="fixed_failed",
                timeout_code="fixed_timeout", cleanup_code="fixed_cleanup_failed",
            )
        self.assertEqual(str(caught.exception), "fixed_timeout")
        with mock.patch.object(launcher, "read_host_memory", return_value=safe_memory), \
             mock.patch.object(launcher.time, "monotonic", side_effect=[0, 0, 2]), \
             self.assertRaises(launcher.LaunchFailure) as caught:
            launcher.run_fixed_child(
                SimpleNamespace(launch_renderer_process=mock.Mock(
                    return_value=Process(cleanup_fails=True))),
                ["fixed"], 9, timeout=1, failure_code="fixed_failed",
                timeout_code="fixed_timeout", cleanup_code="fixed_cleanup_failed",
            )
        self.assertEqual(str(caught.exception), "fixed_cleanup_failed")

    def test_action_child_memory_drop_is_sampled_within_one_second_and_reaped(self):
        launcher = self.launcher
        events = []

        class Process:
            returncode = None

            def poll(self):
                return self.returncode

            def kill(self):
                events.append("kill")
                self.returncode = -9

            def wait(self, timeout):
                events.append(("wait", timeout))
                if self.returncode is None:
                    raise subprocess.TimeoutExpired("fixed", timeout)
                return self.returncode

        gib = 1024 ** 3
        memory = [
            {"MemTotal": 32 * gib, "MemAvailable": 32 * gib},
            {"MemTotal": 32 * gib, "MemAvailable": 16 * gib},
            {"MemTotal": 32 * gib, "MemAvailable": 8 * gib - 1},
        ]
        with mock.patch.object(launcher, "read_host_memory", side_effect=memory) as sampled, \
             self.assertRaises(launcher.LaunchFailure) as caught:
            launcher.run_fixed_child(
                SimpleNamespace(launch_renderer_process=mock.Mock(return_value=Process())),
                ["fixed"], 9, timeout=30, failure_code="fixed_failed",
                timeout_code="fixed_timeout", cleanup_code="fixed_cleanup_failed",
            )
        self.assertEqual(str(caught.exception), "host_memory_below_media_stop_gate")
        self.assertEqual(caught.exception.minimum_mem_available_bytes, 8 * gib - 1)
        self.assertEqual(caught.exception.host_memory_stop_threshold_bytes, 8 * gib)
        self.assertEqual(sampled.call_count, 3)
        self.assertEqual(events[-2:], ["kill", ("wait", 30)])
        self.assertLessEqual(events[0][1], 1.0)

    def test_action_child_success_returns_minimum_memory_observation(self):
        launcher = self.launcher
        waits = []

        class Process:
            returncode = 0

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                waits.append(timeout)
                return self.returncode

        gib = 1024 ** 3
        memory = [
            {"MemTotal": 32 * gib, "MemAvailable": 32 * gib},
            {"MemTotal": 32 * gib, "MemAvailable": 16 * gib},
            {"MemTotal": 32 * gib, "MemAvailable": 12 * gib},
        ]
        with mock.patch.object(launcher, "read_host_memory", side_effect=memory):
            result = launcher.run_fixed_child(
                SimpleNamespace(launch_renderer_process=mock.Mock(return_value=Process())),
                ["fixed"], 9, timeout=30, failure_code="fixed_failed",
                timeout_code="fixed_timeout", cleanup_code="fixed_cleanup_failed",
            )
        self.assertEqual(result["minimum_mem_available_bytes"], 12 * gib)
        self.assertEqual(result["host_memory_stop_threshold_bytes"], 8 * gib)
        self.assertEqual(result["host_memory_sampling_interval_seconds"], 1)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(waits), 1)
        self.assertLessEqual(waits[0], 1.0)

    def test_decode_consumes_only_matching_result_and_writes_exclusive_evidence(self):
        spec = self.launcher.build_spec(
            self.SHA, "accept01", "short", "4c2t", "decode", "r2")
        identity = {"device": 1, "inode": 2, "size_bytes": 999,
                    "mtime_ns": 3, "nlink": 1}
        frozen = {"artifact": {"sha256": "e" * 64, "size_bytes": 999},
                  "artifact_identity": identity,
                  "benchmark_evidence_sha256": "f" * 64}
        with mock.patch.object(self.launcher, "validate_render_result", return_value=frozen), \
             mock.patch.object(self.launcher, "run_fixed_child",
                               return_value={"elapsed_seconds": 12.5, "exit_code": 0,
                                             "minimum_mem_available_bytes":
                                             16 * 1024 ** 3}) as child, \
             mock.patch.object(self.launcher, "write_exclusive_json") as write:
            result = self.launcher.run_decode(spec, 1009, 1010, 9, object())
        self.assertTrue(result["ok"])
        command = child.call_args.args[1]
        self.assertEqual(command[command.index("-i") + 1],
                         self.launcher.path_text(spec.output_dir / "result.mp4"))
        self.assertEqual(command[-3:], ["-f", "null", "-"])
        self.assertEqual(child.call_args.args[2], 9)
        evidence = write.call_args.args[1]
        self.assertEqual(evidence["trial"], "r2")
        self.assertEqual(evidence["exit_code"], 0)
        self.assertEqual(evidence["render_unit"], self.launcher.build_spec(
            self.SHA, "accept01", "short", "4c2t", "render", "r2"
        ).unit)
        self.assertTrue(evidence["result_reverified_after_decode"])
        self.assertEqual(evidence["result_identity_before"], identity)
        self.assertEqual(evidence["result_identity_after"], identity)
        self.assertEqual(evidence["minimum_mem_available_bytes"], 16 * 1024 ** 3)
        self.assertEqual(evidence["generated_video_files"], 0)
        self.assertEqual((evidence["cos_uploads"], evidence["production_requests"]), (0, 0))

    def test_decode_result_validation_binds_candidate_run_config_trial_and_hashes(self):
        spec = self.launcher.build_spec(
            self.SHA, "accept01", "short", "4c2t", "decode", "r2")
        render = self.launcher.build_spec(
            self.SHA, "accept01", "short", "4c2t", "render", "r2")
        artifact = {"sha256": "1" * 64, "size_bytes": 1234}
        evidence_fp = {"sha256": "2" * 64, "size_bytes": 4321}
        launcher = {
            "version": 1, "ok": True, "operation": "render",
            "candidate_sha": self.SHA, "run_id": "accept01", "sample_kind": "short",
            "configuration": "4c2t", "trial": "r2", "unit": render.unit,
            "benchmark_evidence": self.launcher.path_text(render.output_dir / "evidence.json"),
            "benchmark_evidence_sha256": evidence_fp["sha256"],
            "output_sha256": artifact["sha256"], "output_size": artifact["size_bytes"],
            "source_sha256": "3" * 64, "source_size": 5678,
            "minimum_mem_available_bytes": 16 * 1024 ** 3,
            "host_memory_stop_threshold_bytes": 8 * 1024 ** 3,
            "host_memory_sampling_interval_seconds": 1,
            "cos_uploads": 0, "production_requests": 0,
        }
        benchmark = {
            "version": 1, "kind": "render", "ok": True, "sample_kind": "short",
            "filter_threads": 2, "recipe_sha256": self.launcher.RECIPE_SHA256,
            "asset_manifest_sha256": self.launcher.ASSET_MANIFEST_SHA256,
            "duration_seconds": 120, "render_timeout_seconds": 43200,
            "render_planned_timeout_seconds": 43200,
            "render_global_cap_seconds": 86400,
            "acceptance_launcher_lock_inherited": True,
            "minimum_mem_available_bytes": 16 * 1024 ** 3,
            "source": {"sha256": "3" * 64, "size_bytes": 5678},
            "source_final": {"sha256": "3" * 64, "size_bytes": 5678},
            "source_unchanged": True,
            "source_identity": {"device": 1, "inode": 2, "mtime_ns": 3, "nlink": 1},
            "source_final_identity": {"device": 1, "inode": 2, "mtime_ns": 3, "nlink": 1},
            "cos_uploads": 0, "production_requests": 0,
            "result": {"output_sha256": artifact["sha256"],
                       "output_size": artifact["size_bytes"]},
        }
        directory = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=1009,
                                    st_gid=1010)
        artifact_identity = {"device": 7, "inode": 8, "size_bytes": 1234,
                             "mtime_ns": 9, "nlink": 1}
        with mock.patch.object(self.launcher.os, "lstat", return_value=directory), \
             mock.patch.object(self.launcher, "validate_action_completion"), \
             mock.patch.object(self.launcher, "read_owned_json",
                               side_effect=[launcher, benchmark]), \
             mock.patch.object(self.launcher, "fingerprint_regular",
                               side_effect=[artifact, evidence_fp]), \
             mock.patch.object(self.launcher, "owned_regular_identity",
                               return_value=artifact_identity), \
             mock.patch.object(self.launcher, "validate_prepared_short", return_value={
                 "prepared_sha256": "3" * 64, "prepared_size": 5678
             }):
            frozen = self.launcher.validate_render_result(spec, 1009, 1010)
        self.assertEqual(frozen["artifact"], artifact)
        self.assertEqual(frozen["artifact_identity"], artifact_identity)
        tampered = dict(launcher, trial="r1")
        with mock.patch.object(self.launcher.os, "lstat", return_value=directory), \
             mock.patch.object(self.launcher, "validate_action_completion"), \
             mock.patch.object(self.launcher, "read_owned_json",
                               side_effect=[tampered, benchmark]), \
             mock.patch.object(self.launcher, "fingerprint_regular",
                               side_effect=[artifact, evidence_fp]), \
             mock.patch.object(self.launcher, "owned_regular_identity",
                               return_value=artifact_identity), \
             mock.patch.object(self.launcher, "validate_prepared_short", return_value={
                 "prepared_sha256": "3" * 64, "prepared_size": 5678
             }), \
             self.assertRaises(self.launcher.LaunchFailure) as caught:
            self.launcher.validate_render_result(spec, 1009, 1010)
        self.assertEqual(str(caught.exception), "render_launcher_evidence_invalid")


class BenchmarkCgroupTests(unittest.TestCase):
    def read_fixture(self, membership, mountinfo, files):
        from scripts import benchmark_drama_synthesis_media as benchmark
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        proc_cgroup, proc_mountinfo = root / "membership", root / "mountinfo"
        proc_cgroup.write_text(membership, encoding="utf-8")
        proc_mountinfo.write_text(mountinfo, encoding="utf-8")
        for name, value in files.items():
            path = root.joinpath(*name.lstrip("/").split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="ascii")
        return benchmark.cgroup_limits(proc_cgroup=proc_cgroup, proc_mountinfo=proc_mountinfo,
                                       filesystem_root=root)

    @staticmethod
    def v1_fixture():
        membership = "3:cpu,cpuacct:/system.slice/test.service\n4:memory:/system.slice/test.service\n5:pids:/system.slice/test.service\n"
        mountinfo = (
            "31 20 0:27 /system.slice /sys/fs/cgroup/cpu\\040pool rw,nosuid - cgroup cgroup rw,cpuacct,cpu\n"
            "32 20 0:28 / /sys/fs/cgroup/memory rw,nosuid - cgroup cgroup rw,memory\n"
            "33 20 0:29 / /sys/fs/cgroup/pids rw,nosuid - cgroup cgroup rw,pids\n"
        )
        files = {
            "/sys/fs/cgroup/cpu pool/test.service/cpu.cfs_quota_us": "200000\n",
            "/sys/fs/cgroup/cpu pool/test.service/cpu.cfs_period_us": "100000\n",
            "/sys/fs/cgroup/memory/system.slice/test.service/memory.limit_in_bytes": "17179869184\n",
            "/sys/fs/cgroup/memory/system.slice/test.service/memory.memsw.limit_in_bytes": "21474836480\n",
            "/sys/fs/cgroup/pids/system.slice/test.service/pids.max": "128\n",
        }
        return membership, mountinfo, files

    @staticmethod
    def v2_fixture():
        return (
            "0::/tenant/test.service\n",
            "40 20 0:32 /tenant /custom/cgroup rw,nosuid - cgroup2 cgroup rw\n",
            {"/custom/cgroup/test.service/cpu.max": "400000 100000\n",
             "/custom/cgroup/test.service/memory.max": "17179869184\n",
             "/custom/cgroup/test.service/memory.swap.max": "0\n",
             "/custom/cgroup/test.service/pids.max": "128\n"},
        )

    def test_v1_combined_cpu_mount_and_separate_memory_pids_respect_mount_root(self):
        result = self.read_fixture(*self.v1_fixture())
        self.assertEqual(result["cgroup_version"], 1)
        self.assertEqual(result["limit_read_status"], "complete")
        self.assertTrue(result["cpu_quota_read"])
        self.assertEqual(result["cpu_quota_cores"], 2)
        self.assertEqual(result["controllers"]["cpu"]["directory"], "/sys/fs/cgroup/cpu pool/test.service")
        self.assertEqual(result["cpu.cfs_quota_us"], "200000")
        self.assertEqual(result["cpu.cfs_period_us"], "100000")
        self.assertEqual(result["memory.limit_in_bytes"], "17179869184")
        self.assertEqual(result["memory.memsw.limit_in_bytes"], "21474836480")
        self.assertEqual(result["pids.max"], "128")
        self.assertFalse(result["ancestor_limits_checked"])

    def test_v2_uses_actual_mount_and_exposes_swap_limit(self):
        result = self.read_fixture(*self.v2_fixture())
        self.assertEqual(result["cgroup_version"], 2)
        self.assertEqual(result["limit_read_status"], "complete")
        self.assertEqual(result["cpu.max"], "400000 100000")
        self.assertEqual(result["cpu_quota_cores"], 4)
        self.assertEqual(result["memory.swap.max"], "0")
        self.assertEqual(result["controllers"]["memory"]["directory"], "/custom/cgroup/test.service")
        self.assertFalse(result["ancestor_limits_checked"])

    def test_v1_missing_memsw_does_not_claim_swap_protection(self):
        membership, mounts, files = self.v1_fixture()
        del files[next(name for name in files if name.endswith("memory.memsw.limit_in_bytes"))]
        result = self.read_fixture(membership, mounts, files)
        self.assertTrue(result["cpu_quota_read"])
        self.assertEqual(result["limit_read_status"], "partial")
        self.assertEqual(result["read_errors"]["memory.memsw.limit_in_bytes"], "unreadable")
        self.assertNotIn("memory.memsw.limit_in_bytes", result)
        self.assertNotIn("memory.swap.max", result)

    def test_absent_invalid_or_oversized_cpu_limits_are_not_verified(self):
        for factory, suffix in ((self.v1_fixture, "cpu.cfs_period_us"), (self.v2_fixture, "cpu.max")):
            for value in (None, "0\n", "9" * 257):
                with self.subTest(version=factory.__name__, value=value):
                    membership, mounts, files = factory()
                    path = next(name for name in files if name.endswith(suffix))
                    if value is None:
                        del files[path]
                    else:
                        files[path] = value
                    result = self.read_fixture(membership, mounts, files)
                    self.assertEqual(result["limit_read_status"], "partial")
                    self.assertFalse(result["cpu_quota_read"])
                    self.assertNotIn("cpu_quota_cores", result)
                    self.assertEqual(result["read_errors"][suffix], "unreadable" if value is None else "invalid")

    def test_missing_proc_files_report_unavailable_without_hiding_failure(self):
        from scripts import benchmark_drama_synthesis_media as benchmark
        with tempfile.TemporaryDirectory() as directory:
            result = benchmark.cgroup_limits(proc_cgroup=Path(directory) / "missing-cgroup",
                                             proc_mountinfo=Path(directory) / "missing-mountinfo")
        self.assertEqual(result["limit_read_status"], "unavailable")
        self.assertFalse(result["cpu_quota_read"])
        self.assertEqual(result["read_errors"], {"membership": "unreadable", "mountinfo": "unreadable"})

    def test_unsafe_membership_and_unmatched_mount_cannot_use_another_cgroup(self):
        membership, mounts, files = self.v2_fixture()
        for path in ("/tenant/../test.service", "/other/test.service"):
            with self.subTest(path=path):
                result = self.read_fixture("0::" + path + "\n", mounts, files)
                self.assertEqual(result["limit_read_status"], "unavailable")
                self.assertFalse(result["cpu_quota_read"])
                self.assertNotIn("cpu.max", result)
                self.assertTrue(result["read_errors"])

    def test_unlimited_cpu_is_read_but_never_reported_as_a_two_or_four_core_quota(self):
        for factory, suffix, value in ((self.v1_fixture, "cpu.cfs_quota_us", "-1\n"),
                                       (self.v2_fixture, "cpu.max", "max 100000\n")):
            with self.subTest(version=factory.__name__):
                membership, mounts, files = factory()
                files[next(name for name in files if name.endswith(suffix))] = value
                result = self.read_fixture(membership, mounts, files)
                self.assertEqual(result["limit_read_status"], "complete")
                self.assertTrue(result["cpu_quota_read"])
                self.assertIsNone(result["cpu_quota_cores"])


class BenchmarkRenderGuardTests(unittest.TestCase):
    def setUp(self):
        from scripts import benchmark_drama_synthesis_media as benchmark
        self.benchmark = benchmark
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        source, recipe = self.root / "source.mp4", self.root / "recipe.json"
        source.write_bytes(b"local-test-source")
        recipe.write_text(json.dumps({"recipe_sha256": "a" * 64}), encoding="utf-8")
        self.args = SimpleNamespace(source=str(source), recipe=str(recipe), output_dir=str(self.root / "result"),
                                    sample_kind="short", filter_threads=2, ffmpeg="fake-ffmpeg", ffprobe="fake-ffprobe",
                                    timeout=60, asset_root=str(self.root / "assets"), asset_manifest_sha256="b" * 64)
        self.events, self.ignore_runner_error = [], False
        events = self.events

        class Process:
            pid = 87654321

            def __init__(self):
                self.stdout = io.StringIO("frame=1\nout_time_us=1000\nprogress=continue\n")
                self.returncode, self.killed, self.pending_waits = None, False, 0

            def wait(self, timeout):
                events.append(("wait", timeout))
                if self.killed:
                    self.returncode = -9
                elif self.pending_waits:
                    self.pending_waits -= 1
                    raise subprocess.TimeoutExpired("fake-ffmpeg", timeout)
                else:
                    self.returncode = 0
                return self.returncode

            def poll(self):
                return self.returncode

            def kill(self):
                events.append(("kill", self.pid))
                self.killed = True

        self.process = Process()

        def patch(target, name, **kwargs):
            value = mock.patch.object(target, name, **kwargs)
            result = value.start()
            self.addCleanup(value.stop)
            return result

        original_is_file = Path.is_file
        patch(Path, "is_file", autospec=True, side_effect=lambda path:
              True if path == Path("/proc/self/stat") else original_is_file(path))
        patch(benchmark.gpu, "_probe", return_value={"duration": 5})
        patch(benchmark, "cgroup_limits", return_value={"cgroup_version": 1})
        self.host = patch(benchmark, "host_memory_sample", return_value={
            "mem_available_bytes": 8 * 1024 ** 3, "mem_total_bytes": 32 * 1024 ** 3})
        self.sample = patch(benchmark, "process_sample", return_value={
            "rss_bytes": 14 * 1024 ** 3 - 1, "threads": 120, "cpu_seconds": 1.0})
        self.popen = patch(benchmark.subprocess, "Popen", return_value=self.process)

        def render(**kwargs):
            try:
                planned = benchmark.gpu.render_budget_seconds(5, kwargs["timeout"])
                kwargs["runner"](["fake-ffmpeg", str(kwargs["output"])], timeout=planned)
            except benchmark.BenchmarkGuardError:
                if not self.ignore_runner_error:
                    raise
            Path(kwargs["output"]).write_bytes(b"completed-test-output")
            return {"output_sha256": "c" * 64}

        patch(benchmark.gpu, "render_random_output", side_effect=render)

    def run_benchmark(self):
        result = self.benchmark.benchmark_render(self.args)
        persisted = json.loads((Path(self.args.output_dir) / "evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted, result)
        return result

    def assert_stopped(self, result, reason):
        self.assertFalse(result["ok"])
        self.assertTrue(result["resource_guard"]["triggered"])
        self.assertEqual(result["error_code"], reason)
        self.assertEqual(self.process.returncode, -9)
        self.assertEqual([x for x in self.events if x[0] == "kill"], [("kill", self.process.pid)])
        self.assertEqual(self.events[-1], ("wait", 30))
        self.assertTrue(self.process.stdout.closed)
        self.assertTrue(result["resource_guard"]["observed_at_utc"])
        self.assertGreaterEqual(result["resource_guard"]["elapsed_seconds"], 0)

    def test_exact_safe_boundaries_succeed_and_host_memory_is_recorded(self):
        original = self.benchmark.gpu.run_render_with_progress
        with mock.patch.object(self.benchmark.gpu, "run_render_with_progress",
                               wraps=original) as tracked:
            result = self.run_benchmark()
        self.assertTrue(result["ok"])
        self.assertFalse(result["resource_guard"]["triggered"])
        self.assertTrue(result["resource_guard"]["outer_cgroup_hard_limits_required"])
        self.assertEqual(self.host.call_count, 3)
        self.assertEqual(self.events, [("wait", 1)])
        rows = [json.loads(x) for x in (Path(self.args.output_dir) / "process-samples.jsonl").read_text().splitlines()]
        self.assertTrue(all(row["mem_available_bytes"] == 8 * 1024 ** 3 for row in rows))
        self.assertEqual(result["render_timeout_seconds"], 60)
        self.assertEqual(result["render_planned_timeout_seconds"], 43200)
        self.assertEqual(result["render_global_cap_seconds"], 86400)
        self.assertEqual(tracked.call_args.kwargs["timeout"], 43200)
        self.assertEqual(tracked.call_args.kwargs["configured_timeout"], 60)
        self.assertEqual(tracked.call_args.kwargs["absolute_timeout"], 86400)

    def test_low_memory_before_launch_never_starts_a_child(self):
        self.host.return_value["mem_available_bytes"] -= 1
        result = self.run_benchmark()
        self.assertFalse(result["ok"])
        self.assertEqual(result["resource_guard"]["phase"], "before_launch")
        self.assertEqual(result["error_code"], "benchmark_host_memory_low")
        self.popen.assert_not_called()

    def test_host_read_failure_before_launch_never_starts_a_child(self):
        self.host.side_effect = self.benchmark.BenchmarkGuardError("benchmark_host_memory_unavailable")
        result = self.run_benchmark()
        self.assertEqual(result["error_code"], "benchmark_host_memory_unavailable")
        self.popen.assert_not_called()

    def test_rss_at_limit_stops_only_our_child_and_cleanup_does_not_resample(self):
        self.sample.return_value["rss_bytes"] = 14 * 1024 ** 3
        result = self.run_benchmark()
        self.assert_stopped(result, "benchmark_renderer_rss_limit")
        self.assertEqual(result["resource_guard"]["metrics"]["rss_bytes"], 14 * 1024 ** 3)
        self.assertEqual(result["peak_rss_bytes"], 14 * 1024 ** 3)
        self.assertEqual(self.host.call_count, 2)
        self.sample.assert_called_once_with(self.process.pid)

    def test_threads_above_limit_stop_only_our_child(self):
        self.sample.return_value["threads"] = 121
        result = self.run_benchmark()
        self.assert_stopped(result, "benchmark_renderer_thread_limit")
        self.assertEqual(result["resource_guard"]["metrics"]["threads"], 121)

    def test_running_host_pressure_is_checked_again_at_the_next_second(self):
        safe = self.host.return_value
        self.host.side_effect = [safe, safe, {**safe, "mem_available_bytes": 8 * 1024 ** 3 - 1}]
        self.process.pending_waits = 1
        result = self.run_benchmark()
        self.assert_stopped(result, "benchmark_host_memory_low")
        self.assertEqual(self.events[0], ("wait", 1))
        self.assertEqual(self.host.call_count, 3)

    def test_running_host_read_failure_still_reaps_without_another_read(self):
        safe = self.host.return_value
        self.host.side_effect = [safe, self.benchmark.BenchmarkGuardError("benchmark_host_memory_invalid")]
        result = self.run_benchmark()
        self.assert_stopped(result, "benchmark_host_memory_invalid")
        self.assertEqual(self.host.call_count, 2)

    def test_unreadable_live_child_metrics_are_not_silently_skipped(self):
        self.sample.return_value = None
        self.assert_stopped(self.run_benchmark(), "benchmark_process_sample_invalid")

    def test_nonfinite_child_metrics_are_rejected(self):
        self.sample.return_value["cpu_seconds"] = float("nan")
        self.assert_stopped(self.run_benchmark(), "benchmark_process_sample_invalid")

    def test_unexpected_sampling_exception_stops_child_without_leaking_details(self):
        self.sample.side_effect = RuntimeError(URL)
        result = self.run_benchmark()
        self.assert_stopped(result, "benchmark_resource_sampling_failed")
        self.assertNotIn(URL, json.dumps(result))

    def test_final_host_read_rejects_even_an_already_successful_process(self):
        safe = self.host.return_value
        self.host.side_effect = [safe, safe, {**safe, "mem_available_bytes": 7 * 1024 ** 3}]
        result = self.run_benchmark()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "benchmark_host_memory_low")
        self.assertEqual(self.process.returncode, 0)
        self.assertEqual(self.events, [("wait", 1)])

    def test_a_returned_output_cannot_override_a_sticky_protection_trigger(self):
        self.ignore_runner_error = True
        self.sample.return_value["rss_bytes"] = 14 * 1024 ** 3
        result = self.run_benchmark()
        self.assertTrue((Path(self.args.output_dir) / "result.mp4").exists())
        self.assert_stopped(result, "benchmark_renderer_rss_limit")
        self.assertNotIn("result", result)

    @contextmanager
    def failing_sample_log(self, failure):
        original_open = Path.open

        class Log:
            def write(self, value):
                if failure == "write":
                    raise OSError(URL)
                return len(value)

            def flush(self):
                if failure == "flush":
                    raise OSError(URL)

            def close(self):
                if failure == "close":
                    raise OSError(URL)

        def open_file(path, *args, **kwargs):
            if path.name == "process-samples.jsonl":
                if failure == "open":
                    raise OSError(URL)
                return Log()
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", autospec=True, side_effect=open_file):
            yield

    def test_log_open_failure_prevents_renderer_start(self):
        with self.failing_sample_log("open"):
            result = self.run_benchmark()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "benchmark_sample_log_failed")
        self.popen.assert_not_called()

    def test_log_write_failure_stops_and_reaps_renderer(self):
        with self.failing_sample_log("write"):
            result = self.run_benchmark()
        self.assert_stopped(result, "benchmark_sample_log_failed")
        self.assertNotIn(URL, json.dumps(result))

    def test_log_flush_failure_stops_and_reaps_renderer(self):
        with self.failing_sample_log("flush"):
            result = self.run_benchmark()
        self.assert_stopped(result, "benchmark_sample_log_failed")

    def test_log_close_failure_cannot_mark_completed_output_successful(self):
        with self.failing_sample_log("close"):
            result = self.run_benchmark()
        self.assertTrue((Path(self.args.output_dir) / "result.mp4").exists())
        self.assertFalse(result["ok"])
        self.assertEqual(result["resource_guard"]["phase"], "closing_log")
        self.assertEqual(self.process.returncode, 0)

    def test_final_evidence_failure_returns_no_success_and_retains_safe_diagnostic(self):
        real_write = self.benchmark.atomic_write_record
        writes = []

        def write(path, value):
            writes.append(path)
            if len(writes) == 2:
                raise OSError(URL)
            return real_write(path, value)

        with mock.patch.object(self.benchmark, "atomic_write_record", side_effect=write), \
                mock.patch.object(sys, "stderr", new_callable=io.StringIO) as error:
            with self.assertRaises(self.benchmark.BenchmarkGuardError):
                self.benchmark.benchmark_render(self.args)
        self.assertEqual(self.process.returncode, 0)
        self.assertIn("benchmark_evidence_write_failed", error.getvalue())
        self.assertNotIn(URL, error.getvalue())
        persisted = json.loads((Path(self.args.output_dir) / "evidence.json").read_text())
        self.assertFalse(persisted["ok"])


class BenchmarkPolicyTests(unittest.TestCase):
    def test_host_meminfo_requires_real_available_bytes_and_valid_units(self):
        from scripts import benchmark_drama_synthesis_media as benchmark
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "meminfo"
            with self.assertRaises(benchmark.BenchmarkGuardError):
                benchmark.host_memory_sample(path)
            path.write_text("MemTotal: 33554432 kB\nMemFree: 1 kB\nMemAvailable: 8388608 kB\n")
            self.assertEqual(benchmark.host_memory_sample(path)["mem_available_bytes"], 8 * 1024 ** 3)
            invalid = ("", "MemFree: 90 kB\n", "MemAvailable: 90 kB\n", "x" * 65537)
            invalid += tuple("MemTotal: 100 kB\nMemAvailable: " + value + "\n"
                             for value in ("-1 kB", "nan kB", "90 MB", "101 kB", "90 kB\nMemAvailable: 90 kB"))
            for text in invalid:
                with self.subTest(text=text[:80]), self.assertRaises(benchmark.BenchmarkGuardError):
                    path.write_text(text)
                    benchmark.host_memory_sample(path)

    def test_output_directory_is_new_absolute_and_never_overwritten(self):
        from scripts import benchmark_drama_synthesis_media as benchmark
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new-run"
            benchmark.fresh_directory(output)
            (output / "evidence.txt").write_text("keep")
            with self.assertRaises(ValueError):
                benchmark.fresh_directory(output)
            self.assertEqual((output / "evidence.txt").read_text(), "keep")
        with self.assertRaises(ValueError):
            benchmark.fresh_directory("relative-benchmark-output")

    def test_short_and_long_sources_must_meet_actual_duration_policy(self):
        from scripts import benchmark_drama_synthesis_media as benchmark
        benchmark.check_sample_duration("short", 120)
        benchmark.check_sample_duration("long", 93 * 60)
        for kind, duration in (("long", 5), ("short", 5400), ("long", float("nan")), ("long", 7201)):
            with self.subTest(kind=kind, duration=duration), self.assertRaises(ValueError):
                benchmark.check_sample_duration(kind, duration)

    def test_download_probe_budget_is_fixed_and_url_values_are_only_hashed(self):
        from scripts import benchmark_drama_synthesis_media as benchmark
        urls = [URL + str(index) for index in range(8)]
        digest, definition = benchmark.download_definition(urls, 32 * 1024 * 1024)
        self.assertEqual(len(digest), 64)
        self.assertEqual(len(definition["source_ids"]), 8)
        self.assertNotIn("never-print-this", json.dumps(definition))
        for values, count in ((urls, 33 * 1024 * 1024), (urls + [URL], 32 * 1024 * 1024), ([{}], 10), ([URL], True)):
            with self.assertRaises(ValueError):
                benchmark.download_definition(values, count)

    def test_cross_domain_comparison_never_changes_the_real_download_identity(self):
        from scripts import benchmark_drama_synthesis_media as benchmark
        domestic = "https://img.tianmai.cn/resource/series/video.mp4?token=test"
        overseas = domestic.replace("img.tianmai.cn", "accelerate.tianmai.cn")
        _, first = benchmark.download_definition([domestic], 10)
        _, second = benchmark.download_definition([overseas], 10)
        self.assertEqual(first["comparison_resource_ids"], second["comparison_resource_ids"])
        self.assertNotEqual(first["source_ids"], second["source_ids"])
        source = {"resource_id": first["comparison_resource_ids"][0], "size_bytes": 10,
                  "total_source_bytes": 100, "sample_sha256": "a" * 64}
        baseline = {"kind": "download", "ok": True, "workers": 1, "definition": first, "sources": [source], "bytes_per_second": 100}
        candidate = {"kind": "download", "ok": True, "workers": 1, "definition": second, "sources": [source], "bytes_per_second": 200}
        result = benchmark.compare_download_evidence(baseline, candidate)
        self.assertTrue(result["content_equal_for_sample"])
        self.assertEqual(result["throughput_ratio"], 2)
        self.assertIn("not-full-object-proof", result["comparison_scope"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
