"""Extract only arrival second, task type, product, pool and batch size from the capacity study."""
import argparse
import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import zlib


WINDOWS = {
    "burst": ("2026-11-21 15:40:56", 10, 720, 1600),
    "hour": ("2026-11-23 12:39:32", 3600, 6165, 11269),
}
PRODUCTS = {"Dramawave": 1479, "FreeReels": 979, "HotDrama": 3302}


def extract(demand):
    events = json.loads(zlib.decompress(base64.b64decode(demand["events_z"])))
    profiles = {}
    for name, (start, seconds, count, items) in WINDOWS.items():
        begin = int((datetime.fromisoformat(start) - datetime(2026, 11, 19)).total_seconds())
        arrivals = []
        for timestamp, product, cross, size, unresolved, pool in events:
            if 0 <= timestamp - begin < seconds:
                if unresolved or size <= 0:
                    raise ValueError("unresolved workload row")
                for offset in range(0, size, 100):
                    arrivals.append([timestamp-begin, min(100, size-offset),
                                     PRODUCTS[demand["products"][product]], pool, int(bool(cross))])
        arrivals.sort()
        if (len(arrivals), sum(e[1] for e in arrivals)) != (count, items):
            raise ValueError("capacity study differs from the selected period")
        data = json.dumps(arrivals, separators=(",", ":")).encode()
        profiles[name] = dict(start_beijing=start, seconds=seconds, requests=count, items=items,
                              sha256=hashlib.sha256(data).hexdigest(),
                              rows_z=base64.b64encode(zlib.compress(data, 9)).decode())
    return dict(format=1, columns=["arrival_second", "size", "product", "pool_or_zero", "is_cross"],
                profiles=profiles)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("demand", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    args.destination.write_text(json.dumps(extract(json.loads(args.demand.read_text(encoding="utf8"))),
                                           indent=2)+"\n", encoding="utf8")
