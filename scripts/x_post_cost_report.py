#!/usr/bin/env python3
"""Estimate confirmed X publishing writes for a Beijing calendar month."""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.post_daily_report.common import Window
from features.post_daily_report.x_cost import collect_cost
from scripts.post_daily_report import DEFAULT_PATHS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", required=True, help="YYYY-MM, Beijing time")
    parser.add_argument("--paths-json")
    args = parser.parse_args()
    day = datetime.strptime(args.month, "%Y-%m").date().replace(day=1)
    if day.strftime("%Y-%m") != args.month:
        parser.error("month must be YYYY-MM")
    next_day = day.replace(year=day.year + 1, month=1) if day.month == 12 else day.replace(month=day.month + 1)
    start, end = Window.for_date(day.isoformat()).start, Window.for_date(next_day.isoformat()).start
    paths = dict(DEFAULT_PATHS)
    if args.paths_json:
        paths.update(json.loads(Path(args.paths_json).read_text(encoding="utf-8")))
    print(json.dumps({"month": args.month, "timezone": "Asia/Shanghai", "start_utc": start.isoformat(),
        "end_utc_exclusive": end.isoformat(), "scope": "AI后台 X 主发布及 Auto 桥接台账确认成功的发布操作", "cost": collect_cost(paths, start, end)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
