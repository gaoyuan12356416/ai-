"""CPU-only template metadata lookup; never contact a GPU or read media files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any, Dict, Union

from .core import DramaSynthesisError, RECIPE_CATEGORIES, RECIPE_PROFILE


MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MANIFEST_CATEGORIES = frozenset((*RECIPE_CATEGORIES, "light"))
ASSET_FIELDS = frozenset(("name", "sha256", "size", "media_type"))
ASSET_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
SHA256 = re.compile(r"[0-9a-f]{64}")


def _invalid() -> DramaSynthesisError:
    return DramaSynthesisError(
        "drama_template_catalog_unavailable", "CPU随机模板目录未配置或校验失败", 503
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest key")
        result[key] = value
    return result


def catalog_from_manifest(
    manifest_file: Union[str, os.PathLike], expected_manifest_sha256: str
) -> Dict[str, Any]:
    """Read only the pinned original FB manifest, preserving asset/recipe identity.

    The CPU does not need the 500+ MB asset bundle. The renderer independently
    checks that bundle before producing media; this metadata read is not a GPU
    health check and deliberately has no network fallback.
    """
    try:
        path = Path(manifest_file)
        expected = str(expected_manifest_sha256 or "").lower()
        if not path.is_absolute() or not SHA256.fullmatch(expected):
            raise _invalid()
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_MANIFEST_BYTES:
            raise _invalid()
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or not 0 < opened.st_size <= MAX_MANIFEST_BYTES:
                raise _invalid()
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) != opened.st_size or not secrets.compare_digest(hashlib.sha256(raw).hexdigest(), expected):
            raise _invalid()
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1:
            raise _invalid()
        source_categories = manifest.get("categories")
        if not isinstance(source_categories, dict) or set(source_categories) != MANIFEST_CATEGORIES:
            raise _invalid()
        categories = {}
        for category in (*RECIPE_CATEGORIES, "light"):
            rows = source_categories[category]
            if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
                raise _invalid()
            normalized, seen = [], set()
            for row in rows:
                if not isinstance(row, dict) or set(row) != ASSET_FIELDS:
                    raise _invalid()
                name, digest = row["name"], row["sha256"]
                size, media_type = row["size"], row["media_type"]
                if (
                    not isinstance(name, str) or not ASSET_NAME.fullmatch(name) or name in seen
                    or not isinstance(digest, str) or not SHA256.fullmatch(digest.lower())
                    or type(size) is not int or not 0 < size <= 2 * 1024 * 1024 * 1024
                    or not isinstance(media_type, str) or media_type not in {"image/png", "video/webm"}
                ):
                    raise _invalid()
                normalized.append({"name": name, "sha256": digest.lower(), "size": size, "media_type": media_type})
                seen.add(name)
            if category != "light":
                categories[category] = normalized
        return {"version": 1, "profile": RECIPE_PROFILE, "manifest_sha256": expected, "categories": categories}
    except DramaSynthesisError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError, RecursionError):
        raise _invalid() from None


__all__ = ["catalog_from_manifest"]
