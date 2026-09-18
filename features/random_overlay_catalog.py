"""Select immutable overlay bundles from trusted, process-level configuration.

RANDOM_OVERLAY_ASSET_CATALOGS is a JSON object mapping manifest SHA-256 values
to absolute asset directories. It supplements the active default; it cannot
replace that default's directory. Callers must still use their original asset
loader to verify the chosen manifest and every referenced media file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re


CATALOGS_ENV = "RANDOM_OVERLAY_ASSET_CATALOGS"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_JSON_BYTES = 65536
_MAX_CATALOGS = 64


class AssetCatalogError(ValueError):
    """The trusted catalog configuration or requested identity is invalid."""


def _sha256(value):
    if not isinstance(value, str) or not _SHA256.fullmatch(value.lower()):
        raise AssetCatalogError("random_overlay_catalog_sha256_invalid")
    return value.lower()


def _absolute_root(value):
    try:
        raw = os.fspath(value)
        if (not isinstance(raw, str) or not raw or len(raw) > 4096
                or raw != raw.strip() or any(ord(char) < 32 for char in raw)):
            raise ValueError()
        root = Path(raw)
        if (not root.is_absolute() or root == Path(root.anchor)
                or ".." in root.parts):
            raise ValueError()
        return root
    except (TypeError, ValueError, OSError):
        raise AssetCatalogError("random_overlay_catalog_root_invalid") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AssetCatalogError("random_overlay_catalog_duplicate_sha256")
        result[key] = value
    return result


def configured_asset_catalogs(*, asset_root, manifest_sha256, catalogs_json=None):
    """Return approved SHA -> Path entries, including the active default.

    ``catalogs_json`` is an optional configuration/test injection, never a
    request field. Omitting it reads the process environment. Selection itself
    performs no filesystem access and does not imply that a bundle is verified.
    """
    current_sha = _sha256(manifest_sha256)
    current_root = _absolute_root(asset_root)
    raw = os.environ.get(CATALOGS_ENV, "") if catalogs_json is None else catalogs_json
    try:
        valid_raw = isinstance(raw, str) and len(raw.encode("utf-8")) <= _MAX_JSON_BYTES
    except UnicodeError:
        valid_raw = False
    if not valid_raw:
        raise AssetCatalogError("random_overlay_catalog_config_invalid")
    try:
        configured = json.loads(raw, object_pairs_hook=_unique_object) if raw.strip() else {}
    except (ValueError, RecursionError):
        raise AssetCatalogError("random_overlay_catalog_config_invalid") from None
    if not isinstance(configured, dict) or len(configured) > _MAX_CATALOGS:
        raise AssetCatalogError("random_overlay_catalog_config_invalid")
    approved = {current_sha: current_root}
    seen = set()
    for fingerprint, directory in configured.items():
        fingerprint = _sha256(fingerprint)
        if fingerprint in seen:
            raise AssetCatalogError("random_overlay_catalog_duplicate_sha256")
        seen.add(fingerprint)
        if not isinstance(directory, str):
            raise AssetCatalogError("random_overlay_catalog_root_invalid")
        directory = _absolute_root(directory)
        if fingerprint == current_sha and directory != current_root:
            raise AssetCatalogError("random_overlay_catalog_default_conflict")
        approved[fingerprint] = directory
    return approved


def resolve_asset_catalog(*, asset_root, manifest_sha256, requested_sha256=None,
                          catalogs_json=None):
    """Return ``(Path, SHA)`` for the default or an explicitly approved SHA.

    An absent request (None) selects the default. Empty, malformed, and unknown
    SHA values are rejected; no directory is inferred from an untrusted recipe.
    """
    approved = configured_asset_catalogs(
        asset_root=asset_root, manifest_sha256=manifest_sha256,
        catalogs_json=catalogs_json,
    )
    requested = _sha256(manifest_sha256 if requested_sha256 is None else requested_sha256)
    if requested not in approved:
        raise AssetCatalogError("random_overlay_catalog_unknown_sha256")
    return approved[requested], requested


__all__ = ["AssetCatalogError", "CATALOGS_ENV", "configured_asset_catalogs", "resolve_asset_catalog"]
