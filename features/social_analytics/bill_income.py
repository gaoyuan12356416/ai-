"""Settled social revenue in USD cents, by UTC event date and frozen source IDs."""
from datetime import date, datetime, timedelta, timezone


FIRST_DAY = "2026-11-23"
SITES = {"2049": "facebook", "2310": "facebook", "2116": "x", "2284": "youtube"}


def window(day):
    start = datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def by_campaign(day, site):
    platform = SITES[site]
    start, stop = window(day)
    provenance = ("'adset_id',MIN(adset_id),'ad_id',MIN(ad_id)," if platform == "facebook" else "")
    groups = "BINARY campaign_id" + (",BINARY adset_id,BINARY ad_id" if platform == "facebook" else "")
    return f"""SELECT HEX(JSON_OBJECT('date','{day}','site_id','{site}','platform','{platform}',
        'campaign_id',MIN(campaign_id),{provenance}
        'iap_cents',CAST(SUM(IF(event_name='af_purchase',event_revenue_usd,0))*100 AS SIGNED),
        'iaa_cents',CAST(SUM(IF(event_name='ad_revenue',event_revenue_usd,0))*100 AS SIGNED),
        'ad_events',SUM(event_name='ad_revenue'),'purchase_events',SUM(event_name='af_purchase'),
        'updated_at',CAST(MAX(updated_at) AS CHAR)))
        FROM kunlunads_dev.ads_drama_bills FORCE INDEX(idx_site_event_name_time)
        WHERE site_id='{site}' AND event_name IN ('af_purchase','ad_revenue')
          AND event_time >= {start} AND event_time < {stop}
        GROUP BY {groups}"""


def merge_settled(insights, bills, keys, metric_defaults):
    """Use the two bill event types exactly once, including bill-only identities.

    The insight feed is useful intraday. After IAA arrives the insight money may
    be repopulated by the supplier, so replace its revenue for this source/day.
    """
    if not any(row["ad_events"] for row in bills):
        return insights, False
    def identity(row):
        return tuple(str(row.get(key) or "") for key in keys)
    rows = {identity(row): dict(row, revenue_cents=0, iaa_cents=0) for row in insights}
    for bill in bills:
        key = identity(bill)
        if key not in rows:
            rows[key] = dict(metric_defaults, **{field: bill.get(field) for field in keys})
            rows[key].update(date=bill["date"], site_id=bill["site_id"])
            if "platform" in bill:
                rows[key]["platform"] = bill["platform"]
        row = rows[key]
        row["revenue_cents"] += bill["iap_cents"] + bill["iaa_cents"]
        row["iaa_cents"] = row.get("iaa_cents", 0) + bill["iaa_cents"]
        row["updated_at"] = max(str(row.get("updated_at") or ""), str(bill.get("updated_at") or ""))
    return list(rows.values()), True
