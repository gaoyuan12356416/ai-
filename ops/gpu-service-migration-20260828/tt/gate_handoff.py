#!/usr/bin/env python3
"""Restore captured source gates on a stopped HK target. Never starts services."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from tt_migration import (
    BASE, STATE, SOURCE_COMMIT, UNITS, atomic_bytes, atomic_json,
    closed_environment, digest, env_bytes, preflight, read_env, run_backup_root,
    snapshot, validate_closed_environment, verify_source,
)

TUNNELS = ("tt-gpu-reverse-tunnel.service", "tt-gpu-direct-outro-reverse-tunnel.service")
GATES = ("TT_POST_LIVE_ENABLED", "TT_POST_MANUAL_CANARY_ENABLED",
         "TT_POST_DIRECT_AUDIT_APPROVED", "TT_POST_URL_PROPERTY_VERIFIED")
SOURCE_FILES = {"tt-post-gpu.env", "tt-post-gpu-direct-outro.env", "tt-post-gpu.secrets"}


def checked_source_gates(proof: dict, incoming: Path) -> dict:
    if proof.get("source_host") != "43.166.178.132" or proof.get("source_commit") != SOURCE_COMMIT:
        raise ValueError("gate evidence is not from the approved source release/host")
    hashes = proof.get("source_config_sha256", {})
    if set(hashes) != SOURCE_FILES:
        raise ValueError("source gate evidence has an unexpected config scope")
    for name, sha in hashes.items():
        if incoming.joinpath(name).is_symlink() or digest(incoming / name) != sha:
            raise ValueError("source config changed since gate capture")
    source = read_env(incoming / "tt-post-gpu.env")
    secrets = read_env(incoming / "tt-post-gpu.secrets")
    direct = read_env(incoming / "tt-post-gpu-direct-outro.env")
    lanes = proof.get("lanes", {})
    if set(lanes) != {"random_overlay", "direct_outro"}:
        raise ValueError("both source lanes are required")
    result = {}
    for lane, override in (("random_overlay", {}), ("direct_outro", direct)):
        entry = lanes[lane]
        values = entry.get("proc_gates", {})
        if (entry.get("match") is not True or set(values) != set(GATES)
                or any(type(value) is not bool for value in values.values())
                or values != entry.get("config_gates")):
            raise ValueError("running source gates do not match captured config")
        effective = {**source, **secrets, **override}
        if any(str(effective.get(key, "")).strip().lower() not in
               {"1", "true", "yes", "on", "0", "false", "no", "off", ""} for key in GATES):
            raise ValueError("invalid source gate boolean")
        captured = {key: str(effective.get(key, "")).strip().lower() in {"1", "true", "yes", "on"}
                    for key in GATES}
        if captured != values:
            raise ValueError("source environment does not reproduce the captured gates")
        result[lane] = {key: "1" if values[key] else "0" for key in GATES}
    return result


def require_stopped() -> None:
    for unit in (*UNITS, *TUNNELS):
        state = subprocess.check_output(
            ["systemctl", "show", unit, "--property=ActiveState", "--value"], text=True
        ).strip()
        if state not in {"inactive", "failed", ""}:
            raise ValueError("target worker/tunnel is not stopped: " + unit)


def restore_gates(run_id: str, source_gates_sha256: str, final_fingerprint: str) -> dict:
    for value in (source_gates_sha256, final_fingerprint):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("expected evidence SHA256 is required")
    preflight()
    verify_source(BASE / "current")
    require_stopped()
    root = run_backup_root(run_id)
    proof_file = root / "source-live-gates.json"
    if digest(proof_file) != source_gates_sha256:
        raise ValueError("source running-gate evidence SHA256 changed")
    incoming = root / "source-config"
    gates = checked_source_gates(json.loads(proof_file.read_text()), incoming)
    expected_base, expected_direct = closed_environment(
        read_env(incoming / "tt-post-gpu.env"), read_env(incoming / "tt-post-gpu-direct-outro.env")
    )
    expected = {"base.env": expected_base, "direct-outro.env": expected_direct}
    secrets = read_env(BASE / "config/secrets.env")
    validate_closed_environment(expected_base, expected_direct, secrets)
    if any(key in secrets for key in GATES):
        raise ValueError("secret file must not override any captured gate")
    if digest(BASE / "config/secrets.env") != digest(incoming / "tt-post-gpu.secrets"):
        raise ValueError("target credential file differs from captured source")
    before = {}
    for name, values in expected.items():
        target = BASE / "config" / name
        if target.is_symlink() or read_env(target) != values:
            raise ValueError("target must still match the closed, approved configuration")
        before[name] = target.read_bytes()
    state = snapshot(STATE)
    if state["risk"] or state["fingerprint"] != final_fingerprint:
        raise ValueError("target final manifest/ledger snapshot is not idle or differs")
    backup = root / "target-gates-before"
    backup.mkdir(mode=0o700, exist_ok=False)
    for name, raw in before.items():
        atomic_bytes(backup / name, raw)
    report = {
        "source_gates_sha256": source_gates_sha256,
        "final_fingerprint": final_fingerprint,
        "before_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in before.items()},
        "coordinator_confirmed_external_conditions": True,
        "external_conditions_independently_verified": False,
        "services_started": False, "gates_written": False,
    }
    atomic_json(backup / "manifest.json", report)
    # Only these four booleans change; paths, profiles, source hashes and COS
    # settings are frozen. Direct's values override base per the original lane.
    for name, lane in (("base.env", "random_overlay"), ("direct-outro.env", "direct_outro")):
        atomic_bytes(BASE / "config" / name, env_bytes({**expected[name], **gates[lane]}))
    require_stopped()
    if snapshot(STATE)["fingerprint"] != final_fingerprint:
        raise ValueError("target state changed during gate configuration; remain fenced")
    report.update({"gates_written": True, "gates": gates,
                   "after_sha256": {name: digest(BASE / "config" / name) for name in before}})
    atomic_json(backup / "manifest.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-gates-sha256", required=True)
    parser.add_argument("--final-fingerprint", required=True)
    for condition in ("source-fenced", "cpu-drained", "ingress-gated", "offline-verified"):
        parser.add_argument("--coordinator-confirms-" + condition, action="store_true", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(restore_gates(args.run_id, args.source_gates_sha256, args.final_fingerprint), sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, ValueError) else "gate handoff failed; keep target stopped"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
