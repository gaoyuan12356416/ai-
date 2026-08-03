#!/usr/bin/env python3
"""Static frontend contracts for the TikTok Post publishing pool."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE_PATH = ROOT / "static" / "tt-post-pool.html"
QUICK_NAV_PATH = ROOT / "static" / "quick-nav.js"
NAVIGATION_PATH = ROOT / "static" / "navigation.json"

PAGE = PAGE_PATH.read_text(encoding="utf-8")
QUICK_NAV = QUICK_NAV_PATH.read_text(encoding="utf-8")
NAVIGATION = json.loads(NAVIGATION_PATH.read_text(encoding="utf-8"))


def inline_javascript(source: str) -> str:
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", source, flags=re.DOTALL)
    return "\n".join(script for script in scripts if script.strip())


def source_between(start: str, end: str) -> str:
    start_index = PAGE.index(start)
    end_index = PAGE.index(end, start_index)
    return PAGE[start_index:end_index]


class TtPostPoolUiTest(unittest.TestCase):
    def test_page_javascript_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        with tempfile.TemporaryDirectory() as directory:
            script_path = Path(directory) / "tt-post-pool-inline.js"
            script_path.write_text(inline_javascript(PAGE), encoding="utf-8")
            result = subprocess.run(
                [node, "--check", str(script_path)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_page_uses_existing_shell_and_tt_permission(self):
        self.assertIn('href="/ui-topbar.css"', PAGE)
        self.assertIn('src="/ui-topbar.js"', PAGE)
        self.assertIn('src="/quick-nav.js"', PAGE)
        self.assertIn('api("/api/ui/topbar")', PAGE)
        self.assertIn('activeKey: "ttPostPool"', PAGE)
        self.assertIn("user.permissions.tt_posts", PAGE)
        self.assertIn('id="loginGate"', PAGE)
        self.assertIn('id="permissionGate"', PAGE)

    def test_navigation_keeps_tt_post_pool_registered(self):
        group = next(item for item in NAVIGATION if item.get("key") == "tiktok_platform")
        item = next(entry for entry in group["items"] if entry.get("key") == "ttPostPool")
        self.assertEqual(item["href"], "/tt-post-pool.html")
        self.assertEqual(item["module"], "tt_posts")
        self.assertTrue(item["enabled"])
        self.assertIn('ttPostPool: "/tt-post-pool.html"', QUICK_NAV)
        self.assertIn('key: "ttPostPool"', QUICK_NAV)

    def test_page_does_not_embed_credentials_or_use_unsafe_html_sinks(self):
        for secret_fragment in ("AKIDvgCX", "KTN3WwHT", "secret-key", "secret-id"):
            self.assertNotIn(secret_fragment, PAGE)
        self.assertNotIn(".innerHTML", PAGE)
        self.assertNotIn("document.write", PAGE)

    def test_unified_config_and_direct_test_controls_exist(self):
        required_ids = {
            "accountList",
            "selectedAccountSummary",
            "operationAccountId",
            "caption",
            "dailyPublishTime",
            "scheduleEnabled",
            "saveSchedule",
            "scheduleStatus",
            "autoAccountCount",
            "scheduleVersion",
            "scheduleDraftStatus",
            "directTestSelection",
            "runNow",
            "runNowHelp",
            "directTestSummary",
            "directTestRows",
            "publishConsent",
            "preparationRows",
        }
        for field_id in required_ids:
            self.assertIn(f'id="{field_id}"', PAGE)
        self.assertIn("账号、描述、时间或开关已修改；统一保存后生效。", PAGE)

    def test_every_javascript_dom_reference_exists(self):
        html_ids = set(re.findall(r'\bid="([^"]+)"', PAGE))
        javascript_ids = set(re.findall(r'byId\("([^"]+)"\)', PAGE))
        self.assertEqual(javascript_ids - html_ids, set())

    def test_auto_publish_accounts_are_multi_select_checkboxes(self):
        render_accounts = source_between(
            "function renderAccounts()", "function resetCreatorInfo"
        )
        self.assertIn('role="group"', PAGE)
        self.assertNotIn('role="radiogroup"', PAGE)
        self.assertIn('checkbox.type = "checkbox"', render_accounts)
        self.assertIn("checkbox.dataset.autoAccountId = id", render_accounts)
        self.assertIn("state.autoAccountIds.has(id)", render_accounts)
        self.assertIn('input[data-auto-account-id]', PAGE)

    def test_account_membership_exposes_saved_and_draft_states(self):
        status_source = source_between(
            "function autoAccountStatus(id)", "function renderSelectedAccountSummary()"
        )
        for state_name in ('type: "active"', 'type: "paused"', 'type: "not_selected"'):
            self.assertIn(state_name, status_source)
        self.assertIn('draft: "add"', status_source)
        self.assertIn('draft: "remove"', status_source)
        self.assertIn("已加入自动发布", status_source)
        self.assertIn("自动发布关闭", status_source)
        self.assertIn("未加入自动发布", status_source)
        self.assertIn('savedState === "attention_required"', status_source)
        self.assertIn("account.auto_publish_config_version", status_source)
        self.assertIn("savedStateVersion === currentConfigVersion", status_source)
        self.assertIn("savedStateIsCurrent", status_source)
        self.assertIn('type: "attention_required"', status_source)
        self.assertIn("已加入 · 需要处理", status_source)
        self.assertIn("label.dataset.autoStatus = membership.type", PAGE)
        self.assertIn("label.dataset.draftChange = membership.draft", PAGE)

    def test_operation_account_is_an_independent_explicit_single_select(self):
        options_source = source_between(
            "function renderOperationAccountOptions()", "function renderAccounts()"
        )
        event_source = source_between(
            'byId("operationAccountId").addEventListener',
            'byId("refreshAccounts").addEventListener',
        )
        self.assertRegex(PAGE, r'<select id="operationAccountId"[^>]*>')
        self.assertIn('placeholder.value = ""', options_source)
        self.assertIn('setText(placeholder, "请明确选择一个账号")', options_source)
        self.assertNotIn("state.autoAccountIds.values", options_source)
        self.assertNotIn("state.autoAccountIds.forEach", options_source)
        self.assertIn("state.selectedAccountId = cleanId(event.target.value)", event_source)

    def test_auto_config_loads_from_the_atomic_endpoint(self):
        load_source = source_between("async function loadSchedule", "async function saveSchedule")
        self.assertIn('api(`${API_BASE}/auto-config`)', load_source)
        self.assertIn("applySchedule(result", load_source)
        self.assertNotIn("/schedule", load_source)

    def test_auto_config_save_is_one_atomic_request_with_all_fields(self):
        save_source = source_between("async function saveSchedule", "function validPendingRunNowId")
        self.assertEqual(save_source.count('api(`${API_BASE}/auto-config`'), 1)
        self.assertIn('method: "POST"', save_source)
        self.assertIn("expected_version: expectedVersion", save_source)
        self.assertIn("enabled: requestedEnabled", save_source)
        self.assertIn('timezone: "Asia/Shanghai"', save_source)
        self.assertIn("publish_times: [requestedPublishTime]", save_source)
        self.assertIn("source_account_ids: Array.from(state.autoAccountIds)", save_source)
        self.assertIn('caption_template: byId("caption").value', save_source)
        self.assertIn("consent:", save_source)
        self.assertNotIn("for (", save_source)

    def test_config_draft_uses_version_conflict_protection(self):
        apply_source = source_between("function applySchedule", "async function loadSchedule")
        save_source = source_between("async function saveSchedule", "function validPendingRunNowId")
        self.assertIn("scheduleDraftBaseVersion", PAGE)
        self.assertIn("scheduleStale", PAGE)
        self.assertIn("normalized.version !== state.scheduleDraftBaseVersion", apply_source)
        self.assertIn("expected_version: expectedVersion", save_source)
        self.assertIn("tt_post_auto_config_version_conflict", save_source)
        self.assertIn("preserveDraft: true", save_source)

    def test_disabling_auto_publish_does_not_require_publish_consent(self):
        validation_source = source_between(
            "function scheduleSaveError()", "function runNowDisabledReason()"
        )
        self.assertIn("captionTemplateValidation([])", validation_source)
        self.assertIn(
            'if (byId("scheduleEnabled").checked && !byId("publishConsent").checked)',
            validation_source,
        )
        self.assertNotIn(
            'if (!byId("publishConsent").checked)',
            validation_source,
        )

    def test_validated_materials_are_explicit_direct_test_choices(self):
        render_source = source_between(
            "function renderMaterialResults()", "function renderQueueSubmitResults()"
        )
        validation_source = source_between(
            "async function validateMaterials()", "function normalizeDramaDescription"
        )
        click_source = source_between(
            'byId("materialResults").addEventListener',
            'byId("materialIds").addEventListener("input"',
        )
        self.assertIn('row.dataset.directMaterialId = materialId', render_source)
        self.assertIn('row.setAttribute("aria-pressed"', render_source)
        self.assertIn("state.materials.length === 1", validation_source)
        self.assertIn("state.selectedDirectMaterialId = cleanId(state.materials[0].material_id)", validation_source)
        self.assertIn("state.selectedDirectMaterialId = cleanId(button.dataset.directMaterialId)", click_source)

    def test_direct_test_button_has_only_direct_test_prerequisites(self):
        reason_source = source_between(
            "function runNowDisabledReason()", "function updateScheduleActions()"
        )
        for requirement in (
            "selectedAccount()",
            "selectedDirectMaterial()",
            "Number(state.schedule.version || 0) <= 0",
            "state.scheduleDraftDirty",
            "state.scheduleStale",
            "accountEligible(item)",
            "selectedAccountSettings()",
            "state.creatorInfo",
            'settings.privacy_level !== "PUBLIC_TO_EVERYONE"',
            "boolValue(settings.allow_comment) !== true",
            "gatesOpen()",
            "captionTemplateValidation([material])",
            'byId("publishConsent").checked',
        ):
            self.assertIn(requirement, reason_source)
        for forbidden in (
            "available_material_count",
            "can_publish_now",
            "publication_status",
            "preparationItems",
            "material-pool",
            "savedAutoAccountIds",
        ):
            self.assertNotIn(forbidden, reason_source)

    def test_pool_add_requires_saved_auto_publish_membership_but_direct_test_does_not(self):
        pool_validation = source_between(
            "function formValidationError()", "function validateForm()"
        )
        direct_validation = source_between(
            "function runNowDisabledReason()", "function updateScheduleActions()"
        )
        self.assertIn("Number(state.schedule.version || 0) <= 0", pool_validation)
        self.assertIn("state.savedAutoAccountIds.has(accountId(item))", pool_validation)
        self.assertNotIn("savedAutoAccountIds", direct_validation)

    def test_page_copy_distinguishes_pool_consumption_from_direct_test(self):
        self.assertNotIn("每日到点与立即发布才会消费下一条", PAGE)
        self.assertNotIn("自动和立即发布均不会发送到 TikTok，也不会消费待发素材", PAGE)
        self.assertIn("自动发布才会按时消费素材池中的下一条", PAGE)
        self.assertIn("立即测试使用明确选择的已校验素材独立发布，不占用或消费素材池", PAGE)

    def test_direct_test_posts_the_selected_material_without_using_the_pool(self):
        run_source = source_between("async function runNow()", "async function loadDirectTests")
        self.assertIn('api(`${API_BASE}/test-publish`', run_source)
        self.assertIn('method: "POST"', run_source)
        self.assertIn("source_account_id: requestedAccountId", run_source)
        self.assertIn("material_id: requestedMaterialId", run_source)
        self.assertIn("expected_config_version:", run_source)
        self.assertIn("idempotency_key: requestKey", run_source)
        self.assertIn("consent:", run_source)
        self.assertNotIn("/material-pool", run_source)
        self.assertNotIn("/run-now", run_source)
        self.assertIn("历史已发布也允许再次测试", run_source)

    def test_direct_test_creation_statuses_are_treated_as_submitted(self):
        submitted_match = re.search(
            r"const RUN_NOW_SUBMITTED_STATUSES = new Set\(\[(.*?)\]\);",
            PAGE,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(submitted_match)
        submitted = submitted_match.group(1)
        for status in ("queued", "preparing", "ready", "publishing", "reconciling"):
            self.assertIn(f'"{status}"', submitted)
        classify_source = source_between(
            "function classifyRunNowResponse", "async function runNow()"
        )
        self.assertIn("RUN_NOW_SUBMITTED_STATUSES.has(status)", classify_source)

    def test_direct_test_idempotency_is_scoped_to_account_and_material(self):
        pending_source = source_between(
            "function pendingRunNowTarget", "function classifyRunNowResponse"
        )
        self.assertIn('return `${String(accountIdValue || "")}:${String(materialIdValue || "")}`', pending_source)
        self.assertIn('`tt-post:test-publish:${account}:${material}:`', pending_source)
        self.assertIn('"tt-post:pending-direct-test:v3"', PAGE)
        self.assertIn("account_id: account", pending_source)
        self.assertIn("material_id: material", pending_source)
        self.assertIn("expected_config_version", pending_source)
        self.assertIn("consent_accepted_at", pending_source)
        self.assertIn("direct_test_id", pending_source)

    def test_direct_test_pending_request_is_kept_until_terminal(self):
        pending_source = source_between(
            "function pendingRunNowTarget", "function classifyRunNowResponse"
        )
        run_source = source_between(
            "async function runNow()", "async function loadDirectTests"
        )
        load_source = source_between(
            "async function loadDirectTests", "async function loadAccounts()"
        )
        self.assertIn("function pendingRunNowRequestForTarget", pending_source)
        self.assertIn("if (existing) return existing", pending_source)
        self.assertIn(
            "expected_config_version: pendingRequest.expected_config_version",
            run_source,
        )
        self.assertIn(
            "accepted_at: pendingRequest.consent_accepted_at",
            run_source,
        )
        submitted = run_source.index('outcome.kind === "submitted"')
        submitted_end = run_source.index("} else {", submitted)
        submitted_source = run_source[submitted:submitted_end]
        self.assertIn("updatePendingRunNowEntry", submitted_source)
        self.assertNotIn("clearPendingRunNowKey", submitted_source)
        self.assertIn("reconcilePendingRunNowState(state.directTests)", load_source)
        self.assertIn("Number(error.status) >= 400", run_source)

    def test_direct_test_history_is_loaded_separately(self):
        source = source_between("async function loadDirectTests", "async function loadAccounts()")
        self.assertIn('api(`${API_BASE}/direct-tests?page=1&page_size=20`)', source)
        self.assertIn("state.directTests", source)
        self.assertIn('byId("directTestSummary")', source)
        status_source = source_between("function statusMeta(item)", "function appendCell")
        for direct_status in ("queued", "preparing", "ready", "publishing", "unknown", "canceled"):
            self.assertIn(f"{direct_status}:", status_source)

    def test_direct_test_history_renders_details_and_polls_nonterminal_tasks(self):
        render_source = source_between(
            "function renderDirectTests(items)", "function directTestPollingNeeded()"
        )
        polling_source = source_between(
            "function directTestPollingNeeded()", "async function runNow()"
        )
        load_source = source_between(
            "async function loadDirectTests", "async function loadAccounts()"
        )
        for field in (
            "item.id",
            "item.material_id",
            "item.source_account_id",
            "item.claim_phase",
            "item.gpu_job_id",
            "item.updated_at",
            "item.error_code",
            "item.error_message",
        ):
            self.assertIn(field, render_source)
        for status in ("queued", "preparing", "ready", "publishing", "reconciling"):
            self.assertIn(f'"{status}"', PAGE)
        self.assertIn("DIRECT_TEST_POLL_INTERVAL_MS = 10000", PAGE)
        self.assertIn("DIRECT_TEST_POLL_STATUSES", polling_source)
        self.assertIn("validPendingRunNowEntry", polling_source)
        self.assertIn('entry.status !== "unknown"', polling_source)
        self.assertIn(
            "loadDirectTests({ quiet: true, reschedule: false })",
            polling_source,
        )
        self.assertIn(".finally(syncDirectTestPolling)", polling_source)
        self.assertIn("renderDirectTests(state.directTests)", load_source)
        self.assertIn("syncDirectTestPolling()", load_source)

    def test_pool_add_is_version_bound_to_saved_config(self):
        payload_source = source_between(
            "function materialPoolPayload(material)", "function resetMaterialPoolForm()"
        )
        self.assertIn("source_account_id: state.selectedAccountId", payload_source)
        self.assertIn("material_id: material.material_id", payload_source)
        self.assertIn("expected_config_version:", payload_source)
        self.assertIn('caption_template: byId("caption").value', payload_source)
        self.assertIn("consent:", payload_source)

    def test_publication_status_is_independent_from_pool_availability(self):
        publication_source = source_between(
            "function publicationStatusMeta(item)", "function poolAvailabilityMeta(item)"
        )
        availability_source = source_between(
            "function poolAvailabilityMeta(item)", "function preparationStatusMeta(item)"
        )
        rows_source = source_between(
            "function renderPreparationRows(items)", "function renderPreparationSummary(result, items)"
        )
        self.assertIn('item.publication_status || "unpublished"', publication_source)
        self.assertIn('status: "published"', publication_source)
        self.assertIn('status: "unknown"', publication_source)
        self.assertIn('status: "unpublished"', publication_source)
        self.assertNotIn("item.status", publication_source)
        self.assertIn("consumed:", availability_source)
        self.assertIn("已消费（以发布状态为准）", availability_source)
        self.assertNotIn('consumed: ["已发布"', availability_source)
        self.assertIn("publicationStatusMeta(item)", rows_source)
        self.assertIn("poolAvailabilityMeta(item)", rows_source)

    def test_pool_table_shows_publication_availability_and_preparation_columns(self):
        table_source = source_between('<table class="preparation-table">', "</table>")
        for heading in ("发布状态", "可用状态", "预制作状态"):
            self.assertIn(f"<th>{heading}</th>", table_source)
        self.assertIn("cell.colSpan = 8", PAGE)
        self.assertIn("publicationCounts", PAGE)
        self.assertIn("summary.unknown_publication", PAGE)
        self.assertIn("allPublicationCounts.unpublished", PAGE)

    def test_caption_supports_url_desc_and_both_drama_id_macros(self):
        self.assertIn('new Set(["contect_id", "content_id"])', PAGE)
        self.assertIn('const CAPTION_URL_PLACEHOLDER = "{url}"', PAGE)
        self.assertIn('const CAPTION_DESC_PLACEHOLDER = "{desc}"', PAGE)
        render_source = source_between(
            "function renderCaptionTemplate", "function captionTemplateValidation"
        )
        validation_source = source_between(
            "function captionTemplateValidation", "function updateCaptionState"
        )
        self.assertIn('if (name === "url") return CAPTION_URL_PREVIEW', render_source)
        self.assertIn('if (name === "desc") return normalizedDescription', render_source)
        self.assertIn('["url", "desc"].includes(name)', validation_source)
        self.assertIn("缺少有效剧描述", validation_source)

    def test_material_validation_is_sequential_and_bounded(self):
        parse_source = source_between("function parseMaterialIds()", "function updateMaterialProgress")
        validate_source = source_between(
            "async function validateMaterials()", "function normalizeDramaDescription"
        )
        self.assertIn("ids.length > 100", parse_source)
        self.assertIn("for (let index = 0; index < materialIds.length; index += 1)", validate_source)
        self.assertIn('api(`${API_BASE}/materials/preview`', validate_source)
        self.assertNotIn("Promise.all", validate_source)

    def test_account_settings_remain_read_only_on_this_page(self):
        self.assertIn('href="/tt-account-settings.html"', PAGE)
        self.assertIn("item && item.account_settings", PAGE)
        self.assertIn("settings.configured === true", PAGE)
        self.assertNotIn('api(`${API_BASE}/account-settings`, {', PAGE)

    def test_initial_load_fetches_config_direct_tests_pool_and_preparation(self):
        init_source = source_between("async function init()", 'byId("accountSearch")')
        for loader in (
            "loadAccounts()",
            "loadSchedule()",
            "loadDirectTests()",
            "loadQueue()",
            "loadPreparationStatus()",
        ):
            self.assertIn(loader, init_source)

    def test_publish_task_table_uses_the_unified_server_projection(self):
        query_source = source_between(
            "function queueQuery()", "function updateStats"
        )
        load_source = source_between(
            "async function loadQueue()", "function setEventsEmpty"
        )
        self.assertIn('id="filterTaskType"', PAGE)
        self.assertIn('value="automatic"', PAGE)
        self.assertIn('value="direct_test"', PAGE)
        self.assertIn('task_type: byId("filterTaskType").value || "all"', query_source)
        self.assertIn('api(`${API_BASE}/tasks?${queueQuery()}`)', load_source)
        self.assertNotIn('api(`${API_BASE}/queue?${queueQuery()}`)', load_source)
        self.assertIn("state.publishTasks = items", load_source)

    def test_direct_rows_are_namespaced_and_never_receive_queue_actions(self):
        rows_source = source_between(
            "function renderQueueRows(items)", "function queueQuery()"
        )
        action_start = rows_source.index('actionList.className = "queue-actions"')
        action_source = rows_source[action_start:]
        direct_start = action_source.index("if (directTest)")
        automatic_start = action_source.index("} else {", direct_start)
        direct_branch = action_source[direct_start:automatic_start]
        automatic_branch = action_source[automatic_start:]
        self.assertIn('item.task_type === "direct_test"', rows_source)
        self.assertIn("directDetailsButton(item)", direct_branch)
        self.assertNotIn("queueActionButton", direct_branch)
        for operation in ('"events"', '"cancel"', '"reconcile"'):
            self.assertIn(operation, automatic_branch)

        details_source = source_between(
            "function directDetailsButton(item)", "function renderQueueRows(items)"
        )
        self.assertIn("button.dataset.taskKey", details_source)
        self.assertNotIn("dataset.queueId", details_source)

    def test_direct_task_details_are_read_only_and_poll_with_the_table(self):
        detail_source = source_between(
            "function directTaskTimeline(item)", "async function runQueueAction"
        )
        click_source = source_between(
            'byId("queueRows").addEventListener', 'byId("closeEvents")'
        )
        polling_source = source_between(
            "function syncDirectTestPolling()", "async function runNow()"
        )
        self.assertIn('task.task_type === "direct_test"', detail_source)
        self.assertIn("task.task_key === taskKey", detail_source)
        self.assertIn("item.created_at", detail_source)
        self.assertIn("item.published_at_utc", detail_source)
        self.assertIn('action === "direct-details"', click_source)
        self.assertIn("openDirectTaskDetails(button.dataset.taskKey)", click_source)
        self.assertIn(
            "loadDirectTests({ quiet: true, reschedule: false })",
            polling_source,
        )
        self.assertIn("loadQueue()", polling_source)
        load_source = source_between(
            "async function loadQueue()", "function setEventsEmpty"
        )
        self.assertIn("const requestVersion = ++state.queueRequestVersion", load_source)
        self.assertGreaterEqual(
            load_source.count("requestVersion !== state.queueRequestVersion"),
            2,
        )


if __name__ == "__main__":
    unittest.main()
