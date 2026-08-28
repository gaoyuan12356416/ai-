#!/usr/bin/env python3
"""Offline HTTP/process doubles; no remote downloads, render jobs or COS writes."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests
from features.drama_synthesis import gpu, media_pipeline as media
from features.drama_synthesis.core import DramaSynthesisError, RECIPE_PROFILE
from features.drama_synthesis.local_checkpoint import file_fingerprint


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


def stream_info(width=360, audio=True):
    streams = [{"codec_type": "video", "codec_name": "h264", "width": width, "height": 640,
                "avg_frame_rate": "25/1", "time_base": "1/12800"}]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2, "time_base": "1/48000"})
    return {"streams": streams}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.items = [{"episode_url": str(index), "source_path": str(self.root / (str(index) + ".mp4"))} for index in range(3)]
        self.normalized = []

    def downloader(self, url, path, callback, **kwargs):
        Path(path).write_bytes(("source-" + url).encode())
        value = file_fingerprint(path)
        callback(value["size_bytes"], value["size_bytes"])
        return value

    def normalize(self, source, target):
        self.normalized.append(Path(source).name)
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

        def normalize(source, target):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            self.normalize(source, target)
            normalized_early.set()
            with guard:
                active -= 1

        outputs = media.download_and_prepare_segments(
            self.items, output_dir=self.root / "normalized", download_workers=2,
            probe=lambda path: stream_info(720 if Path(path).stem == "1" else 360),
            normalize=normalize, downloader=download,
        )
        self.assertEqual(peak, 1)
        self.assertEqual([Path(path).read_bytes() for path in outputs], [b"normalized-source-0", b"normalized-source-1", b"normalized-source-2"])

    def test_intro_is_first_and_uses_the_first_episode_as_reference(self):
        intro = self.root / "intro.mp4"
        intro.write_bytes(b"intro")
        factory = mock.Mock(return_value=str(intro))
        outputs = media.download_and_prepare_segments(
            self.items, output_dir=self.root / "normalized", probe=lambda path: stream_info(1280 if path == str(intro) else 360),
            normalize=self.normalize, downloader=self.downloader, intro_factory=factory,
        )
        factory.assert_called_once_with(self.items[0]["source_path"])
        self.assertEqual([Path(path).name for path in outputs], ["000.mp4", "001.mp4", "002.mp4", "003.mp4"])
        self.assertEqual(Path(outputs[0]).read_bytes(), b"normalized-intro")

    def test_single_segment_preserves_existing_fast_path_even_without_audio(self):
        outputs = media.download_and_prepare_segments(
            self.items[:1], output_dir=self.root / "normalized", probe=lambda _: stream_info(audio=False),
            normalize=self.normalize, downloader=self.downloader,
        )
        self.assertEqual(outputs, [self.items[0]["source_path"]])
        self.assertEqual(self.normalized, [])

    def test_normalized_checkpoints_replay_without_reencoding_and_reject_corruption(self):
        kwargs = dict(output_dir=self.root / "normalized", probe=lambda _: stream_info(audio=False),
                      normalize=self.normalize, downloader=self.downloader)
        original = media.download_and_prepare_segments(self.items, **kwargs)
        self.normalized.clear()
        self.assertEqual(media.download_and_prepare_segments(self.items, **kwargs), original)
        self.assertEqual(self.normalized, [])
        Path(original[0]).write_bytes(b"corrupt")
        with self.assertRaises(DramaSynthesisError):
            media.download_and_prepare_segments(self.items, **kwargs)
        self.assertEqual(self.normalized, [])

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

        def normalize(source, target):
            seen.append(("normalize", local.context))
            self.normalize(source, target)

        with mock.patch.object(async_runtime, "capture_context", return_value="frozen-context"), mock.patch.object(async_runtime, "use_context", use_context), mock.patch.object(async_runtime, "emit_progress"):
            media.download_and_prepare_segments(self.items, output_dir=self.root / "normalized",
                                              probe=lambda _: stream_info(audio=False), normalize=normalize, downloader=download)
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
        cases = [(None, None, 43200), ("21600", None, 21600), ("120", 36000, 36000), (None, 86400, 86400)]
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
        self.assertEqual(tracked.call_args.kwargs["timeout"], 28800)

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

        class Process:
            pid = 12345
            returncode = None
            stdout = io.StringIO("")
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

        with mock.patch.object(async_runtime, "process_launch", launch), mock.patch.object(async_runtime, "record_process"), mock.patch.object(async_runtime, "clear_process", side_effect=lambda _: events.append("clear")), mock.patch.object(async_runtime, "emit_progress"), mock.patch.object(gpu.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaises(TimeoutError):
                gpu.run_render_with_progress(["ffmpeg", "output.mp4"], timeout=1, duration_seconds=5, popen=lambda *_, **__: Process())
        self.assertEqual(events, ["kill", "wait", "clear"])


class ResourceGuardTests(unittest.TestCase):
    def setUp(self):
        from scripts import check_drama_media_resource_guard as guard
        self.guard = guard
        self.fixture()

    def fixture(self):
        guard = self.guard
        self.unit, self.pid = "drama-resource-guard-test-0123456789abcdef.service", 321
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
                        "memory.limit_in_bytes": str(8 * guard.MEMORY_BYTES if parent else guard.MEMORY_BYTES),
                        "memory.memsw.limit_in_bytes": str(8 * guard.MEMORY_BYTES),
                        "memory.usage_in_bytes": str(2 * guard.PROBE_BYTES if parent else guard.PROBE_BYTES),
                        "memory.memsw.usage_in_bytes": str(2 * guard.PROBE_BYTES if parent else guard.PROBE_BYTES),
                        "memory.use_hierarchy": "1", "memory.swappiness": "60", "memory.failcnt": "0",
                        "memory.memsw.failcnt": "0", "memory.oom_control": "oom_kill_disable 0\nunder_oom 0",
                        "memory.stat": "hierarchical_memory_limit %s\nhierarchical_memsw_limit %s\ntotal_swap 0" %
                                       (guard.MEMORY_BYTES, 8 * guard.MEMORY_BYTES),
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
                            "hierarchical_memsw_limit " + str(8 * guard.MEMORY_BYTES),
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
            self.guard.run_probe(self.unit, 2, read_fd)
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
                    self.guard.run_probe(self.unit, 2, read_fd)
                sleep.assert_not_called()


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


class BenchmarkPolicyTests(unittest.TestCase):
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
