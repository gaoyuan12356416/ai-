"""Read the same campaign-ID report used by the authenticated web report."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import time

from features.youtube_daily_report.collector import BEIJING, DB_PATH, ENV_PATH, read_settings

X_DB_PATH = "/var/lib/x-post-automation/accounts.sqlite3"
FB_DB_PATH = "/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3"
FACEBOOK_ATTRIBUTION_DB_PATH = "/mnt/data-disk/social-analytics/attribution.sqlite3"
METRICS = (
    "revenue_cents", "refund_cents", "installs", "clicks", "views",
    "conversions", "published_count", "post_count", "repost_count",
)


def _integer(value):
    if value is None:
        return None
    number = Decimal(str(value))
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError("non_integral_report_metric")
    return int(number)


def metrics(row):
    """Presentation conversion only; money is never summed as a float."""
    result = {field: _integer(row.get(field)) for field in METRICS}
    for cents, dollars in (("revenue_cents", "revenue"), ("refund_cents", "refunds")):
        if cents not in row and row.get(dollars) is not None:
            result[cents] = _integer(Decimal(str(row[dollars])) * 100)
    return result


def _add(target, source):
    for field in METRICS:
        # Only this person's own rows can establish a known metric. Other
        # people's sources must not turn an entirely unknown value into zero.
        if source[field] is not None:
            target[field] = (target[field] or 0) + source[field]


def present(shared, report_date):
    """Create a card DTO; campaign resolution and publication counts stay shared."""
    people = {}
    statuses = {}
    for row in shared["all_rows"]:
        # The system report spans tenants; names and bare user IDs are not identities.
        owner = str(row["owner"])
        tenant = str(row.get("owner_tenant") or "")
        person = people.setdefault((tenant, owner), {
            "owner": owner, "owner_tenant": tenant, "name": row.get("owner_label") or owner,
            "platforms": {}, **dict.fromkeys(METRICS, None),
        })
        platform = str(row["platform"])
        if platform in person["platforms"]:
            raise ValueError("duplicate_owner_platform_report_row")
        value = metrics(row)
        person["platforms"][platform] = value
        _add(person, value)
        state = row.get("data_status") or shared["quality"]["platforms"].get(platform, {}).get("state")
        statuses.setdefault((tenant, owner), []).append(
            state if state in ("complete", "partial", "unavailable") else "unavailable")
    quality = shared["quality"]
    shared_totals = metrics(shared["totals"])
    for identity, person in people.items():
        states = statuses[identity]
        person["data_status"] = (
            "complete" if all(state == "complete" for state in states) else
            "unavailable" if all(state == "unavailable" for state in states) else "partial")
    platforms = {}
    for platform, total in shared["platform_totals"].items():
        state = quality["platforms"][platform]
        platforms[platform] = {
            "name": state.get("label") or total.get("platform_label") or
                    shared.get("platform_metadata", {}).get(platform, {}).get("label") or platform,
            "totals": metrics(total), "quality": state,
            "unmatched": metrics(state.get("unmatched_totals", {})),
            "excluded": metrics(state.get("excluded_totals", {})),
            "source": metrics(state.get("source_totals", {})),
        }
    return {
        "schema_version": 3, "report_date": date.fromisoformat(report_date).isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "publication_timezone": "UTC", "metric_timezone": "UTC", "currency": "USD",
        "scope": "all_historical_content_effects_and_confirmed_publications",
        "attribution": "unique_frozen_campaign_id_by_platform",
        "rows": sorted(people.values(), key=lambda row: (
            row["revenue_cents"] is None, -(row["revenue_cents"] or 0),
            -(row["published_count"] or 0), row["name"], row["owner_tenant"], row["owner"])),
        "totals": shared_totals, "platforms": platforms,
        "quality": quality, "warnings": shared.get("warnings", []),
        "shared_report": shared,
    }


def collect(report_date, db_path=DB_PATH, env_path=ENV_PATH, x_db_path=X_DB_PATH,
            fb_db_path=FB_DB_PATH, facebook_attribution_db_path=FACEBOOK_ATTRIBUTION_DB_PATH):
    from features.social_analytics.routes import build_report

    day = date.fromisoformat(report_date).isoformat()
    app = read_settings(env_path)
    app.update(JOB_DB_PATH=db_path, SOCIAL_ANALYTICS_X_DB_PATH=x_db_path,
               SOCIAL_ANALYTICS_FB_DB_PATH=fb_db_path,
               SOCIAL_ANALYTICS_FACEBOOK_ATTRIBUTION_DB=facebook_attribution_db_path,
               SOCIAL_ANALYTICS_QUERY_ATTEMPTS=2,
               SOCIAL_ANALYTICS_QUERY_DEADLINE=time.monotonic() + 120)
    shared = build_report(
        app, {"role": "admin", "_system_report": True},
        {"start": [day], "end": [day], "group_by": ["owner,platform"], "page_size": ["100"]},
        include_titles=False,
    )
    return present(shared, day)
