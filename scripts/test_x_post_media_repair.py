#!/usr/bin/env python3
"""Offline tests for the X Post GPU media-repair worker."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts import media_repair  # noqa: E402


def source_probe(
    width=1080,
    height=1920,
    audio=False,
    rotation=0,
    frame_rate="25/1",
    field_order="progressive",
):
    video = {
        "codec_type": "video",
        "codec_name": "hevc",
        "width": width,
        "height": height,
        "avg_frame_rate": frame_rate,
        "r_frame_rate": frame_rate,
        "field_order": field_order,
    }
    if rotation:
        video["side_data_list"] = [{"rotation": rotation}]
    streams = [video]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "mp3"})
    return {"streams": streams, "format": {"duration": "12.5"}}


def repaired_probe(width=720, height=1280):
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "profile": "High",
                "pix_fmt": "yuv420p",
                "field_order": "progressive",
                "width": width,
                "height": height,
                "avg_frame_rate": "30/1",
                "r_frame_rate": "30/1",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "profile": "LC",
                "sample_rate": "48000",
                "channels": 2,
                "channel_layout": "stereo",
            },
        ],
        "format": {"duration": "12.5"},
    }


class FakeNotFound(Exception):
    status_code = 404


class FakeCosClient:
    def __init__(self):
        self.objects = {}
        self.upload_calls = []
        self.head_calls = []

    def head_object(self, *, Bucket, Key):
        self.head_calls.append((Bucket, Key))
        if Key not in self.objects:
            raise FakeNotFound("not found")
        item = self.objects[Key]
        return {
            "Content-Length": str(len(item["body"])),
            "x-cos-meta-sha256": item["metadata"]["x-cos-meta-sha256"],
            "x-cos-meta-profile": item["metadata"]["x-cos-meta-profile"],
        }

    def upload_file(self, **kwargs):
        self.upload_calls.append(dict(kwargs))
        body = Path(kwargs["LocalFilePath"]).read_bytes()
        self.objects[kwargs["Key"]] = {
            "body": body,
            "metadata": dict(kwargs["Metadata"]),
        }
        return {"ETag": "fixture"}


class FakeRunner:
    def __init__(self, source_payload=None, output_payload=None, output_bytes=b"repaired-video"):
        self.source_payload = source_payload or source_probe()
        self.output_payload = output_payload or repaired_probe()
        self.output_bytes = output_bytes
        self.commands = []
        self.kwargs = []
        self.probe_count = 0

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        self.kwargs.append(dict(kwargs))
        executable = Path(command[0]).name.lower()
        if "ffprobe" in executable:
            self.probe_count += 1
            payload = self.source_payload if self.probe_count == 1 else self.output_payload
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        if "ffmpeg" in executable:
            Path(command[-1]).write_bytes(self.output_bytes)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError("unexpected command: %r" % command)


def make_config(root):
    return media_repair.WorkerConfig(
        enabled=True,
        host="127.0.0.1",
        port=0,
        token="t" * 40,
        allowed_hosts=("media.example.com",),
        work_root=Path(root),
        ffmpeg_bin=str((Path(root) / "ffmpeg").resolve()),
        ffprobe_bin=str((Path(root) / "ffprobe").resolve()),
        cos_secret_id="fixture-id",
        cos_secret_key="fixture-key",
        cos_bucket="fixture-bucket",
        cos_region="ap-fixture",
        cos_domain="https://cos.example.com",
        cos_prefix="x-post-media-repair",
        max_source_bytes=1024 * 1024,
        max_output_bytes=1024 * 1024,
        download_timeout=30,
        probe_timeout=30,
        transcode_timeout=60,
    )


def make_request(source=b"source-video", **overrides):
    payload = {
        "job_key": hashlib.sha256(b"job").hexdigest(),
        "material_id": "5779172",
        "pool_item_id": "8",
        "source_url": "https://media.example.com/source.mp4",
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "source_size": len(source),
        "trigger_code": "invalid_media_codec",
        "profile": media_repair.REPAIR_PROFILE,
    }
    payload.update(overrides)
    return payload


class MediaRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_configuration_is_disabled_by_default_and_rejects_non_loopback(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(media_repair.MediaRepairError) as caught:
                media_repair.WorkerConfig.from_env()
        self.assertEqual(caught.exception.code, "media_repair_disabled")

        with mock.patch.dict(
            os.environ,
            {
                "X_POST_MEDIA_REPAIR_ENABLED": "1",
                "X_POST_MEDIA_REPAIR_HOST": "0.0.0.0",
            },
            clear=True,
        ):
            with self.assertRaises(media_repair.MediaRepairError) as caught:
                media_repair.WorkerConfig.from_env()
        self.assertEqual(caught.exception.code, "invalid_configuration")

    def test_request_contract_is_exact_and_only_two_triggers_are_repairable(self):
        request = media_repair.validate_request(make_request())
        self.assertEqual(request["profile"], media_repair.REPAIR_PROFILE)
        self.assertEqual(request["pool_item_id"], "8")
        resource_id = "21c09b915223a0695f0a4cf85386cabd"
        resource_request = media_repair.validate_request(
            make_request(material_id=resource_id)
        )
        self.assertEqual(resource_request["material_id"], resource_id)

        for trigger in ("invalid_media_codec", "invalid_media_dimensions"):
            request = media_repair.validate_request(make_request(trigger_code=trigger))
            self.assertEqual(request["trigger_code"], trigger)
        for invalid_material_id in (
            "../21c09b915223a0695f0a4cf85386cabd",
            "21c09b915223a0695f0a4cf85386cab/",
            "g1c09b915223a0695f0a4cf85386cabd",
            "21C09B915223A0695F0A4CF85386CABD",
            "21c09b915223a0695f0a4cf85386cab",
            "021c09b915223a0695f0a4cf85386cabd",
            "0",
        ):
            with self.subTest(material_id=invalid_material_id):
                with self.assertRaises(media_repair.MediaRepairError) as caught:
                    media_repair.validate_request(
                        make_request(material_id=invalid_material_id)
                    )
                self.assertEqual(caught.exception.code, "invalid_request")
        with self.assertRaises(media_repair.MediaRepairError) as caught:
            media_repair.validate_request(
                make_request(pool_item_id="21c09b915223a0695f0a4cf85386cabd")
            )
        self.assertEqual(caught.exception.code, "invalid_request")
        with self.assertRaises(media_repair.MediaRepairError) as caught:
            media_repair.validate_request(make_request(trigger_code="invalid_media_duration"))
        self.assertEqual(caught.exception.code, "trigger_not_repairable")
        with self.assertRaises(media_repair.MediaRepairError) as caught:
            media_repair.validate_request(make_request(unexpected=True))
        self.assertEqual(caught.exception.code, "invalid_request")
        with self.assertRaises(media_repair.MediaRepairError) as caught:
            media_repair.validate_request(make_request(profile="other-profile"))
        self.assertEqual(caught.exception.code, "profile_mismatch")

    def test_orientation_selects_standard_canvas_and_honors_rotation(self):
        self.assertEqual(media_repair.inspect_source(source_probe())["canvas"], (720, 1280))
        self.assertEqual(
            media_repair.inspect_source(source_probe(width=1920, height=1080))["canvas"],
            (1280, 720),
        )
        self.assertEqual(
            media_repair.inspect_source(source_probe(width=800, height=800))["canvas"],
            (720, 720),
        )
        self.assertEqual(
            media_repair.inspect_source(
                source_probe(width=1920, height=1080, rotation=90)
            )["canvas"],
            (720, 1280),
        )

    def test_source_with_non_repairable_scan_or_frame_rate_is_rejected(self):
        for payload in (
            source_probe(field_order="tt"),
            source_probe(frame_rate="120/1"),
            source_probe(frame_rate="0/0"),
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(media_repair.MediaRepairError) as caught:
                    media_repair.inspect_source(payload)
                self.assertEqual(caught.exception.code, "source_not_repairable")

    def test_ffmpeg_command_is_nvenc_cfr30_gop60_pad_without_crop_and_adds_silence(self):
        config = make_config(self.root)
        source_info = media_repair.inspect_source(source_probe(audio=False))
        command = media_repair.build_ffmpeg_command(
            config,
            self.root / "source.mp4",
            self.root / "output.mp4",
            source_info,
        )
        joined = " ".join(command)
        self.assertIn("-c:v h264_nvenc", joined)
        self.assertIn("-preset p5", joined)
        self.assertIn("-tune hq", joined)
        self.assertIn("-profile:v high", joined)
        self.assertIn("-pix_fmt yuv420p", joined)
        self.assertIn("-fps_mode cfr", joined)
        self.assertIn("-g 60", joined)
        self.assertIn("-keyint_min 60", joined)
        self.assertIn("-flags +cgop", joined)
        self.assertIn("-rc vbr", joined)
        self.assertIn("-cq 20", joined)
        self.assertIn("-b:v 5M", joined)
        self.assertIn("-maxrate 6M", joined)
        self.assertIn("-bufsize 10M", joined)
        self.assertIn("-profile:a aac_low", joined)
        self.assertIn("-ar 48000", joined)
        self.assertIn("-ac 2", joined)
        self.assertIn("-b:a 128k", joined)
        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=48000", joined)
        self.assertIn("-shortest", command)
        video_filter = command[command.index("-vf") + 1]
        self.assertIn("scale=w=720:h=1280:force_original_aspect_ratio=decrease", video_filter)
        self.assertIn("pad=720:1280", video_filter)
        self.assertNotIn("crop=", video_filter)
        self.assertIn("yadif=", video_filter)

        with_audio = media_repair.build_ffmpeg_command(
            config,
            self.root / "source.mp4",
            self.root / "output.mp4",
            media_repair.inspect_source(source_probe(audio=True)),
        )
        self.assertNotIn("anullsrc=channel_layout=stereo:sample_rate=48000", with_audio)
        self.assertNotIn("-shortest", with_audio)
        self.assertEqual(with_audio[with_audio.index("-map") + 1], "0:v:0")
        second_map = with_audio.index("-map", with_audio.index("-map") + 1)
        self.assertEqual(with_audio[second_map + 1], "0:a:0")

    def test_end_to_end_repair_uploads_verifies_manifests_and_reuses(self):
        source = b"source-video"
        request = make_request(source)
        config = make_config(self.root)
        cos = FakeCosClient()
        runner = FakeRunner()
        download_count = {"value": 0}

        def downloader(url, destination, allowed_hosts, **kwargs):
            download_count["value"] += 1
            self.assertEqual(url, request["source_url"])
            self.assertEqual(tuple(allowed_hosts), ("media.example.com",))
            self.assertIsNone(kwargs["http_client"])
            Path(destination).write_bytes(source)
            return {
                "path": Path(destination),
                "size": len(source),
                "sha256": hashlib.sha256(source).hexdigest(),
                "media_type": "video/mp4",
            }

        processor = media_repair.MediaRepairProcessor(
            config,
            runner=runner,
            cos_client=cos,
            downloader=downloader,
        )
        result = processor.repair(request)
        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["reused"])
        self.assertEqual(result["job_key"], request["job_key"])
        self.assertEqual(result["profile"], media_repair.REPAIR_PROFILE)
        self.assertTrue(result["output_url"].startswith("https://cos.example.com/"))
        self.assertEqual(result["probe"]["codec"], "h264")
        self.assertEqual(result["probe"]["pixel_format"], "yuv420p")
        self.assertEqual(result["probe"]["audio_codec"], "aac")
        self.assertEqual((result["probe"]["width"], result["probe"]["height"]), (720, 1280))
        self.assertEqual(result["probe"]["frame_rate"], 30.0)
        self.assertEqual(result["probe"]["size"], result["output_size"])
        self.assertEqual(len(cos.upload_calls), 1)
        upload = cos.upload_calls[0]
        self.assertEqual(
            upload["Metadata"]["x-cos-meta-sha256"],
            result["output_sha256"],
        )
        self.assertEqual(
            upload["Metadata"]["x-cos-meta-profile"],
            media_repair.REPAIR_PROFILE,
        )
        self.assertIn("/%s/material-5779172/" % media_repair.REPAIR_PROFILE, upload["Key"])
        self.assertIn("/source-%s/" % request["source_sha256"], upload["Key"])
        self.assertTrue(upload["Key"].endswith("/output-%s.mp4" % result["output_sha256"]))

        manifest_path = self.root / "manifests" / (request["job_key"] + ".json")
        self.assertTrue(manifest_path.is_file())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(manifest_path.stat().st_mode), 0o600)
        self.assertFalse((self.root / "work" / request["job_key"]).exists())
        first_command_count = len(runner.commands)

        reused = processor.repair(request)
        self.assertEqual(reused["status"], "ready")
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["output_sha256"], result["output_sha256"])
        self.assertEqual(download_count["value"], 1)
        self.assertEqual(len(runner.commands), first_command_count)
        self.assertEqual(len(cos.upload_calls), 1)
        for kwargs in runner.kwargs:
            self.assertEqual(
                kwargs["env"],
                {
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                },
            )
            self.assertNotIn("COS_SECRET_KEY", kwargs["env"])
            self.assertNotIn("X_POST_MEDIA_REPAIR_TOKEN", kwargs["env"])
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertTrue(kwargs["close_fds"])

    def test_drama_resource_id_uses_a_canonical_path_safe_cos_segment(self):
        config = make_config(self.root)
        processor = media_repair.MediaRepairProcessor(
            config,
            runner=FakeRunner(),
            cos_client=FakeCosClient(),
        )
        resource_id = "21c09b915223a0695f0a4cf85386cabd"
        request = media_repair.validate_request(
            make_request(material_id=resource_id)
        )

        key = processor._cos_key(request, "b" * 64)

        self.assertEqual(
            key,
            "x-post-media-repair/%s/drama-resource-%s/source-%s/output-%s.mp4"
            % (
                media_repair.REPAIR_PROFILE,
                resource_id,
                request["source_sha256"],
                "b" * 64,
            ),
        )
        self.assertNotIn("..", key)

    def test_integrity_mismatch_stops_before_probe_or_cos(self):
        source = b"actual-source"
        config = make_config(self.root)
        cos = FakeCosClient()
        runner = FakeRunner()

        def downloader(_url, destination, _hosts, **_kwargs):
            Path(destination).write_bytes(source)
            return {
                "path": Path(destination),
                "size": len(source),
                "sha256": hashlib.sha256(source).hexdigest(),
                "media_type": "video/mp4",
            }

        processor = media_repair.MediaRepairProcessor(
            config,
            runner=runner,
            cos_client=cos,
            downloader=downloader,
        )
        with self.assertRaises(media_repair.MediaRepairError) as caught:
            processor.repair(make_request(b"different-source"))
        self.assertEqual(caught.exception.code, "source_integrity_mismatch")
        self.assertEqual(runner.commands, [])
        self.assertEqual(cos.upload_calls, [])

    def test_output_validation_fails_closed_before_cos(self):
        source = b"source-video"
        config = make_config(self.root)
        cos = FakeCosClient()
        bad_output = repaired_probe()
        bad_output["streams"][0]["pix_fmt"] = "yuv444p"
        runner = FakeRunner(output_payload=bad_output)

        def downloader(_url, destination, _hosts, **_kwargs):
            Path(destination).write_bytes(source)
            return {
                "path": Path(destination),
                "size": len(source),
                "sha256": hashlib.sha256(source).hexdigest(),
                "media_type": "video/mp4",
            }

        processor = media_repair.MediaRepairProcessor(
            config,
            runner=runner,
            cos_client=cos,
            downloader=downloader,
        )
        with self.assertRaises(media_repair.MediaRepairError) as caught:
            processor.repair(make_request(source))
        self.assertEqual(caught.exception.code, "repaired_media_invalid")
        self.assertEqual(cos.upload_calls, [])

    def test_output_validation_rejects_truncated_transcode_before_cos(self):
        source = b"source-video"
        config = make_config(self.root)
        cos = FakeCosClient()
        truncated = repaired_probe()
        truncated["format"]["duration"] = "2.0"
        runner = FakeRunner(output_payload=truncated)

        def downloader(_url, destination, _hosts, **_kwargs):
            Path(destination).write_bytes(source)
            return {
                "path": Path(destination),
                "size": len(source),
                "sha256": hashlib.sha256(source).hexdigest(),
                "media_type": "video/mp4",
            }

        processor = media_repair.MediaRepairProcessor(
            config,
            runner=runner,
            cos_client=cos,
            downloader=downloader,
        )
        with self.assertRaises(media_repair.MediaRepairError) as caught:
            processor.repair(make_request(source))
        self.assertEqual(caught.exception.code, "repaired_media_invalid")
        self.assertEqual(cos.upload_calls, [])

    def test_ready_manifest_binds_job_key_and_rejects_a_different_request(self):
        source = b"source-video"
        request = make_request(source)
        config = make_config(self.root)
        cos = FakeCosClient()
        runner = FakeRunner()

        def downloader(_url, destination, _hosts, **_kwargs):
            Path(destination).write_bytes(source)
            return {
                "path": Path(destination),
                "size": len(source),
                "sha256": hashlib.sha256(source).hexdigest(),
                "media_type": "video/mp4",
            }

        processor = media_repair.MediaRepairProcessor(
            config,
            runner=runner,
            cos_client=cos,
            downloader=downloader,
        )
        processor.repair(request)
        with self.assertRaises(media_repair.MediaRepairError) as caught:
            processor.repair(dict(request, pool_item_id="9"))
        self.assertEqual(caught.exception.code, "job_key_conflict")
        self.assertEqual(len(cos.upload_calls), 1)

    def test_http_transport_has_safe_health_exact_route_and_bearer_gate(self):
        config = SimpleNamespace(profile=media_repair.REPAIR_PROFILE)
        strict_response = {
            "status": "ready",
            "reused": False,
            "job_key": hashlib.sha256(b"job").hexdigest(),
            "profile": media_repair.REPAIR_PROFILE,
            "output_url": "https://cos.example.com/output.mp4",
            "output_sha256": hashlib.sha256(b"output").hexdigest(),
            "output_size": 100,
            "probe": {
                "codec": "h264",
                "pixel_format": "yuv420p",
                "audio_codec": "aac",
                "width": 720,
                "height": 1280,
                "frame_rate": 30.0,
                "duration": 10.0,
                "size": 100,
            },
        }

        class Processor:
            def __init__(self):
                self.config = config
                self.calls = []

            def repair(self, payload):
                self.calls.append(payload)
                return dict(strict_response)

        processor = Processor()
        server = media_repair.MediaRepairHTTPServer(
            ("127.0.0.1", 0),
            processor,
            "z" * 40,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/health")
            response = connection.getresponse()
            health = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(health["profile"], media_repair.REPAIR_PROFILE)
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            connection.close()

            body = json.dumps(make_request()).encode("utf-8")
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request(
                "POST",
                media_repair.REPAIR_PATH,
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            self.assertNotIn(b"z" * 20, response.read())
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request(
                "POST",
                media_repair.REPAIR_PATH,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + ("z" * 40),
                },
            )
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(result, strict_response)
            self.assertEqual(processor.calls, [make_request()])
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_deployment_units_keep_worker_loopback_secrets_separate_and_disabled(self):
        service = (REPO_ROOT / "deploy" / "x-post-media-repair.service").read_text(
            encoding="utf-8"
        )
        tunnel = (
            REPO_ROOT / "deploy" / "x-post-media-repair-tunnel.service"
        ).read_text(encoding="utf-8")
        example = (
            REPO_ROOT / "deploy" / "x-post-media-repair.env.example"
        ).read_text(encoding="utf-8")
        self.assertIn("EnvironmentFile=/etc/x-post-media-repair.token", service)
        self.assertIn("/root/miniconda3/envs/drama-voice/bin/python", service)
        self.assertIn("WorkingDirectory=/opt/x-post-media-repair/current", service)
        self.assertIn("ReadWritePaths=/data/x-post-media-repair", service)
        self.assertIn("ConditionPathIsMountPoint=/data", service)
        self.assertIn(
            "-R 127.0.0.1:18820:127.0.0.1:8820",
            tunnel,
        )
        self.assertIn("StrictHostKeyChecking=yes", tunnel)
        self.assertIn("X_POST_MEDIA_REPAIR_ENABLED=0", example)
        self.assertIn(
            "X_POST_MEDIA_REPAIR_FFMPEG_BIN=/opt/ffmpeg-nvenc/ffmpeg",
            example,
        )
        self.assertNotRegex(example, r"(?m)^X_POST_MEDIA_REPAIR_TOKEN=")
        self.assertNotRegex(example, r"(?m)^COS_SECRET_(?:ID|KEY)=")


if __name__ == "__main__":
    unittest.main(verbosity=2)
