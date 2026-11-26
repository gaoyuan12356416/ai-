"""Run on the CPU host. Emit read-only ledger facts for the scoped HK cutover."""
import json
from pathlib import Path
import sqlite3
import sys
import time


def evidence(path):
    with sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True, timeout=3) as db:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute("""SELECT id,task,state,created,identity IS NOT NULL AS bound,reason FROM remote_lease
            WHERE node='hk-read-assist' AND state NOT IN ('closed','cancelled') ORDER BY created""")
        return dict(captured_at=time.time(), entries=[dict(lease_id=key, task=task, state=state,
                    created=created, bound=bool(bound), reason=reason)
                    for key,task,state,created,bound,reason in rows])


if __name__ == "__main__":
    print(json.dumps(evidence(sys.argv[1] if len(sys.argv)>1 else
          "/mnt/data-disk/gpu-backend-offload-20261123/sql-teal-state/remote-leases.sqlite3")))
