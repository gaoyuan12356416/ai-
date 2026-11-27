"""Switch only the HK assignment API; finish accepted work and preserve receipts."""
import argparse
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time

ROOT = Path("/data/fb-page-assignment-v2")
POINTER = Path("/opt/fb-page-assignment-v2/current")
UNIT = "fb-page-v2.service"


def pid():
    return int(subprocess.check_output(["systemctl", "show", "--value", "-p", "MainPID", UNIT], text=True).strip())


def facts(db):
    return {r[0]: list(r[1:]) for r in db.execute(
        "SELECT task_id,external_id,fingerprint,pool,response FROM assignment")}


def db_open():
    return sqlite3.connect("file:" + str(ROOT / "state/assignment-v2.sqlite3") + "?mode=ro", uri=True)


def write(path, value):
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())


def health():
    client = http.client.HTTPConnection("10.89.0.125", 8944, timeout=2)
    try:
        client.request("GET", "/healthz")
        response = client.getresponse()
        return response.status == 200 and json.loads(response.read()) == {"ok": True, "version": 2}
    finally:
        client.close()


def activate(target):
    # Signal only the API owner. Its normal shutdown awaits clients, selectors,
    # receipt writes and background workers. Never force-stop an in-flight job.
    current_pid = pid()
    if current_pid:
        os.kill(current_pid, signal.SIGTERM)
        until = time.monotonic() + 60
        while pid() and time.monotonic() < until:
            time.sleep(0.5)
        if pid():
            raise RuntimeError("API still finishing accepted requests; pointer unchanged, no force kill")
    temp = POINTER.with_name(".selector-next-" + str(os.getpid()))
    temp.symlink_to(target)
    os.replace(temp, POINTER)
    subprocess.run(["systemctl", "start", UNIT], check=True, timeout=20)
    until = time.monotonic() + 60
    while time.monotonic() < until:
        try:
            if health() and pid() and Path("/proc", str(pid()), "cwd").resolve() == target:
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError("new selector did not become healthy")


def install(release, expected):
    release = Path(release).resolve()
    previous = POINTER.resolve()
    if release.parent != (ROOT / "releases").resolve() or previous.name != expected:
        raise ValueError("release path or deployed baseline changed")
    if not (release / "features/fb_page_assignment_v2/selection.py").is_file():
        raise ValueError("selector source missing")
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
    backup = ROOT / "deployments" / ("direct-match-" + stamp + "-" + release.name[:8])
    backup.mkdir(mode=0o700)
    unit = subprocess.check_output(["systemctl", "cat", UNIT])
    (backup / "unit.before").write_bytes(unit)
    with closing(db_open()) as source, closing(sqlite3.connect(str(backup / "assignment.before.sqlite3"))) as dst:
        source.backup(dst)
        before = facts(dst)
    write(backup / "state.json", {"previous": str(previous), "candidate": str(release),
                                  "old_pid": pid(), "old_receipts": len(before),
                                  "unit_sha256": hashlib.sha256(unit).hexdigest()})
    (backup / "switch_selector.py").write_bytes(Path(__file__).read_bytes())
    try:
        activate(release)
        with closing(db_open()) as db:
            after = facts(db)
        if any(after.get(k) != v for k, v in before.items()):
            raise RuntimeError("an original assignment receipt changed")
        if subprocess.check_output(["systemctl", "cat", UNIT]) != unit:
            raise RuntimeError("API unit configuration changed during activation")
        result = {"release": release.name, "backup": str(backup), "pid": pid(),
                  "original_receipts_unchanged": len(before), "health": True}
        write(backup / "acceptance.json", result)
        print(json.dumps(result))
    except BaseException:
        if POINTER.resolve() == release:
            activate(previous)
        raise


def rollback(folder):
    folder = Path(folder).resolve()
    if folder.parent != (ROOT / "deployments").resolve():
        raise ValueError("invalid backup path")
    state = json.loads((folder / "state.json").read_text())
    current = POINTER.resolve()
    if str(current) == state["previous"]:
        print("previous release already selected")
        return
    if str(current) != state["candidate"]:
        raise RuntimeError("a later release is active")
    activate(Path(state["previous"]))
    print("rolled back selector code; all current receipts and catalog retained")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="action", required=True)
    start = sub.add_parser("install")
    start.add_argument("release")
    start.add_argument("--expected", required=True)
    back = sub.add_parser("rollback")
    back.add_argument("backup")
    args = p.parse_args()
    if args.action == "install":
        install(args.release, args.expected)
    else:
        rollback(args.backup)
