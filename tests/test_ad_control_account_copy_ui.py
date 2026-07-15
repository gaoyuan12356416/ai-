import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "static" / "ad-control-pages.js").read_text(encoding="utf-8")
RULE_BLOCK = JS[JS.index("async function renderRules()") : JS.index("async function renderPools()")]


def run_payload_harness():
    script = r'''
const fs = require("fs");
const vm = require("vm");
const sourcePath = process.argv[1];
let source = fs.readFileSync(sourcePath, "utf8");
const marker = '  document.addEventListener("DOMContentLoaded", () => init().catch(error => toast(error.message || String(error), "error")));';
if (!source.includes(marker)) throw new Error("test export marker not found");
source = source.replace(marker, '  globalThis.__adControlHooks = { state, aggregateRuleGroups, buildRuleGroupDraft, readRuleGroupDraftFromDrawer, firstPreviewErrorReason, mergeGeneratedPrimaryRule };');

const nodes = {};
const values = {
  drawerRulesJson: JSON.stringify([{
    name: "old copy",
    action: "copy",
    copy: {
      budget: { type: "source_budget_ratio", mode: "source_budget_ratio", ratio: 0.9, legacy_budget: "keep" },
      roas_bid: { direction: "increase", percent: 88 },
      roas: { direction: "increase", percent: 88 },
      legacy_copy: "keep"
    },
    drama_scope: { type: "recent_days", days: 99, recent_days: 99, drama_ids: ["old"], legacy_scope: "keep" },
    candidate_selection: { mode: "all", top_n: 0, legacy_selection: "keep" },
    top_n_per_account: 0
  }, {
    name: "secondary copy",
    action: "copy",
    copy: {
      budget: { type: "source_budget_ratio", mode: "source_budget_ratio", ratio: 0.77, secondary_budget: "keep" },
      roas_bid: { direction: "increase", percent: 33 },
      secondary_copy: "keep"
    },
    drama_scope: { type: "specified", drama_ids: ["secondary-drama"], secondary_scope: "keep" },
    candidate_selection: { mode: "top_n_per_account", top_n: 5, secondary_selection: "keep" },
    top_n_per_account: 5,
    secondary_rule: "keep"
  }]),
  drawerObjectLevel: "campaign",
  drawerBudgetStrategy: "fixed_target_cpi_multiplier",
  drawerCpiMultiple: "2.5",
  drawerFixedTargetCpi: "4.2",
  drawerBudgetRatio: "0.3",
  drawerRoasDirection: "decrease",
  drawerRoasPercent: "15",
  drawerDramaDays: "7",
  drawerDramaScope: "specified",
  drawerDramaIds: "new-a,new-b",
  drawerCandidateSelectionMode: "top_n_per_account",
  drawerTopN: "2",
  drawerScheduleType: "fixed_time",
  drawerExecuteTime: "10:00",
  drawerIntervalMinutes: "60",
  drawerAllowedStart: "00:00",
  drawerExecuteBefore: "23:00",
  drawerRuleDailyLimit: "3",
  drawerUserDailyLimit: "12",
  drawerSourceCooldown: "1",
  drawerGroupName: "payload override",
  drawerWindowType: "since_start",
  drawerWindowHours: "24",
  drawerGroupDescription: "test"
};
for (const [id, value] of Object.entries(values)) nodes[id] = { value };
globalThis.document = {
  body: { dataset: { page: "rules" } },
  getElementById: id => nodes[id] || null,
  querySelectorAll: () => [],
  querySelector: selector => selector.includes("drawerRunMode") ? { value: "observe", checked: true } : null,
  addEventListener: () => {}
};
globalThis.window = globalThis;
globalThis.location = {};
vm.runInThisContext(source, { filename: sourcePath });
const hooks = globalThis.__adControlHooks;
hooks.state.ruleGroupDraft = {
  id: "front-group",
  mode: "edit",
  run_mode: "observe",
  selectedAccountKeys: new Set(["123"]),
  migrate_from_group_ids: ["front-group", "legacy-a", "legacy-b"]
};
const payload = hooks.readRuleGroupDraftFromDrawer();
const mergedPrimary = hooks.mergeGeneratedPrimaryRule(
  [{ name: "primary", action: "copy", copy: { old: true } }, { name: "secondary", action: "copy", copy: { untouched: true } }],
  { name: "primary updated", action: "pause", conditions: [] }
);
const partial = hooks.aggregateRuleGroups([
  { group_id: "legacy-a", name: "legacy", enabled: true, account_ids: ["123"], rules: [{ action: "pause" }], strategy: { frontend_rule_group_id: "front-group" } },
  { group_id: "legacy-b", name: "legacy", enabled: false, account_ids: ["456"], rules: [{ action: "pause" }], strategy: { frontend_rule_group_id: "front-group" } }
])[0];
const legacy = hooks.buildRuleGroupDraft({
  id: "legacy-front",
  target_ids: ["legacy-observe"],
  bindings: [{ group_id: "legacy-observe", account_ids: ["123"], rules: [{ name: "old", action: "observe" }], strategy: {} }]
}, false);
const unknown = hooks.buildRuleGroupDraft({
  id: "unknown-front",
  target_ids: ["unknown-source"],
  bindings: [{ group_id: "unknown-source", account_ids: ["123"], rules: [{ name: "bad", action: "mystery" }], strategy: {} }]
}, false);
hooks.state.ruleGroupDraft = {
  id: "unknown-front",
  mode: "edit",
  run_mode: "observe",
  selectedAccountKeys: new Set(["123"]),
  migrate_from_group_ids: ["unknown-source"]
};
nodes.drawerRulesJson.value = JSON.stringify([{ name: "bad", action: "mystery" }]);
let unknownSaveError = "";
try { hooks.readRuleGroupDraftFromDrawer(); } catch (error) { unknownSaveError = error.message; }
process.stdout.write(JSON.stringify({
  payload,
  merged_primary: mergedPrimary,
  partial: { enabled: partial.enabled, partial_enabled: partial.partial_enabled, enabled_count: partial.enabled_count, target_ids: partial.target_ids },
  legacy: { run_mode: legacy.run_mode, action: legacy.rules[0].action, migrated: legacy.legacy_observe_migrated },
  unknown: { action: unknown.rules[0].action, unknown_actions: unknown.unknown_actions, save_error: unknownSaveError },
  preview_reason: hooks.firstPreviewErrorReason([{ error_count: 1, errors: [{ reason: "phase_not_enabled" }] }])
}));
'''
    completed = subprocess.run(
        ["node", "-e", script, str(ROOT / "static" / "ad-control-pages.js")],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


class AccountCopyRuleUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = run_payload_harness()

    def test_rule_page_has_no_product_dimension(self):
        self.assertNotIn("产品筛选", RULE_BLOCK)
        self.assertNotIn("drawerProducts", RULE_BLOCK)
        self.assertNotIn("accountsByProduct", RULE_BLOCK)
        self.assertIn('api("/api/ad-control/rule-groups"', RULE_BLOCK)
        self.assertIn('api("/api/ad-control/accounts")', RULE_BLOCK)

    def test_object_action_and_run_mode_are_separate(self):
        self.assertIn("调控对象", RULE_BLOCK)
        self.assertIn("命中动作", RULE_BLOCK)
        self.assertIn("运行模式", RULE_BLOCK)
        self.assertIn('<option value="pause">关闭</option>', RULE_BLOCK)
        self.assertIn('<option value="copy">复制</option>', RULE_BLOCK)
        action_select = RULE_BLOCK[RULE_BLOCK.index('id="builderAction"') : RULE_BLOCK.index('id="builderTimezones"')]
        self.assertNotIn('value="observe"', action_select)
        self.assertIn('<option value="ad">广告 Ad（第二阶段仅保存）</option>', RULE_BLOCK)
        self.assertIn("phase_not_enabled", RULE_BLOCK)
        self.assertNotIn("持续观察筛选结果", RULE_BLOCK)

    def test_copy_controls_cover_budget_schedule_and_drama(self):
        for value in (
            "actual_cpi_multiplier",
            "fixed_target_cpi_multiplier",
            "source_budget_ratio",
            "drawerFixedTargetCpi",
            "drawerAllowedStart",
            "drawerExecuteBefore",
            "drawerRuleDailyLimit",
            "drawerUserDailyLimit",
            "drawerSourceCooldown",
            "drawerDramaScope",
            "drawerDramaIds",
            "drawerCandidateSelectionMode",
            "drawerTopN",
            "drawerRoasDirection",
            "drawerRoasPercent",
        ):
            self.assertIn(value, RULE_BLOCK)
        self.assertIn("item.copy =", RULE_BLOCK)
        self.assertIn("item.drama_scope", RULE_BLOCK)
        self.assertIn("item.candidate_selection", RULE_BLOCK)
        self.assertIn("item.top_n_per_account", RULE_BLOCK)
        self.assertIn('<option value="all">全部符合条件</option>', RULE_BLOCK)
        self.assertIn('<option value="top_n_per_account">每账号 Top N</option>', RULE_BLOCK)
        self.assertIn('candidateSelectionMode !== "top_n_per_account"', RULE_BLOCK)

    def test_live_mode_requires_fixed_confirmation_phrase(self):
        self.assertIn("ENABLE_LIVE_MODE", RULE_BLOCK)
        self.assertIn("二次确认", RULE_BLOCK)
        self.assertIn("enabled: false", RULE_BLOCK)
        self.assertIn('run_mode: runMode', RULE_BLOCK)
        enable_block = RULE_BLOCK[
            RULE_BLOCK.index("async function setFrontendRuleGroupEnabled") :
            RULE_BLOCK.index("async function emergencyStopFrontendRuleGroup")
        ]
        self.assertIn('live_mode_confirm: "ENABLE_LIVE_MODE"', enable_block)
        self.assertIn('group.run_mode === "live"', enable_block)

    def test_ad_group_cannot_be_enabled_in_any_run_mode(self):
        enable_block = RULE_BLOCK[
            RULE_BLOCK.index("async function setFrontendRuleGroupEnabled") :
            RULE_BLOCK.index("async function emergencyStopFrontendRuleGroup")
        ]
        self.assertIn('if (group.object_level === "ad")', enable_block)
        self.assertIn("phase_not_enabled", enable_block)
        self.assertNotIn('group.object_level === "ad" && group.run_mode === "live"', enable_block)

    def test_save_payload_is_account_only(self):
        payload_block = RULE_BLOCK[
            RULE_BLOCK.index("function readRuleGroupDraftFromDrawer()") :
            RULE_BLOCK.index("function updateRuleGroupSummary()")
        ]
        self.assertIn("account_ids: accountIds", payload_block)
        self.assertIn("object_level: objectLevel", payload_block)
        self.assertNotIn("product:", payload_block)
        self.assertNotIn("owner", payload_block)

    def test_form_values_override_legacy_copy_payload(self):
        payload = self.runtime["payload"]
        rule = payload["rules"][0]
        self.assertEqual("fixed_target_cpi_multiplier", rule["copy"]["budget"]["type"])
        self.assertEqual(2.5, rule["copy"]["budget"]["multiplier"])
        self.assertEqual(4.2, rule["copy"]["budget"]["target_cpi"])
        self.assertEqual({"direction": "decrease", "percent": 15}, rule["copy"]["roas_bid"])
        self.assertEqual("specified", rule["drama_scope"]["type"])
        self.assertEqual(["new-a", "new-b"], rule["drama_scope"]["drama_ids"])
        self.assertEqual({"mode": "top_n_per_account", "top_n": 2, "legacy_selection": "keep"}, rule["candidate_selection"])
        self.assertEqual(2, rule["top_n_per_account"])
        self.assertEqual(["legacy-a", "legacy-b"], payload["migrate_from_group_ids"])

    def test_secondary_copy_rule_parameters_are_preserved(self):
        secondary = self.runtime["payload"]["rules"][1]
        self.assertEqual(
            {
                "budget": {
                    "type": "source_budget_ratio",
                    "mode": "source_budget_ratio",
                    "ratio": 0.77,
                    "secondary_budget": "keep",
                },
                "roas_bid": {"direction": "increase", "percent": 33},
                "secondary_copy": "keep",
            },
            secondary["copy"],
        )
        self.assertEqual(
            {"type": "specified", "drama_ids": ["secondary-drama"], "secondary_scope": "keep"},
            secondary["drama_scope"],
        )
        self.assertEqual(
            {"mode": "top_n_per_account", "top_n": 5, "secondary_selection": "keep"},
            secondary["candidate_selection"],
        )
        self.assertEqual(5, secondary["top_n_per_account"])
        self.assertEqual("keep", secondary["secondary_rule"])

    def test_primary_builder_merge_keeps_following_rules(self):
        merged = self.runtime["merged_primary"]
        self.assertEqual("pause", merged[0]["action"])
        self.assertNotIn("copy", merged[0])
        self.assertEqual(
            {"name": "secondary", "action": "copy", "copy": {"untouched": True}},
            merged[1],
        )

    def test_partial_state_and_legacy_actions_are_explicit(self):
        self.assertEqual(
            {"enabled": False, "partial_enabled": True, "enabled_count": 1, "target_ids": ["legacy-a", "legacy-b"]},
            self.runtime["partial"],
        )
        self.assertEqual({"run_mode": "observe", "action": "pause", "migrated": True}, self.runtime["legacy"])
        self.assertEqual("mystery", self.runtime["unknown"]["action"])
        self.assertEqual(["mystery"], self.runtime["unknown"]["unknown_actions"])
        self.assertIn("action 仅允许 pause 或 copy", self.runtime["unknown"]["save_error"])
        self.assertIn("旧规则动作 observe", RULE_BLOCK)

    def test_preview_toast_reports_first_error_reason(self):
        self.assertEqual("phase_not_enabled", self.runtime["preview_reason"])
        self.assertIn("首个原因", RULE_BLOCK)

    def test_action_summary_does_not_treat_unknown_as_pause(self):
        self.assertIn('String(rule.action || "").toLowerCase() === "pause"', RULE_BLOCK)

    def test_rules_page_has_feature_cache_buster(self):
        html = (ROOT / "static" / "ad-control-rules.html").read_text(encoding="utf-8")
        self.assertIn("ad-control-pages.js?v=20260715copylog3", html)
        self.assertIn("ad-control-pages.css?v=20260715copylog3", html)

    def test_log_page_defaults_to_all_products_and_lists_v2_actions(self):
        log_block = JS[JS.index("async function renderLogs()") : JS.index("function logStatusBadge")]
        self.assertIn("await loadProducts({ includeAll: true });", log_block)
        self.assertIn("全部产品（含账号规则）", JS)
        self.assertIn('<option value="copy">复制 copy</option>', log_block)
        self.assertIn('<option value="mixed">混合 mixed</option>', log_block)
        self.assertIn('optionHtml(state.bindings, "binding_id", "name", "全部规则组")', log_block)
        self.assertIn("binding_id=${encodeURIComponent(id)}", JS)

    def test_non_log_product_pages_keep_strict_product_default(self):
        load_block = JS[JS.index("async function loadProducts") : JS.index("async function loadAccounts")]
        self.assertIn("const includeAll = !!options.includeAll;", load_block)
        self.assertIn("const items = includeAll", load_block)
        self.assertIn('? [{ product: "", label:', load_block)
        self.assertIn("else if (previous && ALLOWED_PRODUCTS.includes(previous))", load_block)


if __name__ == "__main__":
    unittest.main()
