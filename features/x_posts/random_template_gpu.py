"""X-only random video preparation; no publishing credentials or X API calls."""
from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from . import media_repair as repair
from features.fb_gpu.random_overlay import derive_recipe, load_asset_set, selected_asset_paths, validate_recipe
from features.fb_gpu.prepare_worker import build_command
from features.fb_gpu.render_process import RenderStalled, run_render
from features.random_gpu.compositor import BACKEND

PROFILE = "x-random-overlay-h264-720x1280-v1"
PREPARE_PATH = "/internal/x-post-random-template"
FIELDS = frozenset(("job_key", "queue_id", "content_id", "source_url", "source_sha256",
                    "source_size", "source_duration", "profile", "asset_manifest_sha256", "max_duration_seconds"))
SHA = re.compile(r"[a-f0-9]{64}\Z")
JOB = re.compile(r"x-random-v1-[a-f0-9]{64}\Z")


def validate_request(raw, config):
    if not isinstance(raw, dict) or set(raw) != FIELDS:
        raise repair.MediaRepairError("x_random_request_invalid", "random video request fields invalid", 400)
    if (type(raw["queue_id"]) is not int or not 0 < raw["queue_id"] < 2**63
            or not JOB.fullmatch(str(raw["job_key"])) or raw["profile"] != PROFILE
            or not SHA.fullmatch(str(raw["source_sha256"]))
            or not SHA.fullmatch(str(raw["asset_manifest_sha256"]))
            or type(raw["source_size"]) is not int or not 0 < raw["source_size"] <= config.max_source_bytes
            or type(raw["content_id"]) is not str or not 1 <= len(raw["content_id"]) <= 128
            or type(raw["max_duration_seconds"]) not in (int, float)
            or raw["max_duration_seconds"] not in (140, 600, 14400)):
        raise repair.MediaRepairError("x_random_request_invalid", "random video identity or policy invalid", 400)
    duration = raw["source_duration"]
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0.5 <= duration <= raw["max_duration_seconds"]:
        raise repair.MediaRepairError("x_random_duration_invalid", "random video duration invalid", 400)
    parsed = urlsplit(str(raw["source_url"]))
    if (parsed.scheme != "https" or parsed.hostname not in config.allowed_hosts
            or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443)):
        raise repair.MediaRepairError("x_random_source_not_allowed", "random video source host invalid", 400)
    return dict(raw)


def random_command(config, source, output, info, recipe, assets):
    adapter = SimpleNamespace(ffmpeg=config.ffmpeg_bin, compositor_backend=BACKEND)
    command = build_command(adapter, source, output, info, recipe, assets)
    rate = repair.output_rate_control(config.max_output_bytes, info["duration"])
    command[command.index("-b:v") + 1] = "%sk" % rate["video_bitrate_kbps"]
    command[command.index("-b:a") + 1] = "%sk" % rate["audio_bitrate_kbps"]
    command[-1:-1] = ["-maxrate", "%sk" % rate["video_maxrate_kbps"], "-bufsize", "%sk" % rate["video_bufsize_kbps"]]
    return command


class RandomTemplateProcessor:
    def __init__(self, processor, *, asset_root, manifest_sha256, runner=run_render, assets=None):
        self.repair = processor
        self.config = replace(processor.config, profile=PROFILE,
                              cos_prefix=processor.config.cos_prefix.rstrip("/") + "/random-template")
        self.runner = runner
        self.assets = assets or load_asset_set(Path(asset_root), manifest_sha256)
        self.cos = repair.CosObjectStore(self.config, client=processor.cos.client)
        self.root = repair._ensure_private_directory(self.config.work_root / "random-template")
        self.timeout = int(os.environ.get("X_POST_RANDOM_TEMPLATE_TIMEOUT", "7200"))
        if not 60 <= self.timeout <= 7200:
            raise repair.MediaRepairError("invalid_configuration", "random template timeout invalid", 500)

    @classmethod
    def from_env(cls, processor):
        if os.environ.get("X_POST_RANDOM_TEMPLATE_ENABLED", "0") != "1":
            return None
        return cls(processor, asset_root=os.environ.get("X_POST_RANDOM_TEMPLATE_ASSET_ROOT", ""),
                   manifest_sha256=os.environ.get("X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256", ""))

    def _info(self, path):
        raw = repair._probe_payload(self.config, path, self.repair.runner)
        streams = raw.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        duration = float((raw.get("format") or {}).get("duration") or video.get("duration") or 0)
        if not math.isfinite(duration) or duration < 0.5:
            raise repair.MediaRepairError("x_random_duration_invalid", "source video duration invalid", 422)
        return {"duration": duration, "video": video,
                "has_audio": any(s.get("codec_type") == "audio" for s in streams)}

    def _render(self, source, output, info, recipe, assets):
        started = time.monotonic()
        try:
            self.runner(random_command(self.config, source, output, info, recipe, assets),
                        capture_output=True, text=True, timeout=self.timeout, check=True)
        except RenderStalled:
            # Preserve the proven production stall recovery and original recipe.
            normalized = source.with_name("normalized.mp4")
            remaining = lambda: max(1, int(self.timeout - (time.monotonic() - started)))
            video = info["video"]
            sar = str(video.get("sample_aspect_ratio") or "1:1")
            if not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", sar):
                sar = "1:1"
            cmd = [self.config.ffmpeg_bin, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                   "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-vf",
                   "scale=%s:%s:eval=frame,setsar=%s,fps=30" % (video["width"], video["height"], sar.replace(":", "/")),
                   "-c:v", "h264_nvenc", "-preset", "p5", "-cq", "18", "-b:v", "0", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-af", "aresample=48000:async=1:first_pts=0", "-t", str(info["duration"]), str(normalized)]
            self.runner(cmd, capture_output=True, text=True, timeout=remaining(), check=True)
            normalized_info = self._info(normalized)
            if abs(normalized_info["duration"] - info["duration"]) > 0.05:
                raise repair.MediaRepairError("x_random_duration_changed", "normalized duration changed", 422)
            output.unlink(missing_ok=True)
            self.runner(random_command(self.config, normalized, output, normalized_info, recipe, assets),
                        capture_output=True, text=True, timeout=remaining(), check=True)

    def prepare(self, raw):
        request = validate_request(raw, self.config)
        job = request["job_key"]
        manifest = self.root / (job + ".json")
        with repair._job_flock(self.root / (job + ".lock")):
            saved = repair._read_manifest(manifest)
            if manifest.exists() and saved is None:
                raise repair.MediaRepairError("x_random_manifest_invalid", "random recipe manifest invalid", 409)
            if saved and saved.get("request") != request:
                raise repair.MediaRepairError("x_random_job_conflict", "random video job is already frozen", 409)
            if saved and saved.get("result"):
                result = saved["result"]
                if not self.cos.validate_head(self.cos.head(saved["cos_key"]), result["output_size"], result["output_sha256"]):
                    raise repair.MediaRepairError("x_random_output_missing", "frozen random output is unavailable", 409)
                return result
            if request["asset_manifest_sha256"] != self.assets["manifest_sha256"]:
                raise repair.MediaRepairError("x_random_catalog_mismatch", "frozen random asset catalog is unavailable", 409)
            if saved:
                recipe = saved["recipe"]
                validate_recipe(recipe, self.assets)
            else:
                recipe = derive_recipe(job_id=job, content_id=request["content_id"], profile=PROFILE,
                                       source_url_sha256=hashlib.sha256(request["source_url"].encode()).hexdigest(), asset_set=self.assets)
                saved = {"request": request, "recipe": recipe}
                repair._atomic_write_manifest(manifest, saved)
            with self.repair._gpu_slot:
                if shutil.disk_usage(self.root).free < 16 * 1024**3:
                    raise repair.MediaRepairError("x_random_disk_reserve", "GPU disk reserve unavailable", 503)
                with tempfile.TemporaryDirectory(prefix=job + "-", dir=self.root) as temp:
                    source, output = Path(temp) / "source.mp4", Path(temp) / "output.mp4"
                    downloaded = self.repair.downloader(request["source_url"], source, self.config.allowed_hosts,
                                                        max_bytes=self.config.max_source_bytes, timeout=self.config.download_timeout,
                                                        http_client=self.repair.http_client)
                    if downloaded["sha256"] != request["source_sha256"] or downloaded["size"] != request["source_size"]:
                        raise repair.MediaRepairError("x_random_source_changed", "random source fingerprint changed", 409)
                    info = self._info(source)
                    if abs(info["duration"] - request["source_duration"]) > 0.05:
                        raise repair.MediaRepairError("x_random_source_changed", "random source duration changed", 409)
                    self._render(source, output, info, recipe, selected_asset_paths(recipe, self.assets))
                    probe = repair.validate_output(
                        repair._probe_payload(self.config, output, self.repair.runner), output, (720, 1280),
                        self.config.max_output_bytes, expected_duration=request["source_duration"],
                        max_duration_seconds=140 if request["max_duration_seconds"] == 140 else 14400)
                    if (abs(probe["duration"] - request["source_duration"]) > 0.05
                            or (probe["duration"] > 140) != (request["source_duration"] > 140)
                            or probe["duration"] > request["max_duration_seconds"]
                            or (probe["width"], probe["height"]) != (720, 1280)):
                        raise repair.MediaRepairError("x_random_output_invalid", "random video duration or dimensions changed", 422)
                    digest, size = repair._file_sha256(output)
                    key = self.config.cos_prefix + "/" + job + "/" + digest + ".mp4"
                    self.cos.upload(key, output, digest, size)
                    result = {"job_key": job, "queue_id": request["queue_id"], "profile": PROFILE,
                              "source_sha256": request["source_sha256"], "source_size": request["source_size"],
                              "asset_manifest_sha256": request["asset_manifest_sha256"], "recipe": recipe,
                              "output_url": self.cos.url(key), "output_sha256": digest,
                              "output_size": size, "probe": probe}
                    repair._atomic_write_manifest(manifest, {**saved, "result": result, "cos_key": key})
                    return result
