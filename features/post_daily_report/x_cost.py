"""Read-only estimate of confirmed X publishing writes, never a billing ledger.

The public rate snapshot is dated 2026-10-08. Repost's mapping to the public
User Interaction: Create rate is an explicit estimate assumption. Historical
rates, reads, uploads, failed requests, credits, tax and subscriptions require
the developer-console bill and are not inferred from publisher rows.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP

from .common import BEIJING, in_window, json_value, parse_time, read_db

PRICING_URL = "https://docs.x.com/x-api/getting-started/pricing"
RATES = {"post_with_url": Decimal("0.200"), "post_without_url": Decimal("0.015"),
         "repost": Decimal("0.015")}
URL = re.compile(r"(?:https?://|www\.)|\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|co|tv|app|ai)(?:[/\s]|$)", re.I)
NOTE = ("按确认成功的发布操作及 2026-10-08 公示费率估算；Repost 暂按互动创建单价估算。"
        "不含读取、上传等其他调用、失败/结果不明请求、Premium、服务器及税费；实际扣费以 X 账单为准。")


def _rows(db, table, fields, required=False):
    columns = {r[1] for r in db.execute('PRAGMA table_info("%s")' % table)}
    if not columns:
        if required:
            raise sqlite3.OperationalError("missing cost source")
        return []
    names = [f for f in fields if f in columns]
    return [dict(r) for r in db.execute('SELECT %s FROM "%s"' %
                                      (','.join('"%s"' % f for f in names), table))]


def collect_cost(paths, start, end):
    """Count by actual Beijing publication date, including prior-plan arrivals.

    Original Post IDs deduplicate the primary ledger and Auto bridge. Relay
    originals are separately billable even when the target Repost later fails.
    Neither a prepared queue nor its stored tracking URL proves a paid Post.
    """
    operations, seen_posts, queue_ids, warnings = [], set(), set(), []
    unreadable, unresolved, unpriced = [], 0, 0

    def add_post(post_id, stamp, text, source):
        nonlocal unpriced
        if not post_id or not in_window(stamp, start, end) or str(post_id) in seen_posts:
            return
        seen_posts.add(str(post_id))
        kind = None if text is None else "post_with_url" if URL.search(text) else "post_without_url"
        if kind is None:
            unpriced += 1
        operations.append({"kind": kind, "source": source, "date": parse_time(stamp).astimezone(BEIJING).date().isoformat()})

    try:
        with read_db(paths["x"]) as db:
            queues = {r["id"]: r for r in _rows(db, "x_post_queue", ["id", "delivery_mode"], True)}
            queue_ids = set(queues)
            logs = _rows(db, "x_post_publish_log", ["queue_id", "status", "x_post_id", "post_text", "published_at",
                "unknown_outcome", "started_at", "attempt_count"], True)
            by_queue = {r["queue_id"]: r for r in logs}
            relays = _rows(db, "x_post_repost_ledger", ["queue_id", "status", "source_post_id", "source_published_at",
                "reposted_at", "unknown_outcome"])
            relay_ids = {r["queue_id"] for r in relays}
            for log in logs:
                if log.get("unknown_outcome") and in_window(log.get("started_at"), start, end):
                    unresolved += 1
                if (log.get("status") == "published" and not log.get("unknown_outcome")
                        and log["queue_id"] not in relay_ids
                        and queues.get(log["queue_id"], {}).get("delivery_mode") != "premium_relay_repost"):
                    add_post(log.get("x_post_id"), log.get("published_at"), log.get("post_text"), "direct")
            for relay in relays:
                log = by_queue.get(relay["queue_id"], {})
                add_post(relay.get("source_post_id"), relay.get("source_published_at"), log.get("post_text"), "relay_original")
                if (relay.get("status") == "reposted" and not relay.get("unknown_outcome")
                        and relay.get("source_post_id") and in_window(relay.get("reposted_at"), start, end)):
                    operations.append({"kind": "repost", "source": "target_repost",
                        "date": parse_time(relay["reposted_at"]).astimezone(BEIJING).date().isoformat()})
    except (OSError, sqlite3.Error, KeyError):
        unreadable.append("x")
        warnings.append("X 主台账费用读取失败；费用总额未知，已读取部分单列。")

    try:
        with read_db(paths["x_auto"]) as db:
            tasks = _rows(db, "x_auto_task", ["status", "execution_queue_id", "publish_id", "published_at_utc",
                "unknown_outcome", "post_text", "body_template", "selection_json"], True)
            for task in tasks:
                if task.get("status") != "published" or task.get("unknown_outcome"):
                    continue
                # A bridge is represented by the primary queue. If its primary
                # result is unresolved, Auto must not overwrite that evidence.
                if task.get("execution_queue_id") in queue_ids:
                    continue
                text = task.get("post_text")
                if text is None:
                    selection = json_value(task.get("selection_json"), {})
                    text = selection.get("post_text") if isinstance(selection, dict) else None
                add_post(task.get("publish_id"), task.get("published_at_utc"), text, "auto_unbridged")
    except (OSError, sqlite3.Error, KeyError):
        unreadable.append("x_auto")
        warnings.append("X Auto 台账费用读取失败；费用总额未知，已读取部分单列。")

    counts = Counter(o["kind"] for o in operations if o["kind"])
    amounts = defaultdict(lambda: Decimal("0"))
    daily = defaultdict(lambda: {"estimated": Decimal("0"), "counts": Counter()})
    for op in operations:
        if op["kind"]:
            amount = RATES[op["kind"]]
            amounts[op["kind"]] += amount
            daily[op["date"]]["estimated"] += amount
            daily[op["date"]]["counts"][op["kind"]] += 1
    known = sum(amounts.values(), Decimal("0"))
    complete = not unreadable and not unpriced
    money = lambda value: str(value.quantize(Decimal("0.001")))
    return {"status": "estimated" if complete else "partial", "currency": "USD",
        "estimated_usd": money(known) if complete else None, "known_estimated_usd": money(known),
        "display_usd": str(known.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if complete else None,
        "counts": {k: counts[k] for k in RATES},
        "source_counts": dict(Counter(o["source"] for o in operations)),
        "breakdown_usd": {k: money(amounts[k]) for k in RATES},
        "unpriced_posts": unpriced, "unconfirmed_attempts": unresolved, "unreadable_sources": unreadable,
        "rate_snapshot_date": "2026-10-08", "rates_usd": {k: money(v) for k, v in RATES.items()},
        "pricing_url": PRICING_URL, "note": NOTE, "warnings": warnings,
        "daily": [{"date": day, "known_estimated_usd": money(value["estimated"]), "counts": dict(value["counts"])}
                  for day, value in sorted(daily.items())]}
