import ast
import copy
import importlib.util
import unittest
from pathlib import Path

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
        original = '''import sys
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
        self.assertEqual(source, patch.patch_app(source))
        nav = (ROOT / "static/quick-nav.js").read_text(encoding="utf-8")
        self.assertEqual(nav, patch.patch_nav(nav))


if __name__ == "__main__":
    unittest.main()
