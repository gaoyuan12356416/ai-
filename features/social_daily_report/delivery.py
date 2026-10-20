"""Frozen multipart receipts sharing the existing date/chat delivery ledger."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

from features.post_daily_report.delivery import DefiniteFailure, DeliveryStore, DeliveryUnknown

NAMESPACE = "youtube-publisher-daily-report"


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class SocialDeliveryStore(DeliveryStore):
    def __init__(self, path):
        super().__init__(path, namespace=NAMESPACE)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS social_report_batch (
                report_date TEXT NOT NULL, chat_id TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, report_json TEXT NOT NULL,
                cards_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(report_date, chat_id));
            CREATE TABLE IF NOT EXISTS social_report_part (
                report_date TEXT NOT NULL, chat_id TEXT NOT NULL, part_number INTEGER NOT NULL,
                payload_sha256 TEXT NOT NULL, request_uuid TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                message_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL, PRIMARY KEY(report_date, chat_id, part_number));
        """)
        self.db.commit()

    def batch(self, day, chat_id):
        row = self.db.execute(
            "SELECT * FROM social_report_batch WHERE report_date=? AND chat_id=?", (day, chat_id)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["report"] = json.loads(result.pop("report_json"))
        result["cards"] = json.loads(result.pop("cards_json"))
        if hashlib.sha256(compact(result["cards"]).encode()).hexdigest() != result["payload_sha256"]:
            raise DeliveryUnknown("frozen_batch_hash_mismatch")
        return result

    def freeze(self, day, chat_id, report, cards):
        if not cards:
            raise ValueError("empty_report_batch")
        prior = self.get(day, chat_id)
        if prior and prior["status"] in ("sent", "sending", "unknown"):
            raise DeliveryUnknown("existing_delivery_is_immutable")
        report_json, cards_json = compact(report), compact(cards)
        digest = hashlib.sha256(cards_json.encode()).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.batch(day, chat_id)
            if existing:
                if existing["payload_sha256"] != digest:
                    raise DeliveryUnknown("frozen_batch_payload_changed")
                self.db.rollback()
                return existing
            self.db.execute("INSERT INTO social_report_batch VALUES(?,?,?,?,?,?)",
                            (day, chat_id, digest, report_json, cards_json, datetime.now(timezone.utc).isoformat()))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return self.batch(day, chat_id)

    def part(self, day, chat_id, number):
        row = self.db.execute(
            "SELECT * FROM social_report_part WHERE report_date=? AND chat_id=? AND part_number=?",
            (day, chat_id, number),
        ).fetchone()
        return dict(row) if row else None

    def claim_part(self, day, chat_id, number, card):
        digest = hashlib.sha256(compact(card).encode()).hexdigest()
        request_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "%s/%s/%s/part/%d/%s" % (
            NAMESPACE, day, chat_id, number, digest)))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.part(day, chat_id, number)
            if row and row["payload_sha256"] != digest:
                raise DeliveryUnknown("fragment_payload_changed")
            if row and row["status"] == "sent":
                self.db.rollback()
                return None
            if row and row["status"] in ("sending", "unknown"):
                raise DeliveryUnknown("prior_fragment_unconfirmed")
            if row and row["attempts"] >= 3:
                raise DefiniteFailure("fragment_retry_limit")
            self.db.execute("""
                INSERT INTO social_report_part(
                    report_date,chat_id,part_number,payload_sha256,request_uuid,status,attempts,updated_at)
                VALUES(?,?,?,?,?,'sending',1,?)
                ON CONFLICT(report_date,chat_id,part_number) DO UPDATE SET
                    status='sending', attempts=social_report_part.attempts+1, updated_at=excluded.updated_at
            """, (day, chat_id, number, digest, request_uuid, datetime.now(timezone.utc).isoformat()))
            self.db.commit()
            return request_uuid
        except BaseException:
            self.db.rollback()
            raise

    def finish_part(self, day, chat_id, number, status, message_id="", detail=""):
        self.db.execute("""
            UPDATE social_report_part SET status=?,message_id=?,detail=?,updated_at=?
            WHERE report_date=? AND chat_id=? AND part_number=?
        """, (status, message_id, detail, datetime.now(timezone.utc).isoformat(), day, chat_id, number))
        self.db.commit()

    def receipts(self, day, chat_id):
        return [dict(row) for row in self.db.execute(
            "SELECT part_number,status,message_id,request_uuid,attempts,payload_sha256 "
            "FROM social_report_part WHERE report_date=? AND chat_id=? ORDER BY part_number",
            (day, chat_id),
        )]


def deliver(store, day, chat_id, sender, *, sleep=time.sleep):
    """Resume only definite failures; never resend a confirmed/unknown fragment."""
    prior = store.get(day, chat_id)
    if prior and prior["status"] == "sent":
        return {"status": "already_sent", "message_id": prior["message_id"]}
    if prior and prior["status"] in ("sending", "unknown"):
        raise DeliveryUnknown("prior_delivery_unconfirmed")
    batch = store.batch(day, chat_id)
    if not batch:
        raise ValueError("report_batch_not_frozen")
    if store.claim(day, chat_id) is None:
        return {"status": "already_sent"}
    try:
        for number, card in enumerate(batch["cards"], 1):
            for attempt in range(3):
                request_uuid = store.claim_part(day, chat_id, number, card)
                if request_uuid is None:
                    break
                try:
                    receipt = sender(card, request_uuid)
                    if not receipt.get("message_id"):
                        raise DeliveryUnknown("fragment_success_without_message_id")
                except DefiniteFailure as exc:
                    store.finish_part(day, chat_id, number, "failed", detail=str(exc))
                    if store.part(day, chat_id, number)["attempts"] >= 3:
                        raise
                    sleep(2 * (attempt + 1))
                except Exception:
                    store.finish_part(day, chat_id, number, "unknown", detail="delivery_requires_reconciliation")
                    raise DeliveryUnknown("delivery_requires_reconciliation") from None
                else:
                    store.finish_part(day, chat_id, number, "sent", receipt["message_id"])
                    break
        receipts = store.receipts(day, chat_id)
        if len(receipts) != len(batch["cards"]) or any(row["status"] != "sent" for row in receipts):
            raise DeliveryUnknown("incomplete_fragment_receipts")
        store.finish(day, chat_id, "sent", receipts[0]["message_id"],
                     detail="social_report_v3_parts=%d" % len(receipts))
        return {"status": "sent", "message_id": receipts[0]["message_id"], "parts": receipts}
    except DefiniteFailure as exc:
        store.finish(day, chat_id, "failed", detail=str(exc))
        raise
    except Exception:
        store.finish(day, chat_id, "unknown", detail="delivery_requires_reconciliation")
        raise DeliveryUnknown("delivery_requires_reconciliation") from None
