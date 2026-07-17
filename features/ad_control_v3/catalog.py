"""Server-owned capability, product and optimizer catalog contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from .errors import AdControlV3Error
from .schemas import ACTIONS, OBJECT_LEVELS, Actor


NUMERIC_OPERATORS = ("gt", "gte", "lt", "lte", "eq", "ne", "between", "exists", "not_exists")
ENUM_OPERATORS = ("eq", "ne", "in", "not_in", "exists", "not_exists")
TEXT_OPERATORS = ("eq", "ne", "contains", "not_contains", "starts_with", "exists", "not_exists")
TIME_OPERATORS = ("before", "after", "between", "within_last_days", "older_than_days", "exists", "not_exists")
BOOLEAN_OPERATORS = ("eq", "ne", "exists", "not_exists")


@dataclass(frozen=True)
class FieldCapability:
    key: str
    label: str
    value_type: str
    levels: Sequence[str]
    source: str
    filterable: bool
    previewable: bool
    live_ready: bool = False
    options: Sequence[str] = ()

    @property
    def operators(self) -> Sequence[str]:
        return {
            "number": NUMERIC_OPERATORS,
            "enum": ENUM_OPERATORS,
            "text": TEXT_OPERATORS,
            "time": TIME_OPERATORS,
            "boolean": BOOLEAN_OPERATORS,
            "multi_text": TEXT_OPERATORS,
            "multi_enum": ENUM_OPERATORS,
        }.get(self.value_type, TEXT_OPERATORS)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value_type": self.value_type,
            "levels": list(self.levels),
            "source": self.source,
            "operators": list(self.operators),
            "filterable": self.filterable,
            "previewable": self.previewable,
            "live_ready": self.live_ready,
            "options": list(self.options),
        }


ALL_LEVELS = tuple(OBJECT_LEVELS)
CAMPAIGN_ONLY = ("campaign",)
ADSET_ONLY = ("adset",)
AD_ONLY = ("ad",)
ADSET_AND_AD = ("adset", "ad")


# Only fields backed by the bounded insight query are previewable in this
# release. Richer fields stay visible as roadmap capabilities without allowing
# the UI to save a condition the backend cannot evaluate reliably.
FACEBOOK_FIELDS: Sequence[FieldCapability] = (
    FieldCapability("object_id", "对象 ID", "text", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("object_name", "对象名称", "text", ALL_LEVELS, "meta", False, False),
    FieldCapability("campaign_id", "Campaign ID", "text", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("adset_id", "Ad Set ID", "text", ADSET_AND_AD, "custom_source_insight", True, True),
    FieldCapability("ad_id", "Ad ID", "text", AD_ONLY, "custom_source_insight", True, True),
    FieldCapability("product", "产品", "enum", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("series_code", "剧目编码", "multi_text", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("content_id", "内容 ID", "text", ALL_LEVELS, "unavailable", False, False),
    FieldCapability("resource_id", "资源 ID", "text", AD_ONLY, "custom_source_insight", True, True),
    FieldCapability("resource_name", "资源名称", "text", AD_ONLY, "custom_source_insight", True, True),
    FieldCapability("source_id", "来源素材 ID", "text", AD_ONLY, "custom_source_insight", True, True),
    FieldCapability("app", "APP", "multi_enum", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("app_id", "APP ID", "multi_text", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("os_type", "系统类型", "multi_enum", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("w2a_page_id", "W2A 页面 ID", "text", AD_ONLY, "custom_source_insight", True, True),
    FieldCapability("ad_type", "广告类型", "enum", AD_ONLY, "custom_source_insight", True, True),
    FieldCapability("category", "素材分类", "enum", AD_ONLY, "custom_source_insight", True, True),
    FieldCapability("resource_tag", "资源标签", "text", AD_ONLY, "custom_source_insight", True, True),
    FieldCapability("source_type", "来源类型", "enum", AD_ONLY, "custom_source_insight", True, True),
    FieldCapability("resource_type", "资源类型", "enum", AD_ONLY, "custom_source_insight", True, True),
    FieldCapability("created_data_id", "发布数据 ID", "text", AD_ONLY, "custom_source_insight", True, True),
    FieldCapability("task_id", "任务 ID", "text", AD_ONLY, "custom_source_insight", True, True),
    FieldCapability("bid_type", "出价类型", "multi_enum", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("page_id", "主页 ID", "multi_text", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("task_type", "投放任务类型", "multi_enum", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("country", "国家", "multi_enum", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("language", "语言", "multi_enum", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("country_group", "国家组", "multi_enum", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("drama_language", "剧目语言", "multi_enum", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("latest_auto_publish_dt", "最近自动发布日", "time", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("latest_resource_created_at", "最近资源创建时间", "time", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("latest_spend_at", "最近消耗时间", "time", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("spend", "消耗", "number", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("impressions", "展示", "number", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("clicks", "点击", "number", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("installs", "安装", "number", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("purchase", "购买", "number", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("revenue", "收入", "number", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("day1_retain", "次日留存", "number", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("retain_install", "留存安装基数", "number", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("retention_rate", "次日留存率", "number", ALL_LEVELS, "computed", True, True),
    FieldCapability("events", "事件数", "number", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("atc", "加购", "number", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("delivery_cnt", "交付数", "number", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("af_installs", "AF 安装", "number", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("af_revenue", "AF 收入", "number", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("af_roas", "AF ROAS", "number", ALL_LEVELS, "computed", True, True),
    FieldCapability("ad_impression", "广告变现展示", "number", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("ad_impression_revenue", "广告变现收入", "number", ALL_LEVELS, "custom_source_insight", True, True),
    FieldCapability("ad_impression_roas", "广告变现 ROAS", "number", ALL_LEVELS, "computed", True, True),
    FieldCapability("ctr", "CTR", "number", ALL_LEVELS, "computed", True, True),
    FieldCapability("cpm", "CPM", "number", ALL_LEVELS, "computed", True, True),
    FieldCapability("cpc", "CPC", "number", ALL_LEVELS, "computed", True, True),
    FieldCapability("cpi", "CPI", "number", ALL_LEVELS, "computed", True, True),
    FieldCapability("purchase_cpa", "Purchase CPA", "number", ALL_LEVELS, "computed", True, True),
    FieldCapability("roas", "ROAS", "number", ALL_LEVELS, "computed", True, True),
    FieldCapability("effective_status", "有效状态", "enum", ALL_LEVELS, "meta", False, False),
    FieldCapability("configured_status", "配置状态", "enum", ALL_LEVELS, "meta", False, False),
    FieldCapability("budget", "当前预算", "number", ALL_LEVELS, "meta", False, False),
    FieldCapability("bid_control", "Bid Control", "number", ALL_LEVELS, "meta", False, False),
    FieldCapability("objective", "Campaign Objective", "enum", CAMPAIGN_ONLY, "meta", False, False),
    FieldCapability("is_cbo", "CBO", "boolean", CAMPAIGN_ONLY, "meta", False, False),
    FieldCapability("optimization_goal", "Optimization Goal", "enum", ADSET_ONLY, "meta", False, False),
    FieldCapability("billing_event", "Billing Event", "enum", ADSET_ONLY, "meta", False, False),
    FieldCapability("creative_id", "Creative ID", "text", AD_ONLY, "meta", False, False),
)


def facebook_field_catalog(object_level: Optional[str] = None) -> List[Dict[str, Any]]:
    level = str(object_level or "").strip().lower()
    return [
        field.as_dict()
        for field in FACEBOOK_FIELDS
        if not level or level in field.levels
    ]


def field_map(object_level: str) -> Dict[str, FieldCapability]:
    return {
        field.key: field
        for field in FACEBOOK_FIELDS
        if object_level in field.levels
    }


def validate_rules_against_catalog(rules: Sequence[Mapping[str, Any]], object_level: str) -> None:
    available = field_map(object_level)
    for rule in rules:
        for condition in rule.get("conditions") or []:
            key = str(condition.get("field") or "")
            capability = available.get(key)
            if not capability or not capability.filterable or not capability.previewable:
                raise AdControlV3Error(
                    "field_not_supported",
                    "field is not previewable for this object level",
                    details={"field": key, "object_level": object_level},
                )
            operator = str(condition.get("operator") or "")
            if operator not in capability.operators:
                raise AdControlV3Error(
                    "operator_not_supported",
                    "operator is not supported for this field",
                    details={"field": key, "operator": operator},
                )
            if operator in {"exists", "not_exists"}:
                continue
            value = condition.get("value")
            if operator in {"within_last_days", "older_than_days"}:
                if isinstance(value, bool):
                    raise AdControlV3Error("condition_value_invalid", "relative-day value must be an integer", details={"field": key})
                try:
                    day_value = int(value)
                except (TypeError, ValueError):
                    raise AdControlV3Error("condition_value_invalid", "relative-day value must be an integer", details={"field": key})
                if day_value < 1 or day_value > 3650 or float(value) != float(day_value):
                    raise AdControlV3Error("condition_value_invalid", "relative-day value must be within 1..3650", details={"field": key})
                continue
            if operator == "between":
                if not isinstance(value, (list, tuple)) or len(value) != 2:
                    raise AdControlV3Error("condition_value_invalid", "between requires exactly two values", details={"field": key})
                values = list(value)
            elif operator in {"in", "not_in"}:
                if not isinstance(value, (list, tuple)) or not value or len(value) > 100:
                    raise AdControlV3Error("condition_value_invalid", "in/not_in requires 1-100 values", details={"field": key})
                values = list(value)
            else:
                values = [value]
            for item in values:
                if item in (None, ""):
                    raise AdControlV3Error("condition_value_invalid", "condition value cannot be empty", details={"field": key})
                if capability.value_type == "number":
                    try:
                        numeric = Decimal(str(item))
                    except (InvalidOperation, TypeError, ValueError):
                        raise AdControlV3Error("condition_value_invalid", "numeric condition value is invalid", details={"field": key})
                    if not numeric.is_finite():
                        raise AdControlV3Error("condition_value_invalid", "numeric condition value must be finite", details={"field": key})
                elif capability.value_type == "time":
                    try:
                        datetime.fromisoformat(str(item).replace("Z", "+00:00"))
                    except ValueError:
                        raise AdControlV3Error("condition_value_invalid", "time condition value is invalid", details={"field": key})
                elif len(str(item)) > 1000:
                    raise AdControlV3Error("condition_value_invalid", "condition value is too long", details={"field": key})


class OptimizerIdentityResolver:
    """Resolve the current non-admin user to one or more active optimizers.

    ``candidate_loader`` is supplied by the integration layer and must query
    the authoritative admin user/group tables. Multiple optimizer aliases are
    accepted only when the loader proves they came from an exact strong
    identity layer (``user_id`` or ``email``). A name-only collision remains
    fail-closed.
    """

    TRUSTED_MULTI_IDENTITY_LAYERS = frozenset({"user_id", "email"})

    def __init__(
        self,
        candidate_loader: Callable[[Actor], Iterable[Any]],
        optimizer_loader: Optional[Callable[[], Iterable[Mapping[str, Any]]]] = None,
    ) -> None:
        self._candidate_loader = candidate_loader
        self._optimizer_loader = optimizer_loader or (lambda: [])

    @staticmethod
    def _ids(rows: Iterable[Any]) -> List[int]:
        values: Set[int] = set()
        for row in rows or []:
            if isinstance(row, Mapping):
                raw = row.get("optimizer_id", row.get("id"))
            else:
                raw = row
            try:
                parsed = int(raw)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                values.add(parsed)
        return sorted(values)

    @classmethod
    def _items(cls, rows: Iterable[Any]) -> List[Dict[str, Any]]:
        values: Dict[int, Dict[str, Any]] = {}
        for row in rows or []:
            if isinstance(row, Mapping):
                raw = row.get("optimizer_id", row.get("id"))
                source = dict(row)
            else:
                raw = row
                source = {}
            try:
                optimizer_id = int(raw)
            except (TypeError, ValueError):
                continue
            if optimizer_id <= 0:
                continue
            identity_layer = str(source.get("identity_layer") or "").strip().lower()
            item = {
                "optimizer_id": optimizer_id,
                "name": str(source.get("name") or source.get("username") or optimizer_id),
                "email": str(source.get("email") or ""),
                "identity_layer": identity_layer,
            }
            current = values.get(optimizer_id)
            if current is None or (
                identity_layer in cls.TRUSTED_MULTI_IDENTITY_LAYERS
                and current.get("identity_layer") not in cls.TRUSTED_MULTI_IDENTITY_LAYERS
            ):
                values[optimizer_id] = item
        return [values[key] for key in sorted(values)]

    def resolve_items_for_actor(self, actor: Actor) -> List[Dict[str, Any]]:
        items = self._items(self._candidate_loader(actor))
        if actor.optimizer_id is not None:
            items = [item for item in items if item["optimizer_id"] == actor.optimizer_id]
        if not items:
            raise AdControlV3Error(
                "optimizer_identity_unresolved",
                "current user is not mapped to an active optimizer",
                status=403,
            )
        if len(items) > 1:
            layers = {str(item.get("identity_layer") or "") for item in items}
            if not layers or not layers.issubset(self.TRUSTED_MULTI_IDENTITY_LAYERS):
                raise AdControlV3Error(
                    "optimizer_identity_ambiguous",
                    "current user maps to multiple untrusted optimizer identities",
                    status=409,
                    details={"candidate_count": len(items), "identity_layers": sorted(layers)},
                )
        return items

    def resolve_ids_for_actor(self, actor: Actor) -> List[int]:
        return [item["optimizer_id"] for item in self.resolve_items_for_actor(actor)]

    def resolve_for_actor(self, actor: Actor) -> int:
        candidates = self.resolve_ids_for_actor(actor)
        if len(candidates) != 1:
            raise AdControlV3Error(
                "optimizer_identity_ambiguous",
                "current user maps to multiple active optimizer aliases",
                status=409,
                details={"candidate_count": len(candidates)},
            )
        return candidates[0]

    def list_for_admin(self) -> List[Dict[str, Any]]:
        items: Dict[int, Dict[str, Any]] = {}
        for row in self._optimizer_loader() or []:
            try:
                optimizer_id = int(row.get("optimizer_id", row.get("id")))
            except (AttributeError, TypeError, ValueError):
                continue
            if optimizer_id <= 0:
                continue
            items[optimizer_id] = {
                "optimizer_id": optimizer_id,
                "name": str(row.get("name") or row.get("username") or optimizer_id),
                "email": str(row.get("email") or ""),
            }
        return [items[key] for key in sorted(items)]


class StaticOptimizerIdentityResolver(OptimizerIdentityResolver):
    """Small deterministic resolver for unit tests and local UI stubs."""

    def __init__(
        self,
        mapping: Optional[Mapping[str, Sequence[Any]]] = None,
        optimizers: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> None:
        self.mapping = {str(key): list(value) for key, value in (mapping or {}).items()}
        self.optimizers = list(optimizers or [])
        super().__init__(
            lambda actor: self.mapping.get(actor.user_id, []),
            lambda: self.optimizers,
        )
