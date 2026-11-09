"""Channel-list ACL, local snapshot reads and bounded detail responses."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import parse_qs

DEFAULT_ROOT = '/mnt/data-disk/x-channel-tag-revenue'
_lock = threading.Lock()
_cache = {}


def read_snapshot(root):
    root = Path(root)
    pointer = json.loads((root / 'current.json').read_text(encoding='utf-8'))
    path = (root / pointer['file']).resolve()
    if path.parent != (root / 'snapshots').resolve() or path.suffix != '.json':
        raise ValueError('invalid_snapshot')
    with _lock:
        data = _cache.get(str(path))
    if data is None:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data['meta']['version'] != pointer['version']:
            raise ValueError('snapshot_version_mismatch')
        with _lock:
            _cache.clear()
            _cache[str(path)] = data
    meta = dict(data['meta'])
    age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(meta['source_generated_at'])).total_seconds())
    meta.update(stale=age > 7200, age_seconds=round(age))
    try:
        meta['refresh_failed'] = json.loads((root / 'status.json').read_text()).get('status') == 'error'
    except (OSError, ValueError):
        meta['refresh_failed'] = False
    return data, meta


def empty_account():
    return dict(status='no_history', revenue_cents=0, unknown_revenue_cents=0, known_share_bp=None,
                published_count=0, drama_count=0, tags=[], types=[], combinations=[], audience_basis_counts={})


def tree_summary(nodes):
    return [{k: (tree_summary(v) if k=='children' else v) for k,v in row.items() if k!='dramas'} for row in nodes]


def response(data, meta, accounts, account_id=None, tag=None, path=None):
    allowed = {str(row['id']): row for row in accounts}
    if account_id is not None:
        if account_id not in allowed:
            return 404, {'message': '频道不存在或无权查看'}
        account = allowed[account_id]
        portrait = data['accounts'].get(str(account.get('x_user_id')), empty_account())
        result = dict(portrait)
        if path is not None:
            nodes=portrait.get('types',[])
            found=None
            for key in path:
                found=next((r for r in nodes if r['key']==key),None)
                if found is None:return 404, {'message':'画像层级不存在'}
                nodes=found.get('children',[])
            result=dict(found)
            if len(path)<3:result['children']=tree_summary(nodes)
        elif tag is not None:
            found = next((row for row in portrait['tags'] if row['key'] == tag), None)
            if found is None:
                return 404, {'message': '标签不存在'}
            result = dict(found)
        else:
            result['tags'] = [{k: v for k, v in row.items() if k != 'dramas'} for row in portrait['tags']]
            result['types'] = tree_summary(portrait.get('types',[]))
        return 200, dict(meta=meta, account_id=account_id,
                         account_name=account.get('display_name') or account.get('username') or account_id,
                         username=account.get('username') or '', portrait=result)
    items = {}
    for key, account in allowed.items():
        portrait = data['accounts'].get(str(account.get('x_user_id')), empty_account())
        items[key] = {k: v for k, v in portrait.items() if k not in ('tags','types','combinations')}
        items[key]['types'] = [{k:v for k,v in row.items() if k!='children'} for row in portrait.get('types',[])]
        items[key]['combinations'] = portrait.get('combinations',[])[:3]
        items[key]['tag_count'] = len(portrait['tags'])
        items[key]['tags'] = [{k: v for k, v in row.items() if k != 'dramas'} for row in portrait['tags'][:3]]
    return 200, dict(meta=meta, items=items)


def dispatch(handler, parsed, authorize, root=DEFAULT_ROOT):
    send = handler.send_json
    match = re.fullmatch(r'/api/x-channel-tag-revenue(?:/([0-9]+))?', parsed.path)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if not match or set(query) - {'tag','path'} or any(len(v) != 1 for v in query.values()) or (query and not match.group(1)) or ('tag' in query and 'path' in query):
        return send(400, {'message': '查询参数无效'})
    path=query.get('path',[None])[0]
    if path is not None:
        path=path.split('/')
        if not 1<=len(path)<=3 or any(not re.fullmatch(r'[a-z_]{1,40}',key) for key in path):
            return send(400, {'message':'画像层级参数无效'})
    try:
        # Delegate to the existing authenticated channel-list endpoint.
        status, accounts = authorize(handler.headers.get('Cookie', ''))
        if status != 200:
            return send(status, {'message': '请登录并使用已获授权的 X 频道列表' if status in (401,403) else '频道权限暂时无法核验，请稍后重试'})
    except (OSError, ValueError, KeyError, TypeError):
        return send(503, {'message': '频道权限暂时无法核验，请稍后重试'})
    try:
        data, meta = read_snapshot(root)
        status, payload = response(data, meta, accounts, match.group(1), query.get('tag', [None])[0], path)
        return send(status, payload)
    except (OSError, ValueError, KeyError, TypeError):
        return send(503, {'message': '标签收入画像暂不可用，请稍后重试；频道其他功能可正常使用'})
