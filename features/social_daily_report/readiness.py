"""Read yesterday's count refresh directly from the same UTC insight table as the report."""
from datetime import date, timedelta

from features.social_analytics.adapters import PLATFORMS
from features.social_analytics.routes import query_rows
from features.youtube_daily_report.collector import read_settings


SITES = sorted({source["site_id"] for platform in PLATFORMS.values() for source in platform["sources"]})


def check_counts(day, env_path):
    target = date.fromisoformat(day)
    closed = target + timedelta(days=1)
    sites = ",".join("'%s'" % site for site in SITES)
    # An in-progress refresh can have both yesterday's and today's update times
    # in one site. Zero is a valid metric once the count rows have been refreshed.
    sql = f"""SELECT HEX(JSON_OBJECT('site',site_id,'rows',COUNT(*),
        'pending_rows',SUM(CASE WHEN updated_at >= '{closed} 00:00:00' THEN 0 ELSE 1 END),
        'last_update_utc',CAST(MAX(updated_at) AS CHAR),
        'clicks',SUM(clicks),'views',SUM(views),'installs',SUM(installs)))
        FROM kunlunads_dev.ads_facebook_page_insight FORCE INDEX(sd)
        WHERE dt='{target}' AND site_id IN ({sites})
          AND (clicks IS NOT NULL OR views IS NOT NULL OR installs IS NOT NULL)
        GROUP BY site_id"""
    settings = read_settings(env_path)
    settings["SOCIAL_ANALYTICS_STATEMENT_TIMEOUT_MS"] = 20000
    evidence = {str(row["site"]): row for row in query_rows(settings, sql, len(SITES))}
    waiting = []
    for site in SITES:
        row = evidence.get(site)
        if (not row or row["pending_rows"] != 0 or
                any(row[field] is None for field in ("clicks", "views", "installs"))):
            waiting.append(site)
    return evidence, waiting
