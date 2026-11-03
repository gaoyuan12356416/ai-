"""Durable callback delivery, serialized per receipt across node processes.

Only coordinator receipts are retried here. Task execution and platform writes
are never retried. SQLite leases are renewed while a callback is in flight;
process death releases authority through lease expiry, not by deleting facts.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from .client import CoordinatorError


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _claim_key(payload, identity):
    if payload.get("job_id") is None or payload.get("generation") is None:
        return "receipt:" + identity
    return _canonical([payload["job_id"], payload["generation"]])


def _error_code(value, default="receipt_not_accepted"):
    # Never persist exception messages, URLs or response bodies as diagnostics.
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,119}", value) else default


class ReceiptOutbox:
    """Fsync before delivery; facts and identities remain unchanged on retry."""

    def __init__(self, path, *, lease_seconds=120, clock=time.time):
        self.path, self.clock = str(path), clock
        self.lease_seconds = float(lease_seconds)
        if not math.isfinite(self.lease_seconds) or self.lease_seconds <= 0:
            raise ValueError("invalid receipt delivery lease")
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE IF NOT EXISTS receipt_outbox (id TEXT PRIMARY KEY,route TEXT NOT NULL,payload TEXT NOT NULL,created_at REAL NOT NULL,acked_at REAL)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(receipt_outbox)")}
            additions = {
                "claim_key": "TEXT NOT NULL DEFAULT ''",
                "background_after": "REAL NOT NULL DEFAULT 0",
                "next_attempt_at": "REAL NOT NULL DEFAULT 0",
                "delivery_owner": "TEXT NOT NULL DEFAULT ''",
                "delivery_lease_until": "REAL NOT NULL DEFAULT 0",
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "last_attempt_at": "REAL",
                "last_error_code": "TEXT NOT NULL DEFAULT ''",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    conn.execute("ALTER TABLE receipt_outbox ADD COLUMN " + name + " " + declaration)
            # Existing pending facts gain only delivery metadata, never a new ID.
            for identity, payload in conn.execute("SELECT id,payload FROM receipt_outbox WHERE acked_at IS NULL AND claim_key=''").fetchall():
                conn.execute("UPDATE receipt_outbox SET claim_key=? WHERE id=?", (_claim_key(json.loads(payload), identity), identity))
            conn.execute("CREATE INDEX IF NOT EXISTS receipt_outbox_pending ON receipt_outbox(acked_at,created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS receipt_outbox_claim_pending ON receipt_outbox(claim_key,acked_at,created_at)")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @contextlib.contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA synchronous=FULL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def put(self, route, payload, receipt_id=None, *, defer_background_seconds=0):
        identity = receipt_id or str(payload.get("receipt_id") or payload.get("checkpoint_id") or uuid.uuid4().hex)
        delay = float(defer_background_seconds)
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("invalid receipt background delay")
        encoded, now = _canonical(payload), self.clock()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT route,payload FROM receipt_outbox WHERE id=?", (identity,)).fetchone()
            if existing and existing != (route, encoded):
                raise ValueError("receipt id reused with changed facts")
            conn.execute("INSERT OR IGNORE INTO receipt_outbox(id,route,payload,created_at,claim_key,background_after) VALUES(?,?,?,?,?,?)",
                         (identity, route, encoded, now, _claim_key(payload, identity), now + delay))
        return identity

    def _claim_next(self, only):
        now, owner = self.clock(), uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""SELECT r.id,r.route,r.payload,r.attempts FROM receipt_outbox r
                WHERE r.acked_at IS NULL AND r.next_attempt_at<=? AND r.delivery_lease_until<=?
                AND """ + ("r.id=?" if only else "r.background_after<=?") + """
                AND NOT EXISTS (SELECT 1 FROM receipt_outbox p
                    WHERE p.acked_at IS NULL AND p.claim_key=r.claim_key
                    AND (p.created_at<r.created_at OR (p.created_at=r.created_at AND p.rowid<r.rowid)))
                ORDER BY r.created_at,r.rowid LIMIT 1""", (now, now, only if only else now)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE receipt_outbox SET delivery_owner=?,delivery_lease_until=?,attempts=attempts+1,last_attempt_at=? WHERE id=?",
                         (owner, now + self.lease_seconds, now, row[0]))
        return (*row, owner)

    def _renew(self, identity, owner):
        now = self.clock()
        with self.connect() as conn:
            return conn.execute("UPDATE receipt_outbox SET delivery_lease_until=? WHERE id=? AND acked_at IS NULL AND delivery_owner=? AND delivery_lease_until>?",
                                (now + self.lease_seconds, identity, owner, now)).rowcount == 1

    @contextlib.contextmanager
    def _keep_lease(self, identity, owner):
        stopped = threading.Event()

        def renew():
            while not stopped.wait(max(.05, min(30, self.lease_seconds / 3))):
                try:
                    if not self._renew(identity, owner):
                        return
                except sqlite3.Error:
                    # Losing the delivery lease cannot authorize an ACK. The
                    # original durable callback remains safe to reconcile later.
                    return

        thread = threading.Thread(target=renew, name="pipeline-receipt-lease", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=2)

    def _fail(self, identity, owner, attempt, code):
        now = self.clock()
        delay = 15 * (2 ** min(attempt - 1, 2))
        with self.connect() as conn:
            conn.execute("UPDATE receipt_outbox SET delivery_owner='',delivery_lease_until=0,next_attempt_at=?,last_error_code=? WHERE id=? AND acked_at IS NULL AND delivery_owner=?",
                         (now + delay, _error_code(code), identity, owner))

    def _ack(self, identity, owner):
        now = self.clock()
        with self.connect() as conn:
            return conn.execute("UPDATE receipt_outbox SET acked_at=?,delivery_owner='',delivery_lease_until=0 WHERE id=? AND acked_at IS NULL AND delivery_owner=? AND delivery_lease_until>?",
                                (now, identity, owner, now)).rowcount == 1

    def flush(self, client, *, only=None):
        """Deliver at most 100 callbacks; False also means leased or deferred.

        ``only`` gives fresh inline receipts priority over the background delay,
        but never bypasses another delivery owner, failed-attempt backoff, or an
        earlier unacknowledged receipt belonging to the same task generation.
        """
        for _ in range(1 if only else 100):
            row = self._claim_next(only)
            if row is None:
                break
            identity, route, payload, previous_attempts, owner = row
            with self._keep_lease(identity, owner):
                try:
                    result = client.post(route, json.loads(payload))
                    if not isinstance(result, dict):
                        raise CoordinatorError("coordinator_response_invalid", 502)
                except Exception as exc:
                    self._fail(identity, owner, previous_attempts + 1, getattr(exc, "code", "receipt_delivery_failed"))
                    if only:
                        raise
                    continue
                if result.get("accepted") is not True or result.get("ok") is False:
                    self._fail(identity, owner, previous_attempts + 1, result.get("code") or result.get("error"))
                    continue
                self._ack(identity, owner)
        with self.connect() as conn:
            remaining = conn.execute("SELECT count(*) FROM receipt_outbox WHERE acked_at IS NULL" + (" AND id=?" if only else ""), (only,) if only else ()).fetchone()[0]
        return remaining == 0
