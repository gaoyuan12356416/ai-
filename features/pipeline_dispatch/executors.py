"""Local renderers and direct platform transports used by either worker host.

Only the authority freezes identities, approves credentials and owns canonical
publication state. This module performs actual local compute/network work.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from contextlib import contextmanager
import hashlib
import base64
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.parse import quote

from .client import CoordinatorError


class ExecutionError(RuntimeError):
    def __init__(self, code, *, unknown=False):
        self.code, self.unknown_outcome = code, bool(unknown)
        super().__init__(code)


def _artifact(value):
    result = dict(value or {})
    result["url"] = result.get("url") or result.get("output_url")
    result["sha256"] = result.get("sha256") or result.get("output_sha256")
    result["size"] = result.get("size") or result.get("output_size")
    if not result.get("url") or not re.fullmatch(r"[a-f0-9]{64}", str(result.get("sha256") or "")) or not isinstance(result.get("size"), int) or result["size"] <= 0:
        raise ExecutionError("artifact_identity_missing")
    result.setdefault("mime", "video/mp4")
    if "duration_seconds" not in result and isinstance(result.get("probe"), dict):
        result["duration_seconds"] = result["probe"].get("duration", 0)
    if "recipe" not in result and isinstance(result.get("random_overlay_recipe"), dict):
        result["recipe"] = result["random_overlay_recipe"]
    return result


def cpu_thread_command(command):
    result = list(command)
    if not result or "ffmpeg" not in Path(str(result[0])).name.lower():
        return result
    if any(encoder in result for encoder in ("h264_nvenc", "hevc_nvenc")):
        raise ExecutionError("cpu_executor_gpu_encoder_forbidden")
    for flag in ("-filter_threads", "-filter_complex_threads"):
        if flag not in result:
            result[1:1] = [flag, "4"]
    # Bound each decoder and the encoder. These are per-stream FFmpeg options.
    indices = [i for i, value in enumerate(result) if value == "-i"]
    for index in reversed(indices):
        result[index:index] = ["-threads", "4"]
    result[-1:-1] = ["-threads", "4"]
    return result


class COSArtifactStore:
    """Private recovery receipt, written after qualification and before upload.

    The locator excludes claim generation and host. A receipt alone is never
    ready: the content-addressed object must pass COS HEAD identity checks.
    """
    MAX_RECEIPT = 4 * 1024 * 1024

    def __init__(self, store, kind, request, *, source_key=""):
        self.store, self.config, self.client = store, store.config, store.client
        identity = request.get("job_id") or request.get("job_key")
        if kind == "youtube_prepare":
            task = request.get("task") or {}
            identity = ":".join(str(task.get(name) or "") for name in ("id", "current_version", "generation_retry_id"))
        if not identity:
            raise ExecutionError("artifact_recovery_identity_missing")
        configuration = {name: getattr(self.config, name, "") for name in (
            "profile", "compositor_backend", "video_encoder", "asset_manifest_sha256", "random_overlay_manifest_sha256")}
        self.binding = {"version": 1, "kind": kind, "identity": str(identity), "source_key": str(source_key),
                        "request_sha256": self.digest(request), "configuration_sha256": self.digest(configuration)}
        locator = self.digest({"kind": kind, "identity": str(identity)})
        self.key = self.config.cos_prefix.rstrip("/") + "/pipeline-receipts/v1/" + locator + ".json"

    @staticmethod
    def encoded(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")

    @classmethod
    def digest(cls, value):
        return hashlib.sha256(cls.encoded(value)).hexdigest()

    @staticmethod
    def status(exc):
        value = getattr(exc, "get_status_code", None)
        try:
            return int(value() if callable(value) else getattr(exc, "status_code", 0))
        except (TypeError, ValueError):
            return 0

    def _document(self, key):
        try:
            response = self.client.get_object(Bucket=self.config.cos_bucket, Key=key)
        except Exception as exc:
            if self.status(exc) == 404:
                return None
            raise ExecutionError("artifact_receipt_read_failed") from None
        stream = response["Body"].get_raw_stream()
        try:
            raw = stream.read(self.MAX_RECEIPT + 1)
        finally:
            stream.close()
        try:
            if len(raw) > self.MAX_RECEIPT:
                raise ValueError()
            receipt = json.loads(raw)
            if not isinstance(receipt, dict):
                raise ValueError()
        except (TypeError, ValueError):
            raise ExecutionError("artifact_receipt_conflict") from None
        return receipt

    def _read(self):
        receipt = self._document(self.key)
        if receipt is None:
            return None
        try:
            if receipt.get("binding") != self.binding or receipt.get("result_sha256") != self.digest(receipt["result"]):
                raise ValueError()
            artifact = _artifact(receipt["result"].get("artifact") or receipt["result"])
            key = receipt["object_key"]
            expected_url = self.config.cos_domain.rstrip("/") + "/" + "/".join(quote(part, safe="-._~") for part in key.split("/"))
            if not key.startswith(self.config.cos_prefix.rstrip("/") + "/") or "/../" in key or artifact["url"] != expected_url:
                raise ValueError()
        except (KeyError, TypeError, ValueError, ExecutionError):
            raise ExecutionError("artifact_receipt_conflict") from None
        return receipt

    def begin_generation(self):
        """One paid generation per explicitly authorized task/version/retry.

        A random invocation ID distinguishes our lost PUT acknowledgement from
        another node winning the immutable intent. No existing intent is a
        permission to call the generator again.
        """
        key = self.key[:-5] + ".generation-intent.json"
        if self._document(key) is not None:
            raise ExecutionError("youtube_generation_interrupted")
        intent = {"binding": self.binding, "invocation_id": os.urandom(16).hex()}
        try:
            self.client.put_object(Bucket=self.config.cos_bucket, Key=key, Body=self.encoded(intent),
                                   ACL="private", ContentType="application/json", EnableMD5=True,
                                   Metadata={"x-cos-forbid-overwrite": "true"})
        except Exception:
            pass
        # No generation may start until this invocation's intent is durable.
        if self._document(key) != intent:
            raise ExecutionError("youtube_generation_interrupted")

    def recover(self):
        receipt = self._read()
        if receipt is None:
            return None
        artifact = _artifact(receipt["result"].get("artifact") or receipt["result"])
        try:
            head = self.client.head_object(Bucket=self.config.cos_bucket, Key=receipt["object_key"])
        except Exception as exc:
            if self.status(exc) == 404:
                # Qualified locally does not prove a completed remote upload.
                # Keep the original identity for reconciliation; never rerender.
                raise ExecutionError("artifact_upload_incomplete") from None
            raise ExecutionError("artifact_head_failed") from None
        lowered = {str(k).lower().replace("_", "-"): v for k, v in head.items()}
        try:
            valid = int(lowered.get("content-length", -1)) == artifact["size"] and lowered.get("x-cos-meta-sha256") == artifact["sha256"]
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ExecutionError("artifact_fingerprint_changed")
        return receipt["result"]

    def qualified(self, result, object_key):
        _artifact(result.get("artifact") or result)
        receipt = {"binding": self.binding, "object_key": object_key, "result": result, "result_sha256": self.digest(result)}
        body = self.encoded(receipt)
        if len(body) > self.MAX_RECEIPT:
            raise ExecutionError("artifact_receipt_too_large")
        existing = self._read()
        if existing is None:
            try:
                self.client.put_object(Bucket=self.config.cos_bucket, Key=self.key, Body=body,
                                       ACL="private", ContentType="application/json", EnableMD5=True,
                                       Metadata={"x-cos-forbid-overwrite": "true"})
            except Exception:
                # Includes lost PUT acknowledgements: read the immutable facts.
                pass
            existing = self._read()
        if existing != receipt:
            raise ExecutionError("artifact_receipt_conflict")


class NodeExecutors:
    PREPARE = ("x_preflight", "x_random", "x_repair", "fb_prepare", "tt_prepare", "youtube_prepare")
    PUBLISH = ("x_publish", "x_share_publish", "fb_publish", "tt_publish", "youtube_publish")

    def __init__(self, config, *, factories=None, platform_clients=None):
        self.config = config
        self.factories = factories or {}
        self.platform_clients = platform_clients or {}
        self.processors = {}
        self.work_root = Path(config["state_root"]) / "media"
        self.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def capabilities(self, stage=None):
        # Deployment enables only adapters whose node config/assets were verified.
        enabled = {item.strip() for item in str(os.environ.get("PIPELINE_EXECUTORS", "")).split(",") if item.strip()}
        if not enabled:
            enabled = set(self.factories) | set(self.platform_clients)
        supported = self.PREPARE if stage == "prepare" else self.PUBLISH if stage == "publish" else self.PREPARE + self.PUBLISH
        kinds = {kind for kind in supported if kind in enabled}
        stages = {("prepare" if kind in self.PREPARE else "publish") + ":" + kind.split("_", 1)[0] for kind in kinds}
        profiles = {"profile:" + name.strip() for name in os.environ.get("PIPELINE_RENDER_PROFILES", "").split(",") if name.strip()}
        catalogs = set()
        for channel, variable in (("x", "X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256"),
                                  ("fb", "FB_PAGE_GPU_RANDOM_OVERLAY_MANIFEST_SHA256"),
                                  ("tt", "TT_POST_GPU_RANDOM_OVERLAY_MANIFEST_SHA256")):
            fingerprint = os.environ.get(variable, "").strip().lower()
            if "prepare:" + channel in stages and re.fullmatch(r"[a-f0-9]{64}", fingerprint):
                catalogs.add("catalog:" + channel + ":" + fingerprint)
        return sorted(kinds | stages | profiles | catalogs | {"backend:" + self.config["backend"]})

    def execute(self, job, context):
        payload = dict(job.get("payload") or {})
        kind = str(payload.get("kind") or "")
        if kind not in self.PREPARE + self.PUBLISH:
            raise ExecutionError("executor_not_supported")
        if job["stage"] == "prepare" and kind not in self.PREPARE or job["stage"] == "publish" and kind not in self.PUBLISH:
            raise ExecutionError("executor_stage_mismatch")
        context.check()
        if kind in self.PREPARE:
            return self.prepare(kind, payload, context)
        return getattr(self, kind)(payload, job.get("artifact") or payload.get("artifact"), context)

    def _processor(self, kind):
        if kind in self.processors:
            return self.processors[kind]
        if kind in self.factories:
            processor = self.factories[kind]()
        elif kind in {"x_random", "x_repair"}:
            from features.x_posts.media_repair import WorkerConfig, MediaRepairProcessor
            from features.fb_gpu.prepare_worker import software_command
            config = WorkerConfig.from_env()
            cpu = self.config["backend"] == "cpu"
            if cpu:
                config = replace(config, profile="x-h264-libx264-720-duration-policy-v1")
            def runner(command, **kwargs):
                return subprocess.run(cpu_thread_command(software_command(command)) if cpu else command, **kwargs)
            repair = MediaRepairProcessor(config, runner=runner)
            if kind == "x_repair":
                processor = repair
            else:
                from features.x_posts.random_template_gpu import RandomTemplateProcessor
                from features.fb_gpu.render_process import run_render
                def render(command, **kwargs):
                    return run_render(cpu_thread_command(command) if cpu else command, **kwargs)
                processor = RandomTemplateProcessor(repair, asset_root=os.environ.get("X_POST_RANDOM_TEMPLATE_ASSET_ROOT", ""), manifest_sha256=os.environ.get("X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256", ""), backend=self.config["backend"], runner=render)
        elif kind == "fb_prepare":
            from features.fb_gpu.prepare_worker import WorkerConfig, PrepareProcessor
            from features.fb_gpu.render_process import run_render
            def render(command, **kwargs):
                return run_render(cpu_thread_command(command) if self.config["backend"] == "cpu" else command, **kwargs)
            processor = PrepareProcessor(WorkerConfig.from_env(), runner=render)
        elif kind == "tt_prepare":
            from features.tt_gpu.worker import WorkerConfig, TTPostGPUProcessor
            config = WorkerConfig.from_env()
            if config.storage_backend != "cos":
                raise ExecutionError("portable_artifact_requires_cos")
            if self.config["backend"] == "cpu" and config.video_encoder != "libx264":
                raise ExecutionError("cpu_profile_requires_libx264")
            def runner(command, **kwargs):
                return subprocess.run(cpu_thread_command(command) if self.config["backend"] == "cpu" else command, **kwargs)
            processor = TTPostGPUProcessor(config, runner=runner)
        else:
            raise ExecutionError("preparation_executor_not_configured")
        self.processors[kind] = processor
        return processor

    def _validate_frozen_catalog(self, kind, payload):
        required = {value for value in payload.get("required_capabilities", [])
                    if isinstance(value, str) and value.startswith("catalog:")}
        if not required:
            return  # Existing X descriptors also validate their request manifest.
        if not required.issubset(set(self.capabilities())):
            raise ExecutionError("frozen_catalog_mismatch")
        channel = kind.split("_", 1)[0]
        expected = {value.split(":", 2)[2] for value in required if value.startswith("catalog:" + channel + ":")}
        if not expected:
            return
        name = "x_random" if kind == "x_preflight" else kind
        processor = self._processor(name)
        assets = getattr(processor, "assets", None) or getattr(processor.config, "random_overlay_assets", None)
        actual = assets.get("manifest_sha256") if isinstance(assets, dict) else None
        if len(expected) != 1 or actual not in expected:
            raise ExecutionError("frozen_catalog_mismatch")

    def prepare(self, kind, payload, context):
        self._validate_frozen_catalog(kind, payload)
        context.phase("prepare")
        request = dict(payload.get("request") or {})
        if kind == "x_preflight":
            return self.x_preflight(request, context)
        if kind == "youtube_prepare":
            if request.get("action") == "enqueue":
                return {"status": "ready", "enqueue": True}
            return self.youtube_prepare(request, context, recovery_only=bool(payload.get("recovery_only")))
        processor = self._processor(kind)
        expected_profile = request.get("expected_profile") or request.get("profile")
        if expected_profile and getattr(processor.config, "profile", "") != expected_profile:
            raise ExecutionError("frozen_renderer_profile_mismatch")
        result = self._prepare_processor(kind, processor, request, context)
        artifact = _artifact(result)
        artifact["backend"] = self.config["backend"]
        # A completed object remains a fact after lease loss. Let the durable
        # outbox deliver it; the authority decides whether this claim can finish.
        return {"status": "ready", "artifact": artifact, "preparation": result}

    def _prepare_processor(self, kind, processor, request, context):
        store = getattr(processor, "cos", None) or getattr(processor, "object_store", None)
        if store is None and callable(getattr(processor, "_object_store", None)):
            store = processor._object_store()
        journal = COSArtifactStore(store, kind, request, source_key=getattr(context, "job", {}).get("source_key", ""))
        context.phase("artifact_recovery")
        recovered = journal.recover()
        if recovered is not None:
            return recovered
        previous = getattr(processor, "qualified_artifact", None)
        processor.qualified_artifact = journal.qualified
        try:
            with self._measure_processor(processor, context):
                result = processor.repair(request) if kind == "x_repair" else processor.prepare(request)
            # Native local manifests created before this protocol are also
            # exported once, after their own object-integrity verification.
            if journal._read() is None:
                artifact = _artifact(result)
                prefix = store.config.cos_domain.rstrip("/") + "/"
                if not artifact["url"].startswith(prefix):
                    raise ExecutionError("artifact_origin_changed")
                from urllib.parse import unquote
                journal.qualified(result, unquote(artifact["url"][len(prefix):]))
            return journal.recover()
        finally:
            processor.qualified_artifact = previous

    def youtube_prepare(self, request, context, *, recovery_only=False):
        from .youtube_cover import generate_cover, image_bytes
        from features.x_posts.media_repair import WorkerConfig, CosObjectStore
        store = self.factories["artifact_store"]() if "artifact_store" in self.factories else CosObjectStore(replace(WorkerConfig.from_env(), profile="youtube-cover-v1"))
        journal = COSArtifactStore(store, "youtube_prepare", request, source_key=getattr(context, "job", {}).get("source_key", ""))
        result = journal.recover()
        if result is not None:
            return result
        if recovery_only:
            raise ExecutionError("youtube_generation_interrupted")
        context.check()
        journal.begin_generation()
        context.check()
        result = generate_cover(request, self.work_root, context, self.factories.get("youtube_prepare"))
        data = image_bytes(result["cover"])
        sha = hashlib.sha256(data).hexdigest()
        key = store.config.cos_prefix.rstrip("/") + "/youtube-cover/" + sha[:2] + "/" + sha + ".jpg"
        result["artifact"] = {"url": store.url(key), "sha256": sha, "size": len(data), "mime": "image/jpeg", "profile": "youtube-cover-v1"}
        journal.qualified(result, key)
        context.phase("artifact_upload")
        with tempfile.TemporaryDirectory(dir=self.work_root) as temp:
            path = Path(temp) / "cover.jpg"
            path.write_bytes(data)
            store.upload(key, path, sha, len(data), content_type="image/jpeg")
        return journal.recover()

    @contextmanager
    def _measure_processor(self, processor, context):
        """Measure native boundaries in this one-job process, with no global patch."""
        replaced = []
        def wrap(owner, name, phase):
            original = getattr(owner, name, None)
            if not callable(original): return
            def measured(*args, **kwargs):
                previous = getattr(context, "phase_name", "prepare")
                selected = phase
                if name == "runner":
                    selected = "render" if args and "ffmpeg" in Path(str(args[0][0])).name.lower() else "probe"
                context.phase(selected)
                try:
                    return original(*args, **kwargs)
                finally:
                    context.phase(previous)
            setattr(owner, name, measured)
            replaced.append((owner, name, original))
        owners = [processor]
        if hasattr(processor, "repair") and not callable(processor.repair): owners.append(processor.repair)
        try:
            stores = []
            for owner in owners:
                for name in ("runner", "downloader", "_download"):
                    wrap(owner, name, "download")
                for name in ("cos", "object_store"):
                    store = getattr(owner, name, None)
                    if store is not None and not callable(store): stores.append(store)
                if callable(getattr(owner, "_object_store", None)):
                    stores.append(owner._object_store())
            seen = set()
            for store in stores:
                if id(store) not in seen:
                    wrap(store, "upload", "artifact_upload")
                    seen.add(id(store))
            yield
        finally:
            for owner, name, original in reversed(replaced): setattr(owner, name, original)

    def x_preflight(self, request, context):
        from features.x_posts.media_repair import WorkerConfig, CosObjectStore, REPAIRABLE_TRIGGER_CODES
        from features.x_posts import service as posts
        from features.drama_synthesis.youtube import YouTubeHTTPClient
        from PIL import Image
        if request.get("prepared_artifact"):
            context.check()
            return {"status": "ready", "artifact": _artifact(request["prepared_artifact"])}
        if request.get("identity_mode") != "source_to_fingerprint":
            raise ExecutionError("explicit_source_preflight_required")
        hosts = tuple(filter(None, os.environ.get("PIPELINE_MEDIA_ALLOWED_HOSTS", "").split(",")))
        if not hosts:
            raise ExecutionError("node_media_allowlist_missing")
        with tempfile.TemporaryDirectory(dir=self.work_root) as temp:
            media = Path(temp) / "source"
            context.phase("download")
            YouTubeHTTPClient().download(request["source_url"], media, allowed_hosts=hosts, max_bytes=min(int(request.get("max_media_bytes", request.get("max_bytes", 16 * 1024**3))), 16 * 1024**3), heartbeat=context.check)
            digest = hashlib.sha256()
            with media.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            context.phase("probe")
            probe = {}
            try:
                with Image.open(media) as picture:
                    image_format, width, height = picture.format, picture.width, picture.height
                    frames = getattr(picture, "n_frames", 1)
                    picture.verify()
                mime = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif"}[image_format]
                kind, duration = "gif" if image_format == "GIF" else "image", 0
                probe = {"width": width, "height": height, "frames": frames, "image_format": image_format}
            except (OSError, KeyError):
                command = [os.environ.get("PIPELINE_FFPROBE_BIN", "ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(media)]
                result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=True)
                probe = json.loads(result.stdout)
                videos = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
                if not videos:
                    raise ExecutionError("x_source_video_missing")
                kind, mime = "video", "video/mp4"
                duration = float(probe.get("format", {}).get("duration") or 0)
                width, height = int(videos[0].get("width") or 0), int(videos[0].get("height") or 0)
                if duration <= 0:
                    raise ExecutionError("x_source_duration_invalid")
            expected_kind = request.get("media_kind")
            if expected_kind and expected_kind != kind:
                raise ExecutionError("x_source_kind_changed")
            sha, size = digest.hexdigest(), media.stat().st_size
            if (request.get("source_sha256") and request["source_sha256"] != sha) or (request.get("source_size") and request["source_size"] != size):
                raise ExecutionError("x_source_identity_changed")
            limit = int(request.get("max_media_bytes", 16 * 1024**3))
            duration_limit = float(request.get("max_duration_seconds", 14400))
            repaired = None
            def validate(path):
                context.phase("probe")
                return posts.probe_media(path, max_bytes=limit, max_duration_seconds=duration_limit) if kind == "video" else posts.probe_image(path, mime, max_bytes=limit)
            try:
                probe = validate(media)
            except posts.XPostError as exc:
                if kind != "video" or exc.code not in REPAIRABLE_TRIGGER_CODES or not request.get("repair_request"):
                    raise
                repair_request = dict(request["repair_request"])
                repair_request.update(source_url=request["source_url"], source_sha256=sha, source_size=size, trigger_code=exc.code)
                processor = self._processor("x_repair")
                if repair_request.get("profile") != processor.config.profile:
                    raise ExecutionError("frozen_renderer_profile_mismatch")
                repaired = self._prepare_processor("x_repair", processor, repair_request, context)
                artifact = _artifact(repaired)
                repaired_path = self._download(artifact, Path(temp) / "repaired.mp4", context)
                probe = validate(repaired_path)
            if repaired is None:
                config = WorkerConfig.from_env()
                store = CosObjectStore(config)
                extension = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "video/mp4": "mp4"}[mime]
                key = config.cos_prefix.rstrip("/") + "/source/" + sha[:2] + "/" + sha + "." + extension
                context.phase("artifact_upload")
                store.upload(key, media, sha, size, content_type=mime)
                artifact = {"url": store.url(key), "sha256": sha, "size": size, "profile": "source"}
            context.check()
            artifact.update(mime=mime, media_kind=kind, duration_seconds=probe.get("duration", 0), width=probe.get("width", width), height=probe.get("height", height))
            checkpoint = {"artifact": artifact, "source_url": request["source_url"], "source_sha256": sha, "source_size": size}
            if repaired is not None:
                checkpoint["repair"] = repaired
                checkpoint["repair_request"] = repair_request
            context.checkpoint("x_preflight", checkpoint)
            if request.get("random_request"):
                frozen = dict(request["random_request"])
                frozen.update(source_url=artifact["url"], source_sha256=artifact["sha256"], source_size=artifact["size"], source_duration=artifact["duration_seconds"])
                processor = self._processor("x_random")
                prepared = self._prepare_processor("x_random", processor, frozen, context)
                return {"status": "ready", "artifact": _artifact(prepared), "preparation": prepared}
            return {"status": "ready", **checkpoint, "probe": probe}

    def _download(self, artifact, target, context):
        from features.drama_synthesis.youtube import YouTubeHTTPClient
        artifact = _artifact(artifact)
        hosts = tuple(filter(None, os.environ.get("PIPELINE_MEDIA_ALLOWED_HOSTS", "").split(",")))
        if not hosts:
            raise ExecutionError("node_media_allowlist_missing")
        context.phase("download")
        YouTubeHTTPClient().download(artifact["url"], target, allowed_hosts=hosts, max_bytes=artifact["size"], heartbeat=context.check)
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for data in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(data)
        if target.stat().st_size != artifact["size"] or digest.hexdigest() != artifact["sha256"]:
            raise ExecutionError("artifact_fingerprint_changed")
        return target

    def x_publish(self, payload, artifact, context):
        from features.x_posts.service import XApiClient
        artifact = _artifact(artifact)
        request = dict(payload.get("request") or {})
        account = payload.get("source_account_resource") or payload["account_resource"]
        class BoundXClient(XApiClient):
            def _request(self, method, path, access_token, body=None, content_type=None, expected=200, operation="X API", unknown=False):
                fingerprint = {"method": method, "path": path, "body_sha256": hashlib.sha256(body or b"").hexdigest()}
                stage = "create_post" if path == "/2/tweets" else ("platform_processing" if method == "GET" else "platform_upload") if "/media/" in path else "repost"
                context.phase(stage)
                with context.operation("x:" + method + ":" + path.split("?")[0], fingerprint, account_resource=account, write=method != "GET") as credential:
                    response = super()._request(method, path, credential["access_token"], body, content_type, expected, operation, unknown)
                if path == "/2/media/upload/initialize":
                    initialized = response.get("data", {})
                    context.checkpoint("x_media_initialized", {"media_id": initialized.get("id") or initialized.get("media_id"), "operation_id": context.last_operation_id})
                return response
        client = self.platform_clients.get("x_publish") or BoundXClient()
        with tempfile.TemporaryDirectory(dir=self.work_root) as temp:
            media = self._download(artifact, Path(temp) / "media.bin", context)
            uploaded = client.upload_media("broker", media, media_type=artifact["mime"], media_category=request.get("media_category", "tweet_video"))
            context.checkpoint("x_media_uploaded", {"media_id": uploaded["media_id"], "artifact_sha256": artifact["sha256"]})
            created = client.create_post("broker", request["body"], uploaded["media_id"])
            result = {"status": "published", "post_id": created["post_id"], "media_id": uploaded["media_id"]}
            context.checkpoint("x_source_published", result)
            if request.get("delivery_mode") == "premium_relay_repost":
                account = payload["account_resource"]
                target = request["target_x_user_id"]
                repost = client.repost("broker", target, created["post_id"])
                result.update(repost_id=repost.get("repost_id", ""), delivery_mode="premium_relay_repost")
                context.checkpoint("x_target_reposted", result)
            return result

    def x_share_publish(self, payload, artifact, context):
        from features.x_posts.service import UrllibHttpClient, X_API_BASE_URL, MAX_HTTP_RESPONSE_BYTES
        request = payload.get("request") or {}
        body = json.dumps({"text": request["text"]}, ensure_ascii=False, separators=(",", ":")).encode()
        fingerprint = {"method": "POST", "path": "/2/tweets", "body_sha256": hashlib.sha256(body).hexdigest()}
        transport = self.platform_clients.get("x_share_publish") or UrllibHttpClient()
        context.phase("create_post")
        with context.operation("x.share.create", fingerprint) as credential:
            try:
                response = transport.request("POST", X_API_BASE_URL + "/2/tweets", headers={"Authorization": "Bearer " + credential["access_token"], "Accept": "application/json", "Content-Type": "application/json"}, body=body, timeout=30, stream=False, max_response_bytes=MAX_HTTP_RESPONSE_BYTES)
            except Exception:
                raise ExecutionError("x_share_outcome_unknown", unknown=True) from None
        try:
            if 400 <= response.status < 500 and response.status != 408:
                context.checkpoint("x_share_rejected", {"http_status": response.status})
                result = {"status": "failed", "http_status": response.status}
                if response.status == 429:
                    result.update(rate_limit_scope="account", account_resource=payload.get("account_resource", ""))
                    try:
                        result["retry_after_seconds"] = max(1, min(86400, int(response.headers.get("retry-after"))))
                    except (TypeError, ValueError):
                        pass
                return result
            if response.status != 201:
                raise ExecutionError("x_share_outcome_unknown", unknown=True)
            try:
                post_id = str(json.loads(response.body).get("data", {}).get("id") or "")
            except (TypeError, ValueError, AttributeError):
                post_id = ""
            if not re.fullmatch(r"[0-9]{1,32}", post_id):
                raise ExecutionError("x_share_outcome_unknown", unknown=True)
            receipt = {"post_id": post_id, "http_status": response.status}
            context.checkpoint("x_share_published", receipt)
            return {"status": "published", **receipt}
        finally:
            response.close()

    def fb_publish(self, payload, artifact, context):
        from features.fb_auto_posts.publisher import RequestsGraphTransport
        from features.fb_auto_posts.repositories import PageCredential
        request = dict(payload.get("request") or {})
        artifact = dict(artifact or {}) if (artifact or {}).get("identity_mode") == "remote_url" and payload.get("processing_mode") == "original" else _artifact(artifact)
        if artifact.get("url") != request.get("media_url"):
            raise ExecutionError("fb_media_identity_changed")
        graph = self.platform_clients.get("fb_publish") or RequestsGraphTransport()
        previous = None
        attempted = set()
        while True:
            operation = {**request, "media_url": artifact["url"]}
            if previous:
                operation["previous_receipt"] = previous
            try:
                with context.operation("fb_video_publish", operation) as credential:
                    identity = str(credential.get("credential_id") or "")
                    if not identity or identity in attempted:
                        raise ExecutionError("fb_credential_rotation_conflict")
                    attempted.add(identity)
                    page_credential = PageCredential(str(request["page_id"]), str(credential.get("fb_user_id") or ""), identity, credential["access_token"])
                    context.phase("publish")
                    result = asdict(graph.publish_video(str(request["page_id"]), page_credential, artifact["url"], request["message"], title=request.get("title", "")))
            except CoordinatorError as exc:
                # Exhaustion is authoritative only when at least one explicit
                # rejection was recorded. Transport failure remains unresolved.
                if previous and exc.code in {"credentials_exhausted", "fb_credentials_exhausted"}:
                    return {**previous, "status": "failed"}
                raise
            result["operation_id"] = context.last_operation_id
            context.checkpoint("fb_publish_attempt", result)
            if result.get("post_id") or result["kind"] in {"success", "unknown"}:
                result["status"] = "published" if result.get("post_id") else "unknown"
                return result
            previous = result

    def tt_publish(self, payload, artifact, context):
        from features.tt_gpu.worker import TikTokContentPostingAPI
        artifact = _artifact(artifact)
        request = dict(payload.get("request") or {})
        origin = str(request.get("verified_origin") or "").rstrip("/")
        parsed = urlsplit(artifact["url"])
        if not origin or "https://" + parsed.netloc != origin:
            raise ExecutionError("tt_publish_url_property_mismatch")
        api = self.platform_clients.get("tt_publish") or TikTokContentPostingAPI()
        publish_id = str(request.get("publish_id") or "")
        if not publish_id:
            context.phase("publish_initialize")
            with context.operation("tt_initialize_video", {"post_info": request["post_info"], "video_url": artifact["url"]}) as credential:
                initialized = api.initialize_video(credential["access_token"], request["post_info"], artifact["url"])
            publish_id = initialized["publish_id"]
            context.checkpoint("tt_initialized", initialized)
        deadline = time.monotonic() + 9000
        while time.monotonic() < deadline:
            context.phase("platform_processing")
            with context.operation("tt_fetch_status", {"publish_id": publish_id}, write=False) as credential:
                result = api.fetch_status(credential["access_token"], publish_id)
            result["publish_id"] = publish_id
            context.checkpoint("tt_status", result)
            if result["status"] in {"PUBLISH_COMPLETE", "FAILED"}:
                return result
            time.sleep(15)
        raise ExecutionError("tt_processing_wait_expired", unknown=True)

    def youtube_publish(self, payload, artifact, context):
        from features.youtube_auto_publish.engine import ReviewedYouTubeHTTPClient
        from features.drama_synthesis.youtube import YouTubeHTTPClient
        from .youtube_cover import image_bytes
        request = dict(payload.get("request") or {})
        legacy = payload.get("mode") == "legacy"
        api = self.platform_clients.get("youtube_publish") or (YouTubeHTTPClient() if legacy else ReviewedYouTubeHTTPClient())
        if payload.get("mode") == "continuation":
            video_id = str(request.get("video_id") or "")
            if not video_id:
                raise ExecutionError("youtube_continuation_video_missing")
            context.phase("platform_processing")
            with context.operation("youtube_video_state", {"video_id": video_id, "channel_id": request["channel_id"]}, write=False) as credential:
                confirmed = api.read_reviewed_video_state(credential["access_token"], video_id, expected_channel_id=request["channel_id"])
            control = request.get("schedule_control")
            rejected = False
            if control and confirmed.get("visibility") == "private" and confirmed.get("state") == "succeeded":
                action = control["action"]
                remote = confirmed.get("preserved_status", {}).get("publishAt") or ""
                already = (action == "cancel" and not remote) or (action == "reschedule" and remote == control["publish_at"])
                if not already:
                    with context.operation("youtube_schedule_control", {"video_id": video_id, "control": control, "preserved_status": confirmed["preserved_status"]}) as credential:
                        try:
                            if action == "cancel": api.cancel_video_schedule(credential["access_token"], video_id=video_id, preserved_status=confirmed["preserved_status"])
                            elif action == "immediate": api.make_video_public(credential["access_token"], video_id=video_id, preserved_status=confirmed["preserved_status"])
                            elif action == "reschedule": api.set_video_schedule(credential["access_token"], video_id=video_id, publish_at=control["publish_at"], preserved_status=confirmed["preserved_status"])
                            else: raise ExecutionError("youtube_schedule_control_invalid")
                        except Exception as exc:
                            rejected = getattr(exc, "unknown", True) is False
                            # Even an ambiguous PUT receives one owner readback;
                            # there is never a second visibility write.
                    with context.operation("youtube_video_state", {"video_id": video_id, "channel_id": request["channel_id"]}, write=False) as credential:
                        confirmed = api.read_reviewed_video_state(credential["access_token"], video_id, expected_channel_id=request["channel_id"])
            public = confirmed.get("visibility") == "public"
            cancelled = bool(control and control["action"] == "cancel" and confirmed.get("visibility") == "private" and not confirmed.get("preserved_status", {}).get("publishAt"))
            if confirmed.get("state") != "succeeded" or (not public and not cancelled and not rejected and (confirmed.get("visibility") != "private" or confirmed.get("preserved_status", {}).get("publishAt") != request["publish_at"])):
                raise ExecutionError("youtube_schedule_unconfirmed", unknown=True)
            context.checkpoint("youtube_schedule_readback", {"video_id": video_id, "confirmed": confirmed, "command_rejected": rejected})
            comment_id = str(request.get("comment_id") or "")
            if public and request.get("comment_text") and not comment_id:
                context.phase("comment")
                with context.operation("youtube_comment", {"video_id": video_id, "channel_id": request["channel_id"], "text_sha256": hashlib.sha256(request["comment_text"].encode()).hexdigest()}) as credential:
                    comment_id = api.publish_comment(credential["access_token"], video_id=video_id, comment_text=request["comment_text"], channel_id=request["channel_id"])
                context.checkpoint("youtube_comment", {"video_id": video_id, "comment_id": comment_id})
            return {"video_id": video_id, "comment_id": comment_id, "publish_at": request["publish_at"], "status": "published" if public else "cancelled" if cancelled else "scheduled"}
        artifact = dict(artifact or {})
        if artifact.get("identity_mode") != "source_to_fingerprint":
            artifact = _artifact(artifact)
        video_id, session_uri = str(request.get("video_id") or ""), str(request.get("session_uri") or "")
        cover = request.get("approved_cover")
        with tempfile.TemporaryDirectory(dir=self.work_root) as temp:
            root = Path(temp)
            if not legacy: (root / "cover").write_bytes(image_bytes(cover))
            if not video_id:
                if artifact.get("identity_mode") == "source_to_fingerprint":
                    media = root / "video.mp4"
                    hosts = tuple(filter(None, os.environ.get("PIPELINE_MEDIA_ALLOWED_HOSTS", "").split(",")))
                    if not hosts:
                        raise ExecutionError("node_media_allowlist_missing")
                    context.phase("download")
                    api.download(artifact["url"], media, allowed_hosts=hosts, max_bytes=16 * 1024**3, heartbeat=context.check)
                    digest = hashlib.sha256()
                    with media.open("rb") as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(chunk)
                    probe = subprocess.run([os.environ.get("PIPELINE_FFPROBE_BIN", "ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(media)], capture_output=True, text=True, timeout=60, check=True)
                    media_info = json.loads(probe.stdout)
                    if not any(stream.get("codec_type") == "video" for stream in media_info.get("streams", [])):
                        raise ExecutionError("youtube_source_video_missing")
                    artifact.update(sha256=digest.hexdigest(), size=media.stat().st_size, duration_ms=int(float(media_info["format"]["duration"]) * 1000))
                    if artifact["duration_ms"] <= 0:
                        raise ExecutionError("youtube_source_duration_invalid")
                    context.checkpoint("youtube_source", {key: artifact[key] for key in ("url", "sha256", "size", "duration_ms")})
                else:
                    media = self._download(artifact, root / "video.mp4", context)
                if not session_uri:
                    context.phase("upload_initialize")
                    privacy = "public" if legacy else "private"
                    with context.operation("youtube_begin_resumable", {"title": request["title"], "description": request["description"], "size": artifact["size"], "privacy_status": privacy}) as credential:
                        session_uri = api.begin_resumable(credential["access_token"], title=request["title"], description=request["description"], size=artifact["size"], privacy_status=privacy)
                    self._validate_youtube_session(session_uri)
                    context.checkpoint("youtube_session", {"session_uri": session_uri, "size": artifact["size"]})
                self._validate_youtube_session(session_uri)
                context.phase("platform_upload")
                with context.operation("youtube_upload", {"session_uri_sha256": hashlib.sha256(session_uri.encode()).hexdigest(), "source_sha256": artifact["sha256"], "offset": int(request.get("next_byte", 0))}):
                    result = api.upload(session_uri, media, int(request.get("next_byte", 0)))
                context.checkpoint("youtube_upload", result)
                if result.get("state") != "submitted" or not result.get("video_id"):
                    raise ExecutionError("youtube_upload_reconcile_required", unknown=True)
                video_id = result["video_id"]
            if legacy:
                deadline = time.monotonic() + 9000
                while True:
                    context.phase("platform_processing")
                    with context.operation("youtube_video_state", {"video_id": video_id, "channel_id": request["channel_id"]}, write=False) as credential:
                        state = api.video_status(credential["access_token"], video_id)
                    context.checkpoint("youtube_legacy_status", {"video_id": video_id, **state})
                    if state.get("state") == "published" and state.get("visibility") == "public": break
                    if state.get("state") != "processing" or time.monotonic() >= deadline:
                        raise ExecutionError("youtube_publication_not_confirmed", unknown=True)
                    time.sleep(15)
                comment_id = str(request.get("comment_id") or "")
                if request.get("comment_text") and not comment_id:
                    context.phase("comment")
                    with context.operation("youtube_comment", {"video_id": video_id, "channel_id": request["channel_id"], "text_sha256": hashlib.sha256(request["comment_text"].encode()).hexdigest()}) as credential:
                        comment_id = api.publish_comment(credential["access_token"], video_id=video_id, comment_text=request["comment_text"], channel_id=request["channel_id"])
                    context.checkpoint("youtube_comment", {"video_id": video_id, "comment_id": comment_id})
                return {"video_id": video_id, "comment_id": comment_id, "status": "published"}
            context.phase("thumbnail")
            with context.operation("youtube_thumbnail", {"video_id": video_id, "sha256": cover["sha256"]}) as credential:
                api.set_thumbnail(credential["access_token"], video_id=video_id, content=(root / "cover").read_bytes(), mime_type=cover["mime"])
            context.checkpoint("youtube_thumbnail", {"video_id": video_id, "sha256": cover["sha256"], "status": "set"})
            deadline = time.monotonic() + 9000
            while True:
                context.phase("platform_processing")
                with context.operation("youtube_video_state", {"video_id": video_id, "channel_id": request["channel_id"]}, write=False) as credential:
                    state = api.read_reviewed_video_state(credential["access_token"], video_id, expected_channel_id=request["channel_id"])
                context.checkpoint("youtube_processing", {"video_id": video_id, **state})
                if state["state"] == "succeeded":
                    break
                if state["state"] == "failed" or time.monotonic() >= deadline:
                    raise ExecutionError("youtube_processing_failed" if state["state"] == "failed" else "youtube_processing_wait_expired", unknown=True)
                time.sleep(15)
            context.phase("visibility")
            publish_at = str(request.get("publish_at") or "")
            with context.operation("youtube_visibility", {"video_id": video_id, "publish_at": publish_at, "preserved_status": state["preserved_status"]}) as credential:
                if publish_at:
                    api.set_video_schedule(credential["access_token"], video_id=video_id, publish_at=publish_at, preserved_status=state["preserved_status"])
                else:
                    api.make_video_public(credential["access_token"], video_id=video_id, preserved_status=state["preserved_status"])
            # A successful PUT alone is not publication proof. Confirm the same
            # owned video; an unknown PUT never triggers a second visibility write.
            context.phase("visibility_verify")
            with context.operation("youtube_video_state", {"video_id": video_id, "channel_id": request["channel_id"]}, write=False) as credential:
                confirmed = api.read_reviewed_video_state(credential["access_token"], video_id, expected_channel_id=request["channel_id"])
            expected_visibility = "private" if publish_at else "public"
            if confirmed.get("visibility") != expected_visibility or (publish_at and confirmed.get("preserved_status", {}).get("publishAt") != publish_at):
                raise ExecutionError("youtube_visibility_unconfirmed", unknown=True)
            context.checkpoint("youtube_visibility", {"video_id": video_id, "publish_at": publish_at, "status": "scheduled" if publish_at else "public", "confirmed": confirmed})
            comment_id = str(request.get("comment_id") or "")
            if request.get("comment_text") and not comment_id and not publish_at:
                with context.operation("youtube_comment", {"video_id": video_id, "channel_id": request["channel_id"], "text_sha256": hashlib.sha256(request["comment_text"].encode()).hexdigest()}) as credential:
                    comment_id = api.publish_comment(credential["access_token"], video_id=video_id, comment_text=request["comment_text"], channel_id=request["channel_id"])
                context.checkpoint("youtube_comment", {"video_id": video_id, "comment_id": comment_id})
            return {"video_id": video_id, "comment_id": comment_id, "publish_at": publish_at, "status": "scheduled" if publish_at else "published"}

    @staticmethod
    def _validate_youtube_session(uri):
        parsed = urlsplit(uri)
        if parsed.scheme != "https" or parsed.hostname not in {"www.googleapis.com", "youtube.googleapis.com"} or parsed.username or parsed.password or parsed.fragment:
            raise ExecutionError("youtube_session_origin_invalid", unknown=True)
