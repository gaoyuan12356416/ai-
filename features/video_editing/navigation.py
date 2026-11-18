"""Add the editor to saved navigation without rewriting operator choices."""
from copy import deepcopy

MODULE = "video_editing"
GROUP = {
    "key": "video_editing", "label": "视频剪辑", "order": 15,
    "module": MODULE, "items": [{
        "key": "videoEditing", "label": "视频剪辑工作台", "order": 10,
        "description": "共享视频素材、逐剧剪辑与制作任务", "kind": "page",
        "href": "/video-editing.html", "module": MODULE, "enabled": True,
    }, {
        "key": "videoEditingAi", "label": "AI 智能剪辑", "order": 20,
        "description": "分析原片、调整剪辑方案并制作成片", "kind": "page",
        "href": "/video-editing-ai.html", "module": MODULE, "enabled": True,
    }],
}


def ensure_navigation(config):
    if not isinstance(config, list):
        return config
    result = deepcopy(config)
    keys = {item.get("key") for group in result if isinstance(group, dict)
            for item in (group.get("items") or []) if isinstance(item, dict)}
    group = next((g for g in result if isinstance(g, dict)
                  and g.get("key") == MODULE), None)
    missing = [deepcopy(item) for item in GROUP["items"] if item["key"] not in keys]
    if missing:
        if group is None:
            group = {**GROUP, "items": []}
            result.append(group)
        group.setdefault("items", []).extend(missing)
    return result
