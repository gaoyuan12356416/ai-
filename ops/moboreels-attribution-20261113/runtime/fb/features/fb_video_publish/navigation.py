"""Compatibility additions without resetting operator navigation settings."""
from copy import deepcopy

ITEMS = [
    {"key": "fbPageManagement", "label": "Page 管理", "description": "设置 Page 剧语言和发布文案",
     "kind": "page", "href": "/fb-pages.html", "module": "fb_video_publish",
     "adminOnly": False, "enabled": True, "order": 30},
    {"key": "fbVideoPublish", "label": "视频批量发布", "description": "按剧语言匹配素材与 Page",
     "kind": "page", "href": "/fb-video-publish.html", "module": "fb_video_publish",
     "adminOnly": False, "enabled": True, "order": 40},
    {"key": "fbPostMaterialPool", "label": "FB Post 素材池", "description": "按语言分配素材到 Page 组定时发布",
     "kind": "page", "href": "/fb-post-material-pool.html", "module": "fb_video_publish",
     "adminOnly": False, "enabled": True, "order": 45},
    {"key": "fbVideoPublishRecords", "label": "批量发布记录", "description": "查看批次、冻结文案和发布结果",
     "kind": "page", "href": "/fb-video-publish-records.html", "module": "fb_video_publish",
     "adminOnly": False, "enabled": True, "order": 50},
]


def ensure_navigation(config):
    if not isinstance(config, list):
        raise ValueError("navigation must be a list")
    result = deepcopy(config)
    group = next((x for x in result if isinstance(x, dict) and x.get("key") == "facebook_platform"), None)
    if group is None:
        group = {"key": "facebook_platform", "label": "Facebook 社媒", "order": 38, "items": []}
        result.append(group)
    # The old permission stays on the old children. A group with mixed modules
    # must derive visibility from the authorized children.
    group.pop("module", None)
    if not isinstance(group.get("items"), list):
        raise ValueError("Facebook navigation items must be a list")
    keys = {item.get("key") for item in group["items"] if isinstance(item, dict)}
    for item in ITEMS:
        if item["key"] not in keys:
            group["items"].append(deepcopy(item))
    return result
