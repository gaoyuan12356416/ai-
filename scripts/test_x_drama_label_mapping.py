#!/usr/bin/env python3
"""Regression coverage for tag-only drift, on both deployed selector variants."""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from test_x_post_material_pool_selector import PoolConnection, drama_row, material_row, pool_item


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULES = [
    load(ROOT / "features/x_posts/selector.py", "release_selector"),
    load(ROOT / "deploy/x-drama-label-mapping/main-api-selector.py", "main_selector"),
]


class LabelMappingTests(unittest.TestCase):
    def test_label_presence_order_case_and_duplicates_do_not_split_identity(self):
        for module in MODULES:
            for second in ["Fantasy,Counterattack,BL", "Counterattack,Fantasy", "fantasy, FANTASY,Counterattack"]:
                with self.subTest(module=module.__name__, labels=second):
                    connection = PoolConnection([8])
                    connection.drama_rows["8"] = [drama_row(8), drama_row(8, drama_labels=second)]
                    selected, rejected = module.select_pool_candidates(
                        connection, [pool_item(1, 8, "2026-09-08T00:00:00Z")], "2026-09-08", limit=1)
                    self.assertEqual(rejected, [])
                    self.assertEqual(selected[0]["material_id"], "8")
                    self.assertEqual(selected[0]["tag"], "Fantasy")

    def test_true_mapping_conflicts_still_fail_closed(self):
        for module in MODULES:
            for field, value in [("content_id", "wrong"), ("series_code", "S999"),
                                 ("language", "ja"), ("drama_name", "Other drama"),
                                 ("drama_description", "A different description.")]:
                with self.subTest(module=module.__name__, field=field):
                    connection = PoolConnection([8])
                    connection.drama_rows["8"] = [drama_row(8), drama_row(8, **{field: value})]
                    selected, rejected = module.select_pool_candidates(
                        connection, [pool_item(1, 8, "2026-09-08T00:00:00Z")], "2026-09-08", limit=1)
                    self.assertEqual(selected, [])
                    self.assertIn(rejected[0]["error_code"], ["drama_mapping_ambiguous", "drama_mapping_invalid"])

    def test_missing_labels_remain_invalid(self):
        for module in MODULES:
            for value in ["", ", ,", None]:
                with self.subTest(module=module.__name__, labels=value):
                    connection = PoolConnection([8])
                    connection.drama_rows["8"] = [drama_row(8), drama_row(8, drama_labels=value)]
                    selected, rejected = module.select_pool_candidates(
                        connection, [pool_item(1, 8, "2026-09-08T00:00:00Z")], "2026-09-08", limit=1)
                    self.assertEqual(selected, [])
                    self.assertEqual(rejected[0]["error_code"], "drama_mapping_invalid")

    def test_legacy_selection_accepts_label_drift_but_rejects_identity_drift(self):
        for module in MODULES:
            connection = PoolConnection([8])
            connection.drama_rows["8"] = [drama_row(8), drama_row(8, drama_labels="Counterattack,Fantasy,BL")]
            row = material_row(8, series_count=1, drama_language_count=1,
                               insight_content_id_count=1, series_code="S8",
                               drama_language="en", insight_content_id="C8", spend=1)
            selector = module.DramawaveCandidateSelector(connection)
            self.assertEqual(selector._candidate(row, "2026-09-08")["tag"], "Fantasy")
            connection.drama_rows["8"][1]["drama_description"] = "Conflicting description"
            with self.assertRaises(module.CandidateSelectionError):
                selector._candidate(row, "2026-09-08")


if __name__ == "__main__":
    unittest.main()
