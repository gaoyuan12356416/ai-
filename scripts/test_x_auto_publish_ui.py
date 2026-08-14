#!/usr/bin/env python3
"""Static UI contracts for isolated X Post automatic publishing templates."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
PAGE_PATHS = {
    "templates": STATIC / "x-auto-publish-templates.html",
    "template": STATIC / "x-auto-publish-template.html",
    "runs": STATIC / "x-auto-publish-runs.html",
}
SCRIPT_PATHS = {
    "common": STATIC / "x-auto-publish-common.js",
    "templates": STATIC / "x-auto-publish-templates.js",
    "template": STATIC / "x-auto-publish-template.js",
    "runs": STATIC / "x-auto-publish-runs.js",
}
PAGES = {name: path.read_text(encoding="utf-8") for name, path in PAGE_PATHS.items()}
SCRIPTS = {name: path.read_text(encoding="utf-8") for name, path in SCRIPT_PATHS.items()}
QUICK_NAV = (STATIC / "quick-nav.js").read_text(encoding="utf-8")
NAVIGATION = json.loads((STATIC / "navigation.json").read_text(encoding="utf-8"))
ASSET_VERSION = "20260812run1"


class IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: set[str] = set()
        self.scripts: list[str] = []
        self.stylesheets: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "script" and values.get("src"):
            self.scripts.append(values["src"])
        if tag == "link" and values.get("rel") == "stylesheet" and values.get("href"):
            self.stylesheets.append(values["href"])


def parse_page(source: str) -> IdParser:
    parser = IdParser()
    parser.feed(source)
    return parser


class XAutoPublishUiTest(unittest.TestCase):
    def test_javascript_files_parse(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        for path in [*SCRIPT_PATHS.values(), STATIC / "quick-nav.js"]:
            with self.subTest(path=path.name):
                result = subprocess.run(
                    [node, "--check", str(path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_pages_reuse_shared_shell_and_x_permission(self):
        active_keys = {
            "templates": 'activeKey: "xAutoPublishTemplates"',
            "template": 'activeKey: "xAutoPublishTemplates"',
            "runs": 'activeKey: "xAutoPublishRuns"',
        }
        for name, source in PAGES.items():
            with self.subTest(page=name):
                parser = parse_page(source)
                self.assertIn("/ui-topbar.css", parser.stylesheets)
                self.assertIn(
                    f"/x-auto-publish.css?v={ASSET_VERSION}", parser.stylesheets
                )
                self.assertIn("/ui-topbar.js", parser.scripts)
                self.assertIn("/quick-nav.js", parser.scripts)
                self.assertIn(
                    f"/x-auto-publish-common.js?v={ASSET_VERSION}", parser.scripts
                )
                for node_id in (
                    "quickNav",
                    "userCard",
                    "authButton",
                    "loginGate",
                    "permissionGate",
                    "pageRoot",
                ):
                    self.assertIn(node_id, parser.ids)
                self.assertIn("x_accounts", source)
                self.assertIn(active_keys[name], SCRIPTS[name])
        self.assertIn('api("/api/ui/topbar")', SCRIPTS["common"])
        self.assertIn("user.permissions.x_accounts", SCRIPTS["common"])
        self.assertIn("window.QuickNav.render", SCRIPTS["common"])
        self.assertIn("window.UiTopbar.render", SCRIPTS["common"])
        self.assertIn("window.UiTopbar.handleAuthAction", SCRIPTS["common"])

    def test_navigation_registers_pages_after_accounts_before_existing_pools(self):
        group = next(item for item in NAVIGATION if item.get("key") == "x_platform")
        ordered = sorted(group["items"], key=lambda item: item["order"])
        ordered_keys = [item["key"] for item in ordered]
        expected = {
            "xAutoPublishTemplates": "/x-auto-publish-templates.html",
            "xAutoPublishRuns": "/x-auto-publish-runs.html",
        }
        items = {item["key"]: item for item in ordered}
        for key, href in expected.items():
            with self.subTest(key=key):
                self.assertEqual(items[key]["href"], href)
                self.assertEqual(items[key]["module"], "x_accounts")
                self.assertTrue(items[key]["enabled"])
                self.assertIn(f'key: "{key}"', QUICK_NAV)
                self.assertIn(f'{key}: "{href}"', QUICK_NAV)
        self.assertLess(ordered_keys.index("xAccountList"), ordered_keys.index("xAutoPublishTemplates"))
        self.assertLess(ordered_keys.index("xAutoPublishRuns"), ordered_keys.index("xPostMaterialPool"))
        self.assertIn("xAutoPublishTemplatesExists", QUICK_NAV)
        self.assertIn("xAutoPublishRunsExists", QUICK_NAV)

    def test_pages_use_only_the_new_admin_namespace(self):
        combined = "\n".join(SCRIPTS.values())
        self.assertIn('const API_BASE = "/api/admin/x-auto-publish"', SCRIPTS["common"])
        self.assertNotIn("/api/admin/x-posts", combined)
        self.assertNotIn("/api/admin/tt", combined)
        self.assertNotIn("/publish-logs", combined)
        for route in ("/accounts", "/templates", "/runs"):
            self.assertIn(route, combined)

    def test_untrusted_content_is_rendered_without_inner_html(self):
        for name, source in {**PAGES, **SCRIPTS}.items():
            with self.subTest(source=name):
                self.assertNotIn(".innerHTML", source)
                self.assertNotIn("document.write", source)
        self.assertIn("node.textContent", SCRIPTS["common"])
        self.assertIn("node.replaceChildren()", SCRIPTS["common"])

    def test_template_list_supports_preview_and_confirmed_idempotent_real_run(self):
        ids = parse_page(PAGES["templates"]).ids
        for required in (
            "templateRows",
            "reloadTemplates",
            "filterQuery",
            "filterStatus",
            "prevPage",
            "nextPage",
            "confirmDialog",
        ):
            self.assertIn(required, ids)
        source = SCRIPTS["templates"]
        for action in ('"edit"', '"copy"', '"preview"', '"enable"', '"disable"', '"run"'):
            self.assertIn(action, source)
        self.assertIn("/preview", source)
        self.assertIn('"run-now"', source)
        self.assertIn("body.confirmed = true", source)
        self.assertIn("body.idempotency_key", source)
        self.assertIn("expected_version: version", source)
        self.assertIn("确认真实执行", source)
        self.assertIn("视频准备完成后自动发布", source)

    def test_template_list_uses_real_schedule_and_run_summary_dto(self):
        source = SCRIPTS["templates"]
        self.assertIn("item.next_run_at", source)
        self.assertIn("item.last_run_status", source)
        self.assertIn("item.last_run_at", source)

    def test_editor_has_required_language_x_body_and_two_filter_layers(self):
        ids = parse_page(PAGES["template"]).ids
        required = {
            "templateLanguage",
            "bodyTemplate",
            "accountList",
            "metricWindowDays",
            "platform",
            "dramaLaunchWindowDays",
            "cooldownDays",
            "dramaResourceTypes",
            "dramaRoasMin",
            "dramaRoasMax",
            "dramaSpendMin",
            "dramaSpendMax",
            "dramaSortBy",
            "dramaSortDirection",
            "materialDurationMin",
            "materialDurationMax",
            "materialRoasMin",
            "materialRoasMax",
            "materialSpendMin",
            "materialSpendMax",
            "materialSortBy",
            "materialSortDirection",
        }
        self.assertFalse(required - ids, required - ids)
        page = PAGES["template"]
        script = SCRIPTS["template"]
        self.assertIn('class="required" for="templateLanguage"', page)
        self.assertIn('class="required" for="bodyTemplate"', page)
        self.assertLess(page.index("先筛选剧"), page.index("再筛选素材"))
        for field in (
            "language",
            "body_template",
            "account_ids",
            "metric_window_days",
            "drama_launch_window_days",
            "cooldown_days",
            "resource_type_v2",
            "duration_min_seconds",
            "duration_max_seconds",
            "drama_rule",
            "material_rule",
            "sort_by",
            "sort_direction",
        ):
            self.assertIn(field, script)
        self.assertIn("{{drama_name}}", page)
        self.assertIn("{{desc}}", page)
        self.assertIn("{{url}}", page)
        self.assertIn('["drama_name", "desc"]', script)
        self.assertIn('macro === "url"', script)
        self.assertIn("必须且只能包含一次", script)
        self.assertIn("最多出现一次", script)
        self.assertIn("ads_setting.ads_facebook_post_blacklist", page)
        self.assertIn("历史素材永久排除", page)

    def test_editor_refreshes_only_approved_expired_accounts_before_selection(self):
        ids = parse_page(PAGES["template"]).ids
        self.assertIn("refreshAccountEligibility", ids)
        self.assertIn("accountRefreshStatus", ids)
        page = PAGES["template"]
        source = SCRIPTS["template"]
        self.assertIn("刷新可选账号资格", page)
        self.assertIn("未批准账号不会被自动开放", page)

        refreshable = source[
            source.index("  function accountRefreshable(item) {") :
            source.index("  function accountEligibilityText(item) {")
        ]
        self.assertIn("accountApproved(item)", refreshable)
        self.assertIn('=== "expired"', refreshable)

        load_accounts = source[
            source.index("  async function loadAccounts() {") :
            source.index("  async function refreshAccountEligibility() {")
        ]
        self.assertIn("`${ui.API_BASE}/accounts`", load_accounts)
        self.assertNotIn("/verify", load_accounts)
        self.assertNotIn('method: "POST"', load_accounts)

        refresh = source[
            source.index("  async function refreshAccountEligibility() {") :
            source.index("  async function loadTemplate() {")
        ]
        self.assertIn("const candidates = refreshableAccounts();", refresh)
        self.assertIn("for (let index = 0; index < candidates.length; index += 1)", refresh)
        self.assertIn("/accounts/${encodeURIComponent(id)}/verify", refresh)
        self.assertIn('method: "POST"', refresh)
        self.assertIn('body: "{}"', refresh)
        self.assertIn("if (accountEligible(account)) refreshed += 1", refresh)
        self.assertNotIn("Promise.all(candidates", refresh)

        self.assertIn("checkbox.disabled = !eligible && !checkbox.checked", source)
        self.assertIn('checkbox.dataset.accountEligible !== "1"', source)

    def test_editor_shows_duration_only_for_accounts_without_membership(self):
        source = SCRIPTS["template"]
        self.assertIn('basic: "X Basic"', source)
        self.assertIn('premium: "X Premium"', source)
        self.assertIn('premium_plus: "X Premium+"', source)
        self.assertIn('none: "无会员 · 最长 140 秒"', source)
        self.assertIn('unknown: "会员资格未知 · 最长 140 秒"', source)
        self.assertNotIn("支持长视频", source)
        self.assertNotIn("可发长视频", source)

    def test_editor_omits_tt_only_content_and_account_fields(self):
        combined = PAGES["template"] + SCRIPTS["template"]
        forbidden = (
            "caption_template",
            "captionTemplate",
            "content_id",
            "creator_nickname",
            "creator_username",
            "privacy_level",
            "allow_comment",
            "allow_duet",
            "allow_stitch",
            "commercial_disclosure",
            "brand_organic_toggle",
            "brand_content_toggle",
            "overlay",
            "{{code}}",
        )
        for field in forbidden:
            with self.subTest(field=field):
                self.assertNotIn(field, combined)

    def test_duration_resource_type_and_schedules_are_bounded(self):
        page = PAGES["template"]
        script = SCRIPTS["template"]
        self.assertIn('id="materialDurationMin" type="number" min="1" max="600"', page)
        self.assertIn('id="materialDurationMax" type="number" min="1" max="600"', page)
        self.assertIn("durationMin < 1 || durationMin > 600", script)
        self.assertIn("durationMax < 1 || durationMax > 600", script)
        self.assertIn("非必选；不选择时不限制短剧类型。", page)
        self.assertIn("state.selectedResourceTypes.clear()", script)
        self.assertIn("resource_type_v2: parseResourceTypes()", script)
        self.assertIn('{ mode: "fixed", times }', script)
        self.assertIn('{ mode: "random", daily_count: dailyCount }', script)
        self.assertIn("dailyCount < 1 || dailyCount > 24", script)
        self.assertIn("Array.from(new Set(timeValues())).sort()", script)

    def test_runs_page_shows_x_ledger_and_defensively_redacts_media_urls(self):
        ids = parse_page(PAGES["runs"]).ids
        for required in (
            "runRows",
            "runDetailDialog",
            "runDetailFacts",
            "runTaskRows",
            "runSnapshot",
            "runEvents",
            "filterTemplateId",
            "filterTriggerType",
            "filterStatus",
        ):
            self.assertIn(required, ids)
        source = SCRIPTS["runs"]
        self.assertIn('ui.readItems(payload, ["runs", "items"])', source)
        self.assertIn('ui.readItem(payload, ["run", "item"])', source)
        self.assertIn('ui.readItems(payload, ["tasks", "account_tasks"])', source)
        self.assertIn('ui.readItems(payload, ["events"])', source)
        self.assertIn("template_snapshot", source)
        self.assertIn("blacklist_snapshot", source)
        self.assertIn("execution_queue_id", source)
        self.assertIn("execution_log_id", source)
        self.assertIn("打开 X Post", source)
        self.assertIn("needs_review", source)
        self.assertIn("PRIVATE_DETAIL_KEYS", source)
        self.assertIn("publicDetail(snapshot)", source)
        self.assertNotIn("TikTok", PAGES["runs"] + source)
        for status in (
            "pending",
            "selecting",
            "no_candidate",
            "reserved",
            "retry_wait",
            "ready",
            "unknown",
            "skipped",
        ):
            self.assertIn(f'{status}:', source)
        self.assertIn("item.selected_duration_sec", source)
        self.assertIn('"attention_task_count"', source)
        self.assertIn("x_auto_run_not_found", source)
        self.assertIn("运行记录不存在或已不可访问。", source)
        self.assertIn("`${completedTasks} 个完成", source)

    def test_create_editor_sets_create_document_title(self):
        source = SCRIPTS["template"]
        self.assertIn('document.title = `${templateId ? "编辑" : "创建"}', source)

    def test_template_write_contract_matches_api(self):
        source = SCRIPTS["template"]
        self.assertIn('method: "POST"', source)
        self.assertIn("payload.expected_version = state.version", source)
        self.assertIn('ui.readItem(response, ["template", "item"])', source)
        self.assertIn('ui.readItems(payload, ["accounts", "items"])', source)
        self.assertIn("language,", source)
        self.assertIn("body_template: bodyTemplate", source)
        self.assertIn("platform: integerValue", source)


if __name__ == "__main__":
    unittest.main()
