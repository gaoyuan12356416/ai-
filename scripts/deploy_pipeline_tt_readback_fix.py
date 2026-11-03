#!/usr/bin/env python3
"""Guarded, single-file TT readback deployment; dry-run unless --apply.

Run on CPU as root from an exact GitHub revision. No ledger, capacity, node,
coordinator, publisher, environment or other service changes. A timed-out
graceful stop is left to finish naturally; this tool never sends SIGKILL.
"""
import argparse
import ast
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time
import urllib.error
import urllib.request
import uuid

BASE = "a97fc231e37d869df26c59a8af623217799c9024"
RELATIVE = "features/pipeline_dispatch/tt_readback.py"
ROOT = Path("/opt/tt-auto-post/current")
EXPECTED_ROOT = Path("/mnt/data-disk/pipeline-dispatch-runtime/backlog-native-d69a369660/candidates/tt_auto")
REPO = Path("/mnt/data-disk/pipeline-dispatch/github")
DATA = Path("/mnt/data-disk/pipeline-dispatch")
NATIVE_DB = Path("/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3")
MOUNT_UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
UNIT = "tt-auto-post-service.service"
TIMERS = ("tt-auto-post-runner.timer", "tt-auto-post-scheduler.timer", "tt-auto-post-reconcile.timer")
PUBLISHER_SHA = "da713109c6630fea1df7bba314053ee9f1ad9b837207a93ebf9a8785719a6afb"


def need(value, code):
    if not value:
        raise RuntimeError(code)


def run(args, *, data=None, timeout=30, env=None):
    result = subprocess.run(list(map(str, args)), input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, env=env)
    need(result.returncode == 0, "command_failed:" + str(args[0]) + ":" + str(result.returncode))
    return result.stdout


def show(unit, field):
    return run(["systemctl", "show", unit, "-p", field, "--value"]).decode().strip()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def now():
    return dt.datetime.now(dt.timezone.utc)


def private_write(path, raw):
    with os.fdopen(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def save(path, report):
    temp = path.with_name(path.name + ".next")
    private_write(temp, (json.dumps(report, indent=2) + "\n").encode())
    os.replace(temp, path)


def fetched_source(commit):
    need(bool(re.fullmatch(r"[0-9a-f]{40}", commit)), "exact_github_sha_required")
    origin = run(["git", "-C", REPO, "remote", "get-url", "origin"]).decode().strip()
    need(origin.rstrip("/") in ("git@github.com:gaoyuan12356416/ai-.git",
                                 "https://github.com/gaoyuan12356416/ai-.git"), "unexpected_github_origin")
    env = dict(os.environ, GIT_SSH_COMMAND="ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes")
    for revision in (BASE, commit):
        run(["git", "-C", REPO, "fetch", "origin", revision], env=env, timeout=120)
        need(run(["git", "-C", REPO, "rev-parse", "FETCH_HEAD"]).decode().strip() == revision, "github_fetch_identity_mismatch")
    return tuple(run(["git", "-C", REPO, "show", revision + ":" + RELATIVE]) for revision in (BASE, commit))


def runtime_check(expected):
    need(run(["findmnt", "-n", "-o", "UUID", "--target", "/mnt/data-disk"]).decode().strip() == MOUNT_UUID, "data_mount_mismatch")
    need(ROOT.is_symlink() and ROOT.resolve() == EXPECTED_ROOT, "unreviewed_runtime_root")
    target = ROOT / RELATIVE
    need(target.is_file() and not target.is_symlink() and target.resolve() == EXPECTED_ROOT / RELATIVE, "unexpected_target_path")
    need(target.read_bytes() == expected, "old_file_hash_mismatch")
    need(sha((ROOT / "features/tt_auto_posts/publisher.py").read_bytes()) == PUBLISHER_SHA, "publisher_hash_mismatch")
    return target


def graceful_contract():
    source = (ROOT / "features/tt_auto_posts/service.py").read_text()
    classes = [n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == "TTAutoPostHTTPServer"]
    need(len(classes) == 1, "graceful_server_class_missing")
    values = {target.id: n.value.value for n in classes[0].body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
              for target in n.targets if isinstance(target, ast.Name)}
    need(values.get("daemon_threads") is False and values.get("block_on_close") is True, "request_threads_not_joined")
    need(all(text in source for text in ("def graceful_shutdown(", "signal.signal(signal.SIGTERM, graceful_shutdown)",
                                        "target=server.shutdown", "server.server_close()")), "graceful_shutdown_missing")
    timeout = show(UNIT, "TimeoutStopUSec")
    # systemctl show renders the microsecond property as a duration string.
    factors = {"h": 3600, "min": 60, "s": 1, "ms": .001, "us": .000001}
    parts = re.findall(r"([0-9]+(?:\.[0-9]+)?)(h|min|ms|us|s)", timeout)
    need(parts and "".join(a + b for a, b in parts) == timeout.replace(" ", "") and
         sum(float(a) * factors[b] for a, b in parts) >= 21600, "stop_timeout_less_than_6h")
    need(show(UNIT, "KillSignal") in ("15", "SIGTERM"), "stop_signal_not_sigterm")
    return {"TimeoutStopUSec": timeout, "SendSIGKILL": show(UNIT, "SendSIGKILL"), "graceful_source_sha256": sha(source.encode())}


@contextlib.contextmanager
def readonly_db(path):
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        yield conn


def quiescent():
    with readonly_db(DATA / "dispatch.sqlite3") as conn:
        need(not conn.execute("SELECT 1 FROM pipeline_job WHERE channel='tt' AND status IN ('running','legacy_running') LIMIT 1").fetchone(), "tt_central_work_running")
    with readonly_db(NATIVE_DB) as conn:
        need(not conn.execute("SELECT 1 FROM tt_auto_task WHERE status IN ('selecting','preparing','publishing') LIMIT 1").fetchone(), "tt_native_work_active")
        # reconciling can be an idle durable state between scheduled GETs.
        # An actual reconciliation remains protected by its native lease and
        # the accepted-connection plus graceful-thread-join guards below.
        for row in conn.execute("SELECT claim_token,lease_expires_at_utc FROM tt_auto_task WHERE claim_token<>''"):
            lease = str(row["lease_expires_at_utc"] or "")
            need(bool(lease), "tt_native_claim_without_expiry")
            expiry = dt.datetime.fromisoformat(lease.replace("Z", "+00:00"))
            need(expiry.tzinfo is not None and expiry <= now(), "tt_native_claim_active")


def accepted():
    return bool(run(["ss", "-Htn", "state", "established", "sport", "=", ":18831"]).strip())


def listener():
    lines = run(["ss", "-Hltn", "sport", "=", ":18831"]).decode().splitlines()
    return bool(lines) and all("127.0.0.1:18831" in line for line in lines)


def gate_present(rule):
    result = subprocess.run(["iptables", "-w", "5", "-C", *rule], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    need(result.returncode in (0, 1), "gate_state_unverifiable")
    return result.returncode == 0


def remove_gate(rule):
    if gate_present(rule):
        run(["iptables", "-w", "5", "-D", *rule])
    need(not gate_present(rule), "gate_still_present")


def wait_until(predicate, code, seconds=45):
    deadline = time.monotonic() + seconds
    while not predicate():
        need(time.monotonic() < deadline, code)
        time.sleep(.25)


def health():
    pid = show(UNIT, "MainPID")
    need(pid.isdigit() and int(pid) > 0, "native_pid_missing")
    environ = process_contract(pid)
    need(environ.get("PIPELINE_TT_DISPATCH_ENABLED") == "1" and
         environ.get("PIPELINE_TT_CANONICAL_BRIDGE_ENABLED") == "1", "pipeline_takeover_not_enabled")
    status, value = request_json("/health")
    need(value.get("service") == "tt-auto-post", "health_service_mismatch")
    gates = value.get("gates") or {}
    need(all(gates.get(key) is True for key in ("direct_audit_approved", "is_open", "live_enabled", "url_property_verified")), "health_gates_closed")
    automation = value.get("automation") or {}
    problems = automation.get("problems")
    if status == 200:
        need(value.get("ok") is True and automation.get("ready") is True and problems == [], "health_failed")
    else:
        allowed = {"tt-auto-post-runner.path:inactive", "tt-auto-post-scheduler.timer:not_firing"}
        need(status == 503 and value.get("ok") is False and automation.get("ready") is False and
             isinstance(problems, list) and problems and all(isinstance(p, str) and p in allowed for p in problems), "unrecognized_health_failure")
    proof = canonical_readback(environ.get("TT_AUTO_POST_INTERNAL_TOKEN", ""))
    need(show(UNIT, "MainPID") == pid, "native_pid_changed_during_verify")
    return {"status": status, "service": "tt-auto-post", "ok": True, "mode": "pipeline_takeover",
            "legacy_automation_problems": problems, "canonical_readback": proof}


def process_contract(pid):
    # Never include the bearer or process environment in audit/output.
    allowed = {b"PIPELINE_TT_DISPATCH_ENABLED", b"PIPELINE_TT_CANONICAL_BRIDGE_ENABLED", b"TT_AUTO_POST_INTERNAL_TOKEN"}
    pairs = (part.split(b"=", 1) for part in (Path("/proc") / pid / "environ").read_bytes().split(b"\0") if b"=" in part)
    return {key.decode(): value.decode() for key, value in pairs if key in allowed}


def request_json(path, *, payload=None, token=""):
    need(path in ("/health", "/internal/pipeline-dispatch/tt/reconcile"), "non_readonly_probe_path")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    headers = {"Content-Type": "application/json"}
    if token: headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request("http://127.0.0.1:18831" + path,
        data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
    try: response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as exc: response = exc
    with response:
        raw = response.read(65537); need(len(raw) <= 65536, "health_response_too_large")
        value = json.loads(raw); need(isinstance(value, dict), "health_response_invalid")
        return response.status, value


def canonical_readback(token):
    need(bool(token), "canonical_bearer_missing")
    def receipt():
        with readonly_db(NATIVE_DB) as conn:
            row = conn.execute("SELECT status,publish_id,unknown_outcome,published_at_utc FROM tt_auto_task WHERE id=987").fetchone()
            need(row is not None and row["status"] == "failed" and not row["unknown_outcome"] and not row["publish_id"], "canonical_probe_task_not_terminal")
            return {"status": row["status"], "publish_id": row["publish_id"] or "", "unknown_outcome": bool(row["unknown_outcome"]), "published_at": row["published_at_utc"] or ""}
    before = receipt()
    status, body = request_json("/internal/pipeline-dispatch/tt/reconcile", token=token,
        payload={"job": {"channel": "tt", "family": "auto", "source_id": "987", "source_key": "tt:auto:987"}})
    need(status == 200 and body == before and receipt() == before, "canonical_readback_mismatch")
    return {"source_key": "tt:auto:987", "status": before["status"], "http_status": 200, "matches_native": True}


def verify_existing(audit_path, commit=None):
    audit_path = Path(audit_path).resolve()
    need(audit_path.name == "deploy.json" and audit_path.parent.parent == DATA.resolve() and
         re.fullmatch(r"tt-readback-fix-[0-9]{8}T[0-9]{6}-[a-f0-9]{8}", audit_path.parent.name), "unexpected_audit_path")
    original = audit_path.read_bytes(); prior = json.loads(original)
    commit = commit or prior.get("sha")
    need(prior.get("sha") == commit and prior.get("expected_sha") == BASE and prior.get("file") == RELATIVE and
         prior.get("runtime_root") == str(EXPECTED_ROOT), "audit_identity_mismatch")
    need(prior.get("cleanup_errors") == [] and prior.get("gate_failsafe_cancelled") is True, "prior_cleanup_not_confirmed")
    output = audit_path.parent / "postverify.json"; need(not output.exists(), "postverify_already_exists")
    old, new = fetched_source(commit)
    need(sha(old) == prior.get("old_sha256") and sha(new) == prior.get("new_sha256"), "audit_source_hash_mismatch")
    backup = audit_path.parent / "tt_readback.py.before"
    need(prior.get("backup") == str(backup) and backup.read_bytes() == old, "backup_mismatch")
    runtime_check(new)
    need(show(UNIT, "ActiveState") == "active" and listener(), "native_not_active_loopback")
    pid = int(show(UNIT, "MainPID")); need(pid > 0 and pid != prior.get("old_pid"), "native_not_restarted")
    marker = prior.get("gate_comment", ""); need(bool(re.fullmatch(r"tt-readback-[a-f0-9]{32}", marker)), "invalid_gate_identity")
    rule = ["OUTPUT", "-d", "127.0.0.1", "-p", "tcp", "--dport", "18831", "-m", "conntrack", "--ctstate", "NEW",
            "-m", "comment", "--comment", marker, "-j", "REJECT", "--reject-with", "tcp-reset"]
    need(not gate_present(rule), "prior_gate_still_present")
    need(set(prior.get("timers", {})) == set(TIMERS), "timer_scope_mismatch")
    for unit, previous in prior["timers"].items():
        need(show(unit, "ActiveState") == previous["active"] and show(unit, "UnitFileState") == previous["enabled"], "timer_not_restored")
    proof = health()
    need(show(UNIT, "MainPID") == str(pid) and audit_path.read_bytes() == original, "verify_identity_changed")
    report = {"status": "verified_existing", "sha": commit, "audit": str(audit_path), "original_audit_sha256": sha(original),
              "original_status": prior.get("status"), "original_error": prior.get("error"), "new_sha256": sha(new),
              "old_pid": prior["old_pid"], "current_pid": pid, "health": proof, "verified_at": now().isoformat(), "restarted": False}
    private_write(output, (json.dumps(report, indent=2) + "\n").encode())
    return report


def apply(commit, old, new):
    target = runtime_check(old)
    contract = graceful_contract()
    quiescent()
    need(show(UNIT, "ActiveState") == "active" and listener(), "native_not_active_loopback")
    old_pid = int(show(UNIT, "MainPID")); need(old_pid > 0, "native_pid_missing")
    python = Path("/proc") / str(old_pid) / "exe"
    run([python.resolve(), "-c", "import sys; compile(sys.stdin.buffer.read(), sys.argv[1], 'exec')", RELATIVE], data=new)
    timers = {}
    for unit in TIMERS:
        need(show(unit, "LoadState") == "loaded", "timer_missing:" + unit)
        state = show(unit, "ActiveState"); need(state in ("active", "inactive"), "timer_state_not_stable")
        timers[unit] = {"active": state, "enabled": show(unit, "UnitFileState")}
    folder = DATA / ("tt-readback-fix-" + now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
    folder.mkdir(mode=0o700)
    backup = folder / "tt_readback.py.before"; private_write(backup, old)
    report = {"sha": commit, "expected_sha": BASE, "runtime_root": str(EXPECTED_ROOT), "file": RELATIVE,
              "old_sha256": sha(old), "new_sha256": sha(new), "old_pid": old_pid, "backup": str(backup),
              "contract": contract, "timers": timers, "started_at": now().isoformat(), "status": "prepared"}
    audit = folder / "deploy.json"; save(audit, report)
    marker = "tt-readback-" + uuid.uuid4().hex
    rule = ["OUTPUT", "-d", "127.0.0.1", "-p", "tcp", "--dport", "18831", "-m", "conntrack", "--ctstate", "NEW",
            "-m", "comment", "--comment", marker, "-j", "REJECT", "--reject-with", "tcp-reset"]
    failsafe = marker + "-gate-cleanup"
    gated = False; armed = False; stopped = False; written = False; paused = []
    try:
        for unit, before in timers.items():
            if before["active"] == "active":
                run(["systemctl", "stop", unit]); paused.append(unit)
                need(show(unit, "ActiveState") == "inactive", "timer_not_stopped")
        # Arm exact-rule cleanup before gating, so loss of this operator process
        # cannot leave native RPC blocked. It cannot touch any other rule.
        report["gate_failsafe_unit"] = failsafe; save(audit, report)
        armed = True
        run(["systemd-run", "--unit", failsafe, "--on-active=120s", "--timer-property=AccuracySec=1s", "--collect",
             "--", "/usr/sbin/iptables", "-w", "5", "-D", *rule])
        # An uncertain command acknowledgement still requires exact cleanup.
        gated = True
        run(["iptables", "-w", "5", "-I", *rule])
        report["gate_comment"] = marker; save(audit, report)
        wait_until(lambda: not accepted(), "accepted_requests_not_drained")
        quiescent(); runtime_check(old)
        need(gate_present(rule), "gate_window_expired")
        need(int(show(UNIT, "MainPID")) == old_pid, "native_pid_changed_before_stop")
        run(["systemctl", "stop", "--no-block", UNIT]); report["status"] = "stop_pending"; save(audit, report)
        wait_until(lambda: show(UNIT, "ActiveState") == "inactive" and show(UNIT, "MainPID") == "0", "graceful_stop_pending")
        stopped = True
        # Catch a central claim that raced the last pre-stop observation. No
        # new native operation can be admitted behind the NEW-only gate.
        quiescent(); need(not accepted(), "accepted_request_after_stop"); runtime_check(old)
        need(gate_present(rule), "gate_window_expired")
        metadata = target.stat(); temporary = target.with_name(target.name + "." + marker + ".next")
        private_write(temporary, new)
        os.chown(temporary, metadata.st_uid, metadata.st_gid)
        temporary.chmod(stat.S_IMODE(metadata.st_mode))
        os.replace(temporary, target); written = True
        report["status"] = "file_replaced"; save(audit, report)
        runtime_check(new)
        run(["systemctl", "start", "--no-block", UNIT])
        wait_until(lambda: show(UNIT, "ActiveState") == "active" and int(show(UNIT, "MainPID") or "0") > 0 and listener(), "new_native_not_ready", 10)
        new_pid = int(show(UNIT, "MainPID")); need(new_pid != old_pid, "native_pid_not_replaced")
        remove_gate(rule); gated = False
        report.update(new_pid=new_pid, health=health(), status="applied", completed_at=now().isoformat())
    except BaseException as exc:
        report["error"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        # A central claim may have raced the gate while native was stopped.
        # Restore the verified old code without making that queued handoff wait
        # on an offline service. No handler is restarted or interrupted.
        if stopped and not written:
            try:
                runtime_check(old)
                need(show(UNIT, "ActiveState") == "inactive" and show(UNIT, "MainPID") == "0", "old_native_not_stopped")
                run(["systemctl", "start", "--no-block", UNIT]); report["original_start_requested"] = True
            except Exception as recovery:
                report["recovery_error"] = str(recovery) if isinstance(recovery, RuntimeError) else type(recovery).__name__
        raise
    finally:
        errors = []
        if gated:
            try:
                remove_gate(rule); gated = False
            except Exception: errors.append("gate_removal_failed")
        if armed and not gated:
            try:
                if show(failsafe + ".timer", "LoadState") != "not-found":
                    run(["systemctl", "stop", failsafe + ".timer"])
                report["gate_failsafe_cancelled"] = True
            except Exception: errors.append("gate_failsafe_cancel_failed")
        elif armed:
            # Preserve the already armed cleanup if immediate removal failed.
            report["gate_failsafe_retained"] = True
        for unit in paused:
            try:
                run(["systemctl", "start", unit])
                need(show(unit, "ActiveState") == timers[unit]["active"], "timer_active_state_changed")
            except Exception: errors.append("timer_restore_failed:" + unit)
        for unit, before in timers.items():
            try: need(show(unit, "UnitFileState") == before["enabled"], "timer_enabled_state_changed")
            except Exception: errors.append("timer_enabled_state_changed:" + unit)
        report["cleanup_errors"] = errors; report["cleanup_at"] = now().isoformat(); save(audit, report)
        if errors: raise RuntimeError("deployment_cleanup_failed:" + str(audit))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify-existing", metavar="DEPLOY_JSON")
    args = parser.parse_args()
    need(os.geteuid() == 0, "root_required")
    if args.verify_existing:
        need(not args.apply, "verify_cannot_apply")
        print(json.dumps(verify_existing(args.verify_existing, args.sha)))
        return
    need(bool(args.sha), "exact_github_sha_required")
    old, new = fetched_source(args.sha)
    need(old != new, "readback_patch_missing")
    runtime_check(old); graceful_contract(); quiescent()
    compile(new, RELATIVE, "exec")
    if not args.apply:
        print(json.dumps({"status": "preflight_ok", "sha": args.sha, "expected_sha": BASE, "old_sha256": sha(old), "new_sha256": sha(new), "apply": False}))
        return
    import fcntl
    with (DATA / "tt-readback-deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps(apply(args.sha, old, new)))


if __name__ == "__main__":
    main()
