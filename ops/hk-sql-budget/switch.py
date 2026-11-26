"""Cut over only the two HK consumers which execute SQL. Run with Python 3.10+."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from features.hk_sql_budget.ledger import Ledger


METADATA = "ai-backend-offload-metadata-reader.service"
REFRESH = "ai-backend-offload-campaign-refresh.service"
UNITS = {METADATA: "features.gpu_offload.metadata_reader", REFRESH: "features.gpu_offload.campaign_schedule"}
DROPIN = "95-hk-private-sql-budget.conf"


def run(*args, timeout=15):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=timeout).stdout.strip()


def prop(unit, key):
    return run("systemctl", "show", unit, "--property="+key, "--value")


def atomic(path, data, mode=0o644):
    path = Path(path)
    path.parent.mkdir(exist_ok=True, parents=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix="."+path.name+".")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def dropin(unit):
    return Path("/etc/systemd/system", unit+".d", DROPIN)


def manifest(directory):
    return json.loads((directory/"consumers.json").read_text())


def digest(data):
    return hashlib.sha256(data).hexdigest()


def restore(directory, ledger):
    facts = manifest(directory)
    ledger.configure(legacy_reserved=1)
    # Hold only new acquisitions if a rollback was requested at full capacity.
    end = time.monotonic()+60
    while ledger.status()["in_use"] > ledger.status()["capacity"]:
        if time.monotonic() >= end:
            raise RuntimeError("legacy_reservation_waiting_for_inflight_queries")
        time.sleep(0.2)
    changed = False
    old_metadata = False
    for unit, details in facts["units"].items():
        path = dropin(unit)
        if not path.exists():
            if details["previous"]:
                raise RuntimeError("consumer_dropin_changed_since_backup")
            continue
        stored = path.read_bytes()
        backup = directory/(unit+".previous")
        if details["previous"] and stored == backup.read_bytes():
            continue
        if digest(stored) != details["installed_sha256"]:
            raise RuntimeError("consumer_dropin_changed_since_backup")
        if details["previous"]:
            atomic(path, backup.read_bytes())
        else:
            path.unlink()
        changed = True
        old_metadata |= unit == METADATA
    if changed:
        run("systemctl", "daemon-reload")
        if old_metadata and facts["units"][METADATA]["was_active"]:
            run("systemctl", "restart", METADATA, timeout=150)
    ledger.configure(capacity=facts.get("previous_capacity",30),
                     legacy_reserved=facts.get("rollback_legacy",1))


def apply(directory, ledger):
    if not re.search(r"\binet 10\.89\.0\.125/", run("ip", "-4", "-o", "addr", "show")):
        raise RuntimeError("incorrect_install_host")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (directory/"consumers.json").exists():
        facts = manifest(directory)
        if all(dropin(u).is_file() and digest(dropin(u).read_bytes()) == x["installed_sha256"]
               for u,x in facts["units"].items()):
            return
        raise RuntimeError("existing_backup_use_rollback_or_new_directory")
    baseline=ledger.status()
    facts = {"release":str(ROOT),"units":{},"previous_capacity":baseline["capacity"],
             "rollback_legacy":baseline["legacy_reserved"]}
    payloads = {}
    for unit,module in UNITS.items():
        match = re.search(r"argv\[\]=([^;]+?)\s*;", prop(unit,"ExecStart"))
        args = shlex.split(match[1]) if match else []
        original = len(args)==3 and args[1:]==["-m",module]
        previous_adapter = (len(args)==3 and args[2]==module and
                    args[1].endswith("/ops/hk-sql-budget/launch.py") and dropin(unit).is_file())
        if not (original or previous_adapter) or not Path(args[0]).is_file():
            raise RuntimeError("unrecognized_current_entrypoint_"+unit)
        if original:
            facts["rollback_legacy"] = max(facts["rollback_legacy"],1)
        payload = ("[Service]\nExecStart=\nExecStart="+args[0]+" "+
                   str(ROOT/"ops/hk-sql-budget/launch.py")+" "+module+"\n").encode()
        path = dropin(unit)
        if path.is_file():
            atomic(directory/(unit+".previous"),path.read_bytes(),0o600)
        facts["units"][unit] = dict(previous=path.exists(),was_active=prop(unit,"ActiveState")=="active",
                                     installed_sha256=digest(payload))
        payloads[unit] = payload
    atomic(directory/"consumers.json",json.dumps(facts,sort_keys=True,indent=2).encode(),0o600)
    ledger.configure(capacity=30,legacy_reserved=1)
    try:
        for unit,data in payloads.items():
            atomic(dropin(unit),data)
        run("systemctl","daemon-reload")
        # metadata_reader drains accepted threads. An in-flight oneshot keeps
        # its original image; the next scheduled invocation loads the adapter.
        if facts["units"][METADATA]["was_active"]:
            run("systemctl","restart",METADATA,timeout=150)
            end=time.monotonic()+12
            while not using_adapter(METADATA):
                if time.monotonic()>=end:
                    raise RuntimeError("metadata_adapter_not_running")
                time.sleep(0.1)
    except BaseException:
        restore(directory,ledger)
        raise


def using_adapter(unit):
    pid = prop(unit,"MainPID")
    if not pid.isdecimal() or int(pid)==0:
        return prop(unit,"ActiveState") not in ("active","activating","deactivating")
    try:
        env=Path("/proc",pid,"environ").read_bytes().split(b"\0")
        return ("HK_SQL_BUDGET_RELEASE="+str(ROOT)).encode() in env
    except FileNotFoundError:
        return False


def legacy_count():
    values = {}
    for line in Path("/etc/ai-backend-offload/sql-client.env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key,val=line.split("=",1)
            if key in ("SQL_OFFLOAD_PORT","SQL_OFFLOAD_TOKEN"):
                values[key]=shlex.split(val)[0]
    conn=http.client.HTTPConnection("127.0.0.1",int(values["SQL_OFFLOAD_PORT"]),timeout=4)
    try:
        conn.request("POST","/v1/status",b"{}",{"Authorization":"Bearer "+values["SQL_OFFLOAD_TOKEN"],
                    "Content-Type":"application/json"})
        res=conn.getresponse()
        if res.status!=200:
            raise RuntimeError("legacy_broker_status_unavailable")
        return int(json.loads(res.read(32768))["remote_in_use"])
    finally:
        conn.close()


def first_packet_failure(entry):
    if entry["state"]!="unknown" or entry["bound"] or entry["task"]!="campaign-copy-report":
        return False
    start=int(entry["created"])
    journal=run("journalctl","-u",REFRESH,"--since","@"+str(start-30),"--until","@"+str(start+240),
                "--no-pager","-o","json")
    records = [json.loads(line) for line in journal.splitlines()]
    groups={}
    for row in records:
        key=(row.get("_SYSTEMD_INVOCATION_ID"),row.get("_PID"))
        if all(key):
            groups.setdefault(key,[]).append(str(row.get("MESSAGE","")).strip())
    for messages in groups.values():
        exact = []
        for line in messages:
            if line.startswith("{"):
                try:exact.append(json.loads(line))
                except ValueError:pass
        matching=[v for v in exact if v.get("event")=="offload_sql_close_unconfirmed"
                  and v.get("lease")==entry["lease_id"] and v.get("task")==entry["task"]]
        terminal=any(v.get("status")=="failed" and v.get("exit_code")==1 for v in exact)
        first_packet=("self._do_handshake()" in messages and "packet = bytes(self._socket.recv())" in messages
                      and any(line.startswith("mysql.connector.errors.ConnectionTimeoutError:") for line in messages))
        if len(matching)==1 and terminal and first_packet:
            return True
    return False


def finalize(directory, ledger, evidence_path):
    facts=manifest(directory)
    if str(ROOT)!=facts["release"] or not all(dropin(u).is_file() and
            digest(dropin(u).read_bytes())==v["installed_sha256"] and using_adapter(u)
            for u,v in facts["units"].items()):
        return "waiting_for_old_consumer"
    if any("127.0.0.1:19969" in s.split() for s in run("ss","-Hnt","state","established").splitlines()):
        return "waiting_for_old_tcp_session"
    count=legacy_count()
    if count:
        if not evidence_path:
            return "legacy_lease_evidence_required"
        evidence=json.loads(evidence_path.read_text())
        entries=evidence["entries"]
        if abs(time.time()-evidence["captured_at"])>300 or len(entries)!=count or not all(first_packet_failure(e) for e in entries):
            return "legacy_lease_unconfirmed"
        # The original CPU ledger stays intact. This only discounts operations
        # proven unable to authenticate or execute any SQL on behalf of HK.
        atomic(directory/"confirmed-preauth-failures.json",json.dumps(evidence).encode(),0o600)
    ledger.configure(legacy_reserved=0)
    return "hk_capacity_30"


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=("apply","rollback","finalize","status"))
    parser.add_argument("--backup",type=Path)
    parser.add_argument("--cpu-evidence",type=Path)
    args=parser.parse_args()
    if args.action!="status" and args.backup is None:
        parser.error("--backup is required")
    ledger=Ledger()
    result="ok"
    if args.action=="apply":
        apply(args.backup,ledger)
    elif args.action=="rollback":
        restore(args.backup,ledger)
    elif args.action=="finalize":
        result=finalize(args.backup,ledger,args.cpu_evidence)
    print(json.dumps({"result":result,"budget":ledger.status()},sort_keys=True))
    return 0 if result in ("ok","hk_capacity_30") else 2


if __name__=="__main__":
    sys.exit(main())
