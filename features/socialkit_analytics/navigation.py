"""Add an independently authorized report without broadening existing items."""
import copy

ITEM = dict(key="socialkitAnalytics", label="SocialKit 效果报表",
            description="按平台、产品、账号和剧目查看每日发布与收入",
            kind="page", href="/socialkit-analytics.html",
            module="socialkit_analytics", enabled=True, order=30)


def ensure_navigation(config):
    if not isinstance(config, list):
        return config
    config = copy.deepcopy(config)
    if any(isinstance(group, dict) and any(isinstance(item, dict) and item.get('key') == ITEM['key']
           for item in (group.get('items') or [])) for group in config):
        return config
    reports = next((g for g in config if isinstance(g, dict) and g.get("key") == "social_reports"), None)
    if reports is None:
        reports = dict(key="social_reports", label="数据报表", order=50, items=[])
        config.append(reports)
    if not isinstance(reports.get("items"), list):
        reports["items"] = []
    group_module = reports.get('module')
    custom_combination = group_module and (group_module != 'social_analytics' or any(
        isinstance(item, dict) and item.get('module') not in (None, '', 'social_analytics')
        for item in reports['items']))
    if custom_combination:
        # A custom group+item combination is AND authorization; preserve it intact.
        sibling = next((g for g in config if isinstance(g, dict) and g.get('key') == 'socialkit_reports'), None)
        if sibling is None:
            sibling = dict(key='socialkit_reports', label='数据报表 · SocialKit', order=51, items=[])
            config.append(sibling)
        sibling.setdefault('items', []).append(copy.deepcopy(ITEM))
        return config
    if reports.get("module") == "social_analytics":
        # Preserve the inherited old permission before allowing independent items.
        for item in reports["items"]:
            if isinstance(item, dict) and not item.get("module"):
                item["module"] = "social_analytics"
        reports.pop("module")
    if not any(isinstance(item, dict) and item.get("key") == ITEM["key"] for item in reports["items"]):
        reports["items"].append(copy.deepcopy(ITEM))
    return config
