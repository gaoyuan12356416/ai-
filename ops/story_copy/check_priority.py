"""Exercise three real model calls in a new isolated ledger; never discover or publish tasks."""

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="New, empty canary directory")
    args = parser.parse_args()
    root = args.root.resolve()
    if root == Path("/mnt/data-disk/story-copy") or root.exists():
        raise SystemExit("use a new isolated directory")
    root.mkdir(parents=True, exist_ok=False)
    os.environ["STORY_COPY_ROOT"] = str(root)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from features.story_copy import generator, sources, store, worker

    store.initialize()
    now = time.time()
    with store.connection() as c:
        c.execute("INSERT INTO switches(endpoint,enabled,marks,actor,at) VALUES('fb',1,?, 'isolated-canary',?)",
                  (store.dump({"fa": 0}), now))
        for i in (3, 2, 1):
            task = sources.base("fa", i, "canary:" + str(i), "fixture-" + str(i), "en",
                                "dramawave", "A Hidden Letter",
                                "Ada hides a letter in a locked desk. Ben finds its key and asks Ada who wrote the letter.",
                                publish_at=now + i * 30, urgent=False)
            store.enqueue(c, task)
    barrier = threading.Barrier(3)
    timings = []
    lock = threading.Lock()

    def run(rows, emit, usage):
        barrier.wait(timeout=20)
        start = time.time()
        generator.run(rows, emit, usage)
        with lock:
            timings.append(dict(key=rows[0]["key"], started=start, finished=time.time()))

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(worker.generation_cycle, run) for _ in range(3)]
        for f in futures:
            f.result()
    with store.connection(readonly=True) as c:
        jobs = [dict(r) for r in c.execute("SELECT key,state,batch,reason FROM jobs ORDER BY batch")]
        batches = [dict(r) for r in c.execute("SELECT id,requested,accepted,calls,input_tokens,output_tokens FROM batches ORDER BY id")]
        retries = c.execute("SELECT count(*) FROM retries").fetchone()[0]
    result = dict(jobs=jobs, batches=batches, timings=timings, retries=retries)
    result["passed"] = (len(timings) == 3 and retries == 0
                        and [r["key"] for r in jobs] == ["canary:1", "canary:2", "canary:3"]
                        and all(r["state"] == "ready" for r in jobs)
                        and max(t["started"] for t in timings) < min(t["finished"] for t in timings))
    (root / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
    if not result["passed"]:
        raise SystemExit("isolated concurrent generation did not pass")


if __name__ == "__main__":
    main()
