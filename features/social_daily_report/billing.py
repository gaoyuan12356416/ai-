"""Daily-only financial metrics from purchase bills; no publishing side effects."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import re

from features.social_analytics.backend_scope import eligible
from features.social_analytics.report import campaign_index, resolve
from features.social_analytics.routes import query_source_rows

SITES = {"2049": "facebook", "2310": "facebook", "2116": "x", "2284": "youtube"}
MAX_BILLS = 50000
SOURCE = "kunlunads_dev.ads_drama_bills"


def window(day):
    start = datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def read_bills(app, day):
    start, end = window(day)
    sql = """SELECT HEX(JSON_OBJECT(
      'id',id,'site_id',site_id,'campaign_id',campaign_id,
      'adset_id',adset_id,'ad_id',ad_id,'event_name',event_name,'event_time',event_time,
      'revenue_cents',CAST(ROUND(event_revenue_usd*100) AS SIGNED),
      'user_key',IF(COALESCE(user_id,'')='',NULL,SHA2(user_id,256))))
      FROM kunlunads_dev.ads_drama_bills FORCE INDEX(idx_site_event_name_time)
      WHERE site_id IN ('2049','2310','2116','2284') AND event_name='af_purchase'
        AND event_time >= %d AND event_time < %d
      ORDER BY site_id,event_time,id LIMIT %d""" % (start, end, MAX_BILLS + 1)
    return query_source_rows(app, sql, MAX_BILLS)


@dataclass
class Bucket:
    revenue_cents: int = 0
    orders: int = 0
    users: set = field(default_factory=set)

    def add(self, bill):
        self.revenue_cents += bill["revenue_cents"]
        self.orders += 1
        if bill["revenue_cents"] > 0:
            self.users.add(bill["user_key"])

    def public(self):
        return dict(revenue_cents=self.revenue_cents, conversions=len(self.users),
                    payment_orders=self.orders)


def aggregate(catalog, bills, day):
    """Resolve globally, then retain the existing successful-backend scope."""
    if not bills:
        # No purchase records anywhere cannot establish a completed financial feed.
        raise ValueError("billing_purchase_date_missing")
    if len(bills) > MAX_BILLS:
        raise ValueError("billing_row_limit")
    start, end = window(day)
    index = campaign_index(catalog)
    resolved = {key: resolve(rows) for key, rows in index.items()}
    total, raw = Bucket(), Bucket()
    owners, groups, platforms, sites = (defaultdict(Bucket) for _ in range(4))
    outside = defaultdict(Bucket)
    seen = set()
    for bill in bills:
        site = bill.get("site_id")
        if (site not in SITES or bill.get("event_name") != "af_purchase"
                or type(bill.get("id")) is not int or bill["id"] in seen
                or type(bill.get("event_time")) is not int
                or not start <= bill["event_time"] < end
                or type(bill.get("revenue_cents")) is not int):
            raise ValueError("billing_record_invalid")
        if bill["revenue_cents"] > 0 and not re.fullmatch(r"[a-fA-F0-9]{64}", bill.get("user_key") or ""):
            raise ValueError("billing_payer_missing")
        seen.add(bill["id"])
        raw.add(bill)
        platform = SITES[site]
        key = (platform, site, str(bill.get("campaign_id") or ""))
        candidates, owner = index.get(key, []), resolved.get(key)
        reason = ""
        if not owner:
            reason = "no_unique_backend_campaign"
        elif not any(eligible(row, day) for row in candidates):
            reason = "not_published_or_link_not_yet_created"
        elif platform == "facebook":
            allowed = {(row["effect_source"]["adset_id"], row["effect_source"]["ad_id"])
                       for row in candidates if row.get("effect_source") and eligible(row, day)}
            if (str(bill.get("adset_id") or ""), str(bill.get("ad_id") or "")) not in allowed:
                reason = "outside_backend_frozen_source_ids"
        if reason:
            outside[(site, reason)].add(bill)
            continue
        identity = (owner["tenant"], owner["owner"])
        for bucket in (total, owners[identity], groups[identity + (platform,)],
                       platforms[platform], sites[site]):
            bucket.add(bill)
    if raw.revenue_cents != total.revenue_cents + sum(b.revenue_cents for b in outside.values()):
        raise ValueError("billing_reconciliation_failed")
    return dict(total=total, raw=raw, owners=owners, groups=groups, platforms=platforms,
                sites=sites, outside=outside)


def _patch(row, bucket):
    """Only the two requested metrics and their existing derived values change."""
    row["revenue_cents"] = bucket.revenue_cents if bucket is not None else None
    row["conversions"] = len(bucket.users) if bucket is not None else None
    if "revenue" in row:
        row["revenue"] = round(bucket.revenue_cents / 100, 2) if bucket is not None else None
    if "pay_rate" in row:
        row["pay_rate"] = (round(100 * len(bucket.users) / row["installs"], 2)
                           if bucket is not None and row.get("installs") else None)


def apply(report, financials):
    """Use distinct payers at each scope, including across owners/channels."""
    zero = Bucket()
    known = {(p["owner_tenant"], p["owner"], platform)
             for p in report["rows"] for platform in p["platforms"]}
    if set(financials["groups"]) - known:
        raise ValueError("billing_owner_not_in_report_snapshot")
    _patch(report["totals"], financials["total"])
    for person in report["rows"]:
        owner = (person["owner_tenant"], person["owner"])
        _patch(person, financials["owners"].get(owner, zero))
        for platform, value in person["platforms"].items():
            _patch(value, financials["groups"].get(owner + (platform,), zero))
    report["rows"].sort(key=lambda r: (-r["revenue_cents"], -(r["published_count"] or 0),
                                     r["name"], r["owner_tenant"], r["owner"]))
    for platform, value in report["platforms"].items():
        bucket = financials["platforms"].get(platform, zero)
        _patch(value["totals"], bucket)
        _patch(value["source"], bucket)
        for key in ("unmatched", "excluded"):
            _patch(value[key], zero)
        quality = value["quality"]
        for key in ("source_totals", "attributed_totals"):
            _patch(quality[key], bucket)
        for source in quality.get("sources", {}).values():
            site_bucket = financials["sites"].get(source["site_id"], zero)
            for key in ("totals", "source_totals", "attributed_totals"):
                if key in source:
                    _patch(source[key], site_bucket)
    shared = report["shared_report"]
    _patch(shared["totals"], financials["total"])
    for row in shared["all_rows"]:
        key = (row.get("owner_tenant", ""), row["owner"], row["platform"])
        _patch(row, financials["groups"].get(key, zero))
    shared["all_rows"].sort(key=lambda r: (-r["revenue_cents"], str(r["owner"]), r["platform"]))
    for platform, value in shared["platform_totals"].items():
        _patch(value, financials["platforms"].get(platform, zero))
    for value in shared.get("trend", []):
        _patch(value, financials["total"])
    if "pagination" in shared:
        page = shared["pagination"]
        offset = (page["page"] - 1) * page["page_size"]
        shared["rows"] = shared["all_rows"][offset:offset + page["page_size"]]
        shared["ranking"] = [dict(row, label=" / ".join(
            str(row.get(key + "_label", row[key])) for key in shared["group_by"]))
            for row in shared["all_rows"][:10]]
    # Keep the original insight reconciliation explicitly separate from bills.
    if "scope_audit" in shared:
        shared["insight_scope_audit"] = shared.pop("scope_audit")
    audit = dict(source=SOURCE, event="af_purchase", timezone="UTC", state="complete",
                 payer_identity="distinct_user_id_per_scope", raw=financials["raw"].public(),
                 included=financials["total"].public(),
                 outside=[dict(site_id=site, reason=reason, **bucket.public())
                          for (site, reason), bucket in sorted(financials["outside"].items())])
    report["quality"]["billing"] = audit
    shared["billing_scope_audit"] = audit
    shared.setdefault("meta", {})["financial_source"] = SOURCE
    report["financial_source"] = SOURCE
    report["warnings"] = shared["warnings"] = [
        "收入采用订单账单支付金额；付费人数在各汇总范围内按 user_id 去重，人数不跨范围相加。"
        if value.startswith("转化数采用源表充值人数") else value
        for value in report["warnings"]]
    return report


def unavailable(report, reason):
    """A failed read must never send stale insight money or a false zero."""
    _patch(report["totals"], None)
    for row in report["rows"]:
        _patch(row, None)
        row["data_status"] = "partial"
        for value in row["platforms"].values():
            _patch(value, None)
    for value in report["platforms"].values():
        _patch(value["totals"], None)
        value["quality"].update(state="partial", error="订单账单采集未完成")
    report["quality"].update(complete=False, billing=dict(
        state="unavailable", source=SOURCE, error=reason))
    # These are explicitly retained as diagnostics, not replacement financials.
    report["shared_report"].setdefault("meta", {})["daily_billing_unavailable"] = True
    return report


def collect_financials(report, app, catalog):
    try:
        return apply(report, aggregate(catalog, read_bills(app, report["report_date"]), report["report_date"]))
    except Exception as exc:
        # Driver messages and SQL must not leak into Feishu or failure diagnostics.
        return unavailable(report, type(exc).__name__)
