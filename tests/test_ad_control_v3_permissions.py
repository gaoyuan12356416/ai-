import ast
import json
import unittest
from pathlib import Path

from features.ad_control_v3 import routes


ROOT = Path(__file__).resolve().parents[1]


def _parse_json_text(value, default):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def load_permission_contract():
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    wanted_assignments = {
        "MODULE_PERMISSIONS",
        "DEFAULT_USER_PERMISSIONS",
        "ADMIN_PERMISSIONS",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in wanted_assignments
            for target in node.targets
        ):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "normalize_user_permissions":
            nodes.append(node)
    namespace = {"parse_json_text": _parse_json_text}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), namespace)
    return namespace


class AdControlV3PermissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = load_permission_contract()

    def test_v3_is_an_independent_assignable_module(self):
        modules = self.contract["MODULE_PERMISSIONS"]
        defaults = self.contract["DEFAULT_USER_PERMISSIONS"]
        self.assertEqual("AI自动规则调控（旧版）", modules["ad_control_center"])
        self.assertEqual("AI自动调控 V3", modules["ad_control_v3"])
        self.assertFalse(defaults["ad_control_center"])
        self.assertFalse(defaults["ad_control_v3"])

    def test_legacy_access_is_inherited_only_until_v3_is_explicit(self):
        normalize = self.contract["normalize_user_permissions"]
        inherited = normalize({"ad_control_center": True}, "user")
        self.assertTrue(inherited["ad_control_v3"])

        revoked = normalize(
            {"ad_control_center": True, "ad_control_v3": False},
            "user",
        )
        self.assertFalse(revoked["ad_control_v3"])

        granted = normalize(
            {"ad_control_center": False, "ad_control_v3": True},
            "user",
        )
        self.assertTrue(granted["ad_control_v3"])

    def test_admin_and_new_users_receive_safe_defaults(self):
        normalize = self.contract["normalize_user_permissions"]
        self.assertFalse(normalize({}, "user")["ad_control_v3"])
        self.assertTrue(normalize({}, "admin")["ad_control_v3"])

    def test_navigation_and_routes_use_the_v3_permission(self):
        navigation = json.loads(
            (ROOT / "static" / "navigation.json").read_text(encoding="utf-8")
        )
        groups = {group["key"]: group for group in navigation}
        self.assertEqual("ad_control_center", groups["ad_control"]["module"])
        self.assertEqual("ad_control_v3", groups["ad_control_v3"]["module"])
        self.assertTrue(
            all(
                item["module"] == "ad_control_v3"
                for item in groups["ad_control_v3"]["items"]
            )
        )
        self.assertEqual("ad_control_v3", routes.MODULE_KEY)

    def test_user_management_merges_backend_modules(self):
        source = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function mergeBackendPermissionModules(modules)", source)
        self.assertIn("modules = mergeBackendPermissionModules(modules);", source)


if __name__ == "__main__":
    unittest.main()
