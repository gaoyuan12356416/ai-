#!/usr/bin/env python3
"""Fence only approved US services after a fresh coordinator drain checkpoint."""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import time

RUN_ID = "gpu-service-migration-20260828T1502"
BASE = pathlib.Path("/data/migrations") / RUN_ID / "source-fence"
GROUPS = {
    "materials": ["ad-material-generation.service", "ad-material-vision.service",
                  "codex-cover-generator.service", "codex-screenshot-batch.service",
                  "codex-screenshot-batch-burst.service", "codex-screenshot-square.service",
                  "codex-screenshot-portrait.service", "codex-screenshot-landscape.service",
                  "drama-material-api.service", "gpu-worker-reverse-tunnel.service",
                  "gpu-screenshot-batch-burst-tunnel.service"],
    "tt": ["tt-gpu-publisher.service", "tt-gpu-direct-outro.service",
           "tt-gpu-reverse-tunnel.service", "tt-gpu-direct-outro-reverse-tunnel.service"],
    "x": ["x-post-media-repair.service", "x-post-media-repair-tunnel.service"],
}


def run(args, check=True):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    if check and p.returncode:
        raise RuntimeError("command failed: %s: %s" % (args[0], p.stderr.strip()))
    return p


def prop(unit, name):
    return run(["systemctl", "show", unit, "-p", name, "--value"]).stdout.strip()


def inspect(unit):
    pid = int(prop(unit, "MainPID") or 0)
    result = {"unit": unit, "pid": pid, "active": prop(unit, "ActiveState"),
              "enabled": prop(unit, "UnitFileState"), "fragment": prop(unit, "FragmentPath")}
    if pid:
        status = pathlib.Path("/proc/%s/status" % pid).read_text()
        result["threads"] = int(next(line.split()[1] for line in status.splitlines()
                                     if line.startswith("Threads:")))
        result["children"] = pathlib.Path("/proc/%s/task/%s/children" % (pid, pid)).read_text().split()
    return result


def assert_idle(states):
    for state in states:
        if "tunnel" not in state["unit"] and state["pid"]:
            if state.get("threads") != 1 or state.get("children"):
                raise RuntimeError("source not idle: " + state["unit"])


def retire_local_unit(local, unit_backup):
    """/etc and /data are different filesystems on US: verify copy before unlink."""
    retired = unit_backup / "retired-local.service"
    original = local.read_bytes()
    if retired.exists():
        if retired.read_bytes() != original:
            raise RuntimeError("retired unit archive differs from current unit")
    else:
        with retired.open("xb") as output:
            output.write(original)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(str(retired), 0o600)
    if retired.read_bytes() != original or local.read_bytes() != original:
        raise RuntimeError("unit changed during retirement archive")
    local.unlink()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("group", choices=sorted(GROUPS))
    ap.add_argument("--checkpoint", type=pathlib.Path)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--resume", action="store_true", help="resume a recorded partial fence with fresh drain proof")
    a = ap.parse_args()
    if socket.gethostname() != "VM-0-13-centos":
        raise RuntimeError("wrong host: US source only")
    if run(["findmnt", "-n", "-o", "TARGET", "-T", "/data"]).stdout.strip() != "/data":
        raise RuntimeError("US source data disk missing")
    states = [inspect(u) for u in GROUPS[a.group]]
    assert_idle(states)
    if not a.apply:
        print(json.dumps({"dry_run": True, "group": a.group, "states": states}))
        return
    if a.checkpoint is None:
        raise RuntimeError("fresh coordinator checkpoint required")
    proof = json.loads(a.checkpoint.read_text())
    if proof.get("group") != a.group or proof.get("run_id") != RUN_ID:
        raise RuntimeError("checkpoint scope mismatch")
    if not 0 <= time.time() - float(proof.get("checked_at_epoch", 0)) <= 300:
        raise RuntimeError("checkpoint stale")
    for field in ("new_admission_closed", "triggers_paused", "cpu_drained"):
        if proof.get(field) is not True:
            raise RuntimeError("checkpoint not ready: " + field)
    # X publication ledgers remain on CPU and may contain a pre-existing
    # needs-review outcome. This media-only handoff must preserve that record,
    # not falsify a global no-unknown assertion or attempt a publication retry.
    outcome_field = "no_unknown_repairs" if a.group == "x" else "no_unknown"
    if proof.get(outcome_field) is not True:
        raise RuntimeError("checkpoint not ready: " + outcome_field)
    BASE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(BASE), 0o700)
    snapshot = BASE / (a.group + "-before.json")
    if snapshot.exists() and not a.resume:
        raise RuntimeError("fence snapshot already exists; inspect partial result before retry")
    if a.resume:
        if not snapshot.is_file():
            raise RuntimeError("resume requires an existing initial fence snapshot")
        initial = json.loads(snapshot.read_text())
        if [s["unit"] for s in initial["states"]] != GROUPS[a.group]:
            raise RuntimeError("initial snapshot service scope changed")
        states = initial["states"]
    else:
        snapshot.write_text(json.dumps({"checkpoint_sha256": hashlib.sha256(a.checkpoint.read_bytes()).hexdigest(),
                                        "states": states, "created_at_epoch": time.time()}, indent=2))
        os.chmod(str(snapshot), 0o600)
    for state in states:
        u = state["unit"]
        unit_backup = BASE / u
        if prop(u, "UnitFileState") == "masked":
            if int(prop(u, "MainPID") or 0) or prop(u, "ActiveState") not in ("inactive", "failed"):
                raise RuntimeError("masked source is still active: " + u)
            print(json.dumps({"already_fenced": u}))
            continue
        unit_backup.mkdir(mode=0o700, exist_ok=a.resume)
        fragment = pathlib.Path(state["fragment"]) if state["fragment"] else None
        if fragment and fragment.is_file() and not fragment.is_symlink():
            original = unit_backup / "original.service"
            if original.exists():
                if original.read_bytes() != fragment.read_bytes():
                    raise RuntimeError("original unit changed after partial fence: " + u)
            else:
                shutil.copy2(str(fragment), str(original))
                os.chmod(str(original), 0o600)
        dropins = pathlib.Path("/etc/systemd/system") / (u + ".d")
        if dropins.is_dir() and not (unit_backup / "dropins").exists():
            shutil.copytree(str(dropins), str(unit_backup / "dropins"))
        run(["systemctl", "stop", u])
        run(["systemctl", "disable", u])
        if int(prop(u, "MainPID") or 0) or prop(u, "ActiveState") not in ("inactive", "failed"):
            raise RuntimeError("source still running: " + u)
        local = pathlib.Path("/etc/systemd/system") / u
        if local.exists() and not local.is_symlink():
            retire_local_unit(local, unit_backup)
        run(["systemctl", "mask", u])
        run(["systemctl", "daemon-reload"])
        if prop(u, "UnitFileState") != "masked":
            raise RuntimeError("persistent mask verification failed: " + u)
        print(json.dumps({"fenced": u, "active": prop(u, "ActiveState"), "enabled": prop(u, "UnitFileState")}))


if __name__ == "__main__":
    main()
