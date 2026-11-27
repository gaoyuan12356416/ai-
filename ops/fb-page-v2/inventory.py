"""Publish the private addresses of running publisher CVMs for the V2 listener."""
import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.request

REGION = "ap-hongkong"
VPC = "vpc-lg5p8p82"
SOURCE_GROUP = "sg-lqtd1q1l"
PUBLISHER_GROUPS = {SOURCE_GROUP, "sg-pgzi0n4v"}
# The operator also authorizes this production web caller. Pin its cloud
# identity and address instead of admitting the web security group's peers.
EXPLICIT_CALLERS = {"ins-q12mp898": {"10.89.0.9"}}
NON_PROD = re.compile(r"(?:测试|临时|预发|沙箱|(?:^|[\s_-])(?:test|testing|staging|temp|sandbox)(?:[\s_-]|\d|$))",re.I)
PRIVATE_V4 = tuple(ipaddress.ip_network(v) for v in ("10.0.0.0/8","172.16.0.0/12","192.168.0.0/16"))


def cloud_request(secret_path, service, version, action, parameters):
    credentials = {key.lower().replace("_", ""): value
                   for key, value in json.loads(secret_path.read_text()).items()}
    host = service + ".tencentcloudapi.com"
    now = int(time.time())
    date = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    payload = json.dumps(parameters, separators=(",", ":")).encode()
    headers = "content-type:application/json; charset=utf-8\nhost:" + host + "\n"
    sha = lambda data: hashlib.sha256(data).hexdigest()
    canonical = "POST\n/\n\n" + headers + "\ncontent-type;host\n" + sha(payload)
    scope = date + "/" + service + "/tc3_request"
    to_sign = f"TC3-HMAC-SHA256\n{now}\n{scope}\n{sha(canonical.encode())}"
    def sign(key, value):
        return hmac.new(key, value.encode(), hashlib.sha256).digest()
    key = sign(sign(sign(("TC3" + credentials["secretkey"]).encode(), date), service), "tc3_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    auth = (f"TC3-HMAC-SHA256 Credential={credentials['secretid']}/{scope}, "
            f"SignedHeaders=content-type;host, Signature={signature}")
    headers = {"Content-Type":"application/json; charset=utf-8","Host":host,"Authorization":auth,
               "X-TC-Action":action,"X-TC-Timestamp":str(now),
               "X-TC-Version":version,"X-TC-Region":REGION}
    if credentials.get("token"):
        headers["X-TC-Token"] = credentials["token"]
    request = urllib.request.Request("https://" + host, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        value = json.load(response)["Response"]
    if "Error" in value:
        raise RuntimeError("inventory_" + value["Error"]["Code"])
    return value


def describe(secret_path, offset):
    return cloud_request(secret_path, "cvm", "2017-03-12", "DescribeInstances",
                         {"Filters":[{"Name":"vpc-id","Values":[VPC]}],"Offset":offset,"Limit":100})


def inventory(describer):
    instances = {}
    offset = 0
    while True:
        page = describer(offset)
        for row in page["InstanceSet"]:
            net = row.get("VirtualPrivateCloud", {})
            name = row.get("InstanceName","")
            explicit = EXPLICIT_CALLERS.get(row["InstanceId"], set())
            publisher = (PUBLISHER_GROUPS.intersection(row.get("SecurityGroupIds", []))
                         and name.startswith("ads发布调控") and not NON_PROD.search(name))
            if (net.get("VpcId") != VPC or row.get("InstanceState") != "RUNNING"
                    or not (publisher or explicit)):
                continue
            addresses = []
            for value in row["PrivateIpAddresses"]:
                ip = ipaddress.ip_address(value)
                if (ip.version == 4 and any(ip in network for network in PRIVATE_V4)
                        and (publisher or str(ip) in explicit)):
                    addresses.append(str(ip))
            if addresses:
                instances[row["InstanceId"]] = {"name":name,"addresses":addresses}
        offset += len(page["InstanceSet"])
        if offset >= page["TotalCount"]:
            break
        if not page["InstanceSet"]:
            raise RuntimeError("incomplete_cloud_inventory")
    return {"addresses":sorted({ip for row in instances.values() for ip in row["addresses"]}),
            "instances":instances, "generated_at":datetime.now(timezone.utc).isoformat()}


def write_inventory(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w",dir=path.parent,encoding="utf-8",delete=False) as fh:
        tmp = Path(fh.name)
        try:
            json.dump(value, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        except BaseException:
            fh.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
        if os.name == "posix":
            fd = os.open(str(path.parent),os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--credentials", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--network-state", type=Path)
    a = p.parse_args()
    result = inventory(lambda offset: describe(a.credentials, offset))
    write_inventory(a.out, result)
    print("publisher_addresses", len(result["addresses"]))
    if a.network_state:
        from network import manager, locked
        with locked(a.network_state):
            net = manager(a.credentials,a.network_state)
            print(json.dumps(net.sync(result["addresses"])))
