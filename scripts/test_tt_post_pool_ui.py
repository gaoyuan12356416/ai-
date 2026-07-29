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
            "materialId",
            "previewMaterial",
            "mediaPreview",
            "videoShell",
            "caption",
            "scheduledAt",
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
        self.assertIn("material_id: state.material.material_id", PAGE)
        self.assertIn("content_id: state.material.content_id", PAGE)
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

    def test_caption_template_keeps_exact_drama_id(self):
        expected = (
            "Watch the full story in the app 🎬\\n\\n"
            "Drama ID: {{contect_id}}\\n\\n"
            "Visit my profile → Open the link → Search the Drama ID → Watch now."
        )
        self.assertIn(f'const CAPTION_TEMPLATE = "{expected}";', PAGE)
        self.assertIn(
            'CAPTION_TEMPLATE.replace("{{contect_id}}", contentId)',
            PAGE,
        )
        self.assertIn("function fixedCaption(contentId)", PAGE)
        caption = re.search(r'<textarea id="caption"(?P<attrs>[^>]*)>', PAGE)
        self.assertIsNotNone(caption)
        self.assertIn('maxlength="2200"', caption.group("attrs"))
        self.assertIn("readonly", caption.group("attrs"))
        self.assertNotIn("disabled", caption.group("attrs"))
        self.assertNotIn('byId("caption").addEventListener("input"', PAGE)
        self.assertIn("仅 Drama ID 会按当前素材动态替换，不可编辑", PAGE)
        self.assertIn("utf16Units", PAGE)

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
