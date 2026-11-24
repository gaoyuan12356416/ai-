"""Activate or roll back the two existing X Tag owners in a scoped publishing gap."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import time

from verify_scope import open_db, ROOT

OPS = Path(__file__).resolve().parent
HEALTH = "/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py"
RUNNER = "x-tag-auto-runner"
OWNERS = ["x-tag-post.service","x-tag-auto.service"]


def call(*cmd):
    subprocess.run(cmd,check=True)


def state(unit):
    return subprocess.check_output(["systemctl","show","-p","ActiveState","--value",unit],text=True).strip()


def busy():
    with closing(open_db("live")) as db:
        external = db.execute("""SELECT count(*) FROM d.pipeline_job j JOIN p.x_post_queue q
               ON j.source_key='x:queue:'||q.id WHERE j.channel='x' AND j.status='running'
               AND j.stage IN ('prepare','publish')""").fetchone()[0]
        claimed = db.execute("""SELECT count(*) FROM x_auto_task WHERE claim_token<>''
               AND julianday(lease_expires_at_utc)>julianday('now')""").fetchone()[0]
    return external+claimed or state(RUNNER+".service") in {"activating","active","deactivating"}


def main(mode):
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix=ROOT/(mode+"-"+stamp)
    before=str(prefix)+"-before.json"
    after=str(prefix)+"-after.json"
    call(sys.executable,HEALTH,"snapshot","--lane","x","--output",before)
    was_active=state(RUNNER+".timer")=="active"
    owners=[unit for unit in OWNERS if state(unit)=="active"]
    if len(owners)!=len(OWNERS):
        raise RuntimeError("check the existing X Tag services before switching")
    # Wait for any externally owned request to finish with its original owner.
    while busy():
        time.sleep(3)
    if was_active:
        call("systemctl","stop",RUNNER+".timer")
    try:
        while busy():
            time.sleep(2)
        installer=str(OPS/"install.py")
        call(sys.executable,installer,"rollback" if mode=="rollback" else "code")
        call("systemctl","daemon-reload")
        call("systemctl","restart",*owners)
        if mode=="activate":
            call(sys.executable,installer,"web")
        if not all(state(u)=="active" for u in owners):
            raise RuntimeError("X Tag service did not start")
    finally:
        if was_active:
            call("systemctl","start",RUNNER+".timer")
        call(sys.executable,HEALTH,"restore","--baseline",before,"--wait-seconds","25","--output",after)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("mode",choices=("activate","rollback"))
    main(parser.parse_args().mode)
