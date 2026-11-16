"""Prepare, enable, or independently stop new COS and/or priority admissions."""
import argparse
import contextlib
import grp
from http.client import HTTPConnection
import json
import os
import pwd
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from pipeline_cos_prepare_release import NATIVE, COORDINATOR, ROOT, run, write, put_json, owner

ETC = Path("/etc/pipeline-cos-cloud")
UNITS = Path("/etc/systemd/system")
USER = "pipeline-cos"
ACTOR = {"is_admin":True, "user_id":"cos-dispatch-deployment", "tenant_key":"operations"}
CONFIG = Path("/etc/pipeline-coordinator.json")


def owner_env(unit=COORDINATOR):
    pid=int(run(["systemctl","show",unit,"-p","MainPID","--value"]))
    return dict(p.decode().split("=",1) for p in (Path("/proc")/str(pid)/"environ").read_bytes().split(b"\0") if b"=" in p)


def http(port, method, path, data=None, bearer="", source=None):
    opts={"timeout":20}
    if source:opts["source_address"]=(source,0)
    con=HTTPConnection("127.0.0.1",port,**opts)
    try:
        body=json.dumps(data).encode() if data is not None else None
        headers={"Content-Type":"application/json","Connection":"close"}
        if bearer:headers["Authorization"]="Bearer "+bearer
        con.request(method,path,body=body,headers=headers)
        res=con.getresponse()
        return res.status,json.loads(res.read(8*1024*1024))
    finally:
        con.close()


def config():
    db=Path(json.loads(CONFIG.read_text())["db_path"])
    with contextlib.closing(sqlite3.connect(db.as_uri()+"?mode=ro",uri=True)) as c:
        version, enc=c.execute("SELECT version,config_json FROM pipeline_config WHERE id=1").fetchone()
    return dict(json.loads(enc),version=version)


def admin(path, payload):
    code,out=http(18846,"POST","/internal/pipeline-monitor/"+path,dict(payload,actor=ACTOR),
                  owner_env()["PIPELINE_DISPATCH_INTERNAL_TOKEN"])
    if code!=200:raise RuntimeError("admin request failed "+str(code)+" "+str(out.get("code","")))
    return out


def capacity(**patch):
    current=config()
    admin("capacity",dict(patch,expected_version=current["version"]))
    result=config()
    pool=result["hosts"]["cos-cloud"]["prepare"]
    print("routing",result.get("prepare_routing","legacy"),"COS",pool,"version",result["version"])
    return result


def unit(name, body):
    p=UNITS/name
    if p.exists() and "COS preparation" not in p.read_text():
        raise RuntimeError("unit name already used "+name)
    write(p,body)


def stage(release, manifest):
    node=Path(manifest["node"])
    try:
        user=pwd.getpwnam(USER)
    except KeyError:
        run(["useradd","--system","--no-create-home","--shell","/sbin/nologin",USER])
        user=pwd.getpwnam(USER)
    gid=user.pw_gid
    from pipeline_cos_dependencies import provision
    provision(node)
    ETC.mkdir(exist_ok=True,mode=0o750);os.chown(ETC,0,gid);ETC.chmod(0o750)
    state=ROOT/"state";state.mkdir(exist_ok=True,mode=0o700);os.chown(state,user.pw_uid,gid)
    cfg=json.loads((release/"launch/worker-base.json").read_text())
    for key in ("catalog","outro","font","logo"):
        asset=Path(cfg[key]); path=asset
        if subprocess.run(["runuser","-u",USER,"--","test","-r",str(asset)]).returncode:
            assets=ROOT/"assets";assets.mkdir(exist_ok=True)
            path=assets/(key+"-"+cfg[key+"_sha256"][:16]+asset.suffix)
            if not path.exists():
                if asset.is_dir():shutil.copytree(asset,path)
                else:shutil.copy2(asset,path)
            if subprocess.run(["runuser","-u",USER,"--","test","-r",str(path)]).returncode:
                raise RuntimeError("cloud user cannot read the approved "+key)
            cfg[key]=str(path)
    cfg["credential_gid"]=gid
    put_json(ETC/"worker.json",cfg);os.chown(ETC/"worker.json",0,gid);os.chmod(ETC/"worker.json",0o640)
    if not subprocess.run(["runuser","-u",USER,"--","test","-r",str(node/"features/pipeline_dispatch/cloud_node.py")]).returncode==0:
        raise RuntimeError("node package not readable")
    for ident, _, _, _, _, _ in NATIVE:
        name="pipeline-cos-native-"+ident+".service"
        unit(name,"[Unit]\nDescription=COS preparation "+ident+" canonical bridge\nAfter=network.target\n[Service]\nType=simple\nExecStart=/usr/bin/python3 "+str(node/"scripts/pipeline_cos_launch.py")+" "+str(release/"launch"/(ident+"-launch.json"))+"\nRestart=on-failure\nRestartSec=5\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n")
    unit("pipeline-cos-credentials.service","[Unit]\nDescription=COS preparation scoped credential refresh\n[Service]\nType=oneshot\nUMask=0077\nExecStart=/usr/bin/python3 "+str(node/"scripts/pipeline_cos_credentials.py")+" --config "+str(ETC/"worker.json")+" --issuer /root/drama_material_service/.env\n")
    unit("pipeline-cos-credentials.timer","[Unit]\nDescription=COS preparation scoped credential renewal\n[Timer]\nOnBootSec=1min\nOnUnitActiveSec=45min\nUnit=pipeline-cos-credentials.service\n[Install]\nWantedBy=timers.target\n")
    unit("pipeline-cos-node.service","[Unit]\nDescription=COS preparation worker\nAfter=network-online.target pipeline-coordinator.service\n[Service]\nType=simple\nUser="+USER+"\nWorkingDirectory="+str(node)+"\nExecStart=/usr/bin/python3 -m features.pipeline_dispatch.cloud_node --config "+str(ETC/"worker.json")+"\nRestart=on-failure\nRestartSec=5\nNice=10\nUMask=0077\nNoNewPrivileges=true\n[Install]\nWantedBy=multi-user.target\n")
    run(["systemctl","daemon-reload"])
    run(["systemctl","start","pipeline-cos-credentials.service"])
    run(["systemctl","enable","--now","pipeline-cos-credentials.timer"])
    for ident,_,_,_,_,_ in NATIVE:
        run(["systemctl","enable","--now","pipeline-cos-native-"+ident+".service"])
    verify_bridges()


def verify_bridges():
    tokens=owner_env()
    for ident,_,_,port,channel,token in NATIVE:
        for attempt in range(30):
            try:
                status,ans=http(port,"GET","/health")
                if status==200 and ans.get("ready") and ans["channel"]==channel:break
            except OSError:pass
            time.sleep(.5)
        else:raise RuntimeError("unable to warm "+ident)
        secret=tokens["PIPELINE_X_TAG_CANONICAL_TOKEN" if ident=="x-tag" else token]
        response,body=http(port,"POST","/internal/pipeline-dispatch/"+channel+"/begin",{},secret)
        if response!=409 or body.get("code")!="cos_prepare_only":
            raise RuntimeError("canonical authentication/prepare-only mismatch "+ident)
        print("ready",ident,port,flush=True)


def register(release):
    manifest=json.loads((release/"release.json").read_text())
    if owner(COORDINATOR)!=Path(manifest["candidate"]):raise RuntimeError("compatible coordinator required")
    guild=pwd.getpwnam(USER).pw_gid
    if not (ETC/"node.json").exists():
        token=admin("enroll",{"label":"cos-cloud"})["token"]
        code,info=http(18846,"POST","/internal/pipeline/nodes/enroll",{"node_id":"cos-cloud"},token)
        if code!=200 or info.get("node_id")!="cos-cloud":raise RuntimeError("enrollment failed")
        put_json(ETC/"node.json",{"node_id":"cos-cloud","node_token":info["node_token"]})
        os.chown(ETC/"node.json",0,guild);os.chmod(ETC/"node.json",0o640)
    run(["systemctl","enable","--now","pipeline-cos-node.service"])
    for attempt in range(45):
        pool=config()["hosts"].get("cos-cloud",{}).get("prepare")
        if pool:
            print("enrolled",pool)
            return
        time.sleep(1)
    raise RuntimeError("cloud node did not register")


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("release",type=Path)
    parser.add_argument("action",choices=["stage","verify","register","enable","stop-cloud","legacy-routing","priority-routing"])
    a=parser.parse_args()
    if a.action=="stage":stage(a.release,json.loads((a.release/"release.json").read_text()))
    elif a.action=="verify":verify_bridges()
    elif a.action=="register":register(a.release)
    elif a.action=="enable":capacity(host_id="cos-cloud",stage="prepare",max_workers=1,min_pending=100,scaling_mode="manual",prepare_routing="priority")
    elif a.action=="stop-cloud":capacity(host_id="cos-cloud",stage="prepare",max_workers=0,scaling_mode="manual")
    else:capacity(prepare_routing="priority" if a.action=="priority-routing" else "legacy")
