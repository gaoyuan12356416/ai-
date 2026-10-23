"""Editing adapter for the existing single-slot HK media runtime."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import time

from features.drama_synthesis import async_runtime as runtime
from features.drama_synthesis.cos_upload import _head as authenticated_head
from features.drama_synthesis.local_checkpoint import atomic_write_record, read_record, load_completed, save_completed, file_fingerprint
from . import recipe as contract
from .media import checked_workdir, remove_workdir, upload_private, verified_source_download, verify_receipt
from .render import build_normalize_command, build_assemble_command, assembly_layers, probe_media, run_ffmpeg, validate_output


SAFE_ERRORS = {
    "video_editing_recipe_invalid": "剪辑参数无效，请检查片段范围和过渡",
    "video_editing_source_identity_missing": "素材缺少可验证的版本信息",
    "video_editing_source_forbidden": "视频来源不在允许范围内",
    "video_editing_source_changed": "素材版本已变化，请重新选择素材",
    "video_editing_probe_failed": "无法读取视频信息",
    "video_editing_clip_too_short": "保留片段必须至少包含一帧",
    "video_editing_transition_too_long": "片段长度不足以容纳0.3秒过渡",
    "video_editing_last_transition": "最后一个片段不能设置过渡",
    "video_editing_duration_limit": "成片时长超出剪辑参数可表示范围",
    "video_editing_output_invalid": "成片规格或音画时长校验失败",
    "video_editing_encoder_invalid": "剪辑编码器配置无效",
    "video_editing_path_invalid": "剪辑临时目录不安全",
    "video_editing_cleanup_failed": "剪辑临时文件清理失败",
    "video_editing_storage_unavailable": "剪辑COS存储尚未配置",
    "video_editing_disk_budget": "制作节点临时空间不足，任务未开始制作",
    "video_editing_disabled": "视频剪辑制作入口暂未开启",
}
# Additive, fixed messages only. Legacy error codes/messages remain untouched.
runtime.ERROR_MESSAGES.update(SAFE_ERRORS)


def task_kind(payload):
    if not isinstance(payload, dict):
        raise runtime.runtime_error("invalid_request", 400)
    kind = payload.get("task_type", "drama")
    if kind == contract.TASK_TYPE:
        if not contract.JOB_RE.fullmatch(str(payload.get("job_id", ""))):
            raise runtime.runtime_error("invalid_job_id", 400)
        return kind
    if kind not in (None, "", "drama") or str(payload.get("job_id", "")).startswith("ve_"):
        raise runtime.runtime_error("invalid_request", 400)
    return "drama"


def mixed_validate(payload):
    return contract.validate_payload(payload) if task_kind(payload) == contract.TASK_TYPE else runtime.validate_render_payload(payload)


def mixed_fingerprint(payload):
    return contract.payload_fingerprint(payload) if task_kind(payload) == contract.TASK_TYPE else runtime.render_fingerprint(payload)


def _artifact(value, *, uploaded=True):
    if not isinstance(value, dict):
        raise runtime.runtime_error("gpu_result_cache_unverified")
    fields = ("bucket", "key", "sha256", "size_bytes", "etag", "binding", "duration_ms", "width", "height",
              "fps", "has_audio", "video_codec", "audio_codec", "audio_duration_ms", "format_name", "container_brand",
              "pixel_format", "sample_aspect_ratio")
    result = {k: deepcopy(value[k]) for k in fields if k in value}
    if (not isinstance(result.get("bucket"), str) or not result["bucket"] or not isinstance(result.get("key"), str)
            or not result["key"] or not contract.SHA_RE.fullmatch(str(result.get("sha256", "")))
            or type(result.get("size_bytes")) is not int or result["size_bytes"] <= 0
            or (uploaded and not re.fullmatch(r"[0-9a-f]{32}", str(result.get("binding", ""))))):
        raise runtime.runtime_error("gpu_result_cache_unverified")
    return result


def public_result(value, payload):
    if task_kind(payload) == "drama":
        return None  # AsyncRuntime uses its original projection unchanged.
    expected = contract.recipe_fingerprint(payload["recipe"], payload["operation"])
    if (not isinstance(value, dict) or value.get("job_id") != payload["job_id"]
            or value.get("task_type") != contract.TASK_TYPE or value.get("operation") != payload["operation"]
            or value.get("recipe_sha256") != expected):
        raise runtime.runtime_error("gpu_result_cache_unverified")
    result = {k: value[k] for k in ("job_id", "task_type", "operation", "recipe_sha256")}
    if payload["operation"] == "render":
        result["artifact"] = _artifact(value.get("artifact"))
    else:
        identity = value.get("source_identity")
        if not isinstance(identity, dict) or not contract.SHA_RE.fullmatch(str(identity.get("sha256", ""))):
            raise runtime.runtime_error("gpu_result_cache_unverified")
        result["source_identity"] = contract.normalize_identity(identity, kind=payload["recipe"]["sources"][0]["kind"])
        metadata = value.get("metadata")
        if not isinstance(metadata, dict) or type(metadata.get("duration_ms")) is not int or metadata["duration_ms"] <= 0:
            raise runtime.runtime_error("gpu_result_cache_unverified")
        result["metadata"] = {k: metadata[k] for k in ("duration_ms", "width", "height", "fps", "has_audio", "video_codec", "audio_codec", "audio_duration_ms",
                                                      "format_name", "container_brand", "pixel_format", "sample_aspect_ratio") if k in metadata}
        if value.get("artifact"):
            result["artifact"] = _artifact(value["artifact"], uploaded=False)
        result["thumbnail"] = _artifact(value.get("thumbnail"))
        if value.get("preview"):
            result["preview"] = _artifact(value["preview"])
    return result


@dataclass(frozen=True)
class Config:
    root: Path
    state_root: Path
    bucket: str
    region: str
    prefix: str = "video-editing/v1"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    encoder: str = "h264_nvenc"
    allowed_hosts: tuple = ()
    reserve_bytes: int = 20 * 1024**3


class EditingService:
    def __init__(self, config, client_factory, render_slots=None):
        self.config, self.client_factory = config, client_factory
        self.render_slots = render_slots

    def _client(self):
        if not self.config.bucket or not self.config.region:
            raise contract.error("video_editing_storage_unavailable", SAFE_ERRORS["video_editing_storage_unavailable"], 503)
        return self.client_factory()

    def _state(self, job_id):
        if not contract.JOB_RE.fullmatch(job_id):
            raise runtime.runtime_error("invalid_job_id", 400)
        path = Path(self.config.state_root).absolute() / job_id
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise runtime.runtime_error("gpu_runtime_unverified")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    def cached(self, payload):
        value = contract.validate_payload(payload)
        state = self._state(value["job_id"])
        manifest = read_record(state / "result.json")
        if manifest is None:
            return None
        if manifest.get("fingerprint") != contract.payload_fingerprint(value):
            raise runtime.runtime_error("gpu_job_input_conflict", 409)
        result = public_result(manifest.get("result"), value)
        client = self._client()
        for name in ("artifact", "thumbnail", "preview"):
            if result.get(name):
                verify_receipt(client, result[name], source=(value["operation"] == "probe" and name == "artifact"))
        # A prior cleanup error must not strand media forever after a durable
        # COS result. Recheck the remote receipt before retrying local cleanup.
        remaining = Path(self.config.root) / value["job_id"]
        if remaining.exists():
            remove_workdir(self.config.root, value["job_id"])
        return result

    def can_resume(self, payload):
        value = contract.validate_payload(payload)
        state = self._state(value["job_id"])
        try:
            expiry, epoch, consumed = self._recovery_state(state, contract.payload_fingerprint(value))
            if expiry is not None and not consumed:
                self._verify_unused_expiry(value, state, self._client(), expiry, epoch)
            return True
        except Exception:
            # Unknown storage, malformed evidence and legacy unbound markers
            # are never permission to recreate media or create another upload.
            return False

    @staticmethod
    def _resume_error():
        return runtime.runtime_error("gpu_job_resume_unavailable", 409)

    def _expiry(self, path, fingerprint):
        value = read_record(path)
        if value is None:
            return None
        before = file_fingerprint(path)
        value = read_record(path)
        if (before != file_fingerprint(path)
                or set(value) != {"version", "fingerprint", "expired_generation", "expired_at", "reason"}
                or type(value["version"]) is not int or value["version"] != 1
                or value["fingerprint"] != fingerprint
                or type(value["expired_generation"]) is not int or value["expired_generation"] < 1
                or type(value["expired_at"]) not in (int, float) or not math.isfinite(value["expired_at"])
                or value["expired_at"] <= 0 or value["reason"] != "failed_media_retention"):
            raise self._resume_error()
        return value, before["sha256"]

    def _epoch_directory(self, state, epoch):
        path = state / epoch["local_checkpoint_dir"]
        if any(item.is_symlink() for item in (path, *path.parents)) or (path.exists() and not path.is_dir()):
            raise self._resume_error()
        return path

    def _recovery_state(self, state, fingerprint):
        expiry = self._expiry(state / "expired.json", fingerprint)
        epoch = read_record(state / "scratch_epoch.json")
        if epoch is None:
            return expiry, None, False
        if (expiry is None or set(epoch) != {"version", "fingerprint", "admitted_generation", "consumed_expired_sha256", "local_checkpoint_dir"}
                or type(epoch["version"]) is not int or epoch["version"] != 1
                or epoch["fingerprint"] != fingerprint
                or type(epoch["admitted_generation"]) is not int or epoch["admitted_generation"] < 1
                or not contract.SHA_RE.fullmatch(str(epoch["consumed_expired_sha256"]))
                or epoch["local_checkpoint_dir"] != "local-attempts/g%d" % epoch["admitted_generation"]):
            raise self._resume_error()
        self._epoch_directory(state, epoch)
        same = epoch["consumed_expired_sha256"] == expiry[1]
        if same:
            if epoch["admitted_generation"] <= expiry[0]["expired_generation"]:
                raise self._resume_error()
        else:
            # A second retention pass must have archived the exact marker that
            # admitted the previous epoch before replacing the current marker.
            history = state / "expiry-history" / (epoch["consumed_expired_sha256"] + ".json")
            if any(item.is_symlink() for item in (history, *history.parents)):
                raise self._resume_error()
            previous = self._expiry(history, fingerprint)
            if (previous is None or previous[1] != epoch["consumed_expired_sha256"]
                    or previous[0]["expired_generation"] >= epoch["admitted_generation"]
                    or epoch["admitted_generation"] > expiry[0]["expired_generation"]):
                raise self._resume_error()
        return expiry, epoch, same

    def _verify_unused_expiry(self, value, state, client, expiry, epoch, *, check_remote=True):
        """Require positive absence of all upload intent and all output objects."""
        fingerprint = contract.payload_fingerprint(value)
        work = Path(self.config.root).absolute() / value["job_id"]
        if any(item.is_symlink() for item in (work, *work.parents)) or work.exists():
            raise self._resume_error()  # Retention deletion was not confirmed.
        local_name = re.compile(r"(?:thumbnail|preview|clip-[0-9]{4}|join-[0-9]{2}-[0-9]{4})\.local\.json(?:\.failure\.json)?")
        uploads = ("material.mp4",) if value["operation"] == "render" else ("thumbnail.jpg", "preview.mp4")
        locks = {name + ".upload.json.lock" for name in uploads}
        for path in state.rglob("*"):
            relative = path.relative_to(state).as_posix()
            if path.is_symlink():
                raise self._resume_error()
            if path.is_dir():
                if relative not in {"local-attempts", "expiry-history"} and not re.fullmatch(r"local-attempts/g[1-9][0-9]*", relative):
                    raise self._resume_error()
                if relative.startswith("local-attempts/g") and (epoch is None or int(path.name[1:]) > epoch["admitted_generation"]):
                    raise self._resume_error()  # No unreferenced future attempt may become the new epoch.
                continue
            if not path.is_file():
                raise self._resume_error()
            if relative in {"expired.json", "scratch_epoch.json"}:
                continue  # Already checked strictly above.
            if relative in locks and path.stat().st_size == 0:
                continue  # A durable empty flock file is not upload intent.
            record = read_record(path)
            if relative == "input.json":
                if (set(record) != {"fingerprint", "created_at"} or record["fingerprint"] != fingerprint
                        or type(record["created_at"]) not in (int, float) or not math.isfinite(record["created_at"])):
                    raise self._resume_error()
            elif re.fullmatch(r"expiry-history/[0-9a-f]{64}\.json", relative):
                previous = self._expiry(path, fingerprint)
                if previous[1] != path.stem or previous[0]["expired_generation"] >= expiry[0]["expired_generation"]:
                    raise self._resume_error()
            elif local_name.fullmatch(path.name) and (path.parent == state or re.fullmatch(r"local-attempts/g[1-9][0-9]*", path.parent.relative_to(state).as_posix())):
                if path.name.endswith(".failure.json"):
                    context = record.get("context", {})
                    if (record.get("version") != 1 or record.get("kind") != "drama_random_render_failure"
                            or context.get("recipe_sha256") != contract.recipe_fingerprint(value["recipe"], value["operation"])
                            or context.get("job_id", value["job_id"]) != value["job_id"]):
                        raise self._resume_error()
                elif (set(record) != {"version", "identity", "artifact", "result"} or record["version"] != 1
                        or record.get("identity", {}).get("fingerprint") != fingerprint
                        or not isinstance(record.get("result"), dict)
                        or type(record.get("artifact", {}).get("size_bytes")) is not int
                        or record["artifact"]["size_bytes"] <= 0
                        or not contract.SHA_RE.fullmatch(str(record["artifact"].get("sha256", "")))):
                    raise self._resume_error()
            else:
                # Includes every upload checkpoint, hidden atomic-write residue,
                # unknown sidecar and evidence in an unexpected subdirectory.
                raise self._resume_error()
        if check_remote:
            prefix = self.config.prefix.rstrip("/") + "/" + ("outputs" if value["operation"] == "render" else "probes") + "/" + value["job_id"]
            for name in uploads:
                if authenticated_head(client, {"bucket": self.config.bucket, "key": prefix + "/" + name}) is not None:
                    raise self._resume_error()

    def _local_state(self, value, state, client):
        fingerprint = contract.payload_fingerprint(value)
        expiry, epoch, consumed = self._recovery_state(state, fingerprint)
        if expiry is None:
            return state
        context = runtime.capture_context()
        if context is None or context.job_id != value["job_id"] or type(context.generation) is not int:
            raise self._resume_error()
        def require_active():
            active = context.runtime._active(context)
            if active is None or active["fingerprint"] != fingerprint or context.generation <= expiry[0]["expired_generation"]:
                raise self._resume_error()
        with context.runtime._mutex:
            require_active()
            if consumed:
                if context.generation < epoch["admitted_generation"]:
                    raise self._resume_error()
                return self._epoch_directory(state, epoch)
        # Recheck in the executing slot, but never hold the shared runtime
        # mutex over COS I/O: an unavailable HEAD must not block legacy GETs or
        # heartbeats. Revalidate the active owner and all local evidence before
        # committing the new pointer after that network call.
        self._verify_unused_expiry(value, state, client, expiry, epoch)
        with context.runtime._mutex:
            require_active()
            if self._recovery_state(state, fingerprint) != (expiry, epoch, False):
                raise self._resume_error()
            self._verify_unused_expiry(value, state, client, expiry, epoch, check_remote=False)
            epoch = {"version": 1, "fingerprint": fingerprint, "admitted_generation": context.generation,
                "consumed_expired_sha256": expiry[1], "local_checkpoint_dir": "local-attempts/g%d" % context.generation}
            self._epoch_directory(state, epoch)
            atomic_write_record(state / "scratch_epoch.json", epoch)
            return self._epoch_directory(state, epoch)

    def _local_render(self, path, state, identity, command, duration, *, diagnostic_context=None, probe=False):
        completed = load_completed(state, path, identity)
        if completed is not None:
            return
        # Durable, allowlisted runner evidence lives outside the removable media
        # directory. The shared runner stores hashes/tags, never raw stderr,
        # transport URLs, command arguments or credentials.
        # Parallel downloads must not create extra FFmpeg/NVENC render slots.
        slots = self.render_slots if probe else None
        if slots is not None:
            runtime.emit_progress("waiting_media")
            slots.acquire()
        try:
            if probe:
                runtime.emit_progress("normalizing")
            run_ffmpeg(command, duration, diagnostic_path=state.with_name(state.name + ".failure.json"),
                       diagnostic_context=diagnostic_context)
        finally:
            if slots is not None:
                slots.release()
        save_completed(state, path, identity, {})

    def execute(self, payload):
        value = contract.validate_payload(payload)
        cached = self.cached(value)
        if cached is not None:
            return cached
        job_id, recipe = value["job_id"], value["recipe"]
        state, client = self._state(job_id), self._client()
        local_state = self._local_state(value, state, client)
        work = checked_workdir(self.config.root, job_id)
        fingerprint = contract.payload_fingerprint(value)
        duration = sum(c["end_frame"] - c["start_frame"] for c in recipe["clips"]) / 30 if value["operation"] == "render" else 0
        layers = assembly_layers(recipe)[0] if value["operation"] == "render" else []
        budget = sum(s["identity"]["size_bytes"] for s in recipe["sources"]) + int(duration * 850000 * (len(layers) + 1))
        if budget > 64 * 1024**3 or shutil.disk_usage(work).free < budget + self.config.reserve_bytes:
            raise contract.error("video_editing_disk_budget", SAFE_ERRORS["video_editing_disk_budget"], 503)
        atomic_write_record(state / "input.json", {"fingerprint": fingerprint, "created_at": time.time()})
        sources, infos, downloads = {}, {}, {}
        for index, source in enumerate(recipe["sources"]):
            path = work / ("source-%03d.media" % index)
            downloaded = verified_source_download(source, path, client=client, bucket=self.config.bucket,
                allowed_hosts=self.config.allowed_hosts, region=self.config.region)
            runtime.emit_progress("verifying")
            info = probe_media(path, ffprobe=self.config.ffprobe)
            if value["operation"] == "render" and (abs(info["duration_ms"] - source["duration_ms"]) > 100
                    or info["width"] != source["width"] or info["height"] != source["height"] or info["has_audio"] != source["has_audio"]):
                raise contract.error("video_editing_source_changed", SAFE_ERRORS["video_editing_source_changed"], 409)
            sources[source["source_id"]], infos[source["source_id"]], downloads[source["source_id"]] = path, info, downloaded
        result = {"job_id": job_id, "task_type": contract.TASK_TYPE, "operation": value["operation"],
                  "recipe_sha256": contract.recipe_fingerprint(recipe, value["operation"])}
        primary = downloads[recipe["sources"][0]["source_id"]]
        source_set = [{"source_id": s["source_id"], "identity": {**s["identity"], "sha256": downloads[s["source_id"]]["sha256"]}}
                      for s in recipe["sources"]]
        diagnostic_context = {"recipe_sha256": result["recipe_sha256"],
            "source_sha256": primary["sha256"], "source_size_bytes": primary["size_bytes"],
            "asset_set_sha256": hashlib.sha256(contract.canonical_json(source_set).encode()).hexdigest()}
        prefix = self.config.prefix.rstrip("/") + "/" + ("outputs" if value["operation"] == "render" else "probes") + "/" + job_id

        def upload(name, path, metadata=None, content_type="video/mp4"):
            return upload_private(client, bucket=self.config.bucket, key=prefix + "/" + name,
                path=path, checkpoint=state / (name + ".upload.json"), metadata=metadata, content_type=content_type)

        if value["operation"] == "probe":
            source = recipe["sources"][0]
            sid, info = source["source_id"], infos[source["source_id"]]
            result["source_identity"] = {**source["identity"], "sha256": downloads[sid]["sha256"]}
            result["metadata"] = info
            if source["identity"].get("bucket"):
                result["artifact"] = {**result["source_identity"], **info}
            thumb = work / "thumbnail.jpg"
            thumb_cmd = [self.config.ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(sources[sid]),
                         "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "3", str(thumb)]
            self._local_render(thumb, local_state / "thumbnail.local.json", {"fingerprint": fingerprint, "kind": "thumbnail"}, thumb_cmd, 0.04,
                diagnostic_context=diagnostic_context, probe=True)
            result["thumbnail"] = upload("thumbnail.jpg", thumb, content_type="image/jpeg")
            if (info["video_codec"] != "h264" or (info["has_audio"] and info["audio_codec"] != "aac")
                    or "mp4" not in info["format_name"].split(",") or info["container_brand"] == "qt"
                    or info["pixel_format"] != "yuv420p"):
                if shutil.disk_usage(work).free < self.config.reserve_bytes + int(info["duration_ms"] * 850):
                    raise contract.error("video_editing_disk_budget", SAFE_ERRORS["video_editing_disk_budget"], 503)
                preview = work / "preview.mp4"
                clip = {"start_frame": 0, "end_frame": contract.ms_to_frame(info["duration_ms"]), "fit": "contain", "mute": False}
                self._local_render(preview, local_state / "preview.local.json", {"fingerprint": fingerprint, "kind": "preview"},
                    build_normalize_command(sources[sid], clip, preview, has_audio=info["has_audio"],
                        ffmpeg=self.config.ffmpeg, encoder=self.config.encoder), info["duration_ms"] / 1000,
                    diagnostic_context=diagnostic_context, probe=True)
                preview_info = validate_output(probe_media(preview, ffprobe=self.config.ffprobe), clip["end_frame"])
                result["preview"] = upload("preview.mp4", preview, preview_info)
        else:
            segments = {}
            for index, clip in enumerate(recipe["clips"]):
                runtime.emit_progress("normalizing", completed_steps=index, total_steps=len(recipe["clips"]) + 1)
                path = work / ("clip-%04d.mp4" % index)
                count = clip["end_frame"] - clip["start_frame"]
                command = build_normalize_command(sources[clip["source_id"]], clip, path,
                    has_audio=infos[clip["source_id"]]["has_audio"], ffmpeg=self.config.ffmpeg, encoder=self.config.encoder)
                self._local_render(path, local_state / ("clip-%04d.local.json" % index),
                    {"fingerprint": fingerprint, "clip_id": clip["id"]}, command, count / 30,
                    diagnostic_context=diagnostic_context)
                validate_output(probe_media(path, ffprobe=self.config.ffprobe), count)
                segments["clip-%04d" % index] = path
            runtime.emit_progress("concatenating")
            frames = contract.output_frames(recipe)
            layers, final_name = assembly_layers(recipe)
            for layer in layers:
                for group in layer:
                    output = work / (group["name"] + ".mp4")
                    self._local_render(output, local_state / (group["name"] + ".local.json"),
                        {"fingerprint": fingerprint, "kind": group["name"]},
                        build_assemble_command([segments[name] for name in group["inputs"]], group["recipe"], output,
                            ffmpeg=self.config.ffmpeg, encoder=self.config.encoder), group["frames"] / 30,
                        diagnostic_context=diagnostic_context)
                    validate_output(probe_media(output, ffprobe=self.config.ffprobe), group["frames"])
                    segments[group["name"]] = output
            output = segments[final_name]
            output_info = validate_output(probe_media(output, ffprobe=self.config.ffprobe), frames)
            result["artifact"] = upload("material.mp4", output, output_info)
        result = public_result(result, value)
        atomic_write_record(state / "result.json", {"version": 1, "fingerprint": fingerprint, "result": result})
        # Upload helper has authenticated SHA/size/nonce receipts; no public URL or
        # local-serving fallback is possible. Durable result precedes cleanup.
        remove_workdir(self.config.root, job_id)
        return result

    def cleanup_failed(self, shared_runtime, *, now=None, retention_seconds=86400):
        """Expire only terminal failures with positively stopped child processes."""
        now = time.time() if now is None else now
        candidates, cleaned = [], []
        with shared_runtime._mutex:
            for record in shared_runtime._records.values():
                if (record["status"] != "failed" or task_kind(record["_payload"]) != contract.TASK_TYPE
                        or shared_runtime._process_risk(record) is not None):
                    continue
                from datetime import datetime
                stamp = datetime.fromisoformat(record["heartbeat_at"].replace("Z", "+00:00")).timestamp()
                if now - stamp >= retention_seconds:
                    candidates.append(record["job_id"])
            for job_id in candidates:
                state = self._state(job_id)
                if read_record(state / "result.json") is not None:
                    continue
                record = shared_runtime._records[job_id]
                fingerprint = contract.payload_fingerprint(record["_payload"])
                generation = record.get("generation")
                if type(generation) is not int or generation < 1 or record.get("fingerprint") != fingerprint:
                    continue
                try:
                    previous = self._expiry(state / "expired.json", fingerprint)
                except Exception:
                    continue  # Never turn a legacy/corrupt marker into permission.
                if previous is not None and previous[0]["expired_generation"] > generation:
                    continue
                if previous is None or previous[0]["expired_generation"] < generation:
                    if previous is not None:
                        archive = state / "expiry-history" / (previous[1] + ".json")
                        if any(item.is_symlink() for item in (archive, *archive.parents)):
                            raise self._resume_error()
                        saved = self._expiry(archive, fingerprint)
                        if saved is None:
                            atomic_write_record(archive, previous[0])
                        if file_fingerprint(archive)["sha256"] != previous[1]:
                            raise self._resume_error()
                    atomic_write_record(state / "expired.json", {"version": 1, "fingerprint": fingerprint,
                        "expired_generation": generation, "expired_at": now, "reason": "failed_media_retention"})
                remove_workdir(self.config.root, job_id)
                cleaned.append(job_id)
        return cleaned


class RuntimeAdapter:
    """Typed callbacks; a missing legacy task_type retains the original hash."""
    def __init__(self, service, *, legacy_execute, legacy_cached, legacy_sync_cached, legacy_resume):
        self.service = service
        self.legacy_execute, self.legacy_cached = legacy_execute, legacy_cached
        self.legacy_sync_cached, self.legacy_resume = legacy_sync_cached, legacy_resume

    def execute(self, payload):
        return self.service.execute(payload) if task_kind(payload) == contract.TASK_TYPE else self.legacy_execute(payload)

    def cached(self, payload):
        return self.service.cached(payload) if task_kind(payload) == contract.TASK_TYPE else self.legacy_cached(payload)

    def sync_cached(self, payload):
        return self.service.cached(payload) if task_kind(payload) == contract.TASK_TYPE else self.legacy_sync_cached(payload)

    def resume(self, payload):
        return self.service.can_resume(payload) if task_kind(payload) == contract.TASK_TYPE else bool(self.legacy_resume and self.legacy_resume(payload))


def from_app(app, environ=None, render_slots=None):
    env = os.environ if environ is None else environ
    bucket = env.get("VIDEO_EDITING_COS_BUCKET") or getattr(app, "COS_BUCKET", "")
    region = env.get("VIDEO_EDITING_COS_REGION") or getattr(app, "COS_REGION", "")
    config = Config(root=Path(env.get("VIDEO_EDITING_WORK_ROOT", "/data/drama-synthesis-gpu/work/video-editing")),
        state_root=Path(env.get("VIDEO_EDITING_STATE_ROOT", "/data/drama-synthesis-gpu/results/video-editing")),
        bucket=bucket, region=region, prefix=env.get("VIDEO_EDITING_COS_PREFIX", "video-editing/v1"),
        ffmpeg=env.get("VIDEO_EDITING_FFMPEG", getattr(app, "FFMPEG", "ffmpeg")),
        ffprobe=env.get("VIDEO_EDITING_FFPROBE", "ffprobe"), encoder=env.get("VIDEO_EDITING_ENCODER", "h264_nvenc"),
        allowed_hosts=tuple(x.strip().lower() for x in env.get("VIDEO_EDITING_SOURCE_HOSTS", "").split(",") if x.strip()),
        reserve_bytes=int(env.get("VIDEO_EDITING_MIN_FREE_BYTES", str(20 * 1024**3))))
    def client_factory():
        if region != getattr(app, "COS_REGION", ""):
            from qcloud_cos import CosConfig, CosS3Client
            return CosS3Client(CosConfig(Region=region, SecretId=app.COS_SECRET_ID,
                SecretKey=app.COS_SECRET_KEY, Scheme="https"), retry=0)
        return app.get_cos_client(retry=0)
    return EditingService(config, client_factory, render_slots=render_slots)
