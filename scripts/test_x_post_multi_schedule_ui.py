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
LOGS_PATH = ROOT / "static" / "x-post-logs.html"
QUICK_NAV_PATH = ROOT / "static" / "quick-nav.js"
NAVIGATION_PATH = ROOT / "static" / "navigation.json"

MATERIAL = MATERIAL_PATH.read_text(encoding="utf-8")
DRAMA = DRAMA_PATH.read_text(encoding="utf-8")
LOGS = LOGS_PATH.read_text(encoding="utf-8")
QUICK_NAV = QUICK_NAV_PATH.read_text(encoding="utf-8")
NAVIGATION = json.loads(NAVIGATION_PATH.read_text(encoding="utf-8"))


def x_navigation_items():
    group = next(item for item in NAVIGATION if item.get("key") == "x_platform")
    return {item["key"]: item for item in group.get("items", [])}


def inline_javascript(source):
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", source, flags=re.DOTALL)
    return "\n".join(script for script in scripts if script.strip())


class XPostMultiScheduleUiTest(unittest.TestCase):
    def test_pool_pages_describe_newest_upload_first(self):
        self.assertIn("可用素材按上传时间倒序、最新上传优先", MATERIAL)
        self.assertIn("按上传时间倒序领取尚未发布过的新剧", DRAMA)
        self.assertIn("领取最新上传的未绑定短剧", DRAMA)
        self.assertIn("人工素材池按最新上传优先选材", LOGS)
        self.assertNotIn("人工素材池 FIFO", LOGS)

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
            "randomDailyCount",
            "randomPlanPreview",
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
        self.assertIn('publish_times: scheduleMode === "fixed" ? state.schedule.publishTimes.slice() : []', MATERIAL)
        self.assertIn("schedule_mode: scheduleMode", MATERIAL)
        self.assertIn("random_daily_count:", MATERIAL)
        self.assertIn('timezone: "Asia/Shanghai"', MATERIAL)
        self.assertIn("version: state.schedule.version", MATERIAL)
        self.assertIn("accountEligible(item)", MATERIAL)
        self.assertIn('item.status === "active"', MATERIAL)
        self.assertIn("item.publish_eligible === true", MATERIAL)
        self.assertIn("每日最多", MATERIAL)

    def test_both_pools_support_persisted_random_daily_schedules(self):
        for page in (MATERIAL, DRAMA):
            self.assertIn("随机时间发布", page)
            self.assertIn("每天随机发布次数", page)
            self.assertIn("00:00–23:59", page)
            self.assertIn("相邻时间至少间隔 1 小时", page)
            self.assertIn("从次日生效", page)
            self.assertIn('item.schedule_mode || "fixed"', page)
            self.assertIn("item.random_daily_count", page)
            self.assertIn("item.random_daily_plans", page)
            self.assertIn("schedule_mode: scheduleMode", page)
            self.assertIn("random_daily_count:", page)

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

    def test_drama_pool_exposes_reversible_high_priority_without_rewriting_age(self):
        self.assertIn('data-priority-pool-id', DRAMA)
        self.assertIn('setText(priority, highPriority ? "取消高优" : "高优")', DRAMA)
        self.assertIn("function dramaPriorityEligible(item)", DRAMA)
        self.assertIn("Number(item.assigned_account_id || 0) === 0", DRAMA)
        self.assertIn('method: "PUT"', DRAMA)
        self.assertIn("/priority`,", DRAMA)
        self.assertIn("已绑定短剧和已冻结计划不受影响", DRAMA)
        self.assertIn('JSON.stringify({ high_priority: highPriority })', DRAMA)
        self.assertNotIn("item.created_at =", DRAMA)

    def test_material_pool_manual_publish_is_explicit_durable_and_schedule_independent(self):
        required_ids = {
            "manualPublish",
            "manualPublishDialog",
            "manualAccountSearch",
            "manualAccountOptions",
            "manualAccountSummary",
            "manualMapping",
            "manualRunPanel",
            "manualRunTitle",
            "manualRunCounts",
            "manualQueueRows",
            "manualPublishSubmit",
            "manualScheduleField",
            "manualScheduledAt",
            "manualScheduleHint",
        }
        for element_id in required_ids:
            self.assertIn(f'id="{element_id}"', MATERIAL)
        self.assertLess(
            MATERIAL.index('id="manualPublish"'),
            MATERIAL.index('id="addMaterials"'),
        )
        self.assertIn("本批素材不会进入素材池", MATERIAL)
        self.assertIn("可能部分成功、部分失败", MATERIAL)
        self.assertIn("系统无法撤回已经发布的帖子", MATERIAL)
        self.assertIn("原片 763.938 秒可发布", MATERIAL)
        self.assertIn("4 小时时长、512 MiB 文件门禁", MATERIAL)
        self.assertIn("会员账号不再受原 600 秒上限限制", MATERIAL)
        self.assertIn("手动和素材池自动发布使用同一会员时长规则", MATERIAL)
        self.assertIn("弹窗选择不会改动自动发布设置", MATERIAL)
        self.assertIn('parseMaterialIds(50, "手动发布")', MATERIAL)
        self.assertIn("accountIds.length !== materialIds.length", MATERIAL)
        self.assertIn("state.manual.accountIds", MATERIAL)
        self.assertIn("stableManualIdempotencyKey", MATERIAL)
        self.assertIn("window.sessionStorage", MATERIAL)
        self.assertIn(
            'api("/api/admin/x-posts/material-pool/manual-publish", {',
            MATERIAL,
        )
        self.assertIn('method: "POST"', MATERIAL)
        self.assertIn("material_ids: materialIds", MATERIAL)
        self.assertIn("account_ids: accountIds.map(Number)", MATERIAL)
        self.assertIn("idempotency_key: idempotencyKey", MATERIAL)
        self.assertIn('name="manualPublishMode" value="immediate"', MATERIAL)
        self.assertIn('name="manualPublishMode" value="scheduled"', MATERIAL)
        self.assertIn('type="datetime-local" step="60"', MATERIAL)
        self.assertIn('const BEIJING_TIME_ZONE = "Asia/Shanghai"', MATERIAL)
        self.assertIn('scheduledAt = `${raw}:00+08:00`', MATERIAL)
        self.assertIn("publish_mode: state.manual.publishMode", MATERIAL)
        self.assertIn("scheduled_at: timing.scheduledAt", MATERIAL)
        self.assertIn("manualPollDelay(run)", MATERIAL)
        self.assertIn("等待定时发布", MATERIAL)
        self.assertIn("确认定时发布", MATERIAL)
        self.assertIn(
            "/api/admin/x-posts/material-pool/manual-runs/${encodeURIComponent(runId)}",
            MATERIAL,
        )
        self.assertIn("MANUAL_TERMINAL_STATUSES", MATERIAL)
        self.assertNotIn(
            "state.schedule.accountIds = state.manual.accountIds",
            MATERIAL,
        )

    def test_material_and_drama_templates_are_rendered_exactly(self):
        material_expected = (
            "🎬 {{drama_name}}\n"
            "{{desc}}\n\n"
            "#shortdrama #shortfilms #tvdrama #aidrama #dramawave"
        )
        material_match = re.search(
            r'<textarea id="materialPostTemplate"[^>]*>(?P<body>.*?)</textarea>',
            MATERIAL,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(material_match)
        self.assertEqual(material_match.group("body"), material_expected)

        drama_expected = (
            "🎬 {{drama_name}}\n"
            "Episode {{episode_number}}\n"
            "{{desc}}\n\n"
            "#shortdrama #shortfilms #tvdrama #aidrama #dramawave"
        )
        drama_match = re.search(
            r'<textarea id="dramaPostTemplate"[^>]*>(?P<body>.*?)</textarea>',
            DRAMA,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(drama_match)
        self.assertEqual(drama_match.group("body"), drama_expected)
        self.assertNotIn("{{short_link}}", material_expected)
        self.assertNotIn("{{short_link}}", drama_expected)
        self.assertNotIn("Episode {{episode_number}}", material_expected)
        self.assertIn("{{url}}（当前队列的追踪短链）", MATERIAL)
        self.assertIn("{{url}}（当前队列的追踪短链）", DRAMA)
        self.assertIn("body_template: bodyTemplate", MATERIAL)
        self.assertIn("body_template: bodyTemplate", DRAMA)
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

    def test_logs_explain_premium_relay_and_target_repost_states(self):
        self.assertIn(
            'source_published:"原帖已发布，等待目标账号 Repost"',
            LOGS,
        )
        self.assertIn('repost_creating:"目标账号 Repost 中"', LOGS)
        self.assertIn('item.delivery_mode || "direct"', LOGS)
        self.assertIn("目标账号 Repost · 原帖由 @", LOGS)
        self.assertIn('relayDelivery ? "预览原 Post" : "预览 Post"', LOGS)

    def test_inline_javascript_parses(self):
        for path, source in (
            (MATERIAL_PATH, MATERIAL),
            (DRAMA_PATH, DRAMA),
            (LOGS_PATH, LOGS),
        ):
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
