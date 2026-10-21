#!/usr/bin/env python3
"""Explicit, ledger-protected private review delivery; never called by a timer."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.post_daily_report.delivery import DeliveryUnknown, send_card
from features.social_daily_report.collector import DB_PATH
from features.social_daily_report.delivery import SocialDeliveryStore, deliver
from features.social_daily_report.report import build_cards, preview_html, require_complete
from scripts.post_daily_report import atomic_write, ensure_storage


def recipient(db_path, user_id):
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT name,open_id FROM drama_admin_user WHERE user_id=? LIMIT 2", (user_id,)).fetchall()
        if len(rows) != 1 or not re.fullmatch(r"ou_[a-zA-Z0-9]+", rows[0][1] or ""):
            raise ValueError("private_recipient_not_unique")
        return rows[0]
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-json", required=True)
    parser.add_argument("--recipient-user-id", required=True)
    parser.add_argument("--preview-id", required=True)
    parser.add_argument("--state-dir", default="/mnt/data-disk/youtube-publisher-daily-report")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--feishu-config", default="/root/.codex/plugins/feishu/config.json")
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", args.preview_id):
        raise ValueError("invalid_preview_id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", args.recipient_user_id):
        raise ValueError("invalid_recipient_user_id")
    name, open_id = recipient(args.db, args.recipient_user_id)
    report = json.loads(Path(args.report_json).read_text(encoding="utf-8"))
    report = report.get("report", report)
    require_complete(report)
    day = report["report_date"]
    cards = build_cards(report, open_id)
    state = Path(args.state_dir)
    ensure_storage(state)
    os.umask(0o077)
    folder = state / "private-previews" / args.preview_id / args.recipient_user_id
    folder.mkdir(parents=True, exist_ok=True)
    store = handle = None
    try:
        if os.name == "posix":
            import fcntl
            handle = (state / "run.lock").open("a")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        key = open_id + "/" + args.preview_id
        if args.send:
            store = SocialDeliveryStore(folder / "delivery.sqlite3")
            prior = store.get(day, key)
            if prior and prior["status"] == "sent":
                print(json.dumps(dict(status="already_sent", message_id=prior["message_id"])))
                return 0
            if prior and prior["status"] in ("sending", "unknown"):
                raise DeliveryUnknown("prior_private_delivery_unconfirmed")
            batch = store.batch(day, key)
            if batch:
                report, cards = batch["report"], batch["cards"]
                require_complete(report)
            else:
                store.freeze(day, key, report, cards)
        for filename, value in (("report.json", report), ("cards.json", cards)):
            atomic_write(folder / filename, json.dumps(value, ensure_ascii=False, indent=2))
        atomic_write(folder / "preview.html", preview_html(cards))
        if args.send:
            receipt = deliver(store, day, key, lambda card, request_uuid: send_card(
                args.feishu_config, open_id, card, request_uuid, receive_id_type="open_id"))
            atomic_write(folder / "receipt.json", json.dumps(receipt, ensure_ascii=False, indent=2))
        else:
            receipt = {"status": "ready"}
        print(json.dumps(dict(recipient=name, path=str(folder), **receipt), ensure_ascii=False))
        return 0
    finally:
        if store:
            store.close()
        if handle:
            handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
