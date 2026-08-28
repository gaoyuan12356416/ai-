#!/usr/bin/env python3
"""Install private CA paths for stopped HK TT workers. Never starts services."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
import verify_trust
from tt_migration import atomic_bytes, atomic_json, digest, preflight, read_env, run_backup_root

HOSTNAME = "VM-0-125-centos"
BASE = Path("/data/tt-post-gpu")
SYSTEMD = Path("/etc/systemd/system")
WORKERS = ("tt-gpu-publisher.service", "tt-gpu-direct-outro.service")
UNITS = WORKERS + ("tt-gpu-reverse-tunnel.service", "tt-gpu-direct-outro-reverse-tunnel.service")
DROPIN = ("[Service]\n"
          'Environment="SSL_CERT_FILE=/data/tt-post-gpu/trust/ca-bundle.pem"\n'
          'Environment="SSL_CERT_DIR=/data/tt-post-gpu/trust/certs"\n'
          "ReadOnlyPaths=/data/tt-post-gpu/trust\n"
          "ExecStartPre=/data/tt-post-gpu/runtime/bin/python /data/tt-post-gpu/ops/verify_trust.py\n")


def require_stopped() -> dict:
    states = {}
    for unit in UNITS:
        raw = subprocess.check_output(["systemctl", "show", unit, "-p", "LoadState", "-p", "ActiveState",
                                       "-p", "SubState", "-p", "MainPID"], text=True, timeout=10)
        value = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        if (value.get("LoadState") != "loaded" or value.get("ActiveState") not in {"inactive", "failed"}
                or value.get("MainPID") != "0" or value.get("SubState") in {"start", "stop", "auto-restart"}):
            raise ValueError("all four TT workers/tunnels must be stopped")
        states[unit] = value
    return states


def reject_environment_overrides(config: Path) -> dict:
    hashes = {}
    for name in ("base.env", "direct-outro.env", "secrets.env"):
        path = config / name
        verify_trust.no_symlinks(path)
        if any(key.startswith("SSL_CERT_") for key in read_env(path)):
            raise ValueError("existing TT environment overrides SSL_CERT_*; refuse installation")
        hashes[name] = digest(path)
    return hashes


def install(run_id: str, commit: str, source: Path) -> dict:
    if socket.gethostname() != HOSTNAME or os.geteuid() != 0:
        raise ValueError("installation is restricted to root on the approved HK host")
    if not re.fullmatch("[0-9a-f]{40}", commit):
        raise ValueError("an exact published operations commit is required")
    root = run_backup_root(run_id)
    here = Path(__file__).resolve().parent
    expected = root.parent / "checkouts" / commit / "ops/gpu-service-migration-20260828/tt"
    if here != expected:
        raise ValueError("run only from the exact deployed GitHub checkout")
    disk = preflight()
    stopped = require_stopped()
    env_hashes = reject_environment_overrides(BASE / "config")
    ca = verify_trust.check_bundle(source)
    incoming = source.read_bytes()
    if hashlib.sha256(incoming).hexdigest() != verify_trust.CA_SHA256:
        raise ValueError("source CA changed during validation")
    files = {verify_trust.CA_FILE: incoming,
             BASE / "ops/verify_trust.py": (here / "verify_trust.py").read_bytes()}
    for unit in WORKERS:
        template = here / "units" / (unit.removesuffix(".service") + "-trust.conf")
        if template.read_text() != DROPIN:
            raise ValueError("worker trust drop-in differs from the reviewed contract")
        files[SYSTEMD / (unit + ".d") / "40-tt-private-trust.conf"] = DROPIN.encode()
    for path in (*files, verify_trust.CA_DIR, root):
        verify_trust.no_symlinks(path)
    if verify_trust.CA_DIR.exists() and (not verify_trust.CA_DIR.is_dir() or any(verify_trust.CA_DIR.iterdir())):
        raise ValueError("existing private certificate directory is not empty")
    backup = root / "target-trust-before"
    backup.mkdir(mode=0o700, exist_ok=False)
    originals = {}
    for number, path in enumerate(files):
        if path.exists():
            if not path.is_file():
                raise ValueError("installation destination is not a regular file")
            saved = backup / (str(number) + ".before")
            atomic_bytes(saved, path.read_bytes())
            originals[str(path)] = {"existed": True, "backup": str(saved), "sha256": digest(saved)}
        else:
            originals[str(path)] = {"existed": False}
    atomic_json(backup / "manifest.json", {"originals": originals, "env_sha256": env_hashes,
                                           "operations_commit": commit, "ca": ca, "units": stopped})
    require_stopped()
    verify_trust.TRUST.mkdir(mode=0o700, exist_ok=True)
    verify_trust.CA_DIR.mkdir(mode=0o700, exist_ok=True)
    for path, raw in files.items():
        atomic_bytes(path, raw, mode=0o644)
        if path.read_bytes() != raw:
            raise ValueError("installed trust file readback differs")
    env = {**os.environ, "SSL_CERT_FILE": str(verify_trust.CA_FILE), "SSL_CERT_DIR": str(verify_trust.CA_DIR)}
    verified = json.loads(subprocess.check_output([str(BASE / "runtime/bin/python"),
                          str(BASE / "ops/verify_trust.py")], env=env, text=True, timeout=20))
    if verified.get("ok") is not True:
        raise ValueError("installed private trust verification failed")
    subprocess.run(["systemd-analyze", "verify", *(str(SYSTEMD / unit) for unit in WORKERS)], check=True, timeout=30)
    subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)
    after = require_stopped()
    if reject_environment_overrides(BASE / "config") != env_hashes:
        raise ValueError("TT environment files changed during installation")
    result = {"ok": True, "operations_commit": commit, "hostname": HOSTNAME, "disk": disk,
              "ca": ca, "tls_verification": verified, "backup": str(backup),
              "installed_sha256": {str(path): digest(path) for path in files},
              "units_after": after, "services_started": False, "environment_files_unchanged": True}
    atomic_json(backup / "installed.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--ops-commit", required=True)
    parser.add_argument("--ca-file", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.run_id, args.ops_commit, args.ca_file), sort_keys=True))
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, ValueError) else "trust installation failed; keep workers stopped"}))
        raise SystemExit(1)
