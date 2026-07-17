"""Persistence boundary for the isolated V3 control service.

Only the eight reviewed ``ads_ai.ad_control_v3_*`` tables may be written. The
repository exposes no generic table or SQL arguments to callers.
"""

from __future__ import annotations

import copy
import json
import threading
from datetime import datetime
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import AdControlV3Error
from .schemas import deserialize_json, serialize_for_store
from .time_utils import utc8_business_date, utc8_date_bounds


ADS_AI_DATABASE = "ads_ai"
TABLES = {
    "product_catalog": "ad_control_v3_product_catalog",
    "rule_group": "ad_control_v3_rule_group",
    "rule_group_product": "ad_control_v3_rule_group_product",
    "preview": "ad_control_v3_preview",
    "preview_target": "ad_control_v3_preview_target",
    "execution": "ad_control_v3_execution",
    "execution_target": "ad_control_v3_execution_target",
    "runner_event": "ad_control_v3_runner_event",
}
TARGET_INSERT_CHUNK_SIZE = 500
MAX_PERSISTED_TARGETS = 20000


def _validate_target_count(targets: Sequence[Mapping[str, Any]]) -> None:
    if len(targets) > MAX_PERSISTED_TARGETS:
        raise AdControlV3Error(
            "target_persist_limit_exceeded",
            "target count exceeds the reviewed persistence limit",
            status=409,
            details={"limit": MAX_PERSISTED_TARGETS},
        )


def _executemany_chunks(cursor: Any, sql: str, rows: Sequence[Sequence[Any]]) -> None:
    for offset in range(0, len(rows), TARGET_INSERT_CHUNK_SIZE):
        cursor.executemany(sql, rows[offset : offset + TARGET_INSERT_CHUNK_SIZE])


def qualified_table(key: str) -> str:
    if key not in TABLES:
        raise AdControlV3Error("unsafe_repository_table", "repository table is not allowlisted")
    return "`%s`.`%s`" % (ADS_AI_DATABASE, TABLES[key])


def _page(page: Any, page_size: Any) -> Tuple[int, int]:
    try:
        parsed_page = max(1, int(page or 1))
        parsed_size = max(1, min(200, int(page_size or 20)))
    except (TypeError, ValueError):
        raise AdControlV3Error("validation_error", "invalid pagination")
    return parsed_page, parsed_size


def _filter_bool(value: Any, field: str = "enabled") -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    raise AdControlV3Error("validation_error", "%s filter must be boolean" % field)


def _filter_products(filters: Mapping[str, Any]) -> List[str]:
    raw = filters.get("products")
    if raw in (None, ""):
        raw = [filters["product"]] if filters.get("product") else []
    elif isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)) or len(raw) > 50:
        raise AdControlV3Error("validation_error", "products filter must be a list of at most 50 values")
    result: List[str] = []
    for value in raw:
        item = str(value or "").strip()
        if not item or len(item) > 128:
            raise AdControlV3Error("validation_error", "invalid products filter")
        if item not in result:
            result.append(item)
    return result


def _like_keyword(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > 128:
        raise AdControlV3Error("validation_error", "keyword is too long")
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


EXECUTION_FILTERS = {
    "rule_group_id", "channel", "object_level", "run_mode", "status", "trigger_source",
    "optimizer_id", "product", "products", "action", "object_id", "keyword", "date_from", "date_to",
}
GROUP_FILTERS = {"channel", "object_level", "run_mode", "optimizer_id", "enabled", "product", "products", "keyword", "query"}


def _validated_group_filters(filters: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    result = dict(filters or {})
    unknown = sorted(set(result) - GROUP_FILTERS)
    if unknown:
        raise AdControlV3Error("validation_error", "unsupported rule-group filters", details={"fields": unknown})
    _filter_products(result)
    if result.get("enabled") not in (None, ""):
        _filter_bool(result["enabled"])
    return result


def _validated_execution_filters(filters: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    result = dict(filters or {})
    unknown = sorted(set(result) - EXECUTION_FILTERS)
    if unknown:
        raise AdControlV3Error("validation_error", "unsupported execution filters", details={"fields": unknown})
    for key in ("date_from", "date_to"):
        if result.get(key):
            try:
                datetime.strptime(str(result[key]), "%Y-%m-%d")
            except ValueError:
                raise AdControlV3Error("validation_error", "%s must use YYYY-MM-DD" % key)
    if result.get("date_from") and result.get("date_to") and str(result["date_from"]) > str(result["date_to"]):
        raise AdControlV3Error("validation_error", "date_from must not exceed date_to")
    if result.get("action") and result["action"] not in {"pause", "copy"}:
        raise AdControlV3Error("validation_error", "unsupported action filter")
    for key, maximum in (("object_id", 64), ("keyword", 128), ("rule_group_id", 64)):
        if len(str(result.get(key) or "")) > maximum:
            raise AdControlV3Error("validation_error", "%s filter is too long" % key)
    _filter_products(result)
    return result


class MemoryRepository:
    """Thread-safe injectable repository used by tests and local UI stubs."""

    def __init__(self, products: Optional[Sequence[Mapping[str, Any]]] = None) -> None:
        self._lock = threading.RLock()
        self.products: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.groups: Dict[str, Dict[str, Any]] = {}
        self.previews: Dict[str, Dict[str, Any]] = {}
        self.executions: Dict[str, Dict[str, Any]] = {}
        self.runner_events: Dict[str, Dict[str, Any]] = {}
        for item in products or []:
            channel = str(item.get("channel") or "facebook")
            product = str(item.get("product_value") or "")
            if product:
                normalized = dict(item)
                normalized.setdefault("enabled", True)
                normalized.setdefault("product_type", "short_drama")
                self.products[(channel, product)] = normalized

    def list_products(self, channel: str, *, include_disabled: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            items = [
                copy.deepcopy(item)
                for (item_channel, _), item in self.products.items()
                if item_channel == channel and (include_disabled or bool(item.get("enabled")))
            ]
        return sorted(items, key=lambda item: str(item.get("product_value") or "").lower())

    def create_rule_group(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        group = copy.deepcopy(dict(record))
        group_id = str(group.get("group_id") or "")
        with self._lock:
            if not group_id or group_id in self.groups:
                raise AdControlV3Error("rule_group_conflict", "rule group id already exists", status=409)
            self.groups[group_id] = group
        return copy.deepcopy(group)

    def get_rule_group(self, group_id: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        with self._lock:
            group = self.groups.get(str(group_id))
            if not group or (group.get("deleted") and not include_deleted):
                return None
            return copy.deepcopy(group)

    def list_rule_groups(
        self,
        filters: Optional[Mapping[str, Any]] = None,
        *,
        page: int = 1,
        page_size: int = 20,
        optimizer_scope: Optional[int] = None,
    ) -> Dict[str, Any]:
        page, page_size = _page(page, page_size)
        filters = _validated_group_filters(filters)
        with self._lock:
            rows = [copy.deepcopy(item) for item in self.groups.values() if not item.get("deleted")]
        if optimizer_scope is not None:
            rows = [item for item in rows if int(item.get("optimizer_id") or 0) == int(optimizer_scope)]
        for key in ("channel", "object_level", "run_mode"):
            if filters.get(key):
                rows = [item for item in rows if str(item.get(key) or "") == str(filters[key])]
        if filters.get("optimizer_id") not in (None, ""):
            rows = [item for item in rows if int(item.get("optimizer_id") or 0) == int(filters["optimizer_id"])]
        if filters.get("enabled") not in (None, ""):
            enabled = _filter_bool(filters["enabled"])
            rows = [item for item in rows if bool(item.get("enabled")) is enabled]
        selected_products = _filter_products(filters)
        if selected_products:
            selected = {str(item) for item in selected_products}
            rows = [item for item in rows if selected.intersection({str(value) for value in item.get("products") or []})]
        keyword = filters.get("keyword", filters.get("query"))
        if keyword:
            query = str(keyword).strip().lower()
            rows = [
                item for item in rows
                if query in str(item.get("name") or "").lower()
                or query in str(item.get("group_id") or "").lower()
            ]
        rows.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("group_id") or "")), reverse=True)
        total = len(rows)
        start = (page - 1) * page_size
        return {"items": rows[start : start + page_size], "page": page, "page_size": page_size, "total": total}

    def update_rule_group(
        self,
        group_id: str,
        record: Mapping[str, Any],
        *,
        expected_version: int,
    ) -> Dict[str, Any]:
        with self._lock:
            current = self.groups.get(str(group_id))
            if not current or current.get("deleted"):
                raise AdControlV3Error("rule_group_not_found", "rule group not found", status=404)
            if int(current.get("config_version") or 0) != int(expected_version):
                raise AdControlV3Error("version_conflict", "rule group was changed", status=409)
            updated = copy.deepcopy(dict(record))
            self.groups[str(group_id)] = updated
            return copy.deepcopy(updated)

    def soft_delete_rule_group(self, group_id: str, *, updated_by: str, updated_at: str) -> bool:
        with self._lock:
            group = self.groups.get(str(group_id))
            if not group or group.get("deleted"):
                return False
            group.update(
                {
                    "deleted": True,
                    "enabled": False,
                    "updated_by_user_id": updated_by,
                    "updated_at": updated_at,
                }
            )
            return True

    def set_group_state(
        self,
        group_id: str,
        *,
        enabled: Optional[bool] = None,
        emergency_stopped: Optional[bool] = None,
        updated_by: str,
        updated_at: str,
        expected_version: Optional[int] = None,
        expected_behavior_hash: str = "",
        expected_preview_id: str = "",
        require_fresh_preview: bool = False,
        clear_preview: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
            group = self.groups.get(str(group_id))
            if not group or group.get("deleted"):
                raise AdControlV3Error("rule_group_not_found", "rule group not found", status=404)
            if expected_version is not None and int(group.get("config_version") or 0) != int(expected_version):
                raise AdControlV3Error("stale_preview", "rule group changed during enable", status=409)
            if expected_behavior_hash and str(group.get("behavior_hash") or "") != expected_behavior_hash:
                raise AdControlV3Error("stale_preview", "rule group changed during enable", status=409)
            if expected_preview_id and str(group.get("last_preview_id") or "") != expected_preview_id:
                raise AdControlV3Error("stale_preview", "rule group preview changed during enable", status=409)
            if require_fresh_preview:
                preview = self.previews.get(expected_preview_id)
                if not preview or preview.get("status") != "ready":
                    raise AdControlV3Error("stale_preview", "preview is no longer ready", status=409)
            if enabled is not None:
                group["enabled"] = bool(enabled)
            if emergency_stopped is not None:
                group["emergency_stopped"] = bool(emergency_stopped)
            if clear_preview:
                group["last_preview_id"] = ""
                group["last_preview_hash"] = ""
            group["updated_by_user_id"] = updated_by
            group["updated_at"] = updated_at
            return copy.deepcopy(group)

    def emergency_stop_all(self, *, optimizer_scope: Optional[int], updated_by: str, updated_at: str) -> int:
        count = 0
        with self._lock:
            for group in self.groups.values():
                if group.get("deleted"):
                    continue
                if optimizer_scope is not None and int(group.get("optimizer_id") or 0) != int(optimizer_scope):
                    continue
                group.update(
                    {
                        "enabled": False,
                        "emergency_stopped": True,
                        "last_preview_id": "",
                        "last_preview_hash": "",
                        "updated_by_user_id": updated_by,
                        "updated_at": updated_at,
                    }
                )
                count += 1
        return count

    def save_preview(self, record: Mapping[str, Any], targets: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        _validate_target_count(targets)
        preview = copy.deepcopy(dict(record))
        preview["targets"] = copy.deepcopy(list(targets))
        preview_id = str(preview.get("preview_id") or "")
        with self._lock:
            if not preview_id or preview_id in self.previews:
                raise AdControlV3Error("preview_conflict", "preview already exists", status=409)
            self.previews[preview_id] = preview
            group = self.groups.get(str(preview.get("rule_group_id") or ""))
            if group:
                group["last_preview_id"] = preview_id
                group["last_preview_hash"] = preview.get("behavior_hash")
        return copy.deepcopy(preview)

    def get_preview(self, preview_id: str, *, include_targets: bool = True) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self.previews.get(str(preview_id))
            if not item:
                return None
            result = copy.deepcopy(item)
            if not include_targets:
                result.pop("targets", None)
            return result

    def save_execution(self, record: Mapping[str, Any], targets: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        _validate_target_count(targets)
        execution = copy.deepcopy(dict(record))
        execution["targets"] = copy.deepcopy(list(targets))
        execution_id = str(execution.get("execution_id") or "")
        with self._lock:
            if not execution_id or execution_id in self.executions:
                raise AdControlV3Error("execution_conflict", "execution already exists", status=409)
            self.executions[execution_id] = execution
        return copy.deepcopy(execution)

    def save_preview_execution_bundle(
        self,
        preview_record: Mapping[str, Any],
        execution_record: Mapping[str, Any],
        targets: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        _validate_target_count(targets)
        preview_id = str(preview_record.get("preview_id") or "")
        execution_id = str(execution_record.get("execution_id") or "")
        group_id = str(preview_record.get("rule_group_id") or "")
        with self._lock:
            group = self.groups.get(group_id)
            if (
                not group
                or group.get("deleted")
                or int(group.get("config_version") or 0) != int(preview_record.get("config_version") or 0)
                or str(group.get("behavior_hash") or "") != str(preview_record.get("behavior_hash") or "")
            ):
                raise AdControlV3Error("stale_preview", "rule group changed during preview", status=409)
            if not preview_id or preview_id in self.previews or not execution_id or execution_id in self.executions:
                raise AdControlV3Error("preview_conflict", "preview/execution id already exists", status=409)
            preview = copy.deepcopy(dict(preview_record))
            preview["targets"] = copy.deepcopy(list(targets))
            execution = copy.deepcopy(dict(execution_record))
            execution["targets"] = copy.deepcopy(list(targets))
            self.previews[preview_id] = preview
            self.executions[execution_id] = execution
            group["last_preview_id"] = preview_id
            group["last_preview_hash"] = preview_record.get("behavior_hash")
        return {"preview": copy.deepcopy(preview), "execution": copy.deepcopy(execution)}

    def list_executions(
        self,
        filters: Optional[Mapping[str, Any]] = None,
        *,
        page: int = 1,
        page_size: int = 20,
        optimizer_scope: Optional[int] = None,
    ) -> Dict[str, Any]:
        page, page_size = _page(page, page_size)
        filters = _validated_execution_filters(filters)
        with self._lock:
            rows = [copy.deepcopy(item) for item in self.executions.values()]
        if optimizer_scope is not None:
            rows = [item for item in rows if int(item.get("optimizer_id") or 0) == int(optimizer_scope)]
        for key in ("rule_group_id", "channel", "object_level", "run_mode", "status", "trigger_source"):
            if filters.get(key):
                rows = [item for item in rows if str(item.get(key) or "") == str(filters[key])]
        if filters.get("optimizer_id") not in (None, ""):
            rows = [item for item in rows if int(item.get("optimizer_id") or 0) == int(filters["optimizer_id"])]
        selected_products = set(_filter_products(filters))
        if selected_products:
            rows = [
                item
                for item in rows
                if selected_products.intersection(
                    {str(target.get("product") or "") for target in item.get("targets") or []}
                )
            ]
        if filters.get("action"):
            rows = [item for item in rows if any(target.get("action") == filters["action"] for target in item.get("targets") or [])]
        if filters.get("object_id"):
            object_id = str(filters["object_id"])
            rows = [item for item in rows if any(str(target.get("object_id") or "") == object_id for target in item.get("targets") or [])]
        if filters.get("date_from") or filters.get("date_to"):
            date_from = str(filters.get("date_from") or "")
            date_to = str(filters.get("date_to") or "")
            rows = [
                item
                for item in rows
                if (
                    (business_date := utc8_business_date(item.get("created_at"))) is not None
                    and (not date_from or business_date >= date_from)
                    and (not date_to or business_date <= date_to)
                )
            ]
        if filters.get("keyword"):
            keyword = str(filters["keyword"]).lower()
            rows = [
                item for item in rows
                if keyword in str(item.get("execution_id") or "").lower()
                or keyword in str(item.get("rule_group_id") or "").lower()
            ]
        rows.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("execution_id") or "")), reverse=True)
        total = len(rows)
        start = (page - 1) * page_size
        page_rows = rows[start : start + page_size]
        for item in page_rows:
            item.pop("targets", None)
        return {"items": page_rows, "page": page, "page_size": page_size, "total": total}

    def get_execution(self, execution_id: str, *, target_limit: int = 200) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self.executions.get(str(execution_id))
            if not item:
                return None
            result = copy.deepcopy(item)
            targets = list(result.get("targets") or [])
            limit = max(1, min(200, int(target_limit)))
            result["target_total"] = len(targets)
            result["targets"] = targets[:limit]
            result["targets_truncated"] = len(targets) > limit
            return result

    def list_enabled_rule_groups(self, *, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [
                copy.deepcopy(item)
                for item in self.groups.values()
                if item.get("enabled") and not item.get("deleted") and not item.get("emergency_stopped")
            ]
        return sorted(rows, key=lambda item: str(item.get("group_id") or ""))[: max(1, min(1000, int(limit)))]

    def claim_runner_event(self, record: Mapping[str, Any]) -> bool:
        event_key = str(record.get("event_key") or "")
        with self._lock:
            if not event_key or event_key in self.runner_events:
                return False
            self.runner_events[event_key] = copy.deepcopy(dict(record))
            return True

    def finish_runner_event(self, event_key: str, updates: Mapping[str, Any]) -> None:
        with self._lock:
            if event_key not in self.runner_events:
                raise AdControlV3Error("runner_event_not_found", "runner event not found", status=404)
            self.runner_events[event_key].update(copy.deepcopy(dict(updates)))


class MySQLRepository:
    """MySQL 5.7 DB-API repository with a fixed write-table allowlist."""

    def __init__(self, reader_factory: Any, writer_factory: Any) -> None:
        if not callable(reader_factory) or not callable(writer_factory):
            raise AdControlV3Error("repository_not_configured", "reader and writer factories are required", status=503)
        self._reader_factory = reader_factory
        self._writer_factory = writer_factory

    @contextmanager
    def _transaction(self):
        connection = self._writer_factory()
        autocommit = getattr(connection, "autocommit", None)
        if callable(autocommit):
            autocommit(False)
        try:
            yield connection
            connection.commit()
        except Exception:
            try:
                connection.rollback()
            finally:
                pass
            raise
        finally:
            connection.close()

    @staticmethod
    def _rows(cursor: Any) -> List[Dict[str, Any]]:
        rows = cursor.fetchall() or []
        if rows and isinstance(rows[0], Mapping):
            return [dict(row) for row in rows]
        names = [str(item[0]) for item in (cursor.description or [])]
        return [{name: row[index] for index, name in enumerate(names)} for row in rows]

    @staticmethod
    def _inflate_group(row: Mapping[str, Any], products: Sequence[str]) -> Dict[str, Any]:
        item = dict(row)
        item["products"] = list(products)
        for column, target, fallback in (
            ("account_timezones_json", "account_timezones", []),
            ("rules_json", "rules", []),
            ("schedule_json", "schedule", {}),
            ("quotas_json", "quotas", {}),
            ("selection_json", "selection", {}),
        ):
            item[target] = deserialize_json(item.pop(column, None), fallback)
        for flag in ("enabled", "emergency_stopped", "deleted"):
            item[flag] = bool(item.get(flag))
        return item

    def list_products(self, channel: str, *, include_disabled: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT channel,product_value,canonical_product,product_type,source_app_ids_json,evidence_json,enabled FROM %s WHERE channel=%%s" % qualified_table("product_catalog")
        params: List[Any] = [channel]
        if not include_disabled:
            sql += " AND enabled=1"
        sql += " ORDER BY product_value"
        connection = self._reader_factory()
        try:
            cursor = connection.cursor()
            cursor.execute(sql, tuple(params))
            rows = self._rows(cursor)
        finally:
            connection.close()
        for row in rows:
            row["source_app_ids"] = deserialize_json(row.pop("source_app_ids_json", None), [])
            row["evidence"] = deserialize_json(row.pop("evidence_json", None), {})
            row["enabled"] = bool(row.get("enabled"))
        return rows

    @staticmethod
    def _group_columns() -> str:
        return (
            "group_id,name,description,channel,object_level,run_mode,owner_user_id,optimizer_id,"
            "account_timezones_json,rules_json,schedule_json,quotas_json,selection_json,"
            "behavior_hash,config_version,last_preview_id,last_preview_hash,enabled,"
            "emergency_stopped,deleted,created_by_user_id,updated_by_user_id,created_at,updated_at"
        )

    def _product_values(self, connection: Any, group_ids: Sequence[str]) -> Dict[str, List[str]]:
        if not group_ids:
            return {}
        placeholders = ",".join(["%s"] * len(group_ids))
        cursor = connection.cursor()
        cursor.execute(
            "SELECT rule_group_id,product_value FROM %s WHERE rule_group_id IN (%s) ORDER BY product_value"
            % (qualified_table("rule_group_product"), placeholders),
            tuple(group_ids),
        )
        result: Dict[str, List[str]] = {}
        for row in self._rows(cursor):
            result.setdefault(str(row["rule_group_id"]), []).append(str(row["product_value"]))
        return result

    def _insert_products(self, connection: Any, group_id: str, products: Sequence[str]) -> None:
        cursor = connection.cursor()
        cursor.execute("DELETE FROM %s WHERE rule_group_id=%%s" % qualified_table("rule_group_product"), (group_id,))
        sql = "INSERT INTO %s (rule_group_id,product_value,created_at) VALUES (%%s,%%s,UTC_TIMESTAMP(6))" % qualified_table("rule_group_product")
        for product in products:
            cursor.execute(sql, (group_id, product))

    def create_rule_group(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        sql = """INSERT INTO {table} (
            group_id,name,description,channel,object_level,run_mode,owner_user_id,optimizer_id,
            account_timezones_json,rules_json,schedule_json,quotas_json,selection_json,
            behavior_hash,config_version,last_preview_id,last_preview_hash,enabled,
            emergency_stopped,deleted,created_by_user_id,updated_by_user_id,created_at,updated_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'','',0,0,0,%s,%s,%s,%s)""".format(table=qualified_table("rule_group"))
        params = (
            record["group_id"], record["name"], record.get("description", ""), record["channel"],
            record["object_level"], record["run_mode"], record["owner_user_id"], record["optimizer_id"],
            serialize_for_store(record.get("account_timezones") or []), serialize_for_store(record["rules"]),
            serialize_for_store(record.get("schedule") or {}), serialize_for_store(record.get("quotas") or {}),
            serialize_for_store(record.get("selection") or {}), record["behavior_hash"], record["config_version"],
            record["created_by_user_id"], record["updated_by_user_id"], record["created_at"], record["updated_at"],
        )
        with self._transaction() as connection:
            connection.cursor().execute(sql, params)
            self._insert_products(connection, record["group_id"], record["products"])
        return copy.deepcopy(dict(record))

    def get_rule_group(self, group_id: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        connection = self._reader_factory()
        try:
            cursor = connection.cursor()
            sql = "SELECT %s FROM %s WHERE group_id=%%s" % (self._group_columns(), qualified_table("rule_group"))
            if not include_deleted:
                sql += " AND deleted=0"
            cursor.execute(sql, (group_id,))
            rows = self._rows(cursor)
            if not rows:
                return None
            products = self._product_values(connection, [group_id]).get(group_id, [])
            return self._inflate_group(rows[0], products)
        finally:
            connection.close()

    def list_rule_groups(
        self,
        filters: Optional[Mapping[str, Any]] = None,
        *,
        page: int = 1,
        page_size: int = 20,
        optimizer_scope: Optional[int] = None,
    ) -> Dict[str, Any]:
        page, page_size = _page(page, page_size)
        filters = _validated_group_filters(filters)
        where = ["deleted=0"]
        params: List[Any] = []
        if optimizer_scope is not None:
            where.append("optimizer_id=%s")
            params.append(optimizer_scope)
        elif filters.get("optimizer_id") not in (None, ""):
            where.append("optimizer_id=%s")
            params.append(int(filters["optimizer_id"]))
        for key in ("channel", "object_level", "run_mode"):
            if filters.get(key):
                where.append("%s=%%s" % key)
                params.append(filters[key])
        if filters.get("enabled") not in (None, ""):
            where.append("enabled=%s")
            params.append(1 if _filter_bool(filters["enabled"]) else 0)
        keyword = filters.get("keyword", filters.get("query"))
        if keyword:
            where.append("(name LIKE %s ESCAPE '\\\\' OR group_id LIKE %s ESCAPE '\\\\')")
            keyword_value = _like_keyword(keyword)
            params.extend([keyword_value, keyword_value])
        selected_products = _filter_products(filters)
        if selected_products:
            placeholders = ",".join(["%s"] * len(selected_products))
            where.append(
                "EXISTS (SELECT 1 FROM %s gp WHERE gp.rule_group_id=g.group_id AND gp.product_value IN (%s))"
                % (qualified_table("rule_group_product"), placeholders)
            )
            params.extend([str(item) for item in selected_products])
        connection = self._reader_factory()
        try:
            cursor = connection.cursor()
            where_sql = " AND ".join(where)
            cursor.execute("SELECT COUNT(*) AS total FROM %s g WHERE %s" % (qualified_table("rule_group"), where_sql), tuple(params))
            total = int(self._rows(cursor)[0]["total"])
            sql = "SELECT %s FROM %s g WHERE %s ORDER BY updated_at DESC,group_id DESC LIMIT %%s OFFSET %%s" % (
                self._group_columns(), qualified_table("rule_group"), where_sql
            )
            cursor.execute(sql, tuple(params + [page_size, (page - 1) * page_size]))
            rows = self._rows(cursor)
            products = self._product_values(connection, [str(row["group_id"]) for row in rows])
            items = [self._inflate_group(row, products.get(str(row["group_id"]), [])) for row in rows]
            return {"items": items, "page": page, "page_size": page_size, "total": total}
        finally:
            connection.close()

    def update_rule_group(self, group_id: str, record: Mapping[str, Any], *, expected_version: int) -> Dict[str, Any]:
        sql = """UPDATE {table} SET name=%s,description=%s,channel=%s,object_level=%s,run_mode=%s,
            optimizer_id=%s,account_timezones_json=%s,rules_json=%s,schedule_json=%s,quotas_json=%s,
            selection_json=%s,behavior_hash=%s,config_version=%s,last_preview_id='',last_preview_hash='',
            enabled=0,updated_by_user_id=%s,updated_at=%s
            WHERE group_id=%s AND deleted=0 AND config_version=%s""".format(table=qualified_table("rule_group"))
        params = (
            record["name"], record.get("description", ""), record["channel"], record["object_level"], record["run_mode"],
            record["optimizer_id"], serialize_for_store(record.get("account_timezones") or []), serialize_for_store(record["rules"]),
            serialize_for_store(record.get("schedule") or {}), serialize_for_store(record.get("quotas") or {}),
            serialize_for_store(record.get("selection") or {}), record["behavior_hash"], record["config_version"],
            record["updated_by_user_id"], record["updated_at"], group_id, expected_version,
        )
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, params)
            if int(cursor.rowcount or 0) != 1:
                raise AdControlV3Error("version_conflict", "rule group was changed or deleted", status=409)
            self._insert_products(connection, group_id, record["products"])
        return copy.deepcopy(dict(record))

    def soft_delete_rule_group(self, group_id: str, *, updated_by: str, updated_at: str) -> bool:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "UPDATE %s SET deleted=1,enabled=0,updated_by_user_id=%%s,updated_at=%%s WHERE group_id=%%s AND deleted=0"
                % qualified_table("rule_group"),
                (updated_by, updated_at, group_id),
            )
            return int(cursor.rowcount or 0) == 1

    def set_group_state(
        self,
        group_id: str,
        *,
        enabled: Optional[bool] = None,
        emergency_stopped: Optional[bool] = None,
        updated_by: str,
        updated_at: str,
        expected_version: Optional[int] = None,
        expected_behavior_hash: str = "",
        expected_preview_id: str = "",
        require_fresh_preview: bool = False,
        clear_preview: bool = False,
    ) -> Dict[str, Any]:
        current = self.get_rule_group(group_id)
        if not current:
            raise AdControlV3Error("rule_group_not_found", "rule group not found", status=404)
        assignments = ["updated_by_user_id=%s", "updated_at=%s"]
        params: List[Any] = [updated_by, updated_at]
        if enabled is not None:
            assignments.append("enabled=%s")
            params.append(1 if enabled else 0)
        if emergency_stopped is not None:
            assignments.append("emergency_stopped=%s")
            params.append(1 if emergency_stopped else 0)
        if clear_preview:
            assignments.extend(["last_preview_id=''", "last_preview_hash=''"])
        where = ["group_id=%s", "deleted=0"]
        params.append(group_id)
        if expected_version is not None:
            where.append("config_version=%s")
            params.append(expected_version)
        if expected_behavior_hash:
            where.append("behavior_hash=%s")
            params.append(expected_behavior_hash)
        if expected_preview_id:
            where.append("last_preview_id=%s")
            params.append(expected_preview_id)
        if require_fresh_preview:
            where.append(
                "EXISTS (SELECT 1 FROM %s p WHERE p.preview_id=%%s AND p.rule_group_id=%s.group_id "
                "AND p.config_version=%s.config_version AND p.behavior_hash=%s.behavior_hash "
                "AND p.status='ready' AND p.expires_at>UTC_TIMESTAMP(6))"
                % (
                    qualified_table("preview"),
                    TABLES["rule_group"],
                    TABLES["rule_group"],
                    TABLES["rule_group"],
                )
            )
            params.append(expected_preview_id)
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "UPDATE %s SET %s WHERE %s" % (qualified_table("rule_group"), ",".join(assignments), " AND ".join(where)),
                tuple(params),
            )
            if int(cursor.rowcount or 0) != 1:
                if require_fresh_preview:
                    raise AdControlV3Error("stale_preview", "rule group or preview changed during enable", status=409)
                raise AdControlV3Error("rule_group_not_found", "rule group not found", status=404)
        current["updated_by_user_id"] = updated_by
        current["updated_at"] = updated_at
        if enabled is not None:
            current["enabled"] = bool(enabled)
        if emergency_stopped is not None:
            current["emergency_stopped"] = bool(emergency_stopped)
        if clear_preview:
            current["last_preview_id"] = ""
            current["last_preview_hash"] = ""
        return current

    def emergency_stop_all(self, *, optimizer_scope: Optional[int], updated_by: str, updated_at: str) -> int:
        sql = "UPDATE %s SET enabled=0,emergency_stopped=1,last_preview_id='',last_preview_hash='',updated_by_user_id=%%s,updated_at=%%s WHERE deleted=0" % qualified_table("rule_group")
        params: List[Any] = [updated_by, updated_at]
        if optimizer_scope is not None:
            sql += " AND optimizer_id=%s"
            params.append(optimizer_scope)
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, tuple(params))
            return int(cursor.rowcount or 0)

    # Preview/execution inserts use immutable rows; update/delete is purposely
    # absent from the public repository contract.
    def save_preview(self, record: Mapping[str, Any], targets: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        _validate_target_count(targets)
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO %s (preview_id,rule_group_id,config_version,behavior_hash,optimizer_id,channel,object_level,status,summary_json,snapshot_relative_path,snapshot_sha256,snapshot_byte_size,created_by_user_id,created_at,expires_at) VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)"
                % qualified_table("preview"),
                (
                    record["preview_id"], record["rule_group_id"], record["config_version"], record["behavior_hash"],
                    record["optimizer_id"], record["channel"], record["object_level"], record["status"],
                    serialize_for_store(record["summary"]), record["snapshot_relative_path"], record["snapshot_sha256"],
                    record["snapshot_byte_size"], record["created_by_user_id"], record["created_at"], record["expires_at"],
                ),
            )
            target_sql = "INSERT INTO %s (preview_id,target_no,ad_account_id,object_level,object_id,campaign_id,adset_id,ad_id,product_value,optimizer_id,action,control_rule_id,status,reason,detail_json,created_at) VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)" % qualified_table("preview_target")
            target_rows = [(
                    record["preview_id"], index, target.get("ad_account_id", ""), target.get("object_level", ""), target.get("object_id", ""),
                    target.get("campaign_id", ""), target.get("adset_id", ""), target.get("ad_id", ""), target.get("product", ""),
                    target.get("optimizer_id"), target.get("action", ""), target.get("control_rule_id", ""), target.get("status", ""),
                    target.get("reason", ""), serialize_for_store(target), record["created_at"],
                ) for index, target in enumerate(targets, 1)]
            _executemany_chunks(cursor, target_sql, target_rows)
            cursor.execute(
                "UPDATE %s SET last_preview_id=%%s,last_preview_hash=%%s WHERE group_id=%%s AND config_version=%%s AND behavior_hash=%%s AND deleted=0"
                % qualified_table("rule_group"),
                (record["preview_id"], record["behavior_hash"], record["rule_group_id"], record["config_version"], record["behavior_hash"]),
            )
            if int(cursor.rowcount or 0) != 1:
                raise AdControlV3Error("stale_preview", "rule group changed during preview", status=409)
        return dict(record, targets=[dict(item) for item in targets])

    def get_preview(self, preview_id: str, *, include_targets: bool = True) -> Optional[Dict[str, Any]]:
        connection = self._reader_factory()
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT * FROM %s WHERE preview_id=%%s" % qualified_table("preview"), (preview_id,))
            rows = self._rows(cursor)
            if not rows:
                return None
            item = dict(rows[0])
            item["summary"] = deserialize_json(item.pop("summary_json", None), {})
            if include_targets:
                cursor.execute("SELECT detail_json FROM %s WHERE preview_id=%%s ORDER BY target_no LIMIT 200" % qualified_table("preview_target"), (preview_id,))
                item["targets"] = [deserialize_json(row.get("detail_json"), {}) for row in self._rows(cursor)]
            return item
        finally:
            connection.close()

    def save_execution(self, record: Mapping[str, Any], targets: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        _validate_target_count(targets)
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO %s (execution_id,rule_group_id,preview_id,config_version,behavior_hash,optimizer_id,channel,object_level,run_mode,trigger_source,status,summary_json,snapshot_relative_path,snapshot_sha256,snapshot_byte_size,created_by_user_id,created_at,finished_at) VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)"
                % qualified_table("execution"),
                (
                    record["execution_id"], record["rule_group_id"], record.get("preview_id", ""), record["config_version"],
                    record["behavior_hash"], record["optimizer_id"], record["channel"], record["object_level"], record["run_mode"],
                    record["trigger_source"], record["status"], serialize_for_store(record["summary"]), record["snapshot_relative_path"],
                    record["snapshot_sha256"], record["snapshot_byte_size"], record["created_by_user_id"], record["created_at"], record.get("finished_at"),
                ),
            )
            target_sql = "INSERT INTO %s (execution_id,target_no,ad_account_id,object_level,object_id,campaign_id,adset_id,ad_id,product_value,optimizer_id,action,control_rule_id,status,reason,detail_json,created_at) VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)" % qualified_table("execution_target")
            target_rows = [(
                    record["execution_id"], index, target.get("ad_account_id", ""), target.get("object_level", ""), target.get("object_id", ""),
                    target.get("campaign_id", ""), target.get("adset_id", ""), target.get("ad_id", ""), target.get("product", ""),
                    target.get("optimizer_id"), target.get("action", ""), target.get("control_rule_id", ""), target.get("status", ""),
                    target.get("reason", ""), serialize_for_store(target), record["created_at"],
                ) for index, target in enumerate(targets, 1)]
            _executemany_chunks(cursor, target_sql, target_rows)
        return dict(record, targets=[dict(item) for item in targets])

    def save_preview_execution_bundle(
        self,
        preview_record: Mapping[str, Any],
        execution_record: Mapping[str, Any],
        targets: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Atomically persist the immutable preview, execution and CAS pointer."""
        _validate_target_count(targets)
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO %s (preview_id,rule_group_id,config_version,behavior_hash,optimizer_id,channel,object_level,status,summary_json,snapshot_relative_path,snapshot_sha256,snapshot_byte_size,created_by_user_id,created_at,expires_at) VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)"
                % qualified_table("preview"),
                (
                    preview_record["preview_id"], preview_record["rule_group_id"], preview_record["config_version"],
                    preview_record["behavior_hash"], preview_record["optimizer_id"], preview_record["channel"],
                    preview_record["object_level"], preview_record["status"], serialize_for_store(preview_record["summary"]),
                    preview_record["snapshot_relative_path"], preview_record["snapshot_sha256"], preview_record["snapshot_byte_size"],
                    preview_record["created_by_user_id"], preview_record["created_at"], preview_record["expires_at"],
                ),
            )
            preview_target_sql = "INSERT INTO %s (preview_id,target_no,ad_account_id,object_level,object_id,campaign_id,adset_id,ad_id,product_value,optimizer_id,action,control_rule_id,status,reason,detail_json,created_at) VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)" % qualified_table("preview_target")
            preview_target_rows = [(
                    preview_record["preview_id"], index, target.get("ad_account_id", ""), target.get("object_level", ""),
                    target.get("object_id", ""), target.get("campaign_id", ""), target.get("adset_id", ""), target.get("ad_id", ""),
                    target.get("product", ""), target.get("optimizer_id"), target.get("action", ""), target.get("control_rule_id", ""),
                    target.get("status", ""), target.get("reason", ""), serialize_for_store(target), preview_record["created_at"],
                ) for index, target in enumerate(targets, 1)]
            if preview_target_rows:
                _executemany_chunks(cursor, preview_target_sql, preview_target_rows)
            cursor.execute(
                "INSERT INTO %s (execution_id,rule_group_id,preview_id,config_version,behavior_hash,optimizer_id,channel,object_level,run_mode,trigger_source,status,summary_json,snapshot_relative_path,snapshot_sha256,snapshot_byte_size,created_by_user_id,created_at,finished_at) VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)"
                % qualified_table("execution"),
                (
                    execution_record["execution_id"], execution_record["rule_group_id"], execution_record.get("preview_id", ""),
                    execution_record["config_version"], execution_record["behavior_hash"], execution_record["optimizer_id"],
                    execution_record["channel"], execution_record["object_level"], execution_record["run_mode"],
                    execution_record["trigger_source"], execution_record["status"], serialize_for_store(execution_record["summary"]),
                    execution_record["snapshot_relative_path"], execution_record["snapshot_sha256"], execution_record["snapshot_byte_size"],
                    execution_record["created_by_user_id"], execution_record["created_at"], execution_record.get("finished_at"),
                ),
            )
            execution_target_sql = "INSERT INTO %s (execution_id,target_no,ad_account_id,object_level,object_id,campaign_id,adset_id,ad_id,product_value,optimizer_id,action,control_rule_id,status,reason,detail_json,created_at) VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)" % qualified_table("execution_target")
            execution_target_rows = [(
                    execution_record["execution_id"], index, target.get("ad_account_id", ""), target.get("object_level", ""),
                    target.get("object_id", ""), target.get("campaign_id", ""), target.get("adset_id", ""), target.get("ad_id", ""),
                    target.get("product", ""), target.get("optimizer_id"), target.get("action", ""), target.get("control_rule_id", ""),
                    target.get("status", ""), target.get("reason", ""), serialize_for_store(target), execution_record["created_at"],
                ) for index, target in enumerate(targets, 1)]
            if execution_target_rows:
                _executemany_chunks(cursor, execution_target_sql, execution_target_rows)
            cursor.execute(
                "UPDATE %s SET last_preview_id=%%s,last_preview_hash=%%s WHERE group_id=%%s AND config_version=%%s AND behavior_hash=%%s AND deleted=0"
                % qualified_table("rule_group"),
                (
                    preview_record["preview_id"], preview_record["behavior_hash"], preview_record["rule_group_id"],
                    preview_record["config_version"], preview_record["behavior_hash"],
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                raise AdControlV3Error("stale_preview", "rule group changed during preview", status=409)
        return {
            "preview": dict(preview_record, targets=[dict(item) for item in targets]),
            "execution": dict(execution_record, targets=[dict(item) for item in targets]),
        }

    def list_executions(
        self,
        filters: Optional[Mapping[str, Any]] = None,
        *,
        page: int = 1,
        page_size: int = 20,
        optimizer_scope: Optional[int] = None,
    ) -> Dict[str, Any]:
        page, page_size = _page(page, page_size)
        filters = _validated_execution_filters(filters)
        where = ["1=1"]
        params: List[Any] = []
        if optimizer_scope is not None:
            where.append("optimizer_id=%s")
            params.append(optimizer_scope)
        elif filters.get("optimizer_id") not in (None, ""):
            where.append("optimizer_id=%s")
            params.append(int(filters["optimizer_id"]))
        for key in ("rule_group_id", "channel", "object_level", "run_mode", "status", "trigger_source"):
            if filters.get(key):
                where.append("%s=%%s" % key)
                params.append(filters[key])
        start_utc, end_utc = utc8_date_bounds(filters.get("date_from"), filters.get("date_to"))
        if start_utc:
            where.append("e.created_at>=%s")
            params.append(start_utc)
        if end_utc:
            where.append("e.created_at<%s")
            params.append(end_utc)
        if filters.get("action"):
            where.append(
                "EXISTS (SELECT 1 FROM %s ea WHERE ea.execution_id=e.execution_id AND ea.action=%%s)"
                % qualified_table("execution_target")
            )
            params.append(filters["action"])
        if filters.get("object_id"):
            where.append(
                "EXISTS (SELECT 1 FROM %s eo WHERE eo.execution_id=e.execution_id AND eo.object_id=%%s)"
                % qualified_table("execution_target")
            )
            params.append(str(filters["object_id"]))
        if filters.get("keyword"):
            where.append("(e.execution_id LIKE %s ESCAPE '\\\\' OR e.rule_group_id LIKE %s ESCAPE '\\\\')")
            keyword = _like_keyword(filters["keyword"])
            params.extend([keyword, keyword])
        selected_products = _filter_products(filters)
        if selected_products:
            placeholders = ",".join(["%s"] * len(selected_products))
            where.append(
                "EXISTS (SELECT 1 FROM %s et WHERE et.execution_id=e.execution_id AND et.product_value IN (%s))"
                % (qualified_table("execution_target"), placeholders)
            )
            params.extend([str(item) for item in selected_products])
        connection = self._reader_factory()
        try:
            cursor = connection.cursor()
            where_sql = " AND ".join(where)
            cursor.execute("SELECT COUNT(*) AS total FROM %s e WHERE %s" % (qualified_table("execution"), where_sql), tuple(params))
            total = int(self._rows(cursor)[0]["total"])
            cursor.execute(
                "SELECT e.* FROM %s e WHERE %s ORDER BY created_at DESC,execution_id DESC LIMIT %%s OFFSET %%s"
                % (qualified_table("execution"), where_sql),
                tuple(params + [page_size, (page - 1) * page_size]),
            )
            rows = self._rows(cursor)
            for row in rows:
                row["summary"] = deserialize_json(row.pop("summary_json", None), {})
            return {"items": rows, "page": page, "page_size": page_size, "total": total}
        finally:
            connection.close()

    def get_execution(self, execution_id: str, *, target_limit: int = 200) -> Optional[Dict[str, Any]]:
        connection = self._reader_factory()
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT * FROM %s WHERE execution_id=%%s" % qualified_table("execution"), (execution_id,))
            rows = self._rows(cursor)
            if not rows:
                return None
            item = rows[0]
            item["summary"] = deserialize_json(item.pop("summary_json", None), {})
            cursor.execute("SELECT COUNT(*) AS total FROM %s WHERE execution_id=%%s" % qualified_table("execution_target"), (execution_id,))
            item["target_total"] = int(self._rows(cursor)[0]["total"])
            limit = max(1, min(200, int(target_limit)))
            cursor.execute("SELECT detail_json FROM %s WHERE execution_id=%%s ORDER BY target_no LIMIT %%s" % qualified_table("execution_target"), (execution_id, limit))
            item["targets"] = [deserialize_json(row.get("detail_json"), {}) for row in self._rows(cursor)]
            item["targets_truncated"] = item["target_total"] > len(item["targets"])
            return item
        finally:
            connection.close()

    def list_enabled_rule_groups(self, *, limit: int = 200) -> List[Dict[str, Any]]:
        return self.list_rule_groups({"enabled": True}, page=1, page_size=min(200, limit))["items"]

    def claim_runner_event(self, record: Mapping[str, Any]) -> bool:
        try:
            with self._transaction() as connection:
                connection.cursor().execute(
                    "INSERT INTO %s (event_key,rule_group_id,scheduled_for,status,lease_owner,lease_expires_at,attempt_count,created_at,updated_at) VALUES (%%s,%%s,%%s,'running',%%s,%%s,1,%%s,%%s)"
                    % qualified_table("runner_event"),
                    (
                        record["event_key"], record["rule_group_id"], record["scheduled_for"], record["lease_owner"],
                        record["lease_expires_at"], record["created_at"], record["updated_at"],
                    ),
                )
            return True
        except Exception as exc:
            # Duplicate event key is an expected idempotency outcome. Avoid
            # importing a driver-specific exception class into this module.
            if "duplicate" in str(exc).lower() or "1062" in str(exc):
                return False
            raise

    def finish_runner_event(self, event_key: str, updates: Mapping[str, Any]) -> None:
        allowed = {"status", "execution_id", "error_code", "error_message", "updated_at"}
        unknown = set(updates) - allowed
        if unknown:
            raise AdControlV3Error("unsafe_repository_field", "runner update contains unsupported fields")
        assignments = []
        params: List[Any] = []
        for key in sorted(updates):
            assignments.append("%s=%%s" % key)
            params.append(updates[key])
        params.append(event_key)
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "UPDATE %s SET %s WHERE event_key=%%s" % (qualified_table("runner_event"), ",".join(assignments)),
                tuple(params),
            )
            if int(cursor.rowcount or 0) != 1:
                raise AdControlV3Error("runner_event_not_found", "runner event not found", status=404)
