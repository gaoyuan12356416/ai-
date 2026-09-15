"""Read-only, content-verified RGBA assets shared by drama renderer children.

Only the offline builder writes this cache. Enabling the cache is fail-closed:
an absent or changed receipt cannot silently reintroduce per-frame VP9 decoding.
The digest memo includes ctime/inode as well as mtime, so a same-size rewrite is
reverified even if someone restores the old modification time.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re

VERSION = "rgba-nut-v1"
MAX_ITEM_BYTES = 8 * 1024**3
MAX_TOTAL_BYTES = 96 * 1024**3
MIN_FREE_BYTES = 32 * 1024**3
HEX = re.compile(r"[0-9a-f]{64}\Z")


class AssetCacheError(RuntimeError):
    pass


def cache_root(value=None):
    raw = os.environ.get("DRAMA_GPU_ASSET_CACHE_ROOT", "") if value is None else value
    if not raw:
        return None
    root = Path(raw)
    if (not root.is_absolute() or root == Path(root.anchor) or root.is_symlink()
            or not root.is_dir() or root.resolve() != root):
        raise AssetCacheError("drama_asset_cache_root_invalid")
    if os.name == "posix":
        stat = root.stat()
        if stat.st_uid != 0 or stat.st_mode & 0o022:
            raise AssetCacheError("drama_asset_cache_root_untrusted")
    return root


def stat_key(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
            stat.st_ctime_ns, stat.st_uid, stat.st_mode)


@lru_cache(maxsize=64)
def _digest(path_text, key):
    path = Path(path_text)
    if path.is_symlink() or stat_key(path) != key:
        raise AssetCacheError("drama_asset_cache_changed")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    if stat_key(path) != key:
        raise AssetCacheError("drama_asset_cache_changed")
    return digest.hexdigest()


def verified_entry(root, source_sha256, *, verify_digest=True):
    """Validate a root-owned receipt. Parent verifies bytes once per inode.

    An already fenced child can omit the full digest after its parent verified
    the immutable entry, but must still recheck receipt ownership and stat data.
    """
    if not HEX.fullmatch(str(source_sha256)):
        raise AssetCacheError("drama_asset_cache_source_invalid")
    root = cache_root(root)
    if root is None:
        raise AssetCacheError("drama_asset_cache_required")
    path, receipt = root / (source_sha256 + ".nut"), root / (source_sha256 + ".json")
    try:
        for item in (path, receipt):
            if item.is_symlink() or not item.is_file():
                raise ValueError()
            stat = item.stat()
            if os.name == "posix" and (stat.st_uid != 0 or stat.st_mode & 0o222):
                raise ValueError()
        if not 0 < receipt.stat().st_size < 16384:
            raise ValueError()
        info = json.loads(receipt.read_text(encoding="utf-8"))
        key = stat_key(path)
        if (info.get("version") != VERSION or info.get("source_sha256") != source_sha256
                or info.get("rgba_frames_verified") is not True
                or not HEX.fullmatch(str(info.get("sha256")))
                or type(info.get("size")) is not int or not 0 < info["size"] <= MAX_ITEM_BYTES
                or info["size"] != key[2] or info.get("mtime_ns") != key[3]
                or type(info.get("frames")) is not int or not 1 <= info["frames"] <= 7200):
            raise ValueError()
        # Windows ctime is creation time, so only POSIX may memoize this proof.
        digest = _digest if os.name == "posix" else _digest.__wrapped__
        if verify_digest and digest(str(path), key) != info["sha256"]:
            raise ValueError()
        if stat_key(path) != key:
            raise ValueError()
        return {**info, "path": str(path)}
    except (OSError, ValueError, TypeError, KeyError):
        raise AssetCacheError("drama_asset_cache_unverified") from None


def selected_entries(recipe, *, root=None, verify_digest=True):
    root = cache_root(root)
    if root is None:
        return {}
    return {category: verified_entry(root, recipe["assets"][category]["sha256"],
                                     verify_digest=verify_digest)
            for category in ("border", "opacity_video", "corners", "tint")}
