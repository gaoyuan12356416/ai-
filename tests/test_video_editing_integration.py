import ast
import copy
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch as mock_patch

from features.video_editing.navigation import GROUP, ensure_navigation

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("patch_video_editing", ROOT / "scripts/patch_video_editing.py")
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)


class IntegrationTests(unittest.TestCase):
    def test_saved_navigation_gets_independent_permission_group(self):
        old = [{"key": "drama", "module": "drama_synthesis", "items": []}]
        before = copy.deepcopy(old)
        updated = ensure_navigation(old)
        self.assertEqual(before, old)
        self.assertEqual(updated[-1], GROUP)
        self.assertEqual(updated, ensure_navigation(updated))

    def test_operator_disabled_item_remains_disabled(self):
        old = [copy.deepcopy(GROUP)]
        old[0]["items"][0]["enabled"] = False
        self.assertEqual(old, ensure_navigation(old))

    def test_patch_preserves_other_handlers_and_is_idempotent(self):
        original = '''from urllib.parse import urlparse
MODULE_PERMISSIONS = {
    "old": "old",
}
DEFAULT_USER_PERMISSIONS = {
    "old": False,
}
def load_navigation_config():
    with open(NAVIGATION_CONFIG_PATH, "r", encoding="utf-8-sig") as handle:
        return filter_navigation(json.load(handle))
class DramaMaterialHandler:
    def do_GET(self):
        unrelated_get()
    def do_POST(self):
        unrelated_post()
    def do_DELETE(self):
        unrelated_delete()
    def do_PUT(self):
        unrelated_put()
'''
        result = patch.patch_app(original)
        self.assertEqual(result, patch.patch_app(result))
        old = ast.parse(original)
        new = ast.parse(result)
        old_cls = next(n for n in old.body if isinstance(n, ast.ClassDef))
        new_cls = next(n for n in new.body if isinstance(n, ast.ClassDef))
        for method in old_cls.body:
            changed = next(n for n in new_cls.body if n.name == method.name)
            remaining = changed.body if method.name == "do_PUT" else changed.body[1:]
            self.assertEqual(ast.dump(ast.Module(body=method.body, type_ignores=[])),
                             ast.dump(ast.Module(body=remaining, type_ignores=[])))
        self.assertIn('"video_editing": False', result)

    def test_checkout_patch_is_idempotent(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        upgraded = patch.patch_app(source)
        self.assertEqual(upgraded, patch.patch_app(upgraded))
        nav = (ROOT / "static/quick-nav.js").read_text(encoding="utf-8")
        self.assertEqual(nav, patch.patch_nav(nav))

    def dispatch_fixture(self):
        return '''from urllib.parse import urlparse
MODULE_PERMISSIONS = {
    "old": "old",
}
DEFAULT_USER_PERMISSIONS = {
    "old": False,
}
def load_navigation_config():
    with open(NAVIGATION_CONFIG_PATH, "r", encoding="utf-8-sig") as handle:
        return filter_navigation(json.load(handle))
class DramaMaterialHandler:
    def do_GET(self):
        unrelated("GET")
    def do_POST(self):
        unrelated("POST")
    def do_DELETE(self):
        unrelated("DELETE")
'''

    def execute_dispatch(self, source):
        module = types.ModuleType("_video_editing_dispatch_stub")
        bridge = types.ModuleType("features.video_editing.app_bridge")
        dispatched, unrelated, missing = [], [], []
        bridge.handle = lambda handler, method, app: dispatched.append((handler.path, method, app))
        module.__dict__.update(unrelated=unrelated.append,
                               json_response=lambda *args: missing.append(args))
        with mock_patch.dict(sys.modules, {module.__name__: module, bridge.__name__: bridge}):
            exec(compile(source, "patched_dispatch_stub.py", "exec"), module.__dict__)
            self.assertNotIn("sys", module.__dict__)
            handler = module.DramaMaterialHandler()
            for method in ("GET", "POST", "PATCH", "DELETE"):
                handler.path = "/api/video-editing/config?readback=1"
                getattr(handler, "do_" + method)()
                self.assertEqual(dispatched[-1], (handler.path, method, module))
                handler.path = "/api/unrelated"
                getattr(handler, "do_" + method)()
            for path in ("/video-editing.html", "/video-editing.js", "/video-editing.css"):
                handler.path = path
                handler.do_GET()
                self.assertEqual(dispatched[-1], (path, "GET", module))
            self.assertNotIn("sys", module.__dict__)
        self.assertEqual(len(dispatched), 7)
        self.assertEqual(unrelated, ["GET", "POST", "DELETE"])
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0][1], 404)

    def test_new_dispatch_executes_without_global_sys_for_all_verbs_and_static_routes(self):
        self.execute_dispatch(patch.patch_app(self.dispatch_fixture()))

    def test_installed_legacy_dispatch_gets_only_local_sys_imports_and_executes(self):
        current = patch.patch_app(self.dispatch_fixture())
        legacy = current.replace("            import sys\n", "")
        self.assertNotIn("import sys", legacy)
        with self.assertRaisesRegex(NameError, "sys"):
            self.execute_dispatch(legacy)
        repaired = patch.patch_app(legacy)
        self.assertEqual(repaired, current)
        self.assertEqual(repaired.replace("            import sys\n", ""), legacy)
        self.assertEqual(repaired, patch.patch_app(repaired))
        self.execute_dispatch(repaired)


if __name__ == "__main__":
    unittest.main()
