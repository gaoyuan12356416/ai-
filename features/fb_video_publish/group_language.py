"""Shared, versioned Page language selection for the Facebook group pool."""
from __future__ import annotations

from typing import Any, Mapping

from features.fb_auto_posts.validation import LANGUAGE_ALIASES, LANGUAGE_RE

from .sources import SourceError

LEGACY_POLICY = "group_pool_v1"
POLICY_VERSION = "group_pool_v2"


def normalize_language(value: Any) -> str:
    """Normalize known aliases without guessing a missing or invalid language."""
    if not isinstance(value, str):
        return ""
    text = value.strip().lower().replace("_", "-")
    text = LANGUAGE_ALIASES.get(text, text)
    if text == "jp" or text.startswith("jp-"):
        text = "ja" + text[2:]
    # In particular zh, zh-cn and zh-tw remain separate routing languages.
    if text in {"und", "unknown", "null", "none"}:
        return ""
    return text if len(text) <= 32 and LANGUAGE_RE.fullmatch(text) and text[0].isalpha() else ""


def default_selection() -> dict:
    return {"mode": "all", "codes": [], "include_blank": False}


def normalize_selection(value) -> dict:
    """Validate a complete selection; callers explicitly choose defaults."""
    def invalid():
        return SourceError("invalid_page_language_selection", "Page语言选择无效，请选择有效语言或空语言Page", 400)

    if (not isinstance(value, Mapping) or set(value) != {"mode", "codes", "include_blank"}
            or not isinstance(value["mode"], str) or value["mode"] not in {"all", "selected"}
            or not isinstance(value["codes"], list)
            or type(value["include_blank"]) is not bool):
        raise invalid()
    codes = [normalize_language(code) for code in value["codes"]]
    if any(not code for code in codes) or (value["mode"] == "all" and codes):
        raise invalid()
    if value["mode"] == "selected" and not codes and not value["include_blank"]:
        raise invalid()
    return {"mode": value["mode"], "codes": sorted(set(codes)), "include_blank": value["include_blank"]}
