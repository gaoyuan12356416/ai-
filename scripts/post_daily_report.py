#!/usr/bin/env python3
"""Generate the previous Beijing-day report; sending is explicit and deduplicated."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.post_daily_report.common import BEIJING, Window
from features.post_daily_report.delivery import DeliveryStore, DefiniteFailure, DeliveryUnknown, send_card
from features.post_daily_report.report import collect_report, build_card, build_compact_card, preview_html, total

DEFAULT_PATHS = {
    "tt_auto": "/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3",
    "tt": "/mnt/data-disk/tt-post-publisher/tt-post.sqlite3",
    "fb": "/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3",
    "x": "/var/lib/x-post-automation/accounts.sqlite3",
    "x_auto": "/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3",
}
CHAT_ID = "oc_7c683c5770aef2e6c84a456e52cad389"
DISK_UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"


def ensure_storage(path):
    resolved = path.resolve()
    if str(resolved).startswith("/mnt/data-disk/"):
        actual = subprocess.check_output(["findmnt", "-rn", "-o", "UUID", "--mountpoint", "/mnt/data-disk"], text=True).strip()
        if actual != DISK_UUID:
            raise RuntimeError("report_data_disk_unavailable")
    path.mkdir(parents=True, exist_ok=True)


def atomic_write(path, text):
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Beijing report date YYYY-MM-DD; defaults to yesterday")
    parser.add_argument("--state-dir", default="/mnt/data-disk/post-daily-report")
    parser.add_argument("--paths-json", help="Optional DB paths JSON for offline fixtures")
    parser.add_argument("--feishu-config", default="/root/.codex/plugins/feishu/config.json")
    parser.add_argument("--chat-id", default=CHAT_ID)
    parser.add_argument("--send", action="store_true", help="Send one formal report with durable deduplication")
    parser.add_argument("--preview", action="store_true", help="Explicit no-send preview (default)")
    args = parser.parse_args(argv)
    if args.send and args.preview: parser.error("--send and --preview are mutually exclusive")
    os.umask(0o077)
    report_date = args.date or (datetime.now(BEIJING).date() - timedelta(days=1)).isoformat()
    window = Window.for_date(report_date)
    if args.send and datetime.now(timezone.utc) < window.cutoff:
        raise RuntimeError("report_cutoff_not_reached")
    state = Path(args.state_dir)
    ensure_storage(state)
    lock_handle = None
    if os.name == "posix":
        import fcntl
        lock_handle = (state / "run.lock").open("a")
        try: fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "already_running"}))
            return 0
    store = DeliveryStore(state / "delivery.sqlite3") if args.send else None
    try:
        prior = store.get(report_date, args.chat_id) if store else None
        if prior and prior["status"] == "sent":
            print(json.dumps({"status": "already_sent", "date": report_date, "message_id": prior["message_id"]}))
            return 0
        if prior and prior["status"] in ("sending", "unknown"):
            raise DeliveryUnknown("prior_delivery_unconfirmed")
        paths = dict(DEFAULT_PATHS)
        if args.paths_json:
            paths.update(json.loads(Path(args.paths_json).read_text(encoding="utf-8")))
        report = collect_report(paths, window)
        card = build_card(report)
        card_json = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        # Feishu counts the escaped content string, not merely the card's bytes.
        payload_size = len(json.dumps({"receive_id": args.chat_id, "msg_type": "interactive", "content": card_json, "uuid": "0" * 36}, ensure_ascii=False).encode("utf-8"))
        if payload_size > 29000:
            card = build_compact_card(report)
            card_json = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
            payload_size = len(json.dumps({"receive_id": args.chat_id, "msg_type": "interactive", "content": card_json, "uuid": "0" * 36}, ensure_ascii=False).encode("utf-8"))
            if payload_size > 29000: raise RuntimeError("report_card_exceeds_safe_payload_limit")
        folder = state / ("reports" if args.send else "previews") / report_date
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write(folder / "report.json", json.dumps(report, ensure_ascii=False, indent=2))
        atomic_write(folder / "card.json", card_json)
        atomic_write(folder / "preview.html", preview_html(report, card))
        summary = {c["channel"]: total(c.get("rows", [])) if not c.get("error") else {"error": c["error"]} for c in report["channels"]}
        print(json.dumps({"status": "preview_ready", "date": report_date, "summary": summary, "card_bytes": payload_size, "card_sha256": hashlib.sha256(card_json.encode()).hexdigest(), "path": str(folder)}, ensure_ascii=False))
        if not args.send: return 0
        for attempt in range(3):
            request_uuid = store.claim(report_date, args.chat_id)
            if request_uuid is None: return 0
            try:
                receipt = send_card(args.feishu_config, args.chat_id, card, request_uuid)
            except DefiniteFailure as exc:
                store.finish(report_date, args.chat_id, "failed", detail=str(exc))
                if attempt < 2 and store.get(report_date, args.chat_id)["attempts"] < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
            except Exception as exc:
                store.finish(report_date, args.chat_id, "unknown", detail=type(exc).__name__)
                raise DeliveryUnknown("delivery_requires_reconciliation") from None
            else:
                store.finish(report_date, args.chat_id, "sent", receipt["message_id"])
                atomic_write(folder / "receipt.json", json.dumps(receipt, ensure_ascii=False, indent=2))
                print(json.dumps({"status": "sent", "date": report_date, **receipt}, ensure_ascii=False))
                return 0
    finally:
        if store: store.close()
        if lock_handle: lock_handle.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DefiniteFailure, DeliveryUnknown) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(json.dumps({"status": "failed", "reason": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)
