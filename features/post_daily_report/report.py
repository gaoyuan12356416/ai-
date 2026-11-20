from __future__ import annotations

import importlib
from datetime import datetime, timezone


def collect_report(paths, window):
    from .accounting import load_dispatch, backlog
    paths = dict(paths)
    paths['_accounting'] = load_dispatch(paths, window.cutoff)
    channels = []
    for name in ("tt", "fb", "x"):
        try:
            module = importlib.import_module("features.post_daily_report." + name)
            result = module.collect(paths, window)
            validate_channel(result)
        except Exception as exc:
            # Do not include exception text: a failed config/driver can expose secrets.
            result = {"channel": name.upper(), "rows": [], "prior_completed": 0,
                      "warnings": ["数据读取失败（%s），本渠道数量未纳入合计。" % type(exc).__name__],
                      "error": type(exc).__name__}
        channels.append(result)
    # Historical report replay must not manufacture an old queue snapshot from
    # today's mutable projections. Cohort delivery cutoff remains unchanged.
    pending = backlog(paths, cutoff=datetime.now(timezone.utc), channels=('tt', 'fb', 'x'))
    return {"schema_version": 2, "date": window.date, "backlog": pending,
            "cutoff_utc": window.cutoff.isoformat(),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "scope": "AI后台全部Post；不含旧版独立系统及平台端自行发帖", "channels": channels}


def validate_channel(channel):
    for row in channel.get("rows", []):
        for field in ("published", "late", "pending", "unknown", "failed", "blocked", "policy_skipped", "frequency_excluded", "superseded_slots"):
            value = row.get(field, 0)
            if not isinstance(value, int) or value < 0:
                raise ValueError("invalid report counter")
        expected = row.get("expected")
        if expected is not None and (not isinstance(expected, int) or expected < 0):
            raise ValueError("invalid expected counter")
        if expected is not None and row.get("published", 0) + row.get("late", 0) > expected:
            raise ValueError("completion exceeds planned targets")
        if channel.get("accounting") == "actual-fb-v2" and expected is not None:
            if sum(row.get(k, 0) for k in ("published", "late", "pending", "unknown", "failed", "blocked", "policy_skipped")) != expected:
                raise ValueError("FB outcome counters do not reconcile with actual targets")


def total(rows):
    available = all(r.get("data_available", True) for r in rows)
    known = available and all(r.get("expected") is not None for r in rows)
    expected = sum(r.get("expected") if r.get("expected") is not None
                   else r.get("known_expected", 0) for r in rows)
    published = sum(r.get("published", 0) for r in rows)
    late = sum(r.get("late", 0) for r in rows)
    # An explicit collector lower bound may survive an absent plan. Do not infer
    # a quota from successful Posts or turn an unavailable source into zero.
    bounded = available and all(r.get('expected') is not None or 'known_expected' in r for r in rows)
    missing = expected - published - late - sum(r.get('policy_skipped', 0) for r in rows)
    return {"expected": expected if known else None, "known_expected": expected,
            "known_missing": max(0, missing) if bounded else None,
            "expected_lower_bound": expected if bounded else None,
            "published": published if available else None, "late": late if available else None,
            "missing": missing if known else None}



def build_card(report, *, compact=False):
    from .presentation import build_card as render
    return render(report, compact=compact)


def build_compact_card(report):
    return build_card(report, compact=True)


def preview_html(report, card):
    from .presentation import preview_html as render
    return render(report, card)
