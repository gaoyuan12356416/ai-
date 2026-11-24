"""Switch the two verified monitor modules at an idle coordinator boundary."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing


UNIT = "pipeline-coordinator.service"
LEDGER = "file:/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3?mode=ro"
HEALTH = "/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py"


def unit_property(unit, name):
    return subprocess.check_output(["systemctl", "show", unit, "-p", name, "--value"], text=True).strip()


def swap(folder, action):
    record = json.loads((folder / "stage.json").read_bytes())
    live = Path(record["root"])
    assert live.resolve() == Path("/opt/pipeline-dispatch/current").resolve(), "deployment target changed"
    assert unit_property(UNIT, "ActiveState") == "active"
    assert not Path("/run/pipeline-service-drain", UNIT + ".json").exists(), "another drain is active"
    before = unit_property(UNIT, "MainPID")
    cpu_node = unit_property("pipeline-node-cpu-main", "MainPID")
    assert (Path("/proc") / before / "cwd").resolve() == live
    expected, destination = ("before", "after") if action == "apply" else ("after", "before")
    source = folder / ("source" if action == "apply" else "before")
    fingerprint = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    for name, hashes in record["files"].items():
        assert fingerprint(live / name) == hashes[expected], "active code changed: " + name
        assert fingerprint(source / name) == hashes[destination], "staged code changed: " + name
    with closing(sqlite3.connect(LEDGER, uri=True, timeout=1)) as db:
        db.execute("PRAGMA query_only=ON")
        busy = db.execute("SELECT 1 FROM pipeline_job WHERE status IN ('running','legacy_running') LIMIT 1").fetchone()
    if busy:
        return {"status": "waiting_for_idle", "modified": False}
    names = list(record["files"])
    swapped = []
    try:
        for name in names:
            target = live / name
            tmp = target.with_suffix(".clock-next")
            assert not tmp.exists(), "another module update is staged"
            shutil.copy2(source / name, tmp)
            st = target.stat()
            os.chown(tmp, st.st_uid, st.st_gid)
            tmp.chmod(st.st_mode & 0o777)
            os.replace(tmp, target)
            swapped.append(name)
    except BaseException:
        previous = folder / ("before" if action == "apply" else "source")
        for name in reversed(swapped):
            target = live / name
            tmp = target.with_suffix(".clock-revert")
            shutil.copy2(previous / name,tmp)
            st=target.stat()
            os.chown(tmp,st.st_uid,st.st_gid)
            os.replace(tmp,target)
        raise
    transition = {"action":action, "previous_pid":before, "cpu_worker_pid":cpu_node,
                  "requested_at":time.time(), "stage":"restart_requested"}
    receipt = folder / (action + ".json")
    receipt.write_text(json.dumps(transition))
    # Its existing systemd drain guards the race with a newly accepted job.
    subprocess.run(["systemctl","restart","--no-block",UNIT],check=True)
    return transition


def confirm(folder, action):
    path = folder / (action + ".json")
    rec = json.loads(path.read_bytes())
    new = unit_property(UNIT,"MainPID")
    if new in ("0",rec["previous_pid"]) or unit_property(UNIT,"ActiveState")!="active":
        return {"status":"transitioning", "pid":new}
    c=http.client.HTTPConnection("127.0.0.1",18846,timeout=4)
    try:
        c.request("GET","/health");response=c.getresponse();response.read()
        assert response.status==200, "coordinator health check failed"
    finally:c.close()
    assert rec["cpu_worker_pid"]==unit_property("pipeline-node-cpu-main","MainPID")
    check=folder / (action+"-schedulers.json")
    subprocess.run(["/usr/bin/python3",HEALTH,"restore","--baseline",str(folder/"schedulers-before.json"),
                    "--wait-seconds","25","--output",str(check)],check=True,stdout=subprocess.PIPE,timeout=50)
    assert json.loads(check.read_bytes())["ok"]
    rec.update(stage="active",current_pid=new,checked_at=time.time())
    path.write_text(json.dumps(rec))
    return rec


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("folder",type=Path)
    parser.add_argument("action",choices=("apply","rollback","confirm-apply","confirm-rollback"))
    args=parser.parse_args()
    print(json.dumps(confirm(args.folder,args.action[8:]) if args.action.startswith("confirm-") else swap(args.folder,args.action)))
