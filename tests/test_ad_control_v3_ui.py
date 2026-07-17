import ast
import json
import os
import re
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEATURE = ROOT / "features" / "ad_control_v3"
TEMPLATES = FEATURE / "templates"
ASSETS = FEATURE / "assets"
RENDERER = FEATURE / "page_renderer.py"
JS = ASSETS / "app.js"
CSS = ASSETS / "app.css"
USAGE_GUIDE_URL = (
    "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/"
    "codex-artifacts/html/20260717/"
    "ai-ad-control-v3-usage-guide-current-20260717_170557/index.html"
)

sys.path.insert(0, str(ROOT))
from features.ad_control_v3 import page_renderer  # noqa: E402


class ParsedHtml(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attributes = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.append((tag, dict(attrs)))


def test_dynamic_pages_render_complete_accessible_documents():
    for page_name in ["rule-groups", "execution-logs"]:
        html = page_renderer.render_page(page_name, {"apiBase": "/api/ad-control/v3"})
        parsed = ParsedHtml()
        parsed.feed(html)

        assert html.startswith("<!doctype html>")
        assert ("html", {"lang": "zh-CN"}) in parsed.attributes
        assert "main" in parsed.tags
        assert 'class="skip-link"' in html
        assert 'aria-live="polite"' in html
        assert 'id="adControlV3Bootstrap" type="application/json"' in html
        assert "__AD_CONTROL_V3_BOOTSTRAP__" not in html
        assert html.count('id="quickNav"') == 1
        assert 'class="topbar page-header"' in html
        assert 'id="userCard"' in html
        assert 'id="refreshBtn"' in html
        assert 'id="authBtn"' in html
        assert "feature-nav-link" not in html
        assert "sidebar-foot" not in html


def test_rule_groups_exposes_usage_guide_as_safe_new_tab_link():
    html = page_renderer.render_page("rule-groups", {"apiBase": "/api/ad-control/v3"})
    parsed = ParsedHtml()
    parsed.feed(html)
    links = [attrs for tag, attrs in parsed.attributes if tag == "a"]
    guide_link = next(attrs for attrs in links if attrs.get("id") == "usageGuideLink")

    assert guide_link["href"] == USAGE_GUIDE_URL
    assert guide_link["target"] == "_blank"
    assert set(guide_link["rel"].split()) == {"noopener", "noreferrer"}
    assert guide_link["referrerpolicy"] == "no-referrer"
    assert guide_link["aria-label"] == "打开 V3 使用手册（新标签页）"
    assert "使用手册" in html
    assert 'id="usageGuideLink"' not in page_renderer.render_page("execution-logs")


def test_templates_reference_standard_shell_and_dynamic_v3_business_assets():
    expected = {
        ("link", "/ui-topbar.css"),
        ("link", "/api/ad-control/v3/assets/app.css"),
        ("script", "/ui-topbar.js"),
        ("script", "/quick-nav.js?v=20260707nav2"),
        ("script", "/api/ad-control/v3/assets/app.js"),
    }
    for path in TEMPLATES.glob("*.html"):
        html = path.read_text(encoding="utf-8")
        parsed = ParsedHtml()
        parsed.feed(html)
        sources = set()
        for tag, attrs in parsed.attributes:
            if tag == "link" and attrs.get("href"):
                sources.add((tag, attrs["href"]))
            if tag == "script" and attrs.get("src"):
                sources.add((tag, attrs["src"]))
        assert sources == expected
        assert "Content-Security-Policy" in html
        assert "unsafe-inline" not in html
        assert "unsafe-eval" not in html
        assert html.index("/ui-topbar.css") < html.index("/api/ad-control/v3/assets/app.css")
        assert html.index('id="adControlV3Bootstrap"') < html.index("/ui-topbar.js")
        assert html.index("/ui-topbar.js") < html.index("/quick-nav.js?v=20260707nav2")
        assert html.index("/quick-nav.js?v=20260707nav2") < html.index("/api/ad-control/v3/assets/app.js")


def test_shared_quick_nav_and_topbar_use_the_standard_javascript_contract():
    source = JS.read_text(encoding="utf-8")
    assert 'throw new Error("公共顶吸脚本 /ui-topbar.js 未加载")' in source
    assert 'throw new Error("公共快速导航脚本 /quick-nav.js 未加载")' in source
    assert 'state.auth = await rootApi("/api/ui/topbar")' in source
    assert "window.UiTopbar.render({" in source
    assert "window.QuickNav.render(quickNavOptions(state.auth))" in source
    assert 'page === "execution-logs" ? "adControlV3Logs" : "adControlV3Rules"' in source
    assert "requestGuardedNavigation(item.href)" in source
    assert "window.UiTopbar.handleAuthAction({" in source
    assert "api: rootApi" in source
    assert 'afterLogout: () => window.location.assign("/")' in source
    assert 'authButton.addEventListener("click", async () =>' in source
    assert "if (!(await allowShellNavigation())) return;" in source
    assert "function renderUser()" not in source


def test_quick_nav_runtime_style_hash_is_explicitly_allowed_by_csp():
    quick_nav = ROOT / "static" / "quick-nav.js"
    script = r"""
const fs = require("fs");
const crypto = require("crypto");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const quote = String.fromCharCode(96);
const prefix = "style.textContent = " + quote;
const start = source.indexOf(prefix);
const end = source.indexOf(quote + ";", start + prefix.length);
if (start < 0 || end < 0) process.exit(2);
const raw = source.slice(start + prefix.length, end);
const runtimeStyle = vm.runInNewContext(quote + raw + quote);
process.stdout.write("sha256-" + crypto.createHash("sha256").update(runtimeStyle, "utf8").digest("base64"));
"""
    result = subprocess.run(
        ["node", "-e", script, str(quick_nav)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    expected_hash = "sha256-hwxbDTADufampcgI9oc75ltbbfB38tCWOve6LIq/j68="
    assert result.stdout == expected_hash
    for page_name in ["rule-groups", "execution-logs"]:
        assert f"style-src 'self' '{expected_hash}'" in page_renderer.render_page(page_name)


def test_bootstrap_json_cannot_terminate_script_or_inject_html():
    hostile = "</script><img src=x onerror=alert(1)>&\u2028\u2029"
    html = page_renderer.render_rule_groups_page({"displayName": hostile})
    match = re.search(
        r'<script id="adControlV3Bootstrap" type="application/json">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    assert match
    serialized = match.group(1)
    assert hostile not in html
    assert "<img" not in serialized
    assert "&" not in serialized
    assert "\\u003c/script\\u003e" in serialized
    assert "\\u2028" in serialized
    assert "\\u2029" in serialized
    assert json.loads(serialized)["displayName"] == hostile


def test_renderer_and_asset_loader_are_allowlisted():
    assert page_renderer.load_asset("app.css") == CSS.read_bytes()
    assert page_renderer.load_asset("app.js") == JS.read_bytes()
    try:
        page_renderer.render_page("../../secrets")
    except ValueError as error:
        assert "unknown_ad_control_v3_page" in str(error)
    else:
        raise AssertionError("unknown page should fail closed")
    try:
        page_renderer.load_asset("../page_renderer.py")
    except ValueError as error:
        assert "unknown_ad_control_v3_asset" in str(error)
    else:
        raise AssertionError("unknown asset should fail closed")


def test_python_ui_renderer_is_python_39_compatible():
    source = RENDERER.read_text(encoding="utf-8")
    ast.parse(source, filename=str(RENDERER), feature_version=(3, 9))
    assert not re.search(r"Mapping\[[^\n]+\]\s*\|\s*None", source)
    assert not re.search(r"\bmatch\s+[^:\n]+:", source)
    assert not re.search(r"\bcase\s+[^:\n]+:", source)


def test_javascript_has_no_syntax_errors():
    result = subprocess.run(
        ["node", "--check", str(JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_javascript_escape_helper_is_functionally_xss_safe():
    script = r"""
global.window = { setTimeout, clearTimeout, location: { reload() {} } };
global.document = {
  body: { dataset: { v3Page: "test" } },
  addEventListener() {},
  getElementById() { return null; },
};
global.HTMLElement = function HTMLElement() {};
global.Element = function Element() {};
global.Headers = class Headers { has() { return false; } set() {} };
require(process.argv[1]);
const escaped = window.AdControlV3Ui.escapeHtml(`<img src=x onerror="boom">'&`);
if (escaped !== "&lt;img src=x onerror=&quot;boom&quot;&gt;&#39;&amp;") {
  process.stderr.write(escaped);
  process.exit(1);
}
"""
    result = subprocess.run(
        ["node", "-e", script, str(JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_javascript_audit_dates_are_fixed_to_utc8_even_in_another_browser_timezone():
    script = r"""
global.window = { setTimeout, clearTimeout, location: { reload() {} } };
global.document = {
  body: { dataset: { v3Page: "test" } },
  addEventListener() {},
  getElementById() { return null; },
};
global.HTMLElement = function HTMLElement() {};
global.Element = function Element() {};
global.Headers = class Headers { has() { return false; } set() {} };
require(process.argv[1]);
const ui = window.AdControlV3Ui;
if (ui.prettyDate("2026-07-17 06:55:00") !== "2026-07-17 14:55") process.exit(2);
if (ui.prettyDate("2026-07-17T14:55:00+08:00") !== "2026-07-17 14:55") process.exit(3);
if (ui.prettyDate("2026-07-17") !== "2026-07-17") process.exit(4);
if (ui.parseAuditDate("2026-07-17 06:55:00").date.toISOString() !== "2026-07-17T06:55:00.000Z") process.exit(5);
if (ui.prettyDate("2026-07-17T14:55:00.000000+08:00") !== "2026-07-17 14:55") process.exit(6);
"""
    env = dict(os.environ)
    env["TZ"] = "America/Los_Angeles"
    result = subprocess.run(
        ["node", "-e", script, str(JS)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_group_id_and_config_version_drive_preview_and_if_match():
    script = r"""
global.window = { setTimeout, clearTimeout, location: { reload() {} } };
global.document = {
  body: { dataset: { v3Page: "test" } },
  addEventListener() {},
  getElementById() { return null; },
};
global.HTMLElement = function HTMLElement() {};
global.Element = function Element() {};
global.Headers = class Headers { has() { return false; } set() {} };
require(process.argv[1]);
const ui = window.AdControlV3Ui;
const created = ui.normalizeGroup({
  group_id: "rg-created",
  config_version: 7,
  name: "created",
  channel: "facebook",
  object_level: "campaign",
  run_mode: "observe",
  optimizer_id: 248,
  products: ["Dramawave"],
  rules: [],
});
if (created.id !== "rg-created" || created.version !== "7") process.exit(2);
if (ui.previewPath(created.id) !== "/rule-groups/rg-created/preview") process.exit(3);
const request = ui.saveRequestForEditor(created, { name: "edited" });
if (request.path !== "/rule-groups/rg-created" || request.method !== "PUT") process.exit(4);
if (request.headers["If-Match"] !== "7") process.exit(5);
const createRequest = ui.saveRequestForEditor({ id: "", version: "" }, { name: "new" });
if (createRequest.path !== "/rule-groups" || createRequest.method !== "POST") process.exit(6);
if (Object.keys(createRequest.headers).length !== 0) process.exit(7);
"""
    result = subprocess.run(
        ["node", "-e", script, str(JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_meta_normalization_matches_service_permissions_and_level_catalog():
    script = r"""
global.window = { setTimeout, clearTimeout, location: { reload() {} } };
global.document = {
  body: { dataset: { v3Page: "test" } },
  addEventListener() {},
  getElementById() { return null; },
};
global.HTMLElement = function HTMLElement() {};
global.Element = function Element() {};
global.Headers = class Headers { has() { return false; } set() {} };
require(process.argv[1]);
const meta = window.AdControlV3Ui.normalizeMeta({
  permissions: { is_admin: true, current_optimizer_id: null },
  products: [{ product_value: "Dramawave", display_name: "DramaWave" }],
  optimizers: [{ optimizer_id: 248, name: "Owner" }],
  account_timezones: ["+8"],
  field_catalog: {
    campaign: [{ key: "spend", label: "Spend", value_type: "number", levels: ["campaign"], operators: ["gt"] }],
    adset: [{ key: "optimization_goal", label: "Goal", value_type: "enum", levels: ["adset"], operators: ["eq"] }],
    ad: [{ key: "creative_id", label: "Creative", value_type: "text", levels: ["ad"], operators: ["eq"] }],
  },
});
if (!meta.actor.isAdmin || meta.actor.name !== "管理员") process.exit(2);
if (meta.fields.length !== 3) process.exit(3);
if (!meta.fields.some(item => item.key === "optimization_goal")) process.exit(4);
if (meta.products[0].value !== "Dramawave") process.exit(5);
"""
    result = subprocess.run(
        ["node", "-e", script, str(JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_save_flow_does_not_reference_out_of_scope_is_update():
    source = JS.read_text(encoding="utf-8")
    save_block = re.search(r"async function saveEditor\(options\) \{(.*?)\n  function saveRequestForEditor", source, re.DOTALL)
    assert save_block
    assert "const wasUpdate = Boolean(state.editor.id);" in save_block.group(1)
    assert "toast(wasUpdate ?" in save_block.group(1)
    assert "toast(isUpdate ?" not in save_block.group(1)


def test_ui_contract_has_three_levels_fb_only_and_tiktok_disabled():
    source = JS.read_text(encoding="utf-8")
    assert 'campaign: "Campaign"' in source
    assert 'adset: "Ad Set"' in source
    assert 'ad: "Ad"' in source
    assert 'data-value="facebook"' in source
    assert "TikTok" in source
    assert 'disabled aria-disabled="true"' in source
    assert "channel_not_enabled" in source


def test_scope_ui_is_product_optimizer_timezone_without_account_picker():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [*TEMPLATES.glob("*.html"), JS]
    )
    assert "短剧产品" in source
    assert "优化师" in source
    assert "账户时区" in source
    assert "account_timezones" in source
    assert "account_ids" not in source
    assert "account_pool_id" not in source
    assert "account_group_id" not in source
    assert "账户池" not in source
    assert "选择账号" not in source


def test_new_rule_business_inputs_are_blank_and_use_placeholders():
    source = JS.read_text(encoding="utf-8")
    new_editor = re.search(r"function newEditor\(\) \{(.*?)\n  \}", source, re.DOTALL)
    assert new_editor
    block = new_editor.group(1)
    for field in ["name", "description", "channel", "object_level"]:
        assert re.search(rf"\b{field}: \"\"", block)
    assert "run_mode: \"observe\"" in block
    assert "enabled: false" in block
    assert 'placeholder="例如：爆款剧高 ROAS 放量规则"' in source
    assert 'placeholder="输入每日最多动作数"' in source
    assert 'value="10:00"' not in source
    assert 'value="10"' not in source

    script = r"""
global.window = { setTimeout, clearTimeout, location: { reload() {} } };
global.document = {
  body: { dataset: { v3Page: "test" } },
  addEventListener() {},
  getElementById() { return null; },
};
global.HTMLElement = function HTMLElement() {};
global.Element = function Element() {};
global.Headers = class Headers { has() { return false; } set() {} };
require(process.argv[1]);
const draft = window.AdControlV3Ui.newRuleDraft(0);
if (draft.logic !== "" || draft.action !== "") process.exit(2);
if (draft.conditions.length !== 1) process.exit(3);
if (draft.conditions[0].field !== "" || draft.conditions[0].operator !== "" || draft.conditions[0].value !== "") process.exit(4);
if (!draft.rule_id || draft.priority !== "") process.exit(5);
"""
    result = subprocess.run(
        ["node", "-e", script, str(JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_visual_rule_builder_has_no_raw_json_editor():
    source = JS.read_text(encoding="utf-8")
    assert "renderConditionRow" in source
    assert "renderFieldCatalog" in source
    assert "OPERATOR_LABELS" in source
    assert "raw-json" not in source
    assert "JSON 编辑" not in source
    assert "<textarea" not in source


def test_copy_carrier_strategy_is_object_level_specific():
    source = JS.read_text(encoding="utf-8")
    for value in [
        "deep_copy_campaign",
        "same_campaign",
        "new_campaign",
        "same_adset",
        "isolated_adset",
        "isolated_campaign",
    ]:
        assert value in source
    assert "delete rule.copy_parameters.carrier_strategy" in source


def test_lists_are_server_paginated_and_have_loading_empty_error_states():
    source = JS.read_text(encoding="utf-8")
    assert 'params.set("page"' in source
    assert 'params.set("page_size"' in source
    assert "pageSize: 20" in source
    assert 'class="skeleton"' in source
    assert 'class="empty-state"' in source
    assert 'setPageStatus("error"' in source
    assert "/rule-groups?" in source
    assert "/executions?" in source
    assert "20 条/页 · 服务端分页" in source
    assert "logRequestController.abort()" in source
    assert "signal: controller.signal" in source


def test_scheduled_live_log_marks_precheck_as_merged():
    source = JS.read_text(encoding="utf-8")
    assert "预检已合并" in source
    assert "预检并锁定候选" in source
    assert 'item.run_mode === "live" && item.trigger_source === "schedule"' in source


def test_scope_estimate_requires_and_sends_explicit_metric_window():
    source = JS.read_text(encoding="utf-8")
    assert "这里只读取对象身份并返回结构数量，不判断规则指标" in source
    assert "最终可命中数以保存后的手动试算为准" in source
    assert 'id="scopeMetricWindow"' in source
    assert 'placeholder="输入最近天数"' in source
    assert '请填写大于 0 的指标窗口天数' in source
    assert 'metric_window_days: numericIfPossible(state.editor.selection.metric_window_days)' in source
    assert 'object_level: state.editor.object_level' in source
    assert 'object_level: state.editor.object_level || null' not in source


def test_candidate_selection_is_validated_before_save_and_routes_to_step_four():
    script = r"""
global.window = { setTimeout, clearTimeout, location: { reload() {} } };
global.document = {
  body: { dataset: { v3Page: "test" } },
  addEventListener() {},
  getElementById() { return null; },
};
global.HTMLElement = function HTMLElement() {};
global.Element = function Element() {};
global.Headers = class Headers { has() { return false; } set() {} };
require(process.argv[1]);
const ui = window.AdControlV3Ui;

const missingMode = ui.selectionValidationErrors({});
if (missingMode.length !== 1 || !missingMode[0].includes("候选选择")) process.exit(2);
if (ui.firstInvalidStep(missingMode) !== 4) process.exit(3);

const incompleteTopN = ui.selectionValidationErrors({ mode: "account_top_n" });
if (!incompleteTopN.some(item => item.includes("数量"))) process.exit(4);
if (!incompleteTopN.some(item => item.includes("排序指标"))) process.exit(5);
if (!incompleteTopN.some(item => item.includes("排序方向"))) process.exit(6);
if (ui.firstInvalidStep(incompleteTopN) !== 4) process.exit(7);

if (ui.selectionValidationErrors({ mode: "all" }).length !== 0) process.exit(8);
if (ui.selectionValidationErrors({
  mode: "product_top_n", top_n: 3, sort_field: "roas", sort_direction: "desc",
}).length !== 0) process.exit(9);
"""
    result = subprocess.run(
        ["node", "-e", script, str(JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_scheduler_unavailable_disables_enable_but_keeps_stop_available():
    script = r"""
global.window = { setTimeout, clearTimeout, location: { reload() {} } };
global.document = {
  body: { dataset: { v3Page: "test" } },
  addEventListener() {},
  getElementById() { return null; },
};
global.HTMLElement = function HTMLElement() {};
global.Element = function Element() {};
global.Headers = class Headers { has() { return false; } set() {} };
require(process.argv[1]);
const ui = window.AdControlV3Ui;
const unavailable = ui.normalizeMeta({ permissions: { can_enable: false, scheduler_available: false } });
if (unavailable.permissions.canEnable !== false) process.exit(2);
if (unavailable.permissions.schedulerAvailable !== false) process.exit(3);
if (ui.canToggleGroup(false, unavailable.permissions) !== false) process.exit(4);
if (ui.canToggleGroup(true, unavailable.permissions) !== true) process.exit(5);
const available = ui.normalizeMeta({ permissions: { can_enable: true } });
if (available.permissions.canEnable !== true) process.exit(6);
if (ui.canToggleGroup(false, available.permissions) !== true) process.exit(7);
const live = ui.normalizeMeta({ permissions: {
  can_enable: true, can_live_execute: true, scheduler_live_enabled: true,
  live_pause_enabled: true, live_copy_enabled: true,
} });
const liveBanner = ui.capabilityBannerCopy(live.permissions);
if (liveBanner.state !== "live" || !liveBanner.title.includes("自动调度已开放")) process.exit(8);
"""
    result = subprocess.run(
        ["node", "-e", script, str(JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

    source = JS.read_text(encoding="utf-8")
    toggle = re.search(r"async function toggleGroup\(node\) \{(.*?)\n  async function emergencyGroup", source, re.DOTALL)
    assert toggle
    assert "permissions.canEnable" in toggle.group(1)
    assert toggle.group(1).index("permissions.canEnable") < toggle.group(1).index("confirmDialog")
    assert "当前仅支持保存草稿和手动试算，不能持续自动扫描" in toggle.group(1)
    assert 'data-enable-blocked="true"' in source

    rendered = page_renderer.render_rule_groups_page()
    assert 'id="systemCapabilityBanner"' in rendered
    assert "正在读取调控能力" in rendered
    assert "用产品与优化师圈定广告范围" in rendered
    assert "真实暂停、复制与自动调度已开放" in source


def test_execution_logs_preserve_unknown_counts_and_nested_summary_truth():
    script = r"""
global.window = { setTimeout, clearTimeout, location: { reload() {} } };
global.document = {
  body: { dataset: { v3Page: "test" } },
  addEventListener() {},
  getElementById() { return null; },
};
global.HTMLElement = function HTMLElement() {};
global.Element = function Element() {};
global.Headers = class Headers { has() { return false; } set() {} };
require(process.argv[1]);
const ui = window.AdControlV3Ui;
if (ui.executionIdOf({ execution_id: "exec-1", rule_group_id: "rg-1" }) !== "exec-1") process.exit(8);
const item = { summary: { target_count: 8, success_count: 3, snapshot_valid: false, meta_write_count: 0 } };
if (ui.executionValue(item, ["target_count", "requested_count"]) !== 8) process.exit(2);
if (ui.executionValue(item, ["success_count"]) !== 3) process.exit(3);
if (ui.executionValue(item, ["snapshot_valid"]) !== false) process.exit(4);
if (ui.executionValue({}, ["target_count"]) !== null) process.exit(5);
if (ui.displayCount(null) !== "—" || ui.displayCount(undefined) !== "—") process.exit(6);
if (ui.displayCount(0) !== "0") process.exit(7);
"""
    result = subprocess.run(
        ["node", "-e", script, str(JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

    source = JS.read_text(encoding="utf-8")
    assert 'observed: "已观察"' in source
    assert '["pending", "running", "observed", "completed"' in source
    assert 'snapshotValid == null ? "未校验"' in source
    assert 'displayCount(metaWriteCount)' in source
    assert "formatCount(item.target_count" not in source


def test_rule_group_keyword_search_only_renders_when_backend_declares_support():
    script = r"""
global.window = { setTimeout, clearTimeout, location: { reload() {} } };
global.document = {
  body: { dataset: { v3Page: "test" } },
  addEventListener() {},
  getElementById() { return null; },
};
global.HTMLElement = function HTMLElement() {};
global.Element = function Element() {};
global.Headers = class Headers { has() { return false; } set() {} };
require(process.argv[1]);
const ui = window.AdControlV3Ui;
if (ui.normalizeMeta({}).capabilities.supportsRuleGroupSearch !== true) process.exit(2);
if (ui.normalizeMeta({ capabilities: { rule_group_search_fields: ["name"] } }).capabilities.supportsRuleGroupSearch !== false) process.exit(3);
if (ui.normalizeMeta({ capabilities: { rule_group_search_fields: ["name", "group_id"] } }).capabilities.supportsRuleGroupSearch !== true) process.exit(4);
if (ui.normalizeMeta({ capabilities: { rule_group_keyword_search: true } }).capabilities.supportsRuleGroupSearch !== true) process.exit(5);
if (ui.normalizeMeta({ capabilities: { rule_group_keyword_search: false } }).capabilities.supportsRuleGroupSearch !== false) process.exit(6);
"""
    result = subprocess.run(
        ["node", "-e", script, str(JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_write_actions_are_single_flight_and_non_owner_groups_are_read_only():
    script = r"""
global.window = { setTimeout, clearTimeout, location: { reload() {} } };
global.document = {
  body: { dataset: { v3Page: "test" } },
  addEventListener() {},
  getElementById() { return null; },
};
global.HTMLElement = function HTMLElement() {};
global.Element = function Element() {};
global.Headers = class Headers { has() { return false; } set() {} };
require(process.argv[1]);
const ui = window.AdControlV3Ui;
if (!ui.beginInFlight("save")) process.exit(2);
if (ui.beginInFlight("save")) process.exit(3);
if (!ui.isInFlight("save")) process.exit(4);
ui.endInFlight("save");
if (ui.isInFlight("save")) process.exit(5);

const owner = { id: "101", isAdmin: false };
const other = { id: "202", isAdmin: false };
const admin = { id: "1", isAdmin: true };
const group = { owner_user_id: "101" };
if (!ui.canMutateGroup(group, owner)) process.exit(6);
if (ui.canMutateGroup(group, other)) process.exit(7);
if (!ui.canMutateGroup(group, admin)) process.exit(8);
if (ui.canMutateGroup({ owner_user_id: "101", can_mutate: false }, owner)) process.exit(9);
if (!ui.canMutateGroup({ owner_user_id: "202", can_mutate: true }, owner)) process.exit(10);
if (ui.canMutateGroup({}, owner)) process.exit(11);
const firstScope = ui.scopeFingerprint({ channel: "facebook", optimizer_id: 101, object_level: "campaign", products: ["B", "A"], account_timezones: ["UTC+8"], selection: { metric_window_days: 7 } });
const sameScope = ui.scopeFingerprint({ channel: "facebook", optimizer_id: 101, object_level: "campaign", products: ["A", "B"], account_timezones: ["UTC+8"], selection: { metric_window_days: 7 } });
const changedScope = ui.scopeFingerprint({ channel: "facebook", optimizer_id: 101, object_level: "campaign", products: ["A", "B"], account_timezones: ["UTC+8"], selection: { metric_window_days: 14 } });
if (firstScope !== sameScope || firstScope === changedScope) process.exit(12);
"""
    result = subprocess.run(
        ["node", "-e", script, str(JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

    source = JS.read_text(encoding="utf-8")
    save_block = re.search(r"async function saveEditor\(options\) \{(.*?)\n  function saveRequestForEditor", source, re.DOTALL)
    assert save_block
    assert 'beginInFlight("editor-save")' in save_block.group(1)
    assert 'endInFlight("editor-save")' in save_block.group(1)
    assert 'data-action="save-preview"${editorBusy ? " disabled"' in source
    assert "previewBusy" in source
    assert "groupBusy" in source
    assert "该规则组不属于当前用户，仅可查看" in source
    assert 'class="editor-pane-fieldset"${editorBusy ? " disabled"' in source
    estimate_block = re.search(r"async function estimateScope\(\) \{(.*?)\n  async function previewGroup", source, re.DOTALL)
    assert estimate_block
    assert "requestSerial !== state.estimateRequestSerial" in estimate_block.group(1)
    assert "fingerprint !== scopeFingerprint(state.editor)" in estimate_block.group(1)
    assert 'window.addEventListener("beforeunload"' in source
    assert 'target.closest("a[data-guard-editor-exit]")' in source
    assert 'isInFlight("editor-save")' in source
    assert "requestGuardedNavigation(item.href)" in source
    assert page_renderer.render_rule_groups_page().count('id="quickNav"') == 1


def test_relative_day_operators_are_metadata_driven_numeric_day_inputs():
    script = r"""
global.window = { setTimeout, clearTimeout, location: { reload() {} } };
global.document = {
  body: { dataset: { v3Page: "test" } },
  addEventListener() {},
  getElementById() { return null; },
};
global.HTMLElement = function HTMLElement() {};
global.Element = function Element() {};
global.Headers = class Headers { has() { return false; } set() {} };
require(process.argv[1]);
const ui = window.AdControlV3Ui;
const within = ui.conditionValueSpec({ value_type: "time" }, "within_last_days");
if (!within.relativeDays || within.inputType !== "number" || !within.placeholder.includes("天数")) process.exit(2);
if (!within.attributes.includes('min="1"') || !within.attributes.includes('max="3650"') || !within.attributes.includes('step="1"')) process.exit(3);
const older = ui.conditionValueSpec({ value_type: "time" }, "older_than_days");
if (!older.relativeDays || older.inputType !== "number") process.exit(4);
const absolute = ui.conditionValueSpec({ value_type: "time" }, "before");
if (absolute.relativeDays || absolute.inputType !== "datetime-local") process.exit(5);

const meta = ui.normalizeMeta({ field_catalog: { campaign: [
  { key: "release_at", value_type: "time", operators: ["within_last_days", "older_than_days"] },
  { key: "series", value_type: "multi_text", operators: ["eq", "in"] },
] } });
if (meta.fields[0].operators.join(",") !== "within_last_days,older_than_days") process.exit(6);
if (meta.fields[1].operators.join(",") !== "eq,in") process.exit(7);
"""
    result = subprocess.run(
        ["node", "-e", script, str(JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

    source = JS.read_text(encoding="utf-8")
    assert 'within_last_days: "最近 X 天内"' in source
    assert 'older_than_days: "早于 X 天前"' in source
    assert 'RELATIVE_DAY_OPERATORS.has(condition.operator)' in source
    assert 'max="3650"' in source
    assert "请填写 1 到 3650 的整数天数" in source
    assert "series_code" not in source


def test_responsive_accessible_styles_include_keyboard_and_motion_guards():
    css = CSS.read_text(encoding="utf-8")
    assert ":focus-visible" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (max-width: 720px)" in css
    assert ".sr-only" in css
    assert "min-width: 320px" in css


def test_dynamic_pages_are_not_added_to_legacy_static_directory():
    forbidden = [
        ROOT / "static" / "ad-control-v3.html",
        ROOT / "static" / "ad-control-v3-rule-groups.html",
        ROOT / "static" / "ad-control-v3-execution-logs.html",
    ]
    assert not any(path.exists() for path in forbidden)


class TestAdControlV3Ui(unittest.TestCase):
    def test_navigation_adds_one_v3_group_with_only_two_dynamic_pages(self):
        navigation = json.loads((ROOT / "static" / "navigation.json").read_text(encoding="utf-8"))
        groups = [item for item in navigation if item.get("key") == "ad_control_v3"]
        self.assertEqual(1, len(groups))
        items = groups[0].get("items") or []
        self.assertEqual(
            [
                "/api/ad-control/v3/ui/rule-groups",
                "/api/ad-control/v3/ui/execution-logs",
            ],
            [item.get("href") for item in items],
        )
        self.assertTrue(all(item.get("kind") == "page" for item in items))

    """Expose the dependency-free contract functions to unittest discovery."""


for _test_function in [
    test_dynamic_pages_render_complete_accessible_documents,
    test_rule_groups_exposes_usage_guide_as_safe_new_tab_link,
    test_templates_reference_standard_shell_and_dynamic_v3_business_assets,
    test_shared_quick_nav_and_topbar_use_the_standard_javascript_contract,
    test_quick_nav_runtime_style_hash_is_explicitly_allowed_by_csp,
    test_bootstrap_json_cannot_terminate_script_or_inject_html,
    test_renderer_and_asset_loader_are_allowlisted,
    test_python_ui_renderer_is_python_39_compatible,
    test_javascript_has_no_syntax_errors,
    test_javascript_escape_helper_is_functionally_xss_safe,
    test_javascript_audit_dates_are_fixed_to_utc8_even_in_another_browser_timezone,
    test_group_id_and_config_version_drive_preview_and_if_match,
    test_meta_normalization_matches_service_permissions_and_level_catalog,
    test_save_flow_does_not_reference_out_of_scope_is_update,
    test_ui_contract_has_three_levels_fb_only_and_tiktok_disabled,
    test_scope_ui_is_product_optimizer_timezone_without_account_picker,
    test_new_rule_business_inputs_are_blank_and_use_placeholders,
    test_visual_rule_builder_has_no_raw_json_editor,
    test_copy_carrier_strategy_is_object_level_specific,
    test_lists_are_server_paginated_and_have_loading_empty_error_states,
    test_scheduled_live_log_marks_precheck_as_merged,
    test_scope_estimate_requires_and_sends_explicit_metric_window,
    test_candidate_selection_is_validated_before_save_and_routes_to_step_four,
    test_scheduler_unavailable_disables_enable_but_keeps_stop_available,
    test_execution_logs_preserve_unknown_counts_and_nested_summary_truth,
    test_rule_group_keyword_search_only_renders_when_backend_declares_support,
    test_write_actions_are_single_flight_and_non_owner_groups_are_read_only,
    test_relative_day_operators_are_metadata_driven_numeric_day_inputs,
    test_responsive_accessible_styles_include_keyboard_and_motion_guards,
    test_dynamic_pages_are_not_added_to_legacy_static_directory,
]:
    setattr(TestAdControlV3Ui, _test_function.__name__, staticmethod(_test_function))


if __name__ == "__main__":
    unittest.main()
