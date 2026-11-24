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
