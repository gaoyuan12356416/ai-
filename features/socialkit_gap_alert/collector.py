"""Compare exact Dramawave identities; unavailable observations are never zero."""
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import time

from features.socialkit_analytics.query import snapshot
from features.social_analytics.adapters import read_catalogs
from features.social_analytics.report import campaign_index, resolve
from features.social_analytics.backend_scope import eligible
from features.social_analytics.bill_income import FIRST_DAY, by_campaign
from features.social_analytics.routes import build_report, source, scoped_facebook_source

APP_ID = 1479
CHANNELS = ("facebook", "youtube", "x")
FRESH_SECONDS = 7200
HISTORY_FRESH_SECONDS = 26 * 3600  # Historical snapshot dates are reconciled daily.
RULE_VERSION = "previous-complete-utc-day-v1"
MIN_SOCIALKIT_CENTS = 1000
SITES = {"facebook": {"2049", "2310"}, "youtube": {"2284"}, "x": {"2116"}}
ERROR_CODES = frozenset({"socialkit_date_unavailable", "socialkit_snapshot_stale",
                         "socialkit_income_source_stale", "own_source_or_attribution_incomplete",
                         "own_income_source_stale", "own_income_not_integer_cents", "statistics_day_changed",
                         "own_iaa_pending", "socialkit_iaa_pending"})


class ComparisonUnavailable(ValueError):
    pass


def safe_error(exc):
    return str(exc) if isinstance(exc, ComparisonUnavailable) and str(exc) in ERROR_CODES else type(exc).__name__


def utc(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # Source MySQL sessions are explicitly UTC, including naive SQL strings.
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def fresh(value, now, max_age=FRESH_SECONDS):
    try:
        return -300 <= (now - utc(value)).total_seconds() <= max_age
    except (ValueError, TypeError):
        return False


def date_range(start, end):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


def comparison_period(report_date):
    day = date.fromisoformat(report_date)
    yesterday = (day - timedelta(days=1)).isoformat()
    return dict(start=yesterday, end=yesterday, days=1, timezone="UTC")


def socialkit_candidates(root, version, day, now, *, end=None, enforce_fresh=True):
    end = end or day
    conn, manifest, _ = snapshot(root, version)
    try:
        details = {value: manifest.get("date_manifest", {}).get(value, {}) for value in date_range(day, end)}
        historical = end < now.astimezone(timezone.utc).date().isoformat()
        for detail in details.values():
            if detail.get("state") != "complete":
                raise ComparisonUnavailable("socialkit_date_unavailable")
            if enforce_fresh and not fresh(detail.get("refreshed_at"), now,
                                            HISTORY_FRESH_SECONDS if historical else FRESH_SECONDS):
                raise ComparisonUnavailable("socialkit_snapshot_stale")
        if end >= FIRST_DAY and any(d >= FIRST_DAY and not detail.get('quality', {}).get('iaa_covered')
                                    for d, detail in details.items()):
            raise ComparisonUnavailable("socialkit_iaa_pending")
        ad_sum = "SUM(iaa_cents)" if end >= FIRST_DAY else "NULL"
        rows = [dict(row) for row in conn.execute(f"""
            SELECT platform,drama_app_id,drama_content_id AS drama,
                   MAX(drama_name) AS drama_name,SUM(revenue_cents) AS revenue_cents,{ad_sum} AS iaa_cents
            FROM effects WHERE date BETWEEN ? AND ? AND drama_app_id=? AND matched=1
              AND platform IN ('facebook','youtube','x') AND drama_content_id<>''
            GROUP BY platform,drama_app_id,drama_content_id
            HAVING SUM(revenue_cents)>0
        """, (day, end, APP_ID))]
        # Old revenue rows can legitimately retain old updated_at values.
        # Historical freshness is established by successful daily recollection.
        if rows and enforce_fresh and not historical and not all(
                fresh(detail.get("source_updated_at"), now) for detail in details.values()):
            raise ComparisonUnavailable("socialkit_income_source_stale")
        return rows, dict(version=manifest["version"],
                          refreshed_at=min(str(d.get("refreshed_at") or "") for d in details.values()),
                          source_updated_at=max(str(d.get("source_updated_at") or "") for d in details.values()),
                          date_manifest=details)
    finally:
        conn.close()


def history(catalog):
    """Deduplicate all confirmed publications, and quarantine ambiguous identities."""
    result, blocked, publications = defaultdict(dict), set(), defaultdict(list)
    index = campaign_index(catalog)
    for row in catalog:
        drama = str(row.get("drama") or "")
        if row.get("excluded"):
            continue
        key = (row["platform"], row["site_id"], row["key"][0])
        if (row["site_id"] not in SITES[row["platform"]] or not drama
                or not resolve(index.get(key))
                or row.get("attribution_status", "frozen") != "frozen"):
            blocked.add(drama)
            continue
        for fact in row.get("publications", []):
            if not fact.get("id") or not fact.get("date"):
                blocked.add(drama)
                continue
            publications[fact["id"]].append((drama, fact["date"]))
    for publication_id, facts in publications.items():
        if len(set(facts)) != 1:
            blocked.update(drama for drama, _ in facts)
        else:
            drama, day = facts[0]
            result[drama][publication_id] = day
    return {key: dict(count=len(value), last_date=max(value.values())) for key, value in result.items()}, blocked


def source_times(app, day, platform, *, raw_sources=None, catalog=None):
    """Reuse the same collector cache populated by build_report, per source site."""
    values = {}
    for site in sorted(SITES[platform]):
        rows, _ = (scoped_facebook_source(app, day, day, site, catalog, include_unconfirmed=True) if platform == "facebook"
                   else source(app, day, day, platform, site))
        values[site] = max((str(row.get("updated_at") or "") for row in rows), default="")
        if raw_sources is not None:
            raw_sources[site] = rows
    return values


def unconfirmed_income_drama(catalog, raw_sources, day):
    """Do not turn excluded, known publication-uncertain income into zero.

    The shared web report intentionally excludes income until publication is
    confirmed. A comparison must retain that uncertainty instead of claiming
    the drama earned nothing. Reused FB campaign IDs still require the exact
    frozen Page/material pair; unrelated traffic must not block an alert.
    """
    index, blocked = campaign_index(catalog), set()
    for site, metrics in raw_sources.items():
        for metric in metrics:
            if not metric.get("revenue_cents") or metric.get("_coverage_only"):
                continue
            key = (metric["platform"], site, str(metric.get("campaign_id") or ""))
            candidates = index.get(key, [])
            for row in candidates:
                if row.get("excluded") or eligible(row, day):
                    continue
                if row["platform"] == "facebook":
                    expected = row.get("effect_source")
                    actual = (str(metric.get("adset_id") or ""), str(metric.get("ad_id") or ""))
                    if not expected or actual != (expected["adset_id"], expected["ad_id"]):
                        continue
                if row.get("drama"):
                    blocked.add(row["drama"])
    return blocked


def warehouse_snapshot(app):
    root = Path(app.get("SOCIAL_ANALYTICS_WAREHOUSE_ROOT") or "/mnt/data-disk/social-analytics-warehouse")
    manifest = json.loads((root / "current.json").read_text())
    path = (root / manifest["database"]).resolve()
    if path.parent != (root / "snapshots").resolve() or path.suffix != ".sqlite3":
        raise ComparisonUnavailable("own_source_or_attribution_incomplete")
    return manifest, path.as_uri() + "?mode=ro&immutable=1"


def warehouse_channel(app, snapshot_info, day, platform, now, enforce_fresh):
    """Compare the published report generation, retaining uncertain bill identities."""
    from features.social_analytics.routes import query_source_rows
    manifest, uri = snapshot_info
    sites = SITES[platform]
    if enforce_fresh and not fresh(manifest["generated_at"], now, HISTORY_FRESH_SECONDS):
        raise ComparisonUnavailable("own_income_source_stale")
    for site in sites:
        key = platform + ":" + site
        health = manifest.get("source_metadata", {}).get(key, {})
        errors = manifest.get("catalog_errors", {})
        if (key in errors or platform in errors or day not in manifest["coverage"].get(key, ())
                or health.get("state", "complete") != "complete" or health.get("freshness", "fresh") != "fresh"
                or health.get("conflict_count")):
            raise ComparisonUnavailable("own_source_or_attribution_incomplete")
        if day not in manifest.get("iaa_coverage", {}).get(key, ()):
            raise ComparisonUnavailable("own_iaa_pending")
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        catalog = [row for (payload,) in conn.execute("SELECT payload FROM catalog")
                   if (row := json.loads(payload))["platform"] == platform]
        history_rows, blocked = history(catalog)
        index = campaign_index(catalog)
        amounts, ads = defaultdict(int), defaultdict(int)
        for (payload,) in conn.execute("SELECT payload FROM effects WHERE platform=? AND dt=?", (platform, day)):
            fact = json.loads(payload)
            owner = resolve(index.get((platform, fact["site_id"], fact["campaign_id"]), ()))
            if not owner:
                raise ComparisonUnavailable("own_source_or_attribution_incomplete")
            amounts[owner["drama"]] += fact["revenue_cents"]
            ads[owner["drama"]] += fact.get("iaa_cents", 0)
    # The warehouse correctly excludes unconfirmed posts. They cannot serve as
    # evidence of zero income in a comparison. Check the source for these only.
    times = {}
    for site in sorted(sites):
        rows = query_source_rows(app, by_campaign(day, site), 50000)
        for row in rows:
            row["revenue_cents"] = row["iap_cents"] + row["iaa_cents"]
        blocked.update(unconfirmed_income_drama(catalog, {site: rows}, day))
        times[site] = max((str(r["updated_at"] or "") for r in rows), default="")
    return dict(amounts=dict(amounts), iaa=dict(ads), history=history_rows, blocked=blocked,
                source_updated_at=times, source_updated_by_date={day: times}, version=manifest["version"])


def own_channel(app, day, platform, now, *, end=None, enforce_fresh=True, warehouse=None):
    end = end or day
    settings = dict(app)
    # Daily reads share one bounded budget. Keep each SQL capped at 20s.
    settings.update(SOCIAL_ANALYTICS_QUERY_DEADLINE=time.monotonic() + (300 if end != day else 150),
                    SOCIAL_ANALYTICS_STATEMENT_TIMEOUT_MS=20000,
                    SOCIAL_ANALYTICS_QUERY_ATTEMPTS=2)
    if day == end and day >= FIRST_DAY:
        return warehouse_channel(settings, warehouse or warehouse_snapshot(app), day, platform, now, enforce_fresh)
    catalog, errors = read_catalogs(settings, [platform])
    histories, blocked = history(catalog)
    amounts, timestamps, daily_times = defaultdict(int), {}, {}
    for current_day in date_range(day, end):
        report = build_report(settings, {"role": "admin", "_system_report": True},
                          {"start": [current_day], "end": [current_day], "platform": [platform],
                           "group_by": ["platform,drama"], "page_size": ["100"]},
                          ai_backend_only=True, catalog_snapshot=(catalog, errors),
                          include_unconfirmed_sources=platform == "facebook")
        quality = report["quality"]["platforms"][platform]
        if (quality["state"] != "complete" or quality["publication_status"] != "complete"
            or quality.get("unmatched_campaigns") or quality.get("publication_conflicts")):
            raise ComparisonUnavailable("own_source_or_attribution_incomplete")
        raw_sources = {}
        daily_times[current_day] = source_times(settings, current_day, platform,
                                               raw_sources=raw_sources, catalog=catalog)
        blocked.update(unconfirmed_income_drama(catalog, raw_sources, current_day))
        if (enforce_fresh and current_day >= now.astimezone(timezone.utc).date().isoformat()
                and not all(fresh(value, now) for value in daily_times[current_day].values())):
            raise ComparisonUnavailable("own_income_source_stale")
        for site, value in daily_times[current_day].items():
            timestamps[site] = max(timestamps.get(site, ""), value)
        for row in report["all_rows"]:
            if row.get("data_status") != "complete" or row.get("revenue_cents") is None:
                blocked.add(row["drama"])
            else:
                amount = row["revenue_cents"]
                if isinstance(amount, bool) or not isinstance(amount, int):
                    raise ComparisonUnavailable("own_income_not_integer_cents")
                amounts[row["drama"]] += amount
    return dict(amounts=dict(amounts), history=histories, blocked=blocked,
                source_updated_at=timestamps, source_updated_by_date=daily_times)


def compare(candidates, own, day):
    events, pending = [], []
    for row in candidates:
        platform, drama = row["platform"], row["drama"]
        channel = own.get(platform)
        if not channel or channel.get("error") or drama in channel.get("blocked", set()):
            pending.append(dict(platform=platform, drama=drama,
                                reason=(channel or {}).get("error", "drama_attribution_unverified")))
            continue
        # A missing grouped row is zero only after the complete site/date and
        # attribution checks above, never because a source returned no rows.
        amount = channel["amounts"].get(drama, 0)
        if row["drama_app_id"] != APP_ID or row["revenue_cents"] <= 0:
            continue
        previous = channel["history"].get(drama, {})
        count = previous.get("count", 0)
        reasons = []
        if count == 0:
            reasons.append("no_confirmed_publication")
        if row["revenue_cents"] >= MIN_SOCIALKIT_CENTS and amount * 2 < row["revenue_cents"]:
            reasons.append("large_revenue_gap")
        if not reasons:
            continue
        events.append(dict(date=day, app_id=APP_ID, platform=platform, drama=drama,
                           drama_name=row.get("drama_name") or drama,
                           socialkit_revenue_cents=row["revenue_cents"], own_revenue_cents=amount,
                           socialkit_iaa_cents=row.get("iaa_cents"),
                           own_iaa_cents=channel["iaa"].get(drama, 0) if "iaa" in channel else None,
                           revenue_gap_cents=row["revenue_cents"] - amount,
                           period=comparison_period(day), rule_version=RULE_VERSION, reasons=reasons,
                           history_success_count=count, last_published_date=previous.get("last_date"),
                           state=reasons[0]))
    events.sort(key=lambda row: (CHANNELS.index(row["platform"]), -row["socialkit_revenue_cents"], row["drama"]))
    return events, pending


def collect(app, socialkit_root, version=None, report_date=None, now=None, *, enforce_fresh=True):
    now = now or datetime.now(timezone.utc)
    day = date.fromisoformat(report_date or now.date().isoformat()).isoformat()
    period = comparison_period(day)
    candidates, sk = socialkit_candidates(socialkit_root, version, period["start"], now,
                                         end=period["end"], enforce_fresh=enforce_fresh)
    own, errors = {}, {}
    warehouse = None
    for platform in CHANNELS:
        if not any(row["platform"] == platform for row in candidates):
            continue
        try:
            if period["start"] >= FIRST_DAY and warehouse is None:
                warehouse = warehouse_snapshot(app)
            extra = dict(warehouse=warehouse) if warehouse is not None else {}
            own[platform] = own_channel(app, period["start"], platform, now,
                                        end=period["end"], enforce_fresh=enforce_fresh, **extra)
        except Exception as exc:
            # No connector exception text or SQL values enter artifacts/logs.
            code = safe_error(exc)
            own[platform] = {"error": code}
            errors[platform] = code
    events, pending = compare(candidates, own, day)
    return dict(date=day, timezone="UTC", currency="USD", product="Dramawave", app_id=APP_ID,
                period=period, rule_version=RULE_VERSION,
                rules=dict(min_socialkit_revenue_cents=MIN_SOCIALKIT_CENTS, own_ratio_strictly_below="0.50",
                           unpublished_scope="all_confirmed_history_same_platform_content_id"),
                collected_at=datetime.now(timezone.utc).isoformat(), socialkit=sk,
                own_source_updated_at={p: v.get("source_updated_at", {}) for p, v in own.items()},
                own_source_updated_by_date={p: v.get("source_updated_by_date", {}) for p, v in own.items()},
                events=events, pending=pending, errors=errors)
