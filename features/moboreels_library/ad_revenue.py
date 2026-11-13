"""CDReader ad-revenue reads and decimal normalization; no scheduling or writes.

The provider documents one aggregate per source date and code. Its reporting
timezone is not specified, so source dates are retained without conversion.
"""

from copy import deepcopy
from datetime import date as calendar_date
from decimal import Decimal, InvalidOperation, localcontext

import requests


AD_REVENUE_PATH = "/api/story/getadlist"


class AdRevenueError(ValueError):
    """A safe error that contains neither provider rows nor credentials."""


def _source_date(value):
    if not isinstance(value, str):
        raise AdRevenueError("广告收益日期必须为 YYYY-MM-DD")
    try:
        parsed = calendar_date.fromisoformat(value)
    except ValueError:
        raise AdRevenueError("广告收益日期必须为有效的 YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise AdRevenueError("广告收益日期必须为 YYYY-MM-DD")
    return value


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise AdRevenueError("广告收益金额无效")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise AdRevenueError("广告收益金额无效") from None
    if not amount.is_finite():
        raise AdRevenueError("广告收益金额必须为有限数值")
    return amount


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return deepcopy(value)


def normalize_ad_revenue(raw, requested_date=None):
    """Normalize a source row, preserving fractional cents and unknown fields.

    ``amount_cents`` and ``amount_usd`` are decimal strings. ``raw_json`` is a
    JSON-compatible dict; decimal tokens are represented as strings to retain
    all source digits. No external link ID or inferred owner is manufactured.
    """
    if not isinstance(raw, dict):
        raise AdRevenueError("广告收益记录格式无效")
    ad_time = _source_date(raw.get("ad_time"))
    if requested_date is not None and ad_time != _source_date(requested_date):
        raise AdRevenueError("广告收益记录日期与请求日期不一致")
    project_type = raw.get("project_type")
    if type(project_type) not in (int, str) or project_type not in (2, "2"):
        raise AdRevenueError("广告收益记录不属于短剧业务")
    code = raw.get("code")
    if not isinstance(code, str) or not code or code != code.strip():
        raise AdRevenueError("广告收益口令缺失或格式无效")
    amount = _decimal(raw.get("amount"))
    # Division by 100 is exact even for values wider than Decimal's default
    # precision. Do not round fractional cents before converting to dollars.
    with localcontext() as context:
        context.prec = max(28, len(amount.as_tuple().digits) + 2)
        dollars = amount / Decimal(100)
    return {
        "ad_time": ad_time,
        "project_type": 2,
        "code": code,
        "amount_cents": format(amount, "f"),
        "amount_usd": format(dollars, "f"),
        "raw_json": _json_safe(raw),
    }


def get_ad_revenue_page(provider, date, page_index, page_size=100):
    """Read exactly one page using the existing signed provider transport.

    The common report coordinator owns retries, snapshots, complete pagination,
    duplicate-key checks and persistence. This call never invokes creation.
    """
    source_date = _source_date(date)
    if type(page_index) is not int or page_index < 1:
        raise AdRevenueError("广告收益页码必须为正整数")
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise AdRevenueError("广告收益每页条数必须为 1 至 100")
    payload = {
        "date": source_date, "project_type": 2,
        "page_index": page_index, "page_size": page_size,
    }
    try:
        response = provider._post(AD_REVENUE_PATH, payload)
    except requests.RequestException:
        raise AdRevenueError("广告收益接口暂时不可用") from None
    if response.status_code != 200:
        raise AdRevenueError("广告收益接口请求失败（HTTP %s）" % response.status_code)
    try:
        # requests passes parse_float through to json.loads. Preserve source
        # decimal text instead of first rounding it through an IEEE float.
        body = response.json(parse_float=str)
    except (TypeError, ValueError):
        raise AdRevenueError("广告收益接口返回无效 JSON") from None
    if not isinstance(body, dict) or body.get("code") != 200:
        raise AdRevenueError("广告收益接口拒绝请求")
    data = body.get("data")
    if not isinstance(data, dict):
        raise AdRevenueError("广告收益接口分页格式无效")
    total, rows = data.get("total"), data.get("list")
    if type(total) is not int or total < 0 or not isinstance(rows, list):
        raise AdRevenueError("广告收益接口分页格式无效")
    if len(rows) > page_size or len(rows) > total or any(not isinstance(row, dict) for row in rows):
        raise AdRevenueError("广告收益接口分页记录无效")
    return data
