"""Exact Google asset facts. No ad-group allocation and no asset AF inference.

The Google cache is deliberately separate from the V1 Meta/TikTok facts. All
external reads go through the report's bounded, gated read-only MySQL helper.
"""

from __future__ import annotations

import collections
import json
import math
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


ASSET_PATTERN = re.compile(r"^customers/(\d+)/assets/(\d+)$")
MICROS = Decimal("1000000")
CENT = Decimal("0.01")
GOOGLE_COLUMNS = (
    "id", "resource_id", "asset_type", "impressions", "clicks", "conversions",
    "cost", "dt", "type", "app_id", "account", "app_name", "updated_at",
)


def account_key(value):
    raw = str(value or "").strip().replace("-", "")
    return raw if raw.isascii() and raw.isdigit() else ""


def amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid Google numeric fact") from exc
    if not result.is_finite():
        raise ValueError("non-finite Google numeric fact")
    return result


def whole(value):
    result = amount(value)
    if result != result.to_integral_value() or result < 0:
        raise ValueError("invalid Google count")
    return int(result)


def optional_amount(value):
    """A missing FX candidate is a business gap, not a malformed asset fact."""
    try:
        return amount(value)
    except ValueError:
        return None


def conversion_count(value):
    result = amount(value)
    if result < 0:
        raise ValueError("negative Google conversion count")
    # Google conversions are double-valued, unlike impressions/clicks. SQLite
    # also retains fractional values in pre-release INTEGER-affinity caches.
    numeric = float(result)
    if not math.isfinite(numeric):
        raise ValueError("non-finite Google conversion count")
    return int(result) if result == result.to_integral_value() and result <= 9223372036854775807 else numeric


def usd_cents(value):
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def ensure_schema(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS google_insight (
          dt TEXT NOT NULL, app TEXT NOT NULL, account TEXT NOT NULL,
          resource_id TEXT NOT NULL, row_type INTEGER NOT NULL,
          asset_type INTEGER NOT NULL, cost_micros TEXT NOT NULL,
          impressions INTEGER NOT NULL, clicks INTEGER NOT NULL,
          conversions REAL NOT NULL, source_id TEXT NOT NULL,
          updated_at TEXT NOT NULL, custom_source_id INTEGER,
          mapping_status TEXT NOT NULL, currency TEXT NOT NULL,
          fx_rate TEXT, fx_status TEXT NOT NULL, usd_amount TEXT,
          PRIMARY KEY(dt,app,account,resource_id,row_type)
        );
        CREATE INDEX IF NOT EXISTS google_insight_material
          ON google_insight(custom_source_id,app,dt);
        CREATE TABLE IF NOT EXISTS google_month_refresh (
          month TEXT PRIMARY KEY, refreshed_at TEXT NOT NULL,
          row_count INTEGER NOT NULL, metadata_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS google_asset_mapping (
          resource_id TEXT PRIMARY KEY, custom_source_id INTEGER,
          mapping_status TEXT NOT NULL, mapping_rows INTEGER NOT NULL,
          provenance_json TEXT NOT NULL, fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS google_asset_launch (
          resource_id TEXT NOT NULL, app TEXT NOT NULL,
          first_delivery_dt TEXT NOT NULL, fetched_at TEXT NOT NULL,
          PRIMARY KEY(resource_id,app)
        );
        """
    )


def load_app_config(report):
    rows = report.run_mysql(
        """SELECT name,app_id FROM kunlunads_dev.ads_apps_setting
        WHERE LOWER(name) IN ('opay','opay ngn','opaypakistan','opay pakistan')
        ORDER BY id""", timeout=45,
    )
    result = {}
    for name, external_id in rows:
        app = report.app_key(name)
        external_id = report.text(external_id)
        if not app or not external_id:
            continue
        if external_id in result and result[external_id] != app:
            raise RuntimeError("ambiguous Google OPay app configuration")
        result[external_id] = app
    if set(result.values()) != set(report.APP_ORDER):
        raise RuntimeError("missing Google OPay app configuration")
    return result


def fetch_month(report, month, app_config):
    start, end = report.month_bounds(month)
    sql = """
    SELECT id,resource_id,asset_type,impressions,clicks,conversions,cost,dt,type,
      app_id,account,app_name,DATE_FORMAT(updated_at,'%%Y-%%m-%%d %%H:%%i:%%s')
    FROM kunlunads_dev.ads_google_insights FORCE INDEX(idx_app_id)
    WHERE app_id IN (%s) AND dt BETWEEN %s AND %s
      AND (type=0 OR (type=3 AND asset_type IN (2,4)))
    ORDER BY dt,account,type,id
    """ % (
        ",".join(report.sql_quote(key) for key in sorted(app_config)),
        report.sql_quote(start), report.sql_quote(end),
    )
    return [dict(zip(GOOGLE_COLUMNS, row)) for row in report.run_mysql(sql, timeout=240)]


def collapse_mappings(resource_ids, mapping_rows):
    """An asset repeated in multiple ads is still one asset fact."""
    grouped = collections.defaultdict(list)
    resources = set(resource_ids)
    for row in mapping_rows:
        if row["asset_name"] in resources:  # retain byte-exact resource identity
            grouped[row["asset_name"]].append(row)
    result = {}
    for resource in sorted(resources):
        rows = grouped[resource]
        targets = {str(row["resource_id"]).strip() for row in rows}
        valid = set()
        invalid_chain = False
        for row in rows:
            target = str(row["resource_id"]).strip()
            if (target.isascii() and target.isdigit() and int(target) > 0
                    and str(row["source_type"]) == "3"
                    and str(row["source_custom_id"]).strip() == target):
                valid.add(int(target))
            else:
                invalid_chain = True
        if not rows:
            status, custom_id = "unmapped", None
        elif len(targets) != 1 or len(valid) > 1:
            status, custom_id = "ambiguous", None
        elif len(valid) == 1 and not invalid_chain:
            status, custom_id = "exact", next(iter(valid))
        else:
            status, custom_id = "invalid_source", None
        result[resource] = {
            "custom_source_id": custom_id, "mapping_status": status,
            "mapping_rows": len(rows),
            "provenance": sorted({
                (str(row["source_id"]), str(row["resource_id"]), str(row["source_type"]), str(row["source_custom_id"]))
                for row in rows
            }),
        }
    return result


def fetch_mappings(report, resource_ids):
    rows = []
    columns = ("asset_name", "source_id", "resource_id", "source_type", "source_custom_id")
    for part in report.chunked(sorted(set(resource_ids)), size=300):
        sql = """
        SELECT r.asset_name,r.source_id,r.resource_id,s.source_type,s.source_id
        FROM kunlunads_dev.ads_google_resource_mapping r
        LEFT JOIN kunlunads_dev.ads_source s ON s.id=r.source_id
        WHERE r.asset_name IN (%s)
        """ % ",".join(report.sql_quote(key) for key in part)
        rows.extend(dict(zip(columns, row)) for row in report.run_mysql(sql, timeout=90))
    return collapse_mappings(resource_ids, rows)


def fetch_currencies(report, accounts):
    result = collections.defaultdict(set)
    aliases = set(accounts)
    for account in accounts:
        key = account_key(account)
        aliases.add(key)
        if len(key) == 10:
            aliases.add(key[:3] + "-" + key[3:6] + "-" + key[6:])
    for part in report.chunked(sorted(aliases - {""}), size=300):
        sql = """SELECT account_id,currency FROM kunlunads_dev.ads_accounts_setting
        WHERE platform_id=1 AND account_id IN (%s)""" % ",".join(
            report.sql_quote(key) for key in part
        )
        for account, currency in report.run_mysql(sql, timeout=60):
            result[account_key(account)].add(report.text(currency).upper())
    return {key: next(iter(values)) if len(values) == 1 else "" for key, values in result.items()}


def fetch_fx_rows(report, month):
    start, end = report.month_bounds(month)
    columns = ("dt", "account", "currency", "exchange_rate", "last_exchange_rate", "spend", "spend_usd")
    sql = """
    SELECT dt,account_id,currency,exchange_rate,last_exchange_rate,spend,spend_usd
    FROM kunlunads_dev.ads_platform_report_items FORCE INDEX(pdpcic)
    WHERE platform=1 AND dt BETWEEN %s AND %s
      AND product IN ('OPay','OPay NGN','OPayPakistan','OPay Pakistan')
    ORDER BY dt,account_id,id
    """ % (report.sql_quote(start), report.sql_quote(end))
    return [dict(zip(columns, row)) for row in report.run_mysql(sql, timeout=120)]


def resolve_fx(currencies, rate_rows):
    """Recover a stored rate, never an invented spend-ratio scaling factor.

Both exchange_rate columns are candidates: the warehouse can advance the
current column before historical spend_usd is recomputed. Require one rate
that reconciles every positive daily report row within one USD cent.
"""
    grouped = collections.defaultdict(list)
    for row in rate_rows:
        grouped[(str(row["dt"]), account_key(row["account"]))].append(row)
    result = {}
    for key, rows in grouped.items():
        row_currencies = {str(row["currency"]).strip().upper() for row in rows}
        configured = currencies.get(key[1], "")
        currency = next(iter(row_currencies)) if len(row_currencies) == 1 else ""
        if not currency or (configured and currency != configured):
            result[key] = (None, "currency_conflict", currency)
            continue
        if currency == "USD":
            result[key] = (Decimal("1"), "usd_account", currency)
            continue
        candidates = None
        positive = 0
        for row in rows:
            native, dollars = optional_amount(row["spend"]), optional_amount(row["spend_usd"])
            if native is not None and native <= 0:
                continue
            if native is None or dollars is None or dollars < 0:
                positive += 1
                candidates = set()
                continue
            positive += 1
            matches = set()
            for name in ("exchange_rate", "last_exchange_rate"):
                rate = optional_amount(row.get(name))
                if rate is not None and rate > 0 and abs((native / rate).quantize(CENT, rounding=ROUND_HALF_UP) - dollars) <= CENT:
                    matches.add(rate)
            candidates = matches if candidates is None else candidates & matches
        if not positive:
            result[key] = (None, "fx_missing", currency)
        elif candidates and len(candidates) == 1:
            result[key] = (next(iter(candidates)), "historical_reconciled", currency)
        else:
            result[key] = (None, "fx_ambiguous" if candidates else "fx_unreconciled", currency)
    return result


def normalize_rows(report, sources, app_config, mappings, dimensions, currencies, rates):
    normalized = {}
    duplicates = 0
    for source in sources:
        app = app_config.get(report.text(source["app_id"]))
        if not app:
            raise ValueError("out-of-scope Google app id")
        named_app = report.app_key(source.get("app_name"))
        if named_app and named_app != app:
            raise ValueError("conflicting Google app identity")
        day = report.validate_date(source["dt"])
        account = account_key(source["account"])
        if not account:
            raise ValueError("invalid Google account identity")
        row_type, asset_type = whole(source["type"]), whole(source["asset_type"])
        if row_type != 0 and not (row_type == 3 and asset_type in (2, 4)):
            continue
        resource = report.text(source["resource_id"])
        custom_id, mapping_status = None, "campaign"
        if row_type == 3:
            match = ASSET_PATTERN.fullmatch(resource)
            if not match or match.group(1) != account:
                raise ValueError("invalid Google asset resource/account identity")
            mapping = mappings.get(resource, {})
            custom_id = mapping.get("custom_source_id")
            mapping_status = mapping.get("mapping_status", "unmapped")
            dimension = dimensions.get(custom_id)
            if mapping_status == "exact":
                if not dimension:
                    mapping_status = "missing_material"
                elif report.text(dimension.get("product")).casefold() != "opay":
                    mapping_status = "out_of_scope"
                elif report.integer(dimension.get("material_type")) != {2: 2, 4: 1}[asset_type]:
                    mapping_status = "type_mismatch"
        elif not resource:
            resource = "campaign:" + str(whole(source["id"]))
        raw_cost = amount(source["cost"])
        if raw_cost < 0:
            raise ValueError("negative Google cost requires source reconciliation")
        currency = currencies.get(account, "")
        rate, fx_status, historical_currency = rates.get(
            (day, account), (None, "fx_missing", currency)
        )
        currency = historical_currency or currency
        if (day, account) not in rates and currency == "USD":
            rate, fx_status = Decimal("1"), "usd_account"
        native = raw_cost / MICROS
        # A measured zero cost is known even when no historical FX row exists.
        usd = Decimal("0") if native == 0 else (native / rate if rate else None)
        if native == 0 and rate is None:
            fx_status = "zero_cost"
        target = {
            "dt": day, "app": app, "account": account, "resource_id": resource,
            "row_type": row_type, "asset_type": asset_type,
            "cost_micros": str(raw_cost), "impressions": whole(source["impressions"]),
            "clicks": whole(source["clicks"]), "conversions": conversion_count(source["conversions"]),
            "source_id": str(source["id"]), "updated_at": report.text(source["updated_at"]),
            "custom_source_id": custom_id, "mapping_status": mapping_status,
            "currency": currency, "fx_rate": str(rate) if rate else None,
            "fx_status": fx_status, "usd_amount": str(usd) if usd is not None else None,
        }
        key = (day, app, account, resource, row_type)
        if key in normalized:
            previous = normalized[key]
            facts = ("asset_type", "cost_micros", "impressions", "clicks", "conversions")
            if any(previous[name] != target[name] for name in facts):
                raise ValueError("conflicting duplicate Google asset-day facts")
            duplicates += 1
            continue
        normalized[key] = target
    return list(normalized.values()), duplicates


def store_month(report, connection, month, rows, mappings, metadata):
    start, end = report.month_bounds(month)
    now = report.bj_now().isoformat()
    with connection:
        connection.execute("DELETE FROM google_insight WHERE dt BETWEEN ? AND ?", (start, end))
        columns = (
            "dt", "app", "account", "resource_id", "row_type", "asset_type", "cost_micros",
            "impressions", "clicks", "conversions", "source_id", "updated_at",
            "custom_source_id", "mapping_status", "currency", "fx_rate", "fx_status", "usd_amount",
        )
        connection.executemany(
            "INSERT INTO google_insight(%s) VALUES(%s)" % (
                ",".join(columns), ",".join("?" for _ in columns)),
            ([row[name] for name in columns] for row in rows),
        )
        connection.executemany(
            "INSERT OR REPLACE INTO google_asset_mapping VALUES(?,?,?,?,?,?)",
            ((resource, entry["custom_source_id"], entry["mapping_status"], entry["mapping_rows"],
              json.dumps(entry["provenance"], separators=(",", ":")), now)
             for resource, entry in mappings.items()),
        )
        connection.execute(
            "INSERT OR REPLACE INTO google_month_refresh VALUES(?,?,?,?)",
            (month, now, len(rows), json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
        )


def refresh_month(report, connection, month):
    report.assert_read_only()
    config = load_app_config(report)
    sources = fetch_month(report, month, config)
    resources = {report.text(row["resource_id"]) for row in sources if report.integer(row["type"]) == 3}
    mappings = fetch_mappings(report, resources)
    ids = {entry["custom_source_id"] for entry in mappings.values() if entry["custom_source_id"]}
    # Only fetch missing dimensions during a Google-only backfill. Existing
    # Meta/TikTok material metadata must not be refreshed as a side effect.
    report.ensure_dimensions(connection, [
        {"platform": 0, "source_id": 0, "resource_id": key} for key in ids
    ])
    dimensions = report.material_dim_map(connection, ids)
    currencies = fetch_currencies(report, {row["account"] for row in sources})
    fx_rows = fetch_fx_rows(report, month)
    rates = resolve_fx(currencies, fx_rows)
    rows, duplicate_rows = normalize_rows(report, sources, config, mappings, dimensions, currencies, rates)
    metadata = {
        "source": "ads_google_insights", "asset_type": [2, 4],
        "app_config": config, "duplicate_rows": duplicate_rows,
        "historical_fx_rows": len(fx_rows), "source_rows": len(sources),
        "source_date_start": min((row["dt"] for row in rows), default=None),
        "source_date_end": max((row["dt"] for row in rows), default=None),
    }
    store_month(report, connection, month, rows, mappings, metadata)
    return {"month": month, "google_rows": len(rows), "google_assets": len(resources),
            "duplicate_rows": duplicate_rows, "historical_fx_rows": len(fx_rows)}


def month_aggregates(report, connection, month):
    start, end = report.month_bounds(month)
    refreshed = connection.execute("SELECT * FROM google_month_refresh WHERE month=?", (month,)).fetchone()
    totals = {}
    audits = {}
    for app in report.APP_ORDER:
        totals[(1, app)] = {"spend_cents": None, "impressions": 0, "clicks": 0,
                            "installs": None, "source_row_count": 0, "_usd": Decimal("0"), "_missing": 0}
        audit = report.blank_scope_audit()
        audit.update({"refreshed": bool(refreshed), "fx_missing_rows": 0, "platform_fx_missing_rows": 0,
                      "baseline_missing_account_days": 0,
                      "mapping_status_counts": collections.Counter(), "fx_missing_native_spend": collections.defaultdict(Decimal),
                      "platform_fx_missing_native_spend": collections.defaultdict(Decimal),
                      "incomplete_material_count": 0, "asset_count": 0, "_exact_usd": Decimal("0"),
                      "_ambiguous_usd": Decimal("0"), "_invalid_usd": Decimal("0"), "_outside_usd": Decimal("0")})
        audits[(1, app)] = audit
    materials = {}
    asset_sets = collections.defaultdict(set)
    campaign_days = collections.defaultdict(set)
    asset_days = collections.defaultdict(set)
    for source in connection.execute("SELECT * FROM google_insight WHERE dt BETWEEN ? AND ? ORDER BY dt", (start, end)):
        row = dict(source)
        scope = (1, row["app"])
        total, audit = totals[scope], audits[scope]
        usd = amount(row["usd_amount"]) if row["usd_amount"] is not None else None
        if row["row_type"] == 0:
            campaign_days[scope].add((row["dt"], row["account"]))
            total["source_row_count"] += 1
            total["impressions"] += row["impressions"]
            total["clicks"] += row["clicks"]
            if usd is None:
                total["_missing"] += 1
                audit["platform_fx_missing_native_spend"][row["currency"] or "UNKNOWN"] += amount(row["cost_micros"]) / MICROS
            else:
                total["_usd"] += usd
            continue
        audit["source_row_count"] += 1
        if amount(row["cost_micros"]) > 0 or row["impressions"] or row["clicks"]:
            asset_days[scope].add((row["dt"], row["account"]))
        asset_sets[scope].add(row["resource_id"])
        status = row["mapping_status"]
        audit["mapping_status_counts"][status] += 1
        if usd is None:
            audit["fx_missing_rows"] += 1
            audit["fx_missing_native_spend"][row["currency"] or "UNKNOWN"] += amount(row["cost_micros"]) / MICROS
        if status != "exact":
            audit["invalid_row_count"] += 1
            if usd is not None:
                audit[{"ambiguous": "_ambiguous_usd", "out_of_scope": "_outside_usd"}.get(status, "_invalid_usd")] += usd
            continue
        audit["exact_row_count"] += 1
        if usd is not None:
            audit["_exact_usd"] += usd
        key = (1, row["app"], row["custom_source_id"])
        target = materials.setdefault(key, {
            "platform": 1, "app": row["app"], "custom_source_id": row["custom_source_id"],
            "spend_cents": None, "impressions": 0, "clicks": 0, "installs": None,
            "af_d0_count": None, "platform_conversions": Decimal("0"), "source_row_count": 0,
            "ad_days": 0, "resource_tags": set(), "first_auto_publish_dt": "",
            "first_delivery_dt": "", "asset_resources": set(), "fx_sources": set(),
            "_usd": Decimal("0"), "_missing": 0,
        })
        target["impressions"] += row["impressions"]
        target["clicks"] += row["clicks"]
        target["platform_conversions"] += amount(row["conversions"])
        target["source_row_count"] += 1
        target["ad_days"] += 1
        target["asset_resources"].add(row["resource_id"])
        target["fx_sources"].add(row["fx_status"])
        if amount(row["cost_micros"]) > 0 or row["impressions"] or row["clicks"]:
            target["first_delivery_dt"] = min(target["first_delivery_dt"], row["dt"]) if target["first_delivery_dt"] else row["dt"]
        if usd is None:
            target["_missing"] += 1
        else:
            target["_usd"] += usd
    for scope, total in totals.items():
        missing_baseline = len(asset_days[scope] - campaign_days[scope])
        total["ctr_complete"] = bool(refreshed) and missing_baseline == 0
        # An empty *successfully read* campaign scope is a measured zero; a
        # non-refreshed scope or any unknown currency portion stays unknown.
        if refreshed and not total["_missing"] and not missing_baseline:
            total["spend_cents"] = usd_cents(total["_usd"])
        if missing_baseline or not refreshed:
            total["impressions"] = None
            total["clicks"] = None
        audit = audits[scope]
        audit["baseline_missing_account_days"] = missing_baseline
        audit["platform_fx_missing_rows"] = total["_missing"]
        audit["platform_spend_cents"] = total["spend_cents"]
        audit["exact_spend_cents"] = usd_cents(audit.pop("_exact_usd"))
        audit["ambiguous_spend_cents"] = usd_cents(audit.pop("_ambiguous_usd"))
        audit["invalid_mapping_spend_cents"] = usd_cents(audit.pop("_invalid_usd"))
        audit["out_of_scope_spend_cents"] = usd_cents(audit.pop("_outside_usd"))
        audit["asset_count"] = len(asset_sets[scope])
        for field in ("fx_missing_native_spend", "platform_fx_missing_native_spend"):
            audit[field] = {
                currency: float(value.quantize(CENT, rounding=ROUND_HALF_UP))
                for currency, value in audit[field].items()
            }
        total.pop("_usd")
        total.pop("_missing")
    for material in materials.values():
        material["platform_conversions"] = conversion_count(material["platform_conversions"])
        if not material["_missing"]:
            material["spend_cents"] = usd_cents(material["_usd"])
        else:
            audits[(1, material["app"])]["incomplete_material_count"] += 1
        material.pop("_usd")
        material.pop("_missing")
    return totals, audits, list(materials.values())


def refresh_launch_dates(report, connection, selected_rows, app_config):
    """Look up the earliest observable asset delivery, not mapping creation."""
    ids = {row["custom_source_id"] for row in selected_rows if row["channel"] == "Google"}
    if not ids:
        return
    resources = set()
    for part in report.chunked(sorted(ids)):
        for row in connection.execute(
            "SELECT resource_id FROM google_asset_mapping WHERE custom_source_id IN (%s) AND mapping_status='exact'"
            % ",".join("?" for _ in part), part,
        ):
            resources.add(row[0])
    now = report.bj_now().isoformat()
    fetched = []
    for part in report.chunked(sorted(resources), size=150):
        sql = """
        SELECT resource_id,app_id,MIN(dt)
        FROM kunlunads_dev.ads_google_insights FORCE INDEX(resource_id)
        WHERE resource_id IN (%s) AND app_id IN (%s) AND type=3
          AND asset_type IN (2,4) AND (cost>0 OR impressions>0 OR clicks>0)
        GROUP BY resource_id,app_id
        """ % (
            ",".join(report.sql_quote(key) for key in part),
            ",".join(report.sql_quote(key) for key in sorted(app_config)),
        )
        for resource, external_id, day in report.run_mysql(sql, timeout=120):
            if resource in resources and external_id in app_config:
                fetched.append((resource, app_config[external_id], report.validate_date(day), now))
    with connection:
        connection.executemany("INSERT OR REPLACE INTO google_asset_launch VALUES(?,?,?,?)", fetched)


def earliest_launch(connection, custom_id, app, fallback):
    row = connection.execute(
        """SELECT MIN(l.first_delivery_dt) FROM google_asset_launch l
        JOIN google_asset_mapping m ON m.resource_id=l.resource_id
        WHERE m.custom_source_id=? AND m.mapping_status='exact' AND l.app=?""", (custom_id, app),
    ).fetchone()
    return min(fallback, row[0]) if row and row[0] else fallback
