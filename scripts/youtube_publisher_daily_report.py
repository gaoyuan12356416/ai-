#!/usr/bin/env python3
"""Send a deduplicated YouTube publisher report daily at 11:00 Beijing."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.post_daily_report.delivery import DeliveryStore, DefiniteFailure, DeliveryUnknown, send_card
from features.youtube_daily_report.collector import BEIJING, DB_PATH, ENV_PATH, collect
from features.youtube_daily_report.report import build_card, preview_html
from scripts.post_daily_report import ensure_storage, atomic_write

CHAT_ID = "oc_7c683c5770aef2e6c84a456e52cad389"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Report date YYYY-MM-DD, default yesterday")
    parser.add_argument("--state-dir", default="/mnt/data-disk/youtube-publisher-daily-report")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--env-file", default=ENV_PATH)
    parser.add_argument("--publication-timezone", choices=["Asia/Shanghai", "UTC"], default="Asia/Shanghai")
    parser.add_argument("--feishu-config", default="/root/.codex/plugins/feishu/config.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--send", action="store_true")
    mode.add_argument("--preview", action="store_true")
    args = parser.parse_args(argv)
    now = datetime.now(BEIJING)
    day = args.date or (now.date() - timedelta(days=1)).isoformat()
    cutoff = datetime.fromisoformat(day).replace(tzinfo=BEIJING) + timedelta(days=1, hours=11)
    if args.send and now < cutoff:
        raise RuntimeError("report_11am_cutoff_not_reached")
    os.umask(0o077)
    state = Path(args.state_dir)
    ensure_storage(state)
    handle = None
    store = None
    try:
        if os.name == "posix":
            import fcntl
            handle = (state / "run.lock").open("a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({"status": "already_running"}))
                return 0
        if args.send:
            store = DeliveryStore(state / "delivery.sqlite3", namespace="youtube-publisher-daily-report")
            prior = store.get(day, CHAT_ID)
            if prior and prior["status"] == "sent":
                print(json.dumps(dict(status="already_sent", date=day, message_id=prior["message_id"])))
                return 0
            if prior and prior["status"] in ("sending", "unknown"):
                raise DeliveryUnknown("prior_delivery_unconfirmed")
        report = collect(day, args.db, args.env_file, args.publication_timezone)
        card = build_card(report)
        card_json = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        payload_size = len(json.dumps(dict(receive_id=CHAT_ID, msg_type="interactive", content=card_json,
            uuid="0"*36), ensure_ascii=False).encode("utf-8"))
        if payload_size > 29000:
            raise RuntimeError("report_card_exceeds_safe_payload_limit")
        folder = state / ("reports" if args.send else "previews") / day
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write(folder / "report.json", json.dumps(report, ensure_ascii=False, indent=2))
        atomic_write(folder / "card.json", card_json)
        atomic_write(folder / "preview.html", preview_html(card))
        print(json.dumps(dict(status="ready", date=day, totals=report["totals"],
            metrics_available=report["metrics_available"], unmatched_campaigns=report["unmatched"]["campaigns"],
            card_sha256=hashlib.sha256(card_json.encode()).hexdigest(), card_bytes=payload_size,
            path=str(folder)), ensure_ascii=False), flush=True)
        if not args.send:
            return 0
        for attempt in range(3):
            request_uuid = store.claim(day, CHAT_ID)
            if request_uuid is None:
                return 0
            try:
                receipt = send_card(args.feishu_config, CHAT_ID, card, request_uuid)
            except DefiniteFailure as exc:
                store.finish(day, CHAT_ID, "failed", detail=str(exc))
                if attempt < 2 and store.get(day, CHAT_ID)["attempts"] < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
            except Exception as exc:
                store.finish(day, CHAT_ID, "unknown", detail=type(exc).__name__)
                raise DeliveryUnknown("delivery_requires_reconciliation") from None
            else:
                store.finish(day, CHAT_ID, "sent", receipt["message_id"])
                atomic_write(folder / "receipt.json", json.dumps(receipt, ensure_ascii=False, indent=2))
                print(json.dumps(dict(status="sent", date=day, **receipt)), flush=True)
                return 0
    finally:
        if store:
            store.close()
        if handle:
            handle.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DefiniteFailure, DeliveryUnknown) as exc:
        print(json.dumps(dict(status="failed", reason=str(exc))), file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(json.dumps(dict(status="failed", reason=type(exc).__name__)), file=sys.stderr)
        raise SystemExit(1)
