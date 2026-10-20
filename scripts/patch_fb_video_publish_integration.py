"""Surgical integration into the verified current API/nav, never an old app.py.

Pure transformations are tested on a read-only live source snapshot. Deployment
checks SHA256 and backs up exact target bytes before applying these changes.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.fb_video_publish.navigation import ITEMS, ensure_navigation

MARKER = "# FB_VIDEO_PUBLISH_INTEGRATION_V1"


def once(text, old, new):
    if text.count(old) != 1:
        raise ValueError("Integration anchor mismatch: " + old[:100])
    return text.replace(old, new, 1)


def patch_app(text):
    if MARKER in text:
        return text
    text = once(text, "MODULE_PERMISSIONS = {",
                'MODULE_PERMISSIONS = {\n    "fb_video_publish": "Facebook 视频批量发布", ' + MARKER + '\n')
    text = once(text, "DEFAULT_USER_PERMISSIONS = {",
                'DEFAULT_USER_PERMISSIONS = {\n    "fb_video_publish": False,')
    old = '        return filter_navigation(json.load(handle))'
    new = ('        from features.fb_video_publish.navigation import ensure_navigation\n'
           '        return filter_navigation(ensure_navigation(json.load(handle)))')
    text = once(text, old, new)
    # Parse the actual current class, not a line-offset from an obsolete checkout.
    tree = ast.parse(text)
    handler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DramaMaterialHandler")
    offsets = []
    for method in ("do_GET", "do_POST"):
        func = next(n for n in handler.body if isinstance(n, ast.FunctionDef) and n.name == method)
        assignment = next(n for n in func.body if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "parsed" for t in n.targets))
        offsets.append((assignment.end_lineno, method))
    lines = text.splitlines(keepends=True)
    dispatch = ('        if parsed.path == "/api/fb-video-publish" or parsed.path.startswith("/api/fb-video-publish/"):\n'
                '            from features.fb_video_publish.gateway import dispatch\n'
                '            return dispatch(self, parsed, globals())\n')
    for offset, method in sorted(offsets, reverse=True):
        internal = ('        if parsed.path == "/internal/fb-video-publish/query":\n'
                    '            from features.fb_video_publish.query_broker import dispatch\n'
                    '            return dispatch(self, globals())\n') if method == "do_POST" else ""
        lines.insert(offset, internal + dispatch)
    text = "".join(lines)
    # The Nginx/static deployment is primary; keep the main fallback consistent.
    static_marker = '            "/fb-post-ad-delete.js",'
    text = once(text, static_marker, static_marker + '\n' + "\n".join(
        '            "' + name + '",' for name in (
            "/fb-pages.html", "/fb-video-publish.html", "/fb-video-publish-records.html",
            "/fb-video-publish.js", "/fb-video-publish.css",
        )))
    ast.parse(text)
    return text


def patch_quick_nav(text):
    marker = "// FB_VIDEO_PUBLISH_NAVIGATION_V1"
    if marker in text:
        return text
    anchor = '    if (!Array.isArray(facebookPlatform.items)) facebookPlatform.items = [];'
    insertion = (
        "\n    " + marker + "\n"
        "    delete facebookPlatform.module;\n"
        "    const fbVideoItems = " + json.dumps(ITEMS, ensure_ascii=False) + ";\n"
        "    fbVideoItems.forEach(item => {\n"
        "      if (!facebookPlatform.items.some(existing => existing && existing.key === item.key)) facebookPlatform.items.push(item);\n"
        "    });"
    )
    text = once(text, anchor, anchor + insertion)
    # The normalized path above also upgrades cached/saved configs. Remove only
    # Facebook group-level defaults; the old children's permissions remain.
    text, count = re.subn(r'(key: "facebook_platform",\s*label: "Facebook 社媒",\s*order: 38,)\s*module: "fb_page_posts",',
                         r"\1", text)
    if count != 2:
        raise ValueError("Facebook default group anchors changed")
    return text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        raise ValueError("Use a distinct staging directory")
    args.output.mkdir(parents=True, exist_ok=True)
    for relative, transform in (("app.py", patch_app), ("static/quick-nav.js", patch_quick_nav)):
        target = args.output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(transform((args.source / relative).read_text(encoding="utf-8")),
                          encoding="utf-8", newline="\n")
    path = args.output / "static/navigation.json"
    path.write_text(json.dumps(ensure_navigation(json.loads((args.source / "static/navigation.json").read_text(encoding="utf-8-sig"))),
                               ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Staged current API and navigation with scoped FB additions")


if __name__ == "__main__":
    main()
