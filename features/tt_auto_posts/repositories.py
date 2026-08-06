"""Read-only source repositories and daily metric-cache contracts.

This module deliberately contains no publishing code.  MySQL is used only as
an immutable reporting/catalog source and every statement issued here is a
single parameterized ``SELECT``.  The large insight table is read one complete
Beijing calendar day at a time by the metric refresh runner; selection reads
only activated daily generations from the local auto-post store.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
)


UTC = timezone.utc
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULT_SCHEMA = "kunlunads_dev"
DEFAULT_BLACKLIST_SCHEMA = "ads_setting"
DEFAULT_PRODUCT = "Dramawave"
DEFAULT_PLATFORM = 0
DEFAULT_APP_ID = 1479
DEFAULT_MATERIAL_DATA_SOURCE = 6
DEFAULT_METRIC_QUERY_TIMEOUT_MS = 30_000
MAX_STREAM_BATCH_SIZE = 5_000
MAX_WINDOW_DAYS = 30

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_MATERIAL_ID = re.compile(r"^[1-9][0-9]{0,18}$")
_LANGUAGE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class AutoPostRepositoryError(RuntimeError):
    """Stable fail-closed error raised by selection repositories."""

    def __init__(self, code: str, message: str, status: int = 503):
        self.code = str(code)
        self.status = int(status)
        super().__init__(str(message))


class SourceQueryError(AutoPostRepositoryError):
    """The read-only source could not be queried safely."""


class SourceDataError(AutoPostRepositoryError):
    """The source returned an ambiguous or invalid identity/value."""


class MetricWindowNotReady(AutoPostRepositoryError):
    """At least one required complete day has no active READY generation."""

    def __init__(self, missing_dates: Iterable[str]):
        values = tuple(sorted(set(str(value) for value in missing_dates)))
        self.missing_dates = values
        super().__init__(
            "tt_auto_metric_window_not_ready",
            "metric cache is not READY for: %s" % ",".join(values),
            503,
        )


def _identifier(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_IDENTIFIER.fullmatch(text):
        raise ValueError("%s is invalid" % label)
    return text


def _text(value: Any, label: str, *, limit: int = 256) -> str:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise SourceDataError(
                "tt_auto_source_data_invalid",
                "%s is not valid UTF-8" % label,
            ) from None
    text = str(value or "").strip()
    if not text or len(text) > int(limit) or "\x00" in text:
        raise SourceDataError(
            "tt_auto_source_data_invalid",
            "%s is invalid" % label,
        )
    return text


def canonical_language(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("_", "-")
    if len(text) > 32 or not _LANGUAGE.fullmatch(text):
        raise SourceDataError(
            "tt_auto_language_invalid",
            "drama language is invalid",
            409,
        )
    return text


def canonical_material_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _MATERIAL_ID.fullmatch(text):
        raise SourceDataError(
            "tt_auto_material_id_invalid",
            "material ID is invalid",
            409,
        )
    return text


def decimal_value(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value if value is not None else "0"))
    except (InvalidOperation, TypeError, ValueError):
        raise SourceDataError(
            "tt_auto_metric_value_invalid",
            "%s is invalid" % label,
        ) from None
    if not result.is_finite() or result < 0:
        raise SourceDataError(
            "tt_auto_metric_value_invalid",
            "%s is invalid" % label,
        )
    return result


def normalize_metric_date(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        try:
            parsed = datetime.strptime(str(value or ""), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            raise ValueError("metric date must be YYYY-MM-DD") from None
    return parsed.isoformat()


def complete_beijing_dates(
    now: Optional[datetime] = None,
    days: int = 7,
) -> Tuple[str, ...]:
    """Return oldest-to-newest complete Beijing dates, excluding today."""

    if isinstance(days, bool):
        raise ValueError("metric window days is invalid")
    try:
        count = int(days)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("metric window days is invalid") from None
    if count < 1 or count > MAX_WINDOW_DAYS:
        raise ValueError("metric window days must be between 1 and 30")
    current = now or datetime.now(UTC)
    if not isinstance(current, datetime):
        raise TypeError("now must be a datetime")
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    yesterday = current.astimezone(BEIJING_TZ).date() - timedelta(days=1)
    first = yesterday - timedelta(days=count - 1)
    return tuple((first + timedelta(days=index)).isoformat() for index in range(count))


def utc_iso(value: Optional[datetime] = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class BlacklistSnapshot:
    drama_series_codes: FrozenSet[str]
    material_data_source_ids: FrozenSet[str]
    loaded_at_utc: str
    source_row_count: int
    sha256: str


@dataclass(frozen=True)
class DramaSourceRow:
    source_row_id: str
    content_id: str
    series_code: str
    language: str
    resource_type_v2: str
    deploy_time: int
    app_id: int
    release_status: int
    name: str = ""
    app: str = ""


@dataclass(frozen=True)
class MaterialSourceRow:
    material_id: str
    content_id: str
    language: str
    product: str
    material_type: int
    is_delete: int
    media_url: str
    material_name: str
    video_duration: Decimal
    data_source: int
    tag_name: str = ""


@dataclass(frozen=True)
class DailyMetricRow:
    metric_date: str
    platform: int
    content_id: str
    material_id: str
    spend: Decimal
    af_revenue0: Decimal

    def as_store_mapping(self) -> Dict[str, Any]:
        return {
            "metric_date": self.metric_date,
            "platform": self.platform,
            "content_id": self.content_id,
            "material_id": self.material_id,
            "spend": format(self.spend, "f"),
            "af_revenue0": format(self.af_revenue0, "f"),
        }


@dataclass(frozen=True)
class MetricTotals:
    spend: Decimal = Decimal("0")
    af_revenue0: Decimal = Decimal("0")

    @property
    def d0_roas(self) -> Optional[Decimal]:
        if self.spend == 0:
            return None
        return self.af_revenue0 / self.spend * Decimal("100")

    def plus(self, spend: Decimal, af_revenue0: Decimal) -> "MetricTotals":
        return MetricTotals(self.spend + spend, self.af_revenue0 + af_revenue0)


@dataclass(frozen=True)
class MetricWindowSnapshot:
    platform: int
    metric_dates: Tuple[str, ...]
    by_drama: Mapping[str, MetricTotals]
    by_material: Mapping[Tuple[str, str], MetricTotals]

    def drama(self, content_id: Any) -> MetricTotals:
        return self.by_drama.get(str(content_id or "").strip(), MetricTotals())

    def material(self, content_id: Any, material_id: Any) -> MetricTotals:
        key = (str(content_id or "").strip(), str(material_id or "").strip())
        return self.by_material.get(key, MetricTotals())


class MetricCacheStore(Protocol):
    """Small storage contract implemented by ``TTPostAutoStore``.

    A generation is written completely before activation.  Readers can only
    see rows belonging to the active READY generation for each requested day.
    """

    def ready_metric_dates(
        self,
        platform: int,
        dates: Sequence[str],
        *,
        product: Optional[str] = None,
    ) -> Iterable[str]:
        ...

    def iter_ready_metric_rows(
        self,
        platform: int,
        dates: Sequence[str],
        content_ids: Optional[Sequence[str]] = None,
        *,
        product: Optional[str] = None,
    ) -> Iterable[Mapping[str, Any]]:
        ...

    def record_metric_generation(
        self,
        *,
        platform: int,
        metric_date: str,
        product: str,
        rows: Iterable[Mapping[str, Any]],
        refreshed_at_utc: str,
    ) -> Mapping[str, Any]:
        ...

    def activate_metric_generation(
        self,
        generation_id: int,
    ) -> Mapping[str, Any]:
        ...


class ReadOnlyMySQLRepository:
    """Bounded, streaming Dramawave reads from the fixed replica schemas."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        schema: str = DEFAULT_SCHEMA,
        blacklist_schema: str = DEFAULT_BLACKLIST_SCHEMA,
        product: str = DEFAULT_PRODUCT,
        app_id: int = DEFAULT_APP_ID,
        material_data_source: int = DEFAULT_MATERIAL_DATA_SOURCE,
        stream_batch_size: int = 1_000,
        metric_query_timeout_ms: int = DEFAULT_METRIC_QUERY_TIMEOUT_MS,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if not callable(connection_factory):
            raise ValueError("connection_factory must be callable")
        self.connection_factory = connection_factory
        self.schema = _identifier(schema, "source schema")
        self.blacklist_schema = _identifier(
            blacklist_schema,
            "blacklist schema",
        )
        self.product = _text(product, "product", limit=64)
        self.app_id = int(app_id)
        self.material_data_source = int(material_data_source)
        self.stream_batch_size = int(stream_batch_size)
        self.metric_query_timeout_ms = int(metric_query_timeout_ms)
        self.now_fn = now_fn
        if (
            self.product != DEFAULT_PRODUCT
            or self.app_id != DEFAULT_APP_ID
            or self.material_data_source != DEFAULT_MATERIAL_DATA_SOURCE
            or self.stream_batch_size < 1
            or self.stream_batch_size > MAX_STREAM_BATCH_SIZE
            or self.metric_query_timeout_ms < 1
            or self.metric_query_timeout_ms > 120_000
        ):
            raise ValueError("read-only repository limits are invalid")

    @staticmethod
    def _close(value: Any) -> None:
        close = getattr(value, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _streaming_cursor(connection: Any) -> Any:
        """Prefer PyMySQL's server-side dictionary cursor for bounded memory."""

        try:
            import pymysql
        except ImportError:
            return connection.cursor()
        try:
            return connection.cursor(pymysql.cursors.SSDictCursor)
        except TypeError:
            # Small offline fakes and non-PyMySQL read-only adapters may not
            # accept a cursor-class argument.
            return connection.cursor()

    def _stream_select(
        self,
        sql: str,
        params: Sequence[Any],
    ) -> Iterator[Dict[str, Any]]:
        statement = str(sql or "").lstrip()
        if not re.match(r"(?is)^SELECT\b", statement):
            raise SourceQueryError(
                "tt_auto_read_only_query_denied",
                "source repository attempted a non-SELECT statement",
                500,
            )
        connection = None
        cursor = None
        try:
            connection = self.connection_factory()
            cursor = self._streaming_cursor(connection)
            cursor.execute(sql, tuple(params))
            fetchmany = getattr(cursor, "fetchmany", None)
            if callable(fetchmany):
                while True:
                    batch = fetchmany(self.stream_batch_size)
                    if not batch:
                        break
                    for raw in batch:
                        if not isinstance(raw, Mapping):
                            raise SourceDataError(
                                "tt_auto_source_row_invalid",
                                "source query returned a non-object row",
                            )
                        yield dict(raw)
            else:
                rows = cursor.fetchall()
                for raw in rows:
                    if not isinstance(raw, Mapping):
                        raise SourceDataError(
                            "tt_auto_source_row_invalid",
                            "source query returned a non-object row",
                        )
                    yield dict(raw)
        except AutoPostRepositoryError:
            raise
        except Exception as exc:
            raise SourceQueryError(
                "tt_auto_source_query_failed",
                "read-only source query failed: %s" % type(exc).__name__,
            ) from None
        finally:
            self._close(cursor)
            self._close(connection)

    def blacklist_snapshot(self) -> BlacklistSnapshot:
        sql = """
            SELECT type,content_id
              FROM `{schema}`.ads_facebook_post_blacklist
             WHERE is_delete=%s
             ORDER BY type,content_id,id
        """.format(schema=self.blacklist_schema)
        drama: Set[str] = set()
        material: Set[str] = set()
        canonical_rows: List[Tuple[int, str]] = []
        for row in self._stream_select(sql, (0,)):
            try:
                row_type = int(row.get("type"))
            except (TypeError, ValueError, OverflowError):
                raise SourceDataError(
                    "tt_auto_blacklist_row_invalid",
                    "active blacklist type is invalid",
                ) from None
            identity = _text(
                row.get("content_id"),
                "blacklist content_id",
                limit=128,
            )
            if row_type == 0:
                drama.add(identity)
            elif row_type == 1:
                material.add(identity)
            else:
                raise SourceDataError(
                    "tt_auto_blacklist_row_invalid",
                    "active blacklist type is unsupported",
                )
            canonical_rows.append((row_type, identity))
        digest = hashlib.sha256(
            json.dumps(
                canonical_rows,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return BlacklistSnapshot(
            drama_series_codes=frozenset(drama),
            material_data_source_ids=frozenset(material),
            loaded_at_utc=utc_iso(self.now_fn()),
            source_row_count=len(canonical_rows),
            sha256=digest,
        )

    def list_drama_rows(
        self,
        *,
        language: str,
        now_epoch: int,
        deploy_since_epoch: Optional[int] = None,
        resource_types: Sequence[str] = (),
    ) -> List[DramaSourceRow]:
        normalized_language = canonical_language(language)
        normalized_types = tuple(
            dict.fromkeys(_text(value, "resource type", limit=64) for value in resource_types)
        )
        predicates = [
            "i.app_id=%s",
            "i.release_status=%s",
            "LOWER(TRIM(i.language))=LOWER(%s)",
            "i.deploy_time>%s",
            "i.deploy_time<=%s",
        ]
        params: List[Any] = [
            self.app_id,
            1,
            normalized_language,
            0,
            int(now_epoch),
        ]
        if deploy_since_epoch is not None:
            predicates.append("i.deploy_time>=%s")
            params.append(int(deploy_since_epoch))
        if normalized_types:
            predicates.append(
                "CAST(i.resource_type_v2 AS CHAR) IN (%s)"
                % ",".join("%s" for _ in normalized_types)
            )
            params.extend(normalized_types)
        sql = """
            SELECT CAST(i.id AS CHAR) AS source_row_id,
                   i.content_id,i.series_code,i.language,
                   CAST(i.resource_type_v2 AS CHAR) AS resource_type_v2,
                   i.deploy_time,i.app_id,i.release_status,
                   COALESCE(i.name,'') AS name,
                   COALESCE(i.app,'') AS app
              FROM `{schema}`.ads_drama_info i
             WHERE {where}
             ORDER BY i.content_id,i.app,i.id
        """.format(schema=self.schema, where=" AND ".join(predicates))
        rows: List[DramaSourceRow] = []
        for row in self._stream_select(sql, params):
            try:
                deploy_time = int(row.get("deploy_time"))
                app_id = int(row.get("app_id"))
                release_status = int(row.get("release_status"))
            except (TypeError, ValueError, OverflowError):
                raise SourceDataError(
                    "tt_auto_drama_row_invalid",
                    "drama delivery metadata is invalid",
                ) from None
            rows.append(
                DramaSourceRow(
                    source_row_id=_text(
                        row.get("source_row_id"),
                        "drama source row ID",
                        limit=64,
                    ),
                    content_id=_text(row.get("content_id"), "content_id", limit=128),
                    series_code=_text(row.get("series_code"), "series_code", limit=128),
                    language=canonical_language(row.get("language")),
                    resource_type_v2=_text(
                        row.get("resource_type_v2"),
                        "resource_type_v2",
                        limit=64,
                    ),
                    deploy_time=deploy_time,
                    app_id=app_id,
                    release_status=release_status,
                    name=str(row.get("name") or "").strip()[:500],
                    app=str(row.get("app") or "").strip()[:128],
                )
            )
        return rows

    def list_material_rows(
        self,
        *,
        content_id: str,
        language: str,
    ) -> List[MaterialSourceRow]:
        normalized_content_id = _text(content_id, "content_id", limit=128)
        normalized_language = canonical_language(language)
        sql = """
            SELECT CAST(s.id AS CHAR) AS material_id,
                   s.data_source_id AS content_id,
                   s.language,s.product,s.type AS material_type,
                   s.is_delete,s.url AS media_url,
                   COALESCE(s.name,'') AS material_name,
                   s.video_duration,s.data_source,
                   COALESCE(s.tag_name,'') AS tag_name
              FROM `{schema}`.ads_custom_source s
                   FORCE INDEX (idx_source_type_source_id)
             WHERE s.data_source=%s
               AND s.data_source_id=%s
               AND s.product=%s
               AND s.type=%s
               AND s.is_delete=%s
               AND LOWER(TRIM(s.language))=LOWER(%s)
               AND s.video_duration>%s
               AND s.video_duration<=%s
             ORDER BY s.id
        """.format(schema=self.schema)
        params = (
            self.material_data_source,
            normalized_content_id,
            self.product,
            2,
            0,
            normalized_language,
            0,
            3600,
        )
        rows: List[MaterialSourceRow] = []
        for row in self._stream_select(sql, params):
            try:
                material_type = int(row.get("material_type"))
                is_delete = int(row.get("is_delete"))
                data_source = int(row.get("data_source"))
            except (TypeError, ValueError, OverflowError):
                raise SourceDataError(
                    "tt_auto_material_row_invalid",
                    "material metadata is invalid",
                ) from None
            rows.append(
                MaterialSourceRow(
                    material_id=canonical_material_id(row.get("material_id")),
                    content_id=_text(row.get("content_id"), "content_id", limit=128),
                    language=canonical_language(row.get("language")),
                    product=_text(row.get("product"), "product", limit=64),
                    material_type=material_type,
                    is_delete=is_delete,
                    media_url=_text(row.get("media_url"), "material URL", limit=4096),
                    material_name=str(row.get("material_name") or "").strip()[:500],
                    video_duration=decimal_value(
                        row.get("video_duration"),
                        "video_duration",
                    ),
                    data_source=data_source,
                    tag_name=str(row.get("tag_name") or "").strip()[:255],
                )
            )
        return rows

    def stream_metric_day(
        self,
        metric_date: Any,
        *,
        platform: int = DEFAULT_PLATFORM,
    ) -> Iterator[DailyMetricRow]:
        normalized_date = normalize_metric_date(metric_date)
        if isinstance(platform, bool):
            raise ValueError("platform is invalid")
        normalized_platform = int(platform)
        if normalized_platform < 0 or normalized_platform > 255:
            raise ValueError("platform is invalid")
        sql = """
            SELECT /*+ MAX_EXECUTION_TIME({timeout_ms}) */
                   TRIM(s.data_source_id) AS content_id,
                   TRIM(s.resource_id) AS material_id,
                   SUM(COALESCE(s.spend,0)) AS spend,
                   SUM(COALESCE(s.af_revenue0,0)) AS af_revenue0
              FROM `{schema}`.ads_custom_source_insight s
             WHERE s.product=%s
               AND s.platform=%s
               AND s.dt=%s
               AND s.data_source_id IS NOT NULL
               AND TRIM(s.data_source_id)<>''
               AND s.resource_id REGEXP '^[1-9][0-9]*$'
             GROUP BY TRIM(s.data_source_id),TRIM(s.resource_id)
             ORDER BY TRIM(s.data_source_id),
                      TRIM(s.resource_id)
        """.format(
            timeout_ms=self.metric_query_timeout_ms,
            schema=self.schema,
        )
        previous_key: Optional[Tuple[str, str]] = None
        for row in self._stream_select(
            sql,
            (self.product, normalized_platform, normalized_date),
        ):
            content_id = _text(row.get("content_id"), "content_id", limit=128)
            material_id = canonical_material_id(row.get("material_id"))
            key = (content_id, material_id)
            if key == previous_key:
                raise SourceDataError(
                    "tt_auto_metric_identity_ambiguous",
                    "daily metric query returned a duplicate identity",
                )
            previous_key = key
            yield DailyMetricRow(
                metric_date=normalized_date,
                platform=normalized_platform,
                content_id=content_id,
                material_id=material_id,
                spend=decimal_value(row.get("spend"), "spend"),
                af_revenue0=decimal_value(
                    row.get("af_revenue0"),
                    "af_revenue0",
                ),
            )


class MetricWindowRepository:
    """Read exact ratio-of-sums inputs from activated daily generations."""

    def __init__(
        self,
        store: MetricCacheStore,
        *,
        product: str = DEFAULT_PRODUCT,
    ):
        self.store = store
        self.product = _text(product, "product", limit=64)
        if self.product != DEFAULT_PRODUCT:
            raise ValueError("metric product must be Dramawave")

    def load(
        self,
        *,
        platform: int,
        metric_dates: Sequence[str],
        content_ids: Sequence[str],
    ) -> MetricWindowSnapshot:
        dates = tuple(dict.fromkeys(normalize_metric_date(value) for value in metric_dates))
        if not dates:
            raise ValueError("metric_dates cannot be empty")
        if isinstance(platform, bool):
            raise ValueError("platform is invalid")
        normalized_platform = int(platform)
        if normalized_platform < 0 or normalized_platform > 255:
            raise ValueError("platform is invalid")
        normalized_content_ids = tuple(
            dict.fromkeys(_text(value, "content_id", limit=128) for value in content_ids)
        )
        ready = {
            normalize_metric_date(value)
            for value in self.store.ready_metric_dates(
                normalized_platform,
                dates,
                product=self.product,
            )
        }
        missing = set(dates).difference(ready)
        if missing:
            raise MetricWindowNotReady(missing)

        by_material: Dict[Tuple[str, str], MetricTotals] = {}
        by_drama: Dict[str, MetricTotals] = {}
        seen_daily_identities: Set[Tuple[str, str, str]] = set()
        allowed_dates = set(dates)
        allowed_content_ids = set(normalized_content_ids)
        for row in self.store.iter_ready_metric_rows(
            normalized_platform,
            dates,
            normalized_content_ids,
            product=self.product,
        ):
            if not isinstance(row, Mapping):
                raise SourceDataError(
                    "tt_auto_metric_cache_row_invalid",
                    "metric cache row is not an object",
                )
            row_date = normalize_metric_date(row.get("metric_date"))
            try:
                row_platform = int(row.get("platform"))
            except (TypeError, ValueError, OverflowError):
                raise SourceDataError(
                    "tt_auto_metric_cache_row_invalid",
                    "metric cache platform is invalid",
                ) from None
            content_id = _text(row.get("content_id"), "content_id", limit=128)
            material_id = canonical_material_id(row.get("material_id"))
            row_product = row.get("product")
            if (
                row_date not in allowed_dates
                or row_platform != normalized_platform
                or content_id not in allowed_content_ids
                or (
                    row_product is not None
                    and _text(row_product, "product", limit=64) != self.product
                )
            ):
                raise SourceDataError(
                    "tt_auto_metric_cache_scope_mismatch",
                    "metric cache row is outside the requested scope",
                )
            spend = decimal_value(row.get("spend"), "spend")
            revenue = decimal_value(row.get("af_revenue0"), "af_revenue0")
            daily_identity = (row_date, content_id, material_id)
            if daily_identity in seen_daily_identities:
                raise SourceDataError(
                    "tt_auto_metric_cache_identity_ambiguous",
                    "metric cache returned a duplicate daily identity",
                )
            seen_daily_identities.add(daily_identity)
            material_key = (content_id, material_id)
            by_material[material_key] = by_material.get(
                material_key,
                MetricTotals(),
            ).plus(spend, revenue)
            by_drama[content_id] = by_drama.get(
                content_id,
                MetricTotals(),
            ).plus(spend, revenue)
        return MetricWindowSnapshot(
            platform=normalized_platform,
            metric_dates=dates,
            by_drama=by_drama,
            by_material=by_material,
        )


def _store_result_mapping(value: Any, label: str) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        converted = as_dict()
        if isinstance(converted, Mapping):
            return dict(converted)
    raise SourceDataError(
        "tt_auto_metric_generation_invalid",
        "%s is invalid" % label,
        500,
    )


def refresh_metric_day(
    source: ReadOnlyMySQLRepository,
    store: MetricCacheStore,
    metric_date: Any,
    *,
    platform: int = DEFAULT_PLATFORM,
    refreshed_at: Optional[datetime] = None,
) -> Mapping[str, Any]:
    """Stream one day into an inactive generation, then atomically activate it."""

    normalized_date = normalize_metric_date(metric_date)
    if isinstance(platform, bool):
        raise ValueError("platform is invalid")
    normalized_platform = int(platform)
    if normalized_platform < 0 or normalized_platform > 255:
        raise ValueError("platform is invalid")

    def rows() -> Iterator[Mapping[str, Any]]:
        for item in source.stream_metric_day(
            normalized_date,
            platform=normalized_platform,
        ):
            yield item.as_store_mapping()

    generation = _store_result_mapping(
        store.record_metric_generation(
            platform=normalized_platform,
            metric_date=normalized_date,
            product=source.product,
            rows=rows(),
            refreshed_at_utc=utc_iso(refreshed_at or source.now_fn()),
        ),
        "metric generation",
    )
    try:
        generation_id = int(generation.get("id") or generation.get("generation_id"))
    except (TypeError, ValueError, OverflowError):
        raise SourceDataError(
            "tt_auto_metric_generation_invalid",
            "metric generation ID is invalid",
            500,
        ) from None
    if generation_id <= 0:
        raise SourceDataError(
            "tt_auto_metric_generation_invalid",
            "metric generation ID is invalid",
            500,
        )
    return _store_result_mapping(
        store.activate_metric_generation(generation_id),
        "activated metric generation",
    )
