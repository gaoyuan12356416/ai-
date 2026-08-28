#!/usr/bin/env python3
"""Finalize only confirmed legacy configuration; no API restarts or business calls."""
import argparse
import hashlib
import json
import os
import pathlib
import shlex
import socket
import time

from maintenance import BASE, RUN_ID, atomic_write, run, storage_guard
from source_fence import GROUPS

GPU_KEYS = ("GPU_VIDEO_WORKER_URL", "GPU_VIDEO_WORKER_TOKEN")
OLD_CLEANUP_SHA = "fe59362c5cdd74ba88487056fd8b63c550121d90ad16d3255046abb5de71f2d8"


def read_values(text):
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        if key.strip() not in GPU_KEYS:
            continue
        value = shlex.split(raw, comments=True)
        if len(value) != 1 or key.strip() in values:
            raise RuntimeError("GPU configuration is missing, duplicated or ambiguous")
        values[key.strip()] = value[0]
    if set(values) != set(GPU_KEYS):
        raise RuntimeError("GPU URL/token pair incomplete")
    return values


def replace_pair(text, values):
    read_values(text)
    result = []
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in GPU_KEYS:
            line = key + "=" + shlex.quote(values[key])
        result.append(line)
    return "\n".join(result) + "\n"


def align_drama(apply):
    if socket.gethostname() != "VM-0-108-centos":
        raise RuntimeError("CPU host required")
    storage_guard()
    source = pathlib.Path("/etc/drama-synthesis/cpu.env")
    target = pathlib.Path("/root/drama_material_service/.env")
    values = read_values(source.read_text())
    if values[GPU_KEYS[0]] != "http://127.0.0.1:18788":
        raise RuntimeError("effective HK drama endpoint changed")
    pid = int(run(["systemctl", "show", "drama-material-api.service", "-p", "MainPID", "--value"]).stdout)
    env = dict(item.split(b"=", 1) for item in pathlib.Path("/proc/%d/environ" % pid).read_bytes().split(b"\0") if b"=" in item)
    if any(env.get(key.encode()) != values[key].encode() for key in GPU_KEYS):
        raise RuntimeError("running API does not use the confirmed HK URL/token pair")
    before = target.read_text()
    old = read_values(before)
    if old[GPU_KEYS[0]] not in ("http://127.0.0.1:18787", "http://127.0.0.1:18788"):
        raise RuntimeError("legacy endpoint changed; review before replacing")
    after = replace_pair(before, values)
    receipt = {"action": "align-drama", "main_api_pid": pid, "old_url": old[GPU_KEYS[0]],
               "new_url": values[GPU_KEYS[0]], "token_pair_matches_running_api": True,
               "api_restarted": False, "changed": before != after,
               "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
               "after_sha256": hashlib.sha256(after.encode()).hexdigest()}
    if apply:
        gates = json.loads((BASE / "gates.json").read_text())
        if "materials" not in gates["groups"]:
            raise RuntimeError("material write gate must be active")
        for unit in ("ad-material-frontend-test.service", "drama-material-api-test.service"):
            if run(["systemctl", "is-active", unit], check=False).stdout.strip() in ("active", "activating"):
                raise RuntimeError("test services must be paused")
        backup = BASE / "drama-dotenv-before.env"
        if not backup.exists():
            atomic_write(backup, before)
        if target.read_text() != before:
            raise RuntimeError("dotenv changed concurrently")
        atomic_write(target, after)
        atomic_write(BASE / "drama-config-aligned.json", json.dumps(receipt, indent=2))
    return receipt


def retire_cleanup(apply):
    if socket.gethostname() != "VM-0-13-centos":
        raise RuntimeError("US host required")
    if run(["findmnt", "-n", "-o", "TARGET", "-T", "/data"]).stdout.strip() != "/data":
        raise RuntimeError("US data disk missing")
    path = pathlib.Path("/etc/cron.d/gpu-drama-cleanup")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != OLD_CLEANUP_SHA:
        raise RuntimeError("legacy cleanup cron drifted or already retired")
    receipt = {"action": "retire-legacy-cleanup", "path": str(path), "before_sha256": OLD_CLEANUP_SHA,
               "root_template_cleanup_unchanged": True, "kronos_unchanged": True}
    if apply:
        for unit in sum(GROUPS.values(), []):
            enabled = run(["systemctl", "show", unit, "-p", "UnitFileState", "--value"]).stdout.strip()
            active = run(["systemctl", "show", unit, "-p", "ActiveState", "--value"]).stdout.strip()
            if enabled != "masked" or active not in ("inactive", "failed"):
                raise RuntimeError("retiring services are not fully fenced: " + unit)
        backup = pathlib.Path("/data/migrations") / RUN_ID / "retired-cleanup"
        atomic_write(backup / "gpu-drama-cleanup.original", raw.decode())
        atomic_write(path, "# Retired after verified business migration: " + RUN_ID + "\n", 0o644)
        atomic_write(backup / "receipt.json", json.dumps(receipt, indent=2))
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("align-drama", "retire-legacy-cleanup"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = align_drama(args.apply) if args.action == "align-drama" else retire_cleanup(args.apply)
    result.update({"dry_run": not args.apply, "checked_at_epoch": time.time(), "run_id": RUN_ID})
    print(json.dumps(result))


if __name__ == "__main__":
    main()
