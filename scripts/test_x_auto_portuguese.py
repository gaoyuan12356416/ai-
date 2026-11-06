"""Portuguese catalog selection keeps the original task and deduplication."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_auto_posts.repositories import canonical_language
from scripts.test_x_auto_post_metrics import mysql_repo
from scripts.test_x_auto_post_selector import (
    FakeSource, FakeMetricStore, FakePublishedHistory, FakeStore, FakeValidator,
    drama, material, request, selector, NoEligibleMaterial,
)


class PortugueseTests(unittest.TestCase):
    def test_catalog_queries_accept_both_portuguese_spellings(self):
        for language in ("pt", "pt-br", "PT_BR"):
            with self.subTest(language=language):
                repository, conn = mysql_repo([])
                repository.list_drama_rows(language=language, now_epoch=200)
                sql, params = conn.cursor_value.executions[0]
                self.assertIn("LOWER(TRIM(i.language)) IN (%s,%s)", sql)
                self.assertEqual(params, (1479, 1, 3, "pt", "pt-br", 0, 200))
                repository, conn = mysql_repo([])
                repository.list_material_rows(content_id="C1", language=language)
                sql, params = conn.cursor_value.executions[0]
                self.assertIn("LOWER(TRIM(s.language)) IN (%s,%s)", sql)
                self.assertEqual(params, (6, "C1", "Dramawave", 2, 0, "pt", "pt-br", 0, 14400))

    def test_catalog_rows_normalize_without_changing_frozen_task(self):
        for source_language in ("pt", "pt-br"):
            repository, _ = mysql_repo([dict(source_row_id="1", content_id="C1",
                series_code="S1", language=source_language, resource_type_v2="1",
                deploy_time=100, app_id=1479, release_status=1, name="Drama", app="app")])
            rows = repository.list_drama_rows(language="pt-br", now_epoch=200)
            repository, _ = mysql_repo([dict(material_id="101", content_id="C1",
                language=source_language, product="Dramawave", material_type=2,
                is_delete=0, media_url="https://example.test/101.mp4", video_duration=30,
                data_source=6)])
            materials = repository.list_material_rows(content_id="C1", language="pt-br")
            source = FakeSource(rows, {"C1": materials})
            frozen = request(language="pt-br")
            winner = selector(source, FakeMetricStore(),
                validator=FakeValidator(overrides={"material_language": source_language})).preview(frozen)
            self.assertEqual(winner.material.source.material_id, "101")
            self.assertEqual(frozen.language, "pt-br")

    def test_portuguese_alias_keeps_global_history_deduplication(self):
        source = FakeSource([drama("C1", language="pt")], {
            "C1": [material("101", "C1", language="pt"), material("102", "C1", language="pt")]})
        store = FakeStore()
        selected = selector(source, FakeMetricStore(), history=FakePublishedHistory(["101"]),
            store=store, validator=FakeValidator(overrides={"material_language": "pt"}))
        winner = selected.select_and_reserve(request(language="pt-br"))
        self.assertEqual(winner.material.source.material_id, "102")
        with self.assertRaises(NoEligibleMaterial):
            selected.select_and_reserve(request(language="pt-br", task_id=12))
        self.assertEqual([row["material_id"] for row in store.reserve_calls], ["102"])

    def test_other_languages_and_portuguese_regions_stay_distinct(self):
        for language in ("pt-pt", "en", "en-us", "ja", "zh-tw"):
            self.assertEqual(canonical_language(language), language)
        source = FakeSource([drama("C1", language="es")], {"C1": [material("101", "C1", language="es")]})
        with self.assertRaises(NoEligibleMaterial):
            selector(source, FakeMetricStore()).preview(request(language="pt-br"))


if __name__ == "__main__":
    unittest.main()
