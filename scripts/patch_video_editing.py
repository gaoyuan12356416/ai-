"""Idempotent, AST-anchored integration into the composite production app.

Only the module registries, navigation return and four HTTP entrypoints change.
The deployer separately pins and verifies the complete input file hashes.
"""
import ast
from pathlib import Path


def patch_app(source):
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    edits = []
    for name, value in (("MODULE_PERMISSIONS", '"视频剪辑"'),
                        ("DEFAULT_USER_PERMISSIONS", "False")):
        node = next(n for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))
        if not isinstance(node.value, ast.Dict):
            raise ValueError("Unexpected module registry: " + name)
        if not any(isinstance(k, ast.Constant) and k.value == "video_editing"
                   for k in node.value.keys):
            edits.append((node.value.lineno, node.value.lineno,
                          '    "video_editing": ' + value + ',\n'))
    nav = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name == "load_navigation_config")
    nav_source = "".join(lines[nav.lineno - 1:nav.end_lineno])
    if "ensure_navigation" not in nav_source:
        old = "return filter_navigation(json.load(handle))"
        if nav_source.count(old) != 1:
            raise ValueError("Unexpected navigation loader")
        nav_source = nav_source.replace(old,
            "from features.video_editing.navigation import ensure_navigation\n"
            "        return ensure_navigation(filter_navigation(json.load(handle)))")
        edits.append((nav.lineno - 1, nav.end_lineno, nav_source))
    handler = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == "DramaMaterialHandler")
    for method in ("GET", "POST", "PATCH", "DELETE"):
        function = next((n for n in handler.body if isinstance(n, ast.FunctionDef)
                         and n.name == "do_" + method), None)
        block = (
            '        if urlparse(self.path).path.startswith("/api/video-editing/")'
            ' or urlparse(self.path).path in {"/video-editing.html", '
            '"/video-editing.js", "/video-editing.css"}:\n'
            '            from features.video_editing.app_bridge import handle as video_editing_handle\n'
            '            video_editing_handle(self, "' + method + '", sys.modules[__name__])\n'
            '            return\n'
        )
        if function:
            body = "".join(lines[function.lineno - 1:function.end_lineno])
            if "video_editing_handle" not in body:
                edits.append((function.body[0].lineno - 1,
                              function.body[0].lineno - 1, block))
        else:
            edits.append((handler.end_lineno, handler.end_lineno,
                          '\n    def do_' + method + '(self):\n' + block +
                          '        json_response(self, 404, {"error": "not_found"})\n'))
    for start, end, replacement in sorted(edits, reverse=True):
        lines[start:end] = [replacement]
    result = "".join(lines)
    compile(result, "app.py", "exec")
    return result


def patch_nav(source):
    if 'key: "videoEditing"' in source:
        return source
    item = '''{
      key: "video_editing", label: "视频剪辑", order: 15, module: "video_editing",
      items: [{ key: "videoEditing", label: "视频剪辑工作台", order: 10,
        description: "共享视频素材、逐剧剪辑与制作任务", kind: "page",
        href: "/video-editing.html", module: "video_editing", enabled: true }],
    }'''
    anchor = "const DEFAULT_NAV = ["
    if source.count(anchor) != 1:
        raise ValueError("Unexpected navigation defaults")
    source = source.replace(anchor, anchor + "\n    " + item + ",", 1)
    anchor = "const INTERNAL_VIEW_HREFS = {"
    if source.count(anchor) != 1:
        raise ValueError("Unexpected navigation href registry")
    source = source.replace(anchor, anchor + '\n    videoEditing: "/video-editing.html",', 1)
    anchor = "const normalized = cloneNav(config);"
    if source.count(anchor) != 1:
        raise ValueError("Unexpected saved navigation normalization")
    source = source.replace(anchor, anchor + '''
    if (!normalized.some(group => group && Array.isArray(group.items) &&
        group.items.some(item => item && item.key === "videoEditing"))) {
      const editorDefault = DEFAULT_NAV.find(group => group.key === "video_editing");
      const editorGroup = normalized.find(group => group && group.key === "video_editing");
      if (editorGroup) {
        if (!Array.isArray(editorGroup.items)) editorGroup.items = [];
        editorGroup.items.push(JSON.parse(JSON.stringify(editorDefault.items[0])));
      } else normalized.push(JSON.parse(JSON.stringify(editorDefault)));
    }''', 1)
    return source


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    for relative, patch in (("app.py", patch_app), ("static/quick-nav.js", patch_nav)):
        path = root / relative
        current = path.read_text(encoding="utf-8")
        updated = patch(current)
        if updated != current:
            path.write_text(updated, encoding="utf-8", newline="\n")
            print("Patched " + relative)
