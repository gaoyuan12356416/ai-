"""Immutable asset-set and deterministic recipe support for TT overlays."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from pathlib import Path
from typing import Any, Dict, Mapping


ASSET_CATEGORIES = ("border", "light", "opacity_video", "corners", "tint")
CATEGORIES = ("border", "opacity_video", "corners", "tint")
HEX_64_RE = re.compile(r"[0-9a-f]{64}")
SAFE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")


class RandomOverlayError(RuntimeError):
    """Raised when an overlay asset set or recipe fails closed."""


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def load_asset_set(root: Path, expected_manifest_sha256: str) -> Dict[str, Any]:
    """Load and fully verify one immutable asset-set manifest."""

    root = Path(root)
    expected = str(expected_manifest_sha256 or "").strip().lower()
    if not root.is_absolute() or not HEX_64_RE.fullmatch(expected):
        raise RandomOverlayError("overlay root or manifest fingerprint is invalid")
    if not root.is_dir() or root.is_symlink():
        raise RandomOverlayError("overlay root is missing or unsafe")
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RandomOverlayError("overlay manifest is missing or unsafe")
    try:
        manifest_sha, manifest_size = sha256_file(manifest_path)
        if not secrets.compare_digest(manifest_sha, expected):
            raise RandomOverlayError("overlay manifest fingerprint mismatch")
        if manifest_size <= 0 or manifest_size > 2 * 1024 * 1024:
            raise RandomOverlayError("overlay manifest size is invalid")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RandomOverlayError("overlay manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise RandomOverlayError("overlay manifest version is unsupported")
    categories = manifest.get("categories")
    if not isinstance(categories, dict) or set(categories) != set(ASSET_CATEGORIES):
        raise RandomOverlayError("overlay manifest categories are invalid")
    root_resolved = root.resolve(strict=True)
    verified: Dict[str, tuple[Dict[str, Any], ...]] = {}
    for category in ASSET_CATEGORIES:
        rows = categories.get(category)
        if not isinstance(rows, list) or not rows:
            raise RandomOverlayError("overlay category is empty: %s" % category)
        normalized_rows = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "media_type",
                "name",
                "sha256",
                "size",
            }:
                raise RandomOverlayError("overlay asset entry is invalid")
            name = str(row.get("name") or "")
            sha256 = str(row.get("sha256") or "").lower()
            media_type = str(row.get("media_type") or "")
            try:
                size = int(row.get("size"))
            except (TypeError, ValueError, OverflowError) as exc:
                raise RandomOverlayError("overlay asset size is invalid") from exc
            if (
                not SAFE_NAME_RE.fullmatch(name)
                or name in seen
                or not HEX_64_RE.fullmatch(sha256)
                or size <= 0
                or size > 2 * 1024 * 1024 * 1024
                or media_type not in {"image/png", "video/webm"}
            ):
                raise RandomOverlayError("overlay asset contract is invalid")
            path = root / name
            if not path.is_file() or path.is_symlink():
                raise RandomOverlayError("overlay asset is missing or unsafe")
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root_resolved)
                actual_sha, actual_size = sha256_file(resolved)
            except (OSError, ValueError) as exc:
                raise RandomOverlayError("overlay asset is unreadable") from exc
            if actual_size != size or not secrets.compare_digest(actual_sha, sha256):
                raise RandomOverlayError("overlay asset fingerprint mismatch")
            normalized_rows.append(
                {
                    "media_type": media_type,
                    "name": name,
                    "path": resolved,
                    "sha256": sha256,
                    "size": size,
                }
            )
            seen.add(name)
        verified[category] = tuple(normalized_rows)
    return {
        "categories": verified,
        "manifest_sha256": manifest_sha,
        "root": root_resolved,
        "version": 1,
    }


def _seed(identity: Mapping[str, Any], label: str) -> int:
    payload = json.dumps(
        {"identity": dict(identity), "label": label},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def derive_recipe(
    *,
    job_id: str,
    content_id: str,
    profile: str,
    source_url_sha256: str,
    asset_set: Mapping[str, Any],
) -> Dict[str, Any]:
    """Derive one stable recipe so every retry reuses the same choices."""

    identity = {
        "asset_set_sha256": str(asset_set.get("manifest_sha256") or ""),
        "content_id": str(content_id or ""),
        "job_id": str(job_id or ""),
        "profile": str(profile or ""),
        "source_url_sha256": str(source_url_sha256 or ""),
    }
    categories = asset_set.get("categories")
    if not isinstance(categories, Mapping):
        raise RandomOverlayError("overlay asset set is invalid")
    selected = {}
    for category in CATEGORIES:
        rows = categories.get(category)
        if not isinstance(rows, (tuple, list)) or not rows:
            raise RandomOverlayError("overlay category is unavailable")
        row = rows[_seed(identity, "asset:%s" % category) % len(rows)]
        selected[category] = {
            "media_type": row["media_type"],
            "name": row["name"],
            "sha256": row["sha256"],
            "size": int(row["size"]),
        }

    def bounded(label: str, minimum: int, maximum: int) -> int:
        return minimum + (_seed(identity, label) % (maximum - minimum + 1))

    recipe = {
        "asset_set_sha256": identity["asset_set_sha256"],
        "assets": selected,
        "rotation_millidegrees": bounded("rotation", -2000, 2000),
        "scale_bp": bounded("scale", 9800, 10200),
        "tint_opacity_bp": bounded("tint-opacity", 100, 1000),
        "version": 1,
    }
    validate_recipe(recipe, asset_set)
    return recipe


def validate_recipe(recipe: Any, asset_set: Mapping[str, Any]) -> None:
    """Validate a stored recipe against the current immutable asset set."""

    if not isinstance(recipe, dict) or set(recipe) != {
        "asset_set_sha256",
        "assets",
        "rotation_millidegrees",
        "scale_bp",
        "tint_opacity_bp",
        "version",
    }:
        raise RandomOverlayError("overlay recipe is invalid")
    if recipe.get("version") != 1 or not secrets.compare_digest(
        str(recipe.get("asset_set_sha256") or ""),
        str(asset_set.get("manifest_sha256") or ""),
    ):
        raise RandomOverlayError("overlay recipe asset set is invalid")
    try:
        rotation = int(recipe.get("rotation_millidegrees"))
        scale = int(recipe.get("scale_bp"))
        opacity = int(recipe.get("tint_opacity_bp"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise RandomOverlayError("overlay recipe parameters are invalid") from exc
    if not (-2000 <= rotation <= 2000 and 9800 <= scale <= 10200 and 100 <= opacity <= 1000):
        raise RandomOverlayError("overlay recipe parameters are outside contract")
    selected = recipe.get("assets")
    categories = asset_set.get("categories")
    if not isinstance(selected, dict) or set(selected) != set(CATEGORIES):
        raise RandomOverlayError("overlay recipe assets are invalid")
    for category in CATEGORIES:
        row = selected.get(category)
        if not isinstance(row, dict) or set(row) != {
            "media_type",
            "name",
            "sha256",
            "size",
        }:
            raise RandomOverlayError("overlay recipe asset is invalid")
        candidates = categories.get(category) if isinstance(categories, Mapping) else ()
        expected = [item for item in candidates or () if item.get("name") == row.get("name")]
        if len(expected) != 1 or any(
            row.get(key) != expected[0].get(key)
            for key in ("media_type", "name", "sha256", "size")
        ):
            raise RandomOverlayError("overlay recipe asset fingerprint is invalid")


def selected_asset_paths(
    recipe: Mapping[str, Any], asset_set: Mapping[str, Any]
) -> Dict[str, Path]:
    """Return verified source paths for one validated recipe."""

    validate_recipe(recipe, asset_set)
    result = {}
    for category in CATEGORIES:
        selected = recipe["assets"][category]
        candidates = asset_set["categories"][category]
        row = next(item for item in candidates if item["name"] == selected["name"])
        result[category] = Path(row["path"])
    return result
