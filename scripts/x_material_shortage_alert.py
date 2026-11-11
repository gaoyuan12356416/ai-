#!/usr/bin/env python3
"""Daily language inventory reminder; publisher state is strictly read-only."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_material_alert import AlertStore, build_card, read_journal


def daily_report(config):
    import features
    runtime = Path(config.get("publisher_runtime", "/opt/x-post-automation/current")).resolve()
    if str(runtime / "features") not in features.__path__:
        features.__path__.append(str(runtime / "features"))
    from features.x_material_inventory import collect
    return collect(config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--announce", action="store_true")
    parser.add_argument("--event-file", help="Offline card preview only")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.event_file:
        if args.send:
            parser.error("event-file is preview-only")
        print(json.dumps(build_card(json.loads(Path(args.event_file).read_text(encoding="utf-8")), config["mention_id"]), ensure_ascii=False))
        return
    daily = config.get("mode") == "daily_inventory"
    if daily and args.announce:
        parser.error("daily inventory only sends the scheduled summary")
    if daily and args.send:
        now = datetime.now(timezone(timedelta(hours=8)))
        if (now.hour, now.minute) != (10, 30):
            print(json.dumps({"status": "outside_daily_window"}))
            return
    if not args.send:
        if daily:
            report = daily_report(config)
            card = build_card(report, config["mention_id"]) if any(r["alert"] for r in report["languages"]) else None
            print(json.dumps({"status": "preview", "report": report, "card": card}, ensure_ascii=False))
        else:
            print(json.dumps({"status": "preview", "events": len(read_journal(config))}))
        return
    os.umask(0o077)
    state = Path(config["state_dir"])
    if state.resolve().is_relative_to(Path("/mnt/data-disk")):
        disk_uuid = subprocess.check_output(["findmnt", "-rn", "-o", "UUID", "--mountpoint", "/mnt/data-disk"], text=True).strip()
        if disk_uuid != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8":
            raise RuntimeError("alert_data_disk_unavailable")
    state.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (state / "send.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "already_running"}))
            return
        store = AlertStore(state / "delivery.sqlite3")
        try:
            if daily:
                key = "x-material-inventory-v2/" + now.date().isoformat()
                prior = store.get(key, config["chat_id"])
                if prior and prior["status"] in ("sent", "sending", "unknown"):
                    print(json.dumps({"status": "already_handled", "delivery_status": prior["status"]}))
                    return
                report = daily_report(config)
                # A slow source read must not send a late or previous-day report.
                completed = datetime.now(timezone(timedelta(hours=8)))
                if completed.date() != now.date() or (completed.hour, completed.minute) != (10, 30):
                    print(json.dumps({"status": "daily_window_elapsed"}))
                    return
                key = store.enqueue_daily(report, config)
                summary = {"report_date": report["report_date"], "languages": report["languages"]}
                summary["deliveries"] = store.deliver(config, only_key=key)
            elif args.announce:
                summary = {"queued": store.announce(config)}
            else:
                summary = store.ingest(read_journal(config, store.cursor()), config)
            if not daily:
                summary["deliveries"] = store.deliver(config)
            summary["delivery_states"] = {row[0]: row[1] for row in store.db.execute(
                "SELECT status,count(*) FROM delivery GROUP BY status")}
            print(json.dumps(summary, ensure_ascii=False))
        finally:
            store.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}), file=sys.stderr)
        sys.exit(1)
