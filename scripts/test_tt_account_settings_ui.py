#!/usr/bin/env python3
"""Static frontend contracts for TikTok account-level publishing settings."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE_PATH = ROOT / "static" / "tt-account-settings.html"
NAVIGATION_PATH = ROOT / "static" / "navigation.json"

PAGE = PAGE_PATH.read_text(encoding="utf-8")
NAVIGATION = json.loads(NAVIGATION_PATH.read_text(encoding="utf-8"))


def inline_javascript(source: str) -> str:
    scripts = re.findall(
        r"<script(?:\s[^>]*)?>(.*?)</script>",
        source,
        flags=re.DOTALL,
    )
    return "\n".join(script for script in scripts if script.strip())


class TTAccountSettingsUiTest(unittest.TestCase):
    def test_page_is_registered_before_publishing_pool(self):
        group = next(
            item for item in NAVIGATION if item.get("key") == "tiktok_platform"
        )
        settings = next(
            item
            for item in group["items"]
            if item.get("key") == "ttAccountSettings"
        )
        pool = next(
            item for item in group["items"] if item.get("key") == "ttPostPool"
        )
        self.assertEqual(settings["href"], "/tt-account-settings.html")
        self.assertEqual(settings["module"], "tt_posts")
        self.assertEqual(settings["order"], 10)
        self.assertEqual(pool["order"], 20)

    def test_page_uses_existing_shell_and_tt_permission(self):
        self.assertIn('href="/ui-topbar.css"', PAGE)
        self.assertIn('src="/ui-topbar.js"', PAGE)
        self.assertIn('src="/quick-nav.js"', PAGE)
        self.assertIn('api("/api/ui/topbar")', PAGE)
        self.assertIn('activeKey: "ttAccountSettings"', PAGE)
        self.assertIn("user.permissions.tt_posts", PAGE)
        self.assertIn('id="loginGate"', PAGE)
        self.assertIn('id="permissionGate"', PAGE)

    def test_editor_contains_all_account_level_settings(self):
        for element_id in (
            "accountSearch",
            "accountList",
            "settingsForm",
            "privacyLevel",
            "allowComment",
            "allowDuet",
            "allowStitch",
            "commercialDisclosure",
            "commercialOptions",
            "ownBrand",
            "brandedContent",
            "isAigc",
            "versionText",
            "saveSettings",
        ):
            self.assertIn(f'id="{element_id}"', PAGE)
        self.assertIn("互动偏好", PAGE)
        self.assertIn("内容披露", PAGE)
        self.assertIn("不会反向修改 TikTok App 内的隐私设置", PAGE)
        self.assertIn("Music Usage Confirmation 仍需在发布池单独确认", PAGE)
        self.assertIn('href="/tt-post-pool.html"', PAGE)

    def test_only_same_origin_account_settings_contracts_are_called(self):
        self.assertIn(
            'const API_BASE = "/api/admin/tt-posts/account-settings"',
            PAGE,
        )
        self.assertIn("api(API_BASE)", PAGE)
        self.assertIn("api(`${API_BASE}/creator-info`", PAGE)
        self.assertIn("method: \"POST\"", PAGE)
        self.assertNotIn("open.tiktokapis.com", PAGE)

    def test_saved_values_are_limited_by_live_creator_capability(self):
        self.assertIn("privacy_level_options", PAGE)
        self.assertIn("max_video_post_duration_sec", PAGE)
        self.assertIn(
            'boolValue(info.comment_disabled) !== false',
            PAGE,
        )
        self.assertIn('boolValue(info.duet_disabled) !== false', PAGE)
        self.assertIn('boolValue(info.stitch_disabled) !== false', PAGE)
        self.assertIn('input.disabled = !!disabled', PAGE)
        self.assertIn('if (!byId("privacyLevel").value)', PAGE)
        self.assertIn("保存时服务端会再次核对 TikTok 实时能力", PAGE)

    def test_payload_is_explicit_and_optimistically_versioned(self):
        for field in (
            "source_account_id",
            "privacy_level",
            "allow_comment",
            "allow_duet",
            "allow_stitch",
            "commercial_disclosure",
            "brand_organic_toggle",
            "brand_content_toggle",
            "is_aigc",
            "expected_version",
        ):
            self.assertRegex(PAGE, rf"\b{field}\s*:")
        self.assertIn("Number(saved.version || 0)", PAGE)
        self.assertIn("commercial && byId(\"ownBrand\").checked", PAGE)
        self.assertIn("commercial && byId(\"brandedContent\").checked", PAGE)

    def test_commercial_disclosure_requires_a_brand_type(self):
        self.assertIn(
            'byId("commercialDisclosure").checked &&',
            PAGE,
        )
        self.assertIn(
            '!byId("ownBrand").checked &&',
            PAGE,
        )
        self.assertIn(
            '!byId("brandedContent").checked',
            PAGE,
        )
        self.assertIn("applyCommercialVisibility", PAGE)

    def test_frontend_does_not_expose_credentials_or_inject_html(self):
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
            "tt-account-settings.html inline JavaScript failed syntax check:\n"
            f"{completed.stdout}\n{completed.stderr}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
