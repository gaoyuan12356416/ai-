from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))


def parse_time(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    # Publisher SQLite naive timestamps are UTC; callers must explicitly attach
    # Beijing time for local schedule strings.
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def in_window(value, start, end):
    dt = parse_time(value)
    return dt is not None and start <= dt < end


def json_value(value, default=None):
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(value) if value else default
    except (ValueError, TypeError):
        return default


@contextmanager
def read_db(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA busy_timeout=2000")
    deadline = time.monotonic() + 25
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 20000)
    try:
        db.execute("BEGIN")
        yield db
    finally:
        db.close()


@dataclass(frozen=True)
class Window:
    date: str
    start: datetime
    end: datetime
    cutoff: datetime

    @classmethod
    def for_date(cls, value):
        d = date.fromisoformat(value)
        start = datetime.combine(d, day_time(), BEIJING)
        end = start + timedelta(days=1)
        return cls(d.isoformat(), start.astimezone(UTC), end.astimezone(UTC),
                   (end + timedelta(hours=10)).astimezone(UTC))

    def local_slot(self, value):
        return datetime.fromisoformat(self.date + "T" + value).replace(tzinfo=BEIJING).astimezone(UTC)
