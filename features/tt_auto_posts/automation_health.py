"""Read-only systemd readiness. Never starts units or overrides maintenance."""

import subprocess
import time
import re

TRIGGERS = (
    "tt-auto-post-scheduler.timer",
    "tt-auto-post-runner.timer",
    "tt-auto-post-runner.path",
)
SERVICES = ("tt-auto-post-scheduler.service", "tt-auto-post-runner.service")


def monotonic_seconds(raw):
    # systemd 239 prints this property as a timespan, newer/mocked clients may
    # expose microseconds. Reject unrecognized text instead of assuming zero.
    if str(raw).isdigit():
        return int(raw) / 1000000.0
    factors = {"w": 604800, "d": 86400, "h": 3600, "min": 60,
               "s": 1, "ms": .001, "us": .000001}
    tokens = str(raw).split()
    if not tokens:
        return 0
    total = 0
    for token in tokens:
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(min|ms|us|w|d|h|s)", token)
        if not match:
            raise ValueError("invalid systemd timespan")
        total += float(match.group(1)) * factors[match.group(2)]
    return total


def probe_automation(run=subprocess.run, monotonic=time.monotonic):
    try:
        result = run(
            ["/usr/bin/systemctl", "show", "--no-pager",
             "--property=Id,LoadState,ActiveState,LastTriggerUSecMonotonic"]
            + list(TRIGGERS + SERVICES),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=3, check=False,
        )
        if result.returncode:
            raise ValueError("systemd unavailable")
        units = {}
        for block in result.stdout.strip().split("\n\n"):
            fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            if fields.get("Id") in TRIGGERS + SERVICES:
                units[fields["Id"]] = fields
        problems = []
        for name in TRIGGERS + SERVICES:
            state = units.get(name, {})
            if state.get("LoadState") != "loaded":
                problems.append(name + ":not_loaded")
            elif name in TRIGGERS and state.get("ActiveState") != "active":
                problems.append(name + ":inactive")
            elif state.get("ActiveState") == "failed":
                problems.append(name + ":failed")
        # A timer can be active without firing. Long-running render workers are
        # allowed; the short scheduler must fire at least once every 3 minutes.
        scheduler = units.get(TRIGGERS[0], {})
        last = monotonic_seconds(scheduler.get("LastTriggerUSecMonotonic") or "0")
        if scheduler.get("ActiveState") == "active" and (last <= 0 or monotonic() - last > 180):
            problems.append("tt-auto-post-scheduler.timer:not_firing")
        return {"ready": not problems, "problems": problems,
                "units": {name: value.get("ActiveState", "unknown") for name, value in units.items()}}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"ready": False, "problems": ["automation_probe_unavailable"], "units": {}}
