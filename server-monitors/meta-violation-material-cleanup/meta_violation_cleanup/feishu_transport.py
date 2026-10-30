"""Credential-free Feishu transport derived from the configured feishu-bot skill.

The caller injects the private app configuration in memory and owns the durable
notification claim. This module has no command-line entry point.
"""
import json
from urllib import error, parse, request

APP_ID = ''
APP_SECRET = ''
BASE_URL = 'https://open.feishu.cn/open-apis'


class FeishuRejected(RuntimeError):
    """An explicit 4xx rejection; retain only bounded numeric fields."""
    def __init__(self, code, http_status):
        self.code = code
        self.http_status = http_status
        super().__init__('feishu_request_rejected')


def encode_payload(payload):
    return json.dumps(payload, ensure_ascii=False).encode('utf-8')


def text_payload(receive_id, text, uuid=None):
    payload = {'receive_id': receive_id, 'msg_type': 'text',
               'content': json.dumps({'text': text}, ensure_ascii=False)}
    if uuid is not None:
        payload['uuid'] = uuid
    return payload


def post_json(url, payload, token=None):
    headers = {'Content-Type': 'application/json; charset=utf-8'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = request.Request(url, data=encode_payload(payload),
                          headers=headers, method='POST')
    try:
        with request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))
    except error.HTTPError as exc:
        status = exc.code
        code = None
        try:
            raw = exc.read(16385)
            result = json.loads(raw.decode('utf-8')) if len(raw) <= 16384 else None
            code = result.get('code') if isinstance(result, dict) else None
        except Exception:
            pass
        finally:
            exc.close()
        if (status in (400, 401, 403, 404, 405, 413, 422, 429)
                and type(code) is int and 0 < code < 1000000000):
            raise FeishuRejected(code, status) from None
        raise RuntimeError('feishu_transport_unavailable') from None
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
    return post_json(url, text_payload(receive_id, text), token=token)


def send_card(receive_id_type, receive_id, card):
    token = get_tenant_access_token()
    url = BASE_URL + '/im/v1/messages?' + parse.urlencode({'receive_id_type': receive_id_type})
    return post_json(url, {'receive_id': receive_id, 'msg_type': 'interactive',
                           'content': json.dumps(card, ensure_ascii=False)}, token=token)
