"""Daily-only financial metrics from purchase bills; no publishing side effects."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import re

from features.social_analytics.backend_scope import eligible
from features.social_analytics.bill_income import FIRST_DAY, SITES, by_campaign, window
from features.social_analytics.report import campaign_index, resolve
from features.social_analytics.routes import query_source_rows
from features.social_analytics.tiktok import campaign_key
from features.social_analytics.tiktok_scope import UNASSIGNED

MAX_BILLS = 50000
SOURCE = "kunlunads_dev.ads_drama_bills"


def read_bills(app, day, *, tiktok_provisional=False):
    start, end = window(day)
    table = "ads_drama_bills_t0" if tiktok_provisional else "ads_drama_bills"
    sites = "'2250'" if tiktok_provisional else "'2049','2310','2116','2284','2250'"
    sql = """SELECT HEX(JSON_OBJECT(
      'id',id,'site_id',site_id,'campaign_id',campaign_id,'campaign',campaign,
      'adset_id',adset_id,'ad_id',ad_id,'event_name',event_name,'event_time',event_time,
      'revenue_cents',CAST(ROUND(event_revenue_usd*100) AS SIGNED),
      'positive_purchase',IF(site_id='2250',event_revenue_usd>0,ROUND(event_revenue_usd*100)>0),
      'user_key',IF(COALESCE(user_id,'')='',NULL,SHA2(user_id,256))))
      FROM kunlunads_dev.%s FORCE INDEX(idx_site_event_name_time)
      WHERE site_id IN (%s) AND event_name='af_purchase'
        AND event_time >= %d AND event_time < %d
      ORDER BY site_id,event_time,id LIMIT %d""" % (table, sites, start, end, MAX_BILLS + 1)
    records = query_source_rows(app, sql, MAX_BILLS)
    return [dict(row, provisional=True) for row in records] if tiktok_provisional else records


@dataclass
class Bucket:
    revenue_cents: int = 0
    iaa_cents: int = 0
    orders: int = 0
    users: set = field(default_factory=set)

    def add(self, bill):
        kind = bill["event_name"]
        self.revenue_cents += bill["revenue_cents"]
        if kind == "ad_revenue":
            self.iaa_cents += bill["revenue_cents"]
            return
        if kind == "grouped_purchase_income":
            return
        self.orders += 1
        if bill.get("positive_purchase", bill["revenue_cents"] > 0):
            self.users.add(bill["user_key"])

    def public(self):
        return dict(revenue_cents=self.revenue_cents, iap_cents=self.revenue_cents - self.iaa_cents,
                    iaa_cents=self.iaa_cents, conversions=len(self.users),
                    payment_orders=self.orders)


def aggregate(catalog, bills, day, ad_groups=()):
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
    tt_unassigned, tt_attributed = Bucket(), Bucket()
    tt_orders, tt_group_orders = Counter(), Counter()
    seen = set()
    for bill in bills:
        site = bill.get("site_id")
        record_key = (bool(bill.get("provisional")), bill.get("id"))
        if (site not in SITES or bill.get("event_name") != "af_purchase"
                or type(bill.get("id")) is not int or record_key in seen
                or type(bill.get("event_time")) is not int
                or not start <= bill["event_time"] < end
                or type(bill.get("revenue_cents")) is not int):
            raise ValueError("billing_record_invalid")
        if bill.get("positive_purchase", bill["revenue_cents"] > 0) and not re.fullmatch(r"[a-fA-F0-9]{64}", bill.get("user_key") or ""):
            raise ValueError("billing_payer_missing")
        if site == "2250":
            tt_orders[campaign_key(bill.get("campaign_id"), bill.get("campaign"))] += 1
        seen.add(record_key)
    coverage = set()
    ads = []
    for row in ad_groups:
        if row["date"] != day or row["site_id"] not in SITES or type(row["iaa_cents"]) is not int:
            raise ValueError("billing_record_invalid")
        if row["site_id"] == "2250" and row["purchase_events"]:
            tt_group_orders[campaign_key(row.get("campaign_id"), row.get("campaign"))] += row["purchase_events"]
            ads.append(dict(row, event_name="grouped_purchase_income", revenue_cents=row["iap_cents"]))
        if row["ad_events"] and (row["site_id"] != "2250" or day >= FIRST_DAY):
            coverage.add(row["site_id"])
            ads.append(dict(row, event_name="ad_revenue", revenue_cents=row["iaa_cents"]))
    if tt_orders != tt_group_orders:
        raise ValueError("billing_reconciliation_failed")
    for bill in [*bills, *ads]:
        site = bill["site_id"]
        cid = str(bill.get("campaign_id") or "")
        if site == "2250":
            cid = campaign_key(bill.get("campaign_id"), bill.get("campaign"))
            if bill["event_name"] == "af_purchase":
                # Count the transaction and its payer; grouped decimal amounts
                # supply money once, with the same site-level rounding as the web report.
                bill = dict(bill, positive_purchase=bill.get("positive_purchase", bill["revenue_cents"] > 0),
                            revenue_cents=0)
        raw.add(bill)
        platform = SITES[site]
        key = (platform, site, cid)
        candidates, owner = index.get(key, []), resolved.get(key)
        reason = ""
        if not owner or (platform == "tiktok" and owner.get("owner") == UNASSIGNED):
            reason = "no_unique_backend_campaign"
        elif not any(eligible(row, day) for row in candidates):
            reason = "not_published_or_link_not_yet_created"
        elif platform == "facebook":
            allowed = {(row["effect_source"]["adset_id"], row["effect_source"]["ad_id"])
                       for row in candidates if row.get("effect_source") and eligible(row, day)}
            if (str(bill.get("adset_id") or ""), str(bill.get("ad_id") or "")) not in allowed:
                reason = "outside_backend_frozen_source_ids"
        if reason:
            if site == "2250":
                for bucket in (tt_unassigned, total, platforms[platform], sites[site]):
                    bucket.add(bill)
                continue
            outside[(site, reason)].add(bill)
            continue
        if site == "2250":
            tt_attributed.add(bill)
        identity = (owner["tenant"], owner["owner"])
        for bucket in (total, owners[identity], groups[identity + (platform,)],
                       platforms[platform], sites[site]):
            bucket.add(bill)
    if any(getattr(raw, k) != getattr(total, k) + sum(getattr(b, k) for b in outside.values())
           for k in ("revenue_cents", "iaa_cents", "orders")):
        raise ValueError("billing_reconciliation_failed")
    return dict(total=total, raw=raw, owners=owners, groups=groups, platforms=platforms,
                sites=sites, outside=outside, iaa_sites=sorted(coverage),
                tt_unassigned=tt_unassigned, tt_attributed=tt_attributed)


def _patch(row, bucket):
    """Only the two requested metrics and their existing derived values change."""
    row["revenue_cents"] = bucket.revenue_cents if bucket is not None else None
    row["iap_cents"] = bucket.revenue_cents - bucket.iaa_cents if bucket is not None else None
    row["iaa_cents"] = bucket.iaa_cents if bucket is not None else None
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
        linked = financials["tt_attributed"] if platform == "tiktok" else bucket
        unmatched = financials["tt_unassigned"] if platform == "tiktok" else zero
        _patch(value["totals"], bucket)
        _patch(value["source"], bucket)
        _patch(value["unmatched"], unmatched)
        _patch(value["excluded"], zero)
        if platform == "tiktok":
            value.update(assigned_income=linked.public(), unassigned_income=unmatched.public())
        quality = value["quality"]
        _patch(quality["source_totals"], bucket)
        _patch(quality["attributed_totals"], linked)
        if platform == "tiktok":
            _patch(quality["unmatched_totals"], unmatched)
        for source in quality.get("sources", {}).values():
            site_bucket = financials["sites"].get(source["site_id"], zero)
            for key in ("totals", "source_totals", "attributed_totals"):
                if key in source:
                    _patch(source[key], linked if key == "attributed_totals" and platform == "tiktok" else site_bucket)
            if platform == "tiktok" and "unmatched_totals" in source:
                _patch(source["unmatched_totals"], unmatched)
    shared = report["shared_report"]
    _patch(shared["totals"], financials["total"])
    for row in shared["all_rows"]:
        key = (row.get("owner_tenant", ""), row["owner"], row["platform"])
        _patch(row, financials["tt_unassigned"] if row["platform"] == "tiktok" and row["owner"] == UNASSIGNED
               else financials["groups"].get(key, zero))
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
    audit["iaa_sites"] = financials["iaa_sites"]
    audit["revenue_events"] = ["af_purchase", "ad_revenue"]
    shared["billing_scope_audit"] = audit
    shared.setdefault("meta", {})["financial_source"] = SOURCE
    report["financial_source"] = SOURCE
    report["warnings"] = shared["warnings"] = [
        "收入为充值和已回传的 IAA 广告收入之和。付费人数仅按充值账单的用户去重。"
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
        day = report["report_date"]
        bills = read_bills(app, day)
        ad_groups = []
        if bills:
            for site in SITES:
                if day < FIRST_DAY and (site != "2250" or not any(b["site_id"] == site for b in bills)):
                    continue
                groups = query_source_rows(app, by_campaign(day, site), MAX_BILLS)
                if site == "2250":
                    from features.social_analytics.bill_income import allocate_tiktok_cents
                    if any(r["ad_events"] for r in groups) and not any(r["purchase_events"] for r in groups):
                        fallback = read_bills(app, day, tiktok_provisional=True)
                        if fallback:
                            bills = [*bills, *fallback]
                            groups += query_source_rows(app, by_campaign(day, site, provisional_purchases=True), MAX_BILLS)
                    groups = allocate_tiktok_cents(groups)
                ad_groups.extend(groups)
        return apply(report, aggregate(catalog, bills, day, ad_groups))
    except Exception as exc:
        # Driver messages and SQL must not leak into Feishu or failure diagnostics.
        safe_codes = {
            "billing_purchase_date_missing", "billing_row_limit", "billing_record_invalid",
            "billing_payer_missing", "billing_reconciliation_failed",
            "billing_owner_not_in_report_snapshot",
        }
        reason = str(exc) if isinstance(exc, ValueError) and str(exc) in safe_codes else type(exc).__name__
        return unavailable(report, reason)
