"""Render the daily report as a self-contained, safely embedded HTML document."""

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit


DEFAULT_HOME_URL = (
    "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/"
    "codex-artifacts/html/dramawave-ai-image-daily/index.html"
)


def _safe_url(value: Any) -> str:
    """Only public HTTP(S) URL shapes may reach links or image attributes."""
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        return value
    except ValueError:
        return ""


def _sanitize_urls(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _safe_url(item) if key == "url" or key.endswith("_url")
            else _sanitize_urls(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_urls(item) for item in value]
    return value


def render_report(
    report: Dict[str, Any], history: Optional[List[Dict[str, Any]]] = None
) -> str:
    """Render a validated report; percentages are percentage points, not ratios.

    This function performs no network calls and does not modify its inputs.
    Missing/non-finite data must be resolved by the caller before publishing;
    non-finite numbers deliberately raise ValueError rather than become zero.
    """
    payload = _sanitize_urls(copy.deepcopy(report))
    payload["home_url"] = payload.get("home_url") or DEFAULT_HOME_URL
    payload["history"] = _sanitize_urls(copy.deepcopy(history or []))
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    # Escaping < closes both script-tag and HTML-comment injection avenues.
    serialized = (serialized.replace("&", "\\u0026").replace("<", "\\u003c")
                  .replace(">", "\\u003e").replace("\u2028", "\\u2028")
                  .replace("\u2029", "\\u2029"))
    template = Path(__file__).with_name("report.html").read_text(encoding="utf-8")
    marker = "__REPORT_JSON__"
    if template.count(marker) != 1:
        raise ValueError("Report template must contain exactly one JSON marker")
    return template.replace(marker, serialized)
