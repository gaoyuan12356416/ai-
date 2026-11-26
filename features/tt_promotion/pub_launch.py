"""TT promotion's inclusive elapsed-day window and release-aware native adapter."""

from dataclasses import replace
from datetime import datetime, timezone
import re

from .pub_store import iso


def normalize(value):
    """Canonical form used by X templates: 0/N or signed inclusive A~B."""
    text = str(value).strip().replace("～", "~")
    for left, right in (("[", "]"), ("【", "】")):
        if text.startswith(left) and text.endswith(right):
            text = text[1:-1].strip()
            break
    m = re.fullmatch(r"(-?\d+)\s*~\s*(-?\d+)", text)
    if m and len(text) < 64:
        lo, hi = map(int, m.groups())
        if -3650 <= lo <= hi <= 3650:
            return f"{lo}~{hi}"
    elif re.fullmatch(r"\d{1,10}", text) and int(text) <= 3650:
        return int(text)
    raise ValueError("上线窗口请填 0、正整数天数或下限~上限（如 -1~0），上下限须在 -3650 至 3650 天内")


def bounds(window, anchor):
    epoch = int(anchor.timestamp())
    if isinstance(window, str):
        low, high = map(int, window.split("~"))
        return max(1, epoch - high * 86400), epoch - low * 86400
    return (max(1, epoch - window * 86400) if window else 1), epoch


def native_config(config):
    """Native TT's rule parser accepts integer days and two built-in sort keys."""
    result = dict(config)
    if isinstance(normalize(config.get("drama_launch_window_days", 0)), str):
        result["drama_launch_window_days"] = 0
    for key in ("drama_rule", "material_rule"):
        rule = config.get(key) or {}
        if str(rule.get("sort_by", "")).startswith("x_"):
            result[key] = {**rule, "sort_by": "roas"}
    return result


class NativeSelectStore:
    """Scope native template projection to one select call; preserve all store semantics."""

    def __init__(self, store):
        self.store = store

    def __getattr__(self, name):
        return getattr(self.store, name)

    def get_template(self, *args, **kwargs):
        template = self.store.get_template(*args, **kwargs)
        return replace(template, config=native_config(template.config))


def times(at, release):
    if not release:
        return at, None
    release_at = iso(datetime.fromtimestamp(int(release), timezone.utc))
    return max(at, release_at), release_at


def current_release(source, content, language):
    """Read only the originally selected exact identity before a deferred platform submission."""
    rows = source._stream_select(
        f"""SELECT MAX(deploy_time) AS release_at FROM `{source.schema}`.ads_drama_info
        WHERE app_id=%s AND content_id=%s AND BINARY content_id=BINARY %s
        AND language=%s AND release_status=1 AND deploy_time>0""",
        (source.app_id, content, content, language),
    )
    return next((int(r["release_at"]) for r in rows if r.get("release_at")), None)
