"""Run the checked-in QA replay on the actual publisher through Tencent TAT.

Pass the same --output to resume an interrupted controller. A sibling .tat.json
keeps the cloud invocation IDs and input digest. No persistent TAT commands or
public resources are created. The target consumes only an isolated QA bearer.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import gzip
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from urllib import request
import uuid


ORIGIN = "http://10.89.0.125:8944"
PUBLISHER = "10.89.0.17"
VPC = "vpc-lg5p8p82"
INPUT_CHUNK = 28000
OUTPUT_CHUNK = 10000
DONE = {"SUCCESS", "FAILED", "TIMEOUT", "TASK_TIMEOUT", "START_FAILED",
        "DELIVER_FAILED", "TERMINATED", "CANCELLED", "CANCELED"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def b64(data):
    return base64.b64encode(data).decode("ascii")


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def save(path, data):
    with tempfile.NamedTemporaryFile(mode="wb", dir=str(path.parent), delete=False) as dest:
        try:
            dest.write(data)
            dest.flush()
            os.fsync(dest.fileno())
        finally:
            name = dest.name
    os.replace(name, str(path))


class Cloud:
    def __init__(self, path):
        self.keys = {key.lower().replace("_", ""): v for key,v in json.loads(path.read_text()).items()}

    def call(self, service, action, args):
        host = service+".tencentcloudapi.com"
        now = int(time.time())
        date = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        payload = json_bytes(args)
        headers = "content-type:application/json; charset=utf-8\nhost:"+host+"\n"
        canonical = "POST\n/\n\n"+headers+"\ncontent-type;host\n"+sha(payload)
        scope = date+"/"+service+"/tc3_request"
        string = "TC3-HMAC-SHA256\n%d\n%s\n%s" % (now,scope,sha(canonical.encode()))
        def mac(key, value):
            return hmac.new(key, value.encode(), hashlib.sha256).digest()
        key = mac(mac(mac(("TC3"+self.keys["secretkey"]).encode(),date),service),"tc3_request")
        signature = hmac.new(key,string.encode(),hashlib.sha256).hexdigest()
        authorization = ("TC3-HMAC-SHA256 Credential=%s/%s, SignedHeaders=content-type;host, Signature=%s"
                         % (self.keys["secretid"],scope,signature))
        headers = {"Content-Type":"application/json; charset=utf-8","Host":host,"Authorization":authorization,
                   "X-TC-Action":action,"X-TC-Timestamp":str(now),"X-TC-Region":"ap-hongkong",
                   "X-TC-Version":"2020-10-28" if service=="tat" else "2017-03-12"}
        if self.keys.get("token"):
            headers["X-TC-Token"] = self.keys["token"]
        with request.urlopen(request.Request("https://"+host,data=payload,headers=headers),timeout=20) as response:
            result = json.load(response)["Response"]
        if "Error" in result:
            raise RuntimeError("%s: %s (%s)" % (action,result["Error"]["Code"],result.get("RequestId","")))
        return result

    def publisher(self, expected):
        filters = [{"Name":"vpc-id","Values":[VPC]},{"Name":"private-ip-address","Values":[PUBLISHER]}]
        rows = self.call("cvm","DescribeInstances",{"Filters":filters,"Limit":5})["InstanceSet"]
        ids = [r["InstanceId"] for r in rows if PUBLISHER in r.get("PrivateIpAddresses",[])
               and r["VirtualPrivateCloud"]["VpcId"] == VPC and r["InstanceState"] == "RUNNING"
               and r.get("InstanceName","").startswith("ads发布调控")]
        if len(ids)!=1 or (expected and ids[0]!=expected):
            raise ValueError("the expected running publisher is not the sole owner of "+PUBLISHER)
        return ids[0]


class TAT:
    def __init__(self, cloud, journal, path):
        self.cloud, self.journal, self.path = cloud, journal, path
        self.lock = threading.Lock()
        self.directory = "/var/tmp/fb-page-v2-qa-"+journal["case"]

    def command(self, name, script, timeout=60, setup=False):
        pre = "#!/bin/bash\nset -eu\numask 077\nd="+self.directory+"\n"
        if setup:
            pre += 'mkdir -m 700 "$d" 2>/dev/null || test -d "$d"\n'
        pre += ('test ! -L "$d" && test "$(stat -c %u:%a "$d")" = 0:700 || exit 72\n'
                'cd -- "$d"\n')
        content = b64((pre+script+"\n").encode())
        if len(content) >= 65536:
            raise ValueError("TAT encoded command too large")
        with self.lock:
            invocation = self.journal["calls"].get(name)
            if not invocation:
                answer = self.cloud.call("tat","RunCommand",{
                    "Content":content,"CommandName":"PageV2QA-"+name[:40],"CommandType":"SHELL",
                    "SaveCommand":False,"InstanceIds":[self.journal["instance_id"]],"Timeout":timeout})
                invocation = answer["InvocationId"]
                self.journal["calls"][name] = invocation
                save(self.path,json_bytes(self.journal))
        end = time.monotonic()+timeout+110
        interval = 1.0
        while time.monotonic() < end:
            filters = [{"Name":"invocation-id","Values":[invocation]}]
            try:
                rows = self.cloud.call("tat","DescribeInvocationTasks",{"Filters":filters,"HideOutput":False})["InvocationTaskSet"]
            except (OSError, RuntimeError):
                time.sleep(min(6.,interval)); interval = min(6.,interval+1); continue
            if len(rows)>1 or (rows and rows[0]["InstanceId"]!=self.journal["instance_id"]):
                raise ValueError("unexpected TAT result")
            if rows and rows[0]["TaskStatus"] in DONE:
                row = rows[0]
                result = row.get("TaskResult") or {}
                if row["TaskStatus"] != "SUCCESS" or result.get("ExitCode",1) != 0:
                    raw = base64.b64decode(result.get("Output","")).decode(errors="replace")
                    # Remote commands explicitly print only these public diagnostics.
                    codes = re.findall(r"QA_ERROR_[A-Z_0-9]+",raw)
                    raise RuntimeError("%s %s %s exit=%s %s" % (name,invocation,row["TaskStatus"],
                                       result.get("ExitCode"),",".join(codes)))
                if result.get("Dropped",0):
                    raise ValueError(name+" output was truncated")
                return base64.b64decode(result.get("Output","")).strip()
            time.sleep(interval)
            interval = min(5., interval+1) if name=="run" else 1.
        raise TimeoutError(name+" still awaiting "+invocation+"; resume using the same output")


def encrypted_token(pem, token):
    # CPU hosts have openssl. Plaintext is only written to subprocess stdin.
    with tempfile.TemporaryDirectory() as tmp:
        pub = Path(tmp)/"public.pem"
        pub.write_bytes(pem)
        args = ["openssl","pkeyutl","-encrypt","-pubin","-inkey",str(pub),
                "-pkeyopt","rsa_padding_mode:oaep","-pkeyopt","rsa_oaep_md:sha256",
                "-pkeyopt","rsa_mgf1_md:sha256"]
        try:
            return subprocess.check_output(args,input=token,stderr=subprocess.PIPE)
        except FileNotFoundError:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            key = serialization.load_pem_public_key(pem)
            return key.encrypt(token,padding.OAEP(padding.MGF1(hashes.SHA256()),hashes.SHA256(),None))


SETUP = r'''
ip -4 route get 10.89.0.125 | grep -Eq 'src 10\.89\.0\.17( |$)' || { echo QA_ERROR_ROUTE_SOURCE; exit 75; }
exec 9>setup.lock
flock 9
for path in python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3.7 python3 \
    /usr/local/bin/python3.* /opt/rh/rh-python*/root/usr/bin/python3 /usr/bin/python3.*; do
    candidate=$(command -v "$path" || :)
    if test -n "$candidate" && "$candidate" -c 'import sys; assert sys.version_info >= (3,7)' 2>/dev/null; then
        echo "$candidate" > interpreter
        break
    fi
done
test -s interpreter || { echo QA_ERROR_PYTHON_37_REQUIRED; exit 76; }
if ! test -s private.pem; then
    openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out private.next 2>/dev/null
    mv -f private.next private.pem
fi
openssl pkey -in private.pem -pubout
'''


def chunk_script(part, number):
    return ("name=part-%05d\n" % number
            + "printf '%%s' '%s' | base64 -d > \"$name.$$\"\n" % b64(part)
            + 'test "$(sha256sum "$name.$$" | cut -d" " -f1)" = '+sha(part)+'\n'
            + 'mv -f "$name.$$" "$name"')


def assemble(parts, digest, encrypted):
    return ('exec 9>assemble.lock\nflock 9\npython=$(cat interpreter)\n'
            '"$python" - <<\'PY\'\n'
            'import base64,gzip,hashlib,json,os\nfrom pathlib import Path\n'
            'raw=b"".join(Path("part-%05d" % n).read_bytes() for n in range('+str(parts)+'))\n'
            'assert hashlib.sha256(raw).hexdigest()=="'+digest+'"\n'
            'files=json.loads(gzip.decompress(raw))\n'
            'assert sorted(files)==["inputs.json","qa_replay.py","qa_trace.json"]\n'
            'for name,value in files.items():\n'
            ' p=Path(name+".next"); p.write_bytes(base64.b64decode(value,validate=True)); os.replace(str(p),name)\n'
            "PY\n"
            +"printf '%s' '"+b64(encrypted)+"' | base64 -d | openssl pkeyutl -decrypt -inkey private.pem "
            +"-pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256 -pkeyopt rsa_mgf1_md:sha256 "
             "> qa-bearer.next\ntest -s qa-bearer.next\nmv -f qa-bearer.next qa-bearer")


def run_script(args):
    argv = ["qa_replay.py","--origin",args.origin,"--token-file","qa-bearer","--inputs","inputs.json",
            "--profile",args.profile,"--speed",str(args.speed),"--drain-timeout",str(args.drain_timeout),
            "--output","report.json"]
    if args.allow_representative:
        argv.append("--allow-representative")
    if args.expect_mysql_table:
        argv.extend(["--expect-mysql-table", args.expect_mysql_table])
    return ('exec 9>run.lock\nflock 9\nif test -f replay.exit; then exit 0; fi\n'
            'if test -e replay.started; then echo QA_ERROR_PRIOR_REPLAY_INCOMPLETE; exit 77; fi\n'
            'date -u +%FT%TZ > replay.started\nset +e\n'
            '"$(cat interpreter)" -B '+ " ".join(map(shlex.quote,argv)) +' >replay.log 2>&1\n'
            'rc=$?\nset -e\nrm -f -- qa-bearer private.pem\n'
            'if ! test -s report.json; then echo QA_ERROR_NO_REPORT; '
            'tail -n 2 replay.log | cut -c1-220 > failure.txt; exit 78; fi\n'
            'echo "$rc" > replay.exit.next\nmv replay.exit.next replay.exit')


PACK = r'''
exec 9>pack.lock
flock 9
test -f replay.exit
"$(cat interpreter)" - <<'PY'
import gzip,hashlib,json,os
from pathlib import Path
p=Path('report.json')
data=p.read_bytes()
json.loads(data)
if not Path('report.gz').is_file():
    Path('report.gz.next').write_bytes(gzip.compress(data,6))
    os.replace('report.gz.next','report.gz')
raw=Path('report.gz').read_bytes()
print(json.dumps(dict(n=len(raw),sha256=hashlib.sha256(raw).hexdigest(),
      report_sha256=hashlib.sha256(data).hexdigest(),exit=int(Path('replay.exit').read_text()))))
PY
'''


def take(tat, number):
    script = ('"$(cat interpreter)" -c \'import sys,base64; f=open("report.gz","rb"); '
              'f.seek('+str(number*OUTPUT_CHUNK)+'); sys.stdout.buffer.write(base64.b64encode(f.read('
              +str(OUTPUT_CHUNK)+')))\'')
    return base64.b64decode(tat.command("read-%05d"%number,script),validate=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin",default=ORIGIN,choices=[ORIGIN])
    parser.add_argument("--source",type=Path,required=True,help="directory with qa_replay.py and qa_trace.json, or repo root")
    parser.add_argument("--inputs",type=Path,help="published fixture JSON, default <source>/<profile>.json")
    parser.add_argument("--token-file",type=Path,required=True,help="file containing only the HK sandbox bearer")
    parser.add_argument("--profile",choices=["burst","hour"],required=True)
    parser.add_argument("--speed",type=float,default=1)
    parser.add_argument("--drain-timeout",type=float,default=300)
    parser.add_argument("--allow-representative",action="store_true")
    parser.add_argument("--expect-mysql-table")
    parser.add_argument("--credentials",type=Path,default=Path("/root/.config/tencentcloud-billing/credentials.json"))
    parser.add_argument("--instance",help="optionally require a specific cloud instance ID")
    parser.add_argument("--output",type=Path,required=True,help="same path resumes the same run")
    args = parser.parse_args()
    if not all(math.isfinite(n) and n>0 for n in (args.speed,args.drain_timeout)):
        parser.error("speed and drain timeout must be positive")
    source = args.source
    if not (source/"qa_replay.py").is_file():
        source = source/"ops"/"fb-page-v2"
    fixture = args.inputs or args.source/(args.profile+".json")
    files = {n:b64((source/n).read_bytes()) for n in ("qa_replay.py","qa_trace.json")}
    files["inputs.json"] = b64(fixture.read_bytes())
    packed = gzip.compress(json_bytes(files),6,mtime=0)
    digest = sha(packed)
    jpath = args.output.with_name(args.output.name+".tat.json")
    args.output.parent.mkdir(parents=True,exist_ok=True)
    cloud = Cloud(args.credentials)
    instance = cloud.publisher(args.instance)
    binding = dict(instance_id=instance,caller_ip=PUBLISHER,origin=args.origin,profile=args.profile,
                   speed=args.speed,drain_timeout=args.drain_timeout,representative=args.allow_representative,
                   input_sha256=digest,mysql_qa_table=args.expect_mysql_table)
    if jpath.exists():
        journal = json.loads(jpath.read_bytes())
        if any(journal.get(k)!=v for k,v in binding.items()):
            raise ValueError("run parameters or input digests changed; choose a new output")
    else:
        if args.output.exists():
            raise FileExistsError("output exists without its matching TAT journal")
        journal = dict(binding,case=uuid.uuid4().hex,calls={})
        with jpath.open("xb") as f:
            f.write(json_bytes(journal))
    if not re.fullmatch("[a-f0-9]{32}",journal["case"]):
        raise ValueError("invalid run journal ID")
    if journal.get("verified_sha256") and args.output.exists():
        report = args.output.read_bytes()
        if sha(report)!=journal["verified_sha256"]:
            raise ValueError("saved report differs from verified TAT result")
        return 0 if json.loads(report).get("ok") else 2
    tat = TAT(cloud,journal,jpath)
    print("case",journal["case"],"caller",PUBLISHER,instance,"profile",args.profile,flush=True)
    pub = tat.command("setup",SETUP,setup=True)
    if not pub.startswith(b"-----BEGIN PUBLIC KEY-----"):
        raise ValueError("publisher did not return the temporary public key")
    token = args.token_file.read_bytes().strip()
    if not re.fullmatch(br"[A-Za-z0-9_-]{32,128}",token):
        raise ValueError("expected an isolated QA bearer file")
    encrypted = encrypted_token(pub,token)
    parts = [packed[p:p+INPUT_CHUNK] for p in range(0,len(packed),INPUT_CHUNK)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda pair:tat.command("part-%05d"%pair[0],chunk_script(pair[1],pair[0])),
                      enumerate(parts)))
    tat.command("assemble",assemble(len(parts),digest,encrypted))
    duration = 10 if args.profile=="burst" else 3600
    timeout = min(86400,math.ceil(duration/args.speed + args.drain_timeout + 140))
    print("caller staged",len(parts),"parts; TAT replay timeout",timeout,flush=True)
    tat.command("run",run_script(args),timeout)
    manifest = json.loads(tat.command("pack",PACK))
    size = int(manifest["n"])
    if not 0<size<=128*1024*1024:
        raise ValueError("invalid compressed report size")
    with ThreadPoolExecutor(max_workers=3) as pool:
        data = b"".join(pool.map(lambda n:take(tat,n),range(math.ceil(size/OUTPUT_CHUNK))))
    report = gzip.decompress(data)
    if len(data)!=size or sha(data)!=manifest["sha256"] or sha(report)!=manifest["report_sha256"]:
        raise ValueError("downloaded report failed size or SHA-256 verification")
    result = json.loads(report)
    if result.get("profile")!=args.profile or (not result.get("aborted") and result.get("origin")!="10.89.0.125"):
        raise ValueError("unexpected QA result binding")
    # Retain the unmodified publisher-generated bytes for comparison with the isolated ledger.
    save(args.output,report)
    journal["verified_sha256"] = sha(report)
    journal["replay_exit"] = manifest["exit"]
    save(jpath,json_bytes(journal))
    print(json.dumps({k:v for k,v in result.items() if k not in ("samples","trace")},ensure_ascii=False))
    print("verified",journal["verified_sha256"],args.output,flush=True)
    return 0 if result.get("ok") and manifest["exit"]==0 else 2


if __name__ == "__main__":
    sys.exit(main())
