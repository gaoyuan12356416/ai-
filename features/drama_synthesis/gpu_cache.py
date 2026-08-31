"""Versioned, size-bound replay of already completed GPU media jobs.

Only the renderer may create this metadata from its completed local artifacts.
A failed verification of a completed manifest must never trigger regeneration.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Mapping
import unicodedata
from urllib.parse import urlsplit

from .core import DramaSynthesisError, RECIPE_PROFILE
from .local_checkpoint import file_fingerprint


VERSION_KEY = "gpu_result_manifest_version"
ARTIFACTS_KEY = "gpu_result_artifacts"
VERSION = 3
RECEIPT_FIELDS = {"bucket", "key", "sha256", "size_bytes", "etag", "binding"}
ARTIFACT_FIELDS = {"url", *RECEIPT_FIELDS}
SHA_HEADER = "x-cos-meta-drama-sha256"
SIZE_HEADER = "x-cos-meta-drama-size"
BINDING_HEADER = "x-cos-meta-drama-upload"
ARTIFACT_FILENAMES = {
    "output_video_url": "material.mp4",
    "output_video_no_bgm_url": "material_no_bgm.mp4",
    "output_random_template_url": "material_random_template.mp4",
}
OUTPUT_FIELDS = {
    "concat_video": "output_video_url",
    "no_bgm_video": "output_video_no_bgm_url",
    "random_template_video": "output_random_template_url",
}
PUBLIC_FIELDS = (
    "job_id", *ARTIFACT_FILENAMES, "random_template_output_sha256",
    "random_template_output_profile", "random_template_recipe_sha256",
)


def cache_error():
    return DramaSynthesisError(
        "gpu_result_cache_unverified", "已有成片暂时无法校验，已停止重制，请稍后重试", 503
    )


def versioned(result):
    return isinstance(result, Mapping) and (VERSION_KEY in result or ARTIFACTS_KEY in result)


def public_result(result):
    return {key: result[key] for key in PUBLIC_FIELDS if key in result}


def _safe_text(value, maximum):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and not any(unicodedata.category(char) in {"Cc", "Cs"} for char in value))


def _receipt(value):
    if (not isinstance(value, dict) or set(value) != RECEIPT_FIELDS
            or not _safe_text(value.get("bucket"), 255)
            or not _safe_text(value.get("key"), 4096)
            or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256") or ""))
            or type(value.get("size_bytes")) is not int or value["size_bytes"] <= 0
            or not _safe_text(value.get("etag"), 512)
            or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("binding") or ""))):
        raise cache_error()
    return dict(value)


def _headers(response):
    if not isinstance(response, dict):
        raise cache_error()
    result = {}
    for name, value in response.items():
        if not isinstance(name, str):
            raise cache_error()
        name = name.lower()
        if name in result and result[name] != value:
            raise cache_error()
        result[name] = value
    return result


def _positive_number(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,20}", str(value)):
        raise cache_error()
    number = int(value)
    if number <= 0:
        raise cache_error()
    return number


def _validate_artifacts(result):
    if type(result.get(VERSION_KEY)) is not int or result[VERSION_KEY] != VERSION:
        raise cache_error()
    if not re.fullmatch(r"[0-9a-f]{64}", str(result.get("input_fingerprint") or "")):
        raise cache_error()
    artifacts = result.get(ARTIFACTS_KEY)
    selected = {key for key in ARTIFACT_FILENAMES if result.get(key)}
    if not isinstance(artifacts, dict) or not selected or set(artifacts) != selected:
        raise cache_error()
    validated = {}
    for key, item in artifacts.items():
        if not isinstance(item, dict) or set(item) != ARTIFACT_FIELDS:
            raise cache_error()
        receipt = _receipt({name: item[name] for name in RECEIPT_FIELDS})
        url = item["url"]
        if not isinstance(url, str) or url != result[key]:
            raise cache_error()
        try:
            parsed = urlsplit(url)
            if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                    or parsed.username or parsed.password or parsed.fragment):
                raise ValueError("invalid artifact URL")
        except ValueError:
            raise cache_error() from None
        validated[key] = {"url": url, **receipt}
    if "output_random_template_url" in selected:
        for key in ("random_template_output_sha256", "random_template_recipe_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(result.get(key) or "")):
                raise cache_error()
        if result.get("random_template_output_profile") != RECIPE_PROFILE:
            raise cache_error()
        if result["random_template_output_sha256"] != validated["output_random_template_url"]["sha256"]:
            raise cache_error()
    return validated


def artifact_metadata(result, paths, receipts):
    selected = {key for key in ARTIFACT_FILENAMES if result.get(key)}
    if (not selected or not isinstance(paths, Mapping) or set(paths) != selected
            or not isinstance(receipts, Mapping) or set(receipts) != selected):
        raise cache_error()
    artifacts = {}
    for key in sorted(selected):
        path = Path(paths[key])
        if path.is_symlink() or not path.is_file() or path.name != ARTIFACT_FILENAMES[key]:
            raise cache_error()
        try:
            local = file_fingerprint(path)
        except Exception:
            raise cache_error() from None
        receipt = _receipt(receipts[key])
        if local != {"sha256": receipt["sha256"], "size_bytes": receipt["size_bytes"]}:
            raise cache_error()
        artifacts[key] = {"url": result[key], **receipt}
    value = {**result, VERSION_KEY: VERSION, ARTIFACTS_KEY: artifacts}
    _validate_artifacts(value)
    return {VERSION_KEY: VERSION, ARTIFACTS_KEY: artifacts}


def verify_artifacts(result, outputs, *, client, bucket, url_for_key):
    artifacts = _validate_artifacts(result)
    selected = set(artifacts)
    required = {field for flag, field in OUTPUT_FIELDS.items() if outputs.get(flag)}
    if not required or not required.issubset(selected):
        raise cache_error()
    if not _safe_text(bucket, 255) or not callable(url_for_key) or not callable(getattr(client, "head_object", None)):
        raise cache_error()
    # A v3 manifest is one durable result. Verify every selected object, even
    # when the current caller asked for only a subset, so a damaged companion
    # artifact cannot hide behind a partial cache read.
    for field in sorted(selected):
        item = artifacts[field]
        if item["bucket"] != bucket:
            raise cache_error()
        try:
            if url_for_key(item["key"]) != item["url"]:
                raise cache_error()
            headers = _headers(client.head_object(Bucket=item["bucket"], Key=item["key"]))
            if (_positive_number(headers.get("content-length")) != item["size_bytes"]
                    or _positive_number(headers.get(SIZE_HEADER)) != item["size_bytes"]
                    or headers.get(SHA_HEADER) != item["sha256"]
                    or headers.get(BINDING_HEADER) != item["binding"]
                    or headers.get("etag") != item["etag"]):
                raise cache_error()
        except Exception:
            raise cache_error() from None
    return True


def verify_cached_recipe(result, recipe):
    if not isinstance(recipe, Mapping):
        raise DramaSynthesisError("drama_recipe_hash_invalid", "随机模板配方指纹无效", 409)
    supplied = str(recipe.get("recipe_sha256") or "")
    unsigned = {key: value for key, value in recipe.items() if key != "recipe_sha256"}
    try:
        encoded = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        actual = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        raise DramaSynthesisError("drama_recipe_hash_invalid", "随机模板配方指纹无效", 409) from None
    if not re.fullmatch(r"[0-9a-f]{64}", supplied) or actual != supplied:
        raise DramaSynthesisError("drama_recipe_hash_invalid", "随机模板配方指纹无效", 409)
    if result.get("random_template_recipe_sha256") != supplied:
        raise DramaSynthesisError("drama_recipe_conflict", "该任务已有不同的随机模板成片，已停止重制", 409)
