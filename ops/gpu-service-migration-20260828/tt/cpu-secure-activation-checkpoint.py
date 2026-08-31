#!/usr/bin/env python3
"""Create a fresh, read-only CPU proof for the HK TT activation window."""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


RUN_ID = "gpu-service-migration-20260828T1502"
CPU_UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
OUTPUT_ROOT = Path("/mnt/data-disk/migrations") / RUN_ID / "tt"
TRIGGERS = (
    "tt-post-prepare.timer",
    "tt-post-prepare.path",
    "tt-post-runner.timer",
    "tt-post-runner.path",
    "tt-auto-post-scheduler.timer",
    "tt-auto-post-runner.timer",
    "tt-auto-post-runner.path",
)
PROBE_PATHS = (
    "/api/admin/tt-posts/999999999",
    "/api/admin/tt-auto-publish/999999999",
)
PUBLIC_AUTHORITY = "ai.yingliangads.com"


def command(args):
    result = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=10,
    )
    if result.returncode:
        raise RuntimeError("checkpoint command failed: " + args[0])
    return result.stdout.strip()


def systemd_properties(unit):
    output = command(
        [
            "systemctl",
            "show",
            "-p",
            "ActiveState,SubState,UnitFileState",
            unit,
        ]
    )
    values = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if values.get("ActiveState") != "inactive":
        raise RuntimeError("TT trigger is not paused: " + unit)
    if values.get("UnitFileState") != "enabled":
        raise RuntimeError("TT trigger enable state changed: " + unit)
    return {
        "active_state": values["ActiveState"],
        "sub_state": values.get("SubState", ""),
        "unit_file_state": values["UnitFileState"],
    }


def write_probe(path):
    fd, temporary = tempfile.mkstemp(prefix=".tt-gate-body-", dir=str(OUTPUT_ROOT))
    os.close(fd)
    try:
        os.chmod(temporary, 0o600)
        result = subprocess.run(
            [
                "curl",
                "--silent",
                "--show-error",
                "--noproxy",
                "*",
                "--proto",
                "=https",
                "--resolve",
                PUBLIC_AUTHORITY + ":443:127.0.0.1",
                "--connect-timeout",
                "5",
                "--max-time",
                "10",
                "--max-redirs",
                "0",
                "--request",
                "DELETE",
                "--output",
                temporary,
                "--write-out",
                "%{http_code}|%{remote_ip}|%{remote_port}|%{ssl_verify_result}|%{url_effective}",
                "https://" + PUBLIC_AUTHORITY + path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=15,
        )
        if result.returncode:
            raise RuntimeError("TT HTTPS write-gate probe failed")
        parts = result.stdout.strip().split("|")
        if len(parts) != 5:
            raise RuntimeError("TT HTTPS write-gate probe output is invalid")
        status, remote_ip, remote_port, verify_result, effective_url = parts
        body = Path(temporary).read_bytes()
        if len(body) > 1024 * 1024:
            raise RuntimeError("TT gate response exceeded the checkpoint limit")
        if status != "503":
            raise RuntimeError("TT write gate did not return 503")
        if remote_ip != "127.0.0.1" or remote_port != "443" or verify_result != "0":
            raise RuntimeError("TT HTTPS write-gate endpoint or TLS verification changed")
        expected_url = "https://" + PUBLIC_AUTHORITY + path
        if effective_url != expected_url:
            raise RuntimeError("TT HTTPS write-gate probe redirected")
        return {
            "method": "DELETE",
            "scheme": "https",
            "authority": PUBLIC_AUTHORITY,
            "path": path,
            "status": int(status),
            "connected_address": remote_ip,
            "connected_port": int(remote_port),
            "tls_verify_result": int(verify_result),
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_cpu_state(state):
    for key in (
        "cutover_safe_after_ingress_gate",
        "drained",
        "runners_inactive",
        "stable_publication_facts",
        "triggers_paused",
    ):
        if state.get(key) is not True:
            raise RuntimeError("CPU TT drain state is not safe")
    if state.get("sample_count") != 3 or state.get("http_connections") != []:
        raise RuntimeError("CPU TT drain samples are incomplete")
    for database in state.get("databases", {}).values():
        for table in database.get("tables", {}).values():
            for key in (
                "claims_effective",
                "claims_expired",
                "claims_invalid_lease",
                "claims_present",
                "executing_status",
                "unknown_outcome",
            ):
                if int(table.get(key, 0)) != 0:
                    raise RuntimeError("CPU TT state contains an active or unknown item")


def atomic_json(path, value):
    if path.exists() or path.is_symlink():
        raise FileExistsError("checkpoint output already exists")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".tt-checkpoint-", dir=str(path.parent))
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
    parser.add_argument("--cpu-state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if command(["hostname"]) != "VM-0-108-centos":
        raise RuntimeError("checkpoint must run on the CPU host")
    if command(["findmnt", "-n", "-o", "UUID", "-T", "/mnt/data-disk"]) != CPU_UUID:
        raise RuntimeError("CPU data disk identity mismatch")
    output = args.output.resolve(strict=False)
    root = OUTPUT_ROOT.resolve(strict=True)
    if output.parent != root or output.name in {"", ".", ".."}:
        raise RuntimeError("checkpoint output is outside the migration data directory")
    if args.cpu_state.is_symlink():
        raise RuntimeError("CPU drain snapshot is redirected")
    state_path = args.cpu_state.resolve(strict=True)
    if not state_path.is_file():
        raise RuntimeError("CPU drain snapshot is unsafe")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    validate_cpu_state(state)
    state_sha = hashlib.sha256(state_path.read_bytes()).hexdigest()

    command(["nginx", "-t"])
    triggers = {unit: systemd_properties(unit) for unit in TRIGGERS}
    probes = [write_probe(path) for path in PROBE_PATHS]
    checkpoint = {
        "schema": 1,
        "run_id": RUN_ID,
        "at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "host": "VM-0-108-centos",
        "nginx_config_valid": True,
        "ingress_gate_503": True,
        "write_probes": probes,
        "triggers": triggers,
        "cpu_state_sha256": state_sha,
        "cpu_state": state,
        "script_issued_publish_request": False,
    }
    atomic_json(output, checkpoint)
    print(json.dumps({"ok": True, "output": str(output),
                      "sha256": hashlib.sha256(output.read_bytes()).hexdigest()},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
