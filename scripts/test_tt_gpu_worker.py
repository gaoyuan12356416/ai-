#!/usr/bin/env python3
"""Offline contract tests for the TikTok GPU preparation/publish sidecar."""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.tt_gpu import credentials  # noqa: E402
from features.tt_gpu.credentials import (  # noqa: E402
    CredentialEnvelopeError,
    open_access_token,
    seal_access_token,
)
from features.tt_gpu import worker  # noqa: E402


TOKEN = "tt-sensitive-token-" + "x" * 80
ACCOUNT_ID = "123"
JOB_ID = "ttjob_20260729_000001"
CONTENT_ID = "Ag0rfr5F0F"
SOURCE_BYTES = b"source-video"


def input_probe(duration, audio=True):
    streams = [
        {
            "avg_frame_rate": "30/1",
            "codec_name": "h264",
            "codec_type": "video",
            "height": 1920,
            "profile": "High",
            "r_frame_rate": "30/1",
            "width": 1080,
        }
    ]
    if audio:
        streams.append(
            {
                "channels": 2,
                "codec_name": "aac",
                "codec_type": "audio",
                "profile": "LC",
                "sample_rate": "48000",
            }
        )
    return {"format": {"duration": str(duration)}, "streams": streams}


def prepared_probe(duration, video_encoder="hevc_nvenc"):
    is_hevc = video_encoder == "hevc_nvenc"
    return {
        "format": {"duration": str(duration)},
        "streams": [
            {
                "avg_frame_rate": "30/1",
                "codec_name": "hevc" if is_hevc else "h264",
                "codec_tag_string": "hvc1" if is_hevc else "avc1",
                "codec_type": "video",
                "height": 1280,
                "pix_fmt": "yuv420p",
                "profile": "Main" if is_hevc else "High",
                "r_frame_rate": "30/1",
                "width": 720,
            },
            {
                "channel_layout": "stereo",
                "channels": 2,
                "codec_name": "aac",
                "codec_type": "audio",
                "profile": "LC",
                "sample_rate": "48000",
            },
        ],
    }


class FakeRunner:
    def __init__(self, probes=None):
        self.probes = list(
            probes
            or [
                input_probe(39.1),
                input_probe(11.933333),
                prepared_probe(45.8),
            ]
        )
        self.commands = []

    def __call__(self, command, **_kwargs):
        command = list(command)
        self.commands.append(command)
        executable = Path(command[0]).name.lower()
        if "ffprobe" in executable:
            if not self.probes:
                raise AssertionError("unexpected ffprobe")
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(self.probes.pop(0)),
                stderr="",
            )
        if "ffmpeg" in executable:
            Path(command[-1]).write_bytes(b"prepared-video")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError("unexpected command: %r" % command)


class LargeOutputFakeRunner(FakeRunner):
    def __call__(self, command, **_kwargs):
        command = list(command)
        self.commands.append(command)
        executable = Path(command[0]).name.lower()
        if "ffprobe" in executable:
            if not self.probes:
                raise AssertionError("unexpected ffprobe")
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(self.probes.pop(0)),
                stderr="",
            )
        if "ffmpeg" in executable:
            Path(command[-1]).write_bytes(b"x" * (2 * 1024 * 1024))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError("unexpected command: %r" % command)


class FakeObjectStore:
    def __init__(self):
        self.upload_calls = []

    def upload(
        self,
        key,
        path,
        sha256_value,
        size,
        deadline=None,
    ):
        self.upload_calls.append(
            {
                "deadline": deadline,
                "key": key,
                "path": str(path),
                "sha256": sha256_value,
                "size": size,
            }
        )
        return False

    @staticmethod
    def url(key):
        return "https://pull.example.com/" + key


class FakeTikTokAPI:
    def __init__(self):
        self.creator_calls = []
        self.init_calls = []
        self.status_calls = []
        self.init_error = None
        self.status = {
            "fail_reason": "",
            "log_id": "log-status-1",
            "post_ids": ["post-1"],
            "status": "PUBLISH_COMPLETE",
        }

    def creator_info(self, token):
        self.creator_calls.append(token)
        return {
            "comment_disabled": False,
            "creator_avatar_url": "https://avatar.example.com/a.jpg",
            "creator_nickname": "Dramawave",
            "creator_username": "dramawave",
            "duet_disabled": False,
            "log_id": "log-creator-1",
            "max_video_post_duration_sec": 600,
            "privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"],
            "stitch_disabled": False,
        }

    def initialize_video(self, token, post_info, video_url):
        self.init_calls.append((token, dict(post_info), video_url))
        if self.init_error:
            raise self.init_error
        return {"log_id": "log-init-1", "publish_id": "v_pub_url~v2.123"}

    def fetch_status(self, token, publish_id):
        self.status_calls.append((token, publish_id))
        return dict(self.status)


class FakeTikTokResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.payload


class FakeTikTokOpener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return FakeTikTokResponse(
            {
                "data": {"publish_id": "v_pub_url~v2.789"},
                "error": {"code": "ok", "log_id": "log-upstream-1"},
            }
        )


class HTTP500TikTokOpener:
    def open(self, request, timeout):
        body = json.dumps(
            {
                "error": {
                    "code": "internal_error",
                    "log_id": "log-http-500",
                }
            }
        ).encode("utf-8")
        raise urllib.error.HTTPError(
            request.full_url,
            500,
            "Internal Server Error",
            {},
            io.BytesIO(body),
        )


def make_config(root, gates=False):
    root = Path(root)
    outro = root / "fixed-outro.mp4"
    logo = root / "dramawave-logo.png"
    font = root / "font.ttf"
    for path, value in (
        (outro, b"outro"),
        (logo, b"logo"),
        (font, b"font"),
    ):
        if not path.exists():
            path.write_bytes(value)
    return worker.WorkerConfig(
        enabled=True,
        host="127.0.0.1",
        port=0,
        internal_token="i" * 40,
        credential_seal_key=b"k" * 32,
        credential_max_ttl_seconds=300,
        work_root=root / "work",
        fixed_outro_path=outro,
        logo_path=logo,
        font_file=font,
        allowed_source_hosts=("media.example.com",),
        ffmpeg_bin=str((root / "ffmpeg").resolve()),
        ffprobe_bin=str((root / "ffprobe").resolve()),
        video_encoder="hevc_nvenc",
        cos_secret_id="fixture-id",
        cos_secret_key="fixture-key",
        cos_bucket="fixture-bucket",
        cos_region="ap-fixture",
        cos_domain="https://pull.example.com",
        cos_prefix="tt-post-prepared",
        url_property_verified_origin=(
            "https://pull.example.com" if gates else ""
        ),
        live_enabled=bool(gates),
        direct_audit_approved=bool(gates),
        url_property_verified=bool(gates),
        max_source_bytes=1024 * 1024,
        max_output_bytes=1024 * 1024,
        max_duration_seconds=600,
        default_source_trim_tail_seconds=4.333333,
        download_timeout=30,
        probe_timeout=30,
        transcode_timeout=60,
    )


def make_local_config(root, gates=False, **overrides):
    config = replace(
        make_config(root, gates=gates),
        storage_backend="local",
        media_host="127.0.0.1",
        media_port=0,
        local_media_origin="https://tt-media.example.com",
        local_media_prefix="tt-post-media/v1",
        local_media_signing_key=b"s" * 32,
        url_property_verified_origin=(
            "https://tt-media.example.com" if gates else ""
        ),
        terminal_media_grace_seconds=0,
        local_min_free_bytes=0,
    )
    return replace(config, **overrides) if overrides else config


def make_prepare(**overrides):
    payload = {
        "content_id": CONTENT_ID,
        "expected_profile": worker.PROFILE,
        "job_id": JOB_ID,
        "source_url": "https://media.example.com/material.mp4",
    }
    payload.update(overrides)
    return payload


def make_downloader(calls):
    def download(
        url,
        destination,
        expected_sha,
        expected_size,
        _config,
        _deadline=None,
    ):
        calls.append(
            {
                "expected_sha": expected_sha,
                "expected_size": expected_size,
                "url": url,
            }
        )
        Path(destination).write_bytes(SOURCE_BYTES)
        return {
            "sha256": hashlib.sha256(SOURCE_BYTES).hexdigest(),
            "size": len(SOURCE_BYTES),
        }

    return download


def envelope(config, operation, token=TOKEN, job_id=JOB_ID):
    return seal_access_token(
        config.credential_seal_key,
        token,
        job_id=job_id,
        source_account_id=ACCOUNT_ID,
        operation=operation,
        ttl_seconds=120,
    )


def seed_prepared(
    processor,
    job_id=JOB_ID,
    *,
    direct_post_eligible=False,
):
    cos_key = "tt-post-prepared/aa/%s.mp4" % ("a" * 64)
    manifest = {
        "completed_at": "2026-07-29T00:00:00Z",
        "cos_key": cos_key,
        "request": {"content_id": CONTENT_ID},
        "result": {
            "brand_overlay_review_required": True,
            "content_id": CONTENT_ID,
            "direct_post_eligible": direct_post_eligible,
            "job_id": job_id,
            "output_sha256": "a" * 64,
            "output_size": 1234,
            "output_url": "https://pull.example.com/" + cos_key,
            "probe": {
                "audio_channels": 2,
                "audio_codec": "aac",
                "audio_profile": "lc",
                "audio_sample_rate": 48000,
                "duration": 45.8,
                "frame_rate": 30.0,
                "height": 1280,
                "pixel_format": "yuv420p",
                "profile": "main",
                "size": 1234,
                "video_codec": "hevc",
                "video_codec_tag": "hvc1",
                "width": 720,
            },
            "profile": worker.PROFILE,
        },
        "storage": {"backend": "cos", "key": cos_key},
        "status": "ready",
        "version": 2,
    }
    worker._atomic_write_json(processor._prepare_manifest_path(job_id), manifest)


def make_publish(config, title="Watch now", **overrides):
    payload = {
        "credential_envelope": envelope(config, "publish"),
        "disable_comment": False,
        "disable_duet": False,
        "disable_stitch": False,
        "is_aigc": True,
        "job_id": JOB_ID,
        "privacy_level": "SELF_ONLY",
        "source_account_id": ACCOUNT_ID,
        "title": title,
    }
    payload.update(overrides)
    return payload


class TTGPUWorkerTests(unittest.TestCase):
    def test_default_prepared_output_ceiling_matches_tiktok_four_gib(self):
        self.assertEqual(
            worker.DEFAULT_MAX_OUTPUT_BYTES,
            4 * 1024 * 1024 * 1024,
        )
        self.assertEqual(
            worker.DEFAULT_MAX_SOURCE_BYTES,
            2 * 1024 * 1024 * 1024,
        )

    def test_delivery_profile_is_720p_hevc_with_bounded_nvenc_vbr(self):
        config = make_config(self.root)
        arguments = worker._encoder_arguments(config)
        self.assertEqual(worker.PROFILE, "tt-post-hevc-720x1280-v2")
        self.assertIn("scale=w=720:h=1280", worker._base_video_filter())
        self.assertNotIn("1080", worker._base_video_filter())
        self.assertEqual(arguments[arguments.index("-c:v") + 1], "hevc_nvenc")
        self.assertEqual(arguments[arguments.index("-preset") + 1], "p6")
        self.assertEqual(arguments[arguments.index("-rc") + 1], "vbr")
        self.assertEqual(arguments[arguments.index("-b:v") + 1], "900k")
        self.assertEqual(arguments[arguments.index("-maxrate") + 1], "1350k")
        self.assertEqual(arguments[arguments.index("-bufsize") + 1], "1800k")
        self.assertEqual(
            arguments[arguments.index("-multipass") + 1],
            "fullres",
        )
        self.assertEqual(arguments[arguments.index("-tag:v") + 1], "hvc1")
        self.assertNotIn("-cq", arguments)
        self.assertNotIn("8M", arguments)

    def test_delivery_average_bitrate_cap_accepts_profiles_and_rejects_nine_mbps(
        self,
    ):
        self.assertEqual(
            worker.MAX_DELIVERY_AVERAGE_BITRATE_BPS,
            1_900_000,
        )
        duration = 10.0
        cases = (
            ("hevc_nvenc", 1_129_000),
            ("h264_nvenc", 1_656_000),
        )
        for encoder, bitrate in cases:
            with self.subTest(encoder=encoder):
                config = replace(
                    make_config(self.root),
                    video_encoder=encoder,
                    profile=(
                        worker.PROFILE
                        if encoder == "hevc_nvenc"
                        else worker.H264_FALLBACK_PROFILE
                    ),
                )
                path = self.root / ("%s-valid.mp4" % encoder)
                with path.open("wb") as handle:
                    handle.truncate(int(bitrate * duration / 8))
                result = worker.validate_prepared_output(
                    config,
                    prepared_probe(duration, encoder),
                    path,
                    20 * 1024 * 1024,
                    duration,
                )
                self.assertEqual(
                    result["video_codec_tag"],
                    "hvc1" if encoder == "hevc_nvenc" else "avc1",
                )

        config = make_config(self.root)
        oversized = self.root / "nine-mbps.mp4"
        with oversized.open("wb") as handle:
            handle.truncate(int(9_000_000 * duration / 8))
        with self.assertRaises(worker.TTGPUError) as caught:
            worker.validate_prepared_output(
                config,
                prepared_probe(duration),
                oversized,
                20 * 1024 * 1024,
                duration,
            )
        self.assertEqual(caught.exception.code, "prepared_media_invalid")
        cached_config = replace(
            config,
            max_output_bytes=20 * 1024 * 1024,
        )
        processor = self.processor(config=cached_config)
        seed_prepared(processor)
        manifest = worker._read_json(processor._prepare_manifest_path(JOB_ID))
        nine_mbps_size = int(9_000_000 * duration / 8)
        manifest["result"]["output_size"] = nine_mbps_size
        manifest["result"]["probe"]["duration"] = duration
        manifest["result"]["probe"]["size"] = nine_mbps_size
        with self.assertRaises(worker.TTGPUError) as cached:
            worker._prepare_response(
                manifest,
                True,
                cached_config,
                JOB_ID,
            )
        self.assertEqual(cached.exception.code, "prepared_media_invalid")

    def test_h264_nvenc_fallback_stays_gpu_accelerated_and_bounded(self):
        config = replace(
            make_config(self.root),
            video_encoder="h264_nvenc",
            profile=worker.H264_FALLBACK_PROFILE,
        )
        arguments = worker._encoder_arguments(config)
        self.assertEqual(arguments[arguments.index("-c:v") + 1], "h264_nvenc")
        self.assertEqual(arguments[arguments.index("-preset") + 1], "p6")
        self.assertEqual(arguments[arguments.index("-b:v") + 1], "1500k")
        self.assertEqual(arguments[arguments.index("-maxrate") + 1], "2200k")
        self.assertEqual(arguments[arguments.index("-bufsize") + 1], "3000k")
        self.assertEqual(arguments[arguments.index("-tag:v") + 1], "avc1")
        self.assertIn("-spatial-aq", arguments)
        self.assertIn("-temporal-aq", arguments)

    def test_libx264_software_fallback_is_h264_avc1_and_bounded(self):
        config = replace(
            make_config(self.root),
            video_encoder="libx264",
            profile=worker.H264_FALLBACK_PROFILE,
        )
        arguments = worker._encoder_arguments(config)
        contract = worker._delivery_video_contract(config)
        self.assertEqual(
            contract,
            {"codec": "h264", "codec_tag": "avc1", "profile": "high"},
        )
        self.assertEqual(arguments[arguments.index("-c:v") + 1], "libx264")
        self.assertEqual(arguments[arguments.index("-b:v") + 1], "1500k")
        self.assertEqual(arguments[arguments.index("-maxrate") + 1], "2200k")
        self.assertEqual(arguments[arguments.index("-bufsize") + 1], "3000k")
        self.assertEqual(arguments[arguments.index("-tag:v") + 1], "avc1")
        self.assertNotIn("-crf", arguments)

    def test_ready_manifest_is_revalidated_against_current_output_limit(self):
        broad = replace(
            make_config(self.root),
            max_output_bytes=4 * 1024 * 1024,
        )
        processor = self.processor(
            config=broad,
            runner=LargeOutputFakeRunner(),
        )
        created = processor.prepare(make_prepare())
        self.assertEqual(created["output_size"], 2 * 1024 * 1024)
        manifest_path = processor._prepare_manifest_path(JOB_ID)
        valid_manifest = worker._read_json(manifest_path)

        tightened = replace(broad, max_output_bytes=1024 * 1024)
        tightened_processor = self.processor(config=tightened)
        with self.assertRaises(worker.TTGPUError) as caught:
            tightened_processor.prepare(make_prepare())
        self.assertEqual(caught.exception.code, "prepared_media_invalid")

        mutations = {
            "content_id": lambda item: item["result"].__setitem__(
                "content_id",
                "DifferentContent",
            ),
            "job_id": lambda item: item["result"].__setitem__(
                "job_id",
                "ttpreview-different",
            ),
            "output_sha256": lambda item: item["result"].__setitem__(
                "output_sha256",
                "b" * 64,
            ),
            "output_url": lambda item: item["result"].__setitem__(
                "output_url",
                "https://pull.example.com/tt-post-prepared/evil.mp4",
            ),
            "probe": lambda item: item["result"]["probe"].__setitem__(
                "width",
                1080,
            ),
            "video_codec_tag": lambda item: item["result"]["probe"].__setitem__(
                "video_codec_tag",
                "avc1",
            ),
            "profile": lambda item: item["result"].__setitem__(
                "profile",
                "legacy-profile",
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(field=label):
                broken = json.loads(json.dumps(valid_manifest))
                mutate(broken)
                worker._atomic_write_json(manifest_path, broken)
                with self.assertRaises(worker.TTGPUError) as caught:
                    processor.prepare(make_prepare())
                self.assertEqual(
                    caught.exception.code,
                    "prepared_media_invalid",
                )

        broken = json.loads(json.dumps(valid_manifest))
        broken["result"]["direct_post_eligible"] = True
        broken["result"]["output_url"] = (
            "https://pull.example.com/tt-post-prepared/evil.mp4"
        )
        worker._atomic_write_json(manifest_path, broken)
        gated = replace(
            broad,
            live_enabled=True,
            direct_audit_approved=True,
            url_property_verified=True,
            url_property_verified_origin="https://pull.example.com",
        )
        api = FakeTikTokAPI()
        publish_processor = self.processor(config=gated, api=api)
        with self.assertRaises(worker.TTGPUError) as caught:
            publish_processor.publish(make_publish(gated))
        self.assertEqual(caught.exception.code, "prepared_media_invalid")
        self.assertEqual(api.init_calls, [])

    def test_creator_avatar_signed_query_is_not_returned(self):
        normalized = worker.normalize_creator_info(
            {
                "comment_disabled": False,
                "creator_avatar_url": (
                    "https://avatar.example.com/a.jpg"
                    "?refresh_token=not-an-account-token"
                ),
                "creator_nickname": "Dramawave",
                "creator_username": "dramawave",
                "duet_disabled": False,
                "max_video_post_duration_sec": 3600,
                "privacy_level_options": ["SELF_ONLY"],
                "stitch_disabled": False,
            }
        )
        self.assertEqual(normalized["creator_avatar_url"], "")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def processor(
        self,
        config=None,
        runner=None,
        downloader=None,
        api=None,
        monotonic_fn=None,
        object_store=None,
    ):
        config = config or make_config(self.root)
        return worker.TTPostGPUProcessor(
            config,
            runner=runner or FakeRunner(),
            downloader=downloader or make_downloader([]),
            object_store=object_store or FakeObjectStore(),
            tiktok_api=api or FakeTikTokAPI(),
            monotonic_fn=monotonic_fn,
        )

    def test_pycryptodome_fallback_uses_ciphertext_plus_tag_contract(self):
        key = b"q" * 32
        nonce = b"n" * 12
        aad = b"job-binding"
        plaintext = b"sensitive-value"
        fallback = credentials._aesgcm(key, force_fallback=True)
        primary = credentials._aesgcm(key)
        encrypted = fallback.encrypt(nonce, plaintext, aad)
        self.assertEqual(len(encrypted), len(plaintext) + 16)
        self.assertEqual(primary.decrypt(nonce, encrypted, aad), plaintext)
        encrypted_primary = primary.encrypt(nonce, plaintext, aad)
        self.assertEqual(
            fallback.decrypt(nonce, encrypted_primary, aad),
            plaintext,
        )

    def test_credential_envelope_is_bound_short_lived_and_not_plaintext(self):
        value = seal_access_token(
            b"k" * 32,
            TOKEN,
            job_id=JOB_ID,
            source_account_id=ACCOUNT_ID,
            operation="publish",
            ttl_seconds=60,
            now=1000,
        )
        self.assertNotIn(TOKEN, value)
        with open_access_token(
            value,
            b"k" * 32,
            job_id=JOB_ID,
            source_account_id=ACCOUNT_ID,
            operation="publish",
            now=1020,
        ) as token:
            self.assertEqual(token, TOKEN)
        with self.assertRaises(CredentialEnvelopeError):
            with open_access_token(
                value,
                b"k" * 32,
                job_id=JOB_ID,
                source_account_id=ACCOUNT_ID,
                operation="reconcile",
                now=1020,
            ):
                pass
        with self.assertRaises(CredentialEnvelopeError) as expired:
            with open_access_token(
                value,
                b"k" * 32,
                job_id=JOB_ID,
                source_account_id=ACCOUNT_ID,
                operation="publish",
                now=1061,
            ):
                pass
        self.assertEqual(expired.exception.code, "credential_envelope_expired")

    def test_configuration_defaults_to_exact_closed_gates_and_loopback(self):
        config = make_config(self.root)
        self.assertEqual(
            worker.DEFAULT_FONT_FILE,
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        )
        key = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
        env = {
            "TT_POST_GPU_ALLOWED_SOURCE_HOSTS": "media.example.com",
            "TT_POST_GPU_COS_BUCKET": "bucket",
            "TT_POST_GPU_COS_DOMAIN": "https://pull.example.com",
            "TT_POST_GPU_COS_PREFIX": "tt-post-prepared",
            "TT_POST_GPU_COS_REGION": "ap-test",
            "TT_POST_GPU_COS_SECRET_ID": "secret-id",
            "TT_POST_GPU_COS_SECRET_KEY": "secret-key",
            "TT_POST_GPU_CREDENTIAL_SEAL_KEY_B64": key,
            "TT_POST_GPU_ENABLED": "1",
            "TT_POST_GPU_FIXED_OUTRO_PATH": str(config.fixed_outro_path),
            "TT_POST_GPU_FFMPEG_BIN": str((self.root / "ffmpeg").resolve()),
            "TT_POST_GPU_FFPROBE_BIN": str((self.root / "ffprobe").resolve()),
            "TT_POST_GPU_FONT_FILE": str(config.font_file),
            "TT_POST_GPU_INTERNAL_TOKEN": "i" * 40,
            "TT_POST_GPU_LOGO_PATH": str(config.logo_path),
            "TT_POST_GPU_WORK_ROOT": str((self.root / "env-work").resolve()),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            loaded = worker.WorkerConfig.from_env()
        self.assertEqual(
            loaded.gate_state(),
            {
                "TT_POST_DIRECT_AUDIT_APPROVED": False,
                "TT_POST_LIVE_ENABLED": False,
                "TT_POST_URL_PROPERTY_VERIFIED": False,
                "ready": False,
            },
        )
        self.assertEqual(loaded.default_source_trim_tail_seconds, 4.333333)
        self.assertEqual(loaded.video_encoder, "hevc_nvenc")
        self.assertEqual(loaded.profile, worker.PROFILE)
        self.assertEqual(loaded.cos_timeout, 120)
        self.assertEqual(loaded.prepare_total_timeout, 8700)
        with mock.patch.dict(
            os.environ,
            dict(env, TT_POST_GPU_VIDEO_ENCODER="h264_nvenc"),
            clear=True,
        ):
            loaded_h264 = worker.WorkerConfig.from_env()
        self.assertEqual(loaded_h264.video_encoder, "h264_nvenc")
        self.assertEqual(loaded_h264.profile, worker.H264_FALLBACK_PROFILE)
        with mock.patch.dict(
            os.environ,
            dict(env, TT_POST_GPU_HOST="0.0.0.0"),
            clear=True,
        ):
            with self.assertRaises(worker.TTGPUError) as caught:
                worker.WorkerConfig.from_env()
        self.assertEqual(caught.exception.code, "invalid_configuration")

    def test_local_configuration_does_not_require_cos_credentials(self):
        config = make_config(self.root)
        signing_key = base64.urlsafe_b64encode(b"s" * 32).decode("ascii")
        seal_key = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
        env = {
            "TT_POST_GPU_ALLOWED_SOURCE_HOSTS": "media.example.com",
            "TT_POST_GPU_CREDENTIAL_SEAL_KEY_B64": seal_key,
            "TT_POST_GPU_ENABLED": "1",
            "TT_POST_GPU_FIXED_OUTRO_PATH": str(config.fixed_outro_path),
            "TT_POST_GPU_FFMPEG_BIN": str((self.root / "ffmpeg").resolve()),
            "TT_POST_GPU_FFPROBE_BIN": str((self.root / "ffprobe").resolve()),
            "TT_POST_GPU_FONT_FILE": str(config.font_file),
            "TT_POST_GPU_INTERNAL_TOKEN": "i" * 40,
            "TT_POST_GPU_LOCAL_MEDIA_ORIGIN": "https://tt-media.example.com",
            "TT_POST_GPU_LOCAL_URL_SIGNING_KEY_B64": signing_key,
            "TT_POST_GPU_LOGO_PATH": str(config.logo_path),
            "TT_POST_GPU_MEDIA_PORT": "8831",
            "TT_POST_GPU_STORAGE_BACKEND": "local",
            "TT_POST_GPU_WORK_ROOT": str((self.root / "local-work").resolve()),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            loaded = worker.WorkerConfig.from_env()
        self.assertEqual(loaded.storage_backend, "local")
        self.assertEqual(
            loaded.local_media_origin,
            "https://tt-media.example.com",
        )
        self.assertEqual(loaded.local_media_signing_key, b"s" * 32)
        self.assertEqual(loaded.cos_secret_id, "")
        self.assertFalse(loaded.gate_state()["ready"])
        rollback_env = dict(
            env,
            TT_POST_GPU_STORAGE_BACKEND="cos",
            TT_POST_GPU_COS_BUCKET="bucket",
            TT_POST_GPU_COS_DOMAIN="https://pull.example.com",
            TT_POST_GPU_COS_REGION="ap-test",
            TT_POST_GPU_COS_SECRET_ID="secret-id",
            TT_POST_GPU_COS_SECRET_KEY="secret-key",
        )
        with mock.patch.dict(os.environ, rollback_env, clear=True):
            rollback = worker.WorkerConfig.from_env()
        self.assertEqual(rollback.storage_backend, "cos")
        self.assertEqual(
            rollback.local_media_origin,
            "https://tt-media.example.com",
        )
        self.assertEqual(rollback.local_media_signing_key, b"s" * 32)
        with mock.patch.dict(
            os.environ,
            dict(env, TT_POST_GPU_TERMINAL_MEDIA_GRACE_SECONDS="0"),
            clear=True,
        ):
            with self.assertRaises(worker.TTGPUError) as unsafe_grace:
                worker.WorkerConfig.from_env()
        self.assertEqual(
            unsafe_grace.exception.code,
            "invalid_configuration",
        )
        for field, value in (
            ("TT_POST_GPU_LOCAL_MEDIA_ORIGIN", "https://example.com:bad"),
            ("TT_POST_GPU_LOCAL_MEDIA_PREFIX", "tt//media"),
            ("TT_POST_GPU_LOCAL_MEDIA_PREFIX", "tt/./media"),
        ):
            with self.subTest(field=field, value=value):
                with mock.patch.dict(
                    os.environ,
                    dict(env, **{field: value}),
                    clear=True,
                ):
                    with self.assertRaises(worker.TTGPUError) as invalid:
                        worker.WorkerConfig.from_env()
                self.assertEqual(
                    invalid.exception.code,
                    "invalid_configuration",
                )

    def test_cos_upload_request_timeout_disables_sdk_retries(self):
        captured = {}

        class FakeCosConfig:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        fake_client = object()
        client_args = {}

        def make_client(config, retry):
            client_args.update({"config": config, "retry": retry})
            return fake_client

        qcloud_cos = SimpleNamespace(
            CosConfig=FakeCosConfig,
            CosS3Client=make_client,
        )
        config = replace(
            make_config(self.root),
            transcode_timeout=60,
            cos_timeout=120,
        )
        with mock.patch.dict(sys.modules, {"qcloud_cos": qcloud_cos}):
            store = worker.CosObjectStore(config)
        self.assertIs(store.client, fake_client)
        self.assertEqual(captured["Timeout"], 120)
        self.assertFalse(captured["KeepAlive"])
        self.assertEqual(client_args["retry"], 0)

    def test_cos_upload_uses_bounded_manual_multipart_and_verifies_object(self):
        class NotFound(Exception):
            @staticmethod
            def get_status_code():
                return 404

        class FakeCosClient:
            def __init__(self):
                self.abort_calls = []
                self.completed = False
                self.complete_payload = None
                self.create_payload = None
                self.parts = []

            def head_object(self, **_kwargs):
                if not self.completed:
                    raise NotFound()
                return {
                    "Content-Length": len(payload),
                    "x-cos-meta-sha256": digest,
                }

            def create_multipart_upload(self, **kwargs):
                self.create_payload = kwargs
                return {"UploadId": "upload-1"}

            def upload_part(self, **kwargs):
                body = kwargs["Body"]
                self.parts.append(
                    (kwargs["PartNumber"], len(body), kwargs["EnableMD5"])
                )
                return {"ETag": '"part-%s"' % kwargs["PartNumber"]}

            def complete_multipart_upload(self, **kwargs):
                self.complete_payload = kwargs
                self.completed = True
                return {}

            def abort_multipart_upload(self, **kwargs):
                self.abort_calls.append(kwargs)

        payload = b"x" * (worker.COS_PART_SIZE_BYTES + 17)
        digest = hashlib.sha256(payload).hexdigest()
        source = self.root / "multipart.mp4"
        source.write_bytes(payload)
        client = FakeCosClient()
        store = worker.CosObjectStore(make_config(self.root), client=client)
        reused = store.upload(
            "tt-post-prepared/aa/test.mp4",
            source,
            digest,
            len(payload),
            deadline=worker.PrepareDeadline(10),
        )
        self.assertFalse(reused)
        self.assertEqual(
            client.parts,
            [
                (1, worker.COS_PART_SIZE_BYTES, True),
                (2, 17, True),
            ],
        )
        self.assertEqual(
            client.complete_payload["MultipartUpload"]["Part"],
            [
                {"ETag": '"part-1"', "PartNumber": 1},
                {"ETag": '"part-2"', "PartNumber": 2},
            ],
        )
        self.assertEqual(client.create_payload["ACL"], "public-read")
        self.assertEqual(
            client.create_payload["Metadata"]["x-cos-meta-sha256"],
            digest,
        )
        self.assertEqual(client.abort_calls, [])

    def test_cos_part_concurrency_is_bounded_across_store_instances(self):
        state_lock = threading.Lock()
        first_wave = threading.Event()
        release = threading.Event()
        state = {"active": 0, "maximum": 0}

        class BlockingCosClient:
            @staticmethod
            def upload_part(**kwargs):
                with state_lock:
                    state["active"] += 1
                    state["maximum"] = max(
                        state["maximum"],
                        state["active"],
                    )
                    if state["active"] == worker.COS_UPLOAD_THREADS:
                        first_wave.set()
                release.wait(timeout=2)
                with state_lock:
                    state["active"] -= 1
                return {"ETag": '"part-%s"' % kwargs["PartNumber"]}

        stores = [
            worker.CosObjectStore(
                make_config(self.root),
                client=BlockingCosClient(),
            )
            for _index in range(2)
        ]
        deadline = worker.PrepareDeadline(2)
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(
                    stores[part_number % 2]._upload_part,
                    "key",
                    "upload",
                    part_number,
                    b"x",
                    deadline,
                )
                for part_number in range(1, 9)
            ]
            self.assertTrue(first_wave.wait(timeout=1))
            time.sleep(0.05)
            self.assertEqual(state["maximum"], worker.COS_UPLOAD_THREADS)
            release.set()
            for future in futures:
                self.assertIn("ETag", future.result(timeout=1))
        self.assertEqual(state["active"], 0)
        self.assertEqual(state["maximum"], worker.COS_UPLOAD_THREADS)

    def test_cos_part_deadline_does_not_wait_for_executor_shutdown(self):
        release = threading.Event()
        aborted = threading.Event()

        class NotFound(Exception):
            @staticmethod
            def get_status_code():
                return 404

        class SlowCosClient:
            @staticmethod
            def head_object(**_kwargs):
                raise NotFound()

            @staticmethod
            def create_multipart_upload(**_kwargs):
                return {"UploadId": "slow-upload"}

            @staticmethod
            def upload_part(**_kwargs):
                release.wait(timeout=2)
                return {"ETag": '"slow-part"'}

            @staticmethod
            def abort_multipart_upload(**_kwargs):
                aborted.set()
                release.set()

        payload = b"slow-part-body"
        digest = hashlib.sha256(payload).hexdigest()
        source = self.root / "slow-multipart.mp4"
        source.write_bytes(payload)
        store = worker.CosObjectStore(
            make_config(self.root),
            client=SlowCosClient(),
        )
        started = time.monotonic()
        with self.assertRaises(worker.TTGPUError) as caught:
            store.upload(
                "tt-post-prepared/aa/slow.mp4",
                source,
                digest,
                len(payload),
                deadline=worker.PrepareDeadline(0.05),
            )
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "prepare_timeout")
        self.assertLess(elapsed, 0.5)
        self.assertTrue(aborted.wait(timeout=1))

    def test_cos_complete_timeout_is_not_aborted_and_retry_recovers_head(self):
        completed = threading.Event()

        class NotFound(Exception):
            @staticmethod
            def get_status_code():
                return 404

        class SlowCompleteClient:
            def __init__(self):
                self.abort_calls = []
                self.create_calls = 0

            def head_object(self, **_kwargs):
                if not completed.is_set():
                    raise NotFound()
                return {
                    "Content-Length": len(payload),
                    "x-cos-meta-sha256": digest,
                }

            def create_multipart_upload(self, **_kwargs):
                self.create_calls += 1
                return {"UploadId": "complete-unknown"}

            @staticmethod
            def upload_part(**_kwargs):
                return {"ETag": '"part-1"'}

            @staticmethod
            def complete_multipart_upload(**_kwargs):
                time.sleep(0.15)
                completed.set()
                return {}

            def abort_multipart_upload(self, **kwargs):
                self.abort_calls.append(kwargs)

        payload = b"complete-outcome-unknown"
        digest = hashlib.sha256(payload).hexdigest()
        source = self.root / "complete-unknown.mp4"
        source.write_bytes(payload)
        client = SlowCompleteClient()
        store = worker.CosObjectStore(
            make_config(self.root),
            client=client,
        )
        with self.assertRaises(worker.TTGPUError) as caught:
            store.upload(
                "tt-post-prepared/aa/complete.mp4",
                source,
                digest,
                len(payload),
                deadline=worker.PrepareDeadline(0.05),
            )
        self.assertEqual(caught.exception.code, "prepare_timeout")
        self.assertEqual(client.abort_calls, [])
        self.assertTrue(completed.wait(timeout=1))
        self.assertTrue(
            store.upload(
                "tt-post-prepared/aa/complete.mp4",
                source,
                digest,
                len(payload),
                deadline=worker.PrepareDeadline(1),
            )
        )
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(client.abort_calls, [])

    def test_prepare_total_deadline_is_shared_across_pipeline_stages(self):
        class FakeClock:
            def __init__(self):
                self.value = 100.0

            def __call__(self):
                return self.value

        clock = FakeClock()
        calls = []
        store = FakeObjectStore()

        def slow_download(
            url,
            destination,
            expected_sha,
            expected_size,
            _config,
            deadline,
        ):
            calls.append((url, expected_sha, expected_size, deadline))
            Path(destination).write_bytes(SOURCE_BYTES)
            clock.value += 11.0
            return {
                "sha256": hashlib.sha256(SOURCE_BYTES).hexdigest(),
                "size": len(SOURCE_BYTES),
            }

        config = replace(
            make_config(self.root),
            prepare_total_timeout=10,
        )
        processor = self.processor(
            config,
            downloader=slow_download,
            monotonic_fn=clock,
            object_store=store,
        )
        with self.assertRaises(worker.TTGPUError) as caught:
            processor.prepare(make_prepare())
        self.assertEqual(caught.exception.code, "prepare_timeout")
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(calls[0][3], worker.PrepareDeadline)
        self.assertEqual(store.upload_calls, [])
        self.assertIsNone(
            worker._read_json(processor._prepare_manifest_path(JOB_ID))
        )
        self.assertEqual(list(processor.jobs_root.iterdir()), [])

    def test_prepare_downloads_only_on_gpu_freezes_actual_content_and_reuses_job(self):
        config = make_config(self.root)
        calls = []
        runner = FakeRunner()
        processor = self.processor(
            config,
            runner=runner,
            downloader=make_downloader(calls),
        )
        result = processor.prepare(make_prepare())
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["brand_overlay_review_required"])
        self.assertFalse(result["direct_post_eligible"])
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["expected_sha"])
        self.assertIsNone(calls[0]["expected_size"])
        manifest = worker._read_json(processor._prepare_manifest_path(JOB_ID))
        self.assertIs(manifest["result"]["direct_post_eligible"], False)
        self.assertEqual(
            manifest["request"]["source_sha256"],
            hashlib.sha256(SOURCE_BYTES).hexdigest(),
        )
        self.assertEqual(manifest["request"]["source_size"], len(SOURCE_BYTES))
        self.assertEqual(
            manifest["request"]["source_trim_tail_seconds"],
            4.333333,
        )
        self.assertEqual(
            manifest["request"]["logo_sha256"],
            hashlib.sha256(b"logo").hexdigest(),
        )
        reused = processor.prepare(make_prepare())
        self.assertTrue(reused["reused"])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("source_url", manifest["request"])
        self.assertNotIn(
            make_prepare()["source_url"],
            json.dumps(manifest),
        )
        self.assertEqual(
            manifest["request"]["source_url_sha256"],
            hashlib.sha256(
                make_prepare()["source_url"].encode("utf-8")
            ).hexdigest(),
        )

    def test_prepare_reuse_rejects_changed_url_hash_or_trim_contract(self):
        processor = self.processor()
        processor.prepare(make_prepare())
        for changed in (
            {"source_url": "https://media.example.com/rotated.mp4"},
            {"source_trim_tail_seconds": 0},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(worker.TTGPUError) as caught:
                    processor.prepare(make_prepare(**changed))
                self.assertEqual(
                    caught.exception.code,
                    "prepare_idempotency_conflict",
                )

    def test_prepare_rejects_profile_mismatch_before_download(self):
        calls = []
        processor = self.processor(
            downloader=make_downloader(calls),
        )
        with self.assertRaises(worker.TTGPUError) as caught:
            processor.prepare(
                make_prepare(
                    expected_profile=worker.H264_FALLBACK_PROFILE,
                )
            )
        self.assertEqual(caught.exception.code, "prepare_profile_mismatch")
        self.assertEqual(calls, [])
        self.assertIsNone(
            worker._read_json(processor._prepare_manifest_path(JOB_ID))
        )

    def test_prepare_reuse_rejects_changed_logo_or_outro_assets(self):
        config = make_config(self.root)
        calls = []
        processor = self.processor(
            config,
            downloader=make_downloader(calls),
        )
        processor.prepare(make_prepare())
        original_logo = config.logo_path.read_bytes()
        original_outro = config.fixed_outro_path.read_bytes()

        config.logo_path.write_bytes(original_logo + b"-changed")
        with self.assertRaises(worker.TTGPUError) as changed_logo:
            processor.prepare(make_prepare())
        self.assertEqual(
            changed_logo.exception.code,
            "prepare_idempotency_conflict",
        )

        config.logo_path.write_bytes(original_logo)
        config.fixed_outro_path.write_bytes(original_outro + b"-changed")
        with self.assertRaises(worker.TTGPUError) as changed_outro:
            processor.prepare(make_prepare())
        self.assertEqual(
            changed_outro.exception.code,
            "prepare_idempotency_conflict",
        )
        self.assertEqual(len(calls), 1)

    def test_prepare_conflicts_when_same_job_changes_content_id(self):
        processor = self.processor()
        processor.prepare(make_prepare())
        with self.assertRaises(worker.TTGPUError) as caught:
            processor.prepare(make_prepare(content_id="Another123"))
        self.assertEqual(caught.exception.code, "prepare_idempotency_conflict")

    def test_prepare_optional_hints_are_verified_but_not_required(self):
        calls = []
        processor = self.processor(
            runner=FakeRunner(
                [
                    input_probe(39.1),
                    input_probe(11.933333),
                    prepared_probe(50.133333),
                ]
            ),
            downloader=make_downloader(calls),
        )
        result = processor.prepare(
            make_prepare(
                source_sha256=hashlib.sha256(SOURCE_BYTES).hexdigest(),
                source_size=len(SOURCE_BYTES),
                source_trim_tail_seconds=0,
            )
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(
            calls[0]["expected_sha"],
            hashlib.sha256(SOURCE_BYTES).hexdigest(),
        )
        self.assertEqual(calls[0]["expected_size"], len(SOURCE_BYTES))

    def test_local_prepare_persists_blob_and_reuses_without_cos(self):
        config = make_local_config(self.root)
        processor = worker.TTPostGPUProcessor(
            config,
            runner=FakeRunner(),
            downloader=make_downloader([]),
            tiktok_api=FakeTikTokAPI(),
        )
        created = processor.prepare(make_prepare())
        manifest = worker._read_json(
            processor._prepare_manifest_path(JOB_ID)
        )
        self.assertEqual(created["storage_backend"], "local")
        self.assertTrue(
            created["output_url"].startswith(
                "https://tt-media.example.com/tt-post-media/v1/"
            )
        )
        self.assertEqual(manifest["storage"]["backend"], "local")
        key = manifest["storage"]["key"]
        blob = processor._object_store()._path(key)
        self.assertTrue(blob.is_file())
        self.assertEqual(blob.read_bytes(), b"prepared-video")
        self.assertEqual(list(processor.jobs_root.iterdir()), [])
        with mock.patch.object(
            worker.shutil,
            "disk_usage",
            return_value=SimpleNamespace(free=0),
        ):
            reused = processor.prepare(make_prepare())
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["output_url"], created["output_url"])

    def test_local_prepare_rejects_tampered_persisted_blob(self):
        config = make_local_config(self.root)
        processor = worker.TTPostGPUProcessor(
            config,
            runner=FakeRunner(),
            downloader=make_downloader([]),
            tiktok_api=FakeTikTokAPI(),
        )
        processor.prepare(make_prepare())
        manifest = worker._read_json(
            processor._prepare_manifest_path(JOB_ID)
        )
        blob = processor._object_store()._path(manifest["storage"]["key"])
        blob.write_bytes(b"x" * blob.stat().st_size)
        with self.assertRaises(worker.TTGPUError) as caught:
            processor.prepare(make_prepare())
        self.assertEqual(
            caught.exception.code,
            "local_media_verification_failed",
        )

    def test_local_prepare_fails_closed_below_disk_reserve(self):
        config = make_local_config(
            self.root,
            local_min_free_bytes=1024,
        )
        downloads = []
        processor = worker.TTPostGPUProcessor(
            config,
            runner=FakeRunner(),
            downloader=make_downloader(downloads),
            tiktok_api=FakeTikTokAPI(),
        )
        with mock.patch.object(
            worker.shutil,
            "disk_usage",
            return_value=SimpleNamespace(free=1023),
        ):
            with self.assertRaises(worker.TTGPUError) as caught:
                processor.prepare(make_prepare())
        self.assertEqual(caught.exception.code, "local_media_storage_full")
        self.assertEqual(downloads, [])

    def test_local_storage_health_exposes_capacity_without_secrets(self):
        config = make_local_config(
            self.root,
            local_min_free_bytes=1024,
            max_source_bytes=2048,
            max_output_bytes=4096,
        )
        processor = worker.TTPostGPUProcessor(
            config,
            runner=FakeRunner(),
            downloader=make_downloader([]),
            tiktok_api=FakeTikTokAPI(),
        )
        required = (
            1024
            + 2048
            + 4096
            + worker.LOCAL_PREPARE_OVERHEAD_BYTES
        )
        with mock.patch.object(
            worker.shutil,
            "disk_usage",
            return_value=SimpleNamespace(
                free=required,
                total=required * 2,
            ),
        ):
            state = processor.storage_health()
        self.assertTrue(state["local_prepare_admission_ready"])
        self.assertEqual(
            state["next_prepare_required_free_bytes"],
            required,
        )
        serialized = json.dumps(state)
        self.assertNotIn(
            base64.urlsafe_b64encode(
                config.local_media_signing_key
            ).decode("ascii"),
            serialized,
        )
        processor.record_cleanup_state(
            {"failed": 0, "released": 1, "scanned": 2}
        )
        self.assertEqual(
            processor.storage_health()["cleanup"]["status"],
            "ok",
        )

    def test_prepare_admission_is_serialized_across_different_jobs(self):
        processor = self.processor()
        first_started = threading.Event()
        release_first = threading.Event()
        guard = threading.Lock()
        calls = []
        active = 0
        maximum_active = 0

        def fake_prepare_new_locked(
            request,
            _deadline,
            _reuse_contract,
            _manifest_path,
        ):
            nonlocal active, maximum_active
            with guard:
                calls.append(request["job_id"])
                active += 1
                maximum_active = max(maximum_active, active)
                call_number = len(calls)
            if call_number == 1:
                first_started.set()
                self.assertTrue(release_first.wait(timeout=5))
            with guard:
                active -= 1
            return {"job_id": request["job_id"]}

        processor._prepare_new_locked = fake_prepare_new_locked
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                processor.prepare,
                make_prepare(job_id="ttjob_20260729_000001"),
            )
            self.assertTrue(first_started.wait(timeout=5))
            second = executor.submit(
                processor.prepare,
                make_prepare(job_id="ttjob_20260729_000002"),
            )
            time.sleep(0.1)
            with guard:
                self.assertEqual(len(calls), 1)
                self.assertEqual(maximum_active, 1)
            release_first.set()
            self.assertEqual(
                first.result(timeout=5)["job_id"],
                "ttjob_20260729_000001",
            )
            self.assertEqual(
                second.result(timeout=5)["job_id"],
                "ttjob_20260729_000002",
            )
        self.assertEqual(maximum_active, 1)

    def test_ready_reuse_bypasses_another_jobs_long_prepare_slot(self):
        processor = self.processor()
        processor.prepare(make_prepare())
        long_started = threading.Event()
        release_long = threading.Event()

        def blocking_prepare(
            request,
            _deadline,
            _reuse_contract,
            _manifest_path,
        ):
            long_started.set()
            self.assertTrue(release_long.wait(timeout=5))
            return {"job_id": request["job_id"], "status": "ready"}

        processor._prepare_new_locked = blocking_prepare
        with ThreadPoolExecutor(max_workers=2) as executor:
            long_job = executor.submit(
                processor.prepare,
                make_prepare(job_id="ttjob_20260729_000002"),
            )
            self.assertTrue(long_started.wait(timeout=5))
            cached = executor.submit(
                processor.prepare,
                make_prepare(),
            )
            cached_result = cached.result(timeout=1)
            self.assertTrue(cached_result["reused"])
            self.assertEqual(cached_result["job_id"], JOB_ID)
            release_long.set()
            self.assertEqual(
                long_job.result(timeout=5)["job_id"],
                "ttjob_20260729_000002",
            )

    def test_local_media_origin_supports_head_get_and_single_ranges(self):
        config = make_local_config(self.root)
        processor = worker.TTPostGPUProcessor(
            config,
            runner=FakeRunner(),
            downloader=make_downloader([]),
            tiktok_api=FakeTikTokAPI(),
        )
        prepared = processor.prepare(make_prepare())
        request_path = urllib.parse.urlsplit(prepared["output_url"]).path
        manifest = worker._read_json(
            processor._prepare_manifest_path(JOB_ID)
        )
        blob = processor._object_store()._path(manifest["storage"]["key"])
        rollback_processor = worker.TTPostGPUProcessor(
            replace(config, storage_backend="cos"),
            runner=FakeRunner(),
            downloader=make_downloader([]),
            tiktok_api=FakeTikTokAPI(),
        )
        server = worker.TTPostGPUMediaHTTPServer(
            ("127.0.0.1", 0),
            rollback_processor,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address

        def request(method, path, headers=None):
            connection = http.client.HTTPConnection(host, port, timeout=5)
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            body = response.read()
            result = (
                response.status,
                dict(response.getheaders()),
                body,
            )
            connection.close()
            return result

        try:
            status, headers, body = request("HEAD", request_path)
            self.assertEqual(status, 200)
            self.assertEqual(body, b"")
            self.assertEqual(headers["Content-Type"], "video/mp4")
            self.assertEqual(headers["Accept-Ranges"], "bytes")
            self.assertEqual(int(headers["Content-Length"]), 14)
            etag = headers["ETag"]

            status, headers, body = request("GET", request_path)
            self.assertEqual(status, 200)
            self.assertEqual(body, b"prepared-video")
            self.assertNotIn("Location", headers)

            status, headers, body = request(
                "GET",
                request_path,
                {"Range": "bytes=2-5"},
            )
            self.assertEqual(status, 206)
            self.assertEqual(body, b"epar")
            self.assertEqual(headers["Content-Range"], "bytes 2-5/14")

            status, headers, body = request(
                "HEAD",
                request_path,
                {"Range": "bytes=2-5"},
            )
            self.assertEqual(status, 206)
            self.assertEqual(body, b"")
            self.assertEqual(headers["Content-Range"], "bytes 2-5/14")
            self.assertEqual(headers["Content-Length"], "4")

            status, headers, body = request(
                "GET",
                request_path,
                {"Range": "bytes=4-"},
            )
            self.assertEqual(status, 206)
            self.assertEqual(body, b"ared-video")
            self.assertEqual(headers["Content-Range"], "bytes 4-13/14")

            status, headers, body = request(
                "GET",
                request_path,
                {"Range": "bytes=-5"},
            )
            self.assertEqual(status, 206)
            self.assertEqual(body, b"video")

            status, _headers, body = request(
                "GET",
                request_path,
                {
                    "If-Range": etag,
                    "Range": "bytes=0-3",
                },
            )
            self.assertEqual(status, 206)
            self.assertEqual(body, b"prep")

            status, _headers, body = request(
                "GET",
                request_path,
                {
                    "If-Range": '"different-etag"',
                    "Range": "bytes=0-3",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(body, b"prepared-video")

            status, headers, body = request(
                "GET",
                request_path,
                {"Range": "bytes=999-"},
            )
            self.assertEqual(status, 416)
            self.assertEqual(headers["Content-Range"], "bytes */14")
            self.assertEqual(body, b"")

            for invalid_range in ("bytes=5-2", "bytes=0-1,3-4"):
                status, headers, body = request(
                    "GET",
                    request_path,
                    {"Range": invalid_range},
                )
                self.assertEqual(status, 416)
                self.assertEqual(headers["Content-Range"], "bytes */14")
                self.assertEqual(body, b"")

            status, _headers, _body = request(
                "GET",
                request_path + "?probe=1",
            )
            self.assertEqual(status, 404)
            bad_signature = request_path[:-5] + "0.mp4"
            status, _headers, _body = request("GET", bad_signature)
            self.assertEqual(status, 404)

            with blob.open("ab") as handle:
                handle.write(b"-tampered")
            status, _headers, _body = request("GET", request_path)
            self.assertEqual(status, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_terminal_cleanup_releases_local_media_but_processing_does_not(self):
        config = make_local_config(self.root)
        processor = worker.TTPostGPUProcessor(
            config,
            runner=FakeRunner(),
            downloader=make_downloader([]),
            tiktok_api=FakeTikTokAPI(),
        )
        processor.prepare(make_prepare())
        manifest = worker._read_json(
            processor._prepare_manifest_path(JOB_ID)
        )
        blob = processor._object_store()._path(manifest["storage"]["key"])
        ledger_path = processor._publish_ledger_path(JOB_ID)
        worker._atomic_write_json(
            ledger_path,
            {
                "job_id": JOB_ID,
                "state": "processing",
                "version": 1,
            },
        )
        processing = processor.cleanup_due_media()
        self.assertEqual(processing["released"], 0)
        self.assertTrue(blob.exists())
        worker._atomic_write_json(
            ledger_path,
            {
                "job_id": JOB_ID,
                "state": "init_outcome_unknown",
                "version": 1,
            },
        )
        unknown = processor.cleanup_due_media()
        self.assertEqual(unknown["released"], 0)
        self.assertTrue(blob.exists())
        tampered_manifest = worker._read_json(
            processor._prepare_manifest_path(JOB_ID)
        )
        tampered_manifest["result"]["job_id"] = "ttjob_20260729_999999"
        worker._atomic_write_json(
            processor._prepare_manifest_path(JOB_ID),
            tampered_manifest,
        )
        worker._atomic_write_json(
            ledger_path,
            {
                "job_id": "ttjob_20260729_999999",
                "state": "published",
                "version": 1,
            },
        )
        wrong_ledger = processor.cleanup_due_media()
        self.assertEqual(wrong_ledger["failed"], 1)
        self.assertTrue(blob.exists())
        worker._atomic_write_json(
            ledger_path,
            {
                "job_id": JOB_ID,
                "state": "published",
                "version": 1,
            },
        )
        blocked = processor.cleanup_due_media()
        self.assertEqual(blocked["failed"], 1)
        self.assertTrue(blob.exists())
        tampered_manifest["result"]["job_id"] = JOB_ID
        worker._atomic_write_json(
            processor._prepare_manifest_path(JOB_ID),
            tampered_manifest,
        )
        terminal = processor.cleanup_due_media()
        self.assertEqual(terminal["released"], 1)
        self.assertFalse(blob.exists())
        ledger = worker._read_json(ledger_path)
        self.assertEqual(ledger["media_release"]["state"], "released")
        repeated = processor.cleanup_due_media()
        self.assertEqual(repeated["released"], 0)

    def test_phone_match_filter_overlaps_point_nine_and_shrinks_to_phone_size(self):
        config = make_config(self.root)
        command = worker.build_phone_match_command(
            config,
            self.root / "source.mp4",
            self.root / "outro.mp4",
            self.root / "out.mp4",
            34.766667,
            11.933333,
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("scale=w=720:h=1280", graph)
        self.assertIn("720-214*", graph)
        self.assertIn("1280-378*", graph)
        self.assertIn("scale=132:132", graph)
        self.assertIn("overlay=48:72", graph)
        self.assertIn("overlay=x=(W-w)/2:y=(H-h)/2", graph)
        self.assertIn("d=0.900000", graph)
        self.assertIn("afade=t=out", graph)
        self.assertIn("adelay=33867|33867", graph)
        self.assertEqual(command[command.index("-b:a") + 1], "128k")
        self.assertNotIn("1080", graph)
        self.assertNotIn("-f concat", " ".join(command))

    def test_phone_match_synthesizes_audio_when_source_is_silent(self):
        config = make_config(self.root)
        command = worker.build_phone_match_command(
            config,
            self.root / "source.mp4",
            self.root / "outro.mp4",
            self.root / "out.mp4",
            34.766667,
            11.933333,
            source_has_audio=False,
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn(
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            graph,
        )
        self.assertNotIn("[0:a]aresample", graph)

    def test_outro_filter_reads_dynamic_drama_id_and_marks_tutorial(self):
        config = make_config(self.root)
        drama = self.root / "drama.txt"
        tutorial = self.root / "tutorial.txt"
        drama.write_text("DRAMA ID: " + CONTENT_ID, encoding="utf-8")
        tutorial.write_text("TUTORIAL EXAMPLE - use ID above", encoding="utf-8")
        graph = worker.build_outro_filter(config, drama, tutorial)
        self.assertIn("textfile=", graph)
        self.assertIn("drawbox", graph)
        self.assertEqual(drama.read_text(encoding="utf-8"), "DRAMA ID: " + CONTENT_ID)
        self.assertIn("TUTORIAL EXAMPLE", tutorial.read_text(encoding="utf-8"))

    def test_end_to_end_duration_is_trimmed_source_plus_outro_minus_overlap(self):
        runner = FakeRunner()
        processor = self.processor(runner=runner)
        result = processor.prepare(make_prepare())
        self.assertEqual(result["probe"]["duration"], 45.8)
        ffmpeg_commands = [
            command
            for command in runner.commands
            if "ffmpeg" in Path(command[0]).name.lower()
        ]
        self.assertEqual(len(ffmpeg_commands), 2)
        source_commands = [
            command
            for command in ffmpeg_commands
            if any(Path(value).name == "source.mp4" for value in command)
        ]
        self.assertEqual(len(source_commands), 1)
        all_arguments = " ".join(
            value for command in ffmpeg_commands for value in command
        )
        self.assertNotIn("source-normalized.mp4", all_arguments)
        self.assertNotIn("8M", all_arguments)
        self.assertNotIn("-cq", all_arguments)
        for command in ffmpeg_commands:
            self.assertEqual(command[command.index("-c:v") + 1], "hevc_nvenc")
            self.assertEqual(command[command.index("-b:v") + 1], "900k")
            self.assertEqual(command[command.index("-maxrate") + 1], "1350k")
            self.assertEqual(command[command.index("-bufsize") + 1], "1800k")
            self.assertEqual(command[command.index("-tag:v") + 1], "hvc1")
            self.assertEqual(command[command.index("-b:a") + 1], "128k")
        final_graph = ffmpeg_commands[-1][
            ffmpeg_commands[-1].index("-filter_complex") + 1
        ]
        self.assertIn("scale=w=720:h=1280", final_graph)
        self.assertIn("overlay=48:72", final_graph)
        self.assertIn("trim=start=33.866667:end=34.766667", final_graph)
        self.assertIn("concat=n=3:v=1:a=0", final_graph)

    def test_creator_info_is_allowed_with_closed_gates_and_token_is_not_persisted(self):
        config = make_config(self.root, gates=False)
        api = FakeTikTokAPI()
        processor = self.processor(config=config, api=api)
        result = processor.creator_info(
            {
                "credential_envelope": envelope(config, "creator_info"),
                "job_id": JOB_ID,
                "source_account_id": ACCOUNT_ID,
            }
        )
        self.assertEqual(result["creator_info"]["creator_username"], "dramawave")
        self.assertEqual(result["creator_info"]["log_id"], "log-creator-1")
        self.assertEqual(api.creator_calls, [TOKEN])
        all_bytes = b"".join(
            path.read_bytes()
            for path in config.work_root.rglob("*")
            if path.is_file()
        )
        self.assertNotIn(TOKEN.encode("utf-8"), all_bytes)

    def test_closed_gates_block_publish_without_opening_api(self):
        config = make_config(self.root, gates=False)
        api = FakeTikTokAPI()
        processor = self.processor(config=config, api=api)
        seed_prepared(processor)
        with self.assertRaises(worker.TTGPUError) as caught:
            processor.publish(make_publish(config))
        self.assertEqual(caught.exception.code, "tt_publish_compliance_gate_closed")
        self.assertEqual(api.init_calls, [])
        self.assertFalse(processor._publish_ledger_path(JOB_ID).exists())

    def test_verified_property_is_bound_to_selected_storage_origin(self):
        config = replace(
            make_config(self.root, gates=True),
            url_property_verified_origin="https://different.example.com",
        )
        api = FakeTikTokAPI()
        processor = self.processor(config=config, api=api)
        seed_prepared(processor)
        self.assertFalse(config.gate_state()["ready"])
        with self.assertRaises(worker.TTGPUError) as caught:
            processor.publish(make_publish(config))
        self.assertEqual(
            caught.exception.code,
            "tt_publish_compliance_gate_closed",
        )
        self.assertEqual(api.init_calls, [])

    def test_publish_binds_property_to_frozen_manifest_origin_after_backend_switch(
        self,
    ):
        local_config = make_local_config(self.root)
        local_processor = worker.TTPostGPUProcessor(
            local_config,
            runner=FakeRunner(),
            downloader=make_downloader([]),
            tiktok_api=FakeTikTokAPI(),
        )
        local_processor.prepare(make_prepare())
        local_manifest_path = local_processor._prepare_manifest_path(JOB_ID)
        local_manifest = worker._read_json(local_manifest_path)
        local_manifest["result"]["direct_post_eligible"] = True
        worker._atomic_write_json(local_manifest_path, local_manifest)
        switched_to_cos = replace(
            local_config,
            storage_backend="cos",
            live_enabled=True,
            direct_audit_approved=True,
            url_property_verified=True,
            url_property_verified_origin="https://pull.example.com",
        )
        cos_api = FakeTikTokAPI()
        cos_processor = self.processor(
            config=switched_to_cos,
            api=cos_api,
        )
        self.assertTrue(switched_to_cos.gate_state()["ready"])
        with self.assertRaises(worker.TTGPUError) as local_mismatch:
            cos_processor.publish(make_publish(switched_to_cos))
        self.assertEqual(
            local_mismatch.exception.code,
            "tt_publish_url_property_mismatch",
        )
        self.assertEqual(cos_api.init_calls, [])

        second_root = self.root / "reverse"
        second_root.mkdir()
        switched_to_local = make_local_config(second_root, gates=True)
        local_api = FakeTikTokAPI()
        reverse_processor = self.processor(
            config=switched_to_local,
            api=local_api,
        )
        seed_prepared(
            reverse_processor,
            direct_post_eligible=True,
        )
        self.assertTrue(switched_to_local.gate_state()["ready"])
        with self.assertRaises(worker.TTGPUError) as cos_mismatch:
            reverse_processor.publish(make_publish(switched_to_local))
        self.assertEqual(
            cos_mismatch.exception.code,
            "tt_publish_url_property_mismatch",
        )
        self.assertEqual(local_api.init_calls, [])

    def test_branded_manifest_is_blocked_even_when_global_gates_open(self):
        config = make_config(self.root, gates=True)
        api = FakeTikTokAPI()
        processor = self.processor(config=config, api=api)
        seed_prepared(processor, direct_post_eligible=False)
        with self.assertRaises(worker.TTGPUError) as caught:
            processor.publish(make_publish(config))
        self.assertEqual(
            caught.exception.code,
            "tt_media_profile_not_direct_post_eligible",
        )
        self.assertEqual(api.init_calls, [])
        self.assertFalse(processor._publish_ledger_path(JOB_ID).exists())

    def test_expired_credential_does_not_create_ledger_and_fresh_one_can_retry(self):
        config = make_config(self.root, gates=True)
        api = FakeTikTokAPI()
        processor = self.processor(config=config, api=api)
        seed_prepared(processor, direct_post_eligible=True)
        expired = seal_access_token(
            config.credential_seal_key,
            TOKEN,
            job_id=JOB_ID,
            source_account_id=ACCOUNT_ID,
            operation="publish",
            ttl_seconds=1,
            now=1,
        )
        payload = make_publish(config, credential_envelope=expired)
        with self.assertRaises(worker.TTGPUError) as caught:
            processor.publish(payload)
        self.assertEqual(caught.exception.code, "credential_envelope_expired")
        self.assertFalse(processor._publish_ledger_path(JOB_ID).exists())
        result = processor.publish(make_publish(config))
        self.assertEqual(result["state"], "initialized")
        self.assertEqual(len(api.init_calls), 1)

    def test_publish_uses_pull_url_is_aigc_and_never_repeats_init(self):
        config = make_config(self.root, gates=True)
        api = FakeTikTokAPI()
        processor = self.processor(config=config, api=api)
        seed_prepared(processor, direct_post_eligible=True)
        result = processor.publish(make_publish(config))
        self.assertEqual(result["publish_id"], "v_pub_url~v2.123")
        self.assertEqual(result["log_id"], "log-init-1")
        self.assertEqual(len(api.init_calls), 1)
        _token, post_info, video_url = api.init_calls[0]
        self.assertTrue(post_info["is_aigc"])
        self.assertTrue(video_url.startswith("https://pull.example.com/"))
        with self.assertRaises(worker.TTGPUError) as repeated:
            processor.publish(make_publish(config))
        self.assertEqual(repeated.exception.code, "tt_publish_reconcile_required")
        self.assertEqual(
            repeated.exception.details["publish_id"],
            "v_pub_url~v2.123",
        )
        self.assertEqual(len(api.init_calls), 1)

    def test_reconcile_remains_available_after_gates_close_and_returns_publish_id(self):
        enabled = make_config(self.root, gates=True)
        api = FakeTikTokAPI()
        processor = self.processor(config=enabled, api=api)
        seed_prepared(processor, direct_post_eligible=True)
        processor.publish(make_publish(enabled))
        closed = replace(
            enabled,
            live_enabled=False,
            direct_audit_approved=False,
            url_property_verified=False,
        )
        recovery = self.processor(config=closed, api=api)
        result = recovery.reconcile(
            {
                "credential_envelope": envelope(closed, "reconcile"),
                "job_id": JOB_ID,
                "source_account_id": ACCOUNT_ID,
            }
        )
        self.assertEqual(result["publish_id"], "v_pub_url~v2.123")
        self.assertEqual(result["state"], "published")
        self.assertEqual(result["status"]["log_id"], "log-status-1")
        self.assertEqual(len(api.init_calls), 1)
        self.assertEqual(len(api.status_calls), 1)

    def test_unknown_init_outcome_is_ledgered_and_retry_is_blocked(self):
        config = make_config(self.root, gates=True)
        api = FakeTikTokAPI()
        api.init_error = worker.TTGPUError(
            "tt_upstream_unavailable",
            "request outcome unavailable",
            503,
            {"log_id": "log-unknown-1"},
        )
        processor = self.processor(config=config, api=api)
        seed_prepared(processor, direct_post_eligible=True)
        with self.assertRaises(worker.TTGPUError):
            processor.publish(make_publish(config))
        ledger = worker._read_json(processor._publish_ledger_path(JOB_ID))
        self.assertEqual(ledger["state"], "init_outcome_unknown")
        self.assertEqual(ledger["upstream_log_id"], "log-unknown-1")
        with self.assertRaises(worker.TTGPUError) as repeated:
            processor.publish(make_publish(config))
        self.assertEqual(repeated.exception.code, "tt_publish_retry_blocked")
        self.assertEqual(len(api.init_calls), 1)

    def test_http_500_init_is_unknown_and_local_media_is_never_cleaned(self):
        config = make_local_config(self.root, gates=True)
        api = worker.TikTokContentPostingAPI(
            opener=HTTP500TikTokOpener(),
        )
        processor = worker.TTPostGPUProcessor(
            config,
            runner=FakeRunner(),
            downloader=make_downloader([]),
            tiktok_api=api,
        )
        processor.prepare(make_prepare())
        manifest_path = processor._prepare_manifest_path(JOB_ID)
        manifest = worker._read_json(manifest_path)
        manifest["result"]["direct_post_eligible"] = True
        worker._atomic_write_json(manifest_path, manifest)
        blob = processor._local_media_store()._path(
            manifest["storage"]["key"]
        )
        with self.assertRaises(worker.TTGPUError) as caught:
            processor.publish(make_publish(config))
        self.assertEqual(
            caught.exception.code,
            "tt_upstream_unavailable",
        )
        ledger = worker._read_json(
            processor._publish_ledger_path(JOB_ID)
        )
        self.assertEqual(ledger["state"], "init_outcome_unknown")
        cleanup = processor.cleanup_due_media()
        self.assertEqual(cleanup["released"], 0)
        self.assertTrue(blob.exists())

    def test_caption_limit_uses_utf16_units_and_is_aigc_is_boolean(self):
        config = make_config(self.root, gates=True)
        accepted = worker.validate_publish_request(
            make_publish(config, title="😀" * 1100)
        )
        self.assertEqual(accepted["title"], "😀" * 1100)
        with self.assertRaises(worker.TTGPUError):
            worker.validate_publish_request(
                make_publish(config, title="😀" * 1101)
            )
        with self.assertRaises(worker.TTGPUError):
            worker.validate_publish_request(
                make_publish(config, is_aigc="true")
            )

    def test_raw_token_field_is_rejected_without_echoing_secret(self):
        payload = {
            "access_token": TOKEN,
            "job_id": JOB_ID,
            "source_account_id": ACCOUNT_ID,
        }
        with self.assertRaises(worker.TTGPUError) as caught:
            worker.validate_credential_request(payload)
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_http_transport_is_bearer_protected_and_never_reflects_token(self):
        config = make_config(self.root)
        api = FakeTikTokAPI()
        processor = self.processor(config=config, api=api)
        server = worker.TTPostGPUHTTPServer(
            ("127.0.0.1", 0),
            processor,
            config.internal_token,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        try:
            connection = http.client.HTTPConnection(host, port, timeout=5)
            connection.request(
                "POST",
                worker.CREATOR_INFO_PATH,
                body=json.dumps(
                    {
                        "access_token": TOKEN,
                        "job_id": JOB_ID,
                        "source_account_id": ACCOUNT_ID,
                    }
                ),
                headers={
                    "Authorization": "Bearer " + config.internal_token,
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(response.status, 400)
            self.assertNotIn(TOKEN, body)
            request = urllib.request.Request(
                "http://%s:%s%s" % (host, port, worker.CREATOR_INFO_PATH),
                data=json.dumps(
                    {
                        "credential_envelope": envelope(
                            config,
                            "creator_info",
                        ),
                        "job_id": JOB_ID,
                        "source_account_id": ACCOUNT_ID,
                    }
                ).encode("utf-8"),
                headers={
                    "Authorization": "Bearer " + config.internal_token,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                item = json.loads(response.read().decode("utf-8"))["item"]
            self.assertEqual(item["status"], "ok")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_direct_post_client_sends_pull_from_url_and_sanitizes_log_id(self):
        opener = FakeTikTokOpener()
        api = worker.TikTokContentPostingAPI(opener=opener)
        result = api.initialize_video(
            TOKEN,
            {
                "disable_comment": False,
                "disable_duet": False,
                "disable_stitch": False,
                "is_aigc": True,
                "privacy_level": "SELF_ONLY",
                "title": "Watch now",
            },
            "https://pull.example.com/video.mp4",
        )
        self.assertEqual(result["publish_id"], "v_pub_url~v2.789")
        self.assertEqual(result["log_id"], "log-upstream-1")
        request, _timeout = opener.requests[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["source_info"]["source"], "PULL_FROM_URL")
        self.assertEqual(
            body["source_info"]["video_url"],
            "https://pull.example.com/video.mp4",
        )

    def test_manifests_ledgers_and_config_repr_never_contain_token(self):
        config = make_config(self.root, gates=True)
        api = FakeTikTokAPI()
        processor = self.processor(config=config, api=api)
        seed_prepared(processor, direct_post_eligible=True)
        processor.publish(make_publish(config))
        self.assertNotIn(TOKEN, repr(config))
        for path in config.work_root.rglob("*"):
            if path.is_file():
                self.assertNotIn(TOKEN.encode("utf-8"), path.read_bytes())

    def test_deploy_units_are_sandboxed_and_gate_names_have_no_aliases(self):
        publisher = (REPO_ROOT / "deploy" / "tt-gpu-publisher.service").read_text(
            encoding="utf-8"
        )
        tunnel = (
            REPO_ROOT / "deploy" / "tt-gpu-reverse-tunnel.service"
        ).read_text(encoding="utf-8")
        env = (REPO_ROOT / "deploy" / "tt-post-gpu.env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("NoNewPrivileges=true", publisher)
        self.assertIn("ProtectSystem=strict", publisher)
        inaccessible = next(
            line
            for line in publisher.splitlines()
            if line.startswith("InaccessiblePaths=")
        )
        self.assertEqual(
            inaccessible,
            "InaccessiblePaths=/root/.ssh "
            "/root/drama_material_service/.env "
            "/etc/x-post-media-repair.env /etc/ssh",
        )
        self.assertNotIn("/etc/tt-post-gpu.secrets", inaccessible)
        self.assertNotIn("/root/miniconda3", inaccessible)
        self.assertIn("ReadWritePaths=/data/tt-post-publisher", publisher)
        self.assertIn("-R 127.0.0.1:18830:127.0.0.1:8830", tunnel)
        for name in (
            "TT_POST_LIVE_ENABLED=0",
            "TT_POST_DIRECT_AUDIT_APPROVED=0",
            "TT_POST_URL_PROPERTY_VERIFIED=0",
            "TT_POST_URL_PROPERTY_VERIFIED_ORIGIN=",
            "TT_POST_GPU_FONT_FILE=/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "TT_POST_GPU_LOGO_PATH=",
            "TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS=4.333333",
            "TT_POST_GPU_VIDEO_ENCODER=hevc_nvenc",
            "TT_POST_GPU_STORAGE_BACKEND=cos",
            "TT_POST_GPU_MEDIA_HOST=127.0.0.1",
            "TT_POST_GPU_MEDIA_PORT=8831",
            "TT_POST_GPU_LOCAL_MEDIA_ORIGIN=",
            "TT_POST_GPU_LOCAL_MEDIA_PREFIX=tt-post-media/v1",
            "TT_POST_GPU_TERMINAL_MEDIA_GRACE_SECONDS=3600",
            "TT_POST_GPU_COS_TIMEOUT=120",
            "TT_POST_GPU_PREPARE_TOTAL_TIMEOUT=8700",
        ):
            self.assertIn(name, env)
        self.assertNotIn("TT_POST_GPU_LIVE_API_ENABLED", env)
        self.assertNotIn("TT_POST_GPU_PUBLISH_ENABLED", env)
        nginx = (
            REPO_ROOT / "deploy" / "tt-post-gpu-media-nginx.conf.example"
        ).read_text(encoding="utf-8")
        self.assertIn("server_name tt-media.ai.yingliangads.com;", nginx)
        self.assertIn("proxy_pass http://127.0.0.1:8831;", nginx)
        self.assertIn("proxy_set_header Range $http_range;", nginx)
        self.assertIn("proxy_buffering off;", nginx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
