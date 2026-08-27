#!/usr/bin/env python3
"""Read-only, dependency-light preflight for the isolated HK GPU release.

The default check reads package metadata, paths, asset/model hashes only. It
never imports torch, initializes CUDA, downloads models, installs packages, or
opens a database. --check-app-import additionally verifies the complete worker
import closure with database, network, subprocess and file writes denied.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "deploy" / "drama-synthesis-gpu"
BASE = Path("/data/drama-synthesis-gpu")
PYTHON_VERSION = "3.10.20"
REQUIRED_VALUES = (
    "GPU_VIDEO_WORKER_TOKEN", "DRAMA_PUBLIC_BASE_URL",
    "DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256", "COS_SECRET_ID", "COS_SECRET_KEY",
    "COS_BUCKET", "COS_REGION", "COS_DOMAIN", "COS_PREFIX",
)
DIRECTORY_KEYS = (
    "DRAMA_WORK_ROOT", "DRAMA_PUBLIC_ROOT", "GPU_VIDEO_RESULT_ROOT",
    "DRAMA_RANDOM_OVERLAY_ROOT", "DEMUCS_MODEL_REPO", "XDG_CACHE_HOME",
    "TORCH_HOME", "TMPDIR",
)
FILE_KEYS = (
    "DEMUCS_PYTHON", "DEMUCS_SCRIPT", "DRAMA_FFMPEG", "DRAMA_FFPROBE",
    "DRAMA_RANDOM_OVERLAY_FFMPEG", "DRAMA_RANDOM_OVERLAY_FFPROBE",
)
WRITABLE_DIRECTORY_KEYS = (
    "DRAMA_WORK_ROOT", "DRAMA_PUBLIC_ROOT", "GPU_VIDEO_RESULT_ROOT",
    "XDG_CACHE_HOME", "TORCH_HOME", "TMPDIR",
)


def path_in_root(value, root=BASE):
    path = Path(value)
    if not path.is_absolute():
        return False
    try:
        path.resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError, RuntimeError):
        return False
    return True


def validate_environment(env, *, root=BASE):
    issues = []
    for key in REQUIRED_VALUES:
        if not str(env.get(key, "")).strip():
            issues.append(f"missing:{key}")
    if env.get("DRAMA_GPU_HOST") != "127.0.0.1" or env.get("DRAMA_GPU_PORT") != "8787":
        issues.append("invalid:loopback_endpoint")
    try:
        concurrency = int(env.get("DRAMA_GPU_MAX_CONCURRENCY", "1"))
        if not 1 <= concurrency <= 8:
            raise ValueError()
    except (ValueError, TypeError):
        issues.append("invalid:DRAMA_GPU_MAX_CONCURRENCY")
    if env.get("GPU_VIDEO_WORKER_URL", "").strip():
        issues.append("forbidden:GPU_VIDEO_WORKER_URL")
    if env.get("DRAMA_SHORT_LINK_ROOT", "").strip() or env.get("DRAMA_SHORT_LINK_OWNER", "").strip():
        issues.append("forbidden:cpu_short_link_configuration")
    if env.get("DEMUCS_REQUIRE_LOCAL_MODELS") != "1":
        issues.append("invalid:DEMUCS_REQUIRE_LOCAL_MODELS")
    if env.get("DEMUCS_DEVICE") != "cuda":
        issues.append("invalid:DEMUCS_DEVICE")
    if env.get("TT_DRAMA_RESOURCE_SOURCE", "mysql") != "mysql":
        issues.append("forbidden:resource_cache_initialization")
    for key in ("YOUTUBE_LIVE_ENABLED", "DRAMA_YOUTUBE_UNIFIED_SYNC_ENABLED"):
        if env.get(key) != "0":
            issues.append(f"forbidden:{key}")
    for key in ("DEMUCS_MODEL", "DEMUCS_FALLBACK_MODEL"):
        if env.get(key) != "mdx_extra_q":
            issues.append(f"unsupported:{key}")
    manifest_hash = env.get("DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_hash):
        issues.append("invalid:DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256")
    for key in DIRECTORY_KEYS + FILE_KEYS + ("DRAMA_JOB_DB_PATH",):
        raw = env.get(key, "")
        if not raw or not path_in_root(raw, root):
            issues.append(f"outside_isolated_root:{key}")
            continue
        path = Path(raw)
        if key in DIRECTORY_KEYS and not path.is_dir():
            issues.append(f"missing_directory:{key}")
        if key in WRITABLE_DIRECTORY_KEYS and not os.access(path, os.W_OK | os.X_OK):
            issues.append(f"directory_not_writable:{key}")
        if key in FILE_KEYS and not path.is_file():
            issues.append(f"missing_file:{key}")
        if key in FILE_KEYS and key != "DEMUCS_SCRIPT" and not os.access(path, os.X_OK):
            issues.append(f"not_executable:{key}")
    return issues


def direct_requirements(path=PACKAGE / "requirements-direct-cu124.txt"):
    result = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "--")):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)", line)
        if not match:
            raise ValueError("all direct dependencies must have exact versions")
        result[match[1]] = match[2]
    return result


def package_issues(requirements, version_reader=importlib.metadata.version):
    issues = []
    for name, expected in requirements.items():
        try:
            actual = version_reader(name)
        except importlib.metadata.PackageNotFoundError:
            issues.append(f"missing_package:{name}")
            continue
        if actual != expected:
            issues.append(f"package_version_mismatch:{name}")
    return issues


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_model_repository(repo, sources, bag_template):
    root = Path(repo)
    bag_name = sources["bag_file"]
    bag_path = root / bag_name
    if bag_path.is_symlink() or not bag_path.is_file():
        raise ValueError("offline_model_bag_missing")
    expected_bag = Path(bag_template).read_text(encoding="utf-8").strip()
    if bag_path.read_text(encoding="utf-8").strip() != expected_bag:
        raise ValueError("offline_model_bag_mismatch")
    hashes = {bag_name: sha256_file(bag_path)}
    for entry in sources["files"]:
        name, prefix, expected = entry["name"], entry["sha256_prefix"], entry["sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or not expected.startswith(prefix):
            raise ValueError(f"offline_model_manifest_invalid:{name}")
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"offline_model_file_missing:{name}")
        signature = name.split("-", 1)[0]
        duplicates = [p for p in root.glob("*.th") if p.stem.split("-", 1)[0] == signature]
        if len(duplicates) != 1:
            raise ValueError(f"offline_model_duplicate:{signature}")
        actual = sha256_file(path)
        if actual != expected:
            raise ValueError(f"offline_model_checksum_mismatch:{name}")
        hashes[name] = actual
    return hashes


def check_app_import():
    # This check runs in its own short-lived preflight process, never the service.
    sys.dont_write_bytecode = True

    def deny_mutation(event, args):
        if event in {
            "socket.connect", "sqlite3.connect", "subprocess.Popen", "os.system",
            "os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.chmod",
        }:
            raise RuntimeError("worker_import_side_effect_denied")
        if event == "open":
            mode = args[1] if len(args) > 1 else ""
            flags = args[2] if len(args) > 2 else 0
            if (isinstance(mode, str) and any(char in mode for char in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            ):
                raise RuntimeError("worker_import_file_write_denied")

    sys.addaudithook(deny_mutation)
    importlib.import_module("scripts.drama_synthesis_gpu_worker")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-app-import", action="store_true")
    args = parser.parse_args(argv)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    issues = validate_environment(os.environ)
    if platform.python_version() != PYTHON_VERSION:
        issues.append("python_version_mismatch")
    issues.extend(package_issues(direct_requirements()))
    model_hashes = {}
    if not issues:
        try:
            sources = json.loads((PACKAGE / "model-sources.json").read_text(encoding="utf-8"))
            model_hashes = check_model_repository(
                os.environ["DEMUCS_MODEL_REPO"], sources, PACKAGE / sources["bag_file"]
            )
        except (OSError, ValueError, KeyError) as exc:
            # Exception text is restricted to our own non-secret identifiers.
            issues.append(str(exc) if isinstance(exc, ValueError) else "offline_model_read_failed")
        try:
            from features.fb_gpu.random_overlay import load_asset_set
            load_asset_set(Path(os.environ["DRAMA_RANDOM_OVERLAY_ROOT"]), os.environ["DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256"])
        except Exception:
            issues.append("isolated_asset_set_invalid")
    if args.check_app_import and not issues:
        try:
            check_app_import()
        except Exception:
            issues.append("worker_import_check_failed")
    print(json.dumps({
        "ok": not issues,
        "role": "media-only-runtime-preflight",
        "issues": issues,
        "model_sha256": model_hashes,
        "cuda_tested": False,
        "app_import_checked": bool(args.check_app_import and not issues),
    }, sort_keys=True))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
