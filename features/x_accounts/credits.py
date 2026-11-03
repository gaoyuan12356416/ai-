"""Read the running production App's billing balance without token mutation."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_balance(token):
    request = urllib.request.Request('https://api.x.com/2/usage/credits', headers={
        'Authorization': 'Bearer ' + token, 'User-Agent': 'yingliang-x-account-list/1.0'})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('balance response too large')
    payload = json.loads(raw, parse_float=Decimal)
    if not isinstance(payload, dict) or payload.get('errors') or not isinstance(payload.get('data'), dict):
        raise ValueError('balance unavailable')
    result = {}
    for key in ('total_balance', 'prepaid_balance', 'free_balance'):
        raw = payload['data'].get(key)
        if raw is None or isinstance(raw, bool):
            raise ValueError('invalid balance')
        try:
            number = Decimal(str(raw))
        except InvalidOperation:
            raise ValueError('invalid balance') from None
        if not number.is_finite() or (key != 'prepaid_balance' and number < 0):
            raise ValueError('invalid balance')
        result[key] = str(number)
    expected = max(Decimal('0'), Decimal(result['prepaid_balance']) + Decimal(result['free_balance']))
    if abs(Decimal(result['total_balance']) - expected) > Decimal('0.000001'):
        raise ValueError('inconsistent balance')
    return result


def fetch_current_credits(billing_token, app_name):
    result = {"app_name": app_name, "currency": "USD", "status": "unavailable",
              "source": "https://api.x.com/2/usage/credits"}
    if not billing_token:
        result["message"] = "当前 API 未配置余额查询凭据"
        return result
    try:
        balance = fetch_balance(billing_token)
    except Exception:
        # Neither an upstream body nor credentials belong in a browser DTO.
        result["message"] = "余额查询失败，请重试；当前余额未知"
        return result
    result.update(balance, status="live", checked_at_utc=datetime.now(timezone.utc).isoformat())
    return result
