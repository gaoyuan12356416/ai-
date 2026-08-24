"""Read-only Page-pool, token and X-compatible material source adapters."""

from __future__ import annotations

import random
import re
import time
from functools import cmp_to_key
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence
from urllib.parse import urlsplit


UTC = timezone.utc


class RepositoryError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 503):
        self.code, self.status = code, status
        super().__init__(message)


@dataclass(frozen=True)
class PageGroup:
    group_id: str
    owner_user_id: str
    group_type: int
    name: str
    app_id: str
    product: str
    total_pages: int
    publishable_pages: int


@dataclass(frozen=True)
class PageTarget:
    group_id: str
    group_ids: tuple[str, ...]
    page_id: str
    owner_user_id: str
    timezone: str
    language: str
    eligible_token_count: int
    page_name: str = ""


@dataclass(frozen=True)
class PageCredential:
    page_id: str
    fb_user_id: str
    credential_id: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class MaterialCandidate:
    material_id: str
    content_id: str
    media_url: str
    material_name: str
    drama_name: str
    language: str
    duration_seconds: Decimal
    material_spend: Decimal
    material_roas: Decimal | None
    drama_spend: Decimal
    drama_roas: Decimal | None
    resource_type_v2: str
    drama_description: str = ""
    material_tag: str = ""


@dataclass(frozen=True)
class CandidateSnapshot:
    candidates: tuple[MaterialCandidate, ...]
    metric_generation_ids: tuple[int, ...]
    metric_dates: tuple[str, ...]


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        raise RepositoryError("fb_auto_source_data_invalid", "素材指标数据无效") from None
    if not result.is_finite() or result < 0:
        raise RepositoryError("fb_auto_source_data_invalid", "素材指标数据无效")
    return result


def _roas(spend: Decimal, revenue: Decimal) -> Decimal | None:
    return None if spend <= 0 else revenue / spend * Decimal("100")


def _https(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        valid = parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None and parsed.password is None and parsed.fragment == ""
    except ValueError:
        valid = False
    return text if valid else ""


def _description(value: Any) -> str:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return ""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if any(ord(char) < 32 for char in text):
        return ""
    return text[:4096].rstrip()


class ReadOnlyMySQL:
    def __init__(self, connection_factory: Callable[[], Any], schema: str = "kunlunads_dev", blacklist_schema: str = "ads_setting"):
        if not callable(connection_factory) or not re.fullmatch(r"[A-Za-z0-9_]+", schema) or not re.fullmatch(r"[A-Za-z0-9_]+", blacklist_schema):
            raise ValueError("invalid MySQL repository configuration")
        self.connection_factory, self.schema, self.blacklist_schema = connection_factory, schema, blacklist_schema

    def select(self, sql: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
        if not re.match(r"(?is)^\s*SELECT\b", str(sql or "")):
            raise RepositoryError("fb_auto_read_only_query_denied", "只读仓库拒绝了非SELECT语句", 500)
        connection = cursor = None
        try:
            connection = self.connection_factory()
            try:
                import pymysql
                cursor = connection.cursor(pymysql.cursors.DictCursor)
            except (ImportError, TypeError):
                cursor = connection.cursor()
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()
            if not all(isinstance(row, Mapping) for row in rows):
                raise RepositoryError("fb_auto_source_data_invalid", "只读数据返回格式无效")
            return [dict(row) for row in rows]
        except RepositoryError:
            raise
        except Exception:
            raise RepositoryError("fb_auto_source_query_failed", "Page或素材只读查询失败") from None
        finally:
            for value in (cursor, connection):
                close = getattr(value, "close", None)
                if callable(close):
                    close()

    def iter_select(self, sql: str, params: Sequence[Any], *, batch_size: int = 1000):
        """Server-side bounded read used only by exact one-day metric refresh."""
        if not re.match(r"(?is)^\s*SELECT\b", str(sql or "")) or not 1 <= int(batch_size) <= 5000:
            raise RepositoryError("fb_auto_read_only_query_denied", "只读仓库拒绝了无效查询", 500)
        connection = cursor = None
        try:
            connection = self.connection_factory()
            try:
                import pymysql
                cursor = connection.cursor(pymysql.cursors.SSDictCursor)
            except (ImportError, TypeError):
                cursor = connection.cursor()
            cursor.execute(sql, tuple(params))
            fetchmany = getattr(cursor, "fetchmany", None)
            while callable(fetchmany):
                batch = fetchmany(batch_size)
                if not batch:
                    return
                for row in batch:
                    if not isinstance(row, Mapping):
                        raise RepositoryError("fb_auto_source_data_invalid", "只读数据返回格式无效")
                    yield dict(row)
            for row in cursor.fetchall():
                if not isinstance(row, Mapping):
                    raise RepositoryError("fb_auto_source_data_invalid", "只读数据返回格式无效")
                yield dict(row)
        except RepositoryError:
            raise
        except Exception:
            raise RepositoryError("fb_auto_metric_source_query_failed", "单日指标只读查询失败") from None
        finally:
            for value in (cursor, connection):
                close = getattr(value, "close", None)
                if callable(close):
                    close()


class PagePoolRepository:
    def __init__(self, mysql: ReadOnlyMySQL):
        self.mysql = mysql

    def list_groups(self, *, is_admin: bool, owner_user_id: str) -> List[PageGroup]:
        owner_sql, params = ("", []) if is_admin else (" AND CAST(g.user_id AS CHAR)=%s", [owner_user_id])
        if not is_admin and not re.fullmatch(r"[1-9][0-9]{0,30}", owner_user_id):
            raise RepositoryError("fb_auto_owner_mapping_missing", "当前账号未唯一映射到Page池负责人", 403)
        sql = f"""
            SELECT CAST(g.id AS CHAR) AS group_id,
                   CAST(g.user_id AS CHAR) AS owner_user_id,g.type AS group_type,
                   COALESCE(g.name,'') AS group_name,CAST(g.app_id AS CHAR) AS app_id,
                   COALESCE(a.name,'') AS product,
                   COUNT(DISTINCT i.page_id) AS total_pages,
                   COUNT(DISTINCT CASE WHEN EXISTS (
                       SELECT 1 FROM `{self.mysql.schema}`.ads_facebook_page_post p
                        WHERE p.page_id=i.page_id AND p.status<>1
                          AND TRIM(p.page_access_token)<>''
                   ) THEN i.page_id END) AS publishable_pages
              FROM `{self.mysql.schema}`.ads_facebook_page_group g
              LEFT JOIN `{self.mysql.schema}`.ads_apps_setting a ON a.id=g.app_id
              LEFT JOIN `{self.mysql.schema}`.ads_facebook_page_group_ins i
                ON i.group_id=g.id AND i.deleted_at=0 AND TRIM(i.page_id)<>''
             WHERE g.is_delete=0 AND g.type IN (0,1){owner_sql}
             GROUP BY g.id,g.user_id,g.type,g.name,g.app_id,a.name ORDER BY g.id
        """
        return [PageGroup(str(r.get("group_id") or ""), str(r.get("owner_user_id") or ""), int(r.get("group_type") or 0), str(r.get("group_name") or "")[:200], str(r.get("app_id") or ""), str(r.get("product") or "")[:128], int(r.get("total_pages") or 0), int(r.get("publishable_pages") or 0)) for r in self.mysql.select(sql, params)]

    def list_pages(self, group_ids: Sequence[str], *, is_admin: bool, owner_user_id: str) -> List[PageTarget]:
        ids = list(dict.fromkeys(str(item) for item in group_ids))
        if not ids or any(not re.fullmatch(r"[1-9][0-9]{0,30}", item) for item in ids):
            raise RepositoryError("invalid_request", "Page池ID无效", 400)
        if not is_admin and not re.fullmatch(r"[1-9][0-9]{0,30}", owner_user_id):
            raise RepositoryError("fb_auto_owner_mapping_missing", "当前账号未唯一映射到Page池负责人", 403)
        placeholders = ",".join("%s" for _ in ids)
        owner_sql = "" if is_admin else " AND CAST(g.user_id AS CHAR)=%s"
        params: List[Any] = list(ids) + ([] if is_admin else [owner_user_id])
        sql = f"""
            SELECT CAST(g.id AS CHAR) AS group_id,CAST(g.user_id AS CHAR) AS owner_user_id,
                   CAST(i.page_id AS CHAR) AS page_id,COALESCE(i.timezone,'') AS timezone,
                   LOWER(TRIM(COALESCE(i.language,''))) AS language,
                   COALESCE((SELECT MAX(TRIM(pn.page_name)) FROM `{self.mysql.schema}`.ads_facebook_page_post pn
                              WHERE pn.page_id=i.page_id AND pn.status<>1
                                AND TRIM(pn.page_access_token)<>'' AND TRIM(COALESCE(pn.page_name,''))<>''),'') AS page_name,
                   (SELECT COUNT(*) FROM `{self.mysql.schema}`.ads_facebook_page_post p
                     WHERE p.page_id=i.page_id AND p.status<>1 AND TRIM(p.page_access_token)<>'') AS eligible_token_count
              FROM `{self.mysql.schema}`.ads_facebook_page_group g
              JOIN `{self.mysql.schema}`.ads_facebook_page_group_ins i ON i.group_id=g.id
             WHERE g.is_delete=0 AND g.type IN (0,1) AND i.deleted_at=0 AND TRIM(i.page_id)<>''
               AND CAST(g.id AS CHAR) IN ({placeholders}){owner_sql}
             ORDER BY g.id,i.page_id
        """
        rows = self.mysql.select(sql, params)
        grouped: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            page_id = str(row.get("page_id") or "").strip()
            if not page_id:
                continue
            group_id = str(row.get("group_id") or "")
            item = grouped.setdefault(page_id, {"group_ids": [], "owner_user_id": str(row.get("owner_user_id") or ""), "timezone": str(row.get("timezone") or "")[:64], "language": str(row.get("language") or "")[:32], "tokens": int(row.get("eligible_token_count") or 0), "page_name": str(row.get("page_name") or page_id).strip()[:255]})
            if group_id not in item["group_ids"]:
                item["group_ids"].append(group_id)
        return [PageTarget(sorted(item["group_ids"], key=int)[0], tuple(sorted(item["group_ids"], key=int)), page_id, item["owner_user_id"], item["timezone"], item["language"], item["tokens"], item["page_name"]) for page_id, item in sorted(grouped.items())]

    def legacy_conflicts(self, group_ids: Sequence[str]) -> List[Dict[str, str]]:
        ids = list(dict.fromkeys(str(item) for item in group_ids))
        if not ids:
            return []
        placeholders = ",".join("%s" for _ in ids)
        sql = f"""
            SELECT DISTINCT CAST(q.id AS CHAR) AS queue_id,COALESCE(q.name,'') AS queue_name,
                   CAST(q.page_group_id AS CHAR) AS group_id,CAST(si.group_id AS CHAR) AS selected_group_id,
                   CAST(li.page_id AS CHAR) AS overlap_page_id,CAST(q.execute_switch AS CHAR) AS execute_switch
              FROM `{self.mysql.schema}`.ads_facebook_post_publish_queue q
              JOIN `{self.mysql.schema}`.ads_facebook_page_group lg
                ON lg.id=q.page_group_id AND lg.is_delete=0 AND lg.type IN (0,1)
              JOIN `{self.mysql.schema}`.ads_facebook_page_group_ins li
                ON li.group_id=lg.id AND li.deleted_at=0 AND TRIM(li.page_id)<>''
              JOIN `{self.mysql.schema}`.ads_facebook_page_group_ins si
                ON si.page_id=li.page_id AND si.deleted_at=0 AND TRIM(si.page_id)<>''
              JOIN `{self.mysql.schema}`.ads_facebook_page_group sg
                ON sg.id=si.group_id AND sg.is_delete=0 AND sg.type IN (0,1)
             WHERE q.execute_switch=1 AND CAST(si.group_id AS CHAR) IN ({placeholders})
             ORDER BY queue_id,overlap_page_id
        """
        return [{"queue_id": str(row.get("queue_id") or ""), "queue_name": str(row.get("queue_name") or "")[:200], "group_id": str(row.get("group_id") or ""), "selected_group_id": str(row.get("selected_group_id") or ""), "overlap_page_id": str(row.get("overlap_page_id") or ""), "status": "enabled"} for row in self.mysql.select(sql, ids)]

    def eligible_credentials(self, page_id: str) -> List[PageCredential]:
        # Page authorization status is mutable.  Keep this as a live query on
        # every execute/reconcile call instead of caching the planning snapshot.
        if not re.fullmatch(r"[1-9][0-9]{3,40}", str(page_id or "")):
            raise RepositoryError("fb_auto_page_id_invalid", "Page ID无效", 400)
        sql = f"""
            SELECT CAST(id AS CHAR) AS credential_id,CAST(page_id AS CHAR) AS page_id,CAST(fb_user_id AS CHAR) AS fb_user_id,page_access_token
              FROM `{self.mysql.schema}`.ads_facebook_page_post
             WHERE page_id=%s AND status<>1 AND TRIM(page_access_token)<>''
             ORDER BY fb_user_id
        """
        rows = self.mysql.select(sql, (str(page_id),))
        result, seen = [], set()
        for row in rows:
            token = str(row.get("page_access_token") or "")
            user = str(row.get("fb_user_id") or "")
            if token and user and token not in seen:
                seen.add(token)
                result.append(PageCredential(str(page_id), user, str(row.get("credential_id") or ""), token))
        return result


class MaterialRepository:
    """Select video candidates using the current X-auto source semantics."""

    def __init__(self, mysql: ReadOnlyMySQL, metric_store: Any | None = None, *, now_fn: Callable[[], datetime] = lambda: datetime.now(UTC), rng: random.Random | None = None, monotonic_fn: Callable[[], float] = time.monotonic, catalog_deadline_seconds: int = 600, catalog_max_pages: int = 100, metric_prefilter_min_content_ids: int = 100, metric_prefilter_batch_size: int = 200, candidate_limit: int = 5000):
        self.mysql, self.metric_store, self.now_fn, self.rng = mysql, metric_store, now_fn, rng or random.SystemRandom()
        self.monotonic_fn, self.catalog_deadline_seconds, self.catalog_max_pages = monotonic_fn, max(60, int(catalog_deadline_seconds)), max(1, int(catalog_max_pages))
        self.metric_prefilter_min_content_ids = max(1, int(metric_prefilter_min_content_ids))
        self.metric_prefilter_batch_size = max(1, min(int(metric_prefilter_batch_size), 500))
        self.candidate_limit = max(1, min(int(candidate_limit), 5000))

    @staticmethod
    def _in_range(value: Decimal | None, low: Any, high: Any) -> bool:
        if value is None:
            return low is None and high is None
        return (low is None or value >= Decimal(str(low))) and (high is None or value <= Decimal(str(high)))

    def candidate_snapshot(self, config: Mapping[str, Any]) -> CandidateSnapshot:
        if self.metric_store is None:
            raise RepositoryError("fb_auto_metric_cache_not_configured", "FB指标缓存尚未配置", 503)
        days = int(config["metric_window_days"])
        today = self.now_fn().astimezone(timezone(timedelta(hours=8))).date()
        start, end = today - timedelta(days=days), today - timedelta(days=1)
        dates = tuple((start + timedelta(days=offset)).isoformat() for offset in range(days))
        metric_window = self.metric_store.load_metric_window(
            product=str(config["metric_product"]), platform=int(config["metric_platform"]), dates=dates
        )
        language = str(config["language"])
        launch_days = int(config["drama_launch_window_days"])
        deploy_after = 0 if not launch_days else int((self.now_fn() - timedelta(days=launch_days)).timestamp())
        drama_rule, material_rule = config["drama_rule"], config["material_rule"]
        allowed_types = [str(item) for item in drama_rule.get("resource_type_v2") or []]
        type_sql = "" if not allowed_types else " AND CAST(d.resource_type_v2 AS CHAR) IN (" + ",".join("%s" for _ in allowed_types) + ")"
        keyset_sql = f"""
            SELECT /*+ MAX_EXECUTION_TIME(45000) */ CAST(s.id AS CHAR) material_id,TRIM(s.data_source_id) content_id,s.url media_url,
                   COALESCE(s.name,'') material_name,COALESCE(s.tag_name,'') material_tag,
                   LOWER(TRIM(s.language)) language,s.video_duration
              FROM `{self.mysql.schema}`.ads_custom_source s FORCE INDEX(PRIMARY)
             WHERE s.data_source=%s AND s.product=%s AND s.type=2 AND s.is_delete=0
               AND LOWER(TRIM(s.language))=%s AND s.video_duration>0
               AND EXISTS (SELECT 1 FROM `{self.mysql.schema}`.ads_drama_info d FORCE INDEX(ac)
                            WHERE d.content_id=s.data_source_id AND d.app_id=%s
                              AND d.release_status=1 AND d.deploy_time>%s AND d.deploy_time<=%s{type_sql})
               AND NOT EXISTS (SELECT 1 FROM `{self.mysql.blacklist_schema}`.ads_facebook_post_blacklist b
                                WHERE b.is_delete=0 AND b.type=1 AND b.content_id=s.data_source_id)
               AND NOT EXISTS (SELECT 1 FROM `{self.mysql.schema}`.ads_drama_info db FORCE INDEX(ac)
                                JOIN `{self.mysql.blacklist_schema}`.ads_facebook_post_blacklist b
                                  ON b.is_delete=0 AND b.type=0 AND b.content_id=db.series_code
                                WHERE db.content_id=s.data_source_id AND db.app_id=%s)
               AND s.video_duration>=%s AND s.video_duration<=%s
               AND s.id>%s
             ORDER BY s.id LIMIT %s
        """
        product, app_id = str(config["product"]), str(config["app_id"])
        allowed_type_set = set(allowed_types)
        uses_description = "{{desc}}" in str(config.get("message_template") or "")
        candidates: List[MaterialCandidate] = []
        seen_material_ids: set[str] = set()
        drama_key = "drama_spend" if drama_rule["sort_by"] == "spend" else "drama_roas"
        material_key = "material_spend" if material_rule["sort_by"] == "spend" else "material_roas"

        def compare(left: MaterialCandidate, right: MaterialCandidate) -> int:
            for key, direction in ((drama_key, drama_rule["sort_direction"]), (material_key, material_rule["sort_direction"])):
                left_value, right_value = getattr(left, key), getattr(right, key)
                if left_value is None or right_value is None:
                    if left_value is right_value: continue
                    return 1 if left_value is None else -1
                if left_value != right_value:
                    ordered = -1 if left_value < right_value else 1
                    return -ordered if direction == "desc" else ordered
            return (int(left.material_id) > int(right.material_id)) - (int(left.material_id) < int(right.material_id))

        deadline = self.monotonic_fn() + self.catalog_deadline_seconds

        def add_rows(rows: Sequence[Mapping[str, Any]], *, expected_content_ids: set[str] | None = None) -> None:
            if expected_content_ids is not None:
                rows = [row for row in rows if str(row.get("content_id") or "").strip() in expected_content_ids]
            content_ids = tuple(dict.fromkeys(str(row.get("content_id") or "").strip() for row in rows if str(row.get("content_id") or "").strip()))
            metadata: Dict[str, Mapping[str, Any]] = {}
            descriptions: Dict[str, str] = {}
            if content_ids:
                ids_sql = ",".join("%s" for _ in content_ids)
                metadata_type_sql = "" if not allowed_types else " AND CAST(d0.resource_type_v2 AS CHAR) IN (" + ",".join("%s" for _ in allowed_types) + ")"
                metadata_sql = f"""
                    SELECT TRIM(d.content_id) content_id,COALESCE(d.name,'') drama_name,
                           CAST(d.resource_type_v2 AS CHAR) resource_type_v2,COALESCE(d.series_code,'') series_code
                      FROM `{self.mysql.schema}`.ads_drama_info d
                      JOIN (SELECT d0.content_id,MAX(d0.id) id
                              FROM `{self.mysql.schema}`.ads_drama_info d0 FORCE INDEX(ac)
                             WHERE d0.app_id=%s AND d0.release_status=1
                               AND d0.deploy_time>%s AND d0.deploy_time<=%s{metadata_type_sql}
                               AND TRIM(d0.content_id) IN ({ids_sql})
                             GROUP BY d0.content_id) pick ON pick.id=d.id
                     ORDER BY d.content_id
                """
                end_epoch = int(self.now_fn().timestamp())
                metadata_rows = self.mysql.select(metadata_sql, (app_id, deploy_after, end_epoch, *allowed_types, *content_ids))
                metadata = {str(item.get("content_id") or "").strip(): item for item in metadata_rows}
                if uses_description:
                    description_sql = f"""
                        SELECT r.content_id,MAX(TRIM(r.`desc`)) drama_description,
                               COUNT(DISTINCT BINARY TRIM(r.`desc`)) description_count
                          FROM `{self.mysql.schema}`.ads_drama_resource r FORCE INDEX(content_id)
                         WHERE r.app_id=%s AND r.type=2 AND LOWER(TRIM(r.language))=%s
                           AND r.content_id IN ({ids_sql})
                           AND TRIM(COALESCE(r.`desc`,''))<>''
                         GROUP BY r.content_id
                         ORDER BY r.content_id
                    """
                    description_rows = self.mysql.select(description_sql, (app_id, language, *content_ids))
                    for item in description_rows:
                        content_id = str(item.get("content_id") or "").strip()
                        description = _description(item.get("drama_description"))
                        if content_id and description and int(item.get("description_count") or 0) == 1:
                            descriptions[content_id] = description
            for row in rows:
                raw_id = str(row.get("material_id") or "")
                if not re.fullmatch(r"[1-9][0-9]*", raw_id):
                    raise RepositoryError("fb_auto_catalog_page_invalid", "素材目录分页顺序无效")
                if raw_id in seen_material_ids:
                    continue
                content_id = str(row.get("content_id") or "").strip()
                url, duration = _https(row.get("media_url")), _decimal(row.get("video_duration"))
                material_totals = metric_window.by_material.get((content_id, raw_id))
                drama_totals = metric_window.by_drama.get(content_id)
                m_spend = material_totals.spend if material_totals else Decimal("0")
                m_roas = material_totals.roas if material_totals else None
                d_spend = drama_totals.spend if drama_totals else Decimal("0")
                d_roas = drama_totals.roas if drama_totals else None
                detail = metadata.get(content_id, row)
                resource_type = str(detail.get("resource_type_v2") or "")
                if not url or (allowed_type_set and resource_type not in allowed_type_set):
                    continue
                description = descriptions.get(content_id, "")
                if uses_description and not description:
                    continue
                if not Decimal(str(material_rule["duration_min_seconds"])) <= duration <= Decimal(str(material_rule["duration_max_seconds"])):
                    continue
                if not self._in_range(m_spend, material_rule["spend_min"], material_rule["spend_max"]) or not self._in_range(m_roas, material_rule["roas_min"], material_rule["roas_max"]):
                    continue
                if not self._in_range(d_spend, drama_rule["spend_min"], drama_rule["spend_max"]) or not self._in_range(d_roas, drama_rule["roas_min"], drama_rule["roas_max"]):
                    continue
                seen_material_ids.add(raw_id)
                candidates.append(MaterialCandidate(raw_id, content_id, url, str(row.get("material_name") or "")[:500], str(detail.get("drama_name") or "")[:500], language, duration, m_spend, m_roas, d_spend, d_roas, resource_type, description, str(row.get("material_tag") or "").strip()[:255]))
            candidates.sort(key=cmp_to_key(compare))
            del candidates[self.candidate_limit:]

        # The primary ordering is always drama-level.  For descending spend,
        # every positive-spend drama sorts before every zero-history drama; for
        # ROAS, every defined value sorts before an undefined value in either
        # direction.  If the indexed metric-drama scan alone produces the full
        # retained candidate limit, it is therefore an exact top-N proof and a
        # six-million-row PRIMARY keyset scan is unnecessary.  Otherwise the
        # original complete scan remains the fail-closed fallback.
        priority_content_ids: List[str] = []
        if drama_rule["sort_by"] == "spend" and drama_rule["sort_direction"] == "desc":
            priority_content_ids = [str(content_id) for content_id, totals in metric_window.by_drama.items() if totals.spend > 0]
        elif drama_rule["sort_by"] == "roas":
            priority_content_ids = [str(content_id) for content_id, totals in metric_window.by_drama.items() if totals.roas is not None]
        priority_content_ids.sort(key=lambda value: value.encode("utf-8"))
        if len(priority_content_ids) >= self.metric_prefilter_min_content_ids:
            for start_at in range(0, len(priority_content_ids), self.metric_prefilter_batch_size):
                if self.monotonic_fn() >= deadline:
                    raise RepositoryError("fb_auto_catalog_scan_timeout", "素材目录完整扫描超过整体安全时限，已停止选择", 409)
                batch = priority_content_ids[start_at:start_at + self.metric_prefilter_batch_size]
                ids_sql = ",".join("%s" for _ in batch)
                priority_sql = f"""
                    SELECT /*+ MAX_EXECUTION_TIME(45000) */ CAST(s.id AS CHAR) material_id,TRIM(s.data_source_id) content_id,s.url media_url,
                           COALESCE(s.name,'') material_name,COALESCE(s.tag_name,'') material_tag,
                           LOWER(TRIM(s.language)) language,s.video_duration
                      FROM `{self.mysql.schema}`.ads_custom_source s FORCE INDEX(idx_source_type_source_id)
                     WHERE s.data_source=%s AND s.data_source_id IN ({ids_sql})
                       AND s.product=%s AND s.type=2 AND s.is_delete=0
                       AND LOWER(TRIM(s.language))=%s AND s.video_duration>0
                       AND EXISTS (SELECT 1 FROM `{self.mysql.schema}`.ads_drama_info d FORCE INDEX(ac)
                                    WHERE d.content_id=s.data_source_id AND d.app_id=%s
                                      AND d.release_status=1 AND d.deploy_time>%s AND d.deploy_time<=%s{type_sql})
                       AND NOT EXISTS (SELECT 1 FROM `{self.mysql.blacklist_schema}`.ads_facebook_post_blacklist b
                                       WHERE b.is_delete=0 AND b.type=1 AND b.content_id=s.data_source_id)
                       AND NOT EXISTS (SELECT 1 FROM `{self.mysql.schema}`.ads_drama_info db FORCE INDEX(ac)
                                       JOIN `{self.mysql.blacklist_schema}`.ads_facebook_post_blacklist b
                                         ON b.is_delete=0 AND b.type=0 AND b.content_id=db.series_code
                                       WHERE db.content_id=s.data_source_id AND db.app_id=%s)
                       AND s.video_duration>=%s AND s.video_duration<=%s
                     ORDER BY s.id
                """
                end_epoch = int(self.now_fn().timestamp())
                params: tuple[Any, ...] = (int(config["material_data_source"]), *batch, product, language, app_id, deploy_after, end_epoch, *allowed_types, app_id, material_rule["duration_min_seconds"], material_rule["duration_max_seconds"])
                add_rows(self.mysql.select(priority_sql, params), expected_content_ids=set(batch))
            if len(candidates) >= self.candidate_limit:
                return CandidateSnapshot(tuple(candidates), tuple(metric_window.generation_ids), tuple(metric_window.dates))
            candidates.clear()
            seen_material_ids.clear()

        page_size, cursor_id, page_count = 1000, 0, 0
        while True:
            if self.monotonic_fn() >= deadline:
                raise RepositoryError("fb_auto_catalog_scan_timeout", "素材目录完整扫描超过整体安全时限，已停止选择", 409)
            end_epoch = int(self.now_fn().timestamp())
            params: tuple[Any, ...] = (int(config["material_data_source"]), product, language, app_id, deploy_after, end_epoch, *allowed_types, app_id, material_rule["duration_min_seconds"], material_rule["duration_max_seconds"], cursor_id, page_size)
            rows = self.mysql.select(keyset_sql, params)
            if not rows: break
            page_count += 1
            if page_count > self.catalog_max_pages:
                raise RepositoryError("fb_auto_catalog_scan_too_large", "素材目录超过安全分页上限，已停止选择", 409)
            for row in rows:
                raw_id = str(row.get("material_id") or "")
                if not re.fullmatch(r"[1-9][0-9]*", raw_id) or int(raw_id) <= cursor_id:
                    raise RepositoryError("fb_auto_catalog_page_invalid", "素材目录分页顺序无效")
                cursor_id = int(raw_id)
            add_rows(rows)
            if len(rows) < page_size: break
            if self.monotonic_fn() >= deadline:
                raise RepositoryError("fb_auto_catalog_scan_timeout", "素材目录完整扫描超过整体安全时限，已停止选择", 409)
        candidates.sort(key=cmp_to_key(compare))
        return CandidateSnapshot(tuple(candidates), tuple(metric_window.generation_ids), tuple(metric_window.dates))

    def candidates(self, config: Mapping[str, Any]) -> List[MaterialCandidate]:
        return list(self.candidate_snapshot(config).candidates)

    def choose(self, config: Mapping[str, Any], excluded_material_ids: Iterable[str]) -> MaterialCandidate | None:
        return self.choose_from(self.candidates(config), excluded_material_ids)

    def choose_from(self, candidates: Sequence[MaterialCandidate], excluded_material_ids: Iterable[str]) -> MaterialCandidate | None:
        blocked = {str(item) for item in excluded_material_ids}
        available = [item for item in candidates if item.material_id not in blocked]
        if not available:
            return None
        # Keep the configured metric order but randomize within the first 50 to avoid a global hot spot.
        return self.rng.choice(available[:50])


__all__ = ["CandidateSnapshot", "MaterialCandidate", "MaterialRepository", "PageCredential", "PageGroup", "PagePoolRepository", "PageTarget", "ReadOnlyMySQL", "RepositoryError"]
