#!/usr/bin/env python3
"""Bounded loopback readiness check for the X Post sidecar systemd unit."""

from __future__ import annotations

import argparse
import time
import urllib.error
import urllib.parse
import urllib.request


_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def wait_until_ready(url, timeout):
    parsed = urllib.parse.urlsplit(str(url or ""))
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.path != "/health"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("health URL must be the loopback /health endpoint")
    deadline = time.monotonic() + max(1, min(int(timeout), 60))
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(url, headers={"Accept": "text/plain"})
            with _DIRECT_OPENER.open(request, timeout=2) as response:
                if response.status == 200 and response.read(16) == b"ok\n":
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.25)
    raise RuntimeError("X Post sidecar did not become ready before timeout")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    wait_until_ready(args.url, args.timeout)


if __name__ == "__main__":
    main()
