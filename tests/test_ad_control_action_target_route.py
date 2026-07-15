import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ActionTargetOwnerRouteTests(unittest.TestCase):
    def test_target_function_and_http_route_forward_owner(self):
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        target_function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "get_ad_control_action_targets"
        )
        self.assertIn(
            "owner_user_id", [argument.arg for argument in target_function.args.args]
        )
        fetch_call = next(
            node
            for node in ast.walk(target_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "fetch_ad_control_action"
        )
        self.assertIn("owner_user_id", [item.arg for item in fetch_call.keywords])

        do_get = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "do_GET"
        )
        route_call = next(
            node
            for node in ast.walk(do_get)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "get_ad_control_action_targets"
        )
        owner_keyword = next(
            item for item in route_call.keywords if item.arg == "owner_user_id"
        )
        self.assertIsInstance(owner_keyword.value, ast.Call)
        self.assertIsInstance(owner_keyword.value.func, ast.Name)
        self.assertEqual("ad_control_actor", owner_keyword.value.func.id)


if __name__ == "__main__":
    unittest.main()
