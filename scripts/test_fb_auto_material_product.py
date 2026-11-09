"""Source-product selection must not change target-app or attribution scope."""
import copy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_auto_posts.core import ActorScope, FBAutoPostStore
from features.fb_auto_posts.metrics import MetricTotals, MetricWindow
from features.fb_auto_posts.repositories import MaterialRepository, PageGroup, RepositoryError
from features.fb_auto_posts.service import Runtime
from features.fb_auto_posts.validation import normalize_template_payload, ValidationError


def payload():
    rule = dict(spend_min=None, spend_max=None, roas_min=None, roas_max=None,
                sort_by="spend", sort_direction="desc")
    return dict(name="Source selection", group_ids=["6"], message_template="{{desc}} {{url}}",
                video_template="random_overlay", material_product="DramawaveSource",
                drama_rule={**rule, "resource_type_v2": []},
                material_rule={**rule, "duration_min_seconds": 300, "duration_max_seconds": 6000},
                schedule={"mode": "fixed", "times": ["10:30"]})


TARGET = dict(app_id="1479", product="Dramawave", material_data_source=6,
              metric_product="Dramawave", metric_platform=0)


class Source:
    schema = "kunlunads_dev"
    blacklist_schema = "ads_setting"

    def __init__(self):
        self.calls = []
        self.product = "DramawaveSource"

    def select(self, sql, params):
        self.calls.append((sql, tuple(params)))
        if "ads_custom_source" in sql:
            return [dict(material_id="10", content_id="drama", media_url="https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/example.mp4",
                         material_name="source", language="en", video_duration=600,
                         data_source=6, type=2, is_delete=0, product=self.product)]
        if "ads_drama_resource" in sql:
            return [dict(content_id="drama", drama_description="Description", description_count=1)]
        if "ads_facebook_post_blacklist" in sql:
            return []
        return [dict(content_id="drama", drama_name="Drama", language="en", resource_type_v2="1")]


class Metrics:
    def __init__(self):
        self.calls = []

    def load_metric_window(self, **kwargs):
        self.calls.append(kwargs)
        return MetricWindow((1,), ("2026-11-08",),
                            {"drama": MetricTotals(Decimal(10), Decimal(1))}, {})


class ProductTests(unittest.TestCase):
    def config(self):
        return {**normalize_template_payload(payload()), **TARGET, "language": "en"}

    def test_saved_product_survives_server_target_resolution_and_edit(self):
        actor = ActorScope("test", "Test", True, "248")
        group = PageGroup("6", "248", 0, "Test", "1479", "Dramawave", 1, 1)
        runtime = SimpleNamespace(pages=SimpleNamespace(list_groups=lambda **kw: [group]))
        resolved = Runtime.resolve_source(runtime, payload(), actor)
        self.assertEqual(resolved, TARGET)
        with tempfile.TemporaryDirectory() as folder:
            store = FBAutoPostStore(Path(folder) / "test.sqlite3")
            created = store.create_template(payload(), actor, resolved)
            saved = store.get_template(created["id"], actor)
            self.assertEqual(saved["config"]["material_product"], "DramawaveSource")
            self.assertEqual({k: saved["config"][k] for k in TARGET}, TARGET)
            edited = copy.deepcopy(payload()); edited["material_product"] = " Another Product "
            saved = store.update_template(created["id"], edited, actor, 1, resolved)
            self.assertEqual(saved["config"]["material_product"], "Another Product")
            self.assertEqual(saved["status"], "disabled")
            with store.connect() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0], 0)

    def test_legacy_payload_stays_unchanged_and_explicit_name_is_validated(self):
        raw = payload(); raw.pop("material_product")
        self.assertNotIn("material_product", normalize_template_payload(raw))
        for value in (None, "", "  ", [], 5, "x" * 129, "name\nother"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                normalize_template_payload({**raw, "material_product": value})

    def test_keyset_source_product_and_target_metadata_remain_separate(self):
        mysql, metrics = Source(), Metrics()
        result = MaterialRepository(mysql, metrics).candidate_snapshot(self.config())
        self.assertEqual([m.material_id for m in result.candidates], ["10"])
        source = next(params for sql, params in mysql.calls if "ads_custom_source" in sql)
        self.assertEqual(source[1], "DramawaveSource")
        self.assertEqual(source[3], "1479")
        self.assertEqual(metrics.calls[0]["product"], "Dramawave")
        for sql, params in mysql.calls:
            if "ads_drama_resource" in sql or "JOIN (SELECT d0.content_id" in sql:
                self.assertEqual(params[0], "1479")

    def test_priority_path_filters_the_same_source_product(self):
        mysql = Source()
        result = MaterialRepository(mysql, Metrics(), candidate_limit=1,
                    metric_prefilter_min_content_ids=1).candidate_snapshot(self.config())
        self.assertEqual(len(result.candidates), 1)
        calls = [(sql, p) for sql, p in mysql.calls if "ads_custom_source" in sql]
        self.assertEqual(len(calls), 1)
        self.assertIn("idx_source_type_source_id", calls[0][0])
        self.assertEqual(calls[0][1][2], "DramawaveSource")

    def test_legacy_candidates_keep_target_product(self):
        config = self.config(); config.pop("material_product")
        mysql = Source()
        MaterialRepository(mysql, Metrics()).candidate_snapshot(config)
        self.assertEqual(mysql.calls[0][1][1], "Dramawave")

    def test_exact_material_uses_selected_product_and_preserves_target_mapping(self):
        mysql = Source()
        repo = MaterialRepository(mysql, Metrics())
        self.assertEqual(repo.exact_material(self.config(), "10").material_id, "10")
        mysql.product = "Dramawave"
        with self.assertRaises(RepositoryError) as caught:
            repo.exact_material(self.config(), "10")
        self.assertEqual(caught.exception.code, "fb_auto_material_invalid")


if __name__ == "__main__":
    unittest.main()
