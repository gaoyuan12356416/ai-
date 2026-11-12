from copy import deepcopy


GROUP = {
    "key": "partner_libraries", "label": "合作方剧库", "order": 16, "enabled": True,
    "items": [{"key": "moboreelsLibrary", "label": "Moboreels 短剧库", "order": 10,
               "href": "/moboreels-library.html", "module": "moboreels_library", "enabled": True}],
}


def ensure_navigation(config):
    groups = deepcopy(config) if isinstance(config, list) else []
    if any(item.get("key") == "moboreelsLibrary" for group in groups if isinstance(group, dict)
           for item in group.get("items", []) if isinstance(item, dict)):
        return groups
    for group in groups:
        if isinstance(group, dict) and group.get("key") == GROUP["key"]:
            group.setdefault("items", []).append(deepcopy(GROUP["items"][0]))
            return groups
    groups.append(deepcopy(GROUP))
    return groups
