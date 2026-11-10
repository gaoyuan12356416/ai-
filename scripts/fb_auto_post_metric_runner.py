#!/usr/bin/env python3
"""Independent single-flight FB metric refresh (never calls GPU or Graph)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from features.fb_auto_posts.core import FBAutoPostStore
from features.fb_auto_posts.metrics import BEIJING, MetricRefresher
from features.fb_auto_posts.repositories import ReadOnlyMySQL


@contextmanager
def single_flight(path: str):
    try: import fcntl
    except ImportError: raise RuntimeError("metric refresh requires fcntl") from None
    target = Path(path); target.parent.mkdir(parents=True, exist_ok=True)
    handle = target.open("a+")
    try:
        try: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError("fb metric refresh already running") from None
        yield
    finally: handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("mode", choices=("yesterday", "repair")); args = parser.parse_args()
    def connect():
        import pymysql
        return pymysql.connect(host=os.environ.get("FB_AUTO_MYSQL_HOST", "127.0.0.1"), port=int(os.environ.get("FB_AUTO_MYSQL_PORT", "63350")), user=os.environ.get("FB_AUTO_MYSQL_USER", ""), password=os.environ.get("FB_AUTO_MYSQL_PASSWORD", ""), database=os.environ.get("FB_AUTO_MYSQL_DATABASE", "kunlunads_dev"), charset="utf8mb4", autocommit=True, connect_timeout=10, read_timeout=180, write_timeout=10)
    mysql = ReadOnlyMySQL(connect, os.environ.get("FB_AUTO_MYSQL_DATABASE", "kunlunads_dev"), os.environ.get("FB_AUTO_BLACKLIST_MYSQL_DATABASE", "ads_setting"))
    store = FBAutoPostStore(os.environ.get("FB_AUTO_METRIC_DB_PATH", "/mnt/data-disk/fb-auto-post-publisher/fb-auto-metric.sqlite3"))
    refresher = MetricRefresher(mysql, store)
    today = datetime.now(timezone.utc).astimezone(BEIJING).date()
    dates = [today - timedelta(days=1)] if args.mode == "yesterday" else [today - timedelta(days=days) for days in range(1, 31)]
    results = []
    with single_flight(os.environ.get("FB_AUTO_METRIC_LOCK_PATH", "/run/lock/fb-auto-post-metric.lock")):
        for day in dates:
            results.append(dict(refresher.refresh_day(day.isoformat())))
    print(json.dumps({"ok": True, "mode": args.mode, "days": len(results), "items": results}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__": raise SystemExit(main())
