#!/usr/bin/env python3
"""Bounded cgroup-v1 self-check, NOT a renderer or general command launcher.

Run only in a separately approved transient unit on Linux. PID 1 must apply
MemoryLimit=256M, TasksMax=128, CPUQuota=200% or 400%, Nice=10,
NoNewPrivileges=yes, and CapabilityBoundingSet=CAP_SETUID CAP_SETGID first.
Only this unit's memsw/swappiness are written. The fixed unprivileged probe
allocates 8 MiB for three seconds; it never invokes media tools or the network.
Passing this check does not approve a media benchmark or prove swap impossible.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
import sys
import time


PROBE_BYTES = 8 * 1024 * 1024
PROBE_SECONDS = 3
TARGET_USER = "drama-synthesis-gpu"
ROOT_CAPABILITIES = (1 << 6) | (1 << 7)  # CAP_SETGID, CAP_SETUID only.


@dataclass(frozen=True)
class GuardProfile:
    """Immutable, reviewed cgroup contract; callers cannot supply limits."""

    name: str
    memory_bytes: int
    tasks_max: int
    unit_pattern: str
    media_acceptance: bool


SELF_TEST_PROFILE = GuardProfile(
    name="self-test-256mib-v1",
    memory_bytes=256 * 1024 * 1024,
    tasks_max=128,
    unit_pattern=r"drama-resource-guard-test-[0-9a-f]{16}\.service",
    media_acceptance=False,
)
MEDIA_16_GIB_PROFILE = GuardProfile(
    name="media-acceptance-16gib-v1",
    memory_bytes=16 * 1024 * 1024 * 1024,
    tasks_max=128,
    unit_pattern=(r"(?:drama-media-accept-[0-9a-f]{12}-"
                  r"[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?-"
                  r"(?:short|long)-(?:2c2t|4c2t|4c4t)-(?:r1|r2)|"
                  r"drama-media-prepare-[0-9a-f]{12}-"
                  r"[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?|"
                  r"drama-media-decode-[0-9a-f]{12}-"
                  r"[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?-"
                  r"(?:short|long)-(?:2c2t|4c2t|4c4t)-(?:r1|r2)|"
                  r"drama-media-guard-[0-9a-f]{12}-"
                  r"[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?)\.service"),
    media_acceptance=True,
)

# Backward-compatible names describe the public fixed self-check only.
MEMORY_BYTES = SELF_TEST_PROFILE.memory_bytes
TASKS_MAX = SELF_TEST_PROFILE.tasks_max
UNIT_PATTERN = SELF_TEST_PROFILE.unit_pattern


class GuardFailure(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise GuardFailure(code)


def require_frozen_profile(profile):
    require(profile is SELF_TEST_PROFILE or profile is MEDIA_16_GIB_PROFILE,
            "invalid_guard_profile")
    return profile


def absolute_path(value):
    value = re.sub(r"\\(040|011|012|134)", lambda match: chr(int(match[1], 8)), value)
    require(value.startswith("/") and not any(c in value for c in "\x00\r\n\t"), "invalid_cgroup_path")
    require(".." not in value.split("/") and str(PurePosixPath(value)) == value, "invalid_cgroup_path")
    return value


class LinuxFiles:
    """Open every path component without following symlinks; no mkdir/unlink."""
    @staticmethod
    def _open(path, flags, directory=False):
        parts = PurePosixPath(absolute_path(path)).parts[1:]
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                os.close(parent)
                parent = child
            if not parts:
                result, parent = parent, None
                return result
            return os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_CLOEXEC |
                           (os.O_DIRECTORY if directory else 0), dir_fd=parent)
        finally:
            if parent is not None:
                os.close(parent)

    def read(self, path):
        with os.fdopen(self._open(path, os.O_RDONLY), "r", encoding="utf-8") as stream:
            value = stream.read(65537)
        require(len(value) <= 65536, "control_file_too_large")
        return value.strip()

    def write(self, path, value, *, expected_directory):
        directory, name = absolute_path(path).rsplit("/", 1)
        parent = self._open(directory, os.O_RDONLY, directory=True)
        try:
            info = os.fstat(parent)
            require([os.major(info.st_dev), os.minor(info.st_dev), info.st_ino] == expected_directory,
                    "cgroup_identity_changed")
            fd = os.open(name, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(str(value) + "\n")
        finally:
            os.close(parent)

    def directory(self, path):
        fd = self._open(path, os.O_RDONLY, directory=True)
        try:
            info = os.fstat(fd)
            return [os.major(info.st_dev), os.minor(info.st_dev), info.st_ino]
        finally:
            os.close(fd)

    def has_child_groups(self, path):
        fd = self._open(path, os.O_RDONLY, directory=True)
        try:
            return any(stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode)
                       for name in os.listdir(fd))
        finally:
            os.close(fd)


class LinuxProcess:
    @staticmethod
    def pid():
        return os.getpid()

    @staticmethod
    def identity():
        return {"uids": list(os.getresuid()), "gids": list(os.getresgid()), "groups": os.getgroups(),
                "nice": os.getpriority(os.PRIO_PROCESS, 0), "affinity": sorted(os.sched_getaffinity(0))}

    @staticmethod
    def target_identity():
        import pwd
        user = pwd.getpwnam(TARGET_USER)
        return user.pw_uid, user.pw_gid

    @staticmethod
    def drop_identity(uid, gid):
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)


def integer(value, *, unlimited=False):
    if unlimited and value == "max":
        return None
    require(bool(re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", value)), "invalid_limit_value")
    return int(value)


def keyed_values(value):
    result = {}
    for line in value.splitlines():
        key, raw = line.split(None, 1)
        require(key not in result, "duplicate_control_field")
        result[key] = raw.strip()
    return result


def check_identity(files, process, *, privileged, cpu_cores):
    pid = process.pid()
    fields = {}
    for line in files.read("/proc/%s/status" % pid).splitlines():
        if ":" in line:
            name, value = line.split(":", 1)
            require(name not in fields, "duplicate_process_field")
            fields[name] = value.strip()
    actual = process.identity()
    uid, gid = process.target_identity()
    require(uid > 0 and gid > 0, "invalid_target_identity")
    expected_uid, expected_gid = (0, 0) if privileged else (uid, gid)
    require(integer(fields["Pid"]) == pid, "process_identity_mismatch")
    require(actual["uids"] == [expected_uid] * 3 and actual["gids"] == [expected_gid] * 3,
            "process_identity_mismatch")
    require([int(x) for x in fields["Uid"].split()] == [expected_uid] * 4 and
            [int(x) for x in fields["Gid"].split()] == [expected_gid] * 4, "process_identity_mismatch")
    require(sorted(actual["groups"]) == sorted(int(x) for x in fields["Groups"].split()), "groups_mismatch")
    require(privileged or not actual["groups"], "supplementary_groups_retained")
    require(fields["NoNewPrivs"] == "1", "no_new_privileges_missing")
    require(int(fields["CapEff"], 16) == int(fields["CapPrm"], 16) ==
            (ROOT_CAPABILITIES if privileged else 0), "capabilities_mismatch")
    require(int(fields["CapAmb"], 16) == int(fields["CapInh"], 16) == 0, "capabilities_mismatch")
    require(int(fields["CapBnd"], 16) == ROOT_CAPABILITIES, "capability_bound_mismatch")
    require(actual["nice"] == 10 and len(actual["affinity"]) >= cpu_cores, "cpu_scheduling_mismatch")
    return {"pid": pid, "uid": expected_uid, "gid": expected_gid, "groups": actual["groups"],
            "nice": actual["nice"], "affinity": actual["affinity"], "cap_eff": fields["CapEff"],
            "cap_amb": fields["CapAmb"], "no_new_privileges": fields["NoNewPrivs"]}


def discover_layout(files, pid, unit, *, profile):
    profile = require_frozen_profile(profile)
    require(bool(re.fullmatch(profile.unit_pattern, unit)), "invalid_test_unit")
    expected = "/system.slice/" + unit
    memberships = {}
    for line in files.read("/proc/%s/cgroup" % pid).splitlines():
        _, controllers, path = line.split(":", 2)
        for name in controllers.split(","):
            if name in ("cpu", "memory", "pids", "name=systemd"):
                require(name not in memberships, "duplicate_controller_membership")
                memberships[name] = absolute_path(path)
    require(set(memberships) == {"cpu", "memory", "pids", "name=systemd"} and
            all(path == expected for path in memberships.values()), "wrong_unit_membership")
    mounts = []
    for line in files.read("/proc/%s/mountinfo" % pid).splitlines():
        before, after = line.split(" - ", 1)
        left, right = before.split(), after.split()
        if right[0] == "cgroup":
            mounts.append((absolute_path(left[3]), absolute_path(left[4]),
                           [int(x) for x in left[2].split(":")], set(right[2].split(","))))
    layout = {}
    for controller in ("cpu", "memory", "pids"):
        candidates = [mount for mount in mounts if controller in mount[3]]
        require(len(candidates) == 1, "controller_mount_ambiguous_or_missing")
        mount_root, mount_point, device, _ = candidates[0]
        # A subtree mount hides ancestors; never claim their limits were checked.
        require(mount_root == "/" and mount_point.startswith("/sys/fs/cgroup/"), "hidden_or_invalid_cgroup_root")
        paths = [mount_point, mount_point + "/system.slice", mount_point + expected]
        identities = [files.directory(path) for path in paths]
        require(len(device) == 2 and all(identity[:2] == device for identity in identities), "cgroup_device_mismatch")
        require(not files.has_child_groups(paths[-1]), "unexpected_child_cgroup")
        for name in ("cgroup.procs", "tasks"):
            require(files.read(paths[-1] + "/" + name).split() == [str(pid)], "unexpected_cgroup_members")
        layout[controller] = {"paths": paths, "directory_identities": identities}
    return layout


def inspect_resources(files, process, unit, cpu_cores, *, profile, configured,
                      allow_unit_pressure=False):
    profile = require_frozen_profile(profile)
    require(type(cpu_cores) is int and cpu_cores in (2, 4), "invalid_cpu_expectation")
    layout = discover_layout(files, process.pid(), unit, profile=profile)
    limits, observations = {}, {}
    for scope, offset in (("parent", 1), ("unit", 2)):
        cpu, memory, pids = (layout[name]["paths"][offset] for name in ("cpu", "memory", "pids"))
        quota = files.read(cpu + "/cpu.cfs_quota_us")
        quota = -1 if quota == "-1" else integer(quota)
        period = integer(files.read(cpu + "/cpu.cfs_period_us"))
        require(period > 0 and (quota == -1 or quota > 0), "invalid_cpu_limit")
        memory_limit = integer(files.read(memory + "/memory.limit_in_bytes"))
        memsw_limit = integer(files.read(memory + "/memory.memsw.limit_in_bytes"))
        tasks = integer(files.read(pids + "/pids.max"), unlimited=True)
        require(files.read(memory + "/memory.use_hierarchy") == "1", "memory_hierarchy_disabled")
        oom = keyed_values(files.read(memory + "/memory.oom_control"))
        require(set(oom) in ({"oom_kill_disable", "under_oom"},
                             {"oom_kill_disable", "under_oom", "oom_kill"}) and
                oom["oom_kill_disable"] == "0", "unsafe_oom_state")
        under_oom = integer(oom["under_oom"])
        oom_kill = integer(oom["oom_kill"]) if "oom_kill" in oom else None
        if scope == "parent" or not allow_unit_pressure:
            require(under_oom == 0, "unsafe_oom_state")
        if scope == "unit" and profile is MEDIA_16_GIB_PROFILE:
            require(oom_kill is not None, "oom_kill_counter_unavailable")
        limits[scope] = {"cpu_quota_us": quota, "cpu_period_us": period,
                         "memory_bytes": memory_limit, "memsw_bytes": memsw_limit, "tasks_max": tasks}
        observations[scope] = {
            "memory_bytes": integer(files.read(memory + "/memory.usage_in_bytes")),
            "memsw_bytes": integer(files.read(memory + "/memory.memsw.usage_in_bytes")),
            "tasks": integer(files.read(pids + "/pids.current")),
            "oom_control": {
                "oom_kill_disable": 0,
                "under_oom": under_oom,
                "oom_kill": oom_kill,
                "oom_kill_available": oom_kill is not None,
            },
        }
        if scope == "unit":
            require(quota == cpu_cores * period and memory_limit == profile.memory_bytes and
                    tasks == profile.tasks_max,
                    "unit_limit_mismatch")
            swappiness = integer(files.read(memory + "/memory.swappiness"))
            require(swappiness <= 100 and memsw_limit >= profile.memory_bytes, "invalid_memory_limit")
            stats = keyed_values(files.read(memory + "/memory.stat"))
            require(integer(stats["hierarchical_memory_limit"]) == profile.memory_bytes,
                    "effective_memory_limit_mismatch")
            if configured:
                require(memsw_limit == profile.memory_bytes and swappiness == 0 and
                        integer(stats["hierarchical_memsw_limit"]) == profile.memory_bytes,
                        "swap_guard_not_effective")
            limits[scope]["swappiness"] = swappiness
            observations[scope]["swap_bytes"] = integer(stats["total_swap"])
            observations[scope]["memory_failcnt"] = integer(files.read(memory + "/memory.failcnt"))
            observations[scope]["memsw_failcnt"] = integer(files.read(memory + "/memory.memsw.failcnt"))
        else:
            require((quota == -1 or quota >= cpu_cores * period) and
                    memory_limit >= profile.memory_bytes and memsw_limit >= profile.memory_bytes and
                    (tasks is None or tasks >= profile.tasks_max), "tighter_parent_limit")
    require(files.read(layout["memory"]["paths"][0] + "/memory.use_hierarchy") == "1", "memory_hierarchy_disabled")
    parent, own = observations["parent"], observations["unit"]
    require(limits["parent"]["memory_bytes"] - max(0, parent["memory_bytes"] - own["memory_bytes"]) >=
            profile.memory_bytes and
            limits["parent"]["memsw_bytes"] - max(0, parent["memsw_bytes"] - own["memsw_bytes"]) >=
            profile.memory_bytes and
            (limits["parent"]["tasks_max"] is None or
             limits["parent"]["tasks_max"] - max(0, parent["tasks"] - own["tasks"]) >=
             profile.tasks_max),
            "insufficient_parent_headroom")
    if not allow_unit_pressure:
        require(own["swap_bytes"] == own["memory_failcnt"] == own["memsw_failcnt"] == 0 and
                own["oom_control"]["under_oom"] == 0 and
                (own["oom_control"]["oom_kill"] in (None, 0)),
                "unit_swap_or_limit_pressure")
    # The full v1 hierarchy root has no hard quota. Every non-root ancestor of
    # the exact /system.slice/<unit> membership above has been checked.
    return {"profile": profile.name, "layout": layout, "limits": limits, "observations": observations,
            "ancestor_limits_checked": True, "root_semantics": "v1_hierarchy_root_unlimited"}


def resource_fingerprint(state):
    fixed = {key: state[key] for key in
             ("profile", "layout", "limits", "ancestor_limits_checked", "root_semantics")}
    return hashlib.sha256(json.dumps(fixed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def pressure_snapshot(state):
    try:
        own = state["observations"]["unit"]
        value = {
            "memory_failcnt": own["memory_failcnt"],
            "memsw_failcnt": own["memsw_failcnt"],
            "swap_bytes": own["swap_bytes"],
            "oom_control": dict(own["oom_control"]),
        }
    except (KeyError, TypeError, ValueError):
        raise GuardFailure("cgroup_pressure_evidence_invalid") from None
    require(all(type(value[key]) is int and value[key] >= 0 for key in
                ("memory_failcnt", "memsw_failcnt", "swap_bytes")) and
            set(value["oom_control"]) == {
                "oom_kill_disable", "under_oom", "oom_kill", "oom_kill_available"
            } and value["oom_control"]["oom_kill_disable"] == 0 and
            type(value["oom_control"]["under_oom"]) is int and
            value["oom_control"]["under_oom"] >= 0 and
            value["oom_control"]["oom_kill_available"] is True and
            type(value["oom_control"]["oom_kill"]) is int and
            value["oom_control"]["oom_kill"] >= 0,
            "cgroup_pressure_evidence_invalid")
    return value


def capture_pressure(files, process, unit, cpu_cores, *, profile):
    state = inspect_resources(
        files, process, unit, cpu_cores, profile=profile, configured=True,
        allow_unit_pressure=True,
    )
    return {
        "resources_sha256": resource_fingerprint(state),
        "pressure": pressure_snapshot(state),
    }


def verify_pressure_transition(before, after):
    require(isinstance(before, dict) and isinstance(after, dict) and
            set(before) == set(after) == {"resources_sha256", "pressure"} and
            before["resources_sha256"] == after["resources_sha256"],
            "resource_guard_changed")
    first, last = before["pressure"], after["pressure"]
    pressure_snapshot({"observations": {"unit": first}})
    pressure_snapshot({"observations": {"unit": last}})
    require(last["memory_failcnt"] == first["memory_failcnt"] and
            last["memsw_failcnt"] == first["memsw_failcnt"] and
            last["oom_control"]["oom_kill"] == first["oom_control"]["oom_kill"] and
            first["oom_control"]["under_oom"] == last["oom_control"]["under_oom"] == 0 and
            first["swap_bytes"] == last["swap_bytes"] == 0,
            "media_cgroup_pressure_detected")
    return {"before": first, "after": last, "verified": True}


def emit(value):
    print(json.dumps(value, sort_keys=True, allow_nan=False), flush=True)


def run_guard(unit, cpu_cores, *, profile, files=None, process=None, launch_probe=None, report=emit):
    profile = require_frozen_profile(profile)
    require(type(cpu_cores) is int and cpu_cores in (2, 4), "invalid_cpu_expectation")
    files, process = files or LinuxFiles(), process or LinuxProcess()
    check_identity(files, process, privileged=True, cpu_cores=cpu_cores)
    before = inspect_resources(files, process, unit, cpu_cores, profile=profile, configured=False)
    memory = before["layout"]["memory"]["paths"][-1]
    directory = before["layout"]["memory"]["directory_identities"][-1]
    files.write(memory + "/memory.memsw.limit_in_bytes", profile.memory_bytes,
                expected_directory=directory)
    files.write(memory + "/memory.swappiness", 0, expected_directory=directory)
    protected = inspect_resources(files, process, unit, cpu_cores, profile=profile, configured=True)
    require(before["layout"] == protected["layout"], "cgroup_identity_changed")
    uid, gid = process.target_identity()
    process.drop_identity(uid, gid)
    identity = check_identity(files, process, privileged=False, cpu_cores=cpu_cores)
    inherited = inspect_resources(files, process, unit, cpu_cores, profile=profile, configured=True)
    require(resource_fingerprint(protected) == resource_fingerprint(inherited), "resource_guard_changed")
    report({"phase": "guard", "ok": True, "profile": profile.name, "unit": unit,
            "cpu_cores": cpu_cores,
            "identity": identity, "resources": inherited, "child_executed": False})
    proof = {"version": 2, "profile": profile.name, "unit": unit,
             "cpu_cores": cpu_cores, "pid": process.pid(),
             "resources_sha256": resource_fingerprint(inherited)}
    return (launch_probe or exec_fixed_probe)(proof)


def exec_fixed_probe(proof):
    require(proof.get("profile") == SELF_TEST_PROFILE.name, "invalid_probe_proof")
    # A small inherited pipe proves this invocation followed the guard. All
    # privileged control-file descriptors have already been closed.
    content = json.dumps(proof, sort_keys=True).encode()
    require(len(content) <= 1024, "invalid_probe_proof")
    read_fd, write_fd = os.pipe()
    try:
        require(os.write(write_fd, content) == len(content), "probe_proof_write_failed")
        os.close(write_fd)
        write_fd = None
        os.set_inheritable(read_fd, True)
        executable, script = os.path.realpath(sys.executable), os.path.realpath(__file__)
        os.execve(executable, [executable, "-I", "-S", "-B", script, "--unit", proof["unit"],
                              "--cpu-cores", str(proof["cpu_cores"]), "--probe-proof-fd", str(read_fd)],
                  {"PATH": "/usr/bin:/bin", "LANG": "C", "PYTHONDONTWRITEBYTECODE": "1"})
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def verify_inherited_guard(unit, cpu_cores, proof_fd, *, profile):
    profile = require_frozen_profile(profile)
    require(proof_fd >= 3 and stat.S_ISFIFO(os.fstat(proof_fd).st_mode), "invalid_probe_proof")
    try:
        os.set_blocking(proof_fd, False)
        data = os.read(proof_fd, 1025)
    finally:
        os.close(proof_fd)
    require(len(data) <= 1024, "invalid_probe_proof")
    proof = json.loads(data)
    require(set(proof) == {"version", "profile", "unit", "cpu_cores", "pid", "resources_sha256"} and
            type(proof["version"]) is int and proof["version"] == 2 and
            proof["profile"] == profile.name and proof["unit"] == unit and
            proof["cpu_cores"] == cpu_cores and proof["pid"] == os.getpid(), "invalid_probe_proof")
    files, process = LinuxFiles(), LinuxProcess()
    identity = check_identity(files, process, privileged=False, cpu_cores=cpu_cores)
    initial = inspect_resources(files, process, unit, cpu_cores, profile=profile, configured=True)
    require(resource_fingerprint(initial) == proof["resources_sha256"], "resource_guard_changed")
    return {
        "proof": proof,
        "identity": identity,
        "resources": initial,
        "pressure": ({
            "resources_sha256": resource_fingerprint(initial),
            "pressure": pressure_snapshot(initial),
        } if profile is MEDIA_16_GIB_PROFILE else None),
    }


def run_probe(unit, cpu_cores, proof_fd, *, profile):
    verified = verify_inherited_guard(unit, cpu_cores, proof_fd, profile=profile)
    proof, identity = verified["proof"], verified["identity"]
    files, process = LinuxFiles(), LinuxProcess()
    allocation = bytearray(PROBE_BYTES)
    for offset in range(0, len(allocation), 4096):
        allocation[offset] = 1
    for second in range(PROBE_SECONDS):
        time.sleep(1)
        check_identity(files, process, privileged=False, cpu_cores=cpu_cores)
        state = inspect_resources(files, process, unit, cpu_cores, profile=profile, configured=True)
        require(resource_fingerprint(state) == proof["resources_sha256"], "resource_guard_changed")
        emit({"phase": "probe_sample", "second": second + 1, "unit": unit,
              "observations": state["observations"], "allocated_bytes": len(allocation)})
    emit({"phase": "probe", "ok": True, "unit": unit, "identity": identity,
          "resources_sha256": proof["resources_sha256"], "allocated_bytes": len(allocation),
          "observed_seconds": PROBE_SECONDS, "media_tools_started": 0, "media_acceptance": False})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--cpu-cores", type=int, choices=(2, 4), required=True)
    parser.add_argument("--probe-proof-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        require(sys.platform == "linux", "linux_required")
        if args.probe_proof_fd is None:
            run_guard(args.unit, args.cpu_cores, profile=SELF_TEST_PROFILE)
        else:
            run_probe(args.unit, args.cpu_cores, args.probe_proof_fd, profile=SELF_TEST_PROFILE)
        return 0
    except (GuardFailure, OSError, KeyError, ValueError, IndexError, TypeError) as exc:
        # Never print commands, environment, exception text or arbitrary paths.
        reason = str(exc) if isinstance(exc, GuardFailure) else "resource_guard_read_or_operation_failed"
        emit({"phase": "guard" if args.probe_proof_fd is None else "probe", "ok": False, "reason": reason,
              "child_executed": args.probe_proof_fd is not None, "media_tools_started": 0})
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
