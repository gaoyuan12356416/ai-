#!/usr/bin/env python3
"""Keep the operator X publishing error catalog aligned with stable codes."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "doc" / "055.x-post-deferred-deliverable" / "error-catalog.md"
SOURCES = (
    ROOT / "features" / "x_posts" / "selector.py",
    ROOT / "features" / "x_posts" / "drama_selector.py",
    ROOT / "features" / "x_posts" / "service.py",
    ROOT / "features" / "x_posts" / "media_repair.py",
    ROOT / "features" / "x_posts" / "publish_media_repair.py",
    ROOT / "features" / "x_posts" / "account_blockers.py",
    ROOT / "features" / "x_accounts" / "oauth_service.py",
    ROOT / "scripts" / "x_post_daily_runner.py",
    ROOT / "scripts" / "x_post_schedule_runner.py",
    ROOT / "scripts" / "x_post_manual_runner.py",
)
ERROR_CALLS = {
    "CandidatePreflightError",
    "DailyRunError",
    "DramaPoolRejection",
    "ManualRunError",
    "MediaRepairError",
    "PoolCandidateRejection",
    "ScheduleRunError",
    "ServiceError",
    "SidecarError",
    "XPostError",
}
MESSAGE_FIRST_CALLS = {
    "CandidatePreflightError",
    "DailyRunError",
    "ManualRunError",
    "ScheduleRunError",
}
ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{2,63}")


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def stable_literal_error_codes():
    codes = set()
    for source in SOURCES:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if name not in ERROR_CALLS:
                continue
            candidates = []
            if name not in MESSAGE_FIRST_CALLS and node.args:
                candidates.append(node.args[0])
            elif len(node.args) > 1:
                candidates.append(node.args[1])
            candidates.extend(
                keyword.value
                for keyword in node.keywords
                if keyword.arg in {"code", "error_code"}
            )
            for candidate in candidates:
                if (
                    isinstance(candidate, ast.Constant)
                    and isinstance(candidate.value, str)
                    and ERROR_CODE.fullmatch(candidate.value)
                ):
                    codes.add(candidate.value)
                    break
    return codes


class XPostErrorCatalogTest(unittest.TestCase):
    def test_catalog_names_every_stable_literal_publish_error(self):
        catalog = CATALOG.read_text(encoding="utf-8")
        codes = stable_literal_error_codes()
        self.assertGreaterEqual(len(codes), 220)
        missing = sorted(code for code in codes if "`%s`" % code not in catalog)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
