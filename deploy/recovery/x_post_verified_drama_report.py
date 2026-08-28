"""CPU-verify trusted GPU results for the 2026-08-28 bound-drama incident.

This is not a report importer or an apply command. The caller must authenticate
the private GPU ready manifests via trusted SFTP, pin their hashes and provenance,
and select an unambiguous manifest for each frozen queue. A ready worker manifest
proves the worker checked the request's source bytes before transcoding; this
helper independently downloads and probes the COS output before returning proof.

The caller retains live source selection, private per-item checkpoints, full
frozen-row comparisons, process_lock, and existing store validate/apply guards.
No X/OAuth endpoint, database write, source GET, or repair POST is used here.
"""

import math
import re
from datetime import datetime

from features.x_posts.media_repair import (
    MediaRepairError as WorkerManifestError,
    _manifest_matches,
    _response_from_manifest,
    output_rate_control,
    validate_request,
)
from features.x_posts.service import (
    DEFAULT_MAX_MEDIA_BYTES,
    PREMIUM_MAX_DURATION_SECONDS,
    XPostError,
    build_w2a_url,
    redact_text,
)
from scripts.x_post_daily_runner import (
    CandidatePreflightError,
    DEFAULT_REPAIR_PROFILE,
    _plan_candidate,
    _remove_preflight_file,
    _repair_job_key,
    _validate_repair_probe,
    _verify_repaired_download,
)


EXPECTED_QUEUES = {348: tuple(range(635, 648)), 350: (667, 668, 669)}
INCIDENT_PROFILE = "x-h264-nvenc-720-duration-policy-v5"
OUTPUT_ORIGIN = "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com"
COS_PREFIX = "x-post-media-repair/" + INCIDENT_PROFILE + "/"
OUTPUT_PREFIX = OUTPUT_ORIGIN + "/" + COS_PREFIX
HEX_64 = re.compile(r"[a-f0-9]{64}\Z")


def _require(condition):
    if not condition:
        raise CandidatePreflightError(
            "GPU ready manifest or frozen incident identity cannot be verified",
            code="x_post_bound_drama_gpu_manifest_invalid",
        )


def _positive_integer(value):
    return type(value) is int and value > 0


def _finite_positive(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _validated_manifest(config, candidate, expected, frozen_queue, manifest):
    """Validate the incident scope and the complete, canonical v4 worker record."""
    _require(all(isinstance(value, dict) for value in (
        candidate, expected, frozen_queue, manifest,
    )))
    run_id = frozen_queue.get("schedule_run_id")
    queue_id = expected.get("queue_id")
    _require(_positive_integer(run_id) and run_id in EXPECTED_QUEUES)
    _require(_positive_integer(queue_id) and queue_id in EXPECTED_QUEUES[run_id])
    _require(type(frozen_queue.get("id")) is int and frozen_queue["id"] == queue_id)
    _require(frozen_queue.get("source_type") == candidate.get("source_type") == "drama")
    _require(frozen_queue.get("status") == "failed")
    _require(frozen_queue.get("media_validation_mode") == "deferred")
    _require(expected.get("expected_error_code") == "invalid_media_dimensions")
    for name in ("pool_item_id", "episode_number"):
        frozen_name = "drama_pool_item_id" if name == "pool_item_id" else name
        _require(_positive_integer(expected.get(name)))
        _require(type(candidate.get(name)) is int and candidate[name] == expected[name])
        _require(type(frozen_queue.get(frozen_name)) is int)
        _require(frozen_queue[frozen_name] == expected[name])
    _require(type(candidate.get("drama_pool_item_id")) is int)
    _require(candidate["drama_pool_item_id"] == expected["pool_item_id"])
    _require(isinstance(expected.get("content_id"), str) and expected["content_id"])
    _require(candidate.get("content_id") == frozen_queue.get("content_id") == expected["content_id"])
    material_id = candidate.get("material_id")
    source_url = candidate.get("material_url")
    _require(isinstance(material_id, str) and re.fullmatch(r"[a-f0-9]{32}", material_id))
    _require(material_id == frozen_queue.get("material_id"))
    _require(isinstance(source_url, str) and source_url and source_url == frozen_queue.get("material_url"))
    _require(candidate.get("media_kind", "video") == "video")
    for name in ("original_material_url", "media_repair_trigger_code",
                 "media_repair_job_key", "media_repair_profile",
                 "media_repair_source_sha256", "preflight_sha256"):
        _require(not frozen_queue.get(name))
    _require(type(frozen_queue.get("preflight_size", 0)) is int)
    _require(frozen_queue.get("preflight_size", 0) == 0)
    _require(DEFAULT_REPAIR_PROFILE == config.repair_profile == INCIDENT_PROFILE)
    _require(type(config.max_media_bytes) is int)
    _require(0 < config.max_media_bytes <= DEFAULT_MAX_MEDIA_BYTES)

    _require(set(manifest) == {"version", "status", "request", "cos_key",
                               "result", "repair", "completed_at"})
    _require(type(manifest["version"]) is int and manifest["version"] == 4)
    _require(manifest["status"] == "ready")
    try:
        completed = datetime.strptime(manifest["completed_at"], "%Y-%m-%dT%H:%M:%SZ")
        request = validate_request(manifest["request"], INCIDENT_PROFILE)
    except (WorkerManifestError, TypeError, ValueError):
        _require(False)
    # Worker ready records contain normalized requests, including string pool IDs.
    _require(completed.strftime("%Y-%m-%dT%H:%M:%SZ") == manifest["completed_at"])
    _require(type(manifest["request"]["source_size"]) is int)
    _require(request == manifest["request"] and _manifest_matches(manifest, request))
    expected_request = {
        **request,
        "material_id": material_id,
        "pool_item_id": str(expected["pool_item_id"]),
        "source_url": source_url,
        "trigger_code": expected["expected_error_code"],
        "profile": INCIDENT_PROFILE,
        "duration_policy": "premium",
    }
    _require(_manifest_matches(manifest, expected_request))
    _require(_positive_integer(request["source_size"]))
    _require(request["source_size"] <= config.max_media_bytes)
    job_key = _repair_job_key(candidate, request["source_sha256"], INCIDENT_PROFILE, "premium")
    _require(request["job_key"] == job_key)

    _require(isinstance(manifest["result"], dict))
    _require(set(manifest["result"]) == {"job_key", "profile", "output_url",
                                        "output_sha256", "output_size", "probe"})
    result = _response_from_manifest(manifest, True)
    _require(result["job_key"] == job_key and result["profile"] == INCIDENT_PROFILE)
    output_sha = result["output_sha256"]
    _require(isinstance(output_sha, str) and HEX_64.fullmatch(output_sha))
    _require(_positive_integer(result["output_size"]))
    _require(result["output_size"] <= config.max_media_bytes)
    cos_key = COS_PREFIX + "drama-resource-%s/source-%s/output-%s.mp4" % (
        material_id, request["source_sha256"], output_sha,
    )
    _require(manifest["cos_key"] == cos_key)
    _require(result["output_url"] == OUTPUT_ORIGIN + "/" + cos_key)
    # These are the same response/probe checks used by the repair client, without
    # making a POST. Exact COS equality additionally binds both content hashes.
    probe = _validate_repair_probe(
        result["probe"], result["output_size"],
        max_duration_seconds=PREMIUM_MAX_DURATION_SECONDS,
    )
    worker_fields = {"profile": "high", "field_order": "progressive", "gop": 60,
                     "audio_profile": "lc", "audio_sample_rate": 48000,
                     "audio_channels": 2, "audio_channel_layout": "stereo"}
    _require(set(result["probe"]) == set(probe) | set(worker_fields))
    _require(all(result["probe"].get(name) == value for name, value in worker_fields.items()))
    _require((probe["width"], probe["height"]) in {(720, 720), (720, 1280), (1280, 720)})
    _require(probe["frame_rate"] == 30.0)
    repair = manifest["repair"]
    _require(isinstance(repair, dict) and set(repair) == {
        "source_duration", "target_duration", "trim_applied", "rate_control",
    })
    _require(_finite_positive(repair["source_duration"]) and repair["source_duration"] >= 0.5)
    _require(_finite_positive(repair["target_duration"]))
    trimmed = repair["source_duration"] > PREMIUM_MAX_DURATION_SECONDS
    target_duration = PREMIUM_MAX_DURATION_SECONDS - 1 if trimmed else repair["source_duration"]
    _require(repair["trim_applied"] is trimmed and repair["target_duration"] == target_duration)
    tolerance = 0.5 if trimmed else max(0.5, target_duration * 0.02)
    _require(abs(probe["duration"] - target_duration) <= tolerance)
    _require(repair["rate_control"] == output_rate_control(DEFAULT_MAX_MEDIA_BYTES, target_duration))
    result["probe"] = probe
    return request, result


def prepare_from_gpu_manifest(
    config, candidate, account, rank, timestamp, destination, downloader, prober,
    *, expected, frozen_queue, manifest,
):
    """Return a normal preflight item only after CPU checks of the actual output.

    The supplied manifest must already have trusted, hash-pinned GPU provenance.
    This function cannot authenticate caller-supplied JSON. Missing/invalid cache
    raises; the caller may use the existing preflight entry point for missing
    cache, but must not silently replace conflicting cached evidence.
    """
    _require(isinstance(account, dict) and account.get("long_video_eligible") is True)
    request, repaired = _validated_manifest(config, candidate, expected, frozen_queue, manifest)
    try:
        item = _plan_candidate(account, candidate, rank, timestamp)
    except (XPostError, KeyError, TypeError, ValueError) as exc:
        raise CandidatePreflightError(
            redact_text(str(exc), 240), code="x_post_daily_copy_validation_failed",
        ) from None
    try:
        media = downloader(
            repaired["output_url"], destination, config.media_allowed_hosts,
            max_bytes=config.max_media_bytes, timeout=config.media_timeout,
        )
        _require(isinstance(media, dict))
        if media.get("media_kind") not in (None, "", "video") or str(media.get("media_type", "")).startswith("image/"):
            raise XPostError("invalid_media_type", "修复结果必须是视频", 422)
        probe = prober(
            destination, max_bytes=config.max_media_bytes,
            timeout=config.media_timeout,
            max_duration_seconds=PREMIUM_MAX_DURATION_SECONDS,
        )
        sha256, size, probe = _verify_repaired_download(
            repaired, media, probe, max_duration_seconds=PREMIUM_MAX_DURATION_SECONDS,
        )
        item.update({
            "material_url": repaired["output_url"],
            "original_material_url": request["source_url"],
            "media_repair_trigger_code": request["trigger_code"],
            "media_repair_job_key": request["job_key"],
            "media_repair_profile": request["profile"],
            "media_repair_source_sha256": request["source_sha256"],
            "preflight_sha256": sha256,
            "preflight_size": size,
            "preflight_duration": float(probe["duration"]),
            "preflight_width": probe["width"],
            "preflight_height": probe["height"],
        })
        build_w2a_url({
            "username": item["account_username"], "timestamp": timestamp,
            "material_language": item["material_language"],
            "drama_name": item["drama_name"], "tag": item["tag"], "log_id": 1,
            "page_name": item["page_name"], "page_id": item["page_id"],
            "material_name": item["material_name"], "material_id": item["material_id"],
            "queue_id": 1, "content_id": item["content_id"],
            "video_duration_seconds": item["preflight_duration"],
        })
        return item
    finally:
        _remove_preflight_file(destination)
