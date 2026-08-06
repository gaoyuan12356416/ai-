#!/usr/bin/env python3
"""Warm the default aggregate responses after a successful cache refresh."""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def request_plan(meta: dict[str, Any]) -> list[tuple[str, dict[str, str]]]:
    defaults = meta.get("defaults") or {}
    version = str(meta.get("data_version") or "")
    start = str(defaults.get("start_date") or "")
    end = str(defaults.get("end_date") or "")
    basis = str(defaults.get("basis") or "d0")
    dimensions = defaults.get("dimensions") or ["dt", "campaign"]
    if not version or not start or not end:
        raise RuntimeError("meta is missing data_version/default date range")
    common = {"start_date": start, "end_date": end, "data_version": version}
    query = {
        **common,
        "dimensions": ",".join(str(value) for value in dimensions),
        "metric_basis": basis,
        "sort_by": "spend",
        "sort_dir": "desc",
        "limit": "50",
        "offset": "0",
        "include_rankings": "0",
    }
    ranking = {**common, "metric_basis": basis}
    return [("/api/options", common), ("/api/query", query), ("/api/rankings", ranking)]


def fetch_json(
    base_url: str,
    path: str,
    params: Optional[dict[str, str]],
    timeout: float,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + path
    if params:
        url += "?" + urlencode(params)
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "dramawave-cache-warmer/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8832")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    args = parser.parse_args()
    started = time.monotonic()
    attempts = max(1, args.attempts)
    try:
        for attempt in range(1, attempts + 1):
            try:
                meta = fetch_json(args.base_url, "/api/meta", None, args.timeout)
                timings: dict[str, float] = {}
                for path, params in request_plan(meta):
                    call_started = time.monotonic()
                    fetch_json(args.base_url, path, params, args.timeout)
                    timings[path] = round(time.monotonic() - call_started, 3)
                break
            except Exception:
                if attempt >= attempts:
                    raise
                time.sleep(max(0.0, args.retry_delay))
        print(
            json.dumps(
                {
                    "ok": True,
                    "attempt": attempt,
                    "data_version": meta.get("data_version"),
                    "seconds": round(time.monotonic() - started, 3),
                    "timings": timings,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}", "seconds": round(time.monotonic() - started, 3)},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
