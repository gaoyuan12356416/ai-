#!/usr/bin/env python3
"""Small bounded loopback client; no platform credentials or direct Meta calls."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import time

TIMEOUTS = {"prepare-next": 9600, "link-next": 300, "execute-next": 1300, "reconcile-next": 1300, "tick": 60}


def call(action, *, worker_id="fb-video-primary"):
    if action not in TIMEOUTS or not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", worker_id):
        raise ValueError("invalid worker request")
    token = str(os.environ.get("FB_AUTO_POST_INTERNAL_TOKEN", ""))
    if not re.fullmatch(r"[A-Za-z0-9._~-]{32,512}", token):
        raise RuntimeError("internal credential unavailable")
    conn = http.client.HTTPConnection("127.0.0.1", 18835, timeout=TIMEOUTS[action])
    try:
        conn.request("POST", "/internal/fb-video-publish/" + action, body=json.dumps({"worker_id": worker_id}).encode(),
                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Connection": "close"})
        response = conn.getresponse()
        body = response.read(1048577)
        if len(body) > 1048576 or response.status != 200:
            raise RuntimeError("manual worker service unavailable")
        value = json.loads(body)
        if not isinstance(value, dict) or not value.get("ok"):
            raise RuntimeError("manual worker response invalid")
        return value
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("work", "prepare", "once"))
    args = parser.parse_args()
    started = time.monotonic()
    while True:
        try:
            if args.mode == "prepare":
                result = call("prepare-next", worker_id="fb-video-prepare")
                if result.get("status") in {"no_pending", "live_gate_closed"} or time.monotonic() - started > 1800:
                    return 0
            else:
                call("tick")
                for action, maximum in (("reconcile-next", 10), ("link-next", 10), ("execute-next", 4)):
                    for _ in range(maximum):
                        result = call(action)
                        if result.get("status") in {"no_pending", "live_gate_closed"}:
                            break
                if args.mode == "once":
                    return 0
                time.sleep(10)
        except Exception:
            # Do not print request bodies, env, tokens or platform error payloads.
            print("manual worker unavailable; retrying safely", flush=True)
            if args.mode in {"once", "prepare"}:
                return 1
            time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
