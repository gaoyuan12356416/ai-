#!/usr/bin/env python3
"""Send the unified social report through the existing 11:00 daily ledger."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.post_daily_report.delivery import DefiniteFailure, DeliveryUnknown, send_card
from features.social_daily_report.collector import (
    BEIJING, DB_PATH, ENV_PATH, X_DB_PATH, X_PROFILE_DB_PATH, FB_DB_PATH, FACEBOOK_ATTRIBUTION_DB_PATH, collect,
)
from features.social_daily_report.delivery import SocialDeliveryStore, deliver
from features.social_daily_report.report import build_cards, payload_size, preview_html, require_complete
from features.social_daily_report import prepared
from features.social_analytics.report import ReportError
from scripts.post_daily_report import ensure_storage, atomic_write

CHAT_ID = "oc_7c683c5770aef2e6c84a456e52cad389"


def collection_failure(state, day, reason, **details):
    failure = dict(status="collection_failed", date=day, reason=reason,
                   checked_at=datetime.now(BEIJING).isoformat(), **details)
    folder = state / "failures" / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (datetime.now(BEIJING).strftime("%H%M%S-%f") + ".json")
    atomic_write(path, json.dumps(failure, ensure_ascii=False, indent=2))
    print(json.dumps(dict(status="collection_failed", date=day, reason=reason,
                          diagnostic_path=str(path)), ensure_ascii=False), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="UTC report date YYYY-MM-DD, default yesterday")
    parser.add_argument("--state-dir", default="/mnt/data-disk/youtube-publisher-daily-report")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--x-db", default=X_DB_PATH)
    parser.add_argument("--x-profile-db", default=X_PROFILE_DB_PATH)
    parser.add_argument("--fb-db", default=FB_DB_PATH)
    parser.add_argument("--facebook-attribution-db", default=FACEBOOK_ATTRIBUTION_DB_PATH)
    parser.add_argument("--env-file", default=ENV_PATH)
    parser.add_argument("--publication-timezone", choices=["UTC"], default="UTC")
    parser.add_argument("--feishu-config", default="/root/.codex/plugins/feishu/config.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--send", action="store_true")
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--prepare", action="store_true", help="Precompute complete data without claiming or sending")
    args = parser.parse_args(argv)
    now = datetime.now(BEIJING)
    day = date.fromisoformat(args.date or (now.date() - timedelta(days=1)).isoformat()).isoformat()
    cutoff = datetime.fromisoformat(day).replace(tzinfo=BEIJING) + timedelta(days=1, hours=11)
    if args.send and now < cutoff:
        raise RuntimeError("report_11am_cutoff_not_reached")
    os.umask(0o077)
    state = Path(args.state_dir)
    ensure_storage(state)
    store = handle = None
    try:
        if os.name == "posix":
            import fcntl
            # Preparation must never hold up the 11:00 sender. Its last complete
            # snapshot remains readable while another preparation is in flight.
            handle = (state / ("prepare.lock" if args.prepare else "run.lock")).open("a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({"status": "already_running"}))
                return 0
        batch = None
        if args.send:
            store = SocialDeliveryStore(state / "delivery.sqlite3")
            prior = store.get(day, CHAT_ID)
            if prior and prior["status"] == "sent":
                print(json.dumps(dict(status="already_sent", date=day, message_id=prior["message_id"])))
                return 0
            if prior and prior["status"] in ("sending", "unknown"):
                raise DeliveryUnknown("prior_delivery_unconfirmed")
            if day != (now.date() - timedelta(days=1)).isoformat():
                raise RuntimeError("historical_delivery_not_enabled")
            batch = store.batch(day, CHAT_ID)
        if batch:
            report, cards = batch["report"], batch["cards"]
        else:
            report = None
            if args.send:
                try:
                    report = prepared.load(state, day, CHAT_ID)
                except (OSError, ValueError, KeyError, TypeError):
                    print(json.dumps(dict(status="prepared_unavailable", date=day)), flush=True)
                if report is not None:
                    print(json.dumps(dict(status="using_prepared", date=day,
                                          prepared_at=report.get("prepared_at"))), flush=True)
            if report is None:
                # Preparation runs well before delivery and may finish slow,
                # bounded batches without sharing the sender's 120-second budget.
                options = {"query_budget_seconds": 480} if args.prepare else {}
                try:
                    report = collect(day, args.db, args.env_file, args.x_db,
                                     args.fb_db, args.facebook_attribution_db, args.x_profile_db, **options)
                except Exception as exc:
                    # Collection has not claimed delivery. A source-level 503
                    # must reach the same bounded retry path as partial quality.
                    # Never expose arbitrary driver/config exception text.
                    safe_reasons = {
                        "AI 后台记录的 campaign ID 用户归属冲突",
                        "AI 后台发布记录缺少冻结来源 ID",
                        "AI 后台统计范围金额不守恒",
                        "日报采集时间预算已用完",
                    }
                    reason = str(exc) if isinstance(exc, ReportError) and str(exc) in safe_reasons else type(exc).__name__
                    retryable = isinstance(exc, ReportError) and exc.status == 503
                    collection_failure(state, day, reason, retryable=retryable,
                                       error_type=type(exc).__name__)
                    if retryable:
                        raise ValueError("daily_report_sources_incomplete") from exc
                    raise
            if args.send or args.prepare:
                try:
                    require_complete(report)
                except ValueError as exc:
                    # Preserve safe source diagnostics before any batch is frozen.
                    collection_failure(state, day, str(exc), quality=report["quality"], retryable=True)
                    raise
            if args.prepare:
                report["prepared_at"] = datetime.now(BEIJING).isoformat()
            cards = build_cards(report, CHAT_ID)
            if args.prepare:
                path = prepared.save(state, day, CHAT_ID, report)
                print(json.dumps(dict(status="prepared", date=day, prepared_at=report["prepared_at"],
                                      path=str(path)), ensure_ascii=False), flush=True)
                return 0
            if store:
                store.freeze(day, CHAT_ID, report, cards)
        folder = state / ("reports" if args.send else "previews") / day
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write(folder / "report.json", json.dumps(report, ensure_ascii=False, indent=2))
        atomic_write(folder / "cards.json", json.dumps(cards, ensure_ascii=False, indent=2))
        atomic_write(folder / "card.json", json.dumps(cards[0], ensure_ascii=False, separators=(",", ":")))
        atomic_write(folder / "preview.html", preview_html(cards))
        print(json.dumps(dict(
            status="ready", date=day, totals=report["totals"], fragments=len(cards),
            card_bytes=[payload_size(card, CHAT_ID) for card in cards], path=str(folder),
            cards_sha256=hashlib.sha256(json.dumps(cards, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        ), ensure_ascii=False), flush=True)
        if not args.send:
            return 0
        require_complete(report)
        receipt = deliver(store, day, CHAT_ID,
                          lambda card, request_uuid: send_card(args.feishu_config, CHAT_ID, card, request_uuid))
        atomic_write(folder / "receipt.json", json.dumps(receipt, ensure_ascii=False, indent=2))
        print(json.dumps(dict(date=day, **receipt), ensure_ascii=False), flush=True)
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
