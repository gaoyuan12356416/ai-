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


class TtPostPoolUiTest(unittest.TestCase):
    def test_navigation_registers_tt_pool_with_standalone_permission(self):
        group = next(item for item in NAVIGATION if item.get("key") == "tiktok_platform")
        self.assertEqual(group["label"], "TikTok 社媒")
        self.assertEqual(group["order"], 35)
        self.assertEqual(group["module"], "tt_posts")
        item = next(entry for entry in group["items"] if entry.get("key") == "ttPostPool")
        self.assertEqual(item["label"], "TT Post发布池")
        self.assertEqual(item["href"], "/tt-post-pool.html")
        self.assertEqual(item["module"], "tt_posts")
        self.assertEqual(item["order"], 10)
        self.assertFalse(item["adminOnly"])
        self.assertTrue(item["enabled"])

        self.assertIn('ttPostPool: "/tt-post-pool.html"', QUICK_NAV)
        self.assertIn('key: "tiktok_platform"', QUICK_NAV)
        self.assertIn('key: "ttPostPool"', QUICK_NAV)
        self.assertIn('module: "tt_posts"', QUICK_NAV)
        self.assertIn("if (!tiktokPlatform)", QUICK_NAV)
        self.assertIn("if (!ttPostPoolExists)", QUICK_NAV)
        self.assertIn('quickNavConfigCache:v4', QUICK_NAV)

    def test_page_uses_existing_shell_and_tt_permission(self):
        self.assertIn('href="/ui-topbar.css"', PAGE)
        self.assertIn('src="/ui-topbar.js"', PAGE)
        self.assertIn('src="/quick-nav.js"', PAGE)
        self.assertIn('api("/api/ui/topbar")', PAGE)
        self.assertIn('activeKey: "ttPostPool"', PAGE)
        self.assertIn("user.permissions.tt_posts", PAGE)
        self.assertIn('id="loginGate"', PAGE)
        self.assertIn('id="permissionGate"', PAGE)

    def test_create_form_covers_account_material_schedule_and_consent(self):
        required_ids = {
            "accountSearch",
            "refreshAccounts",
            "accountList",
            "creatorCard",
            "materialIds",
            "previewMaterial",
            "materialProgress",
            "materialResults",
            "mediaPreview",
            "videoShell",
            "caption",
            "scheduledAt",
            "scheduleIntervalMinutes",
            "privacyLevel",
            "allowComment",
            "allowDuet",
            "allowStitch",
            "commercialDisclosure",
            "ownBrand",
            "brandedContent",
            "isAigc",
            "publishConsent",
            "createQueue",
        }
        for element_id in required_ids:
            self.assertIn(f'id="{element_id}"', PAGE)

        self.assertIn('type="search"', PAGE)
        self.assertIn('type="datetime-local"', PAGE)
        self.assertIn('timezone: "Asia/Shanghai"', PAGE)
        self.assertIn("shanghaiInputToUtc", PAGE)
        self.assertIn("source_account_id: state.selectedAccountId", PAGE)
        self.assertNotIn("Number(state.selectedAccountId)", PAGE)
        self.assertIn("material_id: material.material_id", PAGE)
        self.assertIn("content_id: material.content_id", PAGE)
        self.assertIn("materials: []", PAGE)
        self.assertIn("failures: []", PAGE)
        self.assertIn("CONSENT_VERSION", PAGE)
        self.assertIn("Music Usage Confirmation", PAGE)

    def test_page_only_calls_same_origin_admin_contracts(self):
        for endpoint in (
            "${API_BASE}/accounts",
            "${API_BASE}/creator-info",
            "${API_BASE}/materials/preview",
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
        self.assertIn("function parseMaterialIds()", PAGE)
        self.assertIn(r"raw.split(/[\s,，;；]+/)", PAGE)
        self.assertIn(r"!/^\d+$/.test(value)", PAGE)
        self.assertIn("String(BigInt(value))", PAGE)
        self.assertIn("Array.from(new Set(", PAGE)
        self.assertIn("if (ids.length > 100)", PAGE)

        preview = source_between(
            "async function previewMaterials()",
            "function renderCaptionTemplate(",
        )
        self.assertIn(
            "for (let index = 0; index < materialIds.length; index += 1)",
            preview,
        )
        self.assertIn("await api(`${API_BASE}/materials/preview`", preview)
        self.assertIn("state.materials.push(", preview)
        self.assertIn("state.failures.push(", preview)
        self.assertIn("renderMaterialResults()", preview)
        self.assertIn("成功 ${state.materials.length} 个", preview)
        self.assertIn("失败 ${state.failures.length} 个", preview)
        self.assertNotIn("Promise.all(", preview)
        self.assertIn(
            'byId("materialResults").addEventListener("click"',
            PAGE,
        )
        self.assertIn("button[data-material-index]", PAGE)
        self.assertIn("renderMaterial(state.materials[index])", PAGE)

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
        self.assertIn('remainder.includes("{{") || remainder.includes("}}")', PAGE)
        self.assertIn(
            "renderCaptionTemplate(template, material.content_id)",
            PAGE,
        )
        self.assertIn("if (units > 2200)", PAGE)
        self.assertIn("caption_template: byId(\"caption\").value", PAGE)
        self.assertIn("utf16Units", PAGE)

    def test_batch_schedule_uses_first_time_interval_and_stable_material_keys(self):
        interval = re.search(
            r'<input id="scheduleIntervalMinutes"(?P<attrs>[^>]*)>',
            PAGE,
        )
        self.assertIsNotNone(interval)
        for attribute in (
            'type="number"',
            'min="1"',
            'max="1440"',
            'step="1"',
            'value="10"',
        ):
            self.assertIn(attribute, interval.group("attrs"))
        self.assertIn("首条上海发布时间", PAGE)
        self.assertIn("function scheduledAtForIndex(index)", PAGE)
        self.assertIn("index * interval * 60 * 1000", PAGE)

        payload = source_between(
            "function queuePayload(material, index)",
            "function resetCreateForm()",
        )
        self.assertIn(
            'idempotency_key: `${ensureFormKey()}:${material.material_id}`',
            payload,
        )
        self.assertIn("scheduled_at: scheduledAtForIndex(index)", payload)
        self.assertIn('caption_template: byId("caption").value', payload)
        self.assertNotIn("caption_text:", payload)

    def test_queue_creation_is_sequential_and_keeps_partial_failure_state(self):
        creation = source_between(
            "async function createQueue()",
            "function statusMeta(",
        )
        self.assertIn(
            "const payloads = materials.map((material, index) => queuePayload(material, index))",
            creation,
        )
        self.assertIn(
            "for (let index = 0; index < materials.length; index += 1)",
            creation,
        )
        self.assertIn("await api(`${API_BASE}/queue`", creation)
        self.assertIn("created.push(", creation)
        self.assertIn("queueFailures.push(", creation)
        self.assertNotIn("Promise.all(", creation)
        self.assertIn(
            "if (!queueFailures.length && !previewFailureCount)",
            creation,
        )
        self.assertIn("resetCreateForm()", creation)
        self.assertIn("state.queueSummary =", creation)
        self.assertIn("页面已保留", creation)
        self.assertIn("已成功任务不会重复创建", creation)

    def test_task_setting_changes_reset_batch_key_and_consent(self):
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
            'byId("scheduleIntervalMinutes").addEventListener("input"',
            PAGE,
        )
        self.assertIn(
            'if (id !== "publishConsent") resetBatchConfirmation();',
            PAGE,
        )

    def test_partial_retry_reuses_frozen_batch_consent_timestamp(self):
        self.assertIn('consentAcceptedAt: ""', PAGE)
        self.assertIn("function ensureConsentAcceptedAt()", PAGE)
        payload = source_between(
            "function queuePayload(material, index)",
            "function resetCreateForm()",
        )
        self.assertIn("accepted_at: ensureConsentAcceptedAt()", payload)
        self.assertNotIn("accepted_at: new Date().toISOString()", payload)

    def test_creator_settings_fail_closed_and_have_no_platform_defaults(self):
        privacy = re.search(
            r'<select id="privacyLevel" disabled>(?P<body>.*?)</select>',
            PAGE,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(privacy)
        self.assertIn('value=""', privacy.group("body"))
        self.assertNotIn(" selected", privacy.group("body"))

        for element_id in ("allowComment", "allowDuet", "allowStitch"):
            checkbox = re.search(
                rf'<input id="{element_id}"(?P<attrs>[^>]*)>',
                PAGE,
            )
            self.assertIsNotNone(checkbox)
            self.assertIn("disabled", checkbox.group("attrs"))
            self.assertNotIn("checked", checkbox.group("attrs"))

        self.assertIn("privacy_level_options", PAGE)
        self.assertIn("max_video_post_duration_sec", PAGE)
        self.assertIn("comment_disabled", PAGE)
        self.assertIn("duet_disabled", PAGE)
        self.assertIn("stitch_disabled", PAGE)
        self.assertIn("boolValue(info.comment_disabled) !== false", PAGE)
        self.assertIn("boolValue(info.duet_disabled) !== false", PAGE)
        self.assertIn("boolValue(info.stitch_disabled) !== false", PAGE)
        self.assertIn('if (!byId("privacyLevel").value)', PAGE)

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
        self.assertIn('publish_mode: gatesOpen() ? "direct_post" : "hold"', PAGE)
        self.assertIn("状态缺失一律按未开放处理", PAGE)
        self.assertIn("当前片尾含 DramaWave 品牌与跳转引导", PAGE)

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
