"""Add the editor to saved navigation without rewriting operator choices."""
from copy import deepcopy

MODULE = "video_editing"
GROUP = {
    "key": "video_editing", "label": "视频剪辑", "order": 15,
    "module": MODULE, "items": [{
        "key": "videoEditing", "label": "视频剪辑工作台", "order": 10,
        "description": "共享视频素材、逐剧剪辑与制作任务", "kind": "page",
        "href": "/video-editing.html", "module": MODULE, "enabled": True,
    }],
}


def ensure_navigation(config):
    if not isinstance(config, list):
        return config
    result = deepcopy(config)
    if any(item.get("key") == "videoEditing" for group in result
           if isinstance(group, dict) for item in group.get("items", [])
           if isinstance(item, dict)):
        return result
    group = next((g for g in result if isinstance(g, dict)
                  and g.get("key") == MODULE), None)
    if group is None:
        result.append(deepcopy(GROUP))
    else:
        group.setdefault("items", []).append(deepcopy(GROUP["items"][0]))
    return result
