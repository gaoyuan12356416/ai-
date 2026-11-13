#!/usr/bin/env python3
"""One 10-second scheduler loop; only the sidecar can inspect/create due slots."""
from __future__ import annotations

import http.client
import json
import os
import re
import signal
import threading
import time
import uuid


def call(action, worker_id):
    if action not in {"pool-start", "pool-tick"} or not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", worker_id):
        raise ValueError("invalid scheduler request")
    token = str(os.environ.get("FB_AUTO_POST_INTERNAL_TOKEN", ""))
    if not re.fullmatch(r"[A-Za-z0-9._~-]{32,512}", token):
        raise RuntimeError("internal credential unavailable")
    conn = http.client.HTTPConnection("127.0.0.1", 18835, timeout=50)
    try:
        conn.request("POST", "/internal/fb-video-publish/" + action,
                     body=json.dumps({"worker_id": worker_id}).encode(),
                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                              "Connection": "close"})
        response = conn.getresponse()
        raw = response.read(65537)
        if len(raw) > 65536 or response.status != 200:
            raise RuntimeError("scheduler service unavailable")
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("scheduler response invalid")
        return result
    finally:
        conn.close()


def run(stop, *, caller=call, clock=time.monotonic):
    worker_id = "fb-video-pool-" + uuid.uuid4().hex
    registered = False
    while not stop.is_set():
        started = clock()
        try:
            if not registered:
                caller("pool-start", worker_id)
                registered = True
            result = caller("pool-tick", worker_id)
            # A sidecar restart has lost its cutoff; register again before ticks.
            if result.get("status") in {"scheduler_not_started", "not_started", "worker_mismatch"}:
                registered = False
        except Exception:
            # Re-registering is idempotent while the same sidecar is alive. On
            # sidecar restart it creates a fresh cutoff instead of catching up.
            registered = False
            print("pool scheduler unavailable; next bounded check will retry", flush=True)
        stop.wait(max(0.1, 10 - (clock() - started)))


def main():
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    run(stop)


if __name__ == "__main__":
    main()
