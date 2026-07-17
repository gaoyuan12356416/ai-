"""UTC storage and fixed UTC+8 presentation helpers for ad-control V3.

Internal persistence remains UTC so idempotency and cross-account scheduling do
not depend on a machine timezone.  API presentation, audit-day filters and copy
name suffixes use the fixed China business timezone (UTC+8).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Mapping, Optional, Tuple


DISPLAY_TIMEZONE_LABEL = "UTC+8"
DISPLAY_IANA_TIMEZONE = "Asia/Shanghai"
DISPLAY_TIMEZONE = timezone(timedelta(hours=8), DISPLAY_TIMEZONE_LABEL)
UTC_STORAGE_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
COPY_NAME_MAX_LENGTH = 255

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AUDIT_TIME_FIELDS = frozenset(
    {
        "at",
        "timestamp",
        "evaluation_time",
        "scheduled_for",
        "ran_at",
    }
)


def aware_utc(value: Optional[datetime] = None) -> datetime:
    """Return an aware UTC datetime; naive input is canonical UTC storage."""

    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def parse_utc_storage(value: Any) -> Optional[datetime]:
    """Parse an internal timestamp, treating a naive value as UTC.

    Date-only values are intentionally not timestamps and are left untouched by
    presentation conversion.
    """

    if isinstance(value, datetime):
        return aware_utc(value)
    text = str(value or "").strip()
    if not text or _DATE_ONLY.fullmatch(text):
        return None
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    normalized = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", normalized)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return aware_utc(parsed)


def utc_storage_text(value: Optional[datetime] = None) -> str:
    return aware_utc(value).strftime(UTC_STORAGE_FORMAT)


def utc8_iso_text(value: Any = None) -> str:
    parsed = parse_utc_storage(value if value is not None else datetime.now(timezone.utc))
    if parsed is None:
        return str(value or "")
    localized = parsed.astimezone(DISPLAY_TIMEZONE)
    return localized.isoformat(timespec="microseconds" if localized.microsecond else "seconds")


def utc8_copy_suffix(value: Optional[datetime] = None) -> str:
    localized = aware_utc(value).astimezone(DISPLAY_TIMEZONE)
    return "[*copybyAI*%s]" % localized.strftime("%m%d%H%M")


def copied_object_name(source_name: Any, suffix: str, *, max_length: int = COPY_NAME_MAX_LENGTH) -> str:
    """Append the complete AI-copy suffix, truncating only the source portion."""

    normalized_suffix = str(suffix or "").strip()
    if not normalized_suffix:
        raise ValueError("copy suffix is required")
    if max_length < len(normalized_suffix):
        raise ValueError("copy name limit is shorter than the required suffix")
    base = str(source_name or "").strip() or "AI Copy"
    return base[: max_length - len(normalized_suffix)] + normalized_suffix


def utc8_date_bounds(date_from: Any = None, date_to: Any = None) -> Tuple[Optional[str], Optional[str]]:
    """Convert inclusive UTC+8 calendar dates to UTC storage query bounds."""

    start_utc: Optional[str] = None
    end_utc: Optional[str] = None
    if date_from:
        first = date.fromisoformat(str(date_from))
        start = datetime.combine(first, time.min, tzinfo=DISPLAY_TIMEZONE)
        start_utc = start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if date_to:
        last_exclusive = date.fromisoformat(str(date_to)) + timedelta(days=1)
        end = datetime.combine(last_exclusive, time.min, tzinfo=DISPLAY_TIMEZONE)
        end_utc = end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return start_utc, end_utc


def utc8_business_date(value: Any) -> Optional[str]:
    parsed = parse_utc_storage(value)
    if parsed is None:
        return None
    return parsed.astimezone(DISPLAY_TIMEZONE).strftime("%Y-%m-%d")


def _is_audit_time_field(key: str) -> bool:
    return key.endswith("_at") or key in _AUDIT_TIME_FIELDS


def convert_audit_times(value: Any, *, field_name: str = "") -> Any:
    """Recursively expose V3 audit timestamps as explicit UTC+8 ISO strings."""

    if isinstance(value, datetime):
        return utc8_iso_text(value)
    if isinstance(value, Mapping):
        result: Dict[Any, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _is_audit_time_field(name) and isinstance(item, (str, datetime)):
                converted = utc8_iso_text(item)
                result[key] = converted if converted else item
            else:
                result[key] = convert_audit_times(item, field_name=name)
        return result
    if isinstance(value, list):
        return [convert_audit_times(item, field_name=field_name) for item in value]
    if isinstance(value, tuple):
        return tuple(convert_audit_times(item, field_name=field_name) for item in value)
    if field_name and _is_audit_time_field(field_name) and isinstance(value, str):
        converted = utc8_iso_text(value)
        return converted if converted else value
    return value
