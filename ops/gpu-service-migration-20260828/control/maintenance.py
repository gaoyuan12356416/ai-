#!/usr/bin/env python3
"""Scoped CPU maintenance gates. Dry-run unless --apply; no publishing calls."""
import argparse
import json
import os
import pathlib
import re
import subprocess
import tempfile

RUN_ID = "gpu-service-migration-20260828T1502"
BASE = pathlib.Path("/mnt/data-disk/migrations") / RUN_ID / "control"
MAP = pathlib.Path("/etc/nginx/conf.d/00-gpu-service-migration-map.conf")
GATE = pathlib.Path("/etc/nginx/default.d/00-gpu-service-migration-gate.conf")
CPU_UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
PATTERNS = {
    "materials": r"/api/(ad-material|drama-screenshot-material|drama-material)(/|$)",
    "tt": r"/api/admin/(tt-posts|tt-auto-publish)(/|$)",
    "x": r"/api/admin/(x-posts|x-auto-posts|x-auto-publish)(/|$)",
}
TRIGGERS = {
    "tt": ["tt-post-prepare.timer", "tt-post-prepare.path", "tt-post-runner.timer",
           "tt-post-runner.path", "tt-auto-post-scheduler.timer",
           "tt-auto-post-runner.timer", "tt-auto-post-runner.path"],
    "x": ["x-post-daily.timer", "x-post-manual.timer", "x-post-schedule-claim.timer",
          "x-post-schedule.timer", "x-auto-post-scheduler.timer",
          "x-auto-post-runner.timer", "x-auto-post-runner.path"],
    "materials": ["ad-material-frontend-test.service", "drama-material-api-test.service"],
}


def run(args, check=True, input_text=None):
    p = subprocess.run(args, input=input_text, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True)
    if check and p.returncode:
        raise RuntimeError("command failed: %s: %s" % (args[0], p.stderr.strip()))
    return p


def storage_guard():
    uuid = run(["findmnt", "-n", "-o", "UUID", "--target", "/mnt/data-disk"]).stdout.strip()
    target = run(["findmnt", "-n", "-o", "TARGET", "--target", "/mnt/data-disk"]).stdout.strip()
    if target != "/mnt/data-disk" or uuid != CPU_UUID or not os.access(target, os.W_OK):
        raise RuntimeError("CPU data disk guard failed")


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".migration-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def gate_text(groups):
    lines = ['map "$request_method:$uri" $gpu_service_migration_block {', '    default 0;']
    if "materials" in groups:
        # The deployed legacy handler accepts a JSON-body GET at this exact
        # batch path and submits jobs. Ordinary GET job/list queries stay open.
        lines.append('    "~^[A-Z]+:/api/drama-screenshot-material/jobs/batch$" 1;')
    for group in sorted(groups):
        lines.append('    "~^(POST|PUT|PATCH|DELETE):%s" 1;' % PATTERNS[group])
    lines.append("}\n")
    body = ('if ($gpu_service_migration_block) {\n'
            '    return 503 \'{"error":"service_migration_maintenance",'
            '"message":"业务迁移中，请稍后重试"}\';\n}\n')
    return "\n".join(lines), body


def gate(group, enabled):
    state_path = BASE / "gates.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"groups": []}
    groups = set(state["groups"])
    if enabled:
        groups.add(group)
    else:
        groups.discard(group)
    old = {p: p.read_bytes() if p.exists() else None for p in (MAP, GATE)}
    try:
        if groups:
            m, g = gate_text(groups)
            atomic_write(MAP, m, 0o644)
            atomic_write(GATE, g, 0o644)
        else:
            for p in (MAP, GATE):
                if p.exists():
                    p.unlink()
        run(["nginx", "-t"])
        run(["systemctl", "reload", "nginx"])
    except Exception:
        for p, data in old.items():
            if data is None:
                if p.exists():
                    p.unlink()
            else:
                atomic_write(p, data.decode("utf-8"), 0o644)
        raise
    atomic_write(state_path, json.dumps({"groups": sorted(groups)}, indent=2))
    print(json.dumps({"active_maintenance_groups": sorted(groups)}))


def pause(group):
    p = BASE / (group + "-triggers.json")
    if p.exists():
        state = json.loads(p.read_text())
        if not state.get("restored"):
            raise RuntimeError("pause already recorded; inspect before retry")
    original = {u: run(["systemctl", "is-active", u], check=False).stdout.strip()
                for u in TRIGGERS[group]}
    atomic_write(p, json.dumps({"original": original, "restored": False}, indent=2))
    for u, active in original.items():
        if active in ("active", "activating"):
            run(["systemctl", "stop", u])
    if group == "materials":
        old = run(["crontab", "-l"]).stdout
        marker = "# " + RUN_ID + " PAUSED "
        lines = old.splitlines()
        candidates = [i for i, line in enumerate(lines)
                      if not line.lstrip().startswith("#") and "run_auto_cover_synthesis.sh" in line]
        if len(candidates) != 1:
            raise RuntimeError("expected exactly one screenshot cron entry")
        atomic_write(BASE / "materials-crontab-before.txt", old)
        lines[candidates[0]] = marker + lines[candidates[0]]
        run(["crontab", "-"], input_text="\n".join(lines) + "\n")
    print(json.dumps({"paused": group, "original": original}))


def resume(group):
    p = BASE / (group + "-triggers.json")
    state = json.loads(p.read_text())
    if state.get("restored"):
        raise RuntimeError("already restored")
    if group == "materials":
        current = run(["crontab", "-l"]).stdout
        marker = "# " + RUN_ID + " PAUSED "
        lines = current.splitlines()
        hits = [i for i, line in enumerate(lines) if line.startswith(marker)]
        if len(hits) != 1:
            raise RuntimeError("cron changed; refuse broad restore")
        lines[hits[0]] = lines[hits[0]][len(marker):]
        run(["crontab", "-"], input_text="\n".join(lines) + "\n")
    for u, active in state["original"].items():
        if active in ("active", "activating"):
            run(["systemctl", "start", u])
    state["restored"] = True
    atomic_write(p, json.dumps(state, indent=2))
    print(json.dumps({"restored": group}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["gate-on", "gate-off", "pause", "resume"])
    ap.add_argument("group", choices=sorted(PATTERNS))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    storage_guard()
    if not a.apply:
        print(json.dumps({"dry_run": True, "action": a.action, "group": a.group,
                          "triggers": TRIGGERS[a.group], "pattern": PATTERNS[a.group]}))
        return
    if a.action.startswith("gate-"):
        gate(a.group, a.action == "gate-on")
    else:
        globals()[a.action](a.group)


if __name__ == "__main__":
    main()
