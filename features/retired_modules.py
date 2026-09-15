"""Permanent retirement boundary for the four discontinued AI backend modules."""
from copy import deepcopy
from urllib.parse import unquote

RETIRED_GROUPS = frozenset({"ad_control", "ad_control_v3", "ad_material", "ad_material_test"})
RETIRED_MODULES = frozenset({"ad_control_center", "ad_control_v3", "ad_material_tasks", "ad_material_test"})
RETIRED_MESSAGE = "该功能已下线"


def retired_path(path):
    path = unquote(str(path or ""))
    # Playable packaging remains a separate, supported API.
    if path == "/api/ad-material/playable-preview":
        return False
    prefixes = (
        "/api/ad-control", "/api/ad-material", "/api/ad-material-vision",
        "/api/ad-material-generation", "/ad-material-test", "/ad-material-test-api",
    )
    if any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes):
        return True
    return (
        path in {"/ad-control.html", "/ad-material-tasks.html", "/ad-materials.html",
                 "/ad-material-task-optimization-test.html"}
        or (path.startswith("/ad-control-") and path.endswith((".html", ".js", ".css")))
    )


def filter_navigation(config):
    """Remove retired entries and preserve the separately owned Meta cleanup page."""
    if not isinstance(config, list):
        return config
    result = []
    asset_item = None
    for group in deepcopy(config):
        if not isinstance(group, dict):
            continue
        items = []
        for item in group.get("items", []):
            if item.get("key") == "fbPostAdDelete":
                if asset_item is None:
                    asset_item = item
                continue
            if item.get("module") in RETIRED_MODULES or retired_path(item.get("href")):
                continue
            items.append(item)
        if group.get("key") in RETIRED_GROUPS or group.get("module") in RETIRED_MODULES:
            continue
        group["items"] = items
        result.append(group)
    if asset_item is not None:
        asset_item.update(module="fb_ad_asset_delete", href="/fb-post-ad-delete.html")
        target = next((group for group in result if group.get("key") == "meta_asset_delete"), None)
        if target is None:
            target = {"key": "meta_asset_delete", "label": "Meta 广告清理", "order": 6,
                      "module": "fb_ad_asset_delete", "items": []}
            result.append(target)
        target["module"] = "fb_ad_asset_delete"
        target["items"].append(asset_item)
    return result
