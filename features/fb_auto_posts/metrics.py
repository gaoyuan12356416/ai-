"""Product-aware, fail-closed daily metric cache for FB automatic posts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Mapping, Sequence

from .repositories import ReadOnlyMySQL, RepositoryError


UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))


def metric_date(value: Any) -> str:
    text = str(value or "")
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise RepositoryError("fb_auto_metric_date_invalid", "指标日期无效", 400) from None
    return parsed.isoformat()


def nonnegative_decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        raise RepositoryError("fb_auto_metric_row_invalid", "指标缓存行无效") from None
    if not result.is_finite() or result < 0:
        raise RepositoryError("fb_auto_metric_row_invalid", "指标缓存行无效")
    return result


@dataclass(frozen=True)
class MetricTotals:
    spend: Decimal = Decimal("0")
    revenue: Decimal = Decimal("0")

    @property
    def roas(self) -> Decimal | None:
        return None if self.spend == 0 else self.revenue / self.spend * Decimal("100")


@dataclass(frozen=True)
class MetricWindow:
    generation_ids: tuple[int, ...]
    dates: tuple[str, ...]
    by_drama: Mapping[str, MetricTotals]
    by_material: Mapping[tuple[str, str], MetricTotals]


class MetricRefresher:
    """Streams one exact product/platform/day aggregation from the read replica."""

    def __init__(self, mysql: ReadOnlyMySQL, store: Any, *, product: str = "Dramawave", platform: int = 0):
        if product != "Dramawave" or platform != 0:
            raise ValueError("unsupported FB metric source mapping")
        self.mysql, self.store, self.product, self.platform = mysql, store, product, platform

    def refresh_day(self, value: Any, *, refreshed_at: datetime | None = None) -> Mapping[str, Any]:
        day = metric_date(value)
        now = refreshed_at or datetime.now(UTC)
        today_bj = now.astimezone(BEIJING).date()
        parsed = date.fromisoformat(day)
        if parsed >= today_bj or parsed < today_bj - timedelta(days=30):
            raise RepositoryError("fb_auto_metric_date_out_of_range", "只允许刷新最近30个完整北京自然日", 409)
        sql = f"""
            SELECT /*+ MAX_EXECUTION_TIME(120000) */
                   TRIM(s.data_source_id) AS content_id,
                   TRIM(s.resource_id) AS material_id,
                   CHAR_LENGTH(TRIM(s.resource_id)) AS material_id_digits,
                   SUM(COALESCE(s.spend,0)) AS spend,
                   SUM(COALESCE(s.af_revenue0,0)) AS af_revenue0
              FROM `{self.mysql.schema}`.ads_custom_source_insight s
             WHERE s.product=%s AND s.platform=%s AND s.dt=%s
               AND s.data_source_id IS NOT NULL AND TRIM(s.data_source_id)<>''
               AND s.resource_id REGEXP '^[1-9][0-9]*$'
             GROUP BY TRIM(s.data_source_id),TRIM(s.resource_id),CHAR_LENGTH(TRIM(s.resource_id))
             ORDER BY TRIM(s.data_source_id),CHAR_LENGTH(TRIM(s.resource_id)),TRIM(s.resource_id)
        """
        def normalized_rows():
            for raw in self.mysql.iter_select(sql, (self.product, self.platform, day)):
                content_id = str(raw.get("content_id") or "").strip()
                material_id = str(raw.get("material_id") or "").strip()
                if not content_id or not material_id.isdigit() or material_id == "0":
                    raise RepositoryError("fb_auto_metric_row_invalid", "指标缓存行身份无效")
                yield {
                    "content_id": content_id,
                    "material_id": material_id,
                    "spend": format(nonnegative_decimal(raw.get("spend")), "f"),
                    "af_revenue0": format(nonnegative_decimal(raw.get("af_revenue0")), "f"),
                }
        return self.store.record_metric_generation_streaming(
            platform=self.platform,
            metric_date=day,
            product=self.product,
            rows=normalized_rows(),
            refreshed_at_utc=now.astimezone(UTC).isoformat(timespec="seconds"),
        )


def checksum_rows(rows: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps([
            str(row["content_id"]), str(row["material_id"]),
            str(row["spend"]), str(row["af_revenue0"]),
        ], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


__all__ = ["MetricRefresher", "MetricTotals", "MetricWindow", "checksum_rows", "metric_date", "nonnegative_decimal"]
