#!/usr/bin/env python3
"""Offline contract tests for X drama-pool validation messages."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"


def load_validation_message_contract():
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"), filename=str(APP_PATH))
    selected = []
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "X_POST_DRAMA_VALIDATION_MESSAGES"
                for target in node.targets
            )
        ) or (
            isinstance(node, ast.FunctionDef)
            and node.name == "x_post_drama_validation_message"
        ):
            selected.append(node)
    if len(selected) != 2:
        raise AssertionError("validation message contract is incomplete")
    module = ast.Module(body=selected, type_ignores=[])
    namespace = {}
    exec(compile(module, str(APP_PATH), "exec"), namespace)
    return namespace["x_post_drama_validation_message"]


def validation_checks_calls_message_formatter():
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"), filename=str(APP_PATH))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "x_post_drama_validation_checks"
    )
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "x_post_drama_validation_message"
        for node in ast.walk(function)
    )


class XPostDramaValidationMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.render = staticmethod(load_validation_message_contract())

    def test_resource_error_includes_field_level_detail(self):
        self.assertEqual(
            self.render(
                "drama_resource_invalid",
                "sub_number is invalid",
            ),
            "短剧资源数据不完整：sub_number is invalid",
        )

    def test_known_business_error_remains_operator_friendly(self):
        self.assertEqual(
            self.render(
                "drama_episode_gap",
                "free episode numbers must be continuous from 1",
            ),
            "免费剧集集数不连续",
        )

    def test_unknown_error_uses_sanitized_detail(self):
        self.assertEqual(
            self.render("new_validation_code", "new validation detail"),
            "new validation detail",
        )

    def test_validation_checks_uses_the_message_formatter(self):
        self.assertTrue(validation_checks_calls_message_formatter())


if __name__ == "__main__":
    unittest.main(verbosity=2)
