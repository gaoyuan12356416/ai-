"""Application service for the isolated V3 automatic-control system."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .catalog import (
    OptimizerIdentityResolver,
    facebook_field_catalog,
    validate_rules_against_catalog,
)
from .channels import ChannelAdapter, FacebookAdapter, TikTokAdapter
from .channels.facebook import FACEBOOK_ACCOUNT_SETTINGS_PLATFORM_ID, SOURCE_QUERY_MAX_EXECUTION_TIME_MS
from .errors import AdControlV3Error
from .live_execution import (
    ENABLE_CONFIRMATION,
    LIVE_CONFIRMATION,
    MAX_COPY_TARGETS,
    MAX_LIVE_TARGETS,
    build_live_executor,
)
from .repository import MySQLRepository
from .rule_engine import evaluate_candidates
from .scheduler import candidate_schedule_due
from .schemas import (
    ACTIONS,
    OBJECT_LEVELS,
    RUN_MODES,
    Actor,
    behavior_hash,
    clean_text,
    ensure_no_account_scope,
    normalize_rule_group_payload,
    normalize_string_list,
    parse_iso_date,
    positive_int,
)
from .storage import SafeDataRoot
from .time_utils import DISPLAY_IANA_TIMEZONE, DISPLAY_TIMEZONE_LABEL


GROUP_INPUT_FIELDS = {
    "name",
    "description",
    "channel",
    "object_level",
    "run_mode",
    "optimizer_id",
    "products",
    "account_timezones",
    "rules",
    "schedule",
    "quotas",
    "selection",
    "enabled",
    "config_version",
}
SERVER_MANAGED_FIELDS = {
    "group_id",
    "owner_user_id",
    "created_by_user_id",
    "updated_by_user_id",
    "created_at",
    "updated_at",
    "deleted",
    "emergency_stopped",
    "last_preview_id",
    "last_preview_hash",
    "behavior_hash",
}
SOURCE_INSIGHT_REQUIRED_COLUMNS = frozenset({
    "ad_account_id", "campaign_id", "adset_id", "ad_id", "data_source", "platform", "product", "dt", "optimizer",
    "series_code", "app", "app_id", "os_type", "country", "language", "country_group", "drama_language",
    "auto_publish_dt", "resource_created_at", "spend_at", "resource_id", "resource_name", "source_id",
    "w2a_page_id", "ad_type", "category", "resource_tag", "source_type", "resource_type",
    "created_data_id", "task_id", "bid_type", "page_id", "task_type",
    "spend", "impressions", "clicks", "installs", "purchase", "revenue", "day1_retain",
    "retain_install", "events", "atc", "delivery_cnt", "af_installs", "af_revenue",
    "ad_impression", "ad_impression_revenue",
})
COMPUTED_INSIGHT_FIELDS = frozenset({
    "ctr", "cpm", "cpc", "cpi", "purchase_cpa", "roas",
    "retention_rate", "af_roas", "ad_impression_roas",
})
SOURCE_MYSQL_READ_TIMEOUT_MIN_SECONDS = 9
SOURCE_MYSQL_READ_TIMEOUT_MAX_SECONDS = 10


def _bounded_environment_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = str(os.environ.get(name, str(default))).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise AdControlV3Error(
            "service_not_configured",
            "V3 integer environment setting is invalid",
            status=503,
            details={"name": name},
        ) from exc
    return max(minimum, min(maximum, value))


def _required_preview_fields(
    rules: Sequence[Mapping[str, Any]],
    selection: Mapping[str, Any],
) -> List[str]:
    """Derive source projection only from validated persisted server state."""

    required = {
        str(condition.get("field") or "").strip()
        for rule in rules
        for condition in (rule.get("conditions") or [])
        if str(condition.get("field") or "").strip()
    }
    for rule in rules:
        if str(rule.get("action") or "") != "copy":
            continue
        copy_parameters = rule.get("copy_parameters") or {}
        if str(copy_parameters.get("budget_mode") or "") == "actual_cpi_multiplier":
            required.add("cpi")
    if str(selection.get("mode") or "") != "all":
        sort_field = str(selection.get("sort_field") or "").strip()
        if sort_field:
            required.add(sort_field)
    return sorted(required)


def _execute_bounded_source_query(
    connection_factory: Callable[[], Any],
    sql: str,
    params: Sequence[Any],
) -> List[Dict[str, Any]]:
    """Execute every production source statement under a session hard limit."""

    conn = None
    cursor = None
    try:
        conn = connection_factory()
        cursor = conn.cursor()
        cursor.execute(
            "SET SESSION max_execution_time = %s",
            (SOURCE_QUERY_MAX_EXECUTION_TIME_MS,),
        )
        cursor.execute(sql, tuple(params))
        return [dict(row) for row in (cursor.fetchall() or [])]
    except AdControlV3Error:
        raise
    except Exception as exc:
        # Source reads are an external dependency. Return a stable retryable
        # contract instead of exposing driver details or reporting an internal
        # application defect. Preview persistence happens only after discovery,
        # so this path cannot save a partial preview/execution bundle.
        logging.warning("ad-control V3 source query failed", exc_info=True)
        raise AdControlV3Error(
            "source_query_unavailable",
            "source insight query is temporarily unavailable",
            status=503,
        ) from exc
    finally:
        if cursor is not None:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()
        if conn is not None:
            conn.close()


def _validate_insight_schema_rows(
    insight_rows: Sequence[Mapping[str, Any]],
    insight_index_rows: Sequence[Mapping[str, Any]],
) -> None:
    insight_columns = {str(row.get("Field") or row.get("field") or "") for row in insight_rows}
    missing = sorted(set(SOURCE_INSIGHT_REQUIRED_COLUMNS) - insight_columns)
    dpdo = sorted(
        [
            (
                int(row.get("Seq_in_index") or row.get("seq_in_index") or 0),
                str(row.get("Column_name") or row.get("column_name") or ""),
            )
            for row in insight_index_rows
            if str(row.get("Key_name") or row.get("key_name") or "") == "dpdo"
        ]
    )
    expected_dpdo = ["data_source", "product", "dt", "optimizer"]
    if missing or [column for _, column in dpdo[:4]] != expected_dpdo:
        raise AdControlV3Error(
            "source_schema_mismatch",
            "source insight schema or reviewed index drifted",
            status=503,
            details={"missing_insight": missing, "dpdo": dpdo[:4]},
        )


def _validate_timezone_schema_rows(
    account_rows: Sequence[Mapping[str, Any]],
    account_index_rows: Sequence[Mapping[str, Any]],
) -> None:
    account_columns = {str(row.get("Field") or row.get("field") or "") for row in account_rows}
    missing_accounts = sorted({"account_id", "platform_id", "time_zone"} - account_columns)
    paa = sorted(
        [
            (
                int(row.get("Seq_in_index") or row.get("seq_in_index") or 0),
                str(row.get("Column_name") or row.get("column_name") or ""),
            )
            for row in account_index_rows
            if str(row.get("Key_name") or row.get("key_name") or "") == "paa"
        ]
    )
    expected_paa = ["platform_id", "account_id", "account_type"]
    if missing_accounts or [column for _, column in paa[:3]] != expected_paa:
        raise AdControlV3Error(
            "source_schema_mismatch",
            "account timezone schema or reviewed index drifted",
            status=503,
            details={"missing_accounts": missing_accounts, "paa": paa[:3]},
        )


def _validate_source_schema_rows(
    insight_rows: Sequence[Mapping[str, Any]],
    account_rows: Sequence[Mapping[str, Any]],
    insight_index_rows: Sequence[Mapping[str, Any]],
    account_index_rows: Sequence[Mapping[str, Any]],
) -> None:
    """Compatibility helper used by tests and offline release validation."""

    _validate_insight_schema_rows(insight_rows, insight_index_rows)
    _validate_timezone_schema_rows(account_rows, account_index_rows)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _time_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _parse_time_text(value: Any) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        raise ValueError("missing time")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Service:
    """Transport-neutral API used by HTTP routes and the independent runner."""

    def __init__(
        self,
        repository: Any,
        adapters: Mapping[str, ChannelAdapter],
        identity_resolver: OptimizerIdentityResolver,
        snapshot_store: Any,
        *,
        timezone_loader: Optional[Callable[[], Sequence[str]]] = None,
        clock: Callable[[], datetime] = _utc_now,
        preview_ttl_seconds: int = 1800,
        live_pause_enabled: bool = False,
        live_copy_enabled: bool = False,
        scheduler_enabled: bool = False,
        scheduler_live_enabled: bool = False,
        live_executor: Any = None,
        scan_concurrency: int = 1,
        meta_cache_ttl_seconds: int = 60,
    ) -> None:
        if repository is None or identity_resolver is None or snapshot_store is None:
            raise AdControlV3Error("service_not_configured", "V3 service dependencies are required", status=503)
        self.repository = repository
        self.adapters = dict(adapters or {})
        self.identity_resolver = identity_resolver
        self.snapshot_store = snapshot_store
        self.timezone_loader = timezone_loader or (lambda: [])
        self.clock = clock
        self.preview_ttl_seconds = max(60, min(86400, int(preview_ttl_seconds)))
        self.live_pause_enabled = bool(live_pause_enabled)
        self.live_copy_enabled = bool(live_copy_enabled)
        self.scheduler_enabled = bool(scheduler_enabled)
        self.scheduler_live_enabled = bool(scheduler_live_enabled)
        self.live_executor = live_executor
        self.meta_cache_ttl_seconds = max(15, min(300, int(meta_cache_ttl_seconds)))
        self._meta_cache_lock = threading.RLock()
        self._meta_cache: Dict[str, Any] = {}
        try:
            parsed_scan_concurrency = int(scan_concurrency)
        except (TypeError, ValueError):
            raise AdControlV3Error("service_not_configured", "scan concurrency is invalid", status=503)
        if parsed_scan_concurrency < 1 or parsed_scan_concurrency > 4:
            raise AdControlV3Error(
                "service_not_configured",
                "scan concurrency must be within 1..4",
                status=503,
            )
        self.scan_concurrency = parsed_scan_concurrency
        self._scan_semaphore = threading.BoundedSemaphore(parsed_scan_concurrency)

    def _cached_meta_value(self, key: str, loader: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._meta_cache_lock:
            cached = self._meta_cache.get(key)
            if cached and float(cached[0]) > now:
                return copy.deepcopy(cached[1])
        value = loader()
        with self._meta_cache_lock:
            self._meta_cache[key] = (now + self.meta_cache_ttl_seconds, copy.deepcopy(value))
        return copy.deepcopy(value)

    def _load_meta_values(self, *, include_optimizers: bool) -> Dict[str, Any]:
        loaders: Dict[str, Callable[[], Any]] = {
            "products": lambda: self.repository.list_products("facebook", include_disabled=False),
            "timezones": lambda: sorted({
                str(item).strip() for item in self.timezone_loader() or [] if str(item).strip()
            }),
        }
        if include_optimizers:
            loaders["optimizers"] = self.identity_resolver.list_for_admin
        values: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=len(loaders), thread_name_prefix="ad-control-v3-meta") as pool:
            futures = {
                key: pool.submit(self._cached_meta_value, "meta:" + key, loader)
                for key, loader in loaders.items()
            }
            for key, future in futures.items():
                values[key] = future.result()
        return values

    def _active_product_catalog(self, channel: str) -> List[Dict[str, Any]]:
        normalized = str(channel or "").strip().lower()
        if normalized == "facebook":
            return self._cached_meta_value(
                "meta:products",
                lambda: self.repository.list_products("facebook", include_disabled=False),
            )
        return self.repository.list_products(normalized, include_disabled=False)

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _reject_group_fields(payload: Mapping[str, Any]) -> None:
        ensure_no_account_scope(payload)
        unknown = sorted(set(payload) - GROUP_INPUT_FIELDS)
        if unknown:
            code = "server_managed_field" if set(unknown).intersection(SERVER_MANAGED_FIELDS) else "validation_error"
            raise AdControlV3Error(code, "payload contains unsupported fields", details={"fields": unknown})

    def _normal_optimizer(self, actor: Actor) -> int:
        return self.identity_resolver.resolve_for_actor(actor)

    def _optimizer_for_payload(self, actor: Actor, value: Any) -> int:
        if actor.is_admin:
            if value in (None, ""):
                raise AdControlV3Error(
                    "optimizer_required",
                    "admin must explicitly select an optimizer",
                    details={"field": "optimizer_id"},
                )
            selected = positive_int(value, "optimizer_id")
            active_ids = {
                int(item.get("optimizer_id") or 0)
                for item in self.identity_resolver.list_for_admin()
                if int(item.get("optimizer_id") or 0) > 0
            }
            if selected not in active_ids:
                raise AdControlV3Error(
                    "invalid_optimizer",
                    "selected optimizer is not active",
                    details={"optimizer_id": selected},
                )
            return selected
        own_optimizer = self._normal_optimizer(actor)
        if value not in (None, "") and int(value) != own_optimizer:
            raise AdControlV3Error(
                "optimizer_forbidden",
                "non-admin users can only create rules for themselves",
                status=403,
            )
        return own_optimizer

    def _optimizer_scope(self, actor: Actor) -> Optional[int]:
        return None if actor.is_admin else self._normal_optimizer(actor)

    def _authorize_group(self, actor: Actor, group: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        if not group:
            raise AdControlV3Error("rule_group_not_found", "rule group not found", status=404)
        if not actor.is_admin and int(group.get("optimizer_id") or 0) != self._normal_optimizer(actor):
            # Hide existence across optimizer boundaries.
            raise AdControlV3Error("rule_group_not_found", "rule group not found", status=404)
        return copy.deepcopy(dict(group))

    def _authorize_mutation(self, actor: Actor, group: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        authorized = self._authorize_group(actor, group)
        if not actor.is_admin and str(authorized.get("owner_user_id") or "") != actor.user_id:
            # Keep the same not-found boundary used for cross-optimizer IDs.
            raise AdControlV3Error("rule_group_not_found", "rule group not found", status=404)
        return authorized

    @staticmethod
    def _can_mutate_group(actor: Actor, group: Mapping[str, Any]) -> bool:
        """Return the explicit UI permission for one already-authorized group.

        Optimizer scope controls visibility, while ownership controls mutation.
        Keeping those decisions separate lets users on the same optimizer see a
        shared rule without accidentally receiving edit controls.
        """

        return bool(actor.is_admin or str(group.get("owner_user_id") or "") == actor.user_id)

    def _with_group_permissions(self, actor: Actor, group: Mapping[str, Any]) -> Dict[str, Any]:
        result = copy.deepcopy(dict(group))
        result["can_mutate"] = self._can_mutate_group(actor, result)
        return result

    def _adapter(self, channel: str) -> ChannelAdapter:
        adapter = self.adapters.get(str(channel or "").strip().lower())
        if not adapter or not adapter.enabled:
            raise AdControlV3Error(
                "channel_not_enabled",
                "channel is not enabled in this release",
                status=409,
                details={"channel": channel},
            )
        return adapter

    def _scan_discover(self, adapter: ChannelAdapter, scope: Mapping[str, Any]) -> List[Dict[str, Any]]:
        if not self._scan_semaphore.acquire(blocking=False):
            raise AdControlV3Error(
                "scan_busy",
                "another V3 scope scan is already running; retry later",
                status=429,
                details={"max_concurrency": self.scan_concurrency},
            )
        try:
            return list(adapter.discover(scope))
        finally:
            self._scan_semaphore.release()

    def _validate_products(self, channel: str, products: Sequence[str]) -> None:
        catalog_items = self._active_product_catalog(channel)
        available = {
            str(item.get("product_value") or "")
            for item in catalog_items
            if str(item.get("product_type") or "short_drama") == "short_drama"
        }
        invalid = [product for product in products if product not in available]
        if invalid:
            raise AdControlV3Error(
                "invalid_product_scope",
                "one or more products are not active short-drama catalog values",
                details={"products": invalid},
            )

    def _delivery_product_scopes(
        self,
        channel: str,
        products: Sequence[str],
    ) -> List[Dict[str, Any]]:
        catalog = self._active_product_catalog(channel)
        by_value = {str(item.get("product_value") or ""): item for item in catalog}
        reporting_values = {
            value for value, item in by_value.items()
            if str((item.get("evidence") or {}).get("catalog_kind") or "reporting_product") != "delivery_product"
        }
        result: List[Dict[str, Any]] = []
        for selected in products:
            item = by_value.get(str(selected))
            evidence = item.get("evidence") if isinstance(item, Mapping) else {}
            if not isinstance(evidence, Mapping) or str(evidence.get("catalog_kind") or "") != "delivery_product":
                continue
            raw_scope = evidence.get("scope")
            if not isinstance(raw_scope, Mapping):
                raise AdControlV3Error("product_catalog_invalid", "delivery product scope is missing", status=503)
            try:
                insight_products = normalize_string_list(
                    raw_scope.get("insight_products"),
                    "insight_products",
                    required=True,
                    max_items=20,
                    max_length=128,
                )
                insight_app_ids = normalize_string_list(
                    raw_scope.get("insight_app_ids") or [],
                    "insight_app_ids",
                    max_items=20,
                    max_length=255,
                )
            except AdControlV3Error as exc:
                raise AdControlV3Error(
                    "product_catalog_invalid",
                    "delivery product scope values are invalid",
                    status=503,
                    details={"field": exc.details.get("field") or "scope"},
                ) from exc
            if any(value not in reporting_values for value in insight_products):
                raise AdControlV3Error(
                    "product_catalog_invalid",
                    "delivery product references an inactive reporting product",
                    status=503,
                )
            raw_page_ids = raw_scope.get("w2a_page_ids") or []
            if not isinstance(raw_page_ids, list) or len(raw_page_ids) > 20:
                raise AdControlV3Error("product_catalog_invalid", "delivery product page ids are invalid", status=503)
            try:
                page_ids = sorted({positive_int(value, "w2a_page_id") for value in raw_page_ids})
            except AdControlV3Error as exc:
                raise AdControlV3Error(
                    "product_catalog_invalid",
                    "delivery product page ids are invalid",
                    status=503,
                ) from exc
            if not insight_app_ids and not page_ids:
                raise AdControlV3Error("product_catalog_invalid", "delivery product has no source identity", status=503)
            result.append({
                "product_value": str(selected),
                "insight_products": insight_products,
                "insight_app_ids": insight_app_ids,
                "w2a_page_ids": page_ids,
            })
        return result

    @staticmethod
    def _validate_selection_for_level(selection: Mapping[str, Any], object_level: str) -> None:
        if selection.get("mode") == "all":
            return
        sort_field = str(selection.get("sort_field") or "")
        fields = {item["key"]: item for item in facebook_field_catalog(object_level)}
        capability = fields.get(sort_field)
        if not capability or capability.get("value_type") != "number" or not capability.get("previewable"):
            raise AdControlV3Error(
                "field_not_supported",
                "selection sort field must be a previewable numeric field",
                details={"field": sort_field, "object_level": object_level},
            )

    def meta(self, actor: Any) -> Dict[str, Any]:
        current = Actor.from_value(actor)
        shared = self._load_meta_values(include_optimizers=current.is_admin)
        if current.is_admin:
            optimizers = shared["optimizers"]
            current_optimizer_id = None
        else:
            current_optimizer_id = self._normal_optimizer(current)
            optimizers = [
                {
                    "optimizer_id": current_optimizer_id,
                    "name": current.name or str(current_optimizer_id),
                    "email": current.email,
                    "locked": True,
                }
            ]
        channel_items = []
        for channel in ("facebook", "tiktok"):
            adapter = self.adapters.get(channel)
            if adapter:
                channel_items.append(adapter.capabilities())
            else:
                channel_items.append({"channel": channel, "enabled": False, "reason": "channel_not_enabled"})
        products = shared["products"]
        timezones = shared["timezones"]
        return {
            "actor": {
                "user_id": current.user_id,
                "name": current.name,
                "email": current.email,
                "role": "admin" if current.is_admin else "optimizer",
                "is_admin": current.is_admin,
                "optimizer_id": current_optimizer_id,
            },
            "channels": channel_items,
            "object_levels": [
                {"value": "campaign", "label": "Campaign"},
                {"value": "adset", "label": "Ad Set"},
                {"value": "ad", "label": "Ad"},
            ],
            "run_modes": [
                {"value": "observe", "label": "只观察", "enabled": True},
                {
                    "value": "live",
                    "label": "正式执行",
                    "enabled": bool(self.live_pause_enabled or self.live_copy_enabled),
                    "reason": "" if (self.live_pause_enabled or self.live_copy_enabled) else "live_execution_disabled",
                },
            ],
            "actions": [
                {"value": "pause", "label": "关闭", "observe_ready": True, "live_ready": self.live_pause_enabled},
                {"value": "copy", "label": "复制", "observe_ready": True, "live_ready": self.live_copy_enabled},
            ],
            "field_catalog": {level: facebook_field_catalog(level) for level in OBJECT_LEVELS},
            "fields": facebook_field_catalog(),
            "products": products,
            "optimizers": optimizers,
            "account_timezones": timezones,
            "permissions": {
                "is_admin": current.is_admin,
                "can_select_optimizer": current.is_admin,
                "current_optimizer_id": current_optimizer_id,
                "live_pause_enabled": self.live_pause_enabled,
                "live_copy_enabled": self.live_copy_enabled,
                "can_live_execute": bool(self.live_executor and (self.live_pause_enabled or self.live_copy_enabled)),
                "tiktok_enabled": False,
                "can_enable": self.scheduler_enabled,
                "scheduler_enabled": self.scheduler_enabled,
                "scheduler_live_enabled": self.scheduler_live_enabled,
                "enable_unavailable_reason": "" if self.scheduler_enabled else "计划调度器尚未发布；可先使用手动正式执行",
            },
            "capabilities": {
                "rule_group_search": True,
                "rule_group_search_fields": ["name", "group_id"],
            },
            "time_standard": {
                "storage_timezone": "UTC",
                "display_timezone": DISPLAY_TIMEZONE_LABEL,
                "iana_timezone": DISPLAY_IANA_TIMEZONE,
            },
            "defaults": {"enabled": False, "run_mode": "observe"},
        }

    def list_rule_groups(
        self,
        actor: Any,
        filters: Optional[Mapping[str, Any]] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        current = Actor.from_value(actor)
        filters = dict(filters or {})
        if not current.is_admin and filters.get("optimizer_id") not in (None, ""):
            own = self._normal_optimizer(current)
            if int(filters["optimizer_id"]) != own:
                raise AdControlV3Error("optimizer_forbidden", "optimizer filter is forbidden", status=403)
        page_result = self.repository.list_rule_groups(
            filters,
            page=page,
            page_size=page_size,
            optimizer_scope=self._optimizer_scope(current),
        )
        page_result["items"] = [
            self._with_group_permissions(current, item)
            for item in page_result.get("items") or []
        ]
        return page_result

    def create_rule_group(self, actor: Any, payload: Mapping[str, Any]) -> Dict[str, Any]:
        current = Actor.from_value(actor)
        body = dict(payload or {})
        self._reject_group_fields(body)
        normalized = normalize_rule_group_payload(body, creating=True)
        if normalized["channel"] != "facebook":
            raise AdControlV3Error("channel_not_enabled", "TikTok is not enabled", status=409)
        normalized["optimizer_id"] = self._optimizer_for_payload(current, normalized.get("optimizer_id"))
        self._validate_products(normalized["channel"], normalized["products"])
        validate_rules_against_catalog(normalized["rules"], normalized["object_level"])
        self._validate_selection_for_level(normalized["selection"], normalized["object_level"])
        now = _time_text(self._now())
        record = dict(normalized)
        record.update(
            {
                "group_id": uuid.uuid4().hex,
                "owner_user_id": current.user_id,
                "created_by_user_id": current.user_id,
                "updated_by_user_id": current.user_id,
                "config_version": 1,
                "last_preview_id": "",
                "last_preview_hash": "",
                "enabled": False,
                "emergency_stopped": False,
                "deleted": False,
                "created_at": now,
                "updated_at": now,
            }
        )
        record["behavior_hash"] = behavior_hash(record)
        return self.repository.create_rule_group(record)

    def get_rule_group(self, actor: Any, group_id: str) -> Dict[str, Any]:
        current = Actor.from_value(actor)
        group = self._authorize_group(
            current,
            self.repository.get_rule_group(clean_text(group_id, "group_id", required=True)),
        )
        return self._with_group_permissions(current, group)

    def update_rule_group(
        self,
        actor: Any,
        group_id: str,
        payload: Mapping[str, Any],
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        current_actor = Actor.from_value(actor)
        current = self._authorize_mutation(current_actor, self.repository.get_rule_group(group_id))
        body = dict(payload or {})
        payload_versions = []
        for version_key in ("version", "config_version"):
            if body.get(version_key) not in (None, ""):
                payload_versions.append(positive_int(body.pop(version_key), version_key))
            else:
                body.pop(version_key, None)
        if len(set(payload_versions)) > 1:
            raise AdControlV3Error("version_conflict", "payload versions disagree", status=409)
        if payload_versions:
            payload_version = payload_versions[0]
            if expected_version is not None and int(expected_version) != payload_version:
                raise AdControlV3Error("version_conflict", "header and payload versions disagree", status=409)
            expected_version = payload_version
        self._reject_group_fields(body)
        if expected_version is None:
            raise AdControlV3Error("version_required", "config_version is required", status=409)
        if int(expected_version) != int(current.get("config_version") or 0):
            raise AdControlV3Error("version_conflict", "rule group was changed", status=409)
        normalized = normalize_rule_group_payload(body, creating=False, current=current)
        if normalized["channel"] != "facebook":
            raise AdControlV3Error("channel_not_enabled", "TikTok is not enabled", status=409)
        normalized["optimizer_id"] = self._optimizer_for_payload(current_actor, normalized.get("optimizer_id"))
        self._validate_products(normalized["channel"], normalized["products"])
        validate_rules_against_catalog(normalized["rules"], normalized["object_level"])
        self._validate_selection_for_level(normalized["selection"], normalized["object_level"])
        now = _time_text(self._now())
        record = dict(current)
        record.update(normalized)
        record.update(
            {
                "enabled": False,
                "last_preview_id": "",
                "last_preview_hash": "",
                "config_version": int(expected_version) + 1,
                "updated_by_user_id": current_actor.user_id,
                "updated_at": now,
            }
        )
        record["behavior_hash"] = behavior_hash(record)
        return self.repository.update_rule_group(
            current["group_id"],
            record,
            expected_version=int(expected_version),
        )

    def delete_rule_group(self, actor: Any, group_id: str) -> Dict[str, Any]:
        current_actor = Actor.from_value(actor)
        group = self._authorize_mutation(current_actor, self.repository.get_rule_group(group_id))
        deleted = self.repository.soft_delete_rule_group(
            group["group_id"],
            updated_by=current_actor.user_id,
            updated_at=_time_text(self._now()),
        )
        if not deleted:
            raise AdControlV3Error("rule_group_not_found", "rule group not found", status=404)
        return {"deleted": True, "group_id": group["group_id"]}

    def duplicate_rule_group(
        self,
        actor: Any,
        group_id: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        current_actor = Actor.from_value(actor)
        source = self._authorize_mutation(current_actor, self.repository.get_rule_group(group_id))
        overrides = dict(payload or {})
        unknown = set(overrides) - {"name", "description", "optimizer_id"}
        if unknown:
            raise AdControlV3Error("validation_error", "duplicate overrides contain unsupported fields", details={"fields": sorted(unknown)})
        body = {key: copy.deepcopy(source.get(key)) for key in GROUP_INPUT_FIELDS if key not in {"enabled", "config_version"}}
        body["name"] = overrides.get("name") or (str(source.get("name") or "") + " - 副本")
        if "description" in overrides:
            body["description"] = overrides["description"]
        if "optimizer_id" in overrides:
            body["optimizer_id"] = overrides["optimizer_id"]
        body["run_mode"] = "observe"
        return self.create_rule_group(current_actor, body)

    def _scope_identity(self, actor: Actor, payload: Mapping[str, Any]) -> int:
        return self._optimizer_for_payload(actor, payload.get("optimizer_id"))

    def _discovery_scope(
        self,
        *,
        channel: str,
        object_level: str,
        products: Sequence[str],
        optimizer_id: int,
        account_timezones: Sequence[str],
        payload: Mapping[str, Any],
        required_fields: Sequence[str] = (),
    ) -> Dict[str, Any]:
        raw_date_from = payload.get("date_from")
        raw_date_to = payload.get("date_to")
        metric_window_days = payload.get("metric_window_days")
        if raw_date_from not in (None, "") or raw_date_to not in (None, ""):
            if raw_date_from in (None, "") or raw_date_to in (None, ""):
                raise AdControlV3Error("validation_error", "date_from and date_to must be provided together")
            date_from = parse_iso_date(raw_date_from, "date_from")
            date_to = parse_iso_date(raw_date_to, "date_to")
        else:
            days = positive_int(metric_window_days, "metric_window_days", maximum=31)
            date_to = self._now().date()
            date_from = date_to - timedelta(days=days - 1)
        result = {
            "channel": channel,
            "object_level": object_level,
            "products": list(products),
            "optimizer_id": optimizer_id,
            "account_timezones": list(account_timezones),
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            # Internal-only. API payload allowlists never accept this key.
            "required_fields": list(required_fields),
        }
        delivery_scopes = self._delivery_product_scopes(channel, products)
        if delivery_scopes:
            result["delivery_product_scopes"] = delivery_scopes
        return result

    def scope_estimate(self, actor: Any, payload: Mapping[str, Any]) -> Dict[str, Any]:
        current = Actor.from_value(actor)
        body = dict(payload or {})
        ensure_no_account_scope(body)
        allowed = {
            "channel", "object_level", "products", "optimizer_id", "account_timezones",
            "date_from", "date_to", "metric_window_days",
        }
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise AdControlV3Error("validation_error", "scope estimate contains unsupported fields", details={"fields": unknown})
        channel = clean_text(body.get("channel"), "channel", required=True).lower()
        object_level = clean_text(body.get("object_level"), "object_level", required=True).lower()
        if object_level not in OBJECT_LEVELS:
            raise AdControlV3Error("validation_error", "unsupported object level")
        products = normalize_string_list(body.get("products"), "products", required=True, max_items=20, max_length=128)
        optimizer_id = self._scope_identity(current, body)
        timezones = normalize_string_list(body.get("account_timezones") or [], "account_timezones", max_items=100, max_length=64)
        adapter = self._adapter(channel)
        self._validate_products(channel, products)
        scope = self._discovery_scope(
            channel=channel,
            object_level=object_level,
            products=products,
            optimizer_id=optimizer_id,
            account_timezones=timezones,
            payload=body,
            required_fields=(),
        )
        candidates = self._scan_discover(adapter, scope)
        blocked = [item for item in candidates if item.get("blocked_reason")]
        return {
            "scope": {key: value for key, value in scope.items() if key != "delivery_product_scopes"},
            "projection_mode": "identity_only",
            "required_fields": [],
            "account_count": len({str(item.get("ad_account_id") or "") for item in candidates}),
            "object_count": len(candidates),
            "eligible_object_count": len(candidates) - len(blocked),
            "blocked_count": len(blocked),
            "blocked_reasons": _reason_counts(blocked),
            "live_ready": bool(self.live_pause_enabled or self.live_copy_enabled),
        }

    def preview(
        self,
        actor: Any,
        group_id: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        _candidate_filter: Optional[Callable[[Dict[str, Any]], bool]] = None,
        _load_account_timezones: bool = False,
    ) -> Dict[str, Any]:
        current = Actor.from_value(actor)
        group = self._authorize_mutation(current, self.repository.get_rule_group(group_id))
        body = dict(payload or {})
        ensure_no_account_scope(body)
        allowed = {"date_from", "date_to", "metric_window_days", "trigger_source"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise AdControlV3Error("validation_error", "preview contains unsupported fields", details={"fields": unknown})
        validate_rules_against_catalog(group["rules"], group["object_level"])
        metric_window_days = (group.get("selection") or {}).get("metric_window_days")
        discovery_payload = dict(body)
        if discovery_payload.get("date_from") in (None, "") and discovery_payload.get("date_to") in (None, ""):
            if discovery_payload.get("metric_window_days") in (None, ""):
                discovery_payload["metric_window_days"] = metric_window_days
        scope = self._discovery_scope(
            channel=group["channel"],
            object_level=group["object_level"],
            products=group["products"],
            optimizer_id=int(group["optimizer_id"]),
            account_timezones=group.get("account_timezones") or [],
            payload=discovery_payload,
            required_fields=_required_preview_fields(
                group["rules"],
                group.get("selection") or {},
            ),
        )
        if _load_account_timezones:
            # Internal runner-only switch. It cannot be submitted through the
            # HTTP payload allowlist above.
            scope["load_account_timezones"] = True
        candidates = self._scan_discover(self._adapter(group["channel"]), scope)
        if _candidate_filter is not None:
            candidates = [candidate for candidate in candidates if _candidate_filter(candidate)]
        now = self._now()
        result = evaluate_candidates(
            candidates,
            group["rules"],
            facebook_field_catalog(group["object_level"]),
            selection=group.get("selection") or {},
            evaluation_time=now,
        )
        result["summary"]["projection_mode"] = "rule_fields"
        result["summary"]["required_fields"] = list(scope.get("required_fields") or [])
        # Observe is the only execution mode connected in this release. Persist
        # an explicit counter; consumers must never infer zero from a missing
        # field once live execution exists.
        result["summary"]["meta_write_count"] = 0
        now_text = _time_text(now)
        preview_id = uuid.uuid4().hex
        immutable_snapshot = {
            "snapshot_version": 1,
            "kind": "preview",
            "preview_id": preview_id,
            "rule_group": {key: copy.deepcopy(group.get(key)) for key in (
                "group_id", "channel", "object_level", "run_mode", "optimizer_id", "products",
                "account_timezones", "rules", "schedule", "quotas", "selection",
                "config_version", "behavior_hash",
            )},
            "scope": scope,
            "summary": result["summary"],
            "targets": result["targets"],
            "created_at": now_text,
            "evaluation_time": now_text,
        }
        snapshot = self.snapshot_store.write_snapshot("preview", preview_id, immutable_snapshot)
        preview_record = {
            "preview_id": preview_id,
            "rule_group_id": group["group_id"],
            "config_version": group["config_version"],
            "behavior_hash": group["behavior_hash"],
            "optimizer_id": group["optimizer_id"],
            "channel": group["channel"],
            "object_level": group["object_level"],
            "status": "ready",
            "summary": result["summary"],
            "snapshot_relative_path": snapshot["relative_path"],
            "snapshot_sha256": snapshot["sha256"],
            "snapshot_byte_size": snapshot["byte_size"],
            "created_by_user_id": current.user_id,
            "created_at": now_text,
            "expires_at": _time_text(now + timedelta(seconds=self.preview_ttl_seconds)),
        }
        execution_id = uuid.uuid4().hex
        execution_record = {
            "execution_id": execution_id,
            "rule_group_id": group["group_id"],
            "preview_id": preview_id,
            "config_version": group["config_version"],
            "behavior_hash": group["behavior_hash"],
            "optimizer_id": group["optimizer_id"],
            "channel": group["channel"],
            "object_level": group["object_level"],
            "run_mode": "observe",
            "trigger_source": clean_text(body.get("trigger_source") or "manual_preview", "trigger_source", max_length=32),
            "status": "observed",
            "summary": result["summary"],
            "snapshot_relative_path": snapshot["relative_path"],
            "snapshot_sha256": snapshot["sha256"],
            "snapshot_byte_size": snapshot["byte_size"],
            "created_by_user_id": current.user_id,
            "created_at": now_text,
            "finished_at": now_text,
        }
        self.repository.save_preview_execution_bundle(
            preview_record,
            execution_record,
            result["targets"],
        )
        return {
            "preview_id": preview_id,
            "execution_id": execution_id,
            "status": "ready",
            "expires_at": preview_record["expires_at"],
            "summary": result["summary"],
            "targets": result["targets"][:200],
            "target_count": len(result["targets"]),
            "truncated": len(result["targets"]) > 200,
            "live_ready": bool(self.live_pause_enabled or self.live_copy_enabled),
        }

    def execute(
        self,
        actor: Any,
        group_id: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        trigger_source: str = "manual_execute",
        require_enabled: bool = False,
    ) -> Dict[str, Any]:
        """Execute the latest immutable preview through the live boundary.

        Manual execution intentionally does not require the scheduled-runner
        ``enabled`` flag.  It still requires a live group, an unexpired exact
        preview, ownership, a confirmation phrase and every server-side kill
        switch.  This lets an optimizer run a controlled Canary before the
        independent scheduler is released.
        """

        current = Actor.from_value(actor)
        group = self._authorize_mutation(current, self.repository.get_rule_group(group_id))
        body = dict(payload or {})
        ensure_no_account_scope(body)
        unknown = sorted(set(body) - {"confirm", "preview_id"})
        if unknown:
            raise AdControlV3Error("validation_error", "execute contains unsupported fields", details={"fields": unknown})
        if group.get("run_mode") != "live":
            raise AdControlV3Error("live_mode_required", "rule group must be saved in live mode", status=409)
        if group.get("emergency_stopped"):
            raise AdControlV3Error("rule_group_emergency_stopped", "rule group is emergency stopped", status=409)
        if require_enabled and not group.get("enabled"):
            raise AdControlV3Error("rule_group_not_enabled", "rule group is not enabled", status=409)
        if str(body.get("confirm") or "") != LIVE_CONFIRMATION:
            raise AdControlV3Error(
                "live_execute_confirm_required",
                "explicit live execution confirmation is required",
                status=409,
                details={"confirmation": LIVE_CONFIRMATION},
            )
        if not self.live_executor:
            raise AdControlV3Error("live_execution_disabled", "live execution service is disabled", status=409)
        preview_id = str(group.get("last_preview_id") or "")
        requested_preview_id = str(body.get("preview_id") or "")
        if requested_preview_id and requested_preview_id != preview_id:
            raise AdControlV3Error("stale_preview", "preview does not match the latest rule configuration", status=409)
        preview = self.repository.get_preview(preview_id, include_targets=True) if preview_id else None
        if not preview or str(preview.get("behavior_hash") or "") != str(group.get("behavior_hash") or ""):
            raise AdControlV3Error("stale_preview", "a current preview is required", status=409)
        try:
            if _parse_time_text(preview.get("expires_at")) <= self._now():
                raise AdControlV3Error("stale_preview", "preview has expired", status=409)
        except (TypeError, ValueError):
            raise AdControlV3Error("stale_preview", "preview has expired", status=409)
        preview_summary = preview.get("summary") if isinstance(preview.get("summary"), Mapping) else {}
        planned_total = int(preview_summary.get("planned_count") or 0)
        if planned_total > MAX_LIVE_TARGETS:
            raise AdControlV3Error(
                "live_target_limit_exceeded",
                "preview contains too many live targets",
                status=409,
                details={"target_count": planned_total, "limit": MAX_LIVE_TARGETS},
            )
        targets = [
            copy.deepcopy(dict(item))
            for item in (preview.get("targets") or [])
            if str(item.get("status") or "").startswith("would_")
        ]
        if len(targets) > MAX_LIVE_TARGETS:
            raise AdControlV3Error(
                "live_target_limit_exceeded",
                "preview contains too many live targets",
                status=409,
                details={"target_count": len(targets), "limit": MAX_LIVE_TARGETS},
            )
        copy_count = len([item for item in targets if item.get("action") == "copy"])
        if copy_count > MAX_COPY_TARGETS:
            raise AdControlV3Error(
                "copy_target_limit_exceeded",
                "preview contains too many copy targets",
                status=409,
                details={"copy_count": copy_count, "limit": MAX_COPY_TARGETS},
            )
        for item in targets:
            action = str(item.get("action") or "")
            if action == "pause" and not self.live_pause_enabled:
                raise AdControlV3Error("live_pause_disabled", "Facebook live pause is disabled", status=409)
            if action == "copy" and not self.live_copy_enabled:
                raise AdControlV3Error("live_copy_disabled", "Facebook live copy is disabled", status=409)

        results: List[Dict[str, Any]] = []
        meta_write_count = 0
        for target in targets:
            # Re-read the kill switch before each new target. An emergency
            # stop or behavior edit never starts another external mutation.
            latest = self.repository.get_rule_group(group["group_id"])
            if (
                not latest
                or latest.get("emergency_stopped")
                or (require_enabled and not latest.get("enabled"))
                or str(latest.get("behavior_hash") or "") != str(group.get("behavior_hash") or "")
            ):
                remaining = copy.deepcopy(target)
                remaining.update({"status": "skipped", "reason": "rule_group_stopped", "meta_write_count": 0})
                results.append(remaining)
                continue
            result = self.live_executor.execute(group, target)
            meta_write_count += int(result.get("meta_write_count") or 0)
            results.append(result)

        now = self._now()
        now_text = _time_text(now)
        execution_id = uuid.uuid4().hex
        summary = {
            "planned_count": len(targets),
            "pause_count": len([item for item in targets if item.get("action") == "pause"]),
            "copy_count": copy_count,
            "succeeded_count": len([item for item in results if item.get("status") == "succeeded"]),
            "skipped_count": len([item for item in results if item.get("status") == "skipped"]),
            "failed_count": len([item for item in results if item.get("status") == "failed"]),
            "meta_write_count": meta_write_count,
        }
        immutable_snapshot = {
            "snapshot_version": 1,
            "kind": "execution",
            "execution_id": execution_id,
            "preview_id": preview_id,
            "rule_group": {key: copy.deepcopy(group.get(key)) for key in (
                "group_id", "channel", "object_level", "run_mode", "optimizer_id", "products",
                "account_timezones", "rules", "schedule", "quotas", "selection",
                "config_version", "behavior_hash",
            )},
            "summary": summary,
            "targets": results,
            "created_at": now_text,
            "finished_at": now_text,
        }
        snapshot = self.snapshot_store.write_snapshot("execution", execution_id, immutable_snapshot)
        status = "failed" if results and summary["failed_count"] == len(results) else (
            "partial_failed" if summary["failed_count"] else "completed"
        )
        execution_record = {
            "execution_id": execution_id,
            "rule_group_id": group["group_id"],
            "preview_id": preview_id,
            "config_version": group["config_version"],
            "behavior_hash": group["behavior_hash"],
            "optimizer_id": group["optimizer_id"],
            "channel": group["channel"],
            "object_level": group["object_level"],
            "run_mode": "live",
            "trigger_source": clean_text(trigger_source, "trigger_source", required=True, max_length=32),
            "status": status,
            "summary": summary,
            "snapshot_relative_path": snapshot["relative_path"],
            "snapshot_sha256": snapshot["sha256"],
            "snapshot_byte_size": snapshot["byte_size"],
            "created_by_user_id": current.user_id,
            "created_at": now_text,
            "finished_at": now_text,
        }
        self.repository.save_execution(execution_record, results)
        return dict(execution_record, targets=results)

    def scheduled_group_due_now(
        self,
        group: Mapping[str, Any],
        now: Optional[datetime] = None,
        timezone_values: Optional[Sequence[str]] = None,
    ) -> bool:
        """Cheap pre-scan gate based on reviewed account timezone values."""

        if not self.scheduler_enabled or not group.get("enabled") or group.get("emergency_stopped"):
            return False
        schedule = group.get("schedule") or {}
        if not schedule:
            return False
        timezones = list(group.get("account_timezones") or []) or list(
            timezone_values if timezone_values is not None else (self.timezone_loader() or [])
        )
        current = now or self._now()
        for timezone_name in timezones:
            due, _, _ = candidate_schedule_due(
                {"account_timezone": str(timezone_name or "")}, schedule, current
            )
            if due:
                return True
        return False

    def run_scheduled_group(self, group_id: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Run one already-claimed scheduler event using account-local time."""

        group = self.repository.get_rule_group(group_id)
        if not group or not group.get("enabled") or group.get("emergency_stopped"):
            return {"status": "stopped", "execution_id": "", "meta_write_count": 0}
        current = now or self._now()
        schedule = group.get("schedule") or {}
        if not schedule:
            raise AdControlV3Error("schedule_required", "enabled rule group has no schedule", status=409)

        due_count = 0

        def due_candidate(candidate: Dict[str, Any]) -> bool:
            nonlocal due_count
            due, reason, context = candidate_schedule_due(candidate, schedule, current)
            candidate.update(context)
            if due:
                due_count += 1
                return True
            if reason in {"missing_account_timezone", "invalid_account_timezone"}:
                candidate["blocked_reason"] = candidate.get("blocked_reason") or reason
                return True
            return False

        actor = {
            "user_id": str(group.get("owner_user_id") or "v3-runner"),
            "name": "V3 runner",
            "is_admin": True,
            "optimizer_id": group.get("optimizer_id"),
        }
        preview = self.preview(
            actor,
            group["group_id"],
            {"trigger_source": "schedule"},
            _candidate_filter=due_candidate,
            _load_account_timezones=True,
        )
        if group.get("run_mode") != "live" or due_count == 0:
            return {
                "status": "observed" if due_count else "not_due",
                "execution_id": str(preview.get("execution_id") or ""),
                "preview_id": str(preview.get("preview_id") or ""),
                "meta_write_count": 0,
            }
        if not self.scheduler_live_enabled:
            raise AdControlV3Error("runner_live_not_released", "scheduled live execution is not released", status=409)
        executed = self.execute(
            actor,
            group["group_id"],
            {"confirm": LIVE_CONFIRMATION, "preview_id": preview["preview_id"]},
            trigger_source="schedule",
            require_enabled=True,
        )
        return {
            "status": executed.get("status") or "completed",
            "execution_id": str(executed.get("execution_id") or ""),
            "preview_id": str(preview.get("preview_id") or ""),
            "meta_write_count": int((executed.get("summary") or {}).get("meta_write_count") or 0),
        }

    def set_enabled(
        self,
        actor: Any,
        group_id: str,
        enabled: Any,
        confirm: str = "",
    ) -> Dict[str, Any]:
        current = Actor.from_value(actor)
        group = self._authorize_mutation(current, self.repository.get_rule_group(group_id))
        if not isinstance(enabled, bool):
            raise AdControlV3Error("validation_error", "enabled must be boolean")
        if enabled:
            if group["channel"] != "facebook":
                raise AdControlV3Error("channel_not_enabled", "channel is not enabled", status=409)
            preview_id = str(group.get("last_preview_id") or "")
            preview = self.repository.get_preview(preview_id, include_targets=False) if preview_id else None
            if not preview or str(preview.get("behavior_hash") or "") != str(group.get("behavior_hash") or ""):
                raise AdControlV3Error("stale_preview", "a current preview is required", status=409)
            try:
                expired = _parse_time_text(preview.get("expires_at")) <= self._now()
            except (TypeError, ValueError):
                expired = True
            if expired:
                raise AdControlV3Error("stale_preview", "preview has expired", status=409)
            if group.get("run_mode") == "live":
                if str(confirm or "") != ENABLE_CONFIRMATION:
                    raise AdControlV3Error(
                        "live_enable_confirm_required",
                        "explicit live enable confirmation is required",
                        status=409,
                        details={"confirmation": ENABLE_CONFIRMATION},
                    )
                actions = {str(rule.get("action") or "") for rule in group.get("rules") or []}
                if "pause" in actions and not self.live_pause_enabled:
                    raise AdControlV3Error("live_pause_disabled", "live pause is disabled", status=409)
                if "copy" in actions and not self.live_copy_enabled:
                    raise AdControlV3Error("live_copy_disabled", "live copy is disabled", status=409)
            if not self.scheduler_enabled:
                raise AdControlV3Error(
                    "runner_scheduler_not_configured",
                    "scheduled observe runner has not been released; use manual preview",
                    status=409,
                )
            if not group.get("schedule"):
                raise AdControlV3Error("schedule_required", "a schedule is required before enabling", status=409)
            if group.get("run_mode") == "live" and not self.scheduler_live_enabled:
                raise AdControlV3Error(
                    "runner_live_not_released",
                    "scheduled live execution has not been released; use manual execution",
                    status=409,
                )
        return self.repository.set_group_state(
            group["group_id"],
            enabled=enabled,
            emergency_stopped=False if enabled else None,
            updated_by=current.user_id,
            updated_at=_time_text(self._now()),
            expected_version=int(group["config_version"]) if enabled else None,
            expected_behavior_hash=str(group["behavior_hash"]) if enabled else "",
            expected_preview_id=str(group.get("last_preview_id") or "") if enabled else "",
            require_fresh_preview=enabled,
        )

    def emergency_stop(self, actor: Any, group_id: Optional[str] = None) -> Dict[str, Any]:
        current = Actor.from_value(actor)
        now = _time_text(self._now())
        if group_id:
            group = self._authorize_mutation(current, self.repository.get_rule_group(group_id))
            self.repository.set_group_state(
                group["group_id"],
                enabled=False,
                emergency_stopped=True,
                clear_preview=True,
                updated_by=current.user_id,
                updated_at=now,
            )
            return {"scope": "rule_group", "group_id": group["group_id"], "affected_count": 1}
        affected = self.repository.emergency_stop_all(
            optimizer_scope=self._optimizer_scope(current),
            updated_by=current.user_id,
            updated_at=now,
        )
        return {"scope": "global" if current.is_admin else "optimizer", "group_id": "", "affected_count": affected}

    def list_executions(
        self,
        actor: Any,
        filters: Optional[Mapping[str, Any]] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        current = Actor.from_value(actor)
        filters = dict(filters or {})
        if not current.is_admin and filters.get("optimizer_id") not in (None, ""):
            own = self._normal_optimizer(current)
            if int(filters["optimizer_id"]) != own:
                raise AdControlV3Error("optimizer_forbidden", "optimizer filter is forbidden", status=403)
        page_result = self.repository.list_executions(
            filters,
            page=page,
            page_size=page_size,
            optimizer_scope=self._optimizer_scope(current),
        )
        optimizer_names = {
            int(item.get("optimizer_id") or 0): str(item.get("name") or "")
            for item in self.identity_resolver.list_for_admin()
        } if current.is_admin else {self._normal_optimizer(current): current.name}
        group_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        for item in page_result.get("items") or []:
            self._enrich_execution(item, optimizer_names=optimizer_names, group_cache=group_cache)
        return page_result

    def _enrich_execution(
        self,
        item: Dict[str, Any],
        *,
        optimizer_names: Mapping[int, str],
        group_cache: Optional[Dict[str, Optional[Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        group_id = str(item.get("rule_group_id") or "")
        cache = group_cache if group_cache is not None else {}
        if group_id not in cache:
            cache[group_id] = self.repository.get_rule_group(group_id, include_deleted=True)
        group = cache.get(group_id)
        summary = item.get("summary") or {}
        item["rule_group_name"] = str((group or {}).get("name") or "")
        item["products"] = list((group or {}).get("products") or [])
        item["actions"] = [
            action for action, count_key in (("pause", "pause_count"), ("copy", "copy_count"))
            if int(summary.get(count_key) or 0) > 0
        ]
        item["optimizer_name"] = optimizer_names.get(int(item.get("optimizer_id") or 0), "")
        item["target_count"] = (
            int(summary.get("planned_count") or 0)
            + int(summary.get("deferred_count") or 0)
            + int(summary.get("blocked_count") or 0)
        )
        item["meta_write_count"] = summary.get("meta_write_count")
        return item

    def get_execution(self, actor: Any, execution_id: str) -> Dict[str, Any]:
        current = Actor.from_value(actor)
        item = self.repository.get_execution(clean_text(execution_id, "execution_id", required=True))
        if not item:
            raise AdControlV3Error("execution_not_found", "execution not found", status=404)
        if not current.is_admin and int(item.get("optimizer_id") or 0) != self._normal_optimizer(current):
            raise AdControlV3Error("execution_not_found", "execution not found", status=404)
        optimizer_names = {
            int(optimizer.get("optimizer_id") or 0): str(optimizer.get("name") or "")
            for optimizer in self.identity_resolver.list_for_admin()
        } if current.is_admin else {self._normal_optimizer(current): current.name}
        self._enrich_execution(item, optimizer_names=optimizer_names)
        snapshot_metadata = {
            "relative_path": item.get("snapshot_relative_path"),
            "sha256": item.get("snapshot_sha256"),
        }
        snapshot = self.snapshot_store.read_snapshot(snapshot_metadata)
        item["snapshot_valid"] = True
        item["snapshot_header"] = {
            key: value for key, value in snapshot.items() if key != "targets"
        } if isinstance(snapshot, Mapping) else {}
        item["snapshot_target_count"] = len(snapshot.get("targets") or []) if isinstance(snapshot, Mapping) else 0
        return item

    # Short aliases keep the dynamic route dispatcher small and make the
    # public service shape match REST verbs without duplicating behavior.
    list = list_rule_groups
    create = create_rule_group
    get = get_rule_group
    update = update_rule_group
    delete = delete_rule_group
    duplicate = duplicate_rule_group


def _reason_counts(candidates: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for item in candidates:
        reason = str(item.get("blocked_reason") or "")
        if reason:
            result[reason] = result.get(reason, 0) + 1
    return result


def build_service(
    *,
    repository: Any,
    facebook_adapter: FacebookAdapter,
    identity_resolver: OptimizerIdentityResolver,
    snapshot_store: Any,
    tiktok_adapter: Optional[TikTokAdapter] = None,
    timezone_loader: Optional[Callable[[], Sequence[str]]] = None,
    clock: Callable[[], datetime] = _utc_now,
    preview_ttl_seconds: int = 1800,
    live_pause_enabled: bool = False,
    live_copy_enabled: bool = False,
    scheduler_enabled: bool = False,
    scheduler_live_enabled: bool = False,
    live_executor: Any = None,
    scan_concurrency: int = 1,
    meta_cache_ttl_seconds: int = 60,
) -> Service:
    return Service(
        repository,
        {
            "facebook": facebook_adapter,
            "tiktok": tiktok_adapter or TikTokAdapter(),
        },
        identity_resolver,
        snapshot_store,
        timezone_loader=timezone_loader,
        clock=clock,
        preview_ttl_seconds=preview_ttl_seconds,
        live_pause_enabled=live_pause_enabled,
        live_copy_enabled=live_copy_enabled,
        scheduler_enabled=scheduler_enabled,
        scheduler_live_enabled=scheduler_live_enabled,
        live_executor=live_executor,
        scan_concurrency=scan_concurrency,
        meta_cache_ttl_seconds=meta_cache_ttl_seconds,
    )


def _required_environment(name: str, *, allow_empty: bool = False) -> str:
    if name not in os.environ:
        raise AdControlV3Error(
            "service_not_configured",
            "required V3 environment is missing",
            status=503,
            details={"name": name},
        )
    raw_value = str(os.environ.get(name) or "")
    value = raw_value if allow_empty else raw_value.strip()
    if not value and not allow_empty:
        raise AdControlV3Error(
            "service_not_configured",
            "required V3 environment is empty",
            status=503,
            details={"name": name},
        )
    return value


def _mysql_environment(prefix: str, expected_port: int, database: str) -> Dict[str, Any]:
    host = _required_environment(prefix + "_HOST")
    user = _required_environment(prefix + "_USER")
    password = _required_environment(prefix + "_PASSWORD", allow_empty=True)
    raw_port = _required_environment(prefix + "_PORT")
    try:
        port = int(raw_port)
    except ValueError:
        raise AdControlV3Error("service_not_configured", "V3 MySQL port is invalid", status=503)
    if port != expected_port:
        raise AdControlV3Error(
            "unsafe_mysql_endpoint",
            "V3 MySQL role is configured on the wrong port",
            status=503,
            details={"prefix": prefix, "expected_port": expected_port},
        )
    allow_host = str(os.environ.get("AD_CONTROL_V3_ALLOW_NONSTANDARD_MYSQL_HOST") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    if host != "101.32.56.53" and not allow_host:
        raise AdControlV3Error(
            "unsafe_mysql_endpoint",
            "V3 MySQL host is not the reviewed production endpoint",
            status=503,
            details={"prefix": prefix},
        )
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database,
    }


def build_service_from_environment() -> Service:
    """Lazily construct the production service after authentication.

    Importing this module performs no I/O. This function requires three
    explicit roles: source/store reader on 63350 and store writer on 63353.
    """
    try:
        import pymysql
    except ImportError as exc:
        raise AdControlV3Error("service_not_configured", "pymysql is unavailable", status=503) from exc

    source_config = _mysql_environment("AD_CONTROL_V3_SOURCE_READER_MYSQL", 63350, "kunlunads_dev")
    store_reader_config = _mysql_environment("AD_CONTROL_V3_STORE_READER_MYSQL", 63350, "ads_ai")
    store_writer_config = _mysql_environment("AD_CONTROL_V3_STORE_WRITER_MYSQL", 63353, "ads_ai")
    connect_timeout = _bounded_environment_int(
        "AD_CONTROL_V3_MYSQL_CONNECT_TIMEOUT_SECONDS", 3, 1, 3
    )
    store_io_timeout = _bounded_environment_int(
        "AD_CONTROL_V3_MYSQL_IO_TIMEOUT_SECONDS", 5, 1, 5
    )
    # Source aggregates have an 8s server circuit breaker. Keep their socket
    # timeout above that limit while preserving a strict 10s client ceiling.
    source_read_timeout = _bounded_environment_int(
        "AD_CONTROL_V3_SOURCE_MYSQL_READ_TIMEOUT_SECONDS",
        10,
        SOURCE_MYSQL_READ_TIMEOUT_MIN_SECONDS,
        SOURCE_MYSQL_READ_TIMEOUT_MAX_SECONDS,
    )
    if SOURCE_QUERY_MAX_EXECUTION_TIME_MS >= source_read_timeout * 1000:
        raise AdControlV3Error(
            "service_not_configured",
            "source server timeout must be below source socket timeout",
            status=503,
        )
    raw_scan_concurrency = str(os.environ.get("AD_CONTROL_V3_SCAN_CONCURRENCY", "1")).strip()
    try:
        scan_concurrency = int(raw_scan_concurrency)
    except ValueError:
        raise AdControlV3Error("service_not_configured", "scan concurrency is invalid", status=503)
    if scan_concurrency < 1 or scan_concurrency > 4:
        raise AdControlV3Error(
            "service_not_configured",
            "scan concurrency must be within 1..4",
            status=503,
        )

    def connection(
        config: Mapping[str, Any],
        autocommit: bool,
        *,
        read_timeout: int,
    ):
        return pymysql.connect(
            host=config["host"],
            port=int(config["port"]),
            user=config["user"],
            password=config["password"],
            database=config["database"],
            charset="utf8mb4",
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            write_timeout=store_io_timeout,
            autocommit=autocommit,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def source_reader():
        return connection(source_config, True, read_timeout=source_read_timeout)

    def store_reader():
        return connection(store_reader_config, True, read_timeout=store_io_timeout)

    def store_writer():
        return connection(store_writer_config, False, read_timeout=store_io_timeout)

    def source_query(sql: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
        return _execute_bounded_source_query(source_reader, sql, params)

    def optimizer_candidates(actor: Actor) -> List[Dict[str, Any]]:
        base = (
            "SELECT DISTINCT au.id AS optimizer_id,COALESCE(NULLIF(au.name,''),au.username) AS name,COALESCE(aug.email,'') AS email "
            "FROM `kunlunads_dev`.admin_user_group aug "
            "JOIN `kunlunads_dev`.admin_users au ON au.id=aug.sub_user_id "
            "WHERE aug.status=0 AND {predicate} ORDER BY au.id LIMIT 10"
        )
        identity_layers = [("CAST(aug.user_id AS CHAR)=%s", (actor.user_id,))]
        if actor.user_id.isdigit():
            identity_layers.append(("aug.sub_user_id=%s", (int(actor.user_id),)))
        if actor.email:
            identity_layers.append(("LOWER(TRIM(aug.email))=LOWER(TRIM(%s))", (actor.email,)))
        if actor.name:
            identity_layers.append(("TRIM(aug.name)=TRIM(%s)", (actor.name,)))
        for predicate, params in identity_layers:
            rows = source_query(base.format(predicate=predicate), params)
            if rows:
                return rows
        return []

    def active_optimizers() -> List[Dict[str, Any]]:
        return source_query(
            "SELECT DISTINCT au.id AS optimizer_id,COALESCE(NULLIF(au.name,''),au.username) AS name,COALESCE(aug.email,'') AS email "
            "FROM `kunlunads_dev`.admin_user_group aug "
            "JOIN `kunlunads_dev`.admin_users au ON au.id=aug.sub_user_id "
            "WHERE aug.status=0 ORDER BY au.id LIMIT 5000",
            (),
        )

    def timezones() -> List[str]:
        rows = source_query(
            "SELECT DISTINCT CAST(time_zone AS CHAR) AS time_zone "
            "FROM `kunlunads_dev`.ads_accounts_setting "
            "WHERE platform_id=%s AND time_zone IS NOT NULL AND CAST(time_zone AS CHAR)<>'' "
            "ORDER BY time_zone LIMIT 500",
            (FACEBOOK_ACCOUNT_SETTINGS_PLATFORM_ID,),
        )
        return [str(row.get("time_zone") or "") for row in rows]

    def validate_source_schema() -> None:
        insight_rows = source_query("SHOW COLUMNS FROM `kunlunads_dev`.ads_custom_source_insight", ())
        insight_index_rows = source_query("SHOW INDEX FROM `kunlunads_dev`.ads_custom_source_insight", ())
        _validate_insight_schema_rows(insight_rows, insight_index_rows)

    def validate_timezone_schema() -> None:
        account_rows = source_query("SHOW COLUMNS FROM `kunlunads_dev`.ads_accounts_setting", ())
        account_index_rows = source_query("SHOW INDEX FROM `kunlunads_dev`.ads_accounts_setting", ())
        _validate_timezone_schema_rows(account_rows, account_index_rows)

    data_root = SafeDataRoot(
        _required_environment("AD_CONTROL_V3_DATA_ROOT"),
        require_distinct_device=True,
        app_root=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
        max_uncompressed_bytes=int(os.environ.get("AD_CONTROL_V3_SNAPSHOT_MAX_RAW_BYTES", str(64 * 1024 * 1024))),
        max_compressed_bytes=int(os.environ.get("AD_CONTROL_V3_SNAPSHOT_MAX_GZIP_BYTES", str(32 * 1024 * 1024))),
        min_free_bytes=int(os.environ.get("AD_CONTROL_V3_DATA_MIN_FREE_BYTES", str(1024 * 1024 * 1024))),
    )
    repository = MySQLRepository(store_reader, store_writer)
    identity_resolver = OptimizerIdentityResolver(optimizer_candidates, active_optimizers)
    live_executor = build_live_executor(
        source_reader,
        store_reader,
        store_writer,
        environment=os.environ,
    )
    live_capabilities = live_executor.capabilities()
    runner_enabled = str(os.environ.get("AD_CONTROL_V3_RUNNER_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
    observe_released = str(os.environ.get("AD_CONTROL_V3_RUNNER_OBSERVE_RELEASED") or "").strip().lower() in {"1", "true", "yes", "on"}
    scheduler_enabled = runner_enabled and observe_released
    scheduler_live_enabled = scheduler_enabled and str(os.environ.get("AD_CONTROL_V3_RUNNER_LIVE_RELEASED") or "").strip().lower() in {"1", "true", "yes", "on"}
    return build_service(
        repository=repository,
        facebook_adapter=FacebookAdapter(
            source_query,
            schema_validator=validate_source_schema,
            timezone_schema_validator=validate_timezone_schema,
        ),
        identity_resolver=identity_resolver,
        snapshot_store=data_root,
        timezone_loader=timezones,
        preview_ttl_seconds=int(os.environ.get("AD_CONTROL_V3_PREVIEW_TTL_SECONDS", "1800")),
        live_pause_enabled=live_capabilities["live_pause_enabled"],
        live_copy_enabled=live_capabilities["live_copy_enabled"],
        scheduler_enabled=scheduler_enabled,
        scheduler_live_enabled=scheduler_live_enabled,
        live_executor=live_executor,
        scan_concurrency=scan_concurrency,
        meta_cache_ttl_seconds=_bounded_environment_int(
            "AD_CONTROL_V3_META_CACHE_TTL_SECONDS", 60, 15, 300
        ),
    )


_SERVICE_LOCK = threading.RLock()
_SERVICE: Optional[Service] = None


def configure_service(service: Service) -> Service:
    if not isinstance(service, Service):
        raise AdControlV3Error("service_not_configured", "invalid V3 service instance", status=503)
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = service
    return service


def get_service() -> Service:
    """Return a configured or lazily environment-built service; never memory."""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = build_service_from_environment()
        return _SERVICE
