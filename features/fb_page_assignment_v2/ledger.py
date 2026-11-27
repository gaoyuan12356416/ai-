"""Durable assignment decisions and outboxes on the Hong Kong host."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


KEYED_INCIDENTS = frozenset(("request_conflict", "pool_authorization"))


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class FrozenCandidates:
    digest: str
    packed: bytes


def freeze_candidates(candidates):
    if isinstance(candidates, FrozenCandidates):
        return candidates
    raw = encoded(candidates).encode("utf-8")
    return FrozenCandidates(digest(raw), zlib.compress(raw, 1))


def safe_code(value):
    value = str(value or "")
    return value if re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", value) else "unspecified"


def event_payload(kind, external_task_id="", page_group_id=None, detail="", event_id=None, action="fault"):
    return dict(id=event_id or uuid.uuid4().hex, kind=safe_code(kind), action=action,
                external_task_id=str(external_task_id or "")[:128],
                page_group_id=page_group_id, detail=safe_code(detail), at=time.time())


class Store:
    def __init__(self, data_root):
        root = Path(data_root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "assignment-v2.sqlite3"
        with self.db() as db:
            if db.execute("PRAGMA page_count").fetchone()[0] == 0:
                db.execute("PRAGMA auto_vacuum=FULL")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS candidate_snapshot(
                    hash TEXT PRIMARY KEY, packed BLOB, receipt TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS assignment(
                    external_id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL, pool INTEGER NOT NULL, created REAL NOT NULL,
                    response TEXT NOT NULL, details BLOB, details_hash TEXT NOT NULL, snapshot TEXT NOT NULL,
                    receipt TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                    claim TEXT, lease_until REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS audit_pending ON assignment(next_try,created) WHERE receipt IS NULL;
                CREATE TABLE IF NOT EXISTS alert_outbox(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS failure_signal(kind TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS failure_key(kind TEXT NOT NULL, external_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,PRIMARY KEY(kind,external_id));
            """)

    @contextmanager
    def db(self, write=False):
        conn = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA journal_size_limit=4194304")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        finally:
            conn.close()

    def record(self, external_id, fingerprint, group, ids, selection):
        """Append this execution without reading previous requests or decisions."""
        rows = selection["rows"]
        if len(rows) != len(ids) or any(row["drama_id"] != item for item, row in zip(ids, rows)):
            raise ValueError("decision_input_order_mismatch")
        meta = dict(selection["snapshot"])
        candidates = freeze_candidates(meta.pop("candidates", []))
        meta["candidate_snapshot"] = candidates.digest
        meta["external_task_id"] = external_id
        meta["execution_mode"] = "direct-per-request"
        task_id = "v2_" + uuid.uuid4().hex
        response = {"task_id": task_id, "results": [{"drama_id": r["drama_id"], "page_id": r["page_id"]}
                                                     for r in rows]}
        details = zlib.compress(encoded({"snapshot": meta, "rows": rows}).encode(), 1)
        now = time.time()
        with self.db(write=True) as db:
            db.execute("INSERT OR IGNORE INTO candidate_snapshot(hash,packed,created) VALUES(?,?,?)",
                       (candidates.digest, candidates.packed, now))
            db.execute("""INSERT INTO assignment(external_id,task_id,fingerprint,pool,created,response,
                       details,details_hash,snapshot) VALUES(?,?,?,?,?,?,?,?,?)""",
                       ("call:" + task_id, task_id, fingerprint, group, now, encoded(response), details,
                        digest(details), candidates.digest))
        return 200, response

    def claim(self, limit=12):
        # A lost claim is safely reconciled by the original MySQL keys and digests.
        now = time.time()
        token = uuid.uuid4().hex
        with self.db() as db:
            ready = db.execute("""SELECT 1 FROM assignment WHERE receipt IS NULL
                      AND next_try<=? AND lease_until<=? LIMIT 1""", (now, now)).fetchone()
        if not ready:
            return []
        with self.db(write=True) as db:
            rows = db.execute("""SELECT task_id,external_id,pool,created,details,details_hash,snapshot,fingerprint
                      FROM assignment WHERE receipt IS NULL AND next_try<=? AND lease_until<=?
                      ORDER BY next_try,created LIMIT ?""", (now, now, limit)).fetchall()
            db.executemany("UPDATE assignment SET claim=?,lease_until=? WHERE task_id=?",
                           [(token, now + 120, r["task_id"]) for r in rows])
        return [dict(row, claim=token) for row in rows]

    def snapshot(self, key):
        with self.db() as db:
            row = db.execute("SELECT * FROM candidate_snapshot WHERE hash=?", (key,)).fetchone()
            if row is None:
                raise ValueError("frozen_candidate_input_missing")
            return dict(row)

    def confirm(self, tasks, task_receipts, snapshots):
        # Called only after an acknowledged commit and a fresh primary readback.
        with self.db(write=True) as db:
            for key, receipt in snapshots.items():
                db.execute("UPDATE candidate_snapshot SET receipt=?,packed=NULL WHERE hash=?",
                           (encoded(receipt), key))
            for task in tasks:
                db.execute("""UPDATE assignment SET receipt=?,details=NULL,claim=NULL,lease_until=0,
                              last_error='' WHERE task_id=? AND claim=? AND receipt IS NULL""",
                           (encoded(task_receipts[task["task_id"]]), task["task_id"], task["claim"]))

    def defer(self, tasks, code):
        now = time.time()
        with self.db(write=True) as db:
            db.executemany("""UPDATE assignment SET attempts=attempts+1,claim=NULL,lease_until=0,
                         next_try=?+min(120,2*(1 << min(attempts,6))),last_error=?
                         WHERE task_id=? AND claim=? AND receipt IS NULL""",
                         [(now, safe_code(code), t["task_id"], t["claim"]) for t in tasks])

    def status(self):
        with self.db() as db:
            row = db.execute("""SELECT count(*) AS pending,
                           coalesce(sum(last_error<>''),0) AS failed,min(created) AS oldest
                           FROM assignment WHERE receipt IS NULL""").fetchone()
            return dict(row)

    def incident(self, kind, external_task_id="", page_group_id=None, detail="", event_id=None):
        event = event_payload(kind, external_task_id, page_group_id, detail, event_id)
        with self.db(write=True) as db:
            db.execute("INSERT OR IGNORE INTO failure_signal(kind) VALUES(?)", (event["kind"],))
            if kind in KEYED_INCIDENTS and external_task_id:
                db.execute("INSERT INTO failure_key VALUES(?,?,?) ON CONFLICT(kind,external_id) "
                           "DO UPDATE SET event_id=excluded.event_id",(kind,external_task_id,event["id"]))
            db.execute("INSERT OR IGNORE INTO alert_outbox(id,body) VALUES(?,?)", (event["id"], encoded(event)))

    def recovery(self, kind):
        kind = safe_code(kind)
        if kind in KEYED_INCIDENTS:
            return
        with self.db() as db:
            if not db.execute("SELECT 1 FROM failure_signal WHERE kind=?", (kind,)).fetchone():
                return
        with self.db(write=True) as db:
            if db.execute("DELETE FROM failure_signal WHERE kind=?", (kind,)).rowcount:
                event = event_payload(kind, action="recovered")
                db.execute("INSERT INTO alert_outbox(id,body) VALUES(?,?)", (event["id"], encoded(event)))

    def active_incidents(self):
        with self.db() as db:
            return {row[0] for row in db.execute("SELECT kind FROM failure_signal")}

    def active_keyed_incidents(self):
        with self.db() as db:
            active = {}
            for kind,key,event in db.execute("SELECT kind,external_id,event_id FROM failure_key"):
                active.setdefault(kind,{})[key] = event
            return active

    def recover_key(self, kind, key, event_id, *, last, volatile=False):
        """Only a successful retry which saw this generation can clear its own key."""
        if kind not in KEYED_INCIDENTS or not key or not event_id:
            raise ValueError("invalid_keyed_recovery")
        with self.db(write=True) as db:
            if volatile:
                # The worker still holds this exact newer fault in memory following a disk outage.
                changed = db.execute("DELETE FROM failure_key WHERE kind=? AND external_id=?",(kind,key)).rowcount
            else:
                changed = db.execute("DELETE FROM failure_key WHERE kind=? AND external_id=? AND event_id=?",
                                     (kind,key,event_id)).rowcount
            if not last or (not changed and not volatile):
                return
            if db.execute("SELECT 1 FROM failure_key WHERE kind=? LIMIT 1",(kind,)).fetchone():
                return
            closed = db.execute("DELETE FROM failure_signal WHERE kind=?",(kind,)).rowcount
            if closed or volatile:
                event = event_payload(kind,external_task_id=key,action="recovered")
                db.execute("INSERT INTO alert_outbox(id,body) VALUES(?,?)",(event["id"],encoded(event)))

    def alert_events(self):
        with self.db() as db:
            rows = db.execute("SELECT body FROM alert_outbox ORDER BY seq LIMIT 50").fetchall()
            return [json.loads(r[0]) for r in rows]

    def relay_confirmed(self, ids):
        with self.db(write=True) as db:
            db.executemany("DELETE FROM alert_outbox WHERE id=?", [(k,) for k in ids])
