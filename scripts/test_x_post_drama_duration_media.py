#!/usr/bin/env python3
"""Offline tests for duration-pending drama media preparation."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts import publish_media_repair, service  # noqa: E402


SOURCE_URL = "https://media.example.test/drama/source.mp4"
OUTPUT_URL = "https://media.example.test/drama/repaired.mp4"
MAX_BYTES = 1024 * 1024


def _queue():
    return {
        "id": 71,
        "source_type": "drama",
        "status": "queued",
        "media_validation_mode": service.MEDIA_VALIDATION_DEFERRED,
        "material_id": "90001",
        "material_url": SOURCE_URL,
        "content_id": "drama-17",
        "drama_pool_item_id": 31,
        "drama_pool_created_at": "2026-09-01T00:01:00Z",
        "episode_number": 3,
        "episode_key": "drama-17:3",
        "drama_replay_generation": 0,
        "account_id": 7,
        "account_username": "reeldrama",
        "account_drama_language": "en",
        "account_drama_language_frozen": 1,
        "schedule_run_id": 51,
        "run_date": "2026-09-01",
        "source_date": "2026-08-31",
        "delivery_mode": "duration_pending",
        "relay_account_id": 0,
        "relay_account_username": "",
        "preflight_sha256": "",
        "preflight_size": 0,
        "preflight_duration": 0.0,
        "original_material_url": "",
        "media_repair_trigger_code": "",
        "media_repair_job_key": "",
        "media_repair_profile": "",
        "media_repair_source_sha256": "",
        "route_version": service.DRAMA_DURATION_ROUTE_VERSION,
        "route_state": "duration_pending",
    }


def _probe(duration, size):
    return {
        "codec": "h264",
        "pixel_format": "yuv420p",
        "audio_codec": "aac",
        "width": 720,
        "height": 1280,
        "frame_rate": 30.0,
        "duration": float(duration),
        "size": int(size),
    }


def _resolved_queue(queue, evidence, *, relay=False):
    resolved = dict(queue)
    resolved.update(dict(evidence))
    resolved.update(
        {
            "status": "queued",
            "route_state": "resolved",
            "resolved_delivery_mode": (
                service.PREMIUM_RELAY_REPOST_MODE
                if relay
                else service.DIRECT_DELIVERY_MODE
            ),
            "delivery_mode": (
                service.PREMIUM_RELAY_REPOST_MODE
                if relay
                else service.DIRECT_DELIVERY_MODE
            ),
            "relay_account_id": 19 if relay else 0,
            "relay_account_username": "premiumrelay" if relay else "",
        }
    )
    return resolved


class DownloadFixture:
    def __init__(self, payloads):
        self.payloads = dict(payloads)
        self.calls = []

    def __call__(
        self,
        url,
        destination,
        allowed_media_hosts,
        *,
        max_bytes,
        timeout,
        http_client=None,
    ):
        self.calls.append(str(url))
        data = self.payloads[str(url)]
        destination = Path(destination)
        destination.write_bytes(data)
        return {
            "path": destination,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "media_kind": "video",
            "media_type": "video/mp4",
        }


class FakeRepairClient:
    def __init__(self, output_bytes, output_duration):
        self.output_bytes = output_bytes
        self.output_duration = float(output_duration)
        self.payloads = []

    def repair(self, payload):
        self.payloads.append(dict(payload))
        output_sha256 = hashlib.sha256(self.output_bytes).hexdigest()
        output_size = len(self.output_bytes)
        return {
            "job_key": payload["job_key"],
            "profile": payload["profile"],
            "duration_policy": payload["duration_policy"],
            "output_url": OUTPUT_URL,
            "output_sha256": output_sha256,
            "output_size": output_size,
            "probe": _probe(self.output_duration, output_size),
        }


class DramaDurationMediaTest(unittest.TestCase):
    def test_raw_duration_probe_has_no_standard_account_cap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = Path(temp_dir) / "source.mp4"
            media_path.write_bytes(b"source-video")
            completed = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "streams": [{"codec_type": "video"}],
                        "format": {"duration": "141.250001"},
                    }
                ),
                stderr="",
            )
            runner = mock.Mock(return_value=completed)

            duration = publish_media_repair._probe_raw_video_duration(
                media_path,
                max_bytes=MAX_BYTES,
                runner=runner,
            )

        self.assertEqual(duration, 141.250001)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.args[0][-1], str(media_path))
        self.assertIs(runner.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_boundaries_choose_policy_and_reuse_one_source_download(self):
        for raw_duration, expected_policy, relay in (
            (139.999, "standard", False),
            (140.0, "standard", False),
            (140.000001, "premium", True),
            (141.0, "premium", True),
        ):
            with self.subTest(raw_duration=raw_duration):
                queue = _queue()
                source_bytes = ("source-%s" % raw_duration).encode("ascii")
                downloader = DownloadFixture({SOURCE_URL: source_bytes})
                strict_probe_limits = []

                def strict_probe(path, **kwargs):
                    strict_probe_limits.append(kwargs["max_duration_seconds"])
                    return _probe(raw_duration, Path(path).stat().st_size)

                with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
                    service, "download_media", side_effect=downloader
                ), mock.patch.object(
                    publish_media_repair,
                    "_probe_raw_video_duration",
                    return_value=raw_duration,
                ), mock.patch.object(
                    service, "probe_media", side_effect=strict_probe
                ), mock.patch.object(
                    publish_media_repair,
                    "_repair_client_from_env",
                    side_effect=AssertionError("healthy media must not repair"),
                ):
                    with publish_media_repair.prepare_duration_pending_drama_media(
                        queue=queue,
                        public_root=Path(temp_dir) / "s2l",
                        allowed_media_hosts={"media.example.test"},
                        max_media_bytes=MAX_BYTES,
                    ) as prepared:
                        prepared_path = Path(prepared.media["path"])
                        self.assertTrue(prepared_path.is_file())
                        self.assertEqual(
                            prepared.repair_audit["duration_policy"],
                            expected_policy,
                        )
                        self.assertFalse(prepared.repair_audit["applied"])
                        self.assertEqual(prepared.final_duration, raw_duration)
                        self.assertEqual(prepared.final_width, 720)
                        self.assertEqual(prepared.final_height, 1280)
                        resolved = _resolved_queue(
                            queue,
                            prepared.evidence,
                            relay=relay,
                        )
                        bound = prepared.bind_resolved(resolved)
                        reused = bound.for_queue(resolved, MAX_BYTES)
                        self.assertEqual(Path(reused["path"]), prepared_path)
                        self.assertEqual(reused["sha256"], prepared.final_sha256)
                    self.assertFalse(prepared_path.exists())

                self.assertEqual(downloader.calls, [SOURCE_URL])
                self.assertEqual(
                    strict_probe_limits,
                    [
                        service.PREMIUM_MAX_DURATION_SECONDS
                        if expected_policy == "premium"
                        else service.STANDARD_MAX_DURATION_SECONDS
                    ],
                )

    def test_repair_policy_comes_from_raw_but_route_comes_from_final(self):
        for raw_duration, final_duration, expected_policy in (
            (139.999, 139.0, "standard"),
            (141.0, 139.5, "premium"),
        ):
            with self.subTest(raw_duration=raw_duration):
                queue = _queue()
                source_bytes = b"unsupported-source"
                output_bytes = b"strict-repaired-output"
                downloader = DownloadFixture(
                    {SOURCE_URL: source_bytes, OUTPUT_URL: output_bytes}
                )
                repair_client = FakeRepairClient(output_bytes, final_duration)
                probe_calls = []

                def strict_probe(path, **kwargs):
                    probe_calls.append(
                        (Path(path).name, kwargs["max_duration_seconds"])
                    )
                    if Path(path).name == "source.bin":
                        raise service.XPostError(
                            "invalid_media_codec",
                            "fixture requires repair",
                            422,
                        )
                    return _probe(final_duration, Path(path).stat().st_size)

                with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
                    service, "download_media", side_effect=downloader
                ), mock.patch.object(
                    publish_media_repair,
                    "_probe_raw_video_duration",
                    return_value=raw_duration,
                ), mock.patch.object(
                    service, "probe_media", side_effect=strict_probe
                ), mock.patch.object(
                    publish_media_repair,
                    "_repair_client_from_env",
                    return_value=(repair_client, "fixture-profile-v5"),
                ), mock.patch("builtins.print"):
                    with publish_media_repair.prepare_duration_pending_drama_media(
                        queue=queue,
                        public_root=Path(temp_dir) / "s2l",
                        allowed_media_hosts={"media.example.test"},
                        max_media_bytes=MAX_BYTES,
                    ) as prepared:
                        self.assertEqual(prepared.final_url, OUTPUT_URL)
                        self.assertEqual(prepared.final_duration, final_duration)
                        self.assertEqual(
                            prepared.final_sha256,
                            hashlib.sha256(output_bytes).hexdigest(),
                        )
                        self.assertEqual(prepared.final_size, len(output_bytes))
                        self.assertTrue(prepared.repair_audit["applied"])
                        self.assertEqual(
                            prepared.repair_audit["duration_policy"],
                            expected_policy,
                        )
                        self.assertEqual(
                            prepared.repair_audit["raw_duration"], raw_duration
                        )
                        self.assertEqual(
                            prepared.repair_audit["output_duration"],
                            final_duration,
                        )
                        self.assertEqual(
                            prepared.evidence["original_material_url"],
                            SOURCE_URL,
                        )
                        resolved = _resolved_queue(
                            queue,
                            prepared.evidence,
                            relay=False,
                        )
                        bound = prepared.bind_resolved(resolved)
                        reused = bound.for_queue(resolved, MAX_BYTES)
                        self.assertEqual(
                            Path(reused["path"]).read_bytes(), output_bytes
                        )

                self.assertEqual(downloader.calls, [SOURCE_URL, OUTPUT_URL])
                self.assertEqual(len(repair_client.payloads), 1)
                self.assertEqual(
                    repair_client.payloads[0]["duration_policy"],
                    expected_policy,
                )
                expected_limit = (
                    service.PREMIUM_MAX_DURATION_SECONDS
                    if expected_policy == "premium"
                    else service.STANDARD_MAX_DURATION_SECONDS
                )
                self.assertEqual(
                    probe_calls,
                    [("source.bin", expected_limit), ("repaired.bin", expected_limit)],
                )

    def test_unresolved_or_waiting_route_cannot_consume_prepared_file(self):
        queue = _queue()
        source_bytes = b"long-source"
        downloader = DownloadFixture({SOURCE_URL: source_bytes})
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            service, "download_media", side_effect=downloader
        ), mock.patch.object(
            publish_media_repair,
            "_probe_raw_video_duration",
            return_value=141.0,
        ), mock.patch.object(
            service,
            "probe_media",
            return_value=_probe(141.0, len(source_bytes)),
        ):
            with publish_media_repair.prepare_duration_pending_drama_media(
                queue=queue,
                public_root=Path(temp_dir) / "s2l",
                allowed_media_hosts={"media.example.test"},
                max_media_bytes=MAX_BYTES,
            ) as prepared:
                with self.assertRaises(service.XPostError) as unbound_error:
                    prepared.for_queue(queue, MAX_BYTES)
                self.assertEqual(
                    unbound_error.exception.code, "media_preflight_changed"
                )
                waiting = dict(queue)
                waiting.update(dict(prepared.evidence))
                waiting.update(
                    {
                        "status": "waiting_relay",
                        "route_state": "waiting_relay",
                        "delivery_mode": "duration_pending",
                    }
                )
                with self.assertRaises(service.XPostError) as waiting_error:
                    prepared.bind_resolved(waiting)
                self.assertEqual(
                    waiting_error.exception.code, "media_preflight_changed"
                )

    def test_prepare_fences_nonpending_or_prefrozen_queue_before_download(self):
        cases = (
            {"status": "waiting_relay", "route_state": "waiting_relay"},
            {"route_state": "resolved", "resolved_delivery_mode": "direct"},
            {"preflight_sha256": "0" * 64},
            {"preflight_duration": 120.0},
            {"media_repair_job_key": "already-frozen"},
        )
        with mock.patch.object(
            service,
            "download_media",
            side_effect=AssertionError("fenced queue must not download"),
        ) as downloader:
            for change in cases:
                with self.subTest(change=change):
                    with self.assertRaises(service.XPostError) as caught:
                        with publish_media_repair.prepare_duration_pending_drama_media(
                            queue={**_queue(), **change},
                            public_root=Path("unused") / "s2l",
                            allowed_media_hosts={"media.example.test"},
                            max_media_bytes=MAX_BYTES,
                        ):
                            self.fail("fenced queue must not prepare")
                    self.assertEqual(
                        caught.exception.code,
                        "x_post_drama_route_resolution_fenced",
                    )
        downloader.assert_not_called()

    def test_resolved_fingerprint_and_local_bytes_are_both_fenced(self):
        queue = _queue()
        source_bytes = b"short-source"
        downloader = DownloadFixture({SOURCE_URL: source_bytes})
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            service, "download_media", side_effect=downloader
        ), mock.patch.object(
            publish_media_repair,
            "_probe_raw_video_duration",
            return_value=120.0,
        ), mock.patch.object(
            service,
            "probe_media",
            return_value=_probe(120.0, len(source_bytes)),
        ):
            with publish_media_repair.prepare_duration_pending_drama_media(
                queue=queue,
                public_root=Path(temp_dir) / "s2l",
                allowed_media_hosts={"media.example.test"},
                max_media_bytes=MAX_BYTES,
            ) as prepared:
                resolved = _resolved_queue(queue, prepared.evidence)
                changed = dict(resolved)
                changed["preflight_duration"] = 121.0
                with self.assertRaises(service.XPostError) as frozen_error:
                    prepared.bind_resolved(changed)
                self.assertEqual(
                    frozen_error.exception.code, "media_preflight_changed"
                )

                changed_route = dict(resolved)
                changed_route["resolved_delivery_mode"] = (
                    service.PREMIUM_RELAY_REPOST_MODE
                )
                with self.assertRaises(service.XPostError) as route_error:
                    prepared.bind_resolved(changed_route)
                self.assertEqual(
                    route_error.exception.code, "media_preflight_changed"
                )

                bound = prepared.bind_resolved(resolved)
                media_path = Path(prepared.media["path"])
                media_path.write_bytes(b"tampered-bytes")
                with self.assertRaises(service.XPostError) as local_error:
                    bound.for_queue(resolved, MAX_BYTES)
                self.assertEqual(
                    local_error.exception.code, "media_preflight_changed"
                )


if __name__ == "__main__":
    unittest.main()
