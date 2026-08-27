"""HK-GPU random-template rendering adapter.

It reuses the verified FB random-overlay asset manifest and FFmpeg graph, while
keeping a drama-specific immutable profile and result identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Mapping, Union

from features.fb_gpu.prepare_worker import build_command
from features.fb_gpu.random_overlay import (
    load_asset_set,
    selected_asset_paths,
    sha256_file,
    validate_recipe,
)

from .core import DramaSynthesisError, RECIPE_CATEGORIES, RECIPE_PROFILE


def catalog_from_assets(asset_root: Union[str, os.PathLike], manifest_sha256: str) -> Dict[str, Any]:
    assets = load_asset_set(Path(asset_root), str(manifest_sha256 or "").lower())
    categories = {}
    for category in RECIPE_CATEGORIES:
        categories[category] = [
            {key: row[key] for key in ("name", "sha256", "media_type", "size")}
            for row in assets["categories"][category]
        ]
    return {"version": 1, "profile": RECIPE_PROFILE, "manifest_sha256": assets["manifest_sha256"], "categories": categories}


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _probe(ffprobe: str, path: Path) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        payload = json.loads(proc.stdout)
    except Exception:
        raise DramaSynthesisError("drama_random_probe_failed", "随机模板视频校验失败", 502) from None
    streams = payload.get("streams") if isinstance(payload, Mapping) else []
    video = next((row for row in streams if row.get("codec_type") == "video"), {})
    audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
    try:
        duration = float((payload.get("format") or {}).get("duration") or video.get("duration") or 0)
    except (TypeError, ValueError, OverflowError):
        duration = 0
    if not math.isfinite(duration) or duration <= 0:
        raise DramaSynthesisError("drama_random_probe_failed", "随机模板视频时长无效", 502)
    return {"duration": duration, "has_audio": audio is not None, "video": video, "audio": audio}


def build_drama_random_command(config, source, output, info, recipe, assets):
    command = list(build_command(config, source, output, info, recipe, assets))
    prefix = "[0:v]setpts=PTS-STARTPTS,"
    if command.count("-filter_complex") != 1 or "-copyts" in command:
        raise DramaSynthesisError("drama_random_graph_contract_invalid", "随机模板处理配置不兼容", 503)
    graph_index = command.index("-filter_complex") + 1
    if graph_index >= len(command) or not command[graph_index].startswith(prefix) or command[graph_index].count(prefix) != 1:
        raise DramaSynthesisError("drama_random_graph_contract_invalid", "随机模板处理配置不兼容", 503)
    # A 16:9 intro followed by portrait episodes reinitializes the filter graph.
    # Demuxer timestamps are already rebased by FFmpeg's default input handling;
    # resetting them again at every reinit silently drops earlier seconds.
    command[graph_index] = command[graph_index].replace(prefix, "[0:v]setpts=PTS,", 1)
    # Match the dedicated worker's two-core budget. The inherited FB graph's
    # automatic per-filter thread pools exceed this service's TasksMax=128.
    # This is a drama-only invocation override, not a change to the FB worker.
    return [command[0], "-filter_complex_threads", "2", *command[1:]]


def random_output_duration_matches(source_seconds, output_seconds, video_seconds):
    try:
        values = [float(value) for value in (source_seconds, output_seconds, video_seconds)]
    except (ValueError, TypeError, OverflowError):
        return False
    # Fixed tolerance covers 30fps/AAC rounding, never a whole missing intro.
    return all(math.isfinite(value) and value > 0 for value in values) and all(
        abs(values[0] - value) <= 0.15 for value in values[1:]
    )


def render_random_output(
    *,
    source: Union[str, os.PathLike],
    output: Union[str, os.PathLike],
    recipe: Mapping[str, Any],
    asset_root: Union[str, os.PathLike],
    manifest_sha256: str,
    ffmpeg: str = "/usr/bin/ffmpeg",
    ffprobe: str = "/usr/bin/ffprobe",
    timeout: int = 10800,
    runner=subprocess.run,
) -> Dict[str, Any]:
    source_path, output_path = Path(source), Path(output)
    if not source_path.is_file() or source_path.is_symlink():
        raise DramaSynthesisError("drama_random_source_missing", "随机模板源视频不存在", 502)
    if recipe.get("profile") != RECIPE_PROFILE or int(recipe.get("version") or 0) != 1:
        raise DramaSynthesisError("drama_recipe_profile_mismatch", "随机模板配方版本不一致", 409)
    supplied_sha = str(recipe.get("recipe_sha256") or "")
    unsigned = {key: value for key, value in recipe.items() if key != "recipe_sha256"}
    actual_sha = hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", supplied_sha) or supplied_sha != actual_sha:
        raise DramaSynthesisError("drama_recipe_hash_invalid", "随机模板配方指纹无效", 409)
    assets = load_asset_set(Path(asset_root), str(manifest_sha256 or "").lower())
    fb_recipe = {
        "asset_set_sha256": recipe.get("asset_set_sha256"),
        "assets": recipe.get("assets"),
        "rotation_millidegrees": int(recipe.get("rotation_millidegrees") or 0),
        "scale_bp": int(recipe.get("scale_bp") or 0),
        "tint_opacity_bp": int(recipe.get("tint_opacity_bp") or 0),
        "version": 1,
    }
    try:
        validate_recipe(fb_recipe, assets)
    except Exception:
        raise DramaSynthesisError("drama_recipe_asset_mismatch", "随机模板配方与GPU素材不一致", 409) from None
    source_info = _probe(ffprobe, source_path)
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    config = SimpleNamespace(ffmpeg=ffmpeg)
    command = build_drama_random_command(config, source_path, output_path, source_info, fb_recipe, selected_asset_paths(fb_recipe, assets))
    try:
        runner(command, check=True, capture_output=True, text=True, timeout=max(60, min(int(timeout), 10800)))
    except Exception:
        output_path.unlink(missing_ok=True)
        raise DramaSynthesisError("drama_random_render_failed", "随机模板视频制作失败", 502) from None
    result_info = _probe(ffprobe, output_path)
    video = result_info["video"]
    if not random_output_duration_matches(source_info["duration"], result_info["duration"], video.get("duration")):
        output_path.unlink(missing_ok=True)
        raise DramaSynthesisError("drama_random_duration_mismatch", "随机模板成片时长与源视频不一致，已阻止上传", 502)
    if (
        video.get("codec_name") != "h264"
        or str(video.get("profile") or "").lower() != "high"
        or int(video.get("width") or 0) != 720
        or int(video.get("height") or 0) != 1280
    ):
        output_path.unlink(missing_ok=True)
        raise DramaSynthesisError("drama_random_output_contract_invalid", "随机模板成片规格不符合要求", 502)
    output_sha, size = sha256_file(output_path)
    return {
        "output_sha256": output_sha,
        "output_size": size,
        "duration_seconds": result_info["duration"],
        "profile": RECIPE_PROFILE,
        "recipe_sha256": supplied_sha,
    }


__all__ = ["catalog_from_assets", "render_random_output"]
