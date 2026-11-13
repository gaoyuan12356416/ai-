from copy import deepcopy


GROUP = {
    "key": "partner_libraries", "label": "合作方剧库", "order": 16, "enabled": True,
    "items": [{"key": "moboreelsLibrary", "label": "Moboreels 短剧库", "order": 10,
               "href": "/moboreels-library.html", "module": "moboreels_library", "enabled": True},
              {"key": "moboreelsRecommendations", "label": "Moboreels 推荐剧单", "order": 20,
               "href": "/moboreels-recommendations.html", "module": "moboreels_library", "enabled": True},
              {"key": "moboreelsMediaAccounts", "label": "Moboreels 媒体账号报备", "order": 30,
               "href": "/moboreels-media-accounts.html", "module": "moboreels_library", "enabled": True}],
}

REPORT_ITEM = {"key": "moboreelsReport", "label": "Moboreels 收益报表", "order": 65,
               "href": "/moboreels-report.html", "module": "moboreels_report", "enabled": True}


def ensure_report_navigation(config):
    groups = deepcopy(config) if isinstance(config, list) else []
    if any(item.get('key') == REPORT_ITEM['key'] for group in groups if isinstance(group, dict)
           for item in group.get('items', []) if isinstance(item, dict)):
        return groups
    for group in groups:
        if isinstance(group, dict) and group.get('label') == '数据报表':
            group.setdefault('items', []).append(deepcopy(REPORT_ITEM))
            return groups
    groups.append({'key': 'moboreels_reports', 'label': '数据报表', 'order': 70, 'enabled': True,
                   'items': [deepcopy(REPORT_ITEM)]})
    return groups


def ensure_navigation(config):
    groups = deepcopy(config) if isinstance(config, list) else []
    existing = {item.get("key") for group in groups if isinstance(group, dict)
                for item in group.get("items", []) if isinstance(item, dict)}
    missing = [deepcopy(item) for item in GROUP["items"] if item["key"] not in existing]
    if not missing:
        return groups
    for group in groups:
        if isinstance(group, dict) and group.get("key") == GROUP["key"]:
            group.setdefault("items", []).extend(missing)
            return groups
    groups.append({**deepcopy(GROUP), "items": missing})
    return groups
