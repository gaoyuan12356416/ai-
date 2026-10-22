"""Render a small v3 shell plus immutable, lazily loaded detail artifacts."""

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit


DEFAULT_HOME_URL = (
    "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/"
    "codex-artifacts/html/ai-image-daily/index.html"
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
    return _render(payload, inline_script=True)


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _render(payload: Dict[str, Any], inline_script: bool = False) -> str:
    serialized = _json(payload).decode("utf-8")
    # Escaping < closes both script-tag and HTML-comment injection avenues.
    serialized = (serialized.replace("&", "\\u0026").replace("<", "\\u003c")
                  .replace(">", "\\u003e").replace("\u2028", "\\u2028")
                  .replace("\u2029", "\\u2029"))
    template = Path(__file__).with_name("report.html").read_text(encoding="utf-8")
    marker = "__REPORT_JSON__"
    if template.count(marker) != 1:
        raise ValueError("Report template must contain exactly one JSON marker")
    script = Path(__file__).with_name("report.js").read_text(encoding="utf-8")
    script_tag = ("<script>\n" + script + "\n</script>" if inline_script else
                  '<script defer src="' + payload["script_url"] + '"></script>')
    return template.replace(marker, serialized).replace("__REPORT_SCRIPT__", script_tag)


def _top_rows(view: Dict[str, Any], weekly: bool = False) -> Dict[str, Any]:
    """Allowlist top rows so original URLs and large metadata stay off the shell."""
    groups = (view.get("week", {}) if weekly else view).get("groups", {})
    dramas = sorted((row for row in groups.get("dramas", []) if float(row.get("spend") or 0) > 0),
                    key=lambda row: float(row.get("spend") or 0), reverse=True)[:10]
    metric_keys = ("spend", "af_revenue_d0", "roas", "impressions", "clicks",
                   "installs", "af_installs", "purchases", "ctr", "cpi")
    materials = []
    for item in view.get("materials", []):
        metrics = item.get("week", {}) if weekly else item
        if float(metrics.get("spend") or 0) <= 0:
            continue
        row = {key: item.get(key) for key in
               ("id", "name", "content_id", "source_product_name", "delivery_product_names")}
        row.update({key: metrics.get(key) for key in metric_keys})
        materials.append(row)
    materials.sort(key=lambda row: float(row.get("spend") or 0), reverse=True)
    return {"dramas": [{key: row.get(key) for key in ("name", "count", *metric_keys)}
                       for row in dramas], "materials": materials[:10]}


def build_bundle(report: Dict[str, Any], history: List[Dict[str, Any]],
                 base_url: str, version: str) -> Dict[str, Dict[str, Any]]:
    """Return upload objects. Only detail JSON contains full row data.

    The publisher owns compression, storage headers, manifest checksums and
    commit order. Paths here are relative to base_url. No network or writes.
    """
    if report.get("schema_version") != 3 or not report.get("views"):
        raise ValueError("build_bundle requires schema_version=3 and views")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", version):
        raise ValueError("Invalid immutable bundle version")
    date = report.get("report_date", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("Invalid report date")
    base_url = _safe_url(base_url.rstrip("/"))
    if not base_url or any(char in base_url for char in '<>"\'?#'):
        raise ValueError("Invalid public report base URL")
    source = _sanitize_urls(copy.deepcopy(report))
    # Fail non-finite values even in sections not sent to the browser.
    _json(source)
    shell = {key: source[key] for key in (
        "schema_version", "report_date", "generated_at", "timezone", "currency",
        "period", "snapshot_kind", "validation", "notes", "products", "freshness",
    ) if key in source}
    shell.update(home_url=base_url + "/index.html", history_url=base_url + "/history.json",
                 status_url=base_url + "/status.json", bundle_version=version,
                 script_url=base_url + "/assets/" + version + "/report.js", views={})
    bundle = {}
    for key, view in source["views"].items():
        key = str(key)
        if not re.fullmatch(r"all|[0-9]+", key):
            raise ValueError("Invalid product view key")
        detail_key = "assets/" + version + "/details-" + key + ".json"
        detail = dict(groups=view.get("groups", {}),
                      week_groups=view.get("week", {}).get("groups", {}),
                      materials=view.get("materials", []), unresolved=view.get("unresolved", []))
        bundle[detail_key] = dict(data=_json(detail), content_type="application/json; charset=utf-8",
                                  immutable=True, compress=True)
        compact = {name: view[name] for name in
                   ("id", "name", "summary", "comparisons", "trend", "insights") if name in view}
        compact["week"] = {name: value for name, value in view.get("week", {}).items()
                           if name in ("production", "performance", "comparisons", "insights")}
        compact.update(details_url=base_url + "/" + detail_key,
                       material_count=len(detail["materials"]),
                       unresolved_count=len(detail["unresolved"]),
                       top=_top_rows(view), week_top=_top_rows(view, True))
        shell["views"][key] = compact
    rendered = _render(shell).encode("utf-8")
    bundle["index.html"] = dict(data=rendered, content_type="text/html; charset=utf-8",
                                immutable=False, compress=True)
    bundle["daily/" + date + "/index.html"] = dict(bundle["index.html"], immutable=True)
    bundle["assets/" + version + "/report.js"] = dict(
        data=Path(__file__).with_name("report.js").read_bytes(),
        content_type="application/javascript; charset=utf-8", immutable=True, compress=True)
    bundle["history.json"] = dict(data=_json(_sanitize_urls(copy.deepcopy(history))),
                                  content_type="application/json; charset=utf-8",
                                  immutable=False, compress=True)
    return bundle
