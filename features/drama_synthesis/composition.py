"""Versioned, deterministic composition plans for GPU media renderers.

The business recipe stays immutable and renderer-agnostic.  This module turns
that recipe into a strict scene graph and a frame-exact chunk plan which can be
consumed by OpenCL today and a CUDA-native renderer later without changing the
CPU/GPU API contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from typing import Any, Dict, Iterable, Mapping, Sequence

from .core import DramaSynthesisError, RECIPE_PROFILE


SPEC_VERSION = 1
RENDERER_PROFILE = "drama-opencl-fused-h264-720x1280-v3-clean"
CANVAS_WIDTH = 720
CANVAS_HEIGHT = 1280
CANVAS_FPS = 30
DEFAULT_CHUNK_SECONDS = 120
MIN_CHUNK_SECONDS = 30
MAX_CHUNK_SECONDS = 300
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def composition_error() -> DramaSynthesisError:
    return DramaSynthesisError("drama_composition_invalid", "随机模板场景配置无效", 503)


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def composition_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _strict_int(value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise composition_error()
    return value


def _positive_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise composition_error()
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise composition_error()
    return number


def chunk_seconds(value: Any = None) -> int:
    raw = os.environ.get("DRAMA_GPU_CHUNK_SECONDS", str(DEFAULT_CHUNK_SECONDS)) if value is None else value
    if isinstance(raw, bool) or not re.fullmatch(r"[0-9]{2,3}", str(raw)):
        raise composition_error()
    seconds = int(raw)
    if not MIN_CHUNK_SECONDS <= seconds <= MAX_CHUNK_SECONDS:
        raise composition_error()
    return seconds


def _asset_row(value: Any, category: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"media_type", "name", "sha256", "size"}:
        raise composition_error()
    name = value.get("name")
    sha = value.get("sha256")
    media_type = value.get("media_type")
    size = value.get("size")
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name)
        or not isinstance(sha, str)
        or not SHA256.fullmatch(sha)
        or media_type not in {"image/png", "video/webm"}
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 < size <= 2 * 1024 * 1024 * 1024
    ):
        raise composition_error()
    return {
        "category": category,
        "media_type": media_type,
        "name": name,
        "sha256": sha,
        "size_bytes": size,
    }


def _validate_compiled_asset(value: Any, category: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "category", "media_type", "name", "sha256", "size_bytes"
    }:
        raise composition_error()
    if value.get("category") != category:
        raise composition_error()
    _asset_row({
        "media_type": value.get("media_type"),
        "name": value.get("name"),
        "sha256": value.get("sha256"),
        "size": value.get("size_bytes"),
    }, category)
    expected_media_type = {
        "border": "image/png",
        "opacity_video": "video/webm",
        "corners": "video/webm",
        "tint": "image/png",
    }.get(category)
    if value.get("media_type") != expected_media_type:
        raise composition_error()


def compile_random_overlay_spec(
    recipe: Mapping[str, Any], source_info: Mapping[str, Any], *, renderer_profile: str = RENDERER_PROFILE
) -> Dict[str, Any]:
    """Compile the immutable random recipe into a renderer-neutral scene."""
    if not isinstance(recipe, Mapping) or recipe.get("profile") != RECIPE_PROFILE:
        raise composition_error()
    if not isinstance(renderer_profile, str) or not re.fullmatch(r"[a-z0-9-]{16,100}", renderer_profile):
        raise composition_error()
    duration = _positive_float(source_info.get("duration"))
    video = source_info.get("video")
    if not isinstance(video, Mapping):
        raise composition_error()
    source_width = _strict_int(video.get("width"), 16, 16384)
    source_height = _strict_int(video.get("height"), 16, 16384)
    total_frames = max(1, int(round(duration * CANVAS_FPS)))
    assets = recipe.get("assets")
    if not isinstance(assets, Mapping) or set(assets) != {"border", "opacity_video", "corners", "tint"}:
        raise composition_error()
    layers = [
        {
            "id": "source-background",
            "kind": "video",
            "source": "input",
            "fit": "cover",
            "opacity_bp": 10000,
            "transform": {"rotation_millidegrees": 0, "scale_bp": 10000},
            "z_index": 0,
        },
        {
            "id": "source-main",
            "kind": "video",
            "source": "input",
            "fit": "contain",
            "opacity_bp": 10000,
            "transform": {
                "rotation_millidegrees": _strict_int(recipe.get("rotation_millidegrees"), -2000, 2000),
                "scale_bp": _strict_int(recipe.get("scale_bp"), 9800, 10200),
            },
            "z_index": 1,
        },
    ]
    for z_index, category in enumerate(("tint", "opacity_video", "border", "corners"), start=2):
        asset = _asset_row(assets.get(category), category)
        layers.append({
            "id": "asset-" + category,
            "kind": "image" if asset["media_type"] == "image/png" else "video",
            "source": asset,
            "fit": "stretch",
            "opacity_bp": (
                _strict_int(recipe.get("tint_opacity_bp"), 100, 1000) if category == "tint" else 10000
            ),
            "transform": {"rotation_millidegrees": 0, "scale_bp": 10000},
            "z_index": z_index,
        })
    spec = {
        "version": SPEC_VERSION,
        "renderer_profile": renderer_profile,
        "canvas": {
            "width": CANVAS_WIDTH,
            "height": CANVAS_HEIGHT,
            "fps_num": CANVAS_FPS,
            "fps_den": 1,
            "pixel_format": "rgba",
            "color_space": "bt709",
        },
        "timeline": {
            "duration_seconds": round(total_frames / CANVAS_FPS, 6),
            "source_duration_seconds": round(duration, 6),
            "total_frames": total_frames,
        },
        "source": {"width": source_width, "height": source_height, "has_audio": bool(source_info.get("has_audio"))},
        "audio": {"mode": "continuous_source", "codec": "aac", "sample_rate": 48000, "channels": 2},
        "layers": layers,
        "output": {
            "codec": "h264_nvenc",
            "profile": "high",
            "rate_control": "vbr",
            "cq": 21,
            "gop_frames": 60,
        },
    }
    validate_composition_spec(spec)
    return spec


def validate_composition_spec(spec: Any) -> Dict[str, Any]:
    if not isinstance(spec, Mapping) or set(spec) != {
        "version", "renderer_profile", "canvas", "timeline", "source", "audio", "layers", "output"
    }:
        raise composition_error()
    if spec.get("version") != SPEC_VERSION or spec.get("renderer_profile") != RENDERER_PROFILE:
        raise composition_error()
    canvas = spec.get("canvas")
    if not isinstance(canvas, Mapping) or dict(canvas) != {
        "width": CANVAS_WIDTH,
        "height": CANVAS_HEIGHT,
        "fps_num": CANVAS_FPS,
        "fps_den": 1,
        "pixel_format": "rgba",
        "color_space": "bt709",
    }:
        raise composition_error()
    timeline = spec.get("timeline")
    if not isinstance(timeline, Mapping) or set(timeline) != {
        "duration_seconds", "source_duration_seconds", "total_frames"
    }:
        raise composition_error()
    total_frames = _strict_int(timeline.get("total_frames"), 1, 100_000_000)
    expected = total_frames / CANVAS_FPS
    if abs(_positive_float(timeline.get("duration_seconds")) - expected) > 0.000001:
        raise composition_error()
    _positive_float(timeline.get("source_duration_seconds"))
    source = spec.get("source")
    if not isinstance(source, Mapping) or set(source) != {"width", "height", "has_audio"}:
        raise composition_error()
    _strict_int(source.get("width"), 16, 16384)
    _strict_int(source.get("height"), 16, 16384)
    if not isinstance(source.get("has_audio"), bool):
        raise composition_error()
    audio = spec.get("audio")
    if not isinstance(audio, Mapping) or dict(audio) != {
        "mode": "continuous_source", "codec": "aac", "sample_rate": 48000, "channels": 2
    }:
        raise composition_error()
    layers = spec.get("layers")
    if not isinstance(layers, Sequence) or isinstance(layers, (str, bytes)) or len(layers) != 6:
        raise composition_error()
    if [row.get("id") if isinstance(row, Mapping) else None for row in layers] != [
        "source-background", "source-main", "asset-tint", "asset-opacity_video", "asset-border", "asset-corners"
    ]:
        raise composition_error()
    expected_layer_fields = {"id", "kind", "source", "fit", "opacity_bp", "transform", "z_index"}
    for index, row in enumerate(layers):
        if (
            not isinstance(row, Mapping)
            or set(row) != expected_layer_fields
            or row.get("fit") not in {"cover", "contain", "stretch"}
            or row.get("z_index") != index
        ):
            raise composition_error()
        _strict_int(row.get("opacity_bp"), 0, 10000)
        transform = row.get("transform")
        if not isinstance(transform, Mapping) or set(transform) != {"rotation_millidegrees", "scale_bp"}:
            raise composition_error()
        _strict_int(transform.get("rotation_millidegrees"), -360000, 360000)
        _strict_int(transform.get("scale_bp"), 1, 100000)
        if index < 2:
            if row.get("kind") != "video" or row.get("source") != "input":
                raise composition_error()
        else:
            category = str(row.get("id"))[len("asset-"):]
            _validate_compiled_asset(row.get("source"), category)
            expected_kind = "image" if row["source"]["media_type"] == "image/png" else "video"
            if row.get("kind") != expected_kind or row.get("fit") != "stretch":
                raise composition_error()
    output = spec.get("output")
    if not isinstance(output, Mapping) or dict(output) != {
        "codec": "h264_nvenc", "profile": "high", "rate_control": "vbr", "cq": 21, "gop_frames": 60
    }:
        raise composition_error()
    return dict(spec)


def plan_chunks(total_frames: Any, *, fps: int = CANVAS_FPS, seconds: Any = None) -> list[Dict[str, Any]]:
    frames = _strict_int(total_frames, 1, 100_000_000)
    rate = _strict_int(fps, 1, 240)
    seconds = chunk_seconds(seconds)
    chunk_frames = rate * seconds
    result = []
    start = 0
    index = 0
    while start < frames:
        count = min(chunk_frames, frames - start)
        result.append({
            "index": index,
            "start_frame": start,
            "frame_count": count,
            "start_seconds": round(start / rate, 6),
            "duration_seconds": round(count / rate, 6),
        })
        start += count
        index += 1
    if not result or sum(row["frame_count"] for row in result) != frames:
        raise composition_error()
    return result


def selected_asset_identities(spec: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    validate_composition_spec(spec)
    for layer in spec["layers"]:
        if str(layer["id"]).startswith("asset-"):
            yield layer["source"]


__all__ = [
    "CANVAS_FPS", "CANVAS_HEIGHT", "CANVAS_WIDTH", "DEFAULT_CHUNK_SECONDS", "MAX_CHUNK_SECONDS",
    "MIN_CHUNK_SECONDS", "RENDERER_PROFILE", "SPEC_VERSION", "canonical_json", "chunk_seconds",
    "compile_random_overlay_spec", "composition_sha256", "plan_chunks", "selected_asset_identities",
    "validate_composition_spec",
]
