#!/usr/bin/env python3
"""Static frontend contracts for X multi-schedule and drama-pool pages."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATERIAL_PATH = ROOT / "static" / "x-post-material-pool.html"
DRAMA_PATH = ROOT / "static" / "x-post-drama-pool.html"
QUICK_NAV_PATH = ROOT / "static" / "quick-nav.js"
NAVIGATION_PATH = ROOT / "static" / "navigation.json"

MATERIAL = MATERIAL_PATH.read_text(encoding="utf-8")
DRAMA = DRAMA_PATH.read_text(encoding="utf-8")
QUICK_NAV = QUICK_NAV_PATH.read_text(encoding="utf-8")
NAVIGATION = json.loads(NAVIGATION_PATH.read_text(encoding="utf-8"))


def x_navigation_items():
    group = next(item for item in NAVIGATION if item.get("key") == "x_platform")
    return {item["key"]: item for item in group.get("items", [])}


def inline_javascript(source):
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", source, flags=re.DOTALL)
    return "\n".join(script for script in scripts if script.strip())


class XPostMultiScheduleUiTest(unittest.TestCase):
    def test_navigation_registers_drama_pool_and_keeps_configurable_permission(self):
        items = x_navigation_items()
        drama = items["xPostDramaPool"]
        self.assertEqual(drama["label"], "Post短剧池")
        self.assertEqual(drama["href"], "/x-post-drama-pool.html")
        self.assertEqual(drama["module"], "x_accounts")
        self.assertFalse(items["xPostMaterialPool"]["adminOnly"])
        self.assertFalse(drama["adminOnly"])
        self.assertTrue(drama["enabled"])
        self.assertEqual(drama["order"], 40)
        self.assertEqual(items["xPostMaterialPool"]["order"], 30)
        self.assertEqual(items["xPostLogs"]["order"], 50)

        self.assertIn('xPostDramaPool: "/x-post-drama-pool.html"', QUICK_NAV)
        self.assertIn('key: "xPostDramaPool"', QUICK_NAV)
        self.assertIn('href: "/x-post-drama-pool.html"', QUICK_NAV)
        self.assertIn('module: "x_accounts"', QUICK_NAV)
        self.assertIn("if (!dramaPoolExists)", QUICK_NAV)
        self.assertIn("postLogs.order = 50", QUICK_NAV)

    def test_material_pool_has_account_and_multi_time_schedule_contract(self):
        required_ids = {
            "accountSearch",
            "accountOptions",
            "refreshAccounts",
            "publishTimeInput",
            "publishTimeChips",
            "scheduleEnabled",
            "scheduleSummary",
            "scheduleMeta",
            "saveSchedule",
        }
        for element_id in required_ids:
            self.assertIn(f'id="{element_id}"', MATERIAL)

        self.assertIn('href="/x-accounts.html"', MATERIAL)
        self.assertIn('api("/api/admin/x-posts/material-pool/account-options")', MATERIAL)
        self.assertIn('api("/api/admin/x-posts/material-pool/schedule")', MATERIAL)
        self.assertIn('api("/api/admin/x-posts/material-pool/schedule", {', MATERIAL)
        self.assertIn('method: "PUT"', MATERIAL)
        self.assertIn("account_ids: selectedIds.map", MATERIAL)
        self.assertIn("publish_times: state.schedule.publishTimes.slice()", MATERIAL)
        self.assertIn('timezone: "Asia/Shanghai"', MATERIAL)
        self.assertIn("version: state.schedule.version", MATERIAL)
        self.assertIn("accountEligible(item)", MATERIAL)
        self.assertIn('item.status === "active"', MATERIAL)
        self.assertIn("item.publish_eligible === true", MATERIAL)
        self.assertIn("每日最多", MATERIAL)

    def test_drama_pool_covers_preview_pool_detail_delete_and_schedule_apis(self):
        endpoints = {
            "/api/admin/x-posts/drama-pool/preview",
            "/api/admin/x-posts/drama-pool?",
            "/api/admin/x-posts/drama-pool",
            "/episodes?page=1&page_size=100",
            "/api/admin/x-posts/drama-pool/account-options",
            "/api/admin/x-posts/drama-pool/schedule",
        }
        for endpoint in endpoints:
            self.assertIn(endpoint, DRAMA)
        self.assertIn('method: "DELETE"', DRAMA)
        self.assertIn("drama_ids: dramaIds", DRAMA)
        self.assertIn("drama_ids: state.previewIds.slice()", DRAMA)
        self.assertIn('id="dramaPreviewList"', DRAMA)
        self.assertIn('id="episodePanel"', DRAMA)
        self.assertIn('id="episodeRows"', DRAMA)
        self.assertIn("published_episode_count", DRAMA)
        self.assertIn("remaining_episode_count", DRAMA)
        self.assertIn("next_sub_num", DRAMA)
        self.assertIn("assigned_account_id", DRAMA)
        self.assertIn("assigned_account_username", DRAMA)
        self.assertIn("绑定账号 / 下次发布时间", DRAMA)
        self.assertIn("调整账号顺序不会改变已有绑定", DRAMA)
        self.assertIn('"待分配"', DRAMA)
        self.assertIn('"不可分配"', DRAMA)
        self.assertIn("历史发布账号 · 免费剧集已完成", DRAMA)
        self.assertIn("state.schedule.nextDueAt", DRAMA)
        self.assertIn(
            "不可发布，请校验或重新授权",
            DRAMA,
        )
        self.assertNotIn("不可发布，请移除", DRAMA)
        self.assertNotIn("请移除后再保存", DRAMA)
        self.assertNotIn(
            "item.last_account_username || item.account_username",
            DRAMA,
        )
        self.assertNotIn("last_account_", DRAMA)
        self.assertIn("data-episodes-pool-id", DRAMA)
        self.assertIn("data-delete-pool-id", DRAMA)

    def test_drama_pool_supports_current_page_select_all_and_atomic_batch_delete(self):
        for element_id in (
            "selectAllDramaPage",
            "selectedDramaCount",
            "clearDramaSelection",
            "batchDeleteDramas",
            "dramaTableWrap",
        ):
            self.assertIn(f'id="{element_id}"', DRAMA)
        self.assertIn("全选仅作用于当前页", DRAMA)
        self.assertIn("selectedPoolIds: new Set()", DRAMA)
        self.assertIn("function currentDeletablePoolIds()", DRAMA)
        self.assertIn("item.deletable === true", DRAMA)
        self.assertIn("selectAll.indeterminate", DRAMA)
        self.assertIn("const requestSeq = ++state.poolRequestSeq", DRAMA)
        self.assertIn("if (requestSeq !== state.poolRequestSeq) return", DRAMA)
        self.assertIn("if (state.page > maxPage)", DRAMA)
        self.assertIn('reason.className = "subtle delete-reason"', DRAMA)
        self.assertIn('aria-live="polite">已选择 0 部', DRAMA)
        self.assertIn("Number.isInteger(returnedCount)", DRAMA)
        self.assertIn("state.selectedPoolIds.clear()", DRAMA)
        self.assertIn(
            '"/api/admin/x-posts/drama-pool/batch-delete"',
            DRAMA,
        )
        self.assertIn('method: "POST"', DRAMA)
        self.assertIn(
            "JSON.stringify({ pool_item_ids: poolItemIds })",
            DRAMA,
        )
        self.assertIn('colspan="10"', DRAMA)
        self.assertIn('setTableEmpty("dramaRows", 10,', DRAMA)
        self.assertNotIn('setTableEmpty("dramaRows", 9,', DRAMA)

    def test_drama_pool_auto_verifies_only_refresh_required_accounts(self):
        self.assertIn(
            'item.status === "refresh_required"',
            DRAMA,
        )
        self.assertIn("const AUTO_VERIFY_CONCURRENCY = 3", DRAMA)
        self.assertIn("async function autoVerifyAccountOptions()", DRAMA)
        self.assertIn(
            "/api/admin/x-posts/drama-pool/account-options/${id}/verify",
            DRAMA,
        )
        self.assertIn('{ method: "POST", body: "{}" }', DRAMA)
        self.assertIn("await autoVerifyAccountOptions()", DRAMA)
        self.assertIn("state.accountVerifyBusy", DRAMA)
        self.assertIn('error.code === "x_post_rate_limited"', DRAMA)
        self.assertIn("stopRequested = true", DRAMA)
        self.assertIn("deferred: Math.max(0, targets.length - attempted)", DRAMA)
        self.assertIn("busy: true", DRAMA)
        self.assertIn("button.disabled = state.accountVerifyBusy", DRAMA)
        self.assertIn('item.status === "active"', DRAMA)
        self.assertIn("item.publish_eligible === true", DRAMA)

    def test_drama_template_is_rendered_exactly_and_not_merged_into_material_template(self):
        expected = (
            "{url}\n"
            " 👆Full story continues here:☝️\n"
            "Episode👉{sub_num}\n\n"
            "{name_tag}\n\n"
            " {desc}"
        )
        match = re.search(
            r'<pre id="postTemplatePreview">(?P<body>.*?)</pre>',
            DRAMA,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.group("body"), expected)
        self.assertNotIn("Full story continues here", MATERIAL)
        self.assertIn("短剧 ID（content_id）", DRAMA)
        self.assertIn(r"/^[A-Za-z0-9_-]{1,128}$/", DRAMA)

    def test_both_pages_use_fresh_dynamic_navigation_permission(self):
        self.assertIn('fetch("/navigation.json"', MATERIAL)
        self.assertIn('cache: "no-store"', MATERIAL)
        self.assertIn(
            'navigationAllows(state.auth, navigationConfig, "xPostMaterialPool")',
            MATERIAL,
        )
        self.assertNotIn("if (!user.is_admin)", MATERIAL)

        self.assertIn('fetch("/navigation.json"', DRAMA)
        self.assertIn('cache: "no-store"', DRAMA)
        self.assertIn(
            'navigationAllows(state.auth, navigationConfig, "xPostDramaPool")',
            DRAMA,
        )
        self.assertNotIn("if (!user.is_admin)", DRAMA)
        self.assertIn('activeKey: "xPostDramaPool"', DRAMA)
        self.assertIn('id="permissionGate"', DRAMA)

    def test_frontend_does_not_expose_credentials_or_use_html_injection(self):
        for path, source in ((MATERIAL_PATH, MATERIAL), (DRAMA_PATH, DRAMA)):
            lowered = source.lower()
            self.assertNotIn("access_token", lowered, path)
            self.assertNotIn("refresh_token", lowered, path)
            self.assertNotIn("client_secret", lowered, path)
            self.assertNotIn("innerhtml", lowered, path)
            self.assertNotIn("\ufffd", source, path)
        self.assertIn('link.rel = "noopener noreferrer"', MATERIAL)
        self.assertIn('link.rel = "noopener noreferrer"', DRAMA)
        self.assertIn('url.hostname === "x.com"', DRAMA)

    def test_inline_javascript_parses(self):
        for path, source in ((MATERIAL_PATH, MATERIAL), (DRAMA_PATH, DRAMA)):
            javascript = inline_javascript(source)
            self.assertTrue(javascript.strip(), path)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".js",
                delete=False,
            ) as handle:
                handle.write(javascript)
                temporary = Path(handle.name)
            try:
                completed = subprocess.run(
                    ["node", "--check", str(temporary)],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
            finally:
                temporary.unlink(missing_ok=True)
            self.assertEqual(
                completed.returncode,
                0,
                f"{path.name} inline JavaScript failed syntax check:\n"
                f"{completed.stdout}\n{completed.stderr}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
