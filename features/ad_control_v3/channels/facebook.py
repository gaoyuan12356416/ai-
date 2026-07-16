"""Read-only Facebook scope discovery for Campaign, Ad Set and Ad.

The query shape is server-owned and bounded. Every source insight query contains
the mandatory data-source, platform, one exact product, date-range and optimizer
predicates. Products are queried separately and only fields required by the
stored rules/selection are projected. This keeps the reviewed ``dpdo`` index
usable and detects cross-product object ambiguity before rule evaluation.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence, Tuple

from ..catalog import facebook_field_catalog
from ..errors import AdControlV3Error
from ..schemas import OBJECT_LEVELS, parse_iso_date, positive_int
from .base import ChannelAdapter


LEVEL_COLUMNS = {
    "campaign": "campaign_id",
    "adset": "adset_id",
    "ad": "ad_id",
}

# Reviewed source columns only; no user value is interpolated into SQL.
COMMON_CONTEXT_FIELDS = (
    "series_code", "app", "app_id", "os_type", "country", "language",
    "country_group", "drama_language", "bid_type", "page_id", "task_type",
)
AD_CONTEXT_FIELDS = (
    "resource_id", "resource_name", "source_id", "w2a_page_id", "ad_type",
    "category", "resource_tag", "source_type", "resource_type",
    "created_data_id", "task_id",
)
SUM_METRIC_FIELDS = (
    "spend", "impressions", "clicks", "installs", "purchase", "revenue",
    "day1_retain", "retain_install", "events", "atc", "delivery_cnt",
    "af_installs", "af_revenue", "ad_impression", "ad_impression_revenue",
)

TIME_FIELD_SOURCES = {
    "latest_auto_publish_dt": "auto_publish_dt",
    "latest_resource_created_at": "resource_created_at",
    "latest_spend_at": "spend_at",
}

COMPUTED_METRIC_DEPENDENCIES = {
    "ctr": ("clicks", "impressions"),
    "cpm": ("spend", "impressions"),
    "cpc": ("spend", "clicks"),
    "cpi": ("spend", "installs"),
    "purchase_cpa": ("spend", "purchase"),
    "roas": ("revenue", "spend"),
    "retention_rate": ("day1_retain", "retain_install"),
    "af_roas": ("af_revenue", "spend"),
    "ad_impression_roas": ("ad_impression_revenue", "spend"),
}

# MySQL 5.7 server-side circuit breaker. Production measurements showed the
# narrow dpdo query can legitimately approach four seconds, so keep the server
# limit at eight seconds and the source socket read timeout strictly above it.
SOURCE_QUERY_MAX_EXECUTION_TIME_MS = 8000


def _context_aggregate(field: str) -> str:
    aggregate = (
        "GROUP_CONCAT(DISTINCT NULLIF(CAST(s.{0} AS CHAR), '') "
        "ORDER BY CAST(s.{0} AS CHAR) SEPARATOR '\\n')"
    ).format(field)
    return (
        "{0} AS {1}, COUNT(DISTINCT NULLIF(CAST(s.{1} AS CHAR), '')) AS {1}_count, "
        "OCTET_LENGTH({0}) AS {1}_concat_bytes"
    ).format(aggregate, field)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _ratio(numerator: Any, denominator: Any, multiplier: int = 1) -> Any:
    bottom = _decimal(denominator)
    if bottom <= 0:
        return None
    return float((_decimal(numerator) / bottom) * multiplier)


def _dict_rows(rows: Iterable[Any], columns: Sequence[str]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for row in rows or []:
        if isinstance(row, Mapping):
            # Keep the projection contract even for dict cursors/test doubles;
            # unselected source fields must not leak into candidate snapshots.
            result.append({key: row.get(key) for key in columns})
        else:
            result.append({key: row[index] if index < len(row) else None for index, key in enumerate(columns)})
    return result


def _projection_for_level(
    object_level: str,
    required_fields: Any,
) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]:
    """Return validated context/time/raw/computed projections.

    ``required_fields`` is never accepted from an API payload. The service
    derives it from validated stored rule conditions and Top-N selection. The
    adapter still validates it independently so another caller cannot turn
    field names into SQL identifiers.
    """

    catalog = {
        str(item.get("key") or ""): item
        for item in facebook_field_catalog(object_level)
        if item.get("filterable") and item.get("previewable")
    }
    if required_fields is None:
        # Compatibility for direct adapter callers. Production service calls
        # always supply an explicit projection (including an empty scope one).
        requested = set(catalog)
    elif not isinstance(required_fields, (list, tuple, set, frozenset)):
        raise AdControlV3Error(
            "required_field_not_supported",
            "required_fields must be a server-owned field list",
        )
    else:
        requested = {str(value or "").strip() for value in required_fields}
        if "" in requested:
            raise AdControlV3Error(
                "required_field_not_supported",
                "required_fields contains an empty field",
            )
    unknown = sorted(requested - set(catalog))
    if unknown:
        raise AdControlV3Error(
            "required_field_not_supported",
            "required_fields contains an unsupported field",
            details={"fields": unknown, "object_level": object_level},
        )

    contexts = tuple(
        field
        for field in COMMON_CONTEXT_FIELDS + (AD_CONTEXT_FIELDS if object_level == "ad" else ())
        if field in requested
    )
    times = tuple(field for field in TIME_FIELD_SOURCES if field in requested)
    computed = tuple(field for field in COMPUTED_METRIC_DEPENDENCIES if field in requested)
    raw_metrics = {
        field for field in SUM_METRIC_FIELDS if field in requested
    }
    for field in computed:
        raw_metrics.update(COMPUTED_METRIC_DEPENDENCIES[field])
    return contexts, times, tuple(field for field in SUM_METRIC_FIELDS if field in raw_metrics), computed


class FacebookAdapter(ChannelAdapter):
    channel = "facebook"
    enabled = True

    def __init__(
        self,
        query_executor: Callable[[str, Sequence[Any]], Iterable[Any]],
        *,
        source_database: str = "kunlunads_dev",
        max_window_days: int = 31,
        max_rows_per_product: int = 10000,
        schema_validator: Any = None,
        max_products: int = 20,
        max_total_candidates: int = 20000,
        query_deadline_seconds: float = 15.0,
    ) -> None:
        self._query_executor = query_executor
        if source_database != "kunlunads_dev":
            raise AdControlV3Error("unsafe_source_database", "source database is fixed")
        self.source_database = source_database
        self.max_window_days = int(max_window_days)
        self.max_rows_per_product = int(max_rows_per_product)
        self._schema_validator = schema_validator
        self._schema_validated = False
        self._schema_lock = threading.Lock()
        self.max_products = max(1, min(20, int(max_products)))
        self.max_total_candidates = max(1, min(20000, int(max_total_candidates)))
        self.query_deadline_seconds = max(1.0, min(30.0, float(query_deadline_seconds)))

    def _ensure_schema(self) -> None:
        if self._schema_validated or self._schema_validator is None:
            return
        with self._schema_lock:
            if not self._schema_validated:
                self._schema_validator()
                self._schema_validated = True

    def capabilities(self) -> Dict[str, Any]:
        return {
            "channel": self.channel,
            "enabled": True,
            "object_levels": list(OBJECT_LEVELS),
            "observe": True,
            "live_pause": False,
            "live_copy": False,
            "fields": facebook_field_catalog(),
        }

    @staticmethod
    def _validate_product(product: Any) -> str:
        text = str(product or "").strip()
        if not text or len(text) > 128 or "\x00" in text:
            raise AdControlV3Error("invalid_product_scope", "invalid exact product value")
        return text

    def _query_for_level(
        self,
        object_level: str,
        required_fields: Any = None,
        *,
        include_timezone: bool = False,
    ) -> Tuple[str, Sequence[str]]:
        if object_level not in LEVEL_COLUMNS:
            raise AdControlV3Error("validation_error", "unsupported object level")
        context_fields, time_fields, raw_metric_fields, _ = _projection_for_level(
            object_level,
            required_fields,
        )
        object_id_column = LEVEL_COLUMNS[object_level]
        account_expr = (
            "CASE WHEN LEFT(LOWER(CAST(s.ad_account_id AS CHAR)), 4) = 'act_' "
            "THEN SUBSTRING(LOWER(CAST(s.ad_account_id AS CHAR)), 5) "
            "ELSE LOWER(CAST(s.ad_account_id AS CHAR)) END"
        )
        select_columns = [
            account_expr + " AS ad_account_id",
            "CAST(s.{0} AS CHAR) AS object_id".format(object_id_column),
            "'' AS object_name",
        ]
        columns = ["ad_account_id", "object_id", "object_name"]
        if object_level == "campaign":
            select_columns.extend((
                "CAST(s.campaign_id AS CHAR) AS campaign_id",
                "'' AS adset_id",
                "'' AS ad_id",
                "1 AS campaign_parent_count",
                "0 AS adset_parent_count",
            ))
        elif object_level == "adset":
            select_columns.extend((
                "COALESCE(MAX(CAST(s.campaign_id AS CHAR)), '') AS campaign_id",
                "CAST(s.adset_id AS CHAR) AS adset_id",
                "'' AS ad_id",
                "COUNT(DISTINCT CAST(s.campaign_id AS CHAR)) AS campaign_parent_count",
                "1 AS adset_parent_count",
            ))
        else:
            select_columns.extend((
                "COALESCE(MAX(CAST(s.campaign_id AS CHAR)), '') AS campaign_id",
                "COALESCE(MAX(CAST(s.adset_id AS CHAR)), '') AS adset_id",
                "CAST(s.ad_id AS CHAR) AS ad_id",
                "COUNT(DISTINCT CAST(s.campaign_id AS CHAR)) AS campaign_parent_count",
                "COUNT(DISTINCT CAST(s.adset_id AS CHAR)) AS adset_parent_count",
            ))
        columns.extend((
            "campaign_id", "adset_id", "ad_id",
            "campaign_parent_count", "adset_parent_count",
        ))
        select_columns.extend(("%s AS product", "%s AS optimizer_id"))
        columns.extend(("product", "optimizer_id"))

        for field in context_fields:
            select_columns.append(_context_aggregate(field))
            columns.extend((field, field + "_count", field + "_concat_bytes"))
        if context_fields:
            select_columns.append("@@session.group_concat_max_len AS context_concat_limit")
        else:
            select_columns.append("0 AS context_concat_limit")
        columns.append("context_concat_limit")
        for field in time_fields:
            select_columns.append(
                "MAX(s.{0}) AS {1}".format(TIME_FIELD_SOURCES[field], field)
            )
            columns.append(field)
        for field in raw_metric_fields:
            select_columns.append("COALESCE(SUM(s.{0}), 0) AS {0}".format(field))
            columns.append(field)

        timezone_join = ""
        if include_timezone:
            setting_source_expr = (
                "CASE WHEN LEFT(LOWER(CAST(x.account_id AS CHAR)), 4) = 'act_' "
                "THEN SUBSTRING(LOWER(CAST(x.account_id AS CHAR)), 5) "
                "ELSE LOWER(CAST(x.account_id AS CHAR)) END"
            )
            timezone_join = """
                LEFT JOIN (
                  SELECT {setting_source_expr} AS normalized_account,
                         MAX(CAST(x.time_zone AS CHAR)) AS time_zone,
                         COUNT(*) AS settings_row_count,
                         COUNT(DISTINCT NULLIF(CAST(x.time_zone AS CHAR), '')) AS timezone_count
                  FROM `kunlunads_dev`.ads_accounts_setting x
                  WHERE x.platform_id = 1
                  GROUP BY {setting_source_expr}
                ) a ON a.normalized_account = {account_expr}
            """.format(setting_source_expr=setting_source_expr, account_expr=account_expr)
            select_columns.extend((
                "COALESCE(MAX(CAST(a.time_zone AS CHAR)), '') AS account_timezone",
                "COALESCE(MAX(a.settings_row_count), 0) AS settings_row_count",
                "COALESCE(MAX(a.timezone_count), 0) AS settings_timezone_count",
            ))
        else:
            select_columns.extend((
                "'' AS account_timezone",
                "0 AS settings_row_count",
                "0 AS settings_timezone_count",
            ))
        columns.extend(("account_timezone", "settings_row_count", "settings_timezone_count"))

        # Column names are server-owned constants; all user values are bound.
        # Do not add optional WHERE fragments that could omit a mandatory bound.
        sql = """
            SELECT /*+ MAX_EXECUTION_TIME({max_execution_time_ms}) */
              {select_columns}
            FROM `kunlunads_dev`.ads_custom_source_insight s FORCE INDEX (dpdo)
            {timezone_join}
            WHERE s.data_source IN (0, 6)
              AND s.platform = %s
              AND s.product = %s
              AND BINARY s.product = BINARY %s
              AND s.dt BETWEEN %s AND %s
              AND s.optimizer = %s
              AND s.{object_id} IS NOT NULL
              AND CAST(s.{object_id} AS CHAR) <> ''
            GROUP BY {account_expr},
                     CAST(s.{object_id} AS CHAR)
            ORDER BY CAST(s.{object_id} AS CHAR)
            LIMIT %s
        """.format(
            object_id=object_id_column,
            account_expr=account_expr,
            select_columns=", ".join(select_columns),
            timezone_join=timezone_join,
            max_execution_time_ms=SOURCE_QUERY_MAX_EXECUTION_TIME_MS,
        )
        return " ".join(sql.split()), tuple(columns)

    def discover(self, scope: Mapping[str, Any]) -> List[Dict[str, Any]]:
        self._ensure_schema()
        object_level = str(scope.get("object_level") or "").strip().lower()
        products = [self._validate_product(value) for value in (scope.get("products") or [])]
        if not products:
            raise AdControlV3Error("invalid_product_scope", "at least one exact product is required")
        if len(products) > self.max_products:
            raise AdControlV3Error(
                "product_scope_too_large",
                "product count exceeds the safe query limit",
                details={"max_products": self.max_products},
            )
        if len(set(products)) != len(products):
            products = list(dict.fromkeys(products))
        optimizer_id = positive_int(scope.get("optimizer_id"), "optimizer_id")
        date_from = parse_iso_date(scope.get("date_from"), "date_from")
        date_to = parse_iso_date(scope.get("date_to"), "date_to")
        if date_to < date_from:
            raise AdControlV3Error("validation_error", "date_to must not precede date_from")
        if date_to - date_from > timedelta(days=self.max_window_days - 1):
            raise AdControlV3Error(
                "query_window_too_large",
                "insight window exceeds the safe limit",
                details={"max_days": self.max_window_days},
            )
        requested_timezones = {
            str(value or "").strip()
            for value in (scope.get("account_timezones") or [])
            if str(value or "").strip()
        }
        context_fields, _, raw_metric_fields, computed_fields = _projection_for_level(
            object_level,
            scope.get("required_fields") if "required_fields" in scope else None,
        )
        sql, columns = self._query_for_level(
            object_level,
            scope.get("required_fields") if "required_fields" in scope else None,
            include_timezone=bool(requested_timezones),
        )
        by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        deadline = time.monotonic() + self.query_deadline_seconds
        for product in products:
            if time.monotonic() >= deadline:
                raise AdControlV3Error(
                    "scope_query_deadline_exceeded",
                    "soft total candidate scan deadline exceeded",
                    status=503,
                    details={"deadline_seconds": self.query_deadline_seconds},
                )
            params = (
                product,
                optimizer_id,
                0,
                product,
                product,
                date_from.isoformat(),
                date_to.isoformat(),
                optimizer_id,
                self.max_rows_per_product,
            )
            rows = _dict_rows(self._query_executor(sql, params), columns)
            if time.monotonic() >= deadline:
                raise AdControlV3Error(
                    "scope_query_deadline_exceeded",
                    "soft total candidate scan deadline exceeded",
                    status=503,
                    details={"deadline_seconds": self.query_deadline_seconds},
                )
            if len(rows) >= self.max_rows_per_product:
                raise AdControlV3Error(
                    "scope_query_truncated",
                    "candidate query reached the safe row limit",
                    status=409,
                    details={"product": product, "limit": self.max_rows_per_product},
                )
            for row in rows:
                # SQL returns the canonical value after exactly one leading
                # ``act_`` removal. Do not strip again at the adapter boundary.
                account_id = str(row.get("ad_account_id") or "").strip().lower()
                object_id = str(row.get("object_id") or "").strip()
                if not account_id or not object_id:
                    continue
                timezone_name = str(row.get("account_timezone") or "").strip()
                row_product = str(row.get("product") or product).strip()
                key = (self.channel, account_id, object_id)
                existing = by_key.get(key)
                if existing and row_product not in existing["scope_products"]:
                    existing["scope_products"].append(row_product)
                    existing["blocked_reason"] = "ambiguous_object_scope"
                    continue
                if existing:
                    # Duplicate rows for one product should not occur after
                    # GROUP BY. Treat them as an unsafe source shape.
                    existing["blocked_reason"] = "ambiguous_object_scope"
                    continue
                candidate = dict(row)
                candidate.update(
                    {
                        "channel": self.channel,
                        "object_level": object_level,
                        "ad_account_id": account_id,
                        "object_id": object_id,
                        "optimizer_id": optimizer_id,
                        "product": row_product,
                        "scope_products": [row_product],
                        "account_timezone": timezone_name,
                        "blocked_reason": "",
                        "scope_ambiguity_check": "selected_scope_only",
                    }
                )
                campaign_parent_count = int(candidate.get("campaign_parent_count") or 0)
                adset_parent_count = int(candidate.get("adset_parent_count") or 0)
                if campaign_parent_count != 1 or (
                    object_level in {"adset", "ad"} and adset_parent_count != 1
                ):
                    candidate["blocked_reason"] = "ambiguous_object_scope"
                # Duplicate settings rows carrying the same timezone do not
                # make the scope ambiguous.  Only conflicting distinct values
                # are unsafe; an empty timezone filter must remain unrestricted.
                if requested_timezones and int(candidate.get("settings_timezone_count") or 0) > 1:
                    candidate["blocked_reason"] = "ambiguous_account_timezone"
                context_concat_limit = int(candidate.get("context_concat_limit") or 0)
                for context_field in context_fields:
                    values = [item for item in str(candidate.get(context_field) or "").split("\n") if item]
                    at_concat_limit = (
                        context_concat_limit > 0
                        and int(candidate.get(context_field + "_concat_bytes") or 0) >= context_concat_limit
                    )
                    if int(candidate.get(context_field + "_count") or 0) != len(values) or at_concat_limit:
                        candidate["blocked_reason"] = "context_aggregation_truncated"
                    candidate[context_field] = values[0] if len(values) == 1 else values
                    if context_field in AD_CONTEXT_FIELDS:
                        value_count = int(candidate.get(context_field + "_count") or 0)
                        at_concat_limit = (
                            context_concat_limit > 0
                            and int(candidate.get(context_field + "_concat_bytes") or 0) >= context_concat_limit
                        )
                        if value_count == len(values) and value_count > 1 and not at_concat_limit:
                            candidate["blocked_reason"] = "ambiguous_object_context"
                if requested_timezones:
                    if not timezone_name:
                        candidate["blocked_reason"] = "missing_account_timezone"
                    elif timezone_name not in requested_timezones:
                        candidate["blocked_reason"] = "timezone_out_of_scope"
                for metric_field in raw_metric_fields:
                    candidate[metric_field] = float(_decimal(candidate.get(metric_field)))
                ratio_specs = {
                    "ctr": ("clicks", "impressions", 100),
                    "cpm": ("spend", "impressions", 1000),
                    "cpc": ("spend", "clicks", 1),
                    "cpi": ("spend", "installs", 1),
                    "purchase_cpa": ("spend", "purchase", 1),
                    "roas": ("revenue", "spend", 1),
                    "retention_rate": ("day1_retain", "retain_install", 100),
                    "af_roas": ("af_revenue", "spend", 1),
                    "ad_impression_roas": ("ad_impression_revenue", "spend", 1),
                }
                for field in computed_fields:
                    numerator, denominator, multiplier = ratio_specs[field]
                    candidate[field] = _ratio(
                        candidate.get(numerator),
                        candidate.get(denominator),
                        multiplier,
                    )
                by_key[key] = candidate
                if len(by_key) > self.max_total_candidates:
                    raise AdControlV3Error(
                        "scope_candidate_limit_exceeded",
                        "total candidate count exceeds the safe limit",
                        status=409,
                        details={"limit": self.max_total_candidates},
                    )
        return sorted(
            by_key.values(),
            key=lambda row: (row["ad_account_id"], row["object_id"]),
        )

    def pause(self, target: Mapping[str, Any]) -> Dict[str, Any]:
        # Even if a caller accidentally reaches this method, no token resolver
        # or Graph transport exists in the adapter in this release.
        raise AdControlV3Error(
            "live_pause_disabled",
            "Facebook live mutation is disabled for V3",
            status=409,
        )
