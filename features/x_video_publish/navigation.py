"""Add owned publishing navigation without resetting operator configuration."""
from copy import deepcopy

ITEMS = [
    {'key': 'xVideoPublish', 'label': '手动发布', 'description': '全部账号选材、配对与定时发布',
     'kind': 'page', 'href': '/x-video-publish.html', 'module': 'x_accounts',
     'adminOnly': False, 'enabled': True, 'order': 23},
    {'key': 'xVideoPublishRecords', 'label': '手动发布记录', 'description': '查看手动、素材池和 Excel 发布批次',
     'kind': 'page', 'href': '/x-video-publish.html?view=records', 'module': 'x_accounts',
     'adminOnly': False, 'enabled': True, 'order': 24},
]

def ensure_navigation(config):
    result = deepcopy(config)
    if not isinstance(result, list):
        raise ValueError('navigation must be a list')
    group = next((g for g in result if isinstance(g, dict) and g.get('key') == 'x_platform'), None)
    if group is None:
        group = {'key': 'x_platform', 'label': 'X平台推广', 'order': 40, 'module': 'x_accounts', 'items': []}
        result.append(group)
    # Preserve the original parent requirement, including custom child items
    # that inherit it. Personal publishing adds its own module, never relaxes it.
    if not isinstance(group.get('items'), list):
        raise ValueError('X navigation items must be a list')
    keys = {i.get('key') for i in group['items'] if isinstance(i, dict)}
    group['items'].extend(deepcopy(i) for i in ITEMS if i['key'] not in keys)
    for item in group['items']:
        if item.get('key') in {'xAccountList', 'xVideoPublish', 'xVideoPublishRecords'}:
            item.update(module='x_accounts', adminOnly=False)
    return result
