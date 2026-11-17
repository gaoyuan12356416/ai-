"""Apply small reviewed edits to the installed reporting variants."""
import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]


def once(text, before, after):
    if text.count(before) != 1:
        raise ValueError('source_anchor_changed:' + before[:90])
    return text.replace(before, after, 1)


def function(text, name):
    node = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == name)
    return ''.join(text.splitlines(keepends=True)[node.lineno - 1:node.end_lineno])


def patch_source(relative, text):
    if relative == 'features/fb_auto_posts/links.py':
        text = once(text, 'FB_W2A_BASE_URL = "https://www.dramawavew2a.com/ads/0/2049/view"',
                    'FB_W2A_BASE_URL = "https://www.dramawavew2a.com/ads/101/2310/view"')
        text = once(text, 'or parsed.path != base.path',
                    'or parsed.path not in ("/ads/101/2310/view", "/ads/0/2049/view")')
    elif relative == 'features/fb_auto_posts/effect_collector.py':
        text = once(text, "AND site_id='2049' AND ad_id", "AND site_id IN ('2049','2310') AND ad_id")
    elif relative == 'features/social_analytics/facebook.py':
        text = once(text, 'path = "/ads/0/2049/view" if site == "2049" else "/ads/101/2310/view"',
                    'paths = (("/ads/0/2049/view", "/ads/101/2310/view") if site == "2049"\n'
                    '                 else ("/ads/101/2310/view",))')
        text = once(text, 'or parsed.path != path or set(query)', 'or parsed.path not in paths or set(query)')
    elif relative == 'features/social_analytics/report.py':
        source = (SOURCE / relative).read_text(encoding='utf-8')
        text = once(text, function(text, 'campaign_index'),
                    function(source, 'effect_sites') + '\n\n\n' + function(source, 'campaign_index'))
        text = once(text, 'emit(dict(row, date=day), metric)', 'emit(dict(row, date=day, site_id=site), metric)')
    elif relative == 'features/social_analytics/routes.py':
        text = once(text, 'from .adapters import', 'from .report import effect_sites\nfrom .adapters import')
        text = once(text, 'row["site_id"] == site', 'site in effect_sites(row)')
    elif relative == 'features/social_analytics/warehouse_refresh.py':
        text = once(text, 'from .report import', 'from .report import effect_sites\nfrom .report import')
        if text.count("row['site_id'] == site") != 2:
            raise ValueError('warehouse_source_filter_changed')
        text = text.replace("row['site_id'] == site", 'site in effect_sites(row)')
    elif relative == 'features/social_daily_report/collector.py':
        text = once(text, 'from features.youtube_daily_report.collector import',
                    'from features.social_analytics.report import effect_sites\nfrom features.youtube_daily_report.collector import')
        # Only the current daily runtime freezes its exact query scope here.
        text = once(text, 'row["site_id"] == site', 'site in effect_sites(row)')
    elif relative == 'features/social_analytics/adapters.py':
        text = once(text, '{"site_id": "2049", "label": "旧自动发布"}',
                    '{"site_id": "2049", "label": "2049 历史落地页"}')
        text = once(text, '{"site_id": "2310", "label": "手动发布"}',
                    '{"site_id": "2310", "label": "2310 落地页"}')
    else:
        raise ValueError('unsupported_patch:' + relative)
    compile(text, relative, 'exec')
    return text.encode('utf-8')
