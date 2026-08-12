from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


class XMembershipDurationUiTests(unittest.TestCase):
    def source(self, name: str) -> str:
        return (STATIC / name).read_text(encoding="utf-8")

    def test_membership_pages_do_not_append_video_duration_to_members(self) -> None:
        for name in (
            "x-accounts.html",
            "x-account-list.html",
            "x-post-material-pool.html",
            "x-auto-publish-template.js",
        ):
            with self.subTest(name=name):
                source = self.source(name)
                self.assertNotIn("支持长视频", source)
                self.assertNotIn("可发长视频", source)
                self.assertNotIn("可发布 · 长视频", source)

        accounts = self.source("x-accounts.html")
        for member in ("X Basic", "X Premium", "X Premium+"):
            self.assertIn(f'{member}"', accounts)
            self.assertNotIn(f"{member} · 最长", accounts)

    def test_accounts_without_membership_keep_the_140_second_hint(self) -> None:
        for name in (
            "x-accounts.html",
            "x-account-list.html",
            "x-post-material-pool.html",
            "x-auto-publish-template.js",
        ):
            with self.subTest(name=name):
                source = self.source(name)
                self.assertIn("最长 140 秒", source)


if __name__ == "__main__":
    unittest.main()
