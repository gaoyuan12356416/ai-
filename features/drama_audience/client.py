"""Read-only upstream client. Never retain episode/media payloads."""
import json
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .store import BY_NAME, UNKNOWN, language

URL = 'https://admin.mydramawave.com/material/list2'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def read_json(request, timeout=15):
    with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        body = response.read(8 * 1024 * 1024 + 1)
        if len(body) > 8 * 1024 * 1024:
            raise ValueError('response_too_large')
        data = json.loads(body)
        if not isinstance(data, dict) or data.get('code') != 200:
            raise ValueError('upstream_business_error')
        return data


def resolve(cid, data):
    if data.get('code') != 200:
        raise ValueError('upstream_business_error')
    payload = data.get('data') or {}
    # Dramawave Android is the canonical product row; other products may share
    # the requested ID. No resource-ID or title-based joining is permitted.
    rows = [r for r in payload.get('list', []) if r.get('id') == cid and r.get('app_id') == 'com.dramawave.app']
    if not rows:
        raise ValueError('exact_drama_not_found')
    if any(not isinstance(r.get('audience_labels'), list) or not isinstance(r.get('language'), list) for r in rows):
        raise ValueError('audience_contract_missing')
    sets = {tuple(sorted(set(r['audience_labels']))) for r in rows}
    raw = sorted({v for r in rows for v in r['audience_labels']})
    langs = sorted({language(v) for r in rows for v in r['language'] if v})
    if not langs:
        raise ValueError('language_missing')
    status = 'conflict' if len(sets) != 1 or len(raw) > 1 else 'unsupported' if raw and raw[0] not in BY_NAME else 'matched' if raw else 'unlabeled'
    return dict(content_id=cid, languages=langs, audience_labels=raw,
                audience=BY_NAME[raw[0]] if status == 'matched' else UNKNOWN,
                status=status, name=str(rows[0].get('name') or ''))


def fetch(cid):
    return resolve(cid, read_json(Request(URL+'?'+urlencode({'content_id': cid}), headers={'Accept': 'application/json'})))
