#!/usr/bin/env python3
"""Static safety checks for the visible status column and one-off catch-up unit."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCOUNT_LIST = (ROOT / "static" / "x-account-list.html").read_text(
    encoding="utf-8"
)
NAVIGATION = json.loads(
    (ROOT / "static" / "navigation.json").read_text(encoding="utf-8")
)
NGINX = (ROOT / "deploy" / "nginx-x-oauth.conf").read_text(encoding="utf-8")
CATCHUP_SERVICE = (ROOT / "deploy" / "x-post-catchup.service").read_text(
    encoding="utf-8"
)
CATCHUP_ENV_EXAMPLE = (
    ROOT / "deploy" / "x-post-catchup.env.example"
).read_text(encoding="utf-8")


class XPostCatchupDeployTest(unittest.TestCase):
    def test_auto_publish_status_is_the_second_column(self):
        header = (
            "<th>X账号</th>"
            '<th class="auto-publish-col">自动发布 Post</th>'
        )
        self.assertIn(header, ACCOUNT_LIST)
        account_cell = '<td><div class="account">'
        status_cell = '<td class="auto-publish-col"><span class="status '
        self.assertLess(ACCOUNT_LIST.index(account_cell), ACCOUNT_LIST.index(status_cell))
        self.assertLess(
            ACCOUNT_LIST.index(status_cell),
            ACCOUNT_LIST.index("<td>${metricsHtml(item)}</td>"),
        )
        self.assertIn(
            ".auto-publish-col { min-width:120px; white-space:nowrap; }",
            ACCOUNT_LIST,
        )

    def test_navigation_uses_versioned_account_list_url(self):
        account_item = next(
            item
            for group in NAVIGATION
            for item in group.get("items", [])
            if item.get("key") == "xAccountList"
        )
        self.assertEqual(
            account_item["href"],
            "/x-account-list.html?v=20260727catchup1",
        )

    def test_account_list_html_is_not_cacheable(self):
        location = NGINX.split(
            "location = /x-account-list.html {",
            1,
        )[1].split("}", 1)[0]
        self.assertIn(
            'add_header Cache-Control "no-cache, no-store, must-revalidate" always;',
            location,
        )
        self.assertIn("add_header Pragma \"no-cache\" always;", location)
        self.assertIn("expires -1;", location)

    def test_catchup_unit_is_manual_and_reuses_daily_safety_boundary(self):
        self.assertNotIn("[Timer]", CATCHUP_SERVICE)
        self.assertIn("User=x-post-daily", CATCHUP_SERVICE)
        self.assertIn("EnvironmentFile=/etc/x-post-daily.env", CATCHUP_SERVICE)
        self.assertIn(
            "EnvironmentFile=/etc/x-post-media-repair.token",
            CATCHUP_SERVICE,
        )
        self.assertIn(
            "EnvironmentFile=/etc/x-post-catchup.env",
            CATCHUP_SERVICE,
        )
        self.assertIn(
            "scripts/x_post_catchup_runner.py "
            "--run-date ${X_POST_CATCHUP_RUN_DATE} "
            "--expected-missing-count ${X_POST_CATCHUP_EXPECTED_MISSING_COUNT} "
            "--reason ${X_POST_CATCHUP_REASON}",
            CATCHUP_SERVICE,
        )
        self.assertIn(
            "ReadWritePaths=/run/x-post-daily "
            "/mnt/data-disk/x-post-automation/daily-work",
            CATCHUP_SERVICE,
        )

    def test_catchup_example_is_pinned_to_the_approved_scope(self):
        self.assertIn("X_POST_CATCHUP_RUN_DATE=2026-07-27", CATCHUP_ENV_EXAMPLE)
        self.assertIn(
            "X_POST_CATCHUP_EXPECTED_MISSING_COUNT=6",
            CATCHUP_ENV_EXAMPLE,
        )
        self.assertIn(
            "X_POST_CATCHUP_REASON=scope_expansion_v1",
            CATCHUP_ENV_EXAMPLE,
        )


if __name__ == "__main__":
    unittest.main()
