"""Apply only SocialKit integration hunks to a possibly newer backend baseline."""
import argparse
import ast
import pathlib
import re


def patch_app(text):
    if '"socialkit_analytics": "SocialKit 效果报表"' not in text:
        anchor = '    "social_analytics": "社媒数据报表",'
        if text.count(anchor) != 1:
            raise ValueError("module permission anchor changed")
        text = text.replace(anchor, anchor + '\n    "socialkit_analytics": "SocialKit 效果报表",')
    if '"socialkit_analytics": False' not in text:
        anchor = '    "social_analytics": False,'
        if text.count(anchor) != 1:
            raise ValueError("default permission anchor changed")
        text = text.replace(anchor, anchor + '\n    "socialkit_analytics": False,')
    if 'def _socialkit_analytics_actor(' not in text:
        actor = re.search(r'    def _social_analytics_actor\(.*?(?=\n    def )', text, re.S)
        if actor is None:
            raise ValueError("cookie actor anchor changed")
        addition = actor.group().replace('_social_analytics_actor', '_socialkit_analytics_actor').replace('"social_analytics"', '"socialkit_analytics"').replace('"socialAnalytics"', '"socialkitAnalytics"')
        text = text[:actor.end()] + '\n' + addition + text[actor.end():]
    if 'ensure_socialkit_navigation' not in text:
        match = re.search(r'def load_navigation_config\(\):\n.*?(?=\ndef )', text, re.S)
        if match is None:
            raise ValueError("navigation loader anchor changed")
        original = match.group()
        updated = original.replace('def load_navigation_config():\n', 'def load_navigation_config():\n    from features.socialkit_analytics.navigation import ensure_navigation as ensure_socialkit_navigation\n', 1)
        updated, count = re.subn(r'(?m)^(\s*)return filter_navigation\((.+)\)$', r'\1return ensure_socialkit_navigation(filter_navigation(\2))', updated)
        if count != 1:
            raise ValueError("navigation return anchor changed")
        text = text.replace(original, updated, 1)
    route = '        if parsed.path == "/api/socialkit-analytics" or parsed.path.startswith("/api/socialkit-analytics/"):\n            from features.socialkit_analytics.routes import dispatch\n            return dispatch(self, parsed, globals())\n\n'
    old_route = '        if parsed.path == "/api/social-analytics" or parsed.path.startswith("/api/social-analytics/"):'
    if 'from features.socialkit_analytics.routes import dispatch' not in text:
        if text.count(old_route) != 2:
            raise ValueError("GET/POST route anchors changed")
        text = text.replace(old_route, route + old_route)
    if '            "/socialkit-analytics.html",' not in text:
        anchor = '            "/fb-post-ad-delete.html",'
        if text.count(anchor) != 1:
            raise ValueError("static allowlist anchor changed")
        text = text.replace(anchor, '            "/socialkit-analytics.html",\n            "/socialkit-analytics.js",\n            "/socialkit-analytics.css",\n'+anchor, 1)
    ast.parse(text)
    return text


def patch_nav(text):
    if 'key: "socialkitAnalytics"' not in text:
        anchor = '    socialAnalytics: "/social-analytics.html",'
        if text.count(anchor) != 1:
            raise ValueError("navigation href anchor changed")
        text = text.replace(anchor, anchor+'\n    socialkitAnalytics: "/socialkit-analytics.html",')
        anchor = '    const normalized = cloneNav(config);'
        if text.count(anchor) != 1:
            raise ValueError("navigation normalization anchor changed")
        addition = '''    // SocialKit has its own module; retain inherited permissions on old items.
    let socialkitReports = normalized.find(group => group && group.key === "social_reports");
    if (!socialkitReports) {
      socialkitReports = cloneNav([DEFAULT_NAV.find(group => group.key === "social_reports")])[0];
      normalized.push(socialkitReports);
    }
    if (!Array.isArray(socialkitReports.items)) socialkitReports.items = [];
    const socialkitExists = normalized.some(group => Array.isArray(group.items) &&
      group.items.some(item => item && item.key === "socialkitAnalytics"));
    const customReportAcl = socialkitReports.module && (socialkitReports.module !== "social_analytics" ||
      socialkitReports.items.some(item => item && item.module && item.module !== "social_analytics"));
    if (!socialkitExists && customReportAcl) {
      let separate = normalized.find(group => group && group.key === "socialkit_reports");
      if (!separate) {
        separate = {key: "socialkit_reports", label: "数据报表 · SocialKit", order: 51, items: []};
        normalized.push(separate);
      }
      socialkitReports = separate;
    }
    if (!socialkitExists && socialkitReports.module === "social_analytics") {
      socialkitReports.items.forEach(item => { if (!item.module) item.module = "social_analytics"; });
      delete socialkitReports.module;
    }
    if (!socialkitExists) {
      socialkitReports.items.push({key: "socialkitAnalytics", label: "SocialKit 效果报表",
        description: "按平台、产品、账号和剧目查看每日发布与收入",
        kind: "page", href: "/socialkit-analytics.html", module: "socialkit_analytics",
        enabled: true, order: 30});
    }

'''
        text = text.replace(anchor, anchor+'\n'+addition, 1)
    return text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    root = pathlib.Path(args.root)
    for relative, fn in [('app.py', patch_app), ('static/quick-nav.js', patch_nav)]:
        path = root / relative
        original = path.read_text(encoding='utf-8')
        updated = fn(original)
        if not args.check and updated != original:
            path.write_bytes(updated.encode('utf-8'))
        print(relative, 'unchanged' if updated == original else 'patch_valid' if args.check else 'patched')


if __name__ == '__main__':
    main()
