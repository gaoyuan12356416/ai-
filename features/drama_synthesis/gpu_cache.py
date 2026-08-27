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
from urllib.parse import urlsplit

from .core import DramaSynthesisError, RECIPE_PROFILE


VERSION_KEY = "gpu_result_manifest_version"
ARTIFACTS_KEY = "gpu_result_artifacts"
VERSION = 2
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


def artifact_metadata(result, paths):
    selected = {key for key in ARTIFACT_FILENAMES if result.get(key)}
    if not selected or set(paths) != selected:
        raise cache_error()
    artifacts = {}
    for key in sorted(selected):
        path = Path(paths[key])
        if path.is_symlink() or not path.is_file() or path.name != ARTIFACT_FILENAMES[key]:
            raise cache_error()
        size = path.stat().st_size
        if size <= 0:
            raise cache_error()
        artifacts[key] = {"url": result[key], "size_bytes": size}
    return {VERSION_KEY: VERSION, ARTIFACTS_KEY: artifacts}


def verify_artifacts(result, outputs, *, head, timeout):
    if type(result.get(VERSION_KEY)) is not int or result[VERSION_KEY] != VERSION:
        raise cache_error()
    artifacts = result.get(ARTIFACTS_KEY)
    selected = {key for key in ARTIFACT_FILENAMES if result.get(key)}
    if not isinstance(artifacts, dict) or not selected or set(artifacts) != selected:
        raise cache_error()
    for key, item in artifacts.items():
        if not isinstance(item, dict) or set(item) != {"url", "size_bytes"}:
            raise cache_error()
        if type(item["size_bytes"]) is not int or item["size_bytes"] <= 0:
            raise cache_error()
        url = item["url"]
        if not isinstance(url, str) or url != result[key]:
            raise cache_error()
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
                raise ValueError("invalid artifact URL")
        except ValueError:
            raise cache_error() from None
    required = {field for flag, field in OUTPUT_FIELDS.items() if outputs.get(flag)}
    if not required or not required.issubset(selected):
        raise cache_error()
    if "output_random_template_url" in selected:
        for key in ("random_template_output_sha256", "random_template_recipe_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(result.get(key) or "")):
                raise cache_error()
        if result.get("random_template_output_profile") != RECIPE_PROFILE:
            raise cache_error()
    for field in sorted(required):
        item = artifacts[field]
        response = None
        try:
            response = head(item["url"], allow_redirects=False, timeout=timeout)
            length = response.headers.get("Content-Length")
            # A partial 206 response cannot prove the full object length.
            if response.status_code != 200 or not isinstance(length, str) or not re.fullmatch(r"[0-9]+", length):
                raise cache_error()
            if int(length) != item["size_bytes"]:
                raise cache_error()
        except Exception:
            raise cache_error() from None
        finally:
            if response is not None:
                response.close()
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
