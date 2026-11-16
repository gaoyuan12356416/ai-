"""Warm HTTP handoff on loopback. The stable systemd unit remains the only collector."""
import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pipeline_cos_prepare_release import COORDINATOR, HEALTH, LIVE, STATIC, file_hash, owner, put_json, run, write
from pipeline_cos_control import http, verify_bridges

WARM = "pipeline-cos-warm"
MAIN = 18846
AUX = 18940
DROP = Path("/etc/systemd/system/pipeline-coordinator.service.d/82-cos-preparation.conf")
TEMP = Path("/run/systemd/system/pipeline-coordinator.service.d/99-cos-http-handoff.conf")
NGINX = Path("/etc/nginx/default.d/pipeline-cos-capacity.conf")


def rule(src, target):
    return ["OUTPUT","-p","tcp","-d","127.0.0.1/32","!","-s","127.0.0.2/32","--dport",str(src),
            "-m","comment","--comment","cos-http-drain-"+str(src),"-j","REDIRECT","--to-ports",str(target)]


def redirect(cmd, src, target):
    subprocess.run(["iptables","-w","5","-t","nat",cmd]+rule(src,target),check=True,capture_output=True)


def health(port, alternate=False):
    try:
        return http(port,"GET","/health",source="127.0.0.2" if alternate else None)
    except (OSError,ValueError):
        return 0,{}


def wait(f, seconds, message):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if f():return
        time.sleep(.3)
    raise RuntimeError(message)


def smoke():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            data=json.dumps({"port":self.server.server_port}).encode()
            self.send_response(200);self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
    servers=[ThreadingHTTPServer(("127.0.0.1",0),Handler) for _ in range(2)]
    for s in servers:threading.Thread(target=s.serve_forever,daemon=True).start()
    a,b=[s.server_port for s in servers]
    try:
        redirect("-I",a,b)
        try:
            if health(a)[1].get("port")!=b or health(a,True)[1].get("port")!=a:
                raise RuntimeError("new/direct loopback handoff rehearsal failed")
        finally:redirect("-D",a,b)
    finally:
        for s in servers:s.shutdown();s.server_close()
    print("loopback handoff rehearsed",flush=True)


def warm(release):
    manifest=json.loads((release/"release.json").read_text())
    verify_bridges()
    if health(AUX)[1].get("cos_prepare"):
        pid=int(run(["systemctl","show",WARM,"-p","MainPID","--value"]))
        if (Path("/proc")/str(pid)/"cwd").resolve()!=Path(manifest["candidate"]):
            raise RuntimeError("standby revision differs")
        return
    run(["systemd-run","--unit",WARM,"-p","UMask=0077","/usr/bin/python3",
         str(Path(manifest["node"])/"scripts/pipeline_cos_launch.py"),str(release/"launch/standby.json")])
    wait(lambda: health(AUX)[1].get("cos_prepare"),45,"standby did not become ready")
    print("standby ready; collector disabled",flush=True)


def socket_count(pid, port):
    groups=run(["ss","-Htanp","( sport = :"+str(port)+" )"]).splitlines()
    return sum(int(l.split()[1]) if l.split()[0]=="LISTEN" else 1 for l in groups if "pid="+str(pid)+"," in l)


def drained(pid,port, thread_limit):
    tasks=Path("/proc")/str(pid)/"task"
    return not tasks.exists() or (socket_count(pid,port)==0 and len(list(tasks.iterdir()))<=thread_limit)


def point(path):
    temp=LIVE.with_name(".cos-current-"+str(os.getpid()))
    temp.symlink_to(path)
    os.replace(temp,LIVE)


def switch(release):
    mf=json.loads((release/"release.json").read_text())
    previous=Path(mf["previous"]);candidate=Path(mf["candidate"])
    if owner(COORDINATOR)==candidate and health(MAIN)[1].get("cos_prepare"):
        return
    if previous!=LIVE.resolve() or any(file_hash(previous/"features/pipeline_dispatch"/name)!=expected for name,expected in mf["live_checksums"].items()):
        raise RuntimeError("active coordinator changed since preflight")
    verify_bridges()
    smoke()
    warm(release)
    old_pid=int(run(["systemctl","show",COORDINATOR,"-p","MainPID","--value"]))
    warm_pid=int(run(["systemctl","show",WARM,"-p","MainPID","--value"]))
    snapshot=release/"backup/schedulers-before.json"
    run(["python3",HEALTH,"snapshot","--lane","coordinator","--output",snapshot])
    if (Path("/run/pipeline-service-drain")/(COORDINATOR+".json")).exists() or TEMP.exists():
        raise RuntimeError("other service handoff in progress")
    if health(MAIN,True)[0]!=200 or health(AUX)[1].get("listener_port")!=AUX:
        raise RuntimeError("handoff endpoints changed")
    recorded = DROP.read_bytes() if DROP.exists() else None
    if recorded:write(release/"backup/original-cos-drop",recorded,0o600)
    switched=False
    redirected=False
    try:
        redirect("-I",MAIN,AUX);redirected=True
        if health(MAIN)[1].get("listener_port")!=AUX or health(MAIN,True)[1].get("listener_port")==AUX:
            raise RuntimeError("new requests did not reach standby")
        # Main loop + single collector. Drain already accepted HTTP handlers
        # while new requests, heartbeats and publisher callbacks use standby.
        wait(lambda:drained(old_pid,MAIN,2),240,"old accepted handlers still running")
        if previous!=owner(COORDINATOR) or previous!=LIVE.resolve():raise RuntimeError("parallel deployment detected")
        compact=json.dumps(mf["ports"],separators=(",",":"))
        write(DROP,"[Service]\nEnvironment='PIPELINE_COS_NATIVE_PORTS="+compact+"'\n")
        write(TEMP,"[Service]\nExecStop=\n")
        run(["systemctl","daemon-reload"])
        if run(["systemctl","show",COORDINATOR,"-p","ExecStop","--value"]):
            raise RuntimeError("scoped HTTP stop override not installed")
        point(candidate);switched=True
        run(["systemctl","restart",COORDINATOR],timeout=120)
        wait(lambda:owner(COORDINATOR)==candidate and health(MAIN,True)[1].get("cos_prepare"),45,"new stable endpoint unavailable")
    except BaseException:
        if switched:
            point(previous)
            if recorded is not None:write(DROP,recorded)
            elif DROP.exists():DROP.unlink()
            run(["systemctl","daemon-reload"])
            run(["systemctl","restart",COORDINATOR],timeout=120)
            wait(lambda:owner(COORDINATOR)==previous and health(MAIN,True)[0]==200,45,"stable rollback not ready; standby serves requests")
        raise
    finally:
        if health(MAIN,True)[0]==200 and redirected:redirect("-D",MAIN,AUX)
        if TEMP.exists():TEMP.unlink();run(["systemctl","daemon-reload"])
    if health(MAIN)[1].get("cos_prepare") is not True:raise RuntimeError("stable HTTP verification failed")
    wait(lambda:drained(warm_pid,AUX,1),240,"standby still finishing previously accepted work")
    run(["systemctl","stop",WARM])
    run(["python3",HEALTH,"restore","--lane","coordinator","--baseline",snapshot,"--wait-seconds","25"])
    print("stable revision active, standby drained, scheduler health restored",flush=True)


def ui(release):
    mf=json.loads((release/"release.json").read_text())
    if LIVE.resolve()!=Path(mf["candidate"]):raise RuntimeError("coordinator not live")
    conf=("location = /api/pipeline-monitor/cos-capacity {\n    client_max_body_size 8k;\n"
          "    proxy_pass http://127.0.0.1:18846;\n    proxy_set_header Host $host;\n"
          "    proxy_connect_timeout 5s;\n    proxy_read_timeout 15s;\n"
          "    add_header Cache-Control \"no-store\" always;\n}\n")
    if NGINX.exists() and NGINX.read_text()!=conf:raise RuntimeError("nginx location already used")
    new=not NGINX.exists()
    if new:write(NGINX,conf)
    try:
        subprocess.run(["nginx","-t"],capture_output=True,check=True)
        run(["systemctl","reload","nginx"])
    except BaseException:
        if new:NGINX.unlink()
        raise
    for d in mf["static_dirs"]:
        for name in ("pipeline-monitor.js","pipeline-monitor.html"):
            path=Path(d)/name;source=Path(mf["candidate"])/"static"/name
            if file_hash(path) not in (mf["old_static"][name],file_hash(source)):
                raise RuntimeError("concurrent static change: "+name)
            write(path,source.read_bytes())
    print("monitor assets activated",flush=True)


if __name__=="__main__":
    a=argparse.ArgumentParser()
    a.add_argument("release",type=Path);a.add_argument("action",choices=["smoke","warm","switch","ui"])
    args=a.parse_args()
    with Path("/run/pipeline-cos-handoff.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.action=="smoke":smoke()
        else:{"warm":warm,"switch":switch,"ui":ui}[args.action](args.release)
