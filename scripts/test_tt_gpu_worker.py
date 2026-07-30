#!/usr/bin/env python3
"""Offline contract tests for the TikTok GPU preparation/publish sidecar."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
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


def prepared_probe(duration):
    return {
        "format": {"duration": str(duration)},
        "streams": [
            {
                "avg_frame_rate": "30/1",
                "codec_name": "h264",
                "codec_type": "video",
                "height": 1920,
                "pix_fmt": "yuv420p",
                "profile": "High",
                "r_frame_rate": "30/1",
                "width": 1080,
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


class FakeObjectStore:
    def __init__(self):
        self.upload_calls = []

    def upload(self, key, path, sha256_value, size):
        self.upload_calls.append(
            {
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
        video_encoder="h264_nvenc",
        cos_secret_id="fixture-id",
        cos_secret_key="fixture-key",
        cos_bucket="fixture-bucket",
        cos_region="ap-fixture",
        cos_domain="https://pull.example.com",
        cos_prefix="tt-post-prepared",
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


def make_prepare(**overrides):
    payload = {
        "content_id": CONTENT_ID,
        "job_id": JOB_ID,
        "source_url": "https://media.example.com/material.mp4",
    }
    payload.update(overrides)
    return payload


def make_downloader(calls):
    def download(url, destination, expected_sha, expected_size, _config):
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
    manifest = {
        "completed_at": "2026-07-29T00:00:00Z",
        "request": {},
        "result": {
            "brand_overlay_review_required": True,
            "content_id": CONTENT_ID,
            "direct_post_eligible": direct_post_eligible,
            "job_id": job_id,
            "output_sha256": "a" * 64,
            "output_size": 1234,
            "output_url": "https://pull.example.com/tt-post-prepared/aa/%s.mp4"
            % ("a" * 64),
            "probe": prepared_probe(45.8),
            "profile": worker.PROFILE,
        },
        "status": "ready",
        "version": 1,
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

    def processor(self, config=None, runner=None, downloader=None, api=None):
        config = config or make_config(self.root)
        return worker.TTPostGPUProcessor(
            config,
            runner=runner or FakeRunner(),
            downloader=downloader or make_downloader([]),
            object_store=FakeObjectStore(),
            tiktok_api=api or FakeTikTokAPI(),
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
        with mock.patch.dict(
            os.environ,
            dict(env, TT_POST_GPU_HOST="0.0.0.0"),
            clear=True,
        ):
            with self.assertRaises(worker.TTGPUError) as caught:
                worker.WorkerConfig.from_env()
        self.assertEqual(caught.exception.code, "invalid_configuration")

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
        self.assertIn("1080-320*", graph)
        self.assertIn("1920-568*", graph)
        self.assertIn("overlay=x=(W-w)/2:y=(H-h)/2", graph)
        self.assertIn("d=0.900000", graph)
        self.assertIn("afade=t=out", graph)
        self.assertIn("adelay=33867|33867", graph)
        self.assertNotIn("-f concat", " ".join(command))

    def test_source_command_places_198_logo_and_applies_clean_cut(self):
        config = make_config(self.root)
        command = worker.build_normalize_command(
            config,
            self.root / "source.mp4",
            self.root / "out.mp4",
            {"has_audio": True},
            worker._base_video_filter(),
            logo_path=config.logo_path,
            output_duration=34.766667,
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("scale=198:198", graph)
        self.assertIn("overlay=72:108", graph)
        self.assertEqual(command[command.index("-t") + 1], "34.766667")

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
        self.assertEqual(len(ffmpeg_commands), 3)
        final_graph = ffmpeg_commands[-1][
            ffmpeg_commands[-1].index("-filter_complex") + 1
        ]
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
            "TT_POST_GPU_FONT_FILE=/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "TT_POST_GPU_LOGO_PATH=",
            "TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS=4.333333",
        ):
            self.assertIn(name, env)
        self.assertNotIn("TT_POST_GPU_LIVE_API_ENABLED", env)
        self.assertNotIn("TT_POST_GPU_PUBLISH_ENABLED", env)


if __name__ == "__main__":
    unittest.main(verbosity=2)
