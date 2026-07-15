import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "ad_control_rule_runner.py"


def load_pure_runner_functions():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    wanted = {"continuation_state", "group_event_continuation_key"}
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(RUNNER), "exec"), namespace)
    return namespace


class RunnerStateTests(unittest.TestCase):
    def test_new_event_resets_continuation_attempt(self):
        functions = load_pure_runner_functions()
        previous = {
            "status": "partial",
            "action": "pause",
            "event_key": "pause:account:2026-07-14 23:55",
            "result": {"continuation_attempt": 9},
        }
        result, attempt = functions["continuation_state"](
            previous, "pause", "pause:account:2026-07-15 12:00"
        )
        self.assertEqual({}, result)
        self.assertEqual(1, attempt)

    def test_partial_event_keeps_original_key_across_midnight(self):
        functions = load_pure_runner_functions()
        old_key = "pause:account:2026-07-14 23:55"
        last = {"last_event": {
            "status": "partial",
            "action": "pause",
            "event_key": old_key,
            "result": {"continuation_attempt": 9},
        }}
        self.assertEqual(old_key, functions["group_event_continuation_key"](last, "pause"))
        result, attempt = functions["continuation_state"](last["last_event"], "pause", old_key)
        self.assertEqual(9, result["continuation_attempt"])
        self.assertEqual(10, attempt)

    def test_zero_target_verification_precedes_continuation_limit(self):
        source = RUNNER.read_text(encoding="utf-8")
        self.assertLess(
            source.index("if pause_count == 0 and preview_error_count == 0:"),
            source.index("if continuation_attempt > MAX_CONTINUATIONS:"),
        )
        self.assertIn('"blocked" if status == "error" else status', source)


if __name__ == "__main__":
    unittest.main()
