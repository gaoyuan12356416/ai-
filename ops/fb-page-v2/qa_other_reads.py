"""Model four other HK readers during a bounded load test; perform actual metadata reads."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from features.hk_sql_budget import budget_connection


def exercise(seconds):
    until = time.monotonic() + seconds
    def other_reader(index):
        count = 0
        while time.monotonic() < until:
            with budget_connection("read", time.monotonic()+8) as conn, conn.cursor() as cur:
                cur.execute("SELECT id FROM kunlunads_dev.ads_template_make_queue WHERE id=%s",
                            (12367129 if index % 2 == 0 else 12367130,))
                if cur.fetchone() is None:
                    raise RuntimeError("representative_queue_missing")
                count += 1
                time.sleep(min(1., max(0.,until-time.monotonic())))
        return count
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts=list(pool.map(other_reader,range(4)))
    print(json.dumps({"background_readers":4,"queries":counts,"seconds":seconds}))


if __name__ == "__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--seconds",required=True,type=int)
    args=p.parse_args()
    if not 1 <= args.seconds <= 7200:
        p.error("seconds must be between 1 and 7200")
    exercise(args.seconds)
