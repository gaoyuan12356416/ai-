#!/usr/bin/env python3
"""Static contract tests for the standalone TT publish-log page."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
DEPLOY = ROOT / "deploy"


class TTPublishLogsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (STATIC / "tt-publish-logs.html").read_text(encoding="utf-8")
        cls.js = (STATIC / "tt-publish-logs.js").read_text(encoding="utf-8")
        cls.pool = (STATIC / "tt-post-pool.html").read_text(encoding="utf-8")
        cls.quick_nav = (STATIC / "quick-nav.js").read_text(encoding="utf-8")
        cls.navigation = json.loads((STATIC / "navigation.json").read_text(encoding="utf-8"))
        cls.nginx = (DEPLOY / "nginx-tt-auto-publish.conf").read_text(
            encoding="utf-8"
        )

    def test_page_exposes_source_trigger_filters_and_task_columns(self):
        for text in (
            "TT 发布日志",
            "发布来源",
            "素材池发布",
            "自动发布",
            "触发方式",
            "自动定时",
            "手动执行",
            "素材 / Drama ID",
            "4位码",
        ):
            self.assertIn(text, self.html)
        self.assertIn("/publish-logs", self.js)
        self.assertIn("publish_source", self.js)
        self.assertIn("trigger_type", self.js)
        self.assertIn("displayCode(item.code)", self.js)
        self.assertIn("/^[A-Z0-9]{4}$/", self.js)
        self.assertNotIn('String(value || "").trim()', self.js)
        self.assertNotIn("toUpperCase()", self.js)
        self.assertIn('colspan="11"', self.html)
        self.assertIn("cell.colSpan = 11", self.js)

    def test_page_preserves_legacy_actions_and_auto_run_details(self):
        self.assertIn("/queue/${encodeURIComponent(item.task_id)}/${action}", self.js)
        self.assertIn("/events?${ui.queryString({ queue_id: item.task_id })}", self.js)
        self.assertIn("/runs/${encodeURIComponent(item.run_id)}", self.js)
        self.assertIn('activeKey: "ttAutoPublishRuns"', self.js)
        self.assertIn("force_close_allowed", self.js)
        self.assertIn("强制关闭", self.js)
        self.assertIn("/force-close", self.js)

    def test_old_pool_no_longer_renders_or_requests_publish_log(self):
        self.assertNotIn("<h2>发布任务</h2>", self.pool)
        self.assertNotIn('id="queueFilters"', self.pool)
        self.assertNotIn('id="queueRows"', self.pool)
        self.assertNotIn("/tasks?", self.pool)
        self.assertIn("/tt-publish-logs.html?publish_source=material_pool", self.pool)

    def test_navigation_points_to_unified_log_page(self):
        groups = {group["key"]: group for group in self.navigation}
        item = next(
            value
            for value in groups["tiktok_platform"]["items"]
            if value["key"] == "ttAutoPublishRuns"
        )
        self.assertEqual(item["label"], "TT 发布日志")
        self.assertEqual(item["href"], "/tt-publish-logs.html")
        self.assertIn('ttAutoPublishRuns: "/tt-publish-logs.html"', self.quick_nav)

    def test_nginx_serves_unified_log_page_without_cache(self):
        self.assertIn("location = /tt-publish-logs.html", self.nginx)
        self.assertIn('Cache-Control "no-cache, no-store, must-revalidate"', self.nginx)

    def test_dom_writes_are_text_only(self):
        self.assertNotIn("innerHTML", self.js)
        self.assertNotIn("insertAdjacentHTML", self.js)


if __name__ == "__main__":
    unittest.main()
