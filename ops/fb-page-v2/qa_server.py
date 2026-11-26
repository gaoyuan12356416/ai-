"""Run the production HTTP endpoint and selector with an independent audit database and no network notifier."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from features.fb_page_assignment_v2.audit import Workers
from features.fb_page_assignment_v2.audit_sandbox import SQLiteAuditSink
from features.fb_page_assignment_v2.ledger import Store
from features.fb_page_assignment_v2.server import Endpoint
from features.fb_page_assignment_v2.service import Assignment


def private_file(path, content):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w",
                   encoding="utf-8") as f:
        f.write(content)


def initialize(directory):
    root = Path(directory).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = root / "qa-instance"
    if not marker.exists():
        if any(root.iterdir()):
            raise ValueError("use an empty QA directory or a previous QA instance")
        private_file(marker, uuid.uuid4().hex)
        private_file(root/"qa-bearer", secrets.token_urlsafe(36))
    uuid.UUID(marker.read_text().strip())
    return root


class QASources:
    def __init__(self, bind):
        self.sources = {ipaddress.ip_address(ip) for ip in ("127.0.0.1", "10.89.0.17", bind)}

    def allows(self, peer):
        return ipaddress.ip_address(peer) in self.sources


class OfflineRelay:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(str(path))) as c:
            c.execute("CREATE TABLE IF NOT EXISTS qa_event(id TEXT PRIMARY KEY,kind TEXT NOT NULL)")
            c.commit()

    def push(self, events):
        with closing(sqlite3.connect(str(self.path), timeout=5)) as c:
            c.executemany("INSERT OR IGNORE INTO qa_event VALUES(?,?)", [(e["id"], e["kind"]) for e in events])
            c.commit()


def diagnostics(root, store, assignment, budget=None):
    with store.db() as db:
        totals = db.execute("SELECT count(*),count(receipt),count(details),coalesce(sum(attempts),0) "
                            "FROM assignment").fetchone()
        large = db.execute("SELECT count(packed) FROM candidate_snapshot").fetchone()[0]
    with closing(sqlite3.connect(str(root/"qa-audit.sqlite3"))) as db:
        kinds = dict(db.execute("SELECT selection_stage,count(*) FROM audit GROUP BY selection_stage"))
        headers = db.execute("SELECT count(*) FROM audit WHERE item_index=0 AND "
                             "selection_stage<>'candidate_snapshot'").fetchone()[0]
        items = db.execute("SELECT count(*) FROM audit WHERE item_index>0").fetchone()[0]
        no_page = dict(db.execute("SELECT error_code,count(*) FROM audit WHERE item_index>0 "
                                  "AND page_id IS NULL GROUP BY error_code"))
        alerts = dict(db.execute("SELECT kind,count(*) FROM qa_event GROUP BY kind"))
    backlog = store.status()
    age = max(0, time.time()-backlog["oldest"]) if backlog["oldest"] is not None else 0.
    return {"qa_only": True, "instance": (root/"qa-instance").read_text().strip(),
            "audit_target": "isolated-sqlite", "feishu": "disabled", "receipts": totals[0],
            "confirmed": totals[1], "unretired_details": totals[2], "unretired_snapshots": large,
            "audit_retries": totals[3], "pending": backlog["pending"], "oldest_pending_seconds":round(age,1),
            "audit_headers": headers,
            "audit_items": items, "stages": kinds, "null_causes": no_page, "offline_alerts": alerts,
            "active_groups": assignment.active, "unanswered": len(assignment.pending),
            "sql_budget": budget.status() if budget is not None else None}


async def serve(root, bind, port, *, compute=None, on_ready=None, stop=None):
    if bind not in ("127.0.0.1", "10.89.0.125"):
        raise ValueError("use the Hong Kong private address or loopback")
    token = (root/"qa-bearer").read_text().strip()
    store = Store(root/"state")
    assignment = Assignment(store, compute=compute)
    sink = SQLiteAuditSink(root/"qa-audit.sqlite3")
    relay = OfflineRelay(root/"qa-audit.sqlite3")
    budget = None
    if compute is None:
        from features.hk_sql_budget.ledger import local_ledger
        budget = local_ledger()
    await assignment.start()
    workers = Workers(store, busy=assignment.busy, sink=sink, relay=relay)
    assignment.alerts = workers
    endpoint = Endpoint(assignment, token, QASources(bind),
                        diagnostics=lambda: diagnostics(root, store, assignment, budget))
    if stop is None:
        stop = asyncio.Event()
        if os.name == "posix":
            for sig in (signal.SIGINT, signal.SIGTERM):
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    workers.start()
    try:
        async with await asyncio.start_server(endpoint.serve, bind, port, backlog=2048) as server:
            address = server.sockets[0].getsockname()
            print(json.dumps({"qa_ready": True, "bind": address[0], "port": address[1],
                              "state": str(root), "sink": "isolated-sqlite"}), flush=True)
            if on_ready is not None:
                on_ready.set_result(address[1])
            await stop.wait()
        endpoint.stopping = True
        if endpoint.clients:
            await asyncio.gather(*tuple(endpoint.clients), return_exceptions=True)
    finally:
        await assignment.close()
        await asyncio.to_thread(workers.close)


def export(root, destination):
    if not (root/"qa-instance").is_file() or not (root/"qa-audit.sqlite3").is_file():
        raise ValueError("QA run not found")
    if Store(root/"state").status()["pending"]:
        raise ValueError("wait for all remote audit receipts before exporting")
    destination = Path(destination)
    # Reserve the caller-chosen filename and prevent overwriting an earlier evidence copy.
    private_file(destination, "")
    with closing(sqlite3.connect(str(root/"qa-audit.sqlite3"))) as src, closing(sqlite3.connect(str(destination))) as dst:
        src.backup(dst)
        dst.commit()
    h = hashlib.sha256()
    with destination.open("rb") as file:
        for chunk in iter(lambda: file.read(1024*1024), b""):
            h.update(chunk)
    print(json.dumps({"export": str(destination), "sha256": h.hexdigest()}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("serve")
    runner.add_argument("--root", type=Path, required=True)
    runner.add_argument("--bind", default="127.0.0.1")
    runner.add_argument("--port", type=int, default=8945)
    copy = sub.add_parser("export")
    copy.add_argument("--root", type=Path, required=True)
    copy.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "serve":
        asyncio.run(serve(initialize(args.root), args.bind, args.port))
    else:
        export(args.root, args.output)
