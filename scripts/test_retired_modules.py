import ast
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.retired_modules import filter_navigation, retired_path


class RetirementTests(unittest.TestCase):
    def test_routes_preserve_shared_products(self):
        for path in ("/api/ad-control/v3/rule-groups/x/execute", "/api/ad-control/rule-groups",
                     "/api/ad-material/tasks/x", "/ad-material-test/", "/ad-control-rules.html",
                     "/ad-material-tasks.html", "/ad-material-test-api/api/test"):
            self.assertTrue(retired_path(path), path)
        for path in ("/api/fb-post-ad-delete/tasks", "/fb-post-ad-delete.html", "/screenshots.html",
                     "/api/ad-material/playable-preview", "/api/fb-playable/preview",
                     "/ad-materials/published.png", "/api/voiceover-drama/tasks", "/tt"):
            self.assertFalse(retired_path(path), path)

    def test_legacy_navigation_moves_cleanup_before_pruning(self):
        source = [{"key": "ad_control", "module": "ad_control_center", "items": [
            {"key": "adControl", "module": "ad_control_center"},
            {"key": "fbPostAdDelete", "enabled": False, "module": "ad_control_center"}]},
            {"key": "ad_material", "items": []}, {"key": "ad_control_v3", "items": []},
            {"key": "ad_material_test", "items": []},
            {"key": "drama", "items": [{"key": "tasks", "href": "/drama-synthesis.html"}]}]
        result = filter_navigation(source)
        self.assertEqual([x["key"] for x in result], ["drama", "meta_asset_delete"])
        self.assertEqual(result[0], source[-1])
        self.assertFalse(result[1]["items"][0]["enabled"])
        self.assertEqual(result[1]["items"][0]["module"], "fb_ad_asset_delete")
        self.assertEqual(filter_navigation(result), result)
        self.assertEqual(source[0]["items"][1]["module"], "ad_control_center")

    def test_real_handler_checks_retirement_before_any_dispatch(self):
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8-sig"))
        handler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DramaMaterialHandler")
        methods = {n.name: n for n in handler.body if isinstance(n, ast.FunctionDef)}
        for name in ("do_GET", "do_POST", "do_PUT", "do_DELETE"):
            body = methods[name].body
            self.assertIsInstance(body[0], ast.Assign)
            self.assertEqual(ast.unparse(body[1].test), "retired_path(parsed.path)")
            self.assertIn("410", ast.unparse(body[1]))
            self.assertIsInstance(body[1].body[-1], ast.Return)
        recovery = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "recover_inflight_ad_material_tasks")
        self.assertIsInstance(recovery.body[0], ast.Return)

    def test_stale_runner_invocations_cannot_resume(self):
        for name in ("ad_control_rule_runner.py", "ad_control_v3_runner.py"):
            result = subprocess.check_output([sys.executable, str(ROOT / "scripts" / name)], text=True)
            self.assertEqual(json.loads(result), {"ok": True, "status": "retired", "meta_writes": 0})


if __name__ == "__main__":
    unittest.main()
