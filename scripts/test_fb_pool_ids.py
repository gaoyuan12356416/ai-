"""Offline source-ID resolution and partial pool admission contracts."""
import copy
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_fb_video_publish_core import Fixture
from features.fb_video_publish.core import DomainError
from features.fb_video_publish.query_broker import _validate_sources
from features.fb_video_publish.service import PREFIX
from features.fb_video_publish.sources import SourceError, SourceRepository


def raw(mid, **changes):
    return dict(dict(material_id=mid, content_id="content-" + mid, material_name="Video " + mid,
                     material_tag="", language="en", video_duration=32,
                     media_url="https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/" + mid + ".mp4",
                     thumbnail_url="", uploader_id="789", data_source="6", product="Dramawave", type=2,
                     is_delete=0, drama_released=1, content_blocked=0, series_blocked=0), **changes)


class Catalog:
    schema, blacklist_schema = "kunlunads_dev", "ads_setting"

    def __init__(self, eligible, invalid=None):
        self.rows, self.invalid = eligible, invalid or {}
        self.calls, self.last = [], []
        self.bad_name, self.bad_desc = set(), set()

    def select(self, sql, params):
        _validate_sources(sql)
        self.calls.append((sql, params))
        if "ads_custom_source" in sql:
            if "drama_released" in sql:
                return [self.invalid[mid] for mid in params[3:-1] if mid in self.invalid]
            self.last = [copy.deepcopy(self.rows[mid]) for mid in reversed(params[5:-1]) if mid in self.rows]
            return self.last
        if "ads_drama_resource" in sql:
            return [dict(content_id=r["content_id"], language=r["language"], drama_description="Description")
                    for r in self.last if r["material_id"] not in self.bad_desc]
        return [dict(content_id=r["content_id"], language=r["language"], drama_name="Drama")
                for r in self.last if r["material_id"] not in self.bad_name]

    def source(self):
        return SourceRepository(None, SimpleNamespace(mysql=self),
                                now_fn=lambda: datetime(2026, 10, 21, tzinfo=timezone.utc))


class SourceResolutionTests(unittest.TestCase):
    def test_bounded_200_exact_ids_preserve_input_order_and_all_filters(self):
        wanted = [str(i) for i in range(200, 0, -1)]
        catalog = Catalog({mid: raw(mid) for mid in wanted})
        result = catalog.source().resolve_materials(wanted)
        self.assertEqual(wanted, list(result["items"]))
        self.assertEqual({}, result["errors"])
        queries = [(sql, params) for sql, params in catalog.calls if "ads_custom_source" in sql]
        self.assertEqual(2, len(queries))
        self.assertEqual(6, len(catalog.calls))
        for sql, params in queries:
            self.assertEqual(100, len(params[5:-1]))
            for guard in ("s.data_source=%s", "s.product=%s", "s.type=2", "s.is_delete=0",
                          "s.video_duration<=3600", "d.release_status=1", "ads_facebook_post_blacklist",
                          "AND s.id IN (", "FORCE INDEX(PRIMARY)"):
                self.assertIn(guard, sql)
            self.assertEqual(["6", "Dramawave", "1479"], params[:3])

    def test_partial_source_reasons_and_metadata_are_specific(self):
        invalid = {
            "2": raw("2", is_delete=1), "3": raw("3", product="Other"),
            "4": raw("4", data_source="7"), "5": raw("5", type=1),
            "6": raw("6", language=""), "7": raw("7", video_duration=3601),
            "8": raw("8", content_blocked=1), "9": raw("9", drama_released=0),
        }
        catalog = Catalog({"1": raw("1"), "10": raw("10", media_url="https://evil.test/a.mp4"),
                           "11": raw("11")}, invalid)
        catalog.bad_name.add("11")
        wanted = [str(i) for i in range(1, 13)]
        result = catalog.source().resolve_materials(wanted)
        self.assertEqual(["1"], list(result["items"]))
        expected = ["material_deleted", "material_product_mismatch", "material_source_mismatch",
                    "material_not_video", "material_metadata_missing", "material_duration_invalid",
                    "material_blacklisted", "material_not_released", "material_unavailable",
                    "material_metadata_ambiguous", "material_not_found"]
        self.assertEqual(expected, [r["code"] for r in result["errors"].values()])
        diagnostics = [(sql, params) for sql, params in catalog.calls if "drama_released" in sql]
        self.assertEqual(1, len(diagnostics))
        self.assertEqual(["2", "3", "4", "5", "6", "7", "8", "9", "12"], diagnostics[0][1][3:-1])

    def test_need_desc_rejects_only_rows_with_missing_description(self):
        catalog = Catalog({mid: raw(mid) for mid in ("1", "2")})
        catalog.bad_desc.add("2")
        self.assertEqual(["1", "2"], list(catalog.source().resolve_materials(["1", "2"])["items"]))
        result = catalog.source().resolve_materials(["1", "2"], need_desc=True)
        self.assertEqual(["1"], list(result["items"]))
        self.assertEqual("material_metadata_ambiguous", result["errors"]["2"]["code"])

    def test_database_failures_stay_global_and_strict_adapter_stays_strict(self):
        catalog = Catalog({"1": raw("1")})
        with self.assertRaises(SourceError):
            catalog.source().get_materials(["1", "2"])
        catalog.select = Mock(side_effect=RuntimeError("offline simulated database failure"))
        with self.assertRaises(RuntimeError):
            catalog.source().resolve_materials(["1", "2"])

    def test_duplicate_or_unrequested_source_ids_are_global_errors(self):
        catalog = Catalog({})
        catalog.select = Mock(return_value=[raw("1"), raw("1")])
        with self.assertRaises(SourceError) as raised:
            catalog.source().resolve_materials(["1"])
        self.assertEqual(503, raised.exception.status)


class PoolIDTests(Fixture):
    def setUp(self):
        super().setUp()
        def resolve(ids, need_desc=False, *, enforce_blacklist=True):
            return {"items": copy.deepcopy({mid: self.sources.materials[mid] for mid in ids if mid in self.sources.materials}),
                    "errors": {mid: {"code": "material_not_found", "message": "素材ID不存在"}
                               for mid in ids if mid not in self.sources.materials}}
        self.sources.resolve_materials = Mock(side_effect=resolve)

    def call(self, suffix="", payload=None, method="POST"):
        return self.service.handle(method, PREFIX + "/pages/11/pool" + suffix, self.actor, payload)

    def base(self):
        pool = self.call(method="GET")["pool"]
        return {"config_instance_id": pool["config_instance_id"], "expected_version": pool["version"]}

    def resolve(self, ids):
        base = self.base()
        return self.call("/items/resolve", {**base, "material_ids": ids}), base

    def add_resolved(self, result, base, repeat=False):
        payload = {**base, "material_ids": result["material_ids"], "preview_token": result["preview_token"],
                   "operation_id": uuid.uuid4().hex, "allow_repeat": repeat}
        return self.call("/items", payload), payload

    def counts(self):
        with self.store.connect() as conn:
            return {name: conn.execute("SELECT COUNT(*) FROM fb_video_" + name).fetchone()[0]
                    for name in ("pool", "pool_item", "pool_operation", "task", "link", "batch")}

    def test_whitespace_duplicates_invalid_ids_and_order_with_readonly_preview(self):
        before = self.counts()
        result, base = self.resolve([" 22 ", "", "21", "22", "garbage", "999", "  "])
        self.assertEqual(before, self.counts())
        self.assertEqual(["22", "21"], result["material_ids"])
        self.assertEqual({"total": 5, "valid": 2, "skipped": 3}, result["summary"])
        self.assertEqual([4, 5, 6], [r["row_number"] for r in result["skipped"]])
        self.assertEqual(["duplicate_material_id", "invalid_material_id", "material_not_found"],
                         [r["code"] for r in result["skipped"]])
        self.sources.resolve_materials.assert_called_once_with(["22", "21", "999"], need_desc=False, enforce_blacklist=False)
        added, payload = self.add_resolved(result, base)
        self.assertEqual(["22", "21"], [r["material_id"] for r in self.call(method="GET")["items"]])
        replay = self.call("/items", payload)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(added["items"], replay["items"])
        self.assertEqual(1, self.counts()["pool_operation"])

    def test_language_existing_pending_and_macro_errors_are_individual(self):
        first, base = self.resolve(["21"])
        self.add_resolved(first, base)
        self.sources.materials["23"] = dict(self.sources.materials["22"], material_id="23", language="en")
        self.sources.materials["24"] = dict(self.sources.materials["22"], material_id="24", drama_description="")
        result, base = self.resolve(["21", "23", "24", "22"])
        self.assertEqual(["22"], result["material_ids"])
        self.assertEqual(["pool_material_exists", "language_mismatch", "macro_source_missing"],
                         [r["code"] for r in result["skipped"]])
        self.add_resolved(result, base)
        self.assertEqual(["21", "22"], [r["material_id"] for r in self.call(method="GET")["items"]])

    def test_active_pair_and_page_unknown_fence_remain_in_force(self):
        self.submit()
        result, _ = self.resolve(["21", "22"])
        self.assertEqual(["22"], result["material_ids"])
        self.assertEqual("fb_video_material_active", result["skipped"][0]["code"])
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_video_task SET status='unknown',unknown_outcome=1")
        result, _ = self.resolve(["21", "22"])
        self.assertEqual([], result["material_ids"])
        self.assertEqual("", result["preview_token"])
        self.assertEqual({"fb_video_page_unknown"}, {r["code"] for r in result["skipped"]})

    def test_history_requires_explicit_confirmation(self):
        self.submit()
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_video_task SET status='published',graph_post_id='11_1001'")
        result, base = self.resolve(["21", "22"])
        self.assertTrue(result["repeat_required"])
        self.assertEqual([True, False], [r["repeat_required"] for r in result["items"]])
        with self.assertRaises(DomainError):
            self.add_resolved(result, base)
        self.assertEqual(0, self.counts()["pool_item"])
        self.add_resolved(result, base, repeat=True)
        self.assertEqual(2, self.counts()["pool_item"])

    def test_config_change_or_new_conflict_invalidates_proof_without_partial_write(self):
        result, base = self.resolve(["21", "22"])
        config = self.store.configs(self.actor)["11"]
        self.store.save_configs(self.actor, ["11"], "zh", "Updated {name}",
                                expected=(config["config_instance_id"], config["version"]))
        with self.assertRaises(DomainError):
            self.add_resolved(result, base)
        self.assertEqual(0, self.counts()["pool_item"])
        result, base = self.resolve(["21", "22"])
        self.submit()
        with self.assertRaises(DomainError):
            self.add_resolved(result, base)
        self.assertEqual(0, self.counts()["pool_item"])

    def test_all_invalid_and_global_source_failure_have_no_side_effects(self):
        before = self.counts()
        result, _ = self.resolve(["bad", "999"])
        self.assertEqual({"total": 2, "valid": 0, "skipped": 2}, result["summary"])
        self.assertEqual("", result["preview_token"])
        self.sources.resolve_materials.side_effect = RuntimeError("offline database error")
        with self.assertRaises(RuntimeError):
            self.resolve(["21"])
        self.assertEqual(before, self.counts())

    def test_request_limit_applies_before_source_query(self):
        for values in ([], ["21"] * 201, "21\n22"):
            with self.subTest(values=str(values)[:20]), self.assertRaises(DomainError):
                self.resolve(values)
        self.sources.resolve_materials.assert_not_called()


if __name__ == "__main__":
    unittest.main()
