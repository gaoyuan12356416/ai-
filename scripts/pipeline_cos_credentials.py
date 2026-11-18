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
    stores = [config]
    if config.get("legacy_work_ids"):
        stores.append(dict(config, **config["legacy_storage"]))
    object_resources = sorted({resource("cos", target, prefix.strip("/") + "/*")
        for store in stores for target, prefix in (
            (store["bucket"], store["prefix"]), (store["delivery_bucket"], store["delivery_prefix"]))})
    ci_resources = sorted({resource("ci", store["bucket"], "*") for store in stores})
    policy = {"version": "2.0", "statement": [
        {"effect": "allow", "action": ["ci:CreateMediaJobs", "ci:DescribeMediaJob", "ci:DescribeMediaJobs",
            "ci:DescribeMediaQueues"], "resource": ci_resources},
        {"effect": "allow", "action": ["cos:GetObject", "cos:HeadObject", "cos:PutObject",
            "cos:InitiateMultipartUpload", "cos:UploadPart", "cos:ListParts", "cos:CompleteMultipartUpload",
            "cos:AbortMultipartUpload"], "resource": object_resources},
        {"effect": "allow", "action": ["cam:PassRole"], "resource": ["qcs::cam::uin/" + owner + ":roleName/CI_QCSRole"]}]}
    if config.get("fb_cloud_input"):
        policy["statement"].append({"effect": "allow", "action": ["cos:GetObject", "cos:HeadObject"],
            "resource": [resource("cos", bucket, "custom/source/*")]})
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
