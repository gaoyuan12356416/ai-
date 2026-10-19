"""Secret-free transport excerpt from the personal feishu-bot skill.

Original: feishu-bot/scripts/feishu_bot.py. Credentials supplied at runtime.
The monitor deliberately uses the helper's deterministic transport, with a
persisted message UUID and a private-recipient guard in its caller.
"""
import json
from urllib import error, request

APP_ID = ''
APP_SECRET = ''
BASE_URL = 'https://open.feishu.cn/open-apis'


def post_json(url, payload, token=None):
    headers = {'Content-Type': 'application/json; charset=utf-8'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = request.Request(url, data=data, headers=headers, method='POST')
    try:
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except error.HTTPError as exc:
        raise RuntimeError('Feishu HTTP ' + str(exc.code)) from None


def get_tenant_access_token():
    data = post_json(BASE_URL + '/auth/v3/tenant_access_token/internal',
                     {'app_id': APP_ID, 'app_secret': APP_SECRET})
    if data.get('code') != 0:
        raise RuntimeError('Feishu token request failed')
    return data['tenant_access_token']
