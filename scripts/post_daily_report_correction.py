#!/usr/bin/env python3
"""Explicit, deduplicated TT correction of an already sent daily report."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.post_daily_report.common import Window, read_db
from features.post_daily_report.delivery import DeliveryStore, DefiniteFailure, DeliveryUnknown, send_card
from features.post_daily_report.report import build_card, build_compact_card, preview_html, total, validate_channel
from features.post_daily_report.tt import collect as collect_tt
from scripts.post_daily_report import CHAT_ID, DEFAULT_PATHS, atomic_write, ensure_storage


def corrected_report(state, chat_id, paths, window, revision):
    with read_db(state / "delivery.sqlite3") as db:
        original = db.execute("SELECT status,message_id FROM delivery WHERE report_date=? AND chat_id=?",
                              (window.date, chat_id)).fetchone()
    if not original or original["status"] != "sent" or not original["message_id"]:
        raise RuntimeError("correction_requires_confirmed_original_delivery")
    folder = state / "reports" / window.date
    raw = (folder / "report.json").read_bytes()
    report = json.loads(raw)
    receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
    if receipt.get("message_id") != original["message_id"]:
        raise RuntimeError("original_receipt_mismatch")
    if report.get("date") != window.date or report.get("cutoff_utc") != window.cutoff.isoformat():
        raise RuntimeError("original_window_mismatch")
    if [c.get("channel") for c in report.get("channels", [])] != ["TT", "FB", "X"]:
        raise RuntimeError("original_channel_layout_mismatch")
    replacement = collect_tt(paths, window)
    validate_channel(replacement)
    if replacement.get("channel") != "TT" or replacement.get("error") or not replacement.get("rows"):
        raise RuntimeError("tt_correction_unavailable")
    if any(r.get("expected") is None or not r.get("data_available", True) for r in replacement["rows"]):
        raise RuntimeError("tt_correction_incomplete")
    result = copy.deepcopy(report)
    result["channels"][0] = replacement
    result["correction"] = {
        "channel": "TT", "revision": revision,
        "original_message_id": original["message_id"],
        "original_report_sha256": hashlib.sha256(raw).hexdigest(),
        "original_generated_at_utc": report.get("generated_at_utc"),
    }
    result["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def correction_card(report, compact=False):
    card = (build_compact_card if compact else build_card)(report)
    card["header"]["title"]["content"] += "（TT 修正版）"
    card["elements"].insert(1, {"tag": "div", "text": {"tag": "lark_md", "content":
        "**更正说明：TT 统计已修复，请以本条 TT 数据为准。**\n"
        "统计日和次日 10:00 截止口径保持不变；FB、X 数据沿用原日报。"}})
    return card


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Original Beijing report date, not the sending date")
    parser.add_argument("--revision", required=True, help="Stable correction ID; repeat calls are deduplicated")
    parser.add_argument("--state-dir", default="/mnt/data-disk/post-daily-report")
    parser.add_argument("--paths-json", help="Optional publisher paths for offline fixtures")
    parser.add_argument("--feishu-config", default="/root/.codex/plugins/feishu/config.json")
    parser.add_argument("--chat-id", default=CHAT_ID)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--send", action="store_true")
    mode.add_argument("--preview", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.revision):
        parser.error("revision must be a safe 1-64 character identifier")
    window = Window.for_date(args.date)
    if window.date != args.date:
        parser.error("date must be YYYY-MM-DD")
    if args.send and datetime.now(timezone.utc) < window.cutoff:
        raise RuntimeError("report_cutoff_not_reached")
    os.umask(0o077)
    state = Path(args.state_dir)
    ensure_storage(state)
    lock_handle, store = None, None
    try:
        if os.name == "posix":
            import fcntl
            lock_handle = (state / "run.lock").open("a")
            try:
                fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({"status": "already_running"}))
                return 0
        formal = state / "corrections" / window.date / args.revision
        delivery_key = window.date + "/TT-correction/" + args.revision
        if args.send:
            formal.mkdir(parents=True, exist_ok=True)
            store = DeliveryStore(formal / "delivery.sqlite3")
            prior = store.get(delivery_key, args.chat_id)
            if prior and prior["status"] == "sent":
                print(json.dumps({"status": "already_sent", "date": window.date,
                                  "revision": args.revision, "message_id": prior["message_id"]}))
                return 0
            if prior and prior["status"] in ("sending", "unknown"):
                raise DeliveryUnknown("prior_correction_unconfirmed")
        paths = dict(DEFAULT_PATHS)
        if args.paths_json:
            paths.update(json.loads(Path(args.paths_json).read_text(encoding="utf-8")))
        report = corrected_report(state, args.chat_id, paths, window, args.revision)
        for compact in (False, True):
            card = correction_card(report, compact)
            card_json = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
            payload_size = len(json.dumps({"receive_id": args.chat_id, "msg_type": "interactive",
                "content": card_json, "uuid": "0" * 36}, ensure_ascii=False).encode("utf-8"))
            if payload_size <= 29000:
                break
        else:
            raise RuntimeError("report_card_exceeds_safe_payload_limit")
        folder = formal if args.send else state / "previews" / window.date / "corrections" / args.revision
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write(folder / "report.json", json.dumps(report, ensure_ascii=False, indent=2))
        atomic_write(folder / "card.json", card_json)
        atomic_write(folder / "preview.html", preview_html(report, card))
        print(json.dumps({"status": "correction_ready", "date": window.date, "revision": args.revision,
            "tt": total(report["channels"][0]["rows"]), "card_bytes": payload_size,
            "card_sha256": hashlib.sha256(card_json.encode()).hexdigest(), "path": str(folder)}, ensure_ascii=False))
        if not args.send:
            return 0
        request_uuid = store.claim(delivery_key, args.chat_id)
        if request_uuid is None:
            return 0
        try:
            receipt = send_card(args.feishu_config, args.chat_id, card, request_uuid)
        except DefiniteFailure as exc:
            store.finish(delivery_key, args.chat_id, "failed", detail=str(exc))
            raise
        except Exception as exc:
            store.finish(delivery_key, args.chat_id, "unknown", detail=type(exc).__name__)
            raise DeliveryUnknown("correction_delivery_requires_reconciliation") from None
        store.finish(delivery_key, args.chat_id, "sent", receipt["message_id"])
        atomic_write(folder / "receipt.json", json.dumps(receipt, ensure_ascii=False, indent=2))
        print(json.dumps({"status": "sent", "date": window.date, "revision": args.revision, **receipt}))
        return 0
    finally:
        if store:
            store.close()
        if lock_handle:
            lock_handle.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "failed", "reason": str(exc) if isinstance(exc, (DefiniteFailure, DeliveryUnknown))
                          else type(exc).__name__}), file=sys.stderr)
        raise SystemExit(2)
