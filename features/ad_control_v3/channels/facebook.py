"""Read-only Facebook scope discovery for Campaign, Ad Set and Ad.

The query shape is server-owned and bounded. Every source insight query contains
the mandatory data-source, platform, one exact product, date-range and optimizer
predicates. Products are queried separately and only fields required by the
stored rules/selection are projected. Account timezone filtering is deliberately
two phase: candidate aggregation never joins the settings table, then only the
candidate account ids are looked up through the reviewed ``paa`` index. This
keeps the ``dpdo`` index usable and detects cross-product object ambiguity before
rule evaluation.
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
FACEBOOK_ACCOUNT_SETTINGS_PLATFORM_ID = 0

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
TIMEZONE_LOOKUP_COLUMNS = ("account_id", "time_zone")


def _context_aggregate(field: str) -> str:
    aggregate = (
        "GROUP_CONCAT(DISTINCT NULLIF(CAST(s.{0} AS CHAR), '') "
        "ORDER BY CAST(s.{0} AS CHAR) SEPARATOR '\\n')"
    ).format(field)
    return (
        "{0} AS {1}, COUNT(DISTINCT NULLIF(CAST(s.{1} AS CHAR), '')) AS {1}_count, "
        "OCTET_LENGTH({0}) AS {1}_concat_bytes"
    ).format(aggregate, field)


def _scope_identity_aggregate(field: str, alias: str) -> str:
    aggregate = (
        "GROUP_CONCAT(DISTINCT NULLIF(CAST(s.{0} AS CHAR), '') "
        "ORDER BY CAST(s.{0} AS CHAR) SEPARATOR '\\n')"
    ).format(field)
    return (
        "{0} AS {1}, COUNT(DISTINCT NULLIF(CAST(s.{2} AS CHAR), '')) AS {1}_count, "
        "OCTET_LENGTH({0}) AS {1}_concat_bytes"
    ).format(aggregate, alias, field)


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
        timezone_schema_validator: Any = None,
        max_products: int = 20,
        max_total_candidates: int = 20000,
        query_deadline_seconds: float = 15.0,
        max_timezone_accounts: int = 5000,
        timezone_query_chunk_size: int = 200,
        max_timezone_rows_per_chunk: int = 5000,
    ) -> None:
        self._query_executor = query_executor
        if source_database != "kunlunads_dev":
            raise AdControlV3Error("unsafe_source_database", "source database is fixed")
        self.source_database = source_database
        self.max_window_days = int(max_window_days)
        self.max_rows_per_product = int(max_rows_per_product)
        self._schema_validator = schema_validator
        self._timezone_schema_validator = timezone_schema_validator
        self._schema_validated = False
        self._timezone_schema_validated = False
        self._schema_lock = threading.Lock()
        self.max_products = max(1, min(20, int(max_products)))
        self.max_total_candidates = max(1, min(20000, int(max_total_candidates)))
        self.query_deadline_seconds = max(1.0, min(30.0, float(query_deadline_seconds)))
        self.max_timezone_accounts = max(1, min(5000, int(max_timezone_accounts)))
        self.timezone_query_chunk_size = max(1, min(200, int(timezone_query_chunk_size)))
        self.max_timezone_rows_per_chunk = max(1, min(5000, int(max_timezone_rows_per_chunk)))

    def _ensure_schema(self, *, include_timezone: bool) -> None:
        source_ready = self._schema_validated or self._schema_validator is None
        timezone_ready = (
            not include_timezone
            or self._timezone_schema_validated
            or self._timezone_schema_validator is None
        )
        if source_ready and timezone_ready:
            return
        with self._schema_lock:
            if not self._schema_validated and self._schema_validator is not None:
                self._schema_validator()
                self._schema_validated = True
            if (
                include_timezone
                and not self._timezone_schema_validated
                and self._timezone_schema_validator is not None
            ):
                self._timezone_schema_validator()
                self._timezone_schema_validated = True

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
        delivery_product_count: int = 0,
        delivery_page_count: int = 0,
        delivery_app_count: int = 0,
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
        if delivery_product_count:
            select_columns.append(_scope_identity_aggregate("app_id", "_scope_app_id"))
            select_columns.append(_scope_identity_aggregate("w2a_page_id", "_scope_w2a_page_id"))
            columns.extend((
                "_scope_app_id", "_scope_app_id_count", "_scope_app_id_concat_bytes",
                "_scope_w2a_page_id", "_scope_w2a_page_id_count", "_scope_w2a_page_id_concat_bytes",
            ))
        if context_fields or delivery_product_count:
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

        # Timezone values are populated only by the bounded second-phase
        # account lookup.  Keeping constants here makes the candidate snapshot
        # shape stable without ever joining settings into the hot aggregation.
        select_columns.extend((
            "'' AS account_timezone",
            "0 AS settings_row_count",
            "0 AS settings_timezone_count",
        ))
        columns.extend(("account_timezone", "settings_row_count", "settings_timezone_count"))

        # Column names are server-owned constants; all user values are bound.
        # Do not add optional WHERE fragments that could omit a mandatory bound.
        if delivery_product_count:
            if delivery_product_count < 1 or delivery_product_count > self.max_products:
                raise AdControlV3Error("product_catalog_invalid", "delivery query product count is invalid", status=503)
            product_placeholders = ",".join(["%s"] * delivery_product_count)
            product_predicate = (
                "s.product IN ({0}) AND CAST(s.product AS BINARY) IN ({0})"
            ).format(product_placeholders)
            identity_clauses = []
            if delivery_page_count:
                identity_clauses.append("s.w2a_page_id IN (%s)" % ",".join(["%s"] * delivery_page_count))
            if delivery_app_count:
                identity_clauses.append(
                    "(COALESCE(s.w2a_page_id,0)=0 AND CAST(s.app_id AS BINARY) IN (%s))"
                    % ",".join(["%s"] * delivery_app_count)
                )
            if not identity_clauses:
                raise AdControlV3Error("product_catalog_invalid", "delivery query has no source identity", status=503)
            delivery_predicate = "AND (" + " OR ".join(identity_clauses) + ")"
        else:
            product_predicate = "s.product = %s AND BINARY s.product = BINARY %s"
            delivery_predicate = ""

        sql = """
            SELECT /*+ MAX_EXECUTION_TIME({max_execution_time_ms}) */
              {select_columns}
            FROM `kunlunads_dev`.ads_custom_source_insight s FORCE INDEX (dpdo)
            WHERE s.data_source IN (0, 6)
              AND s.platform = %s
              AND {product_predicate}
              AND s.dt BETWEEN %s AND %s
              AND s.optimizer = %s
              {delivery_predicate}
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
            product_predicate=product_predicate,
            delivery_predicate=delivery_predicate,
            max_execution_time_ms=SOURCE_QUERY_MAX_EXECUTION_TIME_MS,
        )
        return " ".join(sql.split()), tuple(columns)

    def _ensure_deadline(self, deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise AdControlV3Error(
                "scope_query_deadline_exceeded",
                "soft total candidate scan deadline exceeded",
                status=503,
                details={"deadline_seconds": self.query_deadline_seconds},
            )

    def _load_account_timezones(
        self,
        account_ids: Iterable[str],
        deadline: float,
    ) -> Dict[str, Dict[str, Any]]:
        """Load settings only for discovered accounts through ``paa``.

        The cache is local to one discovery request so repeated accounts across
        products are queried once without retaining potentially stale timezone
        values between previews.  Both bare and ``act_`` raw forms are bound as
        parameters because the source tables contain both conventions.
        """

        normalized_accounts = sorted({str(value or "").strip().lower() for value in account_ids if str(value or "").strip()})
        if len(normalized_accounts) > self.max_timezone_accounts:
            raise AdControlV3Error(
                "timezone_account_limit_exceeded",
                "candidate account count exceeds the timezone lookup limit",
                status=409,
                details={"limit": self.max_timezone_accounts},
            )
        for account_id in normalized_accounts:
            if len(account_id) > 128 or "\x00" in account_id:
                raise AdControlV3Error(
                    "invalid_candidate_account_id",
                    "candidate account id is invalid",
                    status=409,
                )

        cache: Dict[str, Dict[str, Any]] = {
            account_id: {
                "account_timezone": "",
                "settings_row_count": 0,
                "settings_timezone_count": 0,
                "timezone_identity_ambiguous": False,
            }
            for account_id in normalized_accounts
        }
        if not normalized_accounts:
            return cache

        # One raw value can theoretically collide when a malformed source id
        # already starts with act_. Preserve the relation as a set and fail
        # those candidates closed rather than guessing ownership.
        variant_owners: Dict[str, set] = {}
        for account_id in normalized_accounts:
            for raw_value in (account_id, "act_" + account_id):
                variant_owners.setdefault(raw_value, set()).add(account_id)
        raw_variants = sorted(variant_owners)
        timezone_values: Dict[str, set] = {account_id: set() for account_id in normalized_accounts}

        for offset in range(0, len(raw_variants), self.timezone_query_chunk_size):
            self._ensure_deadline(deadline)
            chunk = raw_variants[offset:offset + self.timezone_query_chunk_size]
            placeholders = ",".join(["%s"] * len(chunk))
            sql = (
                "SELECT /*+ MAX_EXECUTION_TIME({timeout}) */ "
                "CAST(x.account_id AS CHAR) AS account_id,CAST(x.time_zone AS CHAR) AS time_zone "
                "FROM `kunlunads_dev`.ads_accounts_setting x FORCE INDEX (paa) "
                "WHERE x.platform_id = %s AND x.account_id IN ({placeholders}) "
                "ORDER BY x.account_id LIMIT %s"
            ).format(timeout=SOURCE_QUERY_MAX_EXECUTION_TIME_MS, placeholders=placeholders)
            params: Tuple[Any, ...] = tuple(
                [FACEBOOK_ACCOUNT_SETTINGS_PLATFORM_ID] + chunk + [self.max_timezone_rows_per_chunk + 1]
            )
            rows = _dict_rows(self._query_executor(sql, params), TIMEZONE_LOOKUP_COLUMNS)
            self._ensure_deadline(deadline)
            if len(rows) > self.max_timezone_rows_per_chunk:
                raise AdControlV3Error(
                    "timezone_lookup_truncated",
                    "account timezone lookup reached the safe row limit",
                    status=409,
                    details={"limit": self.max_timezone_rows_per_chunk},
                )
            for row in rows:
                raw_account_id = str(row.get("account_id") or "").strip().lower()
                owners = variant_owners.get(raw_account_id) or set()
                timezone_name = str(row.get("time_zone") or "").strip()
                for account_id in owners:
                    cache[account_id]["settings_row_count"] += 1
                    if len(owners) > 1:
                        cache[account_id]["timezone_identity_ambiguous"] = True
                    if timezone_name:
                        timezone_values[account_id].add(timezone_name)

        for account_id, values in timezone_values.items():
            ordered = sorted(values)
            cache[account_id]["settings_timezone_count"] = len(ordered)
            cache[account_id]["account_timezone"] = ordered[0] if len(ordered) == 1 else ""
        return cache

    def discover(self, scope: Mapping[str, Any]) -> List[Dict[str, Any]]:
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
        raw_delivery_scopes = scope.get("delivery_product_scopes") or []
        if not isinstance(raw_delivery_scopes, list) or len(raw_delivery_scopes) > self.max_products:
            raise AdControlV3Error("product_catalog_invalid", "delivery product scopes are invalid", status=503)
        delivery_scopes: List[Dict[str, Any]] = []
        delivery_values = set()
        for raw_scope in raw_delivery_scopes:
            if not isinstance(raw_scope, Mapping):
                raise AdControlV3Error("product_catalog_invalid", "delivery product scope is invalid", status=503)
            selector = self._validate_product(raw_scope.get("product_value"))
            if selector not in products or selector in delivery_values:
                raise AdControlV3Error("product_catalog_invalid", "delivery product selector is inconsistent", status=503)
            delivery_values.add(selector)
            raw_insight_products = raw_scope.get("insight_products") or []
            raw_insight_app_ids = raw_scope.get("insight_app_ids") or []
            raw_page_ids = raw_scope.get("w2a_page_ids") or []
            if not all(isinstance(value, list) for value in (
                raw_insight_products, raw_insight_app_ids, raw_page_ids
            )):
                raise AdControlV3Error(
                    "product_catalog_invalid",
                    "delivery product scope collections are invalid",
                    status=503,
                )
            insight_products = [self._validate_product(value) for value in raw_insight_products]
            insight_products = list(dict.fromkeys(insight_products))
            insight_app_ids = sorted({
                str(value or "").strip() for value in raw_insight_app_ids
                if str(value or "").strip()
            })
            if any(len(value) > 255 or "\x00" in value for value in insight_app_ids):
                raise AdControlV3Error("product_catalog_invalid", "delivery app identity is invalid", status=503)
            try:
                page_ids = sorted({positive_int(value, "w2a_page_id") for value in raw_page_ids})
            except AdControlV3Error as exc:
                raise AdControlV3Error(
                    "product_catalog_invalid",
                    "delivery product page identity is invalid",
                    status=503,
                ) from exc
            if not insight_products or (not insight_app_ids and not page_ids):
                raise AdControlV3Error("product_catalog_invalid", "delivery product scope is incomplete", status=503)
            delivery_scopes.append({
                "product_value": selector,
                "insight_products": insight_products,
                "insight_app_ids": insight_app_ids,
                "w2a_page_ids": page_ids,
            })
        delivery_query_products = sorted({
            value for item in delivery_scopes for value in item["insight_products"]
        })
        if len(delivery_query_products) > self.max_products:
            raise AdControlV3Error("product_catalog_invalid", "delivery query product count exceeds the safe limit", status=503)
        legacy_products = [value for value in products if value not in delivery_values]
        overlap = sorted(set(legacy_products).intersection(delivery_query_products))
        if overlap:
            raise AdControlV3Error(
                "overlapping_product_scope",
                "broad and delivery product scopes cannot overlap in one rule group",
                details={"products": overlap},
            )
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
        load_account_timezones = bool(requested_timezones) or scope.get("load_account_timezones") is True
        context_fields, _, raw_metric_fields, computed_fields = _projection_for_level(
            object_level,
            scope.get("required_fields") if "required_fields" in scope else None,
        )
        # Validate only the source structures this request will actually read.
        # An unrestricted timezone scope must never touch the account settings
        # table, including through a startup/schema probe.
        self._ensure_schema(include_timezone=load_account_timezones)
        by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        deadline = time.monotonic() + self.query_deadline_seconds
        product_rows: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
        candidate_accounts = set()
        raw_candidate_count = 0
        query_jobs: List[Dict[str, Any]] = [
            {"kind": "legacy", "product": product, "query_products": [product]}
            for product in legacy_products
        ]
        if delivery_scopes:
            query_jobs.append({
                "kind": "delivery",
                "product": "",
                "query_products": delivery_query_products,
                "page_ids": sorted({value for item in delivery_scopes for value in item["w2a_page_ids"]}),
                "app_ids": sorted({value for item in delivery_scopes for value in item["insight_app_ids"]}),
                "scopes": delivery_scopes,
            })
        for job in query_jobs:
            self._ensure_deadline(deadline)
            if job["kind"] == "delivery":
                sql, columns = self._query_for_level(
                    object_level,
                    scope.get("required_fields") if "required_fields" in scope else None,
                    delivery_product_count=len(job["query_products"]),
                    delivery_page_count=len(job["page_ids"]),
                    delivery_app_count=len(job["app_ids"]),
                )
                params = tuple(
                    ["", optimizer_id, 0]
                    + job["query_products"] + job["query_products"]
                    + [date_from.isoformat(), date_to.isoformat(), optimizer_id]
                    + job["page_ids"] + job["app_ids"]
                    + [self.max_rows_per_product]
                )
            else:
                product = job["product"]
                sql, columns = self._query_for_level(
                    object_level,
                    scope.get("required_fields") if "required_fields" in scope else None,
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
            self._ensure_deadline(deadline)
            if len(rows) >= self.max_rows_per_product:
                raise AdControlV3Error(
                    "scope_query_truncated",
                    "candidate query reached the safe row limit",
                    status=409,
                    details={"product": job.get("product") or "delivery_product", "limit": self.max_rows_per_product},
                )
            raw_candidate_count += len(rows)
            if raw_candidate_count > self.max_total_candidates:
                raise AdControlV3Error(
                    "scope_candidate_limit_exceeded",
                    "total candidate count exceeds the safe limit",
                    status=409,
                    details={"limit": self.max_total_candidates},
                )
            product_rows.append((job, rows))
            for row in rows:
                account_id = str(row.get("ad_account_id") or "").strip().lower()
                if account_id:
                    candidate_accounts.add(account_id)

        # This request-local cache is populated once after all product scans,
        # so one account shared by several products never causes repeated
        # settings reads. Any lookup error aborts discovery before candidates
        # are returned to the service and therefore before preview persistence.
        timezone_cache = (
            self._load_account_timezones(candidate_accounts, deadline)
            if load_account_timezones
            else {}
        )

        for job, rows in product_rows:
            for row in rows:
                # SQL returns the canonical value after exactly one leading
                # ``act_`` removal. Do not strip again at the adapter boundary.
                account_id = str(row.get("ad_account_id") or "").strip().lower()
                object_id = str(row.get("object_id") or "").strip()
                if not account_id or not object_id:
                    continue
                timezone_record = timezone_cache.get(account_id) or {}
                timezone_name = str(timezone_record.get("account_timezone") or "").strip()
                delivery_blocked_reason = ""
                if job["kind"] == "delivery":
                    context_concat_limit = int(row.get("context_concat_limit") or 0)
                    app_values = [item for item in str(row.get("_scope_app_id") or "").split("\n") if item]
                    page_values = [item for item in str(row.get("_scope_w2a_page_id") or "").split("\n") if item]
                    for alias, values in (("_scope_app_id", app_values), ("_scope_w2a_page_id", page_values)):
                        at_limit = (
                            context_concat_limit > 0
                            and int(row.get(alias + "_concat_bytes") or 0) >= context_concat_limit
                        )
                        if int(row.get(alias + "_count") or 0) != len(values) or at_limit:
                            delivery_blocked_reason = "context_aggregation_truncated"
                    page_id = int(page_values[0]) if len(page_values) == 1 and page_values[0].isdigit() else 0
                    app_id = app_values[0] if len(app_values) == 1 else ""
                    if len(page_values) > 1 or (page_id == 0 and len(app_values) != 1):
                        delivery_blocked_reason = delivery_blocked_reason or "ambiguous_product_scope"
                    matches = [
                        item["product_value"] for item in job["scopes"]
                        if (page_id > 0 and page_id in item["w2a_page_ids"])
                        or (page_id == 0 and app_id and app_id in item["insight_app_ids"])
                    ]
                    row_product = matches[0] if len(matches) == 1 else "delivery_product"
                    if len(matches) != 1:
                        delivery_blocked_reason = delivery_blocked_reason or "ambiguous_product_scope"
                    for alias in ("_scope_app_id", "_scope_w2a_page_id"):
                        row.pop(alias, None)
                        row.pop(alias + "_count", None)
                        row.pop(alias + "_concat_bytes", None)
                else:
                    row_product = str(row.get("product") or job["product"]).strip()
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
                        "settings_row_count": int(timezone_record.get("settings_row_count") or 0),
                        "settings_timezone_count": int(timezone_record.get("settings_timezone_count") or 0),
                        "blocked_reason": delivery_blocked_reason,
                        "scope_ambiguity_check": "selected_scope_only",
                    }
                )
                campaign_parent_count = int(candidate.get("campaign_parent_count") or 0)
                adset_parent_count = int(candidate.get("adset_parent_count") or 0)
                if campaign_parent_count != 1 or (
                    object_level in {"adset", "ad"} and adset_parent_count != 1
                ):
                    candidate["blocked_reason"] = "ambiguous_object_scope"
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
                timezone_blocked_reason = ""
                if requested_timezones:
                    if timezone_record.get("timezone_identity_ambiguous") or int(
                        candidate.get("settings_timezone_count") or 0
                    ) > 1:
                        timezone_blocked_reason = "ambiguous_account_timezone"
                    elif not timezone_name:
                        timezone_blocked_reason = "missing_account_timezone"
                    elif timezone_name not in requested_timezones:
                        timezone_blocked_reason = "timezone_out_of_scope"
                if timezone_blocked_reason and not candidate["blocked_reason"]:
                    candidate["blocked_reason"] = timezone_blocked_reason
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
