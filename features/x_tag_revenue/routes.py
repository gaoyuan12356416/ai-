"""Channel-list ACL and bounded database projections."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import parse_qs

from .storage import SnapshotExpired, VERSION_RE, tree_summary
from .serving import Store

DEFAULT_ROOT = '/mnt/data-disk/x-channel-tag-revenue'


def read_snapshot(root, x_user_ids=None, *, view='tree', version=None, store=None):
    data, meta = (store or Store(root=root)).load(x_user_ids, view=view, version=version)
    meta = dict(meta)
    # The collector's global reconciliation totals are not channel-scoped.
    # Keep them in the database; browser and publisher responses omit them.
    meta.pop('audit', None)
    age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(meta['source_generated_at'])).total_seconds())
    meta.update(stale=age > 7200, age_seconds=round(age))
    try:
        meta['refresh_failed'] = json.loads((Path(root) / 'status.json').read_text()).get('status') == 'error'
    except (OSError, ValueError):
        meta['refresh_failed'] = False
    return data, meta


def empty_account():
    return dict(status='no_history', revenue_cents=0, unknown_revenue_cents=0, known_share_bp=None,
                published_count=0, drama_count=0, tags=[], audiences=[], combinations=[], audience_basis_counts={})


def response(data, meta, accounts, account_id=None, tag=None, path=None):
    allowed = {str(row['id']): row for row in accounts}
    if account_id is not None:
        if account_id not in allowed:
            return 404, {'message': '频道不存在或无权查看'}
        account = allowed[account_id]
        portrait = data['accounts'].get(str(account.get('x_user_id')), empty_account())
        result = dict(portrait)
        if path is not None:
            nodes=portrait.get('audiences',portrait.get('types',[]))
            found=None
            for key in path:
                found=next((r for r in nodes if r['key']==key),None)
                if found is None:return 404, {'message':'画像层级不存在'}
                nodes=found.get('children',[])
            result=dict(found)
            if 'children' in found:result['children']=tree_summary(nodes)
        elif tag is not None:
            found = next((row for row in portrait['tags'] if row['key'] == tag), None)
            if found is None:
                return 404, {'message': '标签不存在'}
            result = dict(found)
        else:
            result['tags'] = [{k: v for k, v in row.items() if k != 'dramas'} for row in portrait['tags']]
            root = 'audiences' if 'audiences' in portrait else 'types'
            result[root] = tree_summary(portrait.get(root,[]))
        return 200, dict(meta=meta, account_id=account_id,
                         account_name=account.get('display_name') or account.get('username') or account_id,
                         username=account.get('username') or '', portrait=result)
    items = {}
    for key, account in allowed.items():
        portrait = data['accounts'].get(str(account.get('x_user_id')), empty_account())
        items[key] = {k: v for k, v in portrait.items() if k not in ('tags','types','audiences','combinations')}
        root = 'audiences' if 'audiences' in portrait else 'types'
        items[key][root] = [{k:v for k,v in row.items() if k!='children'} for row in portrait.get(root,[])]
        items[key]['combinations'] = portrait.get('combinations',[])[:3]
        items[key]['tag_count'] = portrait.get('tag_count', len(portrait['tags']))
        items[key]['tags'] = [{k: v for k, v in row.items() if k != 'dramas'} for row in portrait['tags'][:3]]
    return 200, dict(meta=meta, items=items)


def dispatch(handler, parsed, authorize, root=DEFAULT_ROOT):
    send = handler.send_json
    match = re.fullmatch(r'/api/x-channel-tag-revenue(?:/([0-9]+))?', parsed.path)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if not match or set(query) - {'tag','path','page','version'} or any(len(v) != 1 for v in query.values()) or (query and not match.group(1)) or ('tag' in query and 'path' in query):
        return send(400, {'message': '查询参数无效'})
    version = query.get('version', [None])[0]
    page = query.get('page', ['1'])[0]
    if (version is not None and not re.fullmatch(VERSION_RE, version)) or not re.fullmatch(r'[1-9][0-9]{0,5}', page):
        return send(400, {'message': '查询参数无效'})
    page = int(page)
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
        account_id = match.group(1)
        allowed = {str(a['id']): a for a in accounts}
        if account_id is not None and account_id not in allowed:
            return send(404, {'message': '频道不存在或无权查看'})
        selected = [allowed[account_id]] if account_id else accounts
        store = Store(root=root)
        data, meta = read_snapshot(root, [a.get('x_user_id') for a in selected],
                                   view='tree' if account_id else 'summary', version=version, store=store)
        tag = query.get('tag', [None])[0]
        if account_id and (tag is not None or path):
            uid = str(allowed[account_id].get('x_user_id'))
            profile = data['accounts'].get(uid, empty_account())
            root = 'audiences' if 'audiences' in profile else 'types'
            nodes, json_path, found = profile.get(root, []), '$', None
            if tag is not None:
                path, nodes = [tag], profile.get('tags', [])
            for depth, key in enumerate(path):
                index = next((i for i, node in enumerate(nodes) if node['key'] == key), None)
                if index is None:
                    return send(404, {'message': '画像层级不存在'})
                found = nodes[index]
                json_path += ('.tags' if tag is not None else '.'+root if depth == 0 else '.children') + f'[{index}]'
                nodes = found.get('children', [])
            if 'children' not in found:
                details = store.dramas(uid, meta['version'], json_path + '.dramas', page)
                found['dramas'] = details.pop('items')
                found['pagination'] = details
            if tag is not None:
                path = None
        status, payload = response(data, meta, accounts, account_id, tag, path)
        return send(status, payload)
    except SnapshotExpired:
        return send(409, {'message': '该画像版本已更新，请重新打开详情'})
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        return send(503, {'message': '标签收入画像暂不可用，请稍后重试；频道其他功能可正常使用'})
