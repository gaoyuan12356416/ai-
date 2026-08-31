#!/usr/bin/env python3
"""Normalize the stopped HK TT target release to exact Git file modes."""

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path


HOST = "VM-0-125-centos"
UUID = "659e6f89-71fa-463d-842e-ccdf2c06e0fe"
TARGET = "d05adad41a28383a5c9685e6b75c1c8581a2aa49"
MANIFEST_SHA256 = "b23950accbb12afd78ee36afd0b9387f6d84363ab8c371130076e0fc1153b2de"
UNITS = (
    "tt-gpu-publisher.service",
    "tt-gpu-direct-outro.service",
    "tt-gpu-reverse-tunnel.service",
    "tt-gpu-direct-outro-reverse-tunnel.service",
)


def run(args):
    return subprocess.check_output(
        args, universal_newlines=True, timeout=10
    ).strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_and_validate(manifest_path, release):
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("release manifest is unsafe")
    if sha(manifest_path) != MANIFEST_SHA256:
        raise RuntimeError("release manifest digest mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != 1 or manifest.get("source_commit") != TARGET:
        raise RuntimeError("release manifest identity mismatch")
    entries = manifest.get("entries")
    if not isinstance(entries, dict) or len(entries) != manifest.get("entry_count"):
        raise RuntimeError("release manifest is incomplete")
    actual = {}
    for path in sorted(release.rglob("*")):
        relative = path.relative_to(release).as_posix()
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise RuntimeError("release contains a redirected or special member")
        if path.is_file():
            if path.stat().st_uid != 0 or path.stat().st_gid != 0:
                raise RuntimeError("release file ownership mismatch")
            raw = path.read_bytes()
            actual[relative] = {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
    expected = {
        relative: {"sha256": item["sha256"], "size": item["size"]}
        for relative, item in entries.items()
    }
    if actual != expected:
        raise RuntimeError("release content differs from GitHub manifest")
    return entries


def atomic_json(path, value):
    if path.exists() or path.is_symlink():
        raise FileExistsError("permission report already exists")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".tt-release-mode-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    release = args.release.resolve(strict=True)
    expected_release = Path("/data/tt-post-gpu/releases") / TARGET
    if run(["hostname"]) != HOST or run(["findmnt", "-n", "-o", "UUID", "-T", "/data"]) != UUID:
        raise RuntimeError("HK data disk guard failed")
    if release != expected_release or args.release.is_symlink():
        raise RuntimeError("unexpected target release")
    if release.stat().st_uid != 0 or release.stat().st_gid != 0:
        raise RuntimeError("target release ownership mismatch")
    report = args.report.resolve(strict=False)
    report_root = Path(
        "/data/migrations/gpu-service-migration-20260828T1502/tt"
    ).resolve(strict=True)
    if report.parent != report_root:
        raise RuntimeError("permission report is outside the migration data directory")
    if Path("/data/tt-post-gpu/current").resolve() == release:
        raise RuntimeError("target release is current")
    for unit in UNITS:
        if run(["systemctl", "show", "-p", "ActiveState", "--value", unit]) != "inactive":
            raise RuntimeError("TT unit is not stopped")
        if run(["systemctl", "show", "-p", "MainPID", "--value", unit]) != "0":
            raise RuntimeError("TT unit still has a process")
    entries = load_and_validate(args.manifest.resolve(strict=True), release)
    for path in sorted((p for p in release.rglob("*") if p.is_dir()), reverse=True):
        if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_gid != 0:
            raise RuntimeError("release directory ownership mismatch")
        path.chmod(0o755)
    release.chmod(0o755)
    for relative, item in entries.items():
        mode = item["mode"]
        if mode not in {"100644", "100755"}:
            raise RuntimeError("unsupported Git release mode")
        (release / relative).chmod(0o755 if mode == "100755" else 0o644)
    entries = load_and_validate(args.manifest.resolve(strict=True), release)
    for relative, item in entries.items():
        expected_mode = 0o755 if item["mode"] == "100755" else 0o644
        if stat.S_IMODE((release / relative).stat().st_mode) != expected_mode:
            raise RuntimeError("release mode verification failed")
    atomic_json(report, {
        "schema": 1,
        "result": "permissions_normalized",
        "target_sha": TARGET,
        "entry_count": len(entries),
        "manifest_sha256": MANIFEST_SHA256,
        "services_started": False,
    })
    print(json.dumps({"ok": True, "report": str(report)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
