#!/usr/bin/env python3
"""TT migration checks/configuration. Never submits, reconciles, or starts jobs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

BASE = Path("/data/tt-post-gpu")
STATE = Path("/data/tt-post-publisher")
EXPECTED_UUID = "659e6f89-71fa-463d-842e-ccdf2c06e0fe"
SOURCE_COMMIT = "9425b39fa45390b3dc107f353dc6ef436415365d"
SOURCE_HASHES = {
    "scripts/tt_gpu_worker.py": "b14783bbbff98aa9886c081da501d892243bdf85d617811d9f9203b652ad3198",
    "features/tt_gpu/worker.py": "19562d121653e905aed9ff8325cd7d975f7c836196151a468730740232575f28",
    "features/tt_gpu/credentials.py": "bc24e0fbad4078863df234b2151ebe02a5195c625c85b8aa865cff1738f89009",
    "features/tt_gpu/random_overlay.py": "7273ed1aedeb4fe41296852fd3717e5c9f2d88fb854883aa323178e3b3d1a60b",
}
UNITS = ("tt-gpu-publisher.service", "tt-gpu-direct-outro.service")
RISK_STATES = {"init_inflight", "init_outcome_unknown", "initialized", "processing"}
TERMINAL_STATES = {"published", "failed", "init_rejected"}


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def atomic_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: dict) -> None:
    atomic_bytes(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def preflight(expected_uuid: str = EXPECTED_UUID, min_free_gib: int = 50) -> dict:
    if expected_uuid != EXPECTED_UUID or min_free_gib < 30:
        raise ValueError("unapproved filesystem UUID or insufficient free-space floor")
    if Path("/data").resolve() != Path("/data") or not Path("/data").is_dir():
        raise ValueError("/data must be the existing real directory")
    actual = subprocess.check_output(
        ["findmnt", "-T", "/data", "-n", "-o", "UUID"], text=True
    ).strip()
    if actual != expected_uuid:
        raise ValueError("filesystem UUID changed; do not write migration data")
    usage = shutil.disk_usage("/data")
    if usage.free < min_free_gib * 1024 ** 3:
        raise ValueError("insufficient free space; no root-directory fallback")
    return {"uuid": actual, "free_bytes": usage.free, "min_free_gib": min_free_gib}


def verify_source(root: Path) -> dict:
    hashes = {}
    for relative, expected in SOURCE_HASHES.items():
        source = root / relative
        actual = hashlib.sha256(source.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        if actual != expected:
            raise ValueError("production source differs: " + relative)
        hashes[relative] = actual
    return {"source_commit": SOURCE_COMMIT, "normalized_sha256": hashes}


def read_env(path: Path) -> dict:
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, raw = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("invalid environment file syntax")
        words = shlex.split(raw, comments=False, posix=True)
        if len(words) > 1:
            raise ValueError("environment value must be quoted when it contains spaces")
        values[key] = words[0] if words else ""
    return values


def closed_environment(source: dict, direct: dict) -> tuple[dict, dict]:
    base = dict(source)
    base.update({
        "TT_POST_GPU_ENABLED": "1",
        "TT_POST_GPU_HOST": "127.0.0.1",
        "TT_POST_GPU_PORT": "8830",
        "TT_POST_GPU_WORK_ROOT": str(STATE),
        "TT_POST_GPU_FIXED_OUTRO_PATH": str(STATE / "assets/TT-new-outro.mp4"),
        "TT_POST_GPU_LOGO_PATH": str(STATE / "assets/dramawave-logo-rounded.png"),
        "TT_POST_GPU_FONT_FILE": str(BASE / "assets/DejaVuSans-Bold.ttf"),
        "TT_POST_GPU_FFMPEG_BIN": str(BASE / "ffmpeg/ffmpeg"),
        "TT_POST_GPU_FFPROBE_BIN": str(BASE / "ffmpeg/ffprobe"),
        "TT_POST_GPU_RANDOM_OVERLAY_ROOT": str(STATE / "random-overlay-assets/v1"),
        "TT_POST_GPU_STORAGE_BACKEND": "cos",
        "TT_POST_GPU_MEDIA_MODE": "random_overlay",
        "TT_POST_GPU_LOCAL_MEDIA_ORIGIN": "",
        "TT_POST_LIVE_ENABLED": "0",
        "TT_POST_MANUAL_CANARY_ENABLED": "0",
        "TMPDIR": str(BASE / "tmp"),
        "XDG_CACHE_HOME": str(BASE / "cache"),
        "CUDA_CACHE_PATH": str(BASE / "cache/cuda"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    # Only move local paths. The verified COS pull origin and frozen media URLs
    # must remain unchanged. Never turn a host migration into a storage switch.
    outro = dict(direct)
    outro.update({
        "TT_POST_GPU_PORT": "8832",
        "TT_POST_GPU_MEDIA_MODE": "direct_outro",
        "TT_POST_GPU_WORK_ROOT": str(STATE / "direct-outro-work"),
        "TT_POST_LIVE_ENABLED": "0",
        "TT_POST_MANUAL_CANARY_ENABLED": "0",
    })
    return base, outro


def env_bytes(values: dict) -> bytes:
    for key, value in values.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or any(char in str(value) for char in ("\n", "\r", "\x00")):
            raise ValueError("invalid or multiline environment value")
    return "".join(
        "%s=%s\n" % (key, shlex.quote(str(value)))
        for key, value in sorted(values.items())
    ).encode()


def validate_closed_environment(base: dict, direct: dict, secrets: dict) -> None:
    protected = {"TT_POST_LIVE_ENABLED", "TT_POST_MANUAL_CANARY_ENABLED", "TT_POST_GPU_HOST",
                 "TT_POST_GPU_PORT", "TT_POST_GPU_WORK_ROOT", "TT_POST_GPU_STORAGE_BACKEND",
                 "TT_POST_GPU_MEDIA_MODE", "TT_POST_GPU_FFMPEG_BIN", "TT_POST_GPU_FFPROBE_BIN",
                 "TT_POST_GPU_LOCAL_MEDIA_ORIGIN", "TMPDIR", "XDG_CACHE_HOME", "CUDA_CACHE_PATH"}
    for override in ({}, direct):
        expected = {**base, **override}
        actual = {**base, **secrets, **override}
        if any(actual.get(key) != expected.get(key) for key in protected):
            raise ValueError("secret environment overrides isolated settings")


def run_backup_root(run_id: str) -> Path:
    if not re.fullmatch(r"gpu-service-migration-[0-9]{8}T[0-9]{4}", run_id):
        raise ValueError("invalid migration run ID")
    return Path("/data/migrations") / run_id / "tt"


def configure(run_id: str) -> dict:
    preflight()
    backup_root = run_backup_root(run_id)
    incoming = backup_root / "source-config"
    base, direct = closed_environment(
        read_env(incoming / "tt-post-gpu.env"),
        read_env(incoming / "tt-post-gpu-direct-outro.env"),
    )
    validate_closed_environment(base, direct, read_env(incoming / "tt-post-gpu.secrets"))
    contents = {
        "base.env": env_bytes(base),
        "direct-outro.env": env_bytes(direct),
        "secrets.env": (incoming / "tt-post-gpu.secrets").read_bytes(),
    }
    backup = backup_root / "target-config-before"
    manifest_path = backup / "manifest.json"
    if manifest_path.exists():
        raise ValueError("configuration checkpoint exists; inspect instead of overwriting")
    manifest = {"files": {}}
    for name in contents:
        target = BASE / "config" / name
        if target.is_symlink():
            raise ValueError("refusing symlink config")
        item = {"existed": target.exists()}
        if target.exists():
            raw = target.read_bytes()
            atomic_bytes(backup / name, raw)
            item["sha256"] = hashlib.sha256(raw).hexdigest()
        manifest["files"][name] = item
    atomic_json(manifest_path, manifest)
    for name, raw in contents.items():
        atomic_bytes(BASE / "config" / name, raw)
    for relative in ["tmp", "cache/cuda", "logs", "validation", "ops"]:
        (BASE / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
    return {
        "configured": True,
        "live_enabled": False,
        "manual_canary_enabled": False,
        "secrets_sha256": hashlib.sha256(contents["secrets.env"]).hexdigest(),
    }


def snapshot(root: Path) -> dict:
    result = {"files": {}, "lanes": {}, "risk": []}
    for lane, base in [("random_overlay", root), ("direct_outro", root / "direct-outro-work")]:
        counters = {}
        for name in ["manifests", "publishes"]:
            folder = base / name
            if not folder.is_dir() or folder.is_symlink():
                raise ValueError("missing or redirected state directory: " + str(folder))
            states = Counter()
            for file in sorted(folder.glob("*.json")):
                if file.is_symlink() or not file.is_file():
                    raise ValueError("nonregular state file")
                raw = file.read_bytes()
                value = json.loads(raw)
                state = str(value.get("state", value.get("status", "")))
                if not state:
                    raise ValueError("state file lacks status")
                states[state] += 1
                relative = str(file.relative_to(root))
                result["files"][relative] = hashlib.sha256(raw).hexdigest()
                if name == "publishes":
                    if value.get("job_id") != file.stem:
                        raise ValueError("ledger job identity mismatch")
                    if state in RISK_STATES or state not in TERMINAL_STATES:
                        result["risk"].append({"lane": lane, "job_id": file.stem, "state": state})
            counters[name] = dict(states)
        result["lanes"][lane] = counters
    result["file_count"] = len(result["files"])
    result["fingerprint"] = hashlib.sha256(
        json.dumps(result["files"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


def restore_config(run_id: str) -> dict:
    preflight()
    for unit in UNITS:
        state = subprocess.check_output(
            ["systemctl", "show", unit, "--property=ActiveState", "--value"], text=True
        ).strip()
        if state not in {"inactive", "failed", ""}:
            raise ValueError("target is not stopped: " + unit)
    backup = run_backup_root(run_id) / "target-config-before"
    manifest = json.loads((backup / "manifest.json").read_text())
    if set(manifest["files"]) != {"base.env", "direct-outro.env", "secrets.env"}:
        raise ValueError("unexpected rollback scope")
    for name, item in manifest["files"].items():
        target = BASE / "config" / name
        if item["existed"]:
            raw = (backup / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError("rollback config checksum mismatch")
            atomic_bytes(target, raw)
        elif target.exists():
            target.unlink()
    return {"config_restored": True, "ledgers_restored": False, "services_started": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight")
    pre.add_argument("--expected-uuid", default=EXPECTED_UUID)
    pre.add_argument("--min-free-gib", type=int, default=50)
    source = sub.add_parser("verify-source")
    source.add_argument("--root", type=Path, default=BASE / "current")
    for command in ["configure", "restore-config"]:
        sub.add_parser(command).add_argument("--run-id", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--root", type=Path, default=STATE)
    snap.add_argument("--output", type=Path, required=True)
    snap.add_argument("--require-idle", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            result = preflight(args.expected_uuid, args.min_free_gib)
        elif args.command == "verify-source":
            result = verify_source(args.root)
        elif args.command == "configure":
            result = configure(args.run_id)
        elif args.command == "restore-config":
            result = restore_config(args.run_id)
        else:
            result = snapshot(args.root)
            atomic_json(args.output, result)
            if args.require_idle and result["risk"]:
                raise ValueError("nonterminal/unknown publish ledger; preserve and drain")
            result = {key: value for key, value in result.items() if key != "files"}
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        # Do not print config values or arbitrary upstream errors.
        print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, ValueError) else "migration check failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
