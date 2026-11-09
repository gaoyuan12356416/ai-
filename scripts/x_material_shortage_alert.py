#!/usr/bin/env python3
"""Consume shortage events independently; --send is the only delivery mode."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_material_alert import AlertStore, build_card, read_journal


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
    if not args.send:
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
            if args.announce:
                summary = {"queued": store.announce(config)}
            else:
                summary = store.ingest(read_journal(config, store.cursor()), config)
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
