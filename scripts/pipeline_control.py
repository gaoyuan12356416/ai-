#!/usr/bin/env python3
"""Local authority operations; does not stop, kill or restart publishing units."""
import argparse
import http.client
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("stop-claims", "cutover-plan", "cutover-apply", "cutover-enable"))
    parser.add_argument("--channel", choices=("x", "fb", "tt", "youtube"))
    parser.add_argument("--plan", help="Previously frozen plan response JSON")
    parser.add_argument("--cutoff")
    parser.add_argument("--grandfathered", help="JSON array of exact in-flight source keys")
    parser.add_argument("--output", help="Save nonsecret operation result as JSON")
    args = parser.parse_args()
    token = os.environ.get("PIPELINE_DISPATCH_INTERNAL_TOKEN", "")
    if len(token) < 32:
        parser.error("coordinator authority credential is not loaded")
    body = {"channel": args.channel}
    if args.cutoff:
        body["cutoff"] = args.cutoff
    if args.grandfathered:
        body["grandfathered_source_keys"] = json.loads(Path(args.grandfathered).read_text())
    if args.plan:
        plan = json.loads(Path(args.plan).read_text())
        body.update(plan_id=plan["plan_id"], sha256=plan["sha256"])
    connection = http.client.HTTPConnection("127.0.0.1", 18846, timeout=600)
    try:
        connection.request("POST", "/internal/pipeline/admin/" + args.action, json.dumps(body).encode(), {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read(8 * 1024 * 1024))
        if response.status != 200:
            raise SystemExit(data.get("code", "control_operation_failed"))
        if args.output:
            path = Path(args.output)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            path.chmod(0o600)
            print(json.dumps({"saved": str(path), "status": data.get("status", "accepted"), "errors": len(data.get("errors", []))}))
        else:
            print(json.dumps(data, ensure_ascii=False))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
