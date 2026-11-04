"""Immutable, verified RGBA asset cache; composition still runs on OpenCL.

Cache construction is an offline deployment operation. Runtime never decodes
missing assets or silently falls back after an enabled cache fails validation.
"""
from __future__ import annotations
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
from .gpu_compute import file_digest

ENV = 'RANDOM_GPU_ASSET_CACHE_ROOT'
VERSION = 'rgba-nut-v1'


def sha256_file(path):
    digest = file_digest()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


@lru_cache(maxsize=128)
def _source_hash(path, size, mtime_ns):
    return sha256_file(path)


def cached_path(source, root):
    source = Path(source).resolve()
    stat = source.stat()
    key = _source_hash(str(source), stat.st_size, stat.st_mtime_ns)
    root = Path(root).resolve(strict=True)
    receipt = root / (key + '.json')
    record = json.loads(receipt.read_text(encoding='utf-8'))
    target = root / (key + '.nut')
    if target.is_symlink() or not target.is_file():
        raise ValueError('RGBA cache missing or unsafe; offline rebuild required')
    actual = target.stat()
    if (record.get('version') != VERSION or record.get('source_sha256') != key
            or record.get('rgba_frames_verified') is not True
            or record.get('size') != actual.st_size
            or record.get('mtime_ns') != actual.st_mtime_ns):
        raise ValueError('RGBA cache changed or unverified; offline rebuild required')
    return target


def replace_asset_inputs(prefix, root=None):
    root = root if root is not None else os.environ.get(ENV)
    if not root:
        return list(prefix)
    result = [prefix[0]]
    position = 1
    number = 0
    while position < len(prefix):
        try:
            index = prefix.index('-i', position)
        except ValueError:
            result.extend(prefix[position:])
            break
        options = list(prefix[position:index])
        path = prefix[index + 1]
        if number in (1, 2, 3, 4):
            path = str(cached_path(path, root))
            # Raw NUT has its own exact timestamps, including WebM loop length.
            if '-loop' in options:
                loop = options.index('-loop')
                options[loop:loop + 2] = ['-stream_loop', '-1']
            if '-c:v' in options:
                codec = options.index('-c:v')
                options[codec + 1] = 'rawvideo'
        result.extend([*options, '-i', path])
        position = index + 2
        number += 1
    if number not in (5, 6):
        raise ValueError('RGBA cache requires the four-layer random template')
    return result


def validate_configured_cache(environ=None):
    """Offline node admission check, run as the actual service user.

    Validate every selectable layer in every advertised immutable catalog.
    The unused legacy light layer needs no cache. No rendering, rebuilding,
    source replacement, COS operation, or platform request is performed.
    """
    env = os.environ if environ is None else environ
    root = env.get(ENV)
    if not root:
        return {"enabled": False, "reason": "asset_cache_not_configured"}
    from features.random_overlay_catalog import configured_asset_catalogs
    from features.fb_gpu.random_overlay import CATEGORIES, load_asset_set
    catalogs = {}
    for root_key, sha_key in (
        ("X_POST_RANDOM_TEMPLATE_ASSET_ROOT", "X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256"),
        ("FB_PAGE_GPU_RANDOM_OVERLAY_ROOT", "FB_PAGE_GPU_RANDOM_OVERLAY_MANIFEST_SHA256"),
        ("TT_POST_GPU_RANDOM_OVERLAY_ROOT", "TT_POST_GPU_RANDOM_OVERLAY_MANIFEST_SHA256"),
    ):
        if not env.get(root_key) and not env.get(sha_key):
            continue
        approved = configured_asset_catalogs(asset_root=env.get(root_key),
            manifest_sha256=env.get(sha_key), catalogs_json=env.get("RANDOM_OVERLAY_ASSET_CATALOGS", ""))
        for sha, directory in approved.items():
            if sha in catalogs and catalogs[sha] != directory:
                raise ValueError("asset_cache_catalog_conflict")
            catalogs[sha] = directory
    if not catalogs:
        raise ValueError("asset_cache_catalog_missing")
    checked = set()
    for sha, directory in catalogs.items():
        assets = load_asset_set(directory, sha)
        for category in CATEGORIES:
            for asset in assets["categories"][category]:
                target = cached_path(asset["path"], root)
                with target.open("rb") as handle:
                    if not handle.read(1):
                        raise ValueError("asset_cache_empty")
                checked.add(str(target))
    return {"enabled": True, "catalogs_verified": len(catalogs), "assets_verified": len(checked)}
