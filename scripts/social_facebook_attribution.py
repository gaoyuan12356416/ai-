#!/usr/bin/env python3
"""Collect report-only FB attribution once; sources are always read-only."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.social_analytics.facebook import (
    AttributionError, DEFAULT_FB_DB, DEFAULT_USERS_DB, DEFAULT_LEDGER, collect,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fb-db", default=DEFAULT_FB_DB)
    parser.add_argument("--users-db", default=DEFAULT_USERS_DB)
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    parser.add_argument("--lock")
    args = parser.parse_args(argv)
    try:
        result = collect(args.fb_db, args.users_db, args.ledger, mode=args.mode, lock_path=args.lock)
    except Exception as exc:
        result = dict(status="failed", error=exc.code if isinstance(exc, AttributionError) else "collection_failed")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
