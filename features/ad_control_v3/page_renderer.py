"""Dynamic page renderer for the isolated ad-control V3 UI.

The renderer deliberately accepts only two named templates and two named assets.
It never interpolates request values into HTML.  Server-provided bootstrap data is
serialized into a non-executable JSON script element after escaping every byte
sequence that could terminate the element or become executable JavaScript.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional


_PACKAGE_ROOT = Path(__file__).resolve().parent
_TEMPLATE_ROOT = _PACKAGE_ROOT / "templates"
_ASSET_ROOT = _PACKAGE_ROOT / "assets"

_PAGE_TEMPLATES = {
    "rule-groups": "rule-groups.html",
    "execution-logs": "execution-logs.html",
}

_ASSETS = {
    "app.css": "app.css",
    "app.js": "app.js",
}

_BOOTSTRAP_MARKER = "__AD_CONTROL_V3_BOOTSTRAP__"


def _bootstrap_json(bootstrap: Optional[Mapping[str, Any]]) -> str:
    """Return compact JSON that is safe inside an HTML ``script`` element."""

    payload = dict(bootstrap or {})
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    # Escaping '<' prevents a value containing ``</script>`` from ending the
    # data element.  The remaining escapes keep the payload safe if a browser,
    # proxy, or future refactor interprets it in a JavaScript context.
    return (
        serialized.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_page(
    page_name: str,
    bootstrap: Optional[Mapping[str, Any]] = None,
) -> str:
    """Render one allow-listed V3 page.

    ``ValueError`` is intentional for unknown pages so route wiring fails closed
    instead of reading an arbitrary path from disk.
    """

    template_name = _PAGE_TEMPLATES.get(str(page_name or ""))
    if not template_name:
        raise ValueError("unknown_ad_control_v3_page")
    template = (_TEMPLATE_ROOT / template_name).read_text(encoding="utf-8")
    if template.count(_BOOTSTRAP_MARKER) != 1:
        raise RuntimeError("invalid_ad_control_v3_template")
    return template.replace(_BOOTSTRAP_MARKER, _bootstrap_json(bootstrap), 1)


def render_rule_groups_page(bootstrap: Optional[Mapping[str, Any]] = None) -> str:
    return render_page("rule-groups", bootstrap)


def render_execution_logs_page(bootstrap: Optional[Mapping[str, Any]] = None) -> str:
    return render_page("execution-logs", bootstrap)


def load_asset(asset_name: str) -> bytes:
    """Load one immutable, allow-listed page asset as bytes."""

    filename = _ASSETS.get(str(asset_name or ""))
    if not filename:
        raise ValueError("unknown_ad_control_v3_asset")
    return (_ASSET_ROOT / filename).read_bytes()


__all__ = [
    "load_asset",
    "render_execution_logs_page",
    "render_page",
    "render_rule_groups_page",
]
