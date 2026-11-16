"""Install a GitHub-fetched worker revision without switching the coordinator."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

from pipeline_cos_prepare_release import ROOT, run, write, put_json
from pipeline_cos_dependencies import provision, tools_for_worker

CFG=Path("/etc/pipeline-cos-cloud/worker.json")
DROP=Path("/etc/systemd/system/pipeline-cos-node.service.d/50-worker-version.conf")


def main(source, mirror):
    sha=run(["git","--git-dir="+str(mirror),"rev-parse","refs/cos-dispatch/validation"])
    if source.name!=sha or not ROOT.parent.is_mount():raise RuntimeError("worker source revision mismatch")
    node=ROOT/"worker-versions"/sha
    node.mkdir(parents=True,exist_ok=True)
    shutil.copytree(source/"features",node/"features",dirs_exist_ok=True)
    provision(node)
    before=CFG.read_bytes();cfg=tools_for_worker(node,json.loads(before))
    envtest="from PIL import Image,ImageFont; import qcloud_cos; from features.pipeline_dispatch.cloud_node import CloudNode"
    subprocess.run(["runuser","-u","pipeline-cos","--","/usr/bin/python3","-c",envtest],cwd=node,check=True)
    backup=node/"deployment";backup.mkdir(mode=0o700,exist_ok=True)
    write(backup/"previous-config.json",before,0o600)
    old=DROP.read_bytes() if DROP.exists() else None
    if old:write(backup/"previous-unit.conf",old,0o600)
    db=Path(cfg["state_root"])/"actions.sqlite3"
    # Finish a currently executing operation. Durable postponed work is picked
    # up with its original generation, output keys and provider job identities.
    deadline=time.monotonic()+240
    while time.monotonic()<deadline:
        conn=sqlite3.connect(db.as_uri()+"?mode=ro",uri=True)
        try:
            running=conn.execute("SELECT COUNT(*) FROM work WHERE next_poll<=?",(time.time()+3,)).fetchone()[0]
        finally:conn.close()
        if not running:break
        time.sleep(1)
    else:raise RuntimeError("existing worker still executing; version not switched")
    owner=CFG.stat()
    write(CFG,json.dumps(cfg),0o640);os.chown(CFG,owner.st_uid,owner.st_gid)
    write(DROP,"[Service]\nWorkingDirectory="+str(node)+"\n")
    try:
        run(["systemctl","daemon-reload"])
        run(["systemctl","restart","pipeline-cos-node"],timeout=120)
        pid=int(run(["systemctl","show","pipeline-cos-node","-p","MainPID","--value"]))
        if (Path("/proc")/str(pid)/"cwd").resolve()!=node:raise RuntimeError("worker version did not switch")
    except BaseException:
        write(CFG,before,0o640);os.chown(CFG,owner.st_uid,owner.st_gid)
        if old:write(DROP,old)
        elif DROP.exists():DROP.unlink()
        run(["systemctl","daemon-reload"]);run(["systemctl","restart","pipeline-cos-node"],timeout=120)
        raise
    put_json(backup/"version.json",{"worker":sha,"previous_config":"previous-config.json"})
    print("isolated worker activated",sha,flush=True)


if __name__=="__main__":
    a=argparse.ArgumentParser();a.add_argument("source",type=Path);a.add_argument("mirror",type=Path)
    args=a.parse_args();main(args.source,args.mirror)
