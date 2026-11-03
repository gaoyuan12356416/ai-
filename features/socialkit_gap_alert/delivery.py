"""Durable event reservations and frozen per-card receipts; no blind resends."""
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from features.post_daily_report.delivery import DefiniteFailure, DeliveryUnknown
from .cards import compact, split_cards


def key(event, chat):
    return compact([chat, event["date"], event["app_id"], event["platform"], event["drama"]])


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA foreign_keys=ON;
          PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS alert_card(
            id TEXT PRIMARY KEY, report_date TEXT NOT NULL, chat_id TEXT NOT NULL,
            card_json TEXT NOT NULL, events_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL, request_uuid TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            message_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS alert_event(
            event_key TEXT PRIMARY KEY, report_date TEXT NOT NULL, chat_id TEXT NOT NULL,
            app_id INTEGER NOT NULL, platform TEXT NOT NULL, drama TEXT NOT NULL,
            card_id TEXT NOT NULL REFERENCES alert_card(id),
            UNIQUE(chat_id,report_date,app_id,platform,drama));
          CREATE INDEX IF NOT EXISTS alert_card_day ON alert_card(report_date,chat_id,status);
        """)
        # A process death during transport has an unknown outcome, not failure.
        self.db.execute("UPDATE alert_card SET status='unknown',error='interrupted_send_requires_reconciliation',updated_at=? WHERE status='sending'", (stamp(),))
        self.db.commit()

    def close(self):
        self.db.close()

    def prepare(self, report, chat):
        events = report["events"]
        eligible = {key(event, chat): compact(event) for event in events}
        self.db.execute("BEGIN IMMEDIATE")
        try:
            # Definite failures may be retried only while every frozen item is
            # still eligible. Cancelled reservations can form a fresh batch.
            for card in self.db.execute("SELECT * FROM alert_card WHERE report_date=? AND chat_id=? AND status IN ('prepared','failed')", (report["date"], chat)).fetchall():
                old = {key(e, chat): compact(e) for e in json.loads(card["events_json"])}
                if any(eligible.get(event_key) != evidence for event_key, evidence in old.items()):
                    self.db.execute("UPDATE alert_card SET status='cancelled',updated_at=? WHERE id=?", (stamp(), card["id"]))
                    self.db.execute("DELETE FROM alert_event WHERE card_id=?", (card["id"],))
            known = {row[0] for row in self.db.execute("SELECT event_key FROM alert_event WHERE report_date=? AND chat_id=?", (report["date"], chat))}
            new = [event for event in events if key(event, chat) not in known]
            for group, card in split_cards(report, new, chat):
                card_id = uuid.uuid4().hex
                payload = compact(card)
                request_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "socialkit-gap-alert/" + card_id))
                now = stamp()
                evidence = {k: v for k, v in report.items() if k != "events"}
                self.db.execute("INSERT INTO alert_card VALUES(?,?,?,?,?,?,?,?,'prepared',0,'','',?,?)",
                                (card_id, report["date"], chat, payload, compact(group), compact(evidence),
                                 hashlib.sha256(payload.encode()).hexdigest(), request_uuid, now, now))
                self.db.executemany("INSERT INTO alert_event VALUES(?,?,?,?,?,?,?)", [
                    (key(e, chat), e["date"], chat, e["app_id"], e["platform"], e["drama"], card_id) for e in group])
            self.db.commit()
            return len(new)
        except BaseException:
            self.db.rollback()
            raise

    def pending(self, day, chat):
        return [dict(row) for row in self.db.execute("SELECT * FROM alert_card WHERE report_date=? AND chat_id=? AND status IN ('prepared','failed') AND attempts<3 ORDER BY created_at,rowid", (day, chat))]

    def claim(self, card_id):
        with self.db:
            row = self.db.execute("SELECT * FROM alert_card WHERE id=?", (card_id,)).fetchone()
            if not row or row["status"] not in {"prepared", "failed"} or row["attempts"] >= 3:
                return None
            if hashlib.sha256(row["card_json"].encode()).hexdigest() != row["payload_sha256"]:
                self.db.execute("UPDATE alert_card SET status='unknown',error='payload_hash_mismatch',updated_at=? WHERE id=?", (stamp(), card_id))
                return None
            self.db.execute("UPDATE alert_card SET status='sending',attempts=attempts+1,updated_at=? WHERE id=?", (stamp(), card_id))
            return dict(row)

    def finish(self, card_id, status, message_id="", error=""):
        with self.db:
            self.db.execute("UPDATE alert_card SET status=?,message_id=?,error=?,updated_at=? WHERE id=? AND status='sending'", (status, message_id, error, stamp(), card_id))

    def receipts(self, day, chat):
        return [dict(row) for row in self.db.execute("SELECT id,status,attempts,message_id,error,request_uuid,created_at,updated_at FROM alert_card WHERE report_date=? AND chat_id=? ORDER BY created_at,rowid", (day, chat))]


def deliver(store, report, chat, sender):
    new = store.prepare(report, chat)
    sent = 0
    for pending in store.pending(report["date"], chat):
        row = store.claim(pending["id"])
        if not row:
            continue
        try:
            receipt = sender(json.loads(row["card_json"]), row["request_uuid"])
            if not receipt.get("message_id"):
                raise DeliveryUnknown("missing_message_id")
        except DefiniteFailure as exc:
            store.finish(row["id"], "failed", error=type(exc).__name__)
        except Exception as exc:
            # Includes transport exceptions and unexpected errors after claim.
            store.finish(row["id"], "unknown", error=type(exc).__name__)
        else:
            store.finish(row["id"], "sent", message_id=receipt["message_id"])
            sent += 1
    receipts = store.receipts(report["date"], chat)
    return dict(new_events=new, sent_cards=sent, receipts=receipts,
                held_cards=sum(r["status"] == "unknown" for r in receipts),
                failed_cards=sum(r["status"] == "failed" for r in receipts))
