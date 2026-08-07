#!/usr/bin/env python3
"""Warm the default aggregate responses after a successful cache refresh."""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from common import API_SCHEMA_VERSION


PREWARM_DIMENSION_SETS = (
    ("dt", "campaign"),
    ("campaign",),
    ("adset",),
    ("optimizer",),
    ("country_group",),
    ("channel",),
    ("delivery_product",),
)


def request_plan(meta: dict[str, Any]) -> list[tuple[str, dict[str, str]]]:
    defaults = meta.get("defaults") or {}
    cache = meta.get("cache") or {}
    version = str(meta.get("data_version") or "")
    default_start = str(defaults.get("start_date") or "")
    default_end = str(defaults.get("end_date") or "")
    full_start = str(cache.get("start_date") or default_start)
    full_end = str(cache.get("end_date") or default_end)
    default_dimensions = tuple(str(value) for value in (defaults.get("dimensions") or ["dt", "campaign"]))
    if not version or not default_start or not default_end:
        raise RuntimeError("meta is missing data_version/default date range")
    ranges = [(default_start, default_end)]
    if (full_start, full_end) not in ranges:
        ranges.append((full_start, full_end))
    dimension_sets = [default_dimensions]
    dimension_sets.extend(value for value in PREWARM_DIMENSION_SETS if value not in dimension_sets)
    plan: list[tuple[str, dict[str, str]]] = []
    for start, end in ranges:
        common = {
            "api_schema_version": str(API_SCHEMA_VERSION),
            "start_date": start,
            "end_date": end,
            "data_version": version,
        }
        plan.append(("/api/options", common))
        for basis in ("d0", "d7"):
            for dimensions in dimension_sets:
                plan.append(
                    (
                        "/api/query",
                        {
                            **common,
                            "dimensions": ",".join(dimensions),
                            "metric_basis": basis,
                            "sort_by": "spend",
                            "sort_dir": "desc",
                            "limit": "50",
                            "offset": "0",
                            "include_rankings": "0",
                        },
                    )
                )
            plan.append(("/api/rankings", {**common, "metric_basis": basis}))
    return plan


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
                path_counts: dict[str, int] = {}
                for path, params in request_plan(meta):
                    call_started = time.monotonic()
                    fetch_json(args.base_url, path, params, args.timeout)
                    path_counts[path] = path_counts.get(path, 0) + 1
                    suffix = "" if path_counts[path] == 1 else f"#{path_counts[path]}"
                    timings[path + suffix] = round(time.monotonic() - call_started, 3)
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
