"""Batch frozen receipts into the existing audit table and verify the committed rows."""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
import zlib
from collections import deque
from datetime import datetime, timezone

from .ledger import digest, encoded, event_payload

LOG = logging.getLogger(__name__)
TABLE = "`ads_ai`.`fb_drama_page_assignment_log`"
COLS = ("task_id", "item_index", "external_task_id", "pool_group_id", "drama_id",
        "page_id", "status", "selection_stage", "error_code", "error_message", "diagnostics_json",
        "http_status", "payload_hash", "created_at", "updated_at", "revision", "alert_status",
        "alert_reasons_json", "feishu_message_id", "alert_error_code")


class ReadbackMismatch(ValueError):
    pass


def _row(task_id, index, external, pool, created, drama, page, stage, status, error, detail):
    diag = encoded(detail)
    facts = (task_id, index, external, pool, drama, page, stage, status, error, 200, diag)
    hashed = digest(encoded(facts).encode())
    ts = datetime.fromtimestamp(created, timezone.utc).replace(tzinfo=None)
    return (task_id, index, external, pool, drama, page, status, stage, error, "", diag,
            200, hashed, ts, ts, 1, "none", "[]", "", "")


def _check(row):
    return (1, row[12], digest(row[10].encode()), *row[2:8], row[8], row[11])


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _snapshot(snapshot):
    key = snapshot["hash"]
    if snapshot["receipt"] is not None:
        receipt = json.loads(snapshot["receipt"])
        expected = {(task, 0): tuple(check) for task, check in receipt}
        return [], expected, receipt
    packed = snapshot["packed"]
    if not packed:
        raise ReadbackMismatch("frozen_candidate_payload_missing")
    try:
        valid = digest(zlib.decompress(packed)) == key
    except zlib.error:
        valid = False
    if not valid:
        raise ReadbackMismatch("frozen_candidate_digest_mismatch")
    parts = list(_chunks(packed, 96 * 1024))
    records = []
    for number, chunk in enumerate(parts):
        task = digest(("hk-page-v2:snapshot:%s:%d" % (key, number)).encode())
        payload = {"codec": "zlib-base64", "sha256": key, "part": number, "parts": len(parts),
                   "bytes": base64.b64encode(chunk).decode("ascii")}
        records.append(_row(task, 0, "__v2_candidate_snapshot__", 0, snapshot["created"], None, None,
                            "candidate_snapshot", "frozen", "", payload))
    expected = {(r[0], 0): _check(r) for r in records}
    return records, expected, [(task, check) for (task, _), check in expected.items()]


def prepare(store, tasks):
    records, expected, task_receipts, snapshots = [], {}, {}, {}
    for task in tasks:
        snap = task["snapshot"]
        if snap not in snapshots:
            batch, checks, receipt = _snapshot(store.snapshot(snap))
            records.extend(batch)
            expected.update(checks)
            snapshots[snap] = receipt
        if digest(task["details"]) != task["details_hash"]:
            raise ReadbackMismatch("frozen_decision_digest_mismatch")
        details = json.loads(zlib.decompress(task["details"]))
        fixed = (task["external_id"], task["pool"], task["created"])
        header = {"fingerprint": task["fingerprint"], "items": len(details["rows"]),
                  "snapshot": details["snapshot"]}
        batch = [_row(task["task_id"], 0, *fixed, None, None, "v2_request", "frozen", "", header)]
        for i, item in enumerate(details["rows"], 1):
            page = item["page_id"]
            stage = item.get("selection_stage") or "no_match"
            code = item.get("error_code") or ""
            batch.append(_row(task["task_id"], i, *fixed, item["drama_id"], page, stage,
                              item.get("status") or ("assigned" if page else "unassigned"),
                              code, {"decision": item, "candidate_snapshot": snap}))
        records.extend(batch)
        for row in batch:
            expected[row[0], row[1]] = _check(row)
        task_receipts[task["task_id"]] = {"revision": 1, "rows": [[r[1], r[12]] for r in batch]}
    records.sort(key=lambda row: (row[0], row[1]))
    return records, expected, task_receipts, snapshots


def verify(expected, found):
    if expected != found:
        raise ReadbackMismatch("audit_row_count_revision_or_hash_mismatch")


def missing_records(records, expected, existing):
    if any(key not in expected or expected[key] != value for key, value in existing.items()):
        raise ReadbackMismatch("existing_audit_facts_differ")
    missing = [row for row in records if (row[0], row[1]) not in existing]
    if len(expected) - len(existing) != len(missing):
        raise ReadbackMismatch("confirmed_snapshot_missing_from_primary")
    return missing


class MySQLSink:
    def __init__(self, connection=None):
        if connection is None:
            from features.hk_sql_budget import budget_connection
            connection = budget_connection
        self.connection = connection

    @staticmethod
    def _read(conn, expected, lock=False):
        found = {}
        with conn.cursor() as c:
            for ids in _chunks(sorted({key[0] for key in expected}), 100):
                c.execute("SELECT task_id,item_index,revision,payload_hash,"
                          "CONVERT(SHA2(diagnostics_json,256) USING ascii),"
                          "external_task_id,pool_group_id,drama_id,page_id,status,selection_stage,error_code,"
                          "http_status FROM " + TABLE + " WHERE task_id IN (" +
                          ",".join(["%s"] * len(ids)) + ")" + (" FOR UPDATE" if lock else ""), ids)
                for row in c.fetchall():
                    found[row[0], row[1]] = tuple(row[2:])
        return found

    def write(self, records, expected):
        # Only exact primary keys are used. Duplicate immutable rows are read back,
        # including after loss of a COMMIT acknowledgment.
        sql = ("INSERT INTO " + TABLE + " (" + ",".join(COLS) + ") VALUES "
               "(" + ",".join(["%s"] * len(COLS)) + ") ON DUPLICATE KEY UPDATE task_id=VALUES(task_id)")
        with self.connection(role="write", deadline=time.monotonic() + 25) as conn:
            # Reconcile original keys on the primary before retrying any ambiguous
            # commit. Already confirmed rows do not receive a second INSERT.
            missing = missing_records(records, expected, self._read(conn, expected))
            with conn.cursor() as c:
                for group in _chunks(missing, 80):
                    # PyMySQL caps executemany packet size and preserves individual large rows.
                    c.executemany(sql, group)
            conn.commit()
            with conn.cursor() as c:
                c.execute("START TRANSACTION READ WRITE")
            verify(expected, self._read(conn, expected, lock=True))


class Workers:
    def __init__(self, store, busy=lambda: False, sink=None, relay=None):
        from .alerts import Relay
        self.store = store
        self.busy = busy
        self.sink = sink if sink is not None else MySQLSink()
        self.relay = relay if relay is not None else Relay()
        self.stop = threading.Event()
        self.threads = []
        self._emergency_lock = threading.Lock()
        self._emergency_events = deque()
        self._emergency_open = set()

    def _remember(self, event):
        with self._emergency_lock:
            if len(self._emergency_events) >= 512:
                LOG.error("v2_emergency_relay_full kind=%s", event["kind"])
                return
            self._emergency_events.append(event)
            if event["action"] == "fault":
                self._emergency_open.add(event["kind"])
            else:
                self._emergency_open.discard(event["kind"])

    def incident(self, kind, external_task_id="", page_group_id=None, detail="", event_id=None):
        try:
            self.store.incident(kind, external_task_id, page_group_id, detail, event_id)
        except Exception as exc:
            # A disk outage cannot persist an alert to the broken disk. The
            # independent CPU receiver still deduplicates this event by its ID.
            LOG.error("v2_alert_local_persistence_failed class=%s", type(exc).__name__)
            self._remember(event_payload(kind, external_task_id, page_group_id, detail, event_id))

    def recovery(self, kind):
        try:
            self.store.recovery(kind)
        except Exception as exc:
            LOG.error("v2_alert_local_recovery_failed class=%s", type(exc).__name__)
        with self._emergency_lock:
            active = kind in self._emergency_open
        if active:
            self._remember(event_payload(kind, action="recovered"))

    def start(self):
        if self.threads:
            return self
        for name, target, args in (("audit-1", self._writer, (False,)),
                                   ("audit-2", self._writer, (True,)),
                                   ("incident-relay", self._alerts, ())):
            thread = threading.Thread(name=name, target=target, args=args, daemon=True)
            self.threads.append(thread)
            thread.start()
        return self

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join()

    def flush(self, tasks):
        try:
            records, expected, receipts, snapshots = prepare(self.store, tasks)
            self.sink.write(records, expected)
            self.store.confirm(tasks, receipts, snapshots)
            if not self.store.status()["failed"]:
                self.recovery("audit_sync")
        except ReadbackMismatch as exc:
            if len(tasks) > 1:
                # One conflicting receipt cannot continuously hold unrelated decisions.
                mid = len(tasks) // 2
                self.flush(tasks[:mid])
                self.flush(tasks[mid:])
            else:
                self._failed(tasks, type(exc).__name__)
        except Exception as exc:
            code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
            self._failed(tasks, type(exc).__name__ + (".%d" % code if code is not None else ""))

    def _failed(self, tasks, code):
        self.store.defer(tasks, code)
        for task in tasks:
            self.incident("audit_sync", task["external_id"], task["pool"], code,
                          event_id=task["task_id"] + ".audit." + task["claim"])
        LOG.warning("v2_audit_deferred tasks=%d reason=%s", len(tasks), code)

    def _writer(self, idle_only):
        last_busy = time.monotonic()
        local_failure = False
        while not self.stop.is_set():
            try:
                busy = self.busy()
                if busy:
                    last_busy = time.monotonic()
                if idle_only and time.monotonic() - last_busy < 2:
                    self.stop.wait(0.2)
                    continue
                tasks = self.store.claim()
                if local_failure:
                    self.recovery("audit_store")
                    local_failure = False
                if not tasks:
                    self.stop.wait(0.3)
                    continue
                self.flush(tasks)
                if busy:
                    self.stop.wait(0.25)
            except Exception as exc:
                LOG.error("v2_audit_worker_error class=%s", type(exc).__name__)
                self.incident("audit_store", detail=type(exc).__name__)
                local_failure = True
                self.stop.wait(2)

    def _alerts(self):
        delay = 1
        while not self.stop.is_set():
            try:
                with self._emergency_lock:
                    emergency = list(self._emergency_events)[:50]
                if emergency:
                    self.relay.push(emergency)
                    with self._emergency_lock:
                        for _ in emergency:
                            self._emergency_events.popleft()
                    delay = 1
                    continue
                events = self.store.alert_events()
                if not events:
                    self.stop.wait(0.3)
                    continue
                self.relay.push(events)
                self.store.relay_confirmed([e["id"] for e in events])
                delay = 1
            except Exception as exc:
                LOG.warning("v2_alert_relay_deferred class=%s", type(exc).__name__)
                self.stop.wait(delay)
                delay = min(60, delay * 2)
