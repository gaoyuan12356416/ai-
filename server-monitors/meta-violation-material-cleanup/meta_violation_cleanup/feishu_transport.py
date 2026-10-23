"""Credential-free Feishu transport derived from the configured feishu-bot skill.

The caller injects the private app configuration in memory and owns the durable
notification claim. This module has no command-line entry point.
"""
import json
from urllib import parse, request

APP_ID = ''
APP_SECRET = ''
BASE_URL = 'https://open.feishu.cn/open-apis'


def post_json(url, payload, token=None):
    headers = {'Content-Type': 'application/json; charset=utf-8'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                          headers=headers, method='POST')
    try:
        with request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception:
        raise RuntimeError('feishu_transport_unavailable') from None


def get_tenant_access_token():
    result = post_json(BASE_URL + '/auth/v3/tenant_access_token/internal',
                       {'app_id': APP_ID, 'app_secret': APP_SECRET})
    if result.get('code') != 0 or not result.get('tenant_access_token'):
        raise RuntimeError('feishu_app_authentication_failed')
    return result['tenant_access_token']


def send_text(receive_id_type, receive_id, text):
    token = get_tenant_access_token()
    url = BASE_URL + '/im/v1/messages?' + parse.urlencode({'receive_id_type': receive_id_type})
    payload = {'receive_id': receive_id, 'msg_type': 'text',
               'content': json.dumps({'text': text}, ensure_ascii=False)}
    return post_json(url, payload, token=token)
