"""Complete precomputed reports, separate from the immutable delivery ledger."""
import json
from datetime import datetime
from pathlib import Path

from .report import require_complete
from features.social_analytics.bill_income import FIRST_DAY
from scripts.post_daily_report import atomic_write

PREPARED_VERSION = 2  # Includes TikTok site-wide income and separate unassigned income.


def save(state, day, chat_id, report):
    require_complete(report)
    if report["report_date"] != day:
        raise ValueError("prepared_report_date_mismatch")
    path = Path(state) / "prepared" / day / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(dict(version=PREPARED_VERSION, chat_id=chat_id, report=report),
                                  ensure_ascii=False, indent=2))
    return path


def load(state, day, chat_id):
    path = Path(state) / "prepared" / day / "report.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    report = value["report"]
    if value["chat_id"] != chat_id or report["report_date"] != day:
        raise ValueError("prepared_report_identity_mismatch")
    if datetime.fromisoformat(report["prepared_at"]).tzinfo is None:
        raise ValueError("prepared_report_timestamp_invalid")
    if day >= FIRST_DAY and "iaa_sites" not in report.get("quality", {}).get("billing", {}):
        return None
    if value.get("version") != PREPARED_VERSION:
        return None
    require_complete(report)
    return report
