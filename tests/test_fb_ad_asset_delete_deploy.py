import ast
import unittest
from scripts.deploy_meta_asset_delete import legacy_disabled


class RollbackTests(unittest.TestCase):
    def test_baseline_rollback_closes_all_legacy_mutations(self):
        baseline = '\n'.join('def %s(*args):\n    return "legacy"\n' % name for name in
            ("fb_post_ad_delete_create_preview", "fb_post_ad_delete_start", "fb_post_ad_delete_run", "read_history"))
        tree = ast.parse(legacy_disabled(baseline))
        names = {"fb_post_ad_delete_create_preview", "fb_post_ad_delete_start", "fb_post_ad_delete_run"}
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        self.assertEqual(3, len(functions))
        self.assertTrue(all(isinstance(n.body[0], ast.Raise) for n in functions))


if __name__ == "__main__":
    unittest.main()
