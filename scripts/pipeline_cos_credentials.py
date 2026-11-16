"""Issue a renewable, prefix-scoped session for the COS prepare-only process."""
import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import tempfile
import time
from pathlib import Path

import requests


def sts(action, body, region, identity, secret):
    host = "sts.tencentcloudapi.com"
    data = json.dumps(body, separators=(",", ":")).encode()
    now = int(time.time())
    day = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%d")
    scope = day + "/sts/tc3_request"
    sha = lambda value: hashlib.sha256(value).hexdigest()
    sign = lambda key, value: hmac.new(key, value.encode(), hashlib.sha256).digest()
    canonical = "POST\n/\n\ncontent-type:application/json\nhost:" + host + "\n\ncontent-type;host\n" + sha(data)
    message = "TC3-HMAC-SHA256\n" + str(now) + "\n" + scope + "\n" + sha(canonical.encode())
    key = sign(sign(sign(("TC3" + secret).encode(), day), "sts"), "tc3_request")
    auth = ("TC3-HMAC-SHA256 Credential=" + identity + "/" + scope
            + ", SignedHeaders=content-type;host, Signature=" + sign(key, message).hex())
    response = requests.post("https://" + host, data=data, timeout=15, headers={
        "Content-Type": "application/json", "X-TC-Version": "2018-08-13", "X-TC-Region": region,
        "X-TC-Action": action, "X-TC-Timestamp": str(now), "Authorization": auth}).json()["Response"]
    if "Error" in response:
        raise RuntimeError("Tencent STS " + str(response["Error"].get("Code", "failed")))
    return response


def issue(config, issuer, destination):
    keys = {}
    for line in Path(issuer).read_text().splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name in {"COS_SECRET_KEY", "COS_SECRET_ID"}:
            keys[name] = value.strip().strip("'\x22")
    region, bucket = config["region"], config["bucket"]
    app = bucket.rsplit("-", 1)[1]
    def resource(service, target, path):
        return "qcs::" + service + ":" + region + ":uid/" + app + ":" + ("bucket/" if service == "ci" else "") + target + "/" + path
    credentials = (region, keys["COS_SECRET_ID"], keys["COS_SECRET_KEY"])
    user = sts("GetCallerIdentity", {}, *credentials)
    owner = str(user.get("AccountId") or user["PrincipalId"]).split(":")[0]
    if not owner.isdigit():
        raise RuntimeError("Tencent STS owner identity unavailable")
    object_resources = [resource("cos", target, prefix.strip("/") + "/*") for target, prefix in (
        (bucket, config["prefix"]), (config["delivery_bucket"], config["delivery_prefix"]))]
    policy = {"version": "2.0", "statement": [
        {"effect": "allow", "action": ["ci:CreateMediaJobs", "ci:DescribeMediaJob", "ci:DescribeMediaJobs",
            "ci:DescribeMediaQueues"], "resource": [resource("ci", bucket, "*")]},
        {"effect": "allow", "action": ["cos:GetObject", "cos:HeadObject", "cos:PutObject",
            "cos:InitiateMultipartUpload", "cos:UploadPart", "cos:ListParts", "cos:CompleteMultipartUpload",
            "cos:AbortMultipartUpload"], "resource": object_resources},
        {"effect": "allow", "action": ["cam:PassRole"], "resource": ["qcs::cam::uin/" + owner + ":roleName/CI_QCSRole"]}]}
    response = sts("GetFederationToken", {"Name": "pipeline-cos-prepare", "DurationSeconds": 7200,
                   "Policy": json.dumps(policy, separators=(",", ":"))}, *credentials)
    destination = Path(destination)
    fd, tmp = tempfile.mkstemp(dir=destination.parent, prefix=".ci-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"credentials": response["Credentials"], "expires": response["ExpiredTime"]}, out)
            out.flush()
            os.fsync(out.fileno())
        if "credential_gid" in config:
            os.chown(tmp, 0, int(config["credential_gid"]))
            os.chmod(tmp, 0o640)
        os.replace(tmp, destination)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


if __name__ == "__main__":
    options = argparse.ArgumentParser()
    options.add_argument("--config", required=True)
    options.add_argument("--issuer", required=True)
    args = options.parse_args()
    conf = json.loads(Path(args.config).read_text())
    issue(conf, args.issuer, conf["credential_file"])
