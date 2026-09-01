#!/usr/bin/env python3
"""Read-only, dependency-light preflight for the isolated HK GPU release.

The check never imports torch, downloads models, installs packages, or opens a
database. For the fused compositor it creates and removes a one-second isolated
clip under TMPDIR to exercise the real five-input OpenCL-to-NVENC pipeline.
--check-app-import additionally verifies the complete worker import closure
with database, network, subprocess and file writes denied.
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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "deploy" / "drama-synthesis-gpu"
BASE = Path("/data/drama-synthesis-gpu")
PYTHON_VERSION = "3.10.20"
MIN_FREE_BYTES = 50 * 1024 * 1024 * 1024
REQUIRED_VALUES = (
    "GPU_VIDEO_WORKER_TOKEN", "DRAMA_PUBLIC_BASE_URL",
    "DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256", "COS_SECRET_ID", "COS_SECRET_KEY",
    "COS_BUCKET", "COS_REGION", "COS_DOMAIN", "COS_PREFIX", "DRAMA_GPU_RELEASE_SHA",
    "DRAMA_NVIDIA_SMI",
)
DIRECTORY_KEYS = (
    "DRAMA_WORK_ROOT", "DRAMA_PUBLIC_ROOT", "GPU_VIDEO_RESULT_ROOT",
    "DRAMA_RANDOM_OVERLAY_ROOT", "DEMUCS_MODEL_REPO", "XDG_CACHE_HOME",
    "TORCH_HOME", "TMPDIR", "DRAMA_GPU_COMPOSITOR_CACHE_ROOT",
)
FILE_KEYS = (
    "DEMUCS_PYTHON", "DEMUCS_SCRIPT", "DRAMA_FFMPEG", "DRAMA_FFPROBE",
    "DRAMA_RANDOM_OVERLAY_FFMPEG", "DRAMA_RANDOM_OVERLAY_FFPROBE",
)
WRITABLE_DIRECTORY_KEYS = (
    "DRAMA_WORK_ROOT", "DRAMA_PUBLIC_ROOT", "GPU_VIDEO_RESULT_ROOT",
    "XDG_CACHE_HOME", "TORCH_HOME", "TMPDIR", "DRAMA_GPU_COMPOSITOR_CACHE_ROOT",
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


def release_identity_issues(env, *, code_root=ROOT, base=BASE):
    sha = str(env.get("DRAMA_GPU_RELEASE_SHA", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return ["release_identity_invalid"]
    expected = Path(base) / "releases" / sha
    try:
        if expected.is_symlink() or not expected.is_dir() or Path(code_root).resolve() != expected.resolve():
            return ["release_identity_mismatch"]
    except (OSError, RuntimeError):
        return ["release_identity_mismatch"]
    return []


def valid_nvidia_smi(value):
    path = Path(value)
    try:
        metadata = path.stat()
        return bool(
            path.is_absolute() and not path.is_symlink() and path.is_file()
            and path.name == "nvidia-smi" and metadata.st_uid == 0
            and not metadata.st_mode & 0o022 and os.access(path, os.X_OK)
        )
    except (OSError, AttributeError):
        return False


def storage_issues(env, *, base=BASE, minimum_free_bytes=MIN_FREE_BYTES):
    try:
        base_path = Path(base)
        if base_path.is_symlink() or not base_path.is_dir():
            return ["isolated_storage_unavailable"]
        device = base_path.stat().st_dev
        for key in WRITABLE_DIRECTORY_KEYS:
            path = Path(env[key])
            if path.is_symlink() or not path.is_dir() or path.stat().st_dev != device:
                return ["isolated_storage_identity_mismatch"]
        if shutil.disk_usage(base_path).free < minimum_free_bytes:
            return ["isolated_storage_low_space"]
    except (KeyError, OSError, ValueError):
        return ["isolated_storage_unavailable"]
    return []


def validate_environment(env, *, root=BASE):
    issues = []
    for key in REQUIRED_VALUES:
        if not str(env.get(key, "")).strip():
            issues.append(f"missing:{key}")
    if env.get("DRAMA_GPU_HOST") != "127.0.0.1" or env.get("DRAMA_GPU_PORT") != "8787":
        issues.append("invalid:loopback_endpoint")
    try:
        concurrency = int(env.get("DRAMA_GPU_MAX_CONCURRENCY", "1"))
        if concurrency not in {1, 2}:
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
    if not re.fullmatch(r"[0-9a-f]{40}", env.get("DRAMA_GPU_RELEASE_SHA", "")):
        issues.append("invalid:DRAMA_GPU_RELEASE_SHA")
    if not valid_nvidia_smi(env.get("DRAMA_NVIDIA_SMI", "")):
        issues.append("invalid:DRAMA_NVIDIA_SMI")
    backend = env.get("DRAMA_GPU_COMPOSITOR_BACKEND", "")
    if backend not in {"legacy_cpu", "opencl_fused_v2"}:
        issues.append("invalid:DRAMA_GPU_COMPOSITOR_BACKEND")
    if backend == "opencl_fused_v2":
        if env.get("DRAMA_GPU_MAX_CONCURRENCY") != "1":
            issues.append("invalid:v2_full_job_concurrency")
        if not re.fullmatch(r"[0-9]{1,2}\.[0-9]{1,2}", env.get("DRAMA_GPU_OPENCL_DEVICE", "")):
            issues.append("invalid:DRAMA_GPU_OPENCL_DEVICE")
        if not re.fullmatch(r"[1-4]", env.get("DRAMA_GPU_COMPOSITOR_LANES", "")):
            issues.append("invalid:DRAMA_GPU_COMPOSITOR_LANES")
        if not re.fullmatch(r"[1-4]", env.get("DRAMA_GPU_FILTER_THREADS", "")):
            issues.append("invalid:DRAMA_GPU_FILTER_THREADS")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{7,99}", env.get("DRAMA_GPU_RUNTIME_IDENTITY", "")):
            issues.append("invalid:DRAMA_GPU_RUNTIME_IDENTITY")
        try:
            chunk_seconds = int(env.get("DRAMA_GPU_CHUNK_SECONDS", ""))
            if not 30 <= chunk_seconds <= 300:
                raise ValueError()
        except (TypeError, ValueError):
            issues.append("invalid:DRAMA_GPU_CHUNK_SECONDS")
        try:
            chunk_timeout = int(env.get("DRAMA_GPU_CHUNK_TIMEOUT", ""))
            if not 300 <= chunk_timeout <= 7200:
                raise ValueError()
        except (TypeError, ValueError):
            issues.append("invalid:DRAMA_GPU_CHUNK_TIMEOUT")
        kernel = ROOT / "features" / "drama_synthesis" / "opencl" / "random_overlay_v2.cl"
        try:
            kernel_valid = (
                not kernel.is_symlink()
                and kernel.is_file()
                and 1024 <= kernel.stat().st_size <= 128 * 1024
            )
        except OSError:
            kernel_valid = False
        if not kernel_valid:
            issues.append("invalid:DRAMA_GPU_COMPOSITOR_KERNEL")
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


def compositor_capability_issues(env, runner=subprocess.run):
    """Exercise the actual OpenCL device and NVENC once without writing media."""
    if env.get("DRAMA_GPU_COMPOSITOR_BACKEND") != "opencl_fused_v2":
        return []
    command = [
        env["DRAMA_FFMPEG"], "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-init_hw_device", "opencl=ocl:" + env["DRAMA_GPU_OPENCL_DEVICE"],
        "-filter_hw_device", "ocl", "-f", "lavfi", "-i", "color=black:size=256x256:rate=1",
        "-vf", "format=rgba,hwupload,hwdownload,format=rgba", "-frames:v", "1",
        "-c:v", "h264_nvenc", "-rgb_mode", "yuv420", "-pix_fmt", "yuv420p", "-f", "null", "-",
    ]
    try:
        result = runner(command, check=False, capture_output=True, text=True, timeout=30)
    except Exception:
        return ["gpu_compositor_capability_check_failed"]
    return [] if result.returncode == 0 else ["gpu_compositor_capability_check_failed"]


def compositor_pipeline_issues(env, asset_set, runner=subprocess.run, probe=None):
    """Compile and run one real five-input fused frame sequence, then verify it."""
    if env.get("DRAMA_GPU_COMPOSITOR_BACKEND") != "opencl_fused_v2":
        return []
    try:
        from features.drama_synthesis.composition import (
            CANVAS_FPS, CANVAS_HEIGHT, CANVAS_WIDTH, canonical_json,
            compile_random_overlay_spec, plan_chunks,
        )
        from features.drama_synthesis.core import RECIPE_PROFILE
        from features.drama_synthesis import gpu_compositor

        probe = probe or gpu_compositor._probe
        with tempfile.TemporaryDirectory(
            prefix="drama-compositor-preflight-", dir=env["TMPDIR"]
        ) as directory:
            work = Path(directory)
            source = work / "source.mp4"
            output = work / "output.mp4"
            kernel_path = work / "kernel.cl"
            create = [
                env["DRAMA_FFMPEG"], "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i",
                "testsrc2=size=%dx%d:rate=%d:duration=1" % (CANVAS_WIDTH, CANVAS_HEIGHT, CANVAS_FPS),
                "-an", "-c:v", "mpeg4", "-q:v", "5", "-pix_fmt", "yuv420p", str(source),
            ]
            created = runner(create, check=False, capture_output=True, text=True, timeout=60)
            if created.returncode != 0 or not source.is_file() or source.stat().st_size <= 0:
                return ["gpu_compositor_pipeline_check_failed"]
            assets = {}
            rows = {}
            durations = {}
            for category in ("border", "opacity_video", "corners", "tint"):
                item = asset_set["categories"][category][0]
                rows[category] = {
                    key: item[key] for key in ("media_type", "name", "sha256", "size")
                }
                assets[category] = Path(item["path"])
                if item["media_type"] == "video/webm":
                    durations[category] = float(probe(env["DRAMA_FFPROBE"], assets[category])["duration"])
            unsigned = {
                "profile": RECIPE_PROFILE,
                "version": 1,
                "source": "preflight",
                "assets": rows,
                "asset_set_sha256": asset_set["manifest_sha256"],
                "rotation_millidegrees": 0,
                "scale_bp": 10000,
                "tint_opacity_bp": 500,
            }
            unsigned["recipe_sha256"] = hashlib.sha256(
                canonical_json(unsigned).encode("utf-8")
            ).hexdigest()
            source_info = dict(probe(env["DRAMA_FFPROBE"], source))
            spec = compile_random_overlay_spec(unsigned, source_info)
            kernel = gpu_compositor.compile_opencl_kernel(spec)
            kernel_path.write_text(kernel["source"], encoding="utf-8")
            chunk = plan_chunks(spec["timeline"]["total_frames"], seconds=30)[0]
            command = gpu_compositor.build_opencl_chunk_command(
                ffmpeg=env["DRAMA_FFMPEG"], source=source, output=output, spec=spec,
                assets=assets,
                asset_media_types={key: rows[key]["media_type"] for key in rows},
                asset_durations=durations, chunk=chunk, kernel_path=kernel_path,
                device=env["DRAMA_GPU_OPENCL_DEVICE"],
            )
            rendered = runner(command, check=False, capture_output=True, text=True, timeout=120)
            if rendered.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
                return ["gpu_compositor_pipeline_check_failed"]
            info = dict(probe(env["DRAMA_FFPROBE"], output))
            if not gpu_compositor._video_contract(
                info, spec["timeline"]["duration_seconds"],
                expected_frames=spec["timeline"]["total_frames"], audio=False,
            ):
                return ["gpu_compositor_pipeline_check_failed"]
    except Exception:
        return ["gpu_compositor_pipeline_check_failed"]
    return []


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
    issues.extend(release_identity_issues(os.environ))
    if not issues:
        issues.extend(storage_issues(os.environ))
    if platform.python_version() != PYTHON_VERSION:
        issues.append("python_version_mismatch")
    issues.extend(package_issues(direct_requirements()))
    model_hashes = {}
    asset_set = None
    compositor_pipeline_checked = False
    runtime_fingerprint = {}
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
            asset_set = load_asset_set(
                Path(os.environ["DRAMA_RANDOM_OVERLAY_ROOT"]),
                os.environ["DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256"],
            )
        except Exception:
            issues.append("isolated_asset_set_invalid")
    if not issues:
        issues.extend(compositor_capability_issues(os.environ))
    if not issues and asset_set is not None:
        issues.extend(compositor_pipeline_issues(os.environ, asset_set))
        compositor_pipeline_checked = os.environ.get("DRAMA_GPU_COMPOSITOR_BACKEND") == "opencl_fused_v2"
    if not issues and os.environ.get("DRAMA_GPU_COMPOSITOR_BACKEND") == "opencl_fused_v2":
        try:
            from features.drama_synthesis.gpu_compositor import _runtime_fingerprint
            runtime_fingerprint = _runtime_fingerprint(os.environ["DRAMA_FFMPEG"])
        except Exception:
            issues.append("gpu_runtime_fingerprint_check_failed")
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
        "runtime_fingerprint": runtime_fingerprint,
        "cuda_tested": False,
        "compositor_pipeline_checked": compositor_pipeline_checked and not issues,
        "app_import_checked": bool(args.check_app_import and not issues),
    }, sort_keys=True))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
