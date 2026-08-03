#!/usr/bin/env python3
"""Static frontend contracts for the TikTok Post publishing pool."""

from __future__ import annotations

import json
import re
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


def source_between_text(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


class TtPostPoolUiTest(unittest.TestCase):
    def test_navigation_registers_tt_pool_with_standalone_permission(self):
        group = next(item for item in NAVIGATION if item.get("key") == "tiktok_platform")
        self.assertEqual(group["label"], "TikTok 社媒")
        self.assertEqual(group["order"], 35)
        self.assertEqual(group["module"], "tt_posts")
        settings = next(
            entry
            for entry in group["items"]
            if entry.get("key") == "ttAccountSettings"
        )
        self.assertEqual(settings["label"], "TT 个号管理")
        self.assertEqual(settings["href"], "/tt-account-settings.html")
        self.assertEqual(settings["module"], "tt_posts")
        self.assertEqual(settings["order"], 10)
        self.assertFalse(settings["adminOnly"])
        self.assertTrue(settings["enabled"])
        item = next(entry for entry in group["items"] if entry.get("key") == "ttPostPool")
        self.assertEqual(item["label"], "TT Post发布池")
        self.assertEqual(item["href"], "/tt-post-pool.html")
        self.assertEqual(item["module"], "tt_posts")
        self.assertEqual(item["order"], 20)
        self.assertFalse(item["adminOnly"])
        self.assertTrue(item["enabled"])

        self.assertIn(
            'ttAccountSettings: "/tt-account-settings.html"',
            QUICK_NAV,
        )
        self.assertIn('ttPostPool: "/tt-post-pool.html"', QUICK_NAV)
        self.assertIn('key: "tiktok_platform"', QUICK_NAV)
        self.assertIn('key: "ttAccountSettings"', QUICK_NAV)
        self.assertIn('key: "ttPostPool"', QUICK_NAV)
        self.assertIn('module: "tt_posts"', QUICK_NAV)
        self.assertIn("if (!tiktokPlatform)", QUICK_NAV)
        self.assertIn("if (!ttAccountSettingsExists)", QUICK_NAV)
        self.assertIn("if (!ttPostPoolExists)", QUICK_NAV)
        self.assertIn('quickNavConfigCache:v5', QUICK_NAV)

    def test_page_uses_existing_shell_and_tt_permission(self):
        self.assertIn('href="/ui-topbar.css"', PAGE)
        self.assertIn('src="/ui-topbar.js"', PAGE)
        self.assertIn('src="/quick-nav.js"', PAGE)
        self.assertIn('api("/api/ui/topbar")', PAGE)
        self.assertIn('activeKey: "ttPostPool"', PAGE)
        self.assertIn("user.permissions.tt_posts", PAGE)
        self.assertIn('id="loginGate"', PAGE)
        self.assertIn('id="permissionGate"', PAGE)

    def test_create_form_covers_account_material_daily_schedule_and_consent(self):
        required_ids = {
            "accountSearch",
            "refreshAccounts",
            "accountList",
            "creatorCard",
            "materialIds",
            "previewMaterial",
            "materialProgress",
            "materialResults",
            "caption",
            "dailyPublishTime",
            "scheduleEnabled",
            "saveSchedule",
            "scheduleStatus",
            "scheduleNextRun",
            "scheduleVersion",
            "availableMaterialCount",
            "preparingMaterialCount",
            "runNow",
            "runNowHelp",
            "accountSettingsCard",
            "accountSettingsStatus",
            "accountSettingsSummary",
            "queueSubmitPanel",
            "queueSubmitSummary",
            "queueSubmitResults",
            "publishConsent",
            "addMaterialsToPool",
            "preparationPanel",
            "refreshPreparationStatus",
            "preparationSummary",
            "preparationRows",
        }
        for element_id in required_ids:
            self.assertIn(f'id="{element_id}"', PAGE)

        self.assertIn('type="search"', PAGE)
        self.assertIn('type="time"', PAGE)
        self.assertNotIn('type="datetime-local"', PAGE)
        self.assertIn('schedulePayload.timezone = "Asia/Shanghai"', PAGE)
        self.assertIn("source_account_id: state.selectedAccountId", PAGE)
        self.assertNotIn("Number(state.selectedAccountId)", PAGE)
        self.assertIn("material_id: material.material_id", PAGE)
        self.assertIn("content_id: material.content_id", PAGE)
        self.assertIn("materials: []", PAGE)
        self.assertIn("failures: []", PAGE)
        self.assertIn("CONSENT_VERSION", PAGE)
        self.assertIn("Music Usage Confirmation", PAGE)
        self.assertIn('href="/tt-account-settings.html"', PAGE)

    def test_page_only_calls_same_origin_admin_contracts(self):
        for endpoint in (
            "${API_BASE}/accounts",
            "${API_BASE}/creator-info",
            "${API_BASE}/materials/preview",
            "${API_BASE}/schedule?",
            "${API_BASE}/schedule`",
            "${API_BASE}/material-pool",
            "${API_BASE}/run-now",
            "${API_BASE}/queue",
            "${API_BASE}/events?",
        ):
            self.assertIn(endpoint, PAGE)
        self.assertIn('const API_BASE = "/api/admin/tt-posts"', PAGE)
        self.assertNotIn("open.tiktokapis.com", PAGE)

    def test_batch_material_input_normalizes_deduplicates_and_isolates_failures(self):
        material_input = re.search(
            r'<textarea id="materialIds"(?P<attrs>[^>]*)>',
            PAGE,
        )
        self.assertIsNotNone(material_input)
        self.assertIn('inputmode="numeric"', material_input.group("attrs"))
        self.assertIn("function normalizeMaterialIdToken(value, index)", PAGE)
        self.assertIn("function parseMaterialIds()", PAGE)
        self.assertIn(r"raw.split(/[\s,，;；]+/)", PAGE)
        self.assertIn(r"!/^\d+$/.test(value)", PAGE)
        self.assertIn('value.replace(/^0+/, "")', PAGE)
        self.assertIn(r"!/^[1-9]\d{0,18}$/.test(withoutLeadingZeros)", PAGE)
        self.assertIn("String(BigInt(withoutLeadingZeros))", PAGE)
        self.assertIn("Array.from(new Set(", PAGE)
        self.assertIn("if (ids.length > 100)", PAGE)

        validation = source_between(
            "async function validateMaterials()",
            "function renderCaptionTemplate(",
        )
        self.assertIn(
            "for (let index = 0; index < materialIds.length; index += 1)",
            validation,
        )
        self.assertIn("await api(`${API_BASE}/materials/preview`", validation)
        self.assertIn("state.materials.push(", validation)
        self.assertIn("state.failures.push(", validation)
        self.assertIn("renderMaterialResults()", validation)
        self.assertIn("通过 ${state.materials.length} 个", validation)
        self.assertIn("失败 ${state.failures.length} 个", validation)
        self.assertLess(
            validation.index("materialIds = parseMaterialIds()"),
            validation.index("await api(`${API_BASE}/materials/preview`"),
        )
        self.assertNotIn("Promise.all(", validation)
        self.assertIn("批量校验素材", PAGE)
        self.assertIn("校验通过", PAGE)
        self.assertNotIn('id="mediaPreview"', PAGE)
        self.assertNotIn('id="videoShell"', PAGE)

    def test_caption_template_is_visible_editable_and_rendered_per_material(self):
        expected = (
            "Watch the full story in the app 🎬\\n\\n"
            "Drama ID: {{contect_id}}\\n\\n"
            "Visit my profile → Open the link → Search the Drama ID → Watch now."
        )
        self.assertIn(f'const CAPTION_TEMPLATE = "{expected}";', PAGE)
        caption = re.search(
            r'<textarea id="caption"(?P<attrs>[^>]*)>(?P<body>.*?)</textarea>',
            PAGE,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(caption)
        self.assertIn('maxlength="2200"', caption.group("attrs"))
        self.assertNotIn("readonly", caption.group("attrs"))
        self.assertNotIn("disabled", caption.group("attrs"))
        self.assertEqual(
            caption.group("body"),
            expected.replace("\\n", "\n"),
        )
        self.assertIn('byId("caption").addEventListener("input"', PAGE)
        self.assertIn("byId(\"caption\").value = CAPTION_TEMPLATE", PAGE)
        self.assertIn("CAPTION_PLACEHOLDERS", PAGE)
        self.assertIn('new Set(["contect_id", "content_id"])', PAGE)
        self.assertIn("function captionTemplateValidation()", PAGE)
        self.assertIn("发布描述模板包含未知占位符", PAGE)
        self.assertIn("发布描述模板包含不完整占位符", PAGE)
        self.assertIn('const CAPTION_URL_PLACEHOLDER = "{url}";', PAGE)
        self.assertIn('const CAPTION_DESC_PLACEHOLDER = "{desc}";', PAGE)
        self.assertIn(
            "https://gy.g2flow.com/s2l/8999999999999999999.html",
            PAGE,
        )
        self.assertIn("CAPTION_SINGLE_PLACEHOLDER_PATTERN", PAGE)
        self.assertIn(
            'singlePlaceholders.filter(name => !["url", "desc"].includes(name))',
            PAGE,
        )
        self.assertIn(
            "braceRemainder.includes(\"{\") ||",
            PAGE,
        )
        self.assertIn(
            'if (name === "url") return CAPTION_URL_PREVIEW;',
            PAGE,
        )
        self.assertIn('if (name === "desc") return normalizedDescription;', PAGE)
        self.assertIn("function normalizeDramaDescription(value)", PAGE)
        self.assertIn("缺少有效剧描述，不能使用 {desc}", PAGE)
        self.assertIn("模板中的换行会原样提交给 TikTok", PAGE)
        self.assertIn("material.description", PAGE)
        self.assertIn("if (units > 2200)", PAGE)
        self.assertIn("caption_template: byId(\"caption\").value", PAGE)
        self.assertIn("utf16Units", PAGE)
        payload = source_between(
            "function materialPoolPayload(material)",
            "function resetMaterialPoolForm()",
        )
        self.assertNotIn("description:", payload)

    def test_material_validation_does_not_wait_for_or_require_final_video(self):
        validation = source_between(
            "async function validateMaterials()",
            "function renderCaptionTemplate(",
        )
        form_validation = source_between(
            "function formValidationError()",
            "function validateForm()",
        )
        self.assertIn(
            'body: JSON.stringify({ material_id: materialId })',
            validation,
        )
        self.assertIn("const contentId = cleanId(item.content_id)", validation)
        for forbidden in (
            "prepared_media_url",
            "final_media_url",
            "output_sha256",
            "duration_sec",
            "final_duration_sec",
            "renderVideoPreview",
            "renderMaterial(",
        ):
            self.assertNotIn(forbidden, validation)
            self.assertNotIn(forbidden, form_validation)
        self.assertIn(
            "请先批量校验素材，并至少确认一个真实 Drama ID。",
            form_validation,
        )
        self.assertNotIn("creatorInfo", form_validation)
        self.assertIn(
            'if (!state.creatorInfo) return "请等待 TikTok 账号实时确认完成。";',
            source_between(
                "function scheduleSaveError()",
                "function runNowDisabledReason()",
            ),
        )
        self.assertIn("校验通过即可入池", PAGE)
        self.assertIn("视频将在后台预制作", PAGE)

    def test_daily_schedule_uses_time_of_day_versioning_and_account_scope(self):
        publish_time = re.search(
            r'<input id="dailyPublishTime"(?P<attrs>[^>]*)>',
            PAGE,
        )
        self.assertIsNotNone(publish_time)
        for attribute in (
            'type="time"',
            'step="60"',
            'value="11:00"',
        ):
            self.assertIn(attribute, publish_time.group("attrs"))
        self.assertIn("自动发布设置", PAGE)
        self.assertIn("启用素材池自动发布", PAGE)
        self.assertIn("保存发布设置", PAGE)
        self.assertIn("不同账号需选择不同的分钟", PAGE)
        self.assertIn("每天固定时间自动消费下一条", PAGE)
        self.assertIn(
            "async function loadSchedule({ preserveDraft = false, allowWhileSaving = false } = {})",
            PAGE,
        )
        save = source_between(
            "async function saveSchedule()",
            "function pendingRunNowKeyForAccount(accountIdValue)",
        )
        self.assertIn("await api(`${API_BASE}/schedule`", save)
        self.assertIn('const requestedEnabled = byId("scheduleEnabled").checked', save)
        self.assertIn('const requestedPublishTime = byId("dailyPublishTime").value', save)
        self.assertIn("enabled: requestedEnabled", save)
        self.assertIn("schedulePayload.publish_time = requestedPublishTime", save)
        self.assertIn('schedulePayload.timezone = "Asia/Shanghai"', save)
        self.assertIn(
            "const expectedVersion = Number(state.schedule && state.schedule.version || 0)",
            save,
        )
        self.assertIn("expected_version: expectedVersion", save)
        self.assertNotIn("scheduled_at:", save)
        self.assertNotIn("scheduleIntervalMinutes", PAGE)
        self.assertNotIn("scheduledAtForIndex", PAGE)

    def test_unconfigured_or_loading_account_cannot_inherit_prior_account_time(self):
        self.assertIn(
            'const DEFAULT_DAILY_PUBLISH_TIME = "11:00";',
            PAGE,
        )
        renderer = source_between(
            "function renderSchedule({ hydrateDraft = true } = {})",
            "async function loadSchedule({ preserveDraft = false, allowWhileSaving = false } = {})",
        )
        self.assertIn(
            'byId("dailyPublishTime").value = DEFAULT_DAILY_PUBLISH_TIME;',
            renderer,
        )
        self.assertIn(
            'byId("dailyPublishTime").value = publishTime || DEFAULT_DAILY_PUBLISH_TIME;',
            renderer,
        )

    def test_disabling_schedule_is_actionable_without_new_publish_consent(self):
        validation = source_between(
            "function scheduleSaveError()",
            "function runNowDisabledReason()",
        )
        disable_guard = 'if (!byId("scheduleEnabled").checked) return "";'
        self.assertIn(disable_guard, validation)
        self.assertLess(
            validation.index(disable_guard),
            validation.index("if (!accountEligible(item))"),
        )
        for publish_only_check in (
            "selectedAccountSettings()",
            "state.creatorInfo",
            "manualCanaryReady()",
            "dailyPublishTime",
            "publishConsent",
        ):
            self.assertGreater(
                validation.index(publish_only_check),
                validation.index(disable_guard),
            )

        save = source_between(
            "async function saveSchedule()",
            "function validPendingRunNowAccountId(value)",
        )
        base_payload = source_between_text(
            save,
            "const schedulePayload = {",
            "if (requestedEnabled) {",
        )
        for forbidden in ("publish_time", "timezone", "consent"):
            self.assertNotIn(forbidden, base_payload)
        enabled_payload = source_between_text(
            save,
            "if (requestedEnabled) {",
            'setText(button, "正在保存…")',
        )
        self.assertIn("schedulePayload.publish_time", enabled_payload)
        self.assertIn("schedulePayload.timezone", enabled_payload)
        self.assertIn("schedulePayload.consent", enabled_payload)
        self.assertIn("body: JSON.stringify(schedulePayload)", save)
        self.assertIn('setText(byId("scheduleState"), error)', save)

        save_button = re.search(
            r'<button id="saveSchedule"(?P<attrs>[^>]*)>',
            PAGE,
        )
        self.assertIsNotNone(save_button)
        self.assertNotIn("disabled", save_button.group("attrs"))
        actions = source_between(
            "function updateScheduleActions()",
            "function renderSchedule(",
        )
        self.assertIn("const scheduleControlsLocked = state.scheduleBusy", actions)
        self.assertIn('byId("dailyPublishTime").disabled = scheduleControlsLocked', actions)
        self.assertIn('byId("scheduleEnabled").disabled = scheduleControlsLocked', actions)
        self.assertIn('byId("refreshAccounts").disabled = state.scheduleBusy', actions)
        self.assertIn('.querySelectorAll("input[data-account-id]")', actions)
        self.assertIn("radio.disabled = state.scheduleBusy", actions)
        self.assertIn("saveButton.disabled = state.scheduleBusy", actions)
        self.assertIn('saveButton.title = saveError', actions)
        self.assertIn('"关闭自动发布"', actions)

    def test_schedule_draft_survives_gate_and_background_refreshes(self):
        gates = source_between(
            "function renderGates()",
            "function accountEligible(item)",
        )
        self.assertNotIn("renderSchedule(", gates)
        self.assertIn("scheduleDraftDirty: false", PAGE)

        load = source_between(
            "async function loadSchedule({ preserveDraft = false, allowWhileSaving = false } = {})",
            "async function saveSchedule()",
        )
        self.assertIn("if (!preserveDraft)", load)
        self.assertIn(
            "renderSchedule({ hydrateDraft: !preserveDraft || !state.scheduleDraftDirty })",
            load,
        )
        self.assertIn("state.scheduleDraftDirty = false", load)

        change = source_between(
            '["dailyPublishTime", "scheduleEnabled"].forEach',
            'byId("publishConsent").addEventListener',
        )
        self.assertIn("state.scheduleDraftDirty = true", change)
        self.assertIn("renderSchedule({ hydrateDraft: false })", change)
        self.assertNotIn("resetBatchConfirmation()", change)
        refresh = source_between(
            'byId("refreshGates").addEventListener',
            'byId("previewMaterial").addEventListener',
        )
        self.assertIn("loadSchedule({ preserveDraft: true })", refresh)

    def test_schedule_save_rejects_stale_gets_and_rehydrates_version_conflicts(self):
        load = source_between(
            "async function loadSchedule({ preserveDraft = false, allowWhileSaving = false } = {})",
            "async function saveSchedule()",
        )
        self.assertIn(
            "if (state.scheduleBusy && !allowWhileSaving) return false;",
            load,
        )
        self.assertEqual(load.count("(state.scheduleBusy && !allowWhileSaving)"), 3)
        self.assertIn("return true;", load)
        self.assertGreaterEqual(load.count("return false;"), 4)
        self.assertLess(
            load.index("delete state.preparationScheduleRefreshPendingByAccount"),
            load.index("return true;"),
        )

        save = source_between(
            "async function saveSchedule()",
            "function validPendingRunNowAccountId(value)",
        )
        bump = "state.scheduleRequestVersion += 1"
        self.assertEqual(save.count(bump), 2)
        bumps = [save.index(bump), save.index(bump, save.index(bump) + 1)]
        api_call = save.index("const result = await api(`${API_BASE}/schedule`")
        apply_result = save.index("applyGates(result)", api_call)
        self.assertLess(bumps[0], api_call)
        self.assertGreater(bumps[1], api_call)
        self.assertLess(bumps[1], apply_result)

        conflict = source_between_text(
            save,
            'if (error.code === "tt_post_schedule_version_conflict")',
            "} else {",
        )
        self.assertIn("requestedAccountId !== state.selectedAccountId", conflict)
        self.assertIn("if (!accountChanged) state.scheduleDraftDirty = false", conflict)
        self.assertIn("preserveDraft: accountChanged", conflict)
        self.assertIn("allowWhileSaving: true", conflict)
        self.assertIn("已加载最新设置，请重新修改后再保存", conflict)
        non_conflict = source_between_text(save, "} else {", "} finally {")
        self.assertIn("requestedAccountId !== state.selectedAccountId", non_conflict)
        self.assertIn(
            "await loadSchedule({ preserveDraft: false, allowWhileSaving: true })",
            non_conflict,
        )
        self.assertIn(": false;", non_conflict)
        self.assertIn("每日排期保存失败", non_conflict)

    def test_ineligible_account_remains_selectable_for_schedule_stop(self):
        renderer = source_between(
            "function renderAccounts()",
            "function resetCreatorInfo(",
        )
        self.assertIn('label.className = `account-option${eligible ? "" : " ineligible"}`', renderer)
        self.assertIn('setText(status, eligible ? "可确认" : "不可发布，可管理")', renderer)
        self.assertIn("radio.disabled = state.scheduleBusy", renderer)
        self.assertNotIn("radio.disabled = !eligible", renderer)
        self.assertNotIn("account-option.disabled", PAGE)

    def test_account_source_degradation_shows_backend_warning_not_success_copy(self):
        loader = source_between(
            "async function loadAccounts()",
            "function resetBatchConfirmation()",
        )
        degraded = source_between_text(
            loader,
            "if (boolValue(result.account_source_available) === false)",
            "} else {",
        )
        self.assertIn("result.warning", degraded)
        self.assertIn('className = "helper warning"', degraded)
        self.assertNotIn("已加载 ${state.accounts.length} 个账号", degraded)
        self.assertIn("已加载 ${state.accounts.length} 个账号", loader)
        self.assertIn(".helper.warning { color: var(--amber); }", PAGE)

    def test_preparation_change_refreshes_selected_schedule_and_run_now_reason(self):
        preparation = source_between(
            "async function loadPreparationStatus(",
            "function statusMeta(item)",
        )
        self.assertIn("previousFingerprint = preparationStatusFingerprint", preparation)
        self.assertIn("currentFingerprint = preparationStatusFingerprint", preparation)
        self.assertIn("currentFingerprint !== previousFingerprint", preparation)
        self.assertIn("renderSchedule({ hydrateDraft: false })", preparation)
        self.assertIn("await loadSchedule({ preserveDraft: true })", preparation)
        self.assertIn(
            "state.preparationScheduleRefreshPendingByAccount[requestedAccountId] = true",
            preparation,
        )
        self.assertIn("if (scheduleRefreshed)", preparation)
        self.assertIn(
            "delete state.preparationScheduleRefreshPendingByAccount[requestedAccountId]",
            preparation,
        )

        reason = source_between(
            "function runNowDisabledReason()",
            "function updateScheduleActions()",
        )
        self.assertIn("selectedPreparationState()", reason)
        self.assertIn("正在制作，完成后会自动开放立即发布", reason)
        self.assertIn("没有可立即发布素材", reason)
        self.assertIn('id="preparingMaterialCount"', PAGE)
        self.assertIn('setText(byId("preparingMaterialCount")', PAGE)

    def test_material_pool_creation_is_sequential_and_keeps_partial_failure_state(self):
        creation = source_between(
            "async function addMaterialsToPool()",
            "function statusMeta(",
        )
        self.assertIn(
            "const payloads = materials.map(material => materialPoolPayload(material))",
            creation,
        )
        self.assertIn(
            "for (let index = 0; index < materials.length; index += 1)",
            creation,
        )
        self.assertIn("await api(`${API_BASE}/material-pool`", creation)
        self.assertIn("created.push(", creation)
        self.assertIn("poolFailures.push(", creation)
        self.assertNotIn("Promise.all(", creation)
        self.assertIn(
            "if (!poolFailures.length && !previewFailureCount)",
            creation,
        )
        self.assertIn("resetMaterialPoolForm()", creation)
        self.assertIn("state.queueSummary =", creation)
        self.assertIn("页面已保留", creation)
        self.assertIn("已成功素材不会重复入池", creation)
        self.assertIn("已入池，后台预制作", creation)
        self.assertIn("void loadPreparationStatus({ quiet: true })", creation)

    def test_preparation_status_panel_polls_active_or_pending_refresh_without_form_lock(self):
        preparation = source_between(
            "function preparationStatusMeta(item)",
            "function statusMeta(item)",
        )
        self.assertIn(
            'new Set(["queued", "preparing", "retry_wait"])',
            PAGE,
        )
        self.assertIn("PREPARATION_POLL_INTERVAL_MS = 10000", PAGE)
        for status in ("queued", "preparing", "retry_wait", "ready", "failed"):
            self.assertIn(f"{status}:", preparation)
        self.assertIn("preparation_status", preparation)
        self.assertIn(
            "PREPARATION_ACTIVE_STATUSES.has(preparationStatusMeta(item).status)",
            preparation,
        )
        self.assertIn("preparationHasActive: false", PAGE)
        self.assertIn(
            "if (!preparationPollingNeeded() || document.hidden) return;",
            preparation,
        )
        self.assertIn(
            "return state.preparationHasActive || selectedPreparationScheduleRefreshPending()",
            preparation,
        )
        self.assertIn(
            "status => Number(summary[status] || 0) > 0",
            preparation,
        )
        self.assertIn("window.setTimeout(", preparation)
        self.assertIn(
            "void loadPreparationStatus({ quiet: true })",
            preparation,
        )
        self.assertIn(
            "await api(`${API_BASE}/material-pool?${params.toString()}`)",
            preparation,
        )
        load = source_between(
            "async function loadPreparationStatus(",
            "function statusMeta(item)",
        )
        self.assertIn("state.preparationLoading = true", load)
        self.assertIn("state.preparationLoading = false", load)
        self.assertNotIn("state.busy =", load)
        self.assertNotIn("createFormFields", load)
        self.assertNotIn('data-action="retry', PAGE)
        self.assertIn('meta.status === "failed" ? "待处理" : ""', preparation)
        self.assertIn("document.addEventListener(\"visibilitychange\"", PAGE)

    def test_preparation_refresh_is_account_scoped_and_retries_failed_schedule_get(self):
        fingerprint = source_between(
            "function preparationStatusFingerprint(",
            "function selectedPreparationState()",
        )
        for summary_count in ("active", "ready", "available", "total"):
            self.assertIn(f"`{summary_count}:$", fingerprint)
        self.assertIn("preparationSummary: {}", PAGE)
        self.assertIn("preparationReloadPending: false", PAGE)

        preparation = source_between(
            "async function loadPreparationStatus(",
            "function statusMeta(item)",
        )
        self.assertIn('params.set("source_account_id", requestedAccountId)', preparation)
        self.assertIn("state.preparationSummary = summary", preparation)
        self.assertIn("state.preparationReloadPending = true", preparation)
        self.assertIn(
            "void loadPreparationStatus({ quiet: true, force: true })",
            preparation,
        )
        refresh_call = preparation.index(
            "const scheduleRefreshed = await loadSchedule({ preserveDraft: true })"
        )
        conditional_clear = preparation.index("if (scheduleRefreshed)", refresh_call)
        pending_clear = preparation.index(
            "delete state.preparationScheduleRefreshPendingByAccount[requestedAccountId]",
            conditional_clear,
        )
        self.assertLess(refresh_call, conditional_clear)
        self.assertLess(conditional_clear, pending_clear)

        account_change = source_between(
            'byId("accountList").addEventListener("change"',
            'byId("refreshAccounts").addEventListener',
        )
        self.assertIn(
            "void loadPreparationStatus({ quiet: true, force: true })",
            account_change,
        )

    def test_queue_results_keep_every_material_visible_with_its_own_outcome(self):
        creation = source_between(
            "async function addMaterialsToPool()",
            "function statusMeta(",
        )
        renderer = source_between(
            "function renderQueueSubmitResults()",
            "function resetMaterials(",
        )
        self.assertIn("queueResults: []", PAGE)
        self.assertIn(
            "state.queueResults = payloads.map(payload => ({",
            creation,
        )
        self.assertIn('status: "pending"', creation)
        self.assertIn('poolResult.status = "saving"', creation)
        self.assertIn('poolResult.status = "success"', creation)
        self.assertIn('poolResult.status = "failure"', creation)
        self.assertGreaterEqual(
            creation.count("renderQueueSubmitResults()"),
            3,
        )
        self.assertIn("state.queueResults.forEach(item => {", renderer)
        self.assertIn("state.queueResults.length", renderer)
        self.assertIn("item.material_id", renderer)
        self.assertIn("item.message", renderer)
        self.assertNotIn(".slice(", renderer)
        reset = source_between(
            "function resetMaterialPoolForm()",
            "async function addMaterialsToPool()",
        )
        self.assertNotIn("state.queueResults =", reset)
        self.assertNotIn('hide(byId("queueSubmitPanel"))', reset)

    def test_material_changes_reset_batch_confirmation_but_schedule_draft_does_not(self):
        self.assertIn("function resetBatchConfirmation()", PAGE)
        self.assertIn('state.formKey = "";', PAGE)
        self.assertIn('state.consentAcceptedAt = "";', PAGE)
        self.assertIn('byId("publishConsent").checked = false;', PAGE)
        self.assertIn(
            'byId("materialIds").addEventListener("input"',
            PAGE,
        )
        self.assertIn(
            'byId("caption").addEventListener("input"',
            PAGE,
        )
        self.assertIn(
            '["dailyPublishTime", "scheduleEnabled"].forEach',
            PAGE,
        )
        schedule_change = source_between(
            '["dailyPublishTime", "scheduleEnabled"].forEach',
            'byId("publishConsent").addEventListener',
        )
        self.assertIn("state.scheduleDraftDirty = true", schedule_change)
        self.assertIn('state.consentAcceptedAt = "";', schedule_change)
        self.assertNotIn("resetBatchConfirmation()", schedule_change)
        self.assertNotIn('byId("publishConsent").checked = false', schedule_change)

    def test_partial_retry_reuses_frozen_batch_consent_timestamp(self):
        self.assertIn('consentAcceptedAt: ""', PAGE)
        self.assertIn("function ensureConsentAcceptedAt()", PAGE)
        payload = source_between(
            "function materialPoolPayload(material)",
            "function resetMaterialPoolForm()",
        )
        self.assertIn("accepted_at: ensureConsentAcceptedAt()", payload)
        self.assertNotIn("accepted_at: new Date().toISOString()", payload)

    def test_creator_info_ignores_stale_account_switch_responses(self):
        creator = source_between(
            "async function loadCreatorInfo()",
            "async function loadAccounts()",
        )
        self.assertIn("creatorRequestVersion: 0", PAGE)
        self.assertIn(
            "const requestVersion = ++state.creatorRequestVersion",
            creator,
        )
        self.assertIn("const requestedAccountId = accountId(item)", creator)
        self.assertIn("source_account_id: requestedAccountId", creator)
        stale_guard = (
            "requestVersion !== state.creatorRequestVersion ||\n"
            "            state.selectedAccountId !== requestedAccountId"
        )
        self.assertGreaterEqual(creator.count(stale_guard), 2)

    def test_account_settings_are_read_only_and_required_in_pool(self):
        for removed_id in (
            "privacyLevel",
            "allowComment",
            "allowDuet",
            "allowStitch",
            "commercialDisclosure",
            "ownBrand",
            "brandedContent",
            "isAigc",
        ):
            self.assertNotIn(f'id="{removed_id}"', PAGE)

        self.assertIn("function selectedAccountSettings()", PAGE)
        self.assertIn(
            'if (!selectedAccountSettings()) return "所选账号尚未配置',
            PAGE,
        )
        self.assertIn("renderAccountSettings", PAGE)
        self.assertIn("settings.configured === true", PAGE)
        self.assertNotIn('privacy_level: byId("privacyLevel").value', PAGE)
        self.assertNotIn('allow_comment: byId("allowComment").checked', PAGE)
        self.assertNotIn(
            'brand_content_toggle: commercial && byId("brandedContent").checked',
            PAGE,
        )
        self.assertIn("max_video_post_duration_sec", PAGE)
        self.assertIn("发布池只读展示、不再单独编辑", PAGE)

    def test_live_compliance_gates_default_to_hold(self):
        for element_id in (
            "liveGateCard",
            "auditGateCard",
            "urlGateCard",
            "modeGateCard",
            "refreshGates",
        ):
            self.assertIn(f'id="{element_id}"', PAGE)
        self.assertIn("liveEnabled: undefined", PAGE)
        self.assertIn("auditApproved: undefined", PAGE)
        self.assertIn("urlVerified: undefined", PAGE)
        self.assertIn(
            "if (!gatesOpen() && !manualCanaryReady())",
            PAGE,
        )
        self.assertIn("状态缺失一律按未开放处理", PAGE)
        self.assertIn("当前片尾含 DramaWave 品牌与跳转引导", PAGE)
        self.assertIn("不会消费待发素材", PAGE)

    def test_manual_publish_is_prominent_confirmed_and_never_replaces_daily_slot(self):
        run = source_between(
            "async function runNow()",
            "async function loadAccounts()",
        )
        self.assertIn('id="runNow" class="button run-now"', PAGE)
        self.assertIn("function runNowDisabledReason()", PAGE)
        self.assertIn("available_material_count", PAGE)
        self.assertIn("can_publish_now", PAGE)
        self.assertIn("window.confirm(", run)
        self.assertIn("额外消费素材池中的下一条", run)
        self.assertIn("不修改每日设置；若与自动时点重叠，仍受账号串行安全规则约束", run)
        self.assertIn("await api(`${API_BASE}/run-now`", run)
        self.assertIn("source_account_id: requestedAccountId", run)
        self.assertIn("idempotency_key: requestKey", run)
        self.assertIn(
            "Promise.all([loadSchedule({ preserveDraft: true }), loadQueue()])",
            run,
        )
        self.assertIn("仅一次私密测试", PAGE)
        self.assertIn("manual_canary_ready", PAGE)
        self.assertIn("强制 SELF_ONLY", PAGE)
        self.assertIn("每日自动排期锁定为关闭", PAGE)

    def test_manual_publish_reuses_pending_key_until_server_success(self):
        key_helper = source_between(
            "function pendingRunNowKeyForAccount(accountIdValue)",
            "async function runNow()",
        )
        run = source_between(
            "async function runNow()",
            "async function loadAccounts()",
        )
        self.assertIn("pendingRunNowByAccount: Object.create(null)", PAGE)
        self.assertIn(
            "const existing = pendingRunNowEntry(normalizedAccountId)",
            key_helper,
        )
        self.assertIn("return existing.key", key_helper)
        self.assertIn(
            "state.pendingRunNowByAccount[normalizedAccountId] = {",
            key_helper,
        )
        self.assertIn(
            "const requestKey = `tt-post:run-now:${normalizedAccountId}:${suffix}`",
            key_helper,
        )
        self.assertIn("persistPendingRunNowState()", key_helper)
        self.assertLess(
            run.index("if (!confirmed) return;"),
            run.index("const requestKey = pendingRunNowKeyForAccount(requestedAccountId)"),
        )
        self.assertIn("const outcome = classifyRunNowResponse(run)", run)
        catch_block = run[run.index("} catch (error) {") :]
        self.assertNotIn("clearPendingRunNowKey(", catch_block)
        self.assertIn("复用同一请求标识安全重试", catch_block)
        self.assertNotIn("不会替代、取消或挪动今天的定时发布", PAGE)

    def test_manual_publish_response_status_controls_operator_message_and_key(self):
        classifier = source_between(
            "function classifyRunNowResponse(run)",
            "async function runNow()",
        )
        run = source_between(
            "async function runNow()",
            "async function loadAccounts()",
        )
        self.assertIn("RUN_NOW_NOT_PUBLISHED_STATUSES", PAGE)
        for status in (
            "preflight_failed",
            "failed",
            "canceled",
            "missed",
            "blocked_compliance",
        ):
            self.assertIn(f'"{status}"', PAGE)
        for status in ("scheduled", "claimed", "publishing", "reconciling"):
            self.assertIn(f'"{status}"', PAGE)
        self.assertIn('status === "unknown"', classifier)
        self.assertIn('status === "published"', classifier)
        self.assertIn(
            'RUN_NOW_SUBMITTED_STATUSES.has(status) || cleanId(item.queue_id)',
            classifier,
        )
        self.assertIn('return { kind: "unconfirmed", status }', classifier)
        self.assertIn('outcome.kind === "not_published"', run)
        self.assertIn("本次未发布", run)
        self.assertIn(
            "clearPendingRunNowKey(requestedAccountId, requestKey)",
            run,
        )
        unknown_block = run[
            run.index('outcome.kind === "unknown"') :
            run.index('outcome.kind === "published"')
        ]
        self.assertIn("需人工核对", unknown_block)
        self.assertIn(
            "markPendingRunNowUnknown(requestedAccountId, requestKey)",
            unknown_block,
        )
        self.assertNotIn("clearPendingRunNowKey(", unknown_block)
        published_block = run[
            run.index('outcome.kind === "published"') :
            run.index('outcome.kind === "submitted"')
        ]
        self.assertIn("立即发布已完成", published_block)
        self.assertIn("clearPendingRunNowKey(", published_block)
        submitted_block = run[
            run.index('outcome.kind === "submitted"') :
            run.index("} else {", run.index('outcome.kind === "submitted"'))
        ]
        self.assertIn("立即发布已提交", submitted_block)
        self.assertIn("clearPendingRunNowKey(", submitted_block)
        self.assertNotIn("runId) {", run)

    def test_pending_manual_keys_are_session_scoped_validated_and_per_account(self):
        storage = source_between(
            "function validPendingRunNowAccountId(value)",
            "function classifyRunNowResponse(run)",
        )
        self.assertIn(
            'const RUN_NOW_PENDING_STORAGE_KEY = "tt-post:pending-run-now:v1"',
            PAGE,
        )
        self.assertIn("RUN_NOW_PENDING_STORAGE_MAX_BYTES = 32768", PAGE)
        self.assertIn("RUN_NOW_PENDING_MAX_ACCOUNTS = 100", PAGE)
        self.assertIn(r"/^[1-9]\d{0,29}$/", storage)
        self.assertIn("key.length > 255", storage)
        self.assertIn("key.startsWith(prefix)", storage)
        self.assertIn(r"/^[A-Za-z0-9:-]{8,180}$/", storage)
        self.assertIn('value.status === "" || value.status === "unknown"', storage)
        self.assertIn("window.sessionStorage.getItem(RUN_NOW_PENDING_STORAGE_KEY)", storage)
        self.assertIn("window.sessionStorage.setItem(", storage)
        self.assertIn("window.sessionStorage.removeItem(RUN_NOW_PENDING_STORAGE_KEY)", storage)
        self.assertIn("JSON.parse(raw)", storage)
        self.assertIn("Object.create(null)", storage)
        self.assertIn("state.pendingRunNowByAccount[normalizedAccountId]", storage)
        self.assertIn("delete state.pendingRunNowByAccount[normalizedAccountId]", storage)
        self.assertIn("loadPendingRunNowState();", PAGE)

    def test_queue_and_event_monitor_are_present(self):
        for element_id in (
            "queueFilters",
            "filterMaterialId",
            "filterAccountId",
            "filterStatus",
            "queueRows",
            "pageInfo",
            "eventDialog",
            "eventList",
        ):
            self.assertIn(f'id="{element_id}"', PAGE)
        self.assertIn("PROCESSING_STATUSES", PAGE)
        self.assertIn("结果待确认", PAGE)
        self.assertIn("不会自动创建第二条发布请求", PAGE)
        self.assertIn("queue_id: String(queueId)", PAGE)

    def test_queue_cancel_and_manual_reconcile_are_status_scoped(self):
        self.assertIn("function canCancelQueue(item)", PAGE)
        self.assertIn(
            'return ["scheduled", "claimed"].includes(rawQueueStatus(item));',
            PAGE,
        )
        self.assertIn("function canManuallyReconcileQueue(item)", PAGE)
        self.assertIn(
            '["unknown", "needs_review", "reconciling"].includes(rawQueueStatus(item))',
            PAGE,
        )
        self.assertIn('queueActionButton("cancel", queueId, "取消任务", "danger")', PAGE)
        self.assertIn(
            'queueActionButton("reconcile", queueId, "人工核对", "warning")',
            PAGE,
        )
        self.assertIn(
            "`${API_BASE}/queue/${encodeURIComponent(normalizedQueueId)}/${action}`",
            PAGE,
        )
        self.assertIn('method: "POST"', PAGE)
        self.assertIn(
            'isCancel ? { reason: "由AI后台操作人员人工取消" } : {}',
            PAGE,
        )
        self.assertIn("window.confirm(", PAGE)
        self.assertIn("不会创建第二条发布请求", PAGE)

    def test_frontend_does_not_expose_credentials_or_use_html_injection(self):
        lowered = PAGE.lower()
        for forbidden in (
            "access_token",
            "refresh_token",
            "client_secret",
            "innerhtml",
            "outerhtml",
            "insertadjacenthtml",
            "document.write",
            "eval(",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertNotIn("\ufffd", PAGE)
        self.assertIn("document.createElement", PAGE)
        self.assertIn(".textContent", PAGE)
        self.assertIn(".replaceChildren", PAGE)

    def test_inline_javascript_parses(self):
        javascript = inline_javascript(PAGE)
        self.assertTrue(javascript.strip())
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
            "tt-post-pool.html inline JavaScript failed syntax check:\n"
            f"{completed.stdout}\n{completed.stderr}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
