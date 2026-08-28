#!/usr/bin/env python3
"""Frozen TT state export/import. No merges, publication calls, or service starts."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import tarfile
import time
from pathlib import Path, PurePosixPath

from gate_handoff import TUNNELS, require_stopped
from tt_migration import (
    BASE, STATE, SOURCE_COMMIT, UNITS, atomic_json, digest, preflight, read_env,
    run_backup_root, snapshot, verify_source,
)

FOLDERS = ("manifests", "publishes", "direct-outro-work/manifests", "direct-outro-work/publishes")
CONFIG_NAMES = ("tt-post-gpu.env", "tt-post-gpu-direct-outro.env", "tt-post-gpu.secrets")
MAX_BYTES = 512 * 1024 * 1024


def checked_sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("a SHA256 is required")
    return value


def state_file_name(value: str) -> bool:
    path = PurePosixPath(value)
    return (str(path) == value and "\\" not in value and ".." not in path.parts
            and str(path.parent) in FOLDERS and path.suffix == ".json")


def stable_snapshot(root: Path) -> dict:
    for relative in FOLDERS:
        folder = root / relative
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError("missing or redirected state directory")
        if any(not item.is_file() or item.is_symlink() or item.suffix != ".json" for item in folder.iterdir()):
            raise ValueError("state directory has an unexpected non-JSON entry")
    result = snapshot(root)
    # POSIX names make evidence independent of the operator workstation OS.
    result["files"] = {name.replace("\\", "/"): sha for name, sha in result["files"].items()}
    result["fingerprint"] = hashlib.sha256(json.dumps(
        result["files"], sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    if result["risk"]:
        raise ValueError("nonterminal/unknown GPU publication facts block migration")
    return result


def assets(root: Path, font: Path) -> dict:
    result = {}
    for name in ("assets", "random-overlay-assets/v1"):
        folder = root / name
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError("asset directory missing or redirected")
        for path in sorted(folder.rglob("*")):
            if path.is_symlink():
                raise ValueError("asset symlink is not permitted")
            if path.is_file():
                result[path.relative_to(root).as_posix()] = {"bytes": path.stat().st_size, "sha256": digest(path)}
    if font.is_symlink() or not font.is_file():
        raise ValueError("font missing or redirected")
    result["font/DejaVuSans-Bold.ttf"] = {"bytes": font.stat().st_size, "sha256": digest(font)}
    return result


def config_hashes(root: Path) -> dict:
    if any((root / name).is_symlink() for name in CONFIG_NAMES):
        raise ValueError("source configuration symlink is not permitted")
    return {name: digest(root / name) for name in CONFIG_NAMES}


def cpu_backup(raw: bytes, expected_sha: str, run_id: str) -> dict:
    if hashlib.sha256(raw).hexdigest() != checked_sha(expected_sha):
        raise ValueError("CPU online-backup manifest SHA256 differs")
    value = json.loads(raw)
    databases = value.get("databases", {})
    if (value.get("run_id") != run_id or value.get("ok") is not True
            or value.get("publication_facts_stable") is not True
            or value.get("ingress_gate_confirmed") is not True or len(databases) != 2
            or any(db.get("quick_check") != "ok" for db in databases.values())):
        raise ValueError("CPU online-backup evidence is not a successful frozen backup")
    for database in databases.values():
        checked_sha(database.get("sha256", ""))
    return value


def require_source_fenced() -> dict:
    if socket.gethostname() != "VM-0-13-centos":
        raise ValueError("export is restricted to the US source")
    mount = subprocess.check_output(["findmnt", "-T", "/data", "-n", "-o", "TARGET"], text=True).strip()
    if mount != "/data":
        raise ValueError("US data mount is absent")
    states = {}
    for unit in (*UNITS, *TUNNELS):
        output = subprocess.check_output(["systemctl", "show", unit, "-p", "ActiveState",
                                          "-p", "UnitFileState", "-p", "MainPID"], text=True)
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if (values.get("ActiveState") not in {"inactive", "failed"}
                or values.get("UnitFileState") != "masked" or values.get("MainPID") != "0"):
            raise ValueError("US worker/tunnel is not persistently fenced: " + unit)
        states[unit] = values
    return states


def export_state(run_id: str, cpu_manifest: Path, cpu_manifest_sha256: str, checkpoint: Path) -> dict:
    root = run_backup_root(run_id)
    fenced = require_source_fenced()
    verify_source(Path("/opt/tt-post-gpu/current"))
    raw_cpu = cpu_manifest.read_bytes()
    cpu_backup(raw_cpu, cpu_manifest_sha256, run_id)
    proof = json.loads(checkpoint.read_text())
    if (proof.get("run_id") != run_id or proof.get("group") != "tt"
            or not 0 <= time.time() - float(proof.get("checked_at_epoch", 0)) <= 300
            or any(proof.get(name) is not True for name in
                   ("new_admission_closed", "triggers_paused", "cpu_drained", "no_unknown"))):
        raise ValueError("coordinator drain checkpoint is stale or incomplete")
    before = stable_snapshot(STATE)
    source_env = read_env(Path("/etc/tt-post-gpu.env"))
    font = Path(source_env.get("TT_POST_GPU_FONT_FILE", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"))
    source_assets = assets(STATE, font)
    source_configs = config_hashes(Path("/etc"))
    source_binary = Path(source_env["TT_POST_GPU_FFMPEG_BIN"])
    index = {
        "schema_version": 1, "run_id": run_id, "source_commit": SOURCE_COMMIT,
        "source_host": "43.166.178.132", "at_epoch": time.time(), "state": before,
        "assets": source_assets, "source_config_sha256": source_configs,
        "source_ffmpeg_sha256": digest(source_binary), "source_fenced": fenced,
        "cpu_backup_manifest_sha256": checked_sha(cpu_manifest_sha256),
        "cpu_backup_manifest_json": raw_cpu.decode("utf-8"),
        "coordinator_checkpoint_sha256": digest(checkpoint),
    }
    destination = root / "final-source"
    destination.mkdir(mode=0o700, exist_ok=False)
    atomic_json(destination / "state-index.json", index)
    archive = destination / "tt-final-state.tar.gz"
    total = 0
    with archive.open("xb") as target:
        os.fchmod(target.fileno(), 0o600)
        with tarfile.open(fileobj=target, mode="w:gz", compresslevel=1) as tar:
            for name, expected in sorted(before["files"].items()):
                if not state_file_name(name):
                    raise ValueError("unexpected state filename")
                path = STATE / name
                raw = path.read_bytes()
                total += len(raw)
                if len(raw) > 16 * 1024 * 1024 or total > MAX_BYTES:
                    raise ValueError("state export exceeds bounded archive limits")
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError("source state changed during final export")
                member = tarfile.TarInfo(name)
                member.size, member.mode, member.mtime = len(raw), 0o600, int(path.stat().st_mtime)
                tar.addfile(member, io.BytesIO(raw))
            raw = (destination / "state-index.json").read_bytes()
            member = tarfile.TarInfo("state-index.json")
            member.size, member.mode = len(raw), 0o600
            tar.addfile(member, io.BytesIO(raw))
        target.flush()
        os.fsync(target.fileno())
    after = stable_snapshot(STATE)
    if (after != before or require_source_fenced() != fenced
            or assets(STATE, font) != source_assets or config_hashes(Path("/etc")) != source_configs):
        raise ValueError("source facts changed during final export; archive not approved")
    receipt = {
        "ok": True, "archive": str(archive), "bytes": archive.stat().st_size, "sha256": digest(archive),
        "state_index_sha256": digest(destination / "state-index.json"),
        "state_fingerprint": before["fingerprint"], "file_count": before["file_count"],
        "cpu_backup_manifest_sha256": cpu_manifest_sha256,
        "coordinator_checkpoint_sha256": index["coordinator_checkpoint_sha256"],
        "source_commit": SOURCE_COMMIT, "source_fenced": True,
    }
    atomic_json(destination / "export-receipt.json", receipt)
    return receipt


def unpack_verified(archive: Path, staging: Path, run_id: str, archive_sha256: str,
                    cpu_manifest_sha256: str, expected_fingerprint: str) -> dict:
    if archive.is_symlink() or digest(archive) != checked_sha(archive_sha256):
        raise ValueError("final state archive SHA256 differs")
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        names = [member.name for member in members]
        if (len(members) > 100001 or len(names) != len(set(names))
                or sum(member.size for member in members) > MAX_BYTES
                or any(not member.isfile() or member.size > 32 * 1024 * 1024 for member in members)
                or names.count("state-index.json") != 1):
            raise ValueError("unsupported or duplicate final archive entries")
        index = json.load(tar.extractfile("state-index.json"))
        if (index.get("schema_version") != 1 or index.get("run_id") != run_id
                or index.get("source_commit") != SOURCE_COMMIT or index.get("source_host") != "43.166.178.132"):
            raise ValueError("final state index scope differs")
        cpu_backup(index["cpu_backup_manifest_json"].encode(), cpu_manifest_sha256, run_id)
        if index.get("cpu_backup_manifest_sha256") != cpu_manifest_sha256:
            raise ValueError("final state refers to another CPU backup")
        files = index["state"]["files"]
        if (any(not state_file_name(name) for name in files)
                or set(names) != set(files) | {"state-index.json"}
                or index["state"].get("risk") or index["state"].get("fingerprint") != checked_sha(expected_fingerprint)):
            raise ValueError("archive file set or publication state differs")
        computed = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if computed != expected_fingerprint:
            raise ValueError("indexed file set does not reproduce the expected fingerprint")
        staging.mkdir(parents=True, mode=0o700, exist_ok=False)
        for name in FOLDERS:
            (staging / name).mkdir(parents=True, mode=0o700)
        for name, expected in files.items():
            raw = tar.extractfile(name).read()
            if hashlib.sha256(raw).hexdigest() != checked_sha(expected):
                raise ValueError("final archive file checksum differs")
            with (staging / name).open("xb") as stream:
                if hasattr(os, "fchmod"):
                    os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
    actual = stable_snapshot(staging)
    if actual["files"] != files or actual["fingerprint"] != expected_fingerprint:
        raise ValueError("extracted final state differs from the source file set")
    return index


def replace_directories(staging: Path, target: Path, backup: Path) -> None:
    # Sources are independently validated before this function. Directory moves
    # preserve the entire old set; overlay copying would retain stale precopy JSON.
    for name in FOLDERS:
        current = target / name
        if current.is_symlink() or not current.is_dir():
            raise ValueError("refusing redirected or absent target state directory")
    for name in FOLDERS:
        old = backup / name
        old.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if old.exists() or old.is_symlink():
            raise ValueError("old target state checkpoint already exists")
        os.rename(target / name, old)
        os.rename(staging / name, target / name)


def import_state(run_id: str, archive: Path, archive_sha256: str,
                 cpu_manifest_sha256: str, fingerprint: str) -> dict:
    preflight()
    verify_source(BASE / "current")
    require_stopped()
    base, secret, direct = (read_env(BASE / "config" / name) for name in
                            ("base.env", "secrets.env", "direct-outro.env"))
    for values in ({**base, **secret}, {**base, **secret, **direct}):
        if any(values.get(name) != "0" for name in ("TT_POST_LIVE_ENABLED", "TT_POST_MANUAL_CANARY_ENABLED")):
            raise ValueError("final import requires both target publishing gates closed")
    root = run_backup_root(run_id)
    backup = root / "final-import-before"
    if backup.exists():
        raise ValueError("final import checkpoint exists; inspect rather than overwriting")
    staging = BASE / "final-state-staging" / run_id
    index = unpack_verified(archive, staging, run_id, archive_sha256, cpu_manifest_sha256, fingerprint)
    if (assets(STATE, BASE / "assets/DejaVuSans-Bold.ttf") != index["assets"]
            or config_hashes(root / "source-config") != index["source_config_sha256"]):
        raise ValueError("source assets or source configuration changed after precopy")
    original_ffmpeg = root / "us-ffmpeg-before-compatibility/ffmpeg"
    if digest(original_ffmpeg) != index["source_ffmpeg_sha256"]:
        raise ValueError("preserved US FFmpeg does not match final source provenance")
    before = stable_snapshot(STATE)
    backup.mkdir(mode=0o700)
    atomic_json(backup / "before.json", before)
    atomic_json(backup / "state-index.json", index)
    require_stopped()
    replace_directories(staging, STATE, backup / "state")
    after = stable_snapshot(STATE)
    if after["files"] != index["state"]["files"] or after["fingerprint"] != fingerprint:
        raise ValueError("import verification failed; keep target stopped and preserve checkpoints")
    require_stopped()
    receipt = {
        "ok": True, "state_fingerprint": after["fingerprint"], "file_count": after["file_count"],
        "archive_sha256": archive_sha256, "cpu_backup_manifest_sha256": cpu_manifest_sha256,
        "source_commit": SOURCE_COMMIT, "old_state_backup": str(backup / "state"),
        "source_config_sha256": index["source_config_sha256"],
        "target_runtime_sha256": {name: digest(BASE / "ffmpeg" / name) for name in ("ffmpeg", "ffmpeg.bin", "ffprobe")},
        "state_file_set_exact": True, "assets_exact": True, "services_started": False,
        "cpu_database_imported": False,
    }
    atomic_json(root / "final-target-receipt.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--run-id", required=True)
    export.add_argument("--cpu-backup-manifest", type=Path, required=True)
    export.add_argument("--cpu-backup-manifest-sha256", required=True)
    export.add_argument("--coordinator-checkpoint", type=Path, required=True)
    incoming = sub.add_parser("import")
    incoming.add_argument("--run-id", required=True)
    incoming.add_argument("--archive", type=Path, required=True)
    incoming.add_argument("--archive-sha256", required=True)
    incoming.add_argument("--cpu-backup-manifest-sha256", required=True)
    incoming.add_argument("--state-fingerprint", required=True)
    args = parser.parse_args()
    try:
        if args.command == "export":
            result = export_state(args.run_id, args.cpu_backup_manifest, args.cpu_backup_manifest_sha256,
                                  args.coordinator_checkpoint)
        else:
            result = import_state(args.run_id, args.archive, args.archive_sha256,
                                  args.cpu_backup_manifest_sha256, args.state_fingerprint)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, ValueError) else "final state handoff failed; remain fenced"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
