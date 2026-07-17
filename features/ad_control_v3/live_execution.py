"""Production Facebook mutation boundary for ad-control V3.

The module is intentionally independent from the legacy monolith helpers.  It
owns token resolution, Graph write/readback verification, copy idempotency and
the ``ads_ai`` copy ledger.  Preview/observe paths never instantiate a request
from this module.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 production fallback
    ZoneInfo = None

from .errors import AdControlV3Error


GRAPH_VERSION = "v25.0"
GRAPH_ROOT = "https://graph.facebook.com"
CREATED_DATA_SOURCE_DATABASE = "kunlunads_dev"
CREATED_DATA_STORE_DATABASE = "ads_ai"
CREATED_DATA_TABLE = "ads_facebook_auto_created_data"
INTENT_TABLE = "ad_control_v3_copy_intent"
LINEAGE_TABLE = "ad_control_copy_lineage"
LIVE_CONFIRMATION = "EXECUTE_LIVE_RULE_GROUP"
ENABLE_CONFIRMATION = "ENABLE_LIVE_MODE"
MAX_LIVE_TARGETS = 50
MAX_COPY_TARGETS = 10
MAX_ADSETS_PER_COPY = 100
MAX_ADS_PER_COPY = 500
HARD_DAILY_COPY_LIMIT = 50
ROAS_BID_STRATEGIES = {
    "LOWEST_COST_WITH_MIN_ROAS",
    "MINIMUM_ROAS",
    "VALUE_MIN_ROAS",
    "ROAS",
}
ZERO_DECIMAL_CURRENCIES = {
    "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG",
    "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
}
ACTION_AT_COLUMNS = {
    "campaign": "campaign_action_at",
    "adset": "adset_action_at",
    "ad": "ad_action_at",
}
OBJECT_ID_COLUMNS = {
    "campaign": "campaign_id",
    "adset": "adset_id",
    "ad": "ad_id",
}


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _account_id(value: Any) -> str:
    return re.sub(r"^act_", "", str(value or "").strip(), flags=re.I)


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _positive_int(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(maximum, parsed))


def _utc_text(value: Optional[datetime] = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _rows(cursor: Any) -> List[Dict[str, Any]]:
    raw = cursor.fetchall() or []
    if not raw:
        return []
    if isinstance(raw[0], Mapping):
        return [dict(row) for row in raw]
    names = [str(item[0]) for item in (cursor.description or [])]
    return [dict(zip(names, row)) for row in raw]


def _one(cursor: Any) -> Dict[str, Any]:
    rows = _rows(cursor)
    return rows[0] if rows else {}


def _configured_status(value: Mapping[str, Any]) -> str:
    return str(value.get("configured_status") or value.get("status") or "").upper()


class MetaGraphClient:
    """Small no-retry Graph client.

    A timed-out POST is deliberately never retried because Meta may have
    committed it even when the response was lost.  The surrounding intent is
    quarantined for reconciliation instead.
    """

    def __init__(self, token: str, *, session: Any = None, timeout_seconds: int = 20) -> None:
        token = str(token or "").strip()
        if not token:
            raise AdControlV3Error("missing_meta_token", "Meta token is unavailable", status=409)
        if session is None:
            try:
                import requests
            except ImportError as exc:  # pragma: no cover
                raise AdControlV3Error("service_not_configured", "requests is unavailable", status=503) from exc
            session = requests.Session()
        self.token = token
        self.session = session
        self.timeout_seconds = max(5, min(60, int(timeout_seconds)))
        self.write_count = 0

    @staticmethod
    def _safe_error(payload: Mapping[str, Any], http_status: int) -> AdControlV3Error:
        error = payload.get("error") if isinstance(payload, Mapping) else {}
        error = error if isinstance(error, Mapping) else {}
        return AdControlV3Error(
            "meta_api_error",
            str(error.get("error_user_msg") or error.get("message") or "Meta request failed")[:500],
            status=502,
            details={
                "http_status": int(http_status),
                "code": error.get("code"),
                "subcode": error.get("error_subcode"),
                "transient": bool(error.get("is_transient")),
                "trace_id": str(error.get("fbtrace_id") or "")[:128],
            },
        )

    def call(self, method: str, path_or_url: str, parameters: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        method = str(method or "GET").upper()
        url = str(path_or_url or "")
        if not url.startswith("https://"):
            url = "%s/%s/%s" % (GRAPH_ROOT, GRAPH_VERSION, url.lstrip("/"))
        headers = {"Authorization": "Bearer %s" % self.token}
        values: Dict[str, Any] = {}
        for key, value in dict(parameters or {}).items():
            if isinstance(value, (Mapping, list, tuple, bool)):
                values[str(key)] = _json(value) if not isinstance(value, bool) else ("true" if value else "false")
            elif value is not None:
                values[str(key)] = value
        try:
            if method == "GET":
                response = self.session.get(url, params=values, headers=headers, timeout=self.timeout_seconds)
            else:
                self.write_count += 1
                response = self.session.post(url, data=values, headers=headers, timeout=self.timeout_seconds)
            payload = response.json() if getattr(response, "content", b"") else {}
        except AdControlV3Error:
            raise
        except Exception as exc:
            logging.warning("ad-control V3 Meta transport failed method=%s", method, exc_info=True)
            raise AdControlV3Error(
                "meta_transport_uncertain" if method != "GET" else "meta_transport_unavailable",
                "Meta request did not return a reliable response",
                status=503,
                details={"write_may_have_committed": method != "GET"},
            ) from exc
        if int(getattr(response, "status_code", 500)) >= 400 or not isinstance(payload, Mapping) or payload.get("error"):
            raise self._safe_error(payload if isinstance(payload, Mapping) else {}, int(getattr(response, "status_code", 500)))
        return dict(payload)

    def get(self, object_id: str, fields: str) -> Dict[str, Any]:
        return self.call("GET", str(object_id), {"fields": fields})

    def post(self, object_id: str, values: Mapping[str, Any]) -> Dict[str, Any]:
        return self.call("POST", str(object_id), values)

    def edge(self, object_id: str, edge: str, fields: str, *, limit: int) -> List[Dict[str, Any]]:
        output: List[Dict[str, Any]] = []
        url = "%s/%s/%s/%s" % (GRAPH_ROOT, GRAPH_VERSION, str(object_id), str(edge))
        parameters: Dict[str, Any] = {"fields": fields, "limit": min(500, max(1, limit))}
        seen_cursors = set()
        while True:
            payload = self.call("GET", url, parameters)
            data = payload.get("data") or []
            if not isinstance(data, list):
                raise AdControlV3Error("meta_response_invalid", "Meta paging response is invalid", status=502)
            output.extend(dict(item) for item in data if isinstance(item, Mapping))
            if len(output) > limit:
                raise AdControlV3Error(
                    "copy_tree_too_large",
                    "source object exceeds the reviewed copy size",
                    status=409,
                    details={"limit": limit},
                )
            paging = payload.get("paging") if isinstance(payload.get("paging"), Mapping) else {}
            cursors = paging.get("cursors") if isinstance(paging.get("cursors"), Mapping) else {}
            after = str(cursors.get("after") or "")
            if not paging.get("next") or not after:
                break
            if after in seen_cursors:
                raise AdControlV3Error("meta_response_invalid", "Meta paging cursor repeated", status=502)
            seen_cursors.add(after)
            parameters = {"fields": fields, "limit": min(500, max(1, limit)), "after": after}
        return output


class FacebookLiveExecutor:
    """Execute one already-evaluated Facebook target with fail-closed gates."""

    def __init__(
        self,
        source_reader: Callable[[], Any],
        store_reader: Callable[[], Any],
        store_writer: Callable[[], Any],
        *,
        session_factory: Optional[Callable[[], Any]] = None,
        pause_enabled: bool = False,
        copy_enabled: bool = False,
        persistence_enabled: bool = False,
        activation_enabled: bool = False,
        graph_timeout_seconds: int = 20,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.source_reader = source_reader
        self.store_reader = store_reader
        self.store_writer = store_writer
        self.session_factory = session_factory
        self.pause_enabled = bool(pause_enabled)
        self.copy_enabled = bool(copy_enabled)
        self.persistence_enabled = bool(persistence_enabled)
        self.activation_enabled = bool(activation_enabled)
        self.graph_timeout_seconds = max(5, min(60, int(graph_timeout_seconds)))
        self.clock = clock

    def capabilities(self) -> Dict[str, bool]:
        return {
            "live_pause_enabled": self.pause_enabled,
            "live_copy_enabled": self.copy_enabled and self.persistence_enabled,
            "copy_persistence_enabled": self.persistence_enabled,
            "copy_activation_enabled": self.activation_enabled,
        }

    def _client(self, token: str) -> MetaGraphClient:
        session = self.session_factory() if self.session_factory else None
        return MetaGraphClient(token, session=session, timeout_seconds=self.graph_timeout_seconds)

    def _query(self, factory: Callable[[], Any], sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        connection = factory()
        cursor = None
        try:
            cursor = connection.cursor()
            cursor.execute(sql, tuple(params))
            return _rows(cursor)
        finally:
            if cursor is not None and callable(getattr(cursor, "close", None)):
                cursor.close()
            connection.close()

    def _source_rows(self, target: Mapping[str, Any]) -> List[Dict[str, Any]]:
        level = str(target.get("object_level") or "")
        column = OBJECT_ID_COLUMNS.get(level)
        object_id = str(target.get("object_id") or "").strip()
        account = _account_id(target.get("ad_account_id"))
        if not column or not object_id or not account:
            raise AdControlV3Error("copy_source_identity_missing", "source object identity is incomplete", status=409)
        sql = (
            "SELECT * FROM `{database}`.`{table}` WHERE `{column}`=%s "
            "AND (CAST(`ad_account_id` AS CHAR)=%s OR CAST(`ad_account_id` AS CHAR)=%s) "
            "ORDER BY `id` LIMIT 502"
        )
        # custom_source_insight.product is a reporting enum (for example
        # ``Dramawave``), while created_data.product is legacy publishing
        # metadata and may be an apps_setting id.  The exact Meta object id +
        # account pair is the stable join; requiring equal product strings
        # would incorrectly reject legitimate W2A rows.
        params = (object_id, account, "act_" + account)
        output: List[Dict[str, Any]] = []
        for database, factory in (
            (CREATED_DATA_SOURCE_DATABASE, self.source_reader),
            (CREATED_DATA_STORE_DATABASE, self.store_reader),
        ):
            try:
                rows = self._query(
                    factory,
                    sql.format(database=database, table=CREATED_DATA_TABLE, column=column),
                    params,
                )
            except Exception as exc:
                if database == CREATED_DATA_STORE_DATABASE and "doesn't exist" in str(exc).lower():
                    rows = []
                else:
                    raise
            if len(rows) > 500:
                raise AdControlV3Error("copy_source_too_large", "created_data source row count exceeds limit", status=409)
            for row in rows:
                item = dict(row)
                item["__source_database"] = database
                item["__source_table"] = CREATED_DATA_TABLE
                output.append(item)
        if not output:
            raise AdControlV3Error(
                "missing_source_created_data",
                "no matching Facebook created_data row was found",
                status=409,
            )
        by_ad: Dict[str, List[Dict[str, Any]]] = {}
        for row in output:
            by_ad.setdefault(str(row.get("ad_id") or ""), []).append(row)
        duplicates = sorted(ad_id for ad_id, rows in by_ad.items() if ad_id and len(rows) != 1)
        if duplicates:
            raise AdControlV3Error(
                "ambiguous_source_created_data",
                "source ad maps to more than one created_data row",
                status=409,
                details={"ad_ids": duplicates[:20]},
            )
        return output

    def _token(self, product: str, source_rows: Sequence[Mapping[str, Any]]) -> str:
        app_ids = sorted({str(row.get("app_id") or "").strip() for row in source_rows if str(row.get("app_id") or "").strip()})
        values = [str(product or "").strip()] + app_ids
        values = [value for value in values if value]
        if not values:
            raise AdControlV3Error("missing_product_identity", "product identity is unavailable", status=409)
        clauses: List[str] = []
        params: List[Any] = []
        for value in values[:10]:
            clauses.append("(CAST(`id` AS CHAR)=%s OR CAST(`name` AS CHAR)=%s OR CAST(`app_id` AS CHAR)=%s)")
            params.extend([value, value, value])
        rows = self._query(
            self.source_reader,
            "SELECT CAST(`id` AS CHAR) AS app_row_id,CAST(`default_user` AS CHAR) AS default_user "
            "FROM `kunlunads_dev`.`ads_apps_setting` WHERE COALESCE(`default_user`,0)>0 AND (%s) "
            "ORDER BY `id` LIMIT 20" % " OR ".join(clauses),
            params,
        )
        owners = sorted({str(row.get("default_user") or "").strip() for row in rows if str(row.get("default_user") or "").strip() not in {"", "0"}})
        if len(owners) != 1:
            raise AdControlV3Error(
                "missing_apps_setting_default_user" if not owners else "ambiguous_apps_setting_default_user",
                "product must resolve to exactly one Meta token owner",
                status=409,
                details={"owner_count": len(owners)},
            )
        token_rows = self._query(
            self.source_reader,
            "SELECT `accessToken` AS access_token FROM `kunlunads_dev`.`ads_facebook_info` "
            "WHERE CAST(`user_id` AS CHAR)=%s AND `accessToken` IS NOT NULL AND `accessToken`<>'' LIMIT 1",
            (owners[0],),
        )
        token = str((token_rows[0] if token_rows else {}).get("access_token") or "").strip()
        if not token:
            raise AdControlV3Error("missing_meta_token", "token owner has no usable Meta token", status=409)
        return token

    @staticmethod
    def _verify_account(meta: Mapping[str, Any], expected: str) -> None:
        actual = _account_id(meta.get("account_id"))
        if not actual or actual != _account_id(expected):
            raise AdControlV3Error(
                "meta_object_account_mismatch",
                "Meta object is outside the evaluated ad account",
                status=409,
            )

    def execute(self, group: Mapping[str, Any], target: Mapping[str, Any]) -> Dict[str, Any]:
        result = copy.deepcopy(dict(target))
        action = str(result.get("action") or "")
        try:
            if action == "pause":
                details = self._pause(group, result)
            elif action == "copy":
                details = self._copy(group, result)
            else:
                raise AdControlV3Error("unsupported_live_action", "target action is not executable", status=409)
            result.update(details)
            return result
        except AdControlV3Error as exc:
            result.update(
                {
                    "status": "failed",
                    "reason": exc.code,
                    "error": exc.to_dict().get("error", {}),
                    "meta_write_count": int(exc.details.get("meta_write_count") or result.get("meta_write_count") or 0),
                }
            )
            return result
        except Exception:
            logging.exception("ad-control V3 live target failed")
            result.update(
                {
                    "status": "failed",
                    "reason": "live_execution_failed",
                    "error": {"code": "live_execution_failed", "message": "live target failed"},
                    "meta_write_count": int(result.get("meta_write_count") or 0),
                }
            )
            return result

    def _pause(self, group: Mapping[str, Any], target: Mapping[str, Any]) -> Dict[str, Any]:
        if not self.pause_enabled:
            raise AdControlV3Error("live_pause_disabled", "Facebook live pause is disabled", status=409)
        source_rows = self._source_rows(target)
        token = self._token(str(target.get("product") or ""), source_rows)
        client = self._client(token)
        level = str(target.get("object_level") or "")
        fields = {
            "campaign": "id,name,account_id,status,configured_status,effective_status",
            "adset": "id,name,account_id,campaign_id,status,configured_status,effective_status",
            "ad": "id,name,account_id,campaign_id,adset_id,status,configured_status,effective_status",
        }.get(level)
        if not fields:
            raise AdControlV3Error("unsupported_object_level", "unsupported Facebook object level")
        object_id = str(target.get("object_id") or "")
        before = client.get(object_id, fields)
        self._verify_account(before, str(target.get("ad_account_id") or ""))
        if level in {"adset", "ad"} and str(before.get("campaign_id") or "") != str(target.get("campaign_id") or ""):
            raise AdControlV3Error("meta_parent_mismatch", "Meta Campaign parent changed", status=409)
        if level == "ad" and str(before.get("adset_id") or "") != str(target.get("adset_id") or ""):
            raise AdControlV3Error("meta_parent_mismatch", "Meta Ad Set parent changed", status=409)
        already_paused = _configured_status(before) == "PAUSED"
        if not already_paused:
            client.post(object_id, {"status": "PAUSED"})
        after = client.get(object_id, fields)
        if _configured_status(after) != "PAUSED":
            raise AdControlV3Error("meta_pause_readback_failed", "Meta object was not confirmed PAUSED", status=502)
        self._update_store_copy_status(level, object_id, "PAUSED")
        return {
            "status": "skipped" if already_paused else "succeeded",
            "reason": "already_paused" if already_paused else "",
            "meta_write_count": client.write_count,
            "meta_result": {
                "object_id": object_id,
                "configured_status": _configured_status(after),
                "effective_status": str(after.get("effective_status") or ""),
            },
        }

    def _update_store_copy_status(self, level: str, object_id: str, status: str) -> None:
        column = OBJECT_ID_COLUMNS[level]
        action_column = ACTION_AT_COLUMNS[level]
        connection = self.store_writer()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "UPDATE `ads_ai`.`ads_facebook_auto_created_data` SET `status`=%s,`%s`=UNIX_TIMESTAMP(),`updated_at`=NOW() WHERE `%s`=%%s"
                % ("%s", action_column, column),
                (status, object_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            # A pause of a source-published object has no ads_ai row.  A
            # missing table is a deployment defect; zero affected rows is not.
            raise
        finally:
            connection.close()

    def _schema_signature(self, factory: Callable[[], Any], database: str) -> Tuple[List[Tuple[Any, ...]], List[Tuple[Any, ...]]]:
        columns = self._query(factory, "SHOW FULL COLUMNS FROM `%s`.`%s`" % (database, CREATED_DATA_TABLE))
        indexes = self._query(factory, "SHOW INDEX FROM `%s`.`%s`" % (database, CREATED_DATA_TABLE))
        column_signature = [
            (
                str(row.get("Field") or ""), str(row.get("Type") or "").lower(), str(row.get("Collation") or ""),
                str(row.get("Null") or ""), str(row.get("Key") or ""), row.get("Default"), str(row.get("Extra") or ""),
            )
            for row in columns
        ]
        index_signature = sorted(
            (
                str(row.get("Key_name") or ""), int(row.get("Non_unique") or 0), int(row.get("Seq_in_index") or 0),
                str(row.get("Column_name") or ""), str(row.get("Collation") or ""), row.get("Sub_part"), str(row.get("Index_type") or ""),
            )
            for row in indexes
        )
        return column_signature, index_signature

    def _verify_created_data_schema(self) -> None:
        if not self.persistence_enabled:
            raise AdControlV3Error("copy_persistence_disabled", "copy persistence is disabled", status=409)
        try:
            source = self._schema_signature(self.source_reader, CREATED_DATA_SOURCE_DATABASE)
            target = self._schema_signature(self.store_reader, CREATED_DATA_STORE_DATABASE)
        except Exception as exc:
            raise AdControlV3Error("copy_schema_unavailable", "Facebook copy ledger schema is unavailable", status=503) from exc
        if source != target:
            raise AdControlV3Error(
                "copy_schema_mismatch",
                "Facebook created_data mirror drifted from the source schema",
                status=503,
                details={"source_columns": len(source[0]), "target_columns": len(target[0])},
            )

    @staticmethod
    def _creative_parameters(client: MetaGraphClient, creative_id: str) -> Dict[str, Any]:
        if not creative_id:
            return {}
        creative = client.get(creative_id, "id,degrees_of_freedom_spec")
        degrees = copy.deepcopy(creative.get("degrees_of_freedom_spec") or {})
        if not isinstance(degrees, Mapping):
            return {}
        features = degrees.get("creative_features_spec") if isinstance(degrees.get("creative_features_spec"), Mapping) else {}
        sanitized = {
            str(key): copy.deepcopy(value)
            for key, value in features.items()
            if str(key) != "standard_enhancements" and isinstance(value, Mapping)
        }
        if not sanitized:
            return {}
        return {"degrees_of_freedom_spec": {"creative_features_spec": sanitized}}

    def _source_graph(self, client: MetaGraphClient, target: Mapping[str, Any], source_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        account = _account_id(target.get("ad_account_id"))
        account_meta = client.get("act_" + account, "id,account_id,name,currency,timezone_name,account_status")
        campaign_id = str(target.get("campaign_id") or (target.get("object_id") if target.get("object_level") == "campaign" else ""))
        campaign = client.get(
            campaign_id,
            "id,name,account_id,status,configured_status,effective_status,daily_budget,lifetime_budget,bid_strategy,source_campaign_id",
        )
        self._verify_account(campaign, account)
        level = str(target.get("object_level") or "")
        if level == "campaign":
            adsets = client.edge(
                campaign_id,
                "adsets",
                "id,name,account_id,campaign_id,status,configured_status,effective_status,daily_budget,lifetime_budget,bid_strategy,bid_constraints,start_time,source_adset_id",
                limit=MAX_ADSETS_PER_COPY,
            )
            ads = client.edge(
                campaign_id,
                "ads",
                "id,name,account_id,campaign_id,adset_id,status,configured_status,effective_status,creative{id},source_ad_id",
                limit=MAX_ADS_PER_COPY,
            )
        elif level == "adset":
            adset_id = str(target.get("adset_id") or target.get("object_id") or "")
            adsets = [client.get(
                adset_id,
                "id,name,account_id,campaign_id,status,configured_status,effective_status,daily_budget,lifetime_budget,bid_strategy,bid_constraints,start_time,source_adset_id",
            )]
            ads = client.edge(
                adset_id,
                "ads",
                "id,name,account_id,campaign_id,adset_id,status,configured_status,effective_status,creative{id},source_ad_id",
                limit=MAX_ADS_PER_COPY,
            )
        elif level == "ad":
            ad_id = str(target.get("ad_id") or target.get("object_id") or "")
            ad = client.get(
                ad_id,
                "id,name,account_id,campaign_id,adset_id,status,configured_status,effective_status,creative{id},source_ad_id",
            )
            adsets = [client.get(
                str(ad.get("adset_id") or target.get("adset_id") or ""),
                "id,name,account_id,campaign_id,status,configured_status,effective_status,daily_budget,lifetime_budget,bid_strategy,bid_constraints,start_time,source_adset_id",
            )]
            ads = [ad]
        else:
            raise AdControlV3Error("unsupported_object_level", "unsupported Facebook object level")
        if not adsets or not ads:
            raise AdControlV3Error("copy_tree_empty", "source object has no complete Ad Set and Ad tree", status=409)
        for item in adsets + ads:
            self._verify_account(item, account)
            if str(item.get("campaign_id") or "") != campaign_id:
                raise AdControlV3Error("meta_parent_mismatch", "source tree Campaign mapping changed", status=409)
        adset_ids = {str(item.get("id") or "") for item in adsets}
        if any(str(item.get("adset_id") or "") not in adset_ids for item in ads):
            raise AdControlV3Error("meta_parent_mismatch", "source tree Ad Set mapping is incomplete", status=409)
        source_by_ad = {str(row.get("ad_id") or ""): dict(row) for row in source_rows if str(row.get("ad_id") or "")}
        missing_rows = sorted(str(ad.get("id") or "") for ad in ads if str(ad.get("id") or "") not in source_by_ad)
        extra_rows = sorted(ad_id for ad_id in source_by_ad if ad_id not in {str(ad.get("id") or "") for ad in ads})
        if missing_rows or extra_rows:
            raise AdControlV3Error(
                "copy_created_data_mapping_incomplete",
                "Meta Ads and created_data rows do not map one-to-one",
                status=409,
                details={"missing_ad_ids": missing_rows[:20], "extra_ad_ids": extra_rows[:20]},
            )
        for ad in ads:
            creative = ad.get("creative") if isinstance(ad.get("creative"), Mapping) else {}
            ad["creative_id"] = str(creative.get("id") or "")
            ad["creative_parameters"] = self._creative_parameters(client, ad["creative_id"])
        return {
            "account": account_meta,
            "campaign": campaign,
            "adsets": adsets,
            "ads": ads,
            "source_rows": source_by_ad,
        }

    @staticmethod
    def _budget_type(campaign: Mapping[str, Any], adsets: Sequence[Mapping[str, Any]]) -> Tuple[str, str]:
        for key in ("daily_budget", "lifetime_budget"):
            if _decimal(campaign.get(key)) > 0:
                return "campaign", key
        types = {
            key
            for adset in adsets
            for key in ("daily_budget", "lifetime_budget")
            if _decimal(adset.get(key)) > 0
        }
        if len(types) != 1:
            raise AdControlV3Error("source_budget_unavailable", "source budget level or type is ambiguous", status=409)
        return "adset", next(iter(types))

    @staticmethod
    def _currency_offset(account: Mapping[str, Any]) -> Decimal:
        currency = str(account.get("currency") or "").upper()
        if not currency:
            raise AdControlV3Error("account_currency_unavailable", "ad account currency is unavailable", status=409)
        return Decimal("1") if currency in ZERO_DECIMAL_CURRENCIES else Decimal("100")

    def _budget_plan(self, target: Mapping[str, Any], graph: Mapping[str, Any]) -> Dict[str, Any]:
        params = dict(target.get("copy_parameters") or {})
        campaign = graph["campaign"]
        adsets = graph["adsets"]
        budget_level, budget_type = self._budget_type(campaign, adsets)
        mode = str(params.get("budget_mode") or "")
        if mode == "source_budget_ratio":
            ratio = _decimal(params.get("source_budget_ratio")) / Decimal("100")
            if ratio <= 0:
                raise AdControlV3Error("invalid_copy_budget", "source budget ratio must be positive", status=409)
            if budget_level == "campaign":
                campaign_budget = int((_decimal(campaign.get(budget_type)) * ratio).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                adset_budgets: Dict[str, int] = {}
            else:
                campaign_budget = 0
                adset_budgets = {
                    str(adset.get("id") or ""): int((_decimal(adset.get(budget_type)) * ratio).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                    for adset in adsets
                }
        elif mode in {"actual_cpi_multiplier", "fixed_target_cpi_multiplier"}:
            cpi = _decimal(params.get("target_cpi")) if mode == "fixed_target_cpi_multiplier" else _decimal((target.get("metrics") or {}).get("cpi"))
            multiplier = _decimal(params.get("budget_multiplier"))
            if cpi <= 0 or multiplier <= 0:
                raise AdControlV3Error("actual_cpi_unavailable", "CPI budget inputs are unavailable", status=409)
            total = int((cpi * multiplier * self._currency_offset(graph["account"])).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            if budget_level == "campaign":
                campaign_budget = total
                adset_budgets = {}
            else:
                weights = {str(item.get("id") or ""): _decimal(item.get(budget_type)) for item in adsets}
                weight_total = sum(weights.values(), Decimal("0"))
                if weight_total <= 0:
                    raise AdControlV3Error("source_budget_unavailable", "ABO source budgets are unavailable", status=409)
                remaining = total
                adset_budgets = {}
                ordered = sorted(weights)
                for index, adset_id in enumerate(ordered):
                    value = remaining if index == len(ordered) - 1 else int((Decimal(total) * weights[adset_id] / weight_total).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                    adset_budgets[adset_id] = value
                    remaining -= value
                campaign_budget = 0
        else:
            raise AdControlV3Error("invalid_copy_budget", "copy budget mode is unsupported", status=409)
        if campaign_budget < 0 or any(value <= 0 for value in adset_budgets.values()):
            raise AdControlV3Error("invalid_copy_budget", "computed copy budget is not positive", status=409)
        return {
            "budget_level": budget_level,
            "budget_type": budget_type,
            "campaign_budget": campaign_budget,
            "adset_budgets": adset_budgets,
        }

    @staticmethod
    def _roas_plan(target: Mapping[str, Any], graph: Mapping[str, Any]) -> Dict[str, int]:
        params = dict(target.get("copy_parameters") or {})
        direction = str(params.get("roas_adjustment_direction") or "")
        if not direction:
            return {}
        percent = _decimal(params.get("roas_adjustment_percent"))
        if percent <= 0 or percent > 100:
            raise AdControlV3Error("invalid_roas_adjustment", "ROAS adjustment percent is invalid", status=409)
        factor = Decimal("1") + (percent / Decimal("100")) * (Decimal("1") if direction == "increase" else Decimal("-1"))
        if factor <= 0:
            raise AdControlV3Error("invalid_roas_adjustment", "ROAS adjustment would produce a non-positive bid", status=409)
        campaign_strategy = str(graph["campaign"].get("bid_strategy") or "").upper()
        output: Dict[str, int] = {}
        for adset in graph["adsets"]:
            strategy = str(adset.get("bid_strategy") or campaign_strategy).upper()
            constraints = adset.get("bid_constraints") if isinstance(adset.get("bid_constraints"), Mapping) else {}
            floor = _decimal(constraints.get("roas_average_floor"))
            if strategy not in ROAS_BID_STRATEGIES or floor <= 0:
                raise AdControlV3Error(
                    "roas_bid_incompatible",
                    "source Ad Set is not using a compatible MIN_ROAS strategy",
                    status=409,
                    details={"source_adset_id": str(adset.get("id") or ""), "bid_strategy": strategy},
                )
            output[str(adset.get("id") or "")] = int((floor * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return output

    @staticmethod
    def _local_date(now: datetime, timezone_name: str) -> str:
        current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        if not timezone_name or ZoneInfo is None:
            raise AdControlV3Error("account_timezone_unavailable", "ad account timezone is unavailable", status=409)
        try:
            return current.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d")
        except Exception as exc:
            raise AdControlV3Error("account_timezone_unavailable", "ad account timezone is invalid", status=409) from exc

    def _reserve_intent(
        self,
        group: Mapping[str, Any],
        target: Mapping[str, Any],
        graph: Mapping[str, Any],
    ) -> Dict[str, Any]:
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        account_date = self._local_date(now, str(graph["account"].get("timezone_name") or ""))
        key_payload = {
            "group_id": str(group.get("group_id") or ""),
            "rule_id": str(target.get("control_rule_id") or ""),
            "account_id": _account_id(target.get("ad_account_id")),
            "object_level": str(target.get("object_level") or ""),
            "source_object_id": str(target.get("object_id") or ""),
            "account_date": account_date,
            "behavior_hash": str(group.get("behavior_hash") or ""),
        }
        idempotency_key = hashlib.sha256(_json(key_payload).encode("utf-8")).hexdigest()
        intent_id = uuid.uuid4().hex
        owner = str(group.get("owner_user_id") or "")
        group_limit = _positive_int((group.get("quotas") or {}).get("group_daily_limit"), HARD_DAILY_COPY_LIMIT, HARD_DAILY_COPY_LIMIT)
        user_limit = _positive_int((group.get("quotas") or {}).get("user_daily_limit"), 10, HARD_DAILY_COPY_LIMIT)
        rule_limit = _positive_int((target.get("copy_parameters") or {}).get("daily_copy_limit"), 1, HARD_DAILY_COPY_LIMIT)
        cooldown_days = _positive_int(
            (target.get("copy_parameters") or {}).get("cooldown_days") or (group.get("quotas") or {}).get("object_cooldown_days"),
            1,
            3650,
        )
        connection = self.store_writer()
        lock_names = [
            "adcv3:user:%s:%s" % (hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16], account_date),
            "adcv3:source:%s" % hashlib.sha256(
                (
                    _account_id(target.get("ad_account_id"))
                    + ":" + str(target.get("object_level") or "")
                    + ":" + str(target.get("object_id") or "")
                ).encode("utf-8")
            ).hexdigest()[:24],
        ]
        acquired_locks: List[str] = []
        try:
            cursor = connection.cursor()
            for lock_name in lock_names:
                cursor.execute("SELECT GET_LOCK(%s,3) AS acquired", (lock_name,))
                acquired = int((_one(cursor).get("acquired") or 0))
                if acquired != 1:
                    raise AdControlV3Error("copy_quota_lock_busy", "copy quota reservation is busy", status=409)
                acquired_locks.append(lock_name)
            cursor.execute(
                "SELECT * FROM `ads_ai`.`ad_control_v3_copy_intent` WHERE `idempotency_key`=%s LIMIT 1",
                (idempotency_key,),
            )
            existing = _one(cursor)
            if existing:
                connection.commit()
                return {"reserved": False, "reason": "duplicate_intent", "intent": existing}
            cursor.execute(
                "SELECT "
                "SUM(CASE WHEN `owner_user_id`=%s THEN 1 ELSE 0 END) AS user_count,"
                "SUM(CASE WHEN `rule_group_id`=%s THEN 1 ELSE 0 END) AS group_count,"
                "SUM(CASE WHEN `rule_group_id`=%s AND `control_rule_id`=%s THEN 1 ELSE 0 END) AS rule_count "
                "FROM `ads_ai`.`ad_control_v3_copy_intent` WHERE `account_date`=%s AND `status` NOT IN ('rejected')",
                (owner, str(group.get("group_id") or ""), str(group.get("group_id") or ""), str(target.get("control_rule_id") or ""), account_date),
            )
            counts = _one(cursor)
            if int(counts.get("user_count") or 0) >= user_limit:
                reason = "user_daily_copy_limit"
            elif int(counts.get("group_count") or 0) >= group_limit:
                reason = "group_daily_copy_limit"
            elif int(counts.get("rule_count") or 0) >= rule_limit:
                reason = "rule_daily_copy_limit"
            else:
                reason = ""
            if reason:
                connection.commit()
                return {"reserved": False, "reason": reason, "intent": {}}
            cursor.execute(
                "SELECT `intent_id` FROM `ads_ai`.`ad_control_v3_copy_intent` "
                "WHERE `ad_account_id`=%s AND `object_level`=%s AND `source_object_id`=%s "
                "AND `created_at` >= DATE_SUB(UTC_TIMESTAMP(6), INTERVAL %s DAY) AND `status` NOT IN ('rejected') LIMIT 1",
                (_account_id(target.get("ad_account_id")), str(target.get("object_level") or ""), str(target.get("object_id") or ""), cooldown_days),
            )
            if _one(cursor):
                connection.commit()
                return {"reserved": False, "reason": "source_cooldown", "intent": {}}
            now_text = _utc_text(now)
            cursor.execute(
                "INSERT INTO `ads_ai`.`ad_control_v3_copy_intent` "
                "(`intent_id`,`idempotency_key`,`owner_user_id`,`rule_group_id`,`control_rule_id`,`optimizer_id`,`ad_account_id`,`object_level`,`source_object_id`,`account_date`,`behavior_hash`,`status`,`result_json`,`error_code`,`error_message`,`created_at`,`updated_at`) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'reserved','{}','','',%s,%s)",
                (
                    intent_id, idempotency_key, owner, str(group.get("group_id") or ""), str(target.get("control_rule_id") or ""),
                    int(group.get("optimizer_id") or 0), _account_id(target.get("ad_account_id")), str(target.get("object_level") or ""),
                    str(target.get("object_id") or ""), account_date, str(group.get("behavior_hash") or ""), now_text, now_text,
                ),
            )
            connection.commit()
            return {"reserved": True, "reason": "", "intent": {"intent_id": intent_id, "idempotency_key": idempotency_key}}
        except AdControlV3Error:
            connection.rollback()
            raise
        except Exception as exc:
            connection.rollback()
            raise AdControlV3Error("copy_intent_reservation_failed", "copy intent could not be reserved", status=503) from exc
        finally:
            try:
                cursor = connection.cursor()
                for lock_name in reversed(acquired_locks):
                    cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
            except Exception:
                pass
            connection.close()

    def _update_intent(self, intent_id: str, status: str, result: Mapping[str, Any], error: Optional[AdControlV3Error] = None) -> None:
        connection = self.store_writer()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "UPDATE `ads_ai`.`ad_control_v3_copy_intent` SET `status`=%s,`result_json`=%s,`error_code`=%s,`error_message`=%s,`updated_at`=%s,`completed_at`=%s WHERE `intent_id`=%s",
                (
                    status, _json(result), error.code if error else "", (error.message if error else "")[:1000], _utc_text(),
                    _utc_text() if status in {"completed", "completed_paused", "quarantined", "failed"} else None, intent_id,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _copied_id(payload: Mapping[str, Any], field: str) -> str:
        value = str(payload.get(field) or payload.get("id") or "")
        if not value:
            raise AdControlV3Error("meta_copy_response_invalid", "Meta copy response did not contain a copied object id", status=502)
        return value

    def _copy_campaign(self, client: MetaGraphClient, source_campaign_id: str, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = client.post(source_campaign_id + "/copies", {"deep_copy": False, "status_option": "PAUSED"})
        new_id = self._copied_id(response, "copied_campaign_id")
        if state is not None:
            state["campaign"] = {"id": new_id}
        meta = client.get(new_id, "id,name,account_id,status,configured_status,effective_status,source_campaign_id,daily_budget,lifetime_budget,bid_strategy")
        if _configured_status(meta) != "PAUSED" or str(meta.get("source_campaign_id") or "") != source_campaign_id:
            raise AdControlV3Error("copy_mapping_incomplete", "copied Campaign mapping/status could not be verified", status=502)
        if state is not None:
            state["campaign"] = meta
        return meta

    def _copy_adset(self, client: MetaGraphClient, source_adset_id: str, target_campaign_id: str, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = client.post(
            source_adset_id + "/copies",
            {"campaign_id": target_campaign_id, "deep_copy": False, "status_option": "PAUSED"},
        )
        new_id = self._copied_id(response, "copied_adset_id")
        if state is not None:
            state.setdefault("adsets", {})[source_adset_id] = {"id": new_id}
        meta = client.get(new_id, "id,name,account_id,campaign_id,status,configured_status,effective_status,source_adset_id,daily_budget,lifetime_budget,bid_strategy,bid_constraints,start_time")
        if _configured_status(meta) != "PAUSED" or str(meta.get("source_adset_id") or "") != source_adset_id or str(meta.get("campaign_id") or "") != target_campaign_id:
            raise AdControlV3Error("copy_mapping_incomplete", "copied Ad Set mapping/status could not be verified", status=502)
        if state is not None:
            state.setdefault("adsets", {})[source_adset_id] = meta
        return meta

    def _copy_ad(self, client: MetaGraphClient, source_ad: Mapping[str, Any], target_adset_id: str, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        values: Dict[str, Any] = {"adset_id": target_adset_id, "status_option": "PAUSED"}
        if source_ad.get("creative_parameters"):
            values["creative_parameters"] = source_ad["creative_parameters"]
        response = client.post(str(source_ad.get("id") or "") + "/copies", values)
        new_id = self._copied_id(response, "copied_ad_id")
        source_ad_id = str(source_ad.get("id") or "")
        if state is not None:
            state.setdefault("ads", {})[source_ad_id] = {"id": new_id}
        meta = client.get(new_id, "id,name,account_id,campaign_id,adset_id,status,configured_status,effective_status,source_ad_id,creative{id}")
        if _configured_status(meta) != "PAUSED" or str(meta.get("source_ad_id") or "") != str(source_ad.get("id") or "") or str(meta.get("adset_id") or "") != target_adset_id:
            raise AdControlV3Error("copy_mapping_incomplete", "copied Ad mapping/status could not be verified", status=502)
        creative = meta.get("creative") if isinstance(meta.get("creative"), Mapping) else {}
        meta["creative_id"] = str(creative.get("id") or "")
        if not meta["creative_id"]:
            raise AdControlV3Error("copy_mapping_incomplete", "copied Creative mapping is missing", status=502)
        if state is not None:
            state.setdefault("ads", {})[source_ad_id] = meta
        return meta

    def _apply_adjustments(
        self,
        client: MetaGraphClient,
        graph: Mapping[str, Any],
        copied_campaign: Optional[Mapping[str, Any]],
        copied_adsets: Mapping[str, Mapping[str, Any]],
        budget: Mapping[str, Any],
        roas: Mapping[str, int],
    ) -> Dict[str, Any]:
        if budget["budget_level"] == "campaign":
            if not copied_campaign:
                raise AdControlV3Error("carrier_budget_not_independent", "selected carrier cannot receive an independent CBO budget", status=409)
            client.post(str(copied_campaign.get("id") or ""), {budget["budget_type"]: int(budget["campaign_budget"]), "status": "PAUSED"})
        for source_adset_id, copied in copied_adsets.items():
            values: Dict[str, Any] = {"status": "PAUSED"}
            if budget["budget_level"] == "adset":
                value = (budget.get("adset_budgets") or {}).get(source_adset_id)
                if not value:
                    raise AdControlV3Error("source_budget_unavailable", "copied Ad Set budget mapping is missing", status=409)
                values[budget["budget_type"]] = int(value)
            if source_adset_id in roas:
                values["bid_constraints"] = {"roas_average_floor": int(roas[source_adset_id])}
            client.post(str(copied.get("id") or ""), values)
        campaign_after = None
        if copied_campaign:
            campaign_after = client.get(
                str(copied_campaign.get("id") or ""),
                "id,name,account_id,status,configured_status,effective_status,daily_budget,lifetime_budget,bid_strategy,source_campaign_id",
            )
            if _configured_status(campaign_after) != "PAUSED":
                raise AdControlV3Error("copy_adjustment_readback_failed", "copied Campaign did not remain PAUSED", status=502)
            if budget["budget_level"] == "campaign" and int(campaign_after.get(budget["budget_type"]) or 0) != int(budget["campaign_budget"]):
                raise AdControlV3Error("copy_adjustment_readback_failed", "copied Campaign budget readback mismatched", status=502)
        adsets_after: Dict[str, Dict[str, Any]] = {}
        for source_adset_id, copied in copied_adsets.items():
            verified = client.get(
                str(copied.get("id") or ""),
                "id,name,account_id,campaign_id,status,configured_status,effective_status,daily_budget,lifetime_budget,bid_strategy,bid_constraints,start_time,source_adset_id",
            )
            if _configured_status(verified) != "PAUSED":
                raise AdControlV3Error("copy_adjustment_readback_failed", "copied Ad Set did not remain PAUSED", status=502)
            if budget["budget_level"] == "adset" and int(verified.get(budget["budget_type"]) or 0) != int(budget["adset_budgets"][source_adset_id]):
                raise AdControlV3Error("copy_adjustment_readback_failed", "copied Ad Set budget readback mismatched", status=502)
            constraints = verified.get("bid_constraints") if isinstance(verified.get("bid_constraints"), Mapping) else {}
            if source_adset_id in roas and int(constraints.get("roas_average_floor") or 0) != int(roas[source_adset_id]):
                raise AdControlV3Error("copy_adjustment_readback_failed", "copied ROAS floor readback mismatched", status=502)
            adsets_after[source_adset_id] = verified
        return {"campaign": campaign_after, "adsets": adsets_after}

    def _copy_tree(
        self,
        client: MetaGraphClient,
        target: Mapping[str, Any],
        graph: Mapping[str, Any],
        budget: Mapping[str, Any],
        roas: Mapping[str, int],
        state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        level = str(target.get("object_level") or "")
        carrier = str((target.get("copy_parameters") or {}).get("carrier_strategy") or "")
        source_campaign_id = str(graph["campaign"].get("id") or "")
        output = state if state is not None else {}
        output.setdefault("campaign", None)
        output.setdefault("adsets", {})
        output.setdefault("ads", {})
        output["budget"] = dict(budget)
        output["roas"] = dict(roas)
        copied_campaign: Optional[Dict[str, Any]] = output.get("campaign")
        copied_adsets: Dict[str, Dict[str, Any]] = output["adsets"]
        copied_ads: Dict[str, Dict[str, Any]] = output["ads"]
        if level == "campaign":
            if carrier != "deep_copy_campaign":
                raise AdControlV3Error("invalid_carrier_strategy", "Campaign copy carrier is invalid", status=409)
            copied_campaign = self._copy_campaign(client, source_campaign_id, output)
            for source_adset in graph["adsets"]:
                source_id = str(source_adset.get("id") or "")
                copied_adsets[source_id] = self._copy_adset(client, source_id, str(copied_campaign.get("id") or ""), output)
            for source_ad in graph["ads"]:
                source_id = str(source_ad.get("id") or "")
                copied_ads[source_id] = self._copy_ad(client, source_ad, str(copied_adsets[str(source_ad.get("adset_id") or "")].get("id") or ""), output)
        elif level == "adset":
            source_adset = graph["adsets"][0]
            source_adset_id = str(source_adset.get("id") or "")
            if carrier == "new_campaign":
                copied_campaign = self._copy_campaign(client, source_campaign_id, output)
                target_campaign_id = str(copied_campaign.get("id") or "")
            elif carrier == "same_campaign":
                if budget["budget_level"] == "campaign":
                    raise AdControlV3Error("carrier_budget_not_independent", "same Campaign carrier cannot receive an independent CBO budget", status=409)
                target_campaign_id = source_campaign_id
            else:
                raise AdControlV3Error("invalid_carrier_strategy", "Ad Set copy carrier is invalid", status=409)
            copied_adsets[source_adset_id] = self._copy_adset(client, source_adset_id, target_campaign_id, output)
            for source_ad in graph["ads"]:
                source_id = str(source_ad.get("id") or "")
                copied_ads[source_id] = self._copy_ad(client, source_ad, str(copied_adsets[source_adset_id].get("id") or ""), output)
        elif level == "ad":
            source_ad = graph["ads"][0]
            source_adset = graph["adsets"][0]
            source_adset_id = str(source_adset.get("id") or "")
            if carrier == "same_adset":
                raise AdControlV3Error("carrier_budget_not_independent", "same Ad Set carrier cannot apply an independent budget", status=409)
            if carrier == "isolated_campaign":
                copied_campaign = self._copy_campaign(client, source_campaign_id, output)
                target_campaign_id = str(copied_campaign.get("id") or "")
            elif carrier == "isolated_adset":
                if budget["budget_level"] == "campaign":
                    raise AdControlV3Error("carrier_budget_not_independent", "isolated Ad Set cannot receive an independent CBO budget", status=409)
                target_campaign_id = source_campaign_id
            else:
                raise AdControlV3Error("invalid_carrier_strategy", "Ad copy carrier is invalid", status=409)
            copied_adsets[source_adset_id] = self._copy_adset(client, source_adset_id, target_campaign_id, output)
            copied_ads[str(source_ad.get("id") or "")] = self._copy_ad(client, source_ad, str(copied_adsets[source_adset_id].get("id") or ""), output)
        else:
            raise AdControlV3Error("unsupported_object_level", "unsupported Facebook object level")
        adjusted = self._apply_adjustments(client, graph, copied_campaign, copied_adsets, budget, roas)
        if adjusted.get("campaign"):
            copied_campaign = adjusted["campaign"]
        output["campaign"] = copied_campaign
        output["adsets"] = adjusted["adsets"]
        output["ads"] = copied_ads
        return output

    @staticmethod
    def _pause_created(client: MetaGraphClient, copied: Mapping[str, Any]) -> None:
        ids: List[str] = []
        ids.extend(str(item.get("id") or "") for item in (copied.get("ads") or {}).values())
        ids.extend(str(item.get("id") or "") for item in (copied.get("adsets") or {}).values())
        campaign = copied.get("campaign") if isinstance(copied.get("campaign"), Mapping) else {}
        if campaign:
            ids.append(str(campaign.get("id") or ""))
        for object_id in ids:
            if not object_id:
                continue
            try:
                client.post(object_id, {"status": "PAUSED"})
            except Exception:
                logging.error("failed to quarantine copied Meta object id=%s", object_id, exc_info=True)

    def _write_ledger(
        self,
        intent_id: str,
        group: Mapping[str, Any],
        target: Mapping[str, Any],
        graph: Mapping[str, Any],
        copied: Mapping[str, Any],
    ) -> Dict[str, Any]:
        connection = self.store_writer()
        inserted_ids: List[int] = []
        now_text = _utc_text()
        try:
            cursor = connection.cursor()
            cursor.execute("SHOW COLUMNS FROM `ads_ai`.`ads_facebook_auto_created_data`")
            columns = [str(row.get("Field") or "") for row in _rows(cursor)]
            insert_columns = [column for column in columns if column != "id"]
            required = {"campaign_id", "adset_id", "ad_id", "creative_id", "status", "created_at", "updated_at"}
            if not required.issubset(set(insert_columns)):
                raise AdControlV3Error("copy_schema_mismatch", "Facebook created_data mirror is missing required columns", status=503)
            campaign = copied.get("campaign") if isinstance(copied.get("campaign"), Mapping) else None
            source_campaign = graph["campaign"]
            copied_campaign_id = str((campaign or {}).get("id") or source_campaign.get("id") or "")
            for source_ad_id, new_ad in (copied.get("ads") or {}).items():
                source_row = dict(graph["source_rows"][source_ad_id])
                source_ad = next(item for item in graph["ads"] if str(item.get("id") or "") == source_ad_id)
                source_adset_id = str(source_ad.get("adset_id") or "")
                new_adset = dict((copied.get("adsets") or {})[source_adset_id])
                values = {column: source_row.get(column) for column in insert_columns}
                budget = copied["budget"]
                actual_budget = (
                    int((campaign or {}).get(budget["budget_type"]) or budget.get("campaign_budget") or 0)
                    if budget["budget_level"] == "campaign"
                    else int(new_adset.get(budget["budget_type"]) or (budget.get("adset_budgets") or {}).get(source_adset_id) or 0)
                )
                constraints = new_adset.get("bid_constraints") if isinstance(new_adset.get("bid_constraints"), Mapping) else {}
                values.update(
                    {
                        "campaign_id": copied_campaign_id,
                        "campaign_name": str((campaign or {}).get("name") or source_campaign.get("name") or ""),
                        "adset_id": str(new_adset.get("id") or ""),
                        "adset_name": str(new_adset.get("name") or ""),
                        "ad_id": str(new_ad.get("id") or ""),
                        "ad_name": str(new_ad.get("name") or ""),
                        "creative_id": str(new_ad.get("creative_id") or ""),
                        "status": "PAUSED",
                        "budget_level": "campaign" if budget["budget_level"] == "campaign" else "adset",
                        "budget": actual_budget,
                        "latest_budget": actual_budget,
                        "bid_type": str(new_adset.get("bid_strategy") or source_row.get("bid_type") or ""),
                        "bid_control": "MIN_ROAS" if constraints.get("roas_average_floor") is not None else source_row.get("bid_control"),
                        "bid_amount": constraints.get("roas_average_floor") if constraints.get("roas_average_floor") is not None else source_row.get("bid_amount"),
                        "start_time": new_adset.get("start_time") or source_row.get("start_time"),
                        "local_status": 0,
                        "campaign_action_at": 0,
                        "adset_action_at": 0,
                        "ad_action_at": 0,
                        "created_at": now_text,
                        "updated_at": now_text,
                    }
                )
                placeholders = ",".join(["%s"] * len(insert_columns))
                cursor.execute(
                    "INSERT INTO `ads_ai`.`ads_facebook_auto_created_data` (%s) VALUES (%s)"
                    % (",".join("`%s`" % column for column in insert_columns), placeholders),
                    tuple(values.get(column) for column in insert_columns),
                )
                new_created_id = int(cursor.lastrowid or 0)
                if new_created_id <= 0:
                    raise AdControlV3Error("created_data_write_failed", "created_data insert id is unavailable", status=503)
                inserted_ids.append(new_created_id)
                cursor.execute(
                    "INSERT INTO `ads_ai`.`ad_control_copy_lineage` "
                    "(`channel`,`copy_intent_id`,`source_database`,`source_table`,`source_created_data_id`,`source_campaign_id`,`source_adset_id`,`source_ad_id`,`new_created_data_id`,`new_campaign_id`,`new_adset_id`,`new_ad_id`,`new_creative_id`,`rule_group_id`,`control_rule_id`,`owner_user_id`,`optimizer_id`,`meta_status`,`ledger_status`,`activation_status`,`error_reason`,`created_at`,`updated_at`) "
                    "VALUES ('facebook',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PAUSED','written','pending','',%s,%s)",
                    (
                        intent_id, str(source_row.get("__source_database") or ""), str(source_row.get("__source_table") or ""),
                        int(source_row.get("id") or 0), str(source_campaign.get("id") or ""), source_adset_id, source_ad_id,
                        new_created_id, copied_campaign_id, str(new_adset.get("id") or ""), str(new_ad.get("id") or ""),
                        str(new_ad.get("creative_id") or ""), str(group.get("group_id") or ""), str(target.get("control_rule_id") or ""),
                        str(group.get("owner_user_id") or ""), int(group.get("optimizer_id") or 0), now_text, now_text,
                    ),
                )
            if len(inserted_ids) != len(copied.get("ads") or {}):
                raise AdControlV3Error("created_data_write_failed", "created_data row count mismatched copied Ads", status=503)
            placeholders = ",".join(["%s"] * len(inserted_ids))
            cursor.execute(
                "SELECT COUNT(*) AS total FROM `ads_ai`.`ads_facebook_auto_created_data` WHERE `id` IN (%s)" % placeholders,
                tuple(inserted_ids),
            )
            if int(_one(cursor).get("total") or 0) != len(inserted_ids):
                raise AdControlV3Error("created_data_readback_failed", "created_data readback row count mismatched", status=503)
            cursor.execute(
                "UPDATE `ads_ai`.`ad_control_v3_copy_intent` SET `status`='ledger_written',`result_json`=%s,`updated_at`=%s WHERE `intent_id`=%s AND `status` IN ('reserved','meta_created')",
                (_json({"created_data_ids": inserted_ids, "copied": self._public_copy_result(copied)}), now_text, intent_id),
            )
            if int(cursor.rowcount or 0) != 1:
                raise AdControlV3Error("copy_intent_state_conflict", "copy intent state changed during ledger write", status=409)
            connection.commit()
            return {"created_data_ids": inserted_ids, "row_count": len(inserted_ids)}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _public_copy_result(copied: Mapping[str, Any]) -> Dict[str, Any]:
        campaign = copied.get("campaign") if isinstance(copied.get("campaign"), Mapping) else {}
        return {
            "campaign_id": str(campaign.get("id") or ""),
            "adsets": [
                {"source_adset_id": source_id, "new_adset_id": str(value.get("id") or "")}
                for source_id, value in sorted((copied.get("adsets") or {}).items())
            ],
            "ads": [
                {
                    "source_ad_id": source_id,
                    "new_ad_id": str(value.get("id") or ""),
                    "new_creative_id": str(value.get("creative_id") or ""),
                }
                for source_id, value in sorted((copied.get("ads") or {}).items())
            ],
            "budget": dict(copied.get("budget") or {}),
            "roas": dict(copied.get("roas") or {}),
        }

    def _activate(self, client: MetaGraphClient, copied: Mapping[str, Any], created_data_ids: Sequence[int]) -> Dict[str, Any]:
        campaign = copied.get("campaign") if isinstance(copied.get("campaign"), Mapping) else {}
        if campaign:
            client.post(str(campaign.get("id") or ""), {"status": "ACTIVE"})
        for item in (copied.get("adsets") or {}).values():
            client.post(str(item.get("id") or ""), {"status": "ACTIVE"})
        for item in (copied.get("ads") or {}).values():
            client.post(str(item.get("id") or ""), {"status": "ACTIVE"})
        for item in (copied.get("ads") or {}).values():
            readback = client.get(str(item.get("id") or ""), "id,status,configured_status,effective_status")
            if _configured_status(readback) != "ACTIVE":
                raise AdControlV3Error("copy_activation_failed", "copied Ad was not confirmed ACTIVE", status=502)
        connection = self.store_writer()
        try:
            cursor = connection.cursor()
            placeholders = ",".join(["%s"] * len(created_data_ids))
            cursor.execute(
                "UPDATE `ads_ai`.`ads_facebook_auto_created_data` SET `status`='ACTIVE',`updated_at`=NOW(6) WHERE `id` IN (%s)" % placeholders,
                tuple(created_data_ids),
            )
            cursor.execute(
                "UPDATE `ads_ai`.`ad_control_copy_lineage` SET `meta_status`='ACTIVE',`activation_status`='active',`updated_at`=NOW(6) WHERE `new_created_data_id` IN (%s)" % placeholders,
                tuple(created_data_ids),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {"status": "ACTIVE", "created_data_ids": list(created_data_ids)}

    def _copy(self, group: Mapping[str, Any], target: Mapping[str, Any]) -> Dict[str, Any]:
        if not self.copy_enabled:
            raise AdControlV3Error("live_copy_disabled", "Facebook live copy is disabled", status=409)
        self._verify_created_data_schema()
        source_rows = self._source_rows(target)
        token = self._token(str(target.get("product") or ""), source_rows)
        client = self._client(token)
        graph = self._source_graph(client, target, source_rows)
        budget = self._budget_plan(target, graph)
        roas = self._roas_plan(target, graph)
        reservation = self._reserve_intent(group, target, graph)
        if not reservation.get("reserved"):
            existing = reservation.get("intent") or {}
            status = str(existing.get("status") or "")
            if reservation.get("reason") == "duplicate_intent" and status in {"completed", "completed_paused"}:
                return {
                    "status": "skipped",
                    "reason": "duplicate_completed_intent",
                    "meta_write_count": 0,
                    "copy_intent_id": str(existing.get("intent_id") or ""),
                }
            return {
                "status": "skipped",
                "reason": str(reservation.get("reason") or "copy_intent_not_reserved"),
                "meta_write_count": 0,
                "copy_intent_id": str(existing.get("intent_id") or ""),
            }
        intent_id = str(reservation["intent"]["intent_id"])
        copied: Dict[str, Any] = {
            "campaign": None,
            "adsets": {},
            "ads": {},
            "budget": dict(budget),
            "roas": dict(roas),
        }
        try:
            copied = self._copy_tree(client, target, graph, budget, roas, state=copied)
            public_copy = self._public_copy_result(copied)
            self._update_intent(intent_id, "meta_created", {"copied": public_copy})
            ledger = self._write_ledger(intent_id, group, target, graph, copied)
            if self.activation_enabled:
                activation = self._activate(client, copied, ledger["created_data_ids"])
                final_status = "completed"
                reason = ""
            else:
                activation = {"status": "PAUSED", "created_data_ids": ledger["created_data_ids"]}
                connection = self.store_writer()
                try:
                    cursor = connection.cursor()
                    cursor.execute(
                        "UPDATE `ads_ai`.`ad_control_copy_lineage` SET `activation_status`='kept_paused',`updated_at`=NOW(6) WHERE `copy_intent_id`=%s",
                        (intent_id,),
                    )
                    connection.commit()
                finally:
                    connection.close()
                final_status = "completed_paused"
                reason = "copy_activation_disabled"
            final = {"copied": public_copy, "ledger": ledger, "activation": activation}
            self._update_intent(intent_id, final_status, final)
            return {
                "status": "succeeded",
                "reason": reason,
                "meta_write_count": client.write_count,
                "copy_intent_id": intent_id,
                "copy_result": final,
            }
        except AdControlV3Error as exc:
            if copied:
                self._pause_created(client, copied)
            try:
                self._update_intent(intent_id, "quarantined", {"copied": self._public_copy_result(copied) if copied else {}}, error=exc)
            except Exception:
                logging.exception("failed to quarantine copy intent %s", intent_id)
            exc.details["copy_intent_id"] = intent_id
            exc.details["meta_write_count"] = client.write_count
            raise
        except Exception as exc:
            if copied:
                self._pause_created(client, copied)
            wrapped = AdControlV3Error("copy_execution_failed", "Facebook copy execution failed", status=502)
            try:
                self._update_intent(intent_id, "quarantined", {"copied": self._public_copy_result(copied) if copied else {}}, error=wrapped)
            except Exception:
                logging.exception("failed to quarantine copy intent %s", intent_id)
            raise wrapped from exc


def build_live_executor(
    source_reader: Callable[[], Any],
    store_reader: Callable[[], Any],
    store_writer: Callable[[], Any],
    *,
    environment: Mapping[str, Any],
) -> FacebookLiveExecutor:
    return FacebookLiveExecutor(
        source_reader,
        store_reader,
        store_writer,
        pause_enabled=_truthy(environment.get("AD_CONTROL_V3_LIVE_PAUSE_ENABLED")),
        copy_enabled=_truthy(environment.get("AD_CONTROL_V3_LIVE_COPY_ENABLED")),
        persistence_enabled=_truthy(environment.get("AD_CONTROL_V3_COPY_PERSISTENCE_ENABLED")),
        activation_enabled=_truthy(environment.get("AD_CONTROL_V3_COPY_ACTIVATE_ENABLED")),
        graph_timeout_seconds=int(environment.get("AD_CONTROL_V3_META_TIMEOUT_SECONDS") or 20),
    )


__all__ = [
    "ENABLE_CONFIRMATION",
    "FacebookLiveExecutor",
    "LIVE_CONFIRMATION",
    "MAX_COPY_TARGETS",
    "MAX_LIVE_TARGETS",
    "MetaGraphClient",
    "build_live_executor",
]
