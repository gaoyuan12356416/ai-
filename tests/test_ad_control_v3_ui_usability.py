import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]

from features.ad_control_v3.catalog import StaticOptimizerIdentityResolver
from features.ad_control_v3.channels.facebook import FacebookAdapter
from features.ad_control_v3.errors import AdControlV3Error
from features.ad_control_v3.repository import MemoryRepository
from features.ad_control_v3.service import Service
from features.ad_control_v3.storage import MemorySnapshotStore
from scripts.sync_ad_control_v3_delivery_products import build_catalog_entries, plan_hash


class DeliveryProductCatalogTests(unittest.TestCase):
    def test_same_platform_app_id_becomes_specific_app_and_w2a_selectors(self):
        rows = [
            {"id": 1479, "name": "Dramawave", "app_id": "1031273318485141", "landing_id": 0, "landing_app": ""},
            {"id": 2477, "name": "W2aPage[1723]", "app_id": "1031273318485141", "landing_id": 1723, "landing_app": '{"657":"drama-double"}'},
        ]
        entries = build_catalog_entries(
            rows,
            platform_app_id="1031273318485141",
            canonical_product="Dramawave",
            insight_products=["Dramawave"],
        )
        self.assertEqual(["app:1479", "w2a:1723"], [item["product_value"] for item in entries])
        app_scope = entries[0]["evidence"]["scope"]
        w2a_scope = entries[1]["evidence"]["scope"]
        self.assertEqual(["Dramawave"], app_scope["insight_app_ids"])
        self.assertEqual([1723], w2a_scope["w2a_page_ids"])
        self.assertEqual(["[w2a]drama-double"], w2a_scope["insight_app_ids"])
        rebuilt = build_catalog_entries(
            list(reversed(rows)),
            platform_app_id="1031273318485141",
            canonical_product="Dramawave",
            insight_products=["Dramawave"],
        )
        self.assertEqual(plan_hash(entries), plan_hash(rebuilt))

    def test_source_row_with_other_app_id_is_rejected(self):
        with self.assertRaises(ValueError):
            build_catalog_entries(
                [{"id": 1, "name": "other", "app_id": "wrong", "landing_id": 0}],
                platform_app_id="1031273318485141",
                canonical_product="Dramawave",
                insight_products=["Dramawave"],
            )


class DeliveryProductDiscoveryTests(unittest.TestCase):
    def test_specific_w2a_selector_uses_bounded_product_and_page_predicates(self):
        calls = []

        def query(sql, params):
            calls.append((sql, tuple(params)))
            return [{
                "ad_account_id": "123",
                "object_id": "c-1",
                "campaign_id": "c-1",
                "adset_id": "",
                "ad_id": "",
                "campaign_parent_count": 1,
                "adset_parent_count": 0,
                "product": "",
                "optimizer_id": 248,
                "_scope_app_id": "[w2a]drama-double",
                "_scope_app_id_count": 1,
                "_scope_app_id_concat_bytes": 20,
                "_scope_w2a_page_id": "1723",
                "_scope_w2a_page_id_count": 1,
                "_scope_w2a_page_id_concat_bytes": 4,
                "context_concat_limit": 1024,
                "account_timezone": "",
                "settings_row_count": 0,
                "settings_timezone_count": 0,
            }]

        adapter = FacebookAdapter(query)
        rows = adapter.discover({
            "object_level": "campaign",
            "products": ["w2a:1723"],
            "delivery_product_scopes": [{
                "product_value": "w2a:1723",
                "insight_products": ["Dramawave"],
                "insight_app_ids": ["[w2a]drama-double"],
                "w2a_page_ids": [1723],
            }],
            "optimizer_id": 248,
            "date_from": "2026-07-17",
            "date_to": "2026-07-17",
            "account_timezones": [],
            "required_fields": [],
        })
        self.assertEqual("w2a:1723", rows[0]["product"])
        self.assertEqual("123", rows[0]["ad_account_id"])
        sql, params = calls[0]
        self.assertIn("FORCE INDEX (dpdo)", sql)
        self.assertIn("s.product IN (%s)", sql)
        self.assertIn("CAST(s.product AS BINARY) IN (%s)", sql)
        self.assertIn("s.w2a_page_id IN (%s)", sql)
        self.assertIn(1723, params)
        self.assertNotIn("W2aPage", sql)

    def test_service_resolves_catalog_evidence_without_exposing_internal_scope(self):
        products = [
            {"channel": "facebook", "product_value": "Dramawave", "canonical_product": "Dramawave", "product_type": "short_drama", "evidence": {}, "enabled": True},
            {"channel": "facebook", "product_value": "w2a:1723", "canonical_product": "Dramawave", "product_type": "short_drama", "enabled": True, "evidence": {
                "catalog_kind": "delivery_product",
                "scope": {"insight_products": ["Dramawave"], "insight_app_ids": ["[w2a]drama-double"], "w2a_page_ids": [1723]},
            }},
        ]

        def query(_sql, _params):
            return [{
                "ad_account_id": "123", "object_id": "c-1", "campaign_id": "c-1", "adset_id": "", "ad_id": "",
                "campaign_parent_count": 1, "adset_parent_count": 0, "product": "", "optimizer_id": 248,
                "_scope_app_id": "[w2a]drama-double", "_scope_app_id_count": 1, "_scope_app_id_concat_bytes": 20,
                "_scope_w2a_page_id": "1723", "_scope_w2a_page_id_count": 1, "_scope_w2a_page_id_concat_bytes": 4,
                "context_concat_limit": 1024, "account_timezone": "", "settings_row_count": 0, "settings_timezone_count": 0,
            }]

        service = Service(
            MemoryRepository(products),
            {"facebook": FacebookAdapter(query)},
            StaticOptimizerIdentityResolver({"user": [248]}, [{"optimizer_id": 248, "name": "Owner"}]),
            MemorySnapshotStore(),
        )
        result = service.scope_estimate({"user_id": "user", "name": "Owner"}, {
            "channel": "facebook", "object_level": "campaign", "products": ["w2a:1723"],
            "optimizer_id": 248, "account_timezones": [], "metric_window_days": 1,
        })
        self.assertEqual(1, result["eligible_object_count"])
        self.assertNotIn("delivery_product_scopes", result["scope"])

    def test_adapter_rejects_non_list_catalog_scope_as_service_error(self):
        adapter = FacebookAdapter(lambda _sql, _params: [])
        with self.assertRaises(AdControlV3Error) as caught:
            adapter.discover({
                "object_level": "campaign",
                "products": ["w2a:1723"],
                "delivery_product_scopes": [{
                    "product_value": "w2a:1723",
                    "insight_products": "Dramawave",
                    "insight_app_ids": [],
                    "w2a_page_ids": [1723],
                }],
                "optimizer_id": 248,
                "date_from": "2026-07-17",
                "date_to": "2026-07-17",
                "account_timezones": [],
            })
        self.assertEqual("product_catalog_invalid", caught.exception.code)
        self.assertEqual(503, caught.exception.status)

    def test_broad_and_specific_product_overlap_is_rejected_before_query(self):
        calls = []
        adapter = FacebookAdapter(lambda sql, params: calls.append((sql, params)) or [])
        with self.assertRaises(AdControlV3Error) as caught:
            adapter.discover({
                "object_level": "campaign",
                "products": ["Dramawave", "w2a:1723"],
                "delivery_product_scopes": [{
                    "product_value": "w2a:1723",
                    "insight_products": ["Dramawave"],
                    "insight_app_ids": ["[w2a]drama-double"],
                    "w2a_page_ids": [1723],
                }],
                "optimizer_id": 248,
                "date_from": "2026-07-17",
                "date_to": "2026-07-17",
                "account_timezones": [],
            })
        self.assertEqual("overlapping_product_scope", caught.exception.code)
        self.assertEqual([], calls)

    def test_service_catalog_validation_never_blames_user_input(self):
        products = [
            {"channel": "facebook", "product_value": "Dramawave", "product_type": "short_drama", "enabled": True, "evidence": {}},
            {"channel": "facebook", "product_value": "w2a:1723", "product_type": "short_drama", "enabled": True, "evidence": {
                "catalog_kind": "delivery_product",
                "scope": {"insight_products": "Dramawave", "w2a_page_ids": [1723]},
            }},
        ]
        service = Service(
            MemoryRepository(products),
            {"facebook": FacebookAdapter(lambda _sql, _params: [])},
            StaticOptimizerIdentityResolver({"user": [248]}, [{"optimizer_id": 248, "name": "Owner"}]),
            MemorySnapshotStore(),
        )
        with self.assertRaises(AdControlV3Error) as caught:
            service.scope_estimate({"user_id": "user"}, {
                "channel": "facebook", "object_level": "campaign", "products": ["w2a:1723"],
                "optimizer_id": 248, "account_timezones": [], "metric_window_days": 1,
            })
        self.assertEqual("product_catalog_invalid", caught.exception.code)
        self.assertEqual(503, caught.exception.status)


class MetaCacheTests(unittest.TestCase):
    def test_shared_meta_loaders_are_cached_and_results_are_copied(self):
        class CountingRepository(MemoryRepository):
            def __init__(self):
                super().__init__([{
                    "channel": "facebook", "product_value": "Dramawave",
                    "canonical_product": "Dramawave", "product_type": "short_drama", "enabled": True,
                }])
                self.product_calls = 0

            def list_products(self, channel, *, include_disabled=False):
                self.product_calls += 1
                return super().list_products(channel, include_disabled=include_disabled)

        repository = CountingRepository()
        timezone_calls = []
        resolver = StaticOptimizerIdentityResolver(
            {"admin": [248]},
            [{"optimizer_id": 248, "name": "Owner", "email": "owner@example.com"}],
        )
        service = Service(
            repository,
            {"facebook": FacebookAdapter(lambda _sql, _params: [])},
            resolver,
            MemorySnapshotStore(),
            timezone_loader=lambda: timezone_calls.append(1) or ["UTC"],
            meta_cache_ttl_seconds=60,
        )
        actor = {"user_id": "admin", "role": "admin"}
        first = service.meta(actor)
        first["products"][0]["product_value"] = "mutated"
        second = service.meta(actor)
        self.assertEqual("Dramawave", second["products"][0]["product_value"])
        self.assertEqual(1, repository.product_calls)
        self.assertEqual(1, len(timezone_calls))


class UiContractTests(unittest.TestCase):
    def test_search_estimate_and_save_preview_contract(self):
        source = (ROOT / "features" / "ad_control_v3" / "assets" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "features" / "ad_control_v3" / "assets" / "app.css").read_text(encoding="utf-8")
        scope_block = source[source.index("function renderScopeStep"):source.index("function renderMultiSelect")]
        object_block = source[source.index("function renderObjectStep"):source.index("function levelChoice")]
        preview_block = source[source.index("async function previewGroup"):source.index("async function executeGroup")]
        self.assertIn("renderSearchableSingle(\"editor-optimizer\"", scope_block)
        self.assertNotIn('<select aria-label="优化师"', scope_block)
        self.assertNotIn("结构范围估算", scope_block)
        self.assertIn("结构范围估算", object_block)
        self.assertNotIn("当前层级能力", object_block)
        self.assertIn("state.editor = null", preview_block)
        self.assertIn("await loadRuleGroups()", preview_block)
        self.assertIn("if (state.editor == null) renderRuleGroupShell()", preview_block)
        self.assertIn("Promise.all([loadSharedShell(), api(\"/meta\")])", source)
        self.assertIn('renderSearchableSingle("rule-optimizer"', source)
        self.assertIn('renderSearchableSingle("log-optimizer"', source)
        self.assertIn("function layoutOpenMenus()", source)
        self.assertIn('window.addEventListener("scroll", scheduleOpenMenuLayout, true)', source)
        self.assertIn('card.classList.add("has-open-menu")', source)
        self.assertIn('menu.classList.toggle("is-upward", openUpward)', source)
        self.assertIn('menu.style.setProperty("--menu-available-height"', source)
        self.assertIn(".section-card.has-open-menu", styles)
        self.assertIn(".multi-menu.is-upward", styles)
        self.assertIn("max-height: var(--menu-available-height, 300px)", styles)


if __name__ == "__main__":
    unittest.main()
