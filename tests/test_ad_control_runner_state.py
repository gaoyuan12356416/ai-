import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "ad_control_rule_runner.py"


def load_pure_runner_functions():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    wanted = {
        "continuation_state",
        "group_event_continuation_key",
        "has_ads_ai_action_log",
    }
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(RUNNER), "exec"), namespace)
    return namespace


class RunnerStateTests(unittest.TestCase):
    @staticmethod
    def run_group_event_case(preview, execute_result, previous_result=None, attempt=1):
        tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
        wanted = {"has_ads_ai_action_log", "run_group_event"}
        body = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in wanted
        ]
        updates = []
        warnings = []
        fake_app = SimpleNamespace(
            ad_control_validate_insight_start_schema=lambda: warnings.append(
                ("schema_probe",)
            ) or {"campaign_id"},
            create_ad_control_live_preview=lambda payload, session, internal=False: preview,
            execute_ad_control_live=lambda payload, session: dict(execute_result or {}),
            ad_control_update_action_log_runner=lambda *args: updates.append(args),
        )
        namespace = {
            "app": fake_app,
            "MAX_CONTINUATIONS": 3,
            "continuation_state": lambda previous, action, key: (
                dict(previous_result or {}), attempt
            ),
            "record_rule_group_verification": lambda *args: "verification-action",
            "record_rule_group_preview_failure": lambda *args: "preview-failure-action",
            "execution_log_service": SimpleNamespace(
                graph_error_details=lambda reason: {"retryable": False}
            ),
            "event_payload": lambda rule, action, key, status, result=None, reason="": {
                "status": status, "reason": reason, "result": result or {},
            },
            "logging": SimpleNamespace(
                exception=lambda *args: None,
                warning=lambda *args: warnings.append(args),
            ),
            "threading": threading,
        }
        exec(compile(ast.Module(body=body, type_ignores=[]), str(RUNNER), "exec"), namespace)
        event = namespace["run_group_event"](
            {"group_id": "g1", "run_mode": "live"},
            "pause",
            "tick-1",
            previous_event={"status": "partial"},
        )
        return event, updates, warnings

    def test_ads_ai_log_guard_is_exact(self):
        has_log = load_pure_runner_functions()["has_ads_ai_action_log"]
        self.assertTrue(has_log({"log_store": "ads_ai"}))
        self.assertFalse(has_log({"log_store": "sqlite_fallback"}))
        self.assertFalse(has_log({"log_store": ""}))
        self.assertFalse(has_log(None))

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
            source.index("if pause_count + copy_count == 0 and preview_error_count == 0:"),
            source.index("if continuation_attempt > MAX_CONTINUATIONS:"),
        )
        self.assertIn('"blocked" if status == "error" else status', source)

    def test_no_account_due_skips_without_verification_or_execution(self):
        preview = {
            "preview_id": "preview-no-due",
            "preview_hash": "hash-no-due",
            "pause_count": 0,
            "copy_count": 0,
            "execution_count": 0,
            "error_count": 0,
            "scheduled_due_count": 0,
        }
        event, updates, warnings = self.run_group_event_case(
            preview,
            execute_result={"action_id": "must-not-execute"},
        )
        self.assertEqual("skipped", event["status"])
        self.assertEqual("no_accounts_due", event["reason"])
        self.assertNotIn("action_id", event["result"])
        self.assertFalse(event["result"]["verification_only"])
        self.assertEqual([], updates)
        self.assertEqual([], warnings)

    def test_insight_schema_validation_is_lazy_and_cached_per_tick(self):
        tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef) and item.name == "run_group_event"
        )
        schema_calls = []
        fake_app = SimpleNamespace()

        def validate_schema():
            schema_calls.append("called")
            return {"campaign_id", "dt"}

        def preview(payload, session, internal=False):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(
                    lambda _: fake_app.ad_control_validate_insight_start_schema(),
                    range(8),
                ))
            self.assertEqual([{"campaign_id", "dt"}] * 8, results)
            return {
                "preview_id": "preview-schema-cache",
                "pause_count": 0,
                "copy_count": 0,
                "execution_count": 0,
                "error_count": 0,
                "scheduled_due_count": 0,
            }

        fake_app.ad_control_validate_insight_start_schema = validate_schema
        fake_app.create_ad_control_live_preview = preview
        namespace = {
            "app": fake_app,
            "threading": threading,
            "continuation_state": lambda previous, action, key: ({}, 1),
            "event_payload": lambda rule, action, key, status, result=None, reason="": {
                "status": status, "reason": reason, "result": result or {},
            },
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(RUNNER), "exec"), namespace)
        result = namespace["run_group_event"](
            {"group_id": "g1", "run_mode": "live"}, "pause", "tick-1"
        )
        self.assertEqual("no_accounts_due", result["reason"])
        self.assertEqual(["called"], schema_calls)
        self.assertIs(fake_app.ad_control_validate_insight_start_schema, validate_schema)

    def test_insight_schema_failure_is_singleflight_per_tick(self):
        tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef) and item.name == "run_group_event"
        )
        schema_calls = []
        fake_app = SimpleNamespace()

        class FakeStructuredApiError(ValueError):
            def __init__(self, code, message, **details):
                super().__init__(message)
                self.code = code
                self.message = message
                self.details = details

        def validate_schema():
            schema_calls.append("called")
            raise FakeStructuredApiError(
                "insight_start_schema_unavailable",
                "schema unavailable",
                table="insights",
            )

        def preview(payload, session, internal=False):
            def read_schema(_):
                try:
                    fake_app.ad_control_validate_insight_start_schema()
                except FakeStructuredApiError as exc:
                    return exc
                return None

            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(read_schema, range(8)))
            self.assertEqual(["schema unavailable"] * 8, [str(item) for item in results])
            self.assertEqual(8, len({id(item) for item in results}))
            self.assertEqual(
                ["insight_start_schema_unavailable"] * 8,
                [item.code for item in results],
            )
            self.assertEqual([{"table": "insights"}] * 8, [item.details for item in results])
            return {
                "preview_id": "preview-schema-failure",
                "pause_count": 0,
                "copy_count": 0,
                "execution_count": 0,
                "error_count": 0,
                "scheduled_due_count": 0,
            }

        fake_app.ad_control_validate_insight_start_schema = validate_schema
        fake_app.create_ad_control_live_preview = preview
        fake_app.StructuredApiError = FakeStructuredApiError
        namespace = {
            "app": fake_app,
            "threading": threading,
            "continuation_state": lambda previous, action, key: ({}, 1),
            "event_payload": lambda rule, action, key, status, result=None, reason="": {
                "status": status, "reason": reason, "result": result or {},
            },
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(RUNNER), "exec"), namespace)
        result = namespace["run_group_event"](
            {"group_id": "g1", "run_mode": "live"}, "pause", "tick-1"
        )
        self.assertEqual("no_accounts_due", result["reason"])
        self.assertEqual(["called"], schema_calls)
        self.assertIs(fake_app.ad_control_validate_insight_start_schema, validate_schema)

    def test_preview_failure_restores_original_schema_validator(self):
        tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef) and item.name == "run_group_event"
        )

        def validate_schema():
            return {"campaign_id", "dt"}

        def fail_preview(*args, **kwargs):
            raise RuntimeError("preview failed")

        fake_app = SimpleNamespace(
            ad_control_validate_insight_start_schema=validate_schema,
            create_ad_control_live_preview=fail_preview,
        )
        namespace = {
            "app": fake_app,
            "threading": threading,
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(RUNNER), "exec"), namespace)
        with self.assertRaisesRegex(RuntimeError, "preview failed"):
            namespace["run_group_event"](
                {"group_id": "g1", "run_mode": "live"}, "pause", "tick-1"
            )
        self.assertIs(fake_app.ad_control_validate_insight_start_schema, validate_schema)

    def test_preview_failure_keeps_total_error_count_when_details_are_truncated(self):
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn(
            '"preview_error_count": int(preview.get("error_count") or len(errors))',
            source,
        )

    def test_runner_status_update_requires_successful_initial_ads_ai_write(self):
        functions = load_pure_runner_functions()
        has_log = functions["has_ads_ai_action_log"]
        self.assertTrue(has_log({"log_store": "ads_ai"}))
        self.assertFalse(has_log({"log_store": "sqlite_fallback"}))
        self.assertFalse(has_log({}))
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn("if has_ads_ai_action_log(result):", source)
        self.assertIn(
            "if previous_action_id and has_ads_ai_action_log(previous_result):",
            source,
        )

    def test_observe_group_persists_object_level_would_actions(self):
        tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef) and item.name == "run_group_event"
        )
        calls = []
        preview = {
            "preview_id": "preview-1",
            "preview_hash": "hash-1",
            "pause_count": 1,
            "copy_count": 1,
            "execution_count": 2,
            "error_count": 0,
            "errors": [],
        }
        fake_app = SimpleNamespace(
            ad_control_validate_insight_start_schema=lambda: {"campaign_id"},
            create_ad_control_live_preview=lambda payload, session, internal=False: preview,
            execute_ad_control_live=lambda payload, session: calls.append(payload) or {
                "action_id": "action-1",
                "preview_id": "preview-1",
                "requested_count": 2,
                "success_count": 0,
                "skipped_count": 2,
                "error_count": 0,
                "results": [
                    {"object_id": "c1", "status": "observed", "reason": "would_pause"},
                    {"object_id": "c2", "status": "observed", "reason": "would_copy"},
                ],
            },
        )
        namespace = {
            "app": fake_app,
            "continuation_state": lambda previous, action, key: ({}, 1),
            "event_payload": lambda rule, action, key, status, result=None, reason="": {
                "status": status, "reason": reason, "result": result or {},
            },
            "threading": threading,
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(RUNNER), "exec"), namespace)
        result = namespace["run_group_event"](
            {"group_id": "g1", "run_mode": "observe"}, "pause", "tick-1"
        )
        self.assertEqual([{
            "preview_id": "preview-1", "preview_hash": "hash-1", "dry_run": True,
        }], calls)
        self.assertEqual("executed", result["status"])
        self.assertEqual("action-1", result["result"]["action_id"])
        self.assertEqual(1, result["result"]["would_pause_count"])
        self.assertEqual(1, result["result"]["would_copy_count"])
        self.assertTrue(result["result"]["observation_only"])

    def test_continuation_limit_updates_only_initial_ads_ai_log(self):
        preview = {
            "preview_id": "preview-1", "preview_hash": "hash-1",
            "pause_count": 1, "copy_count": 0,
            "execution_count": 1, "error_count": 0,
        }
        fallback_event, fallback_updates, _ = self.run_group_event_case(
            preview,
            execute_result={},
            previous_result={
                "action_id": "action-fallback",
                "log_store": "sqlite_fallback",
                "continuation_attempt": 3,
            },
            attempt=4,
        )
        self.assertEqual("continuation_limit_reached", fallback_event["reason"])
        self.assertEqual([], fallback_updates)

        _, ads_ai_updates, _ = self.run_group_event_case(
            preview,
            execute_result={},
            previous_result={
                "action_id": "action-ads-ai",
                "log_store": "ads_ai",
                "continuation_attempt": 3,
            },
            attempt=4,
        )
        self.assertEqual([
            (
                "action-ads-ai", "tick-1", "blocked",
                "continuation_limit_reached", 1,
            )
        ], ads_ai_updates)

    def test_runner_status_updates_only_initial_ads_ai_log(self):
        preview = {
            "preview_id": "preview-1", "preview_hash": "hash-1",
            "pause_count": 1, "copy_count": 0,
            "execution_count": 1, "error_count": 0,
        }
        fallback_event, fallback_updates, fallback_warnings = self.run_group_event_case(
            preview,
            execute_result={
                "action_id": "action-fallback",
                "log_store": "sqlite_fallback",
                "requested_count": 1,
                "remaining_count": 0,
            },
        )
        self.assertEqual("partial", fallback_event["status"])
        self.assertEqual([], fallback_updates)
        self.assertEqual(1, len(fallback_warnings))

        _, ads_ai_updates, ads_ai_warnings = self.run_group_event_case(
            preview,
            execute_result={
                "action_id": "action-ads-ai",
                "log_store": "ads_ai",
                "requested_count": 1,
                "remaining_count": 0,
            },
        )
        self.assertEqual([
            (
                "action-ads-ai", "tick-1", "partial",
                "live_execute_verify_remaining", 0,
            )
        ], ads_ai_updates)
        self.assertEqual([], ads_ai_warnings)

    def test_deferred_copy_keeps_the_pause_event_in_continuation(self):
        preview = {
            "preview_id": "preview-mixed", "preview_hash": "hash-mixed",
            "pause_count": 21, "copy_count": 1,
            "execution_count": 20, "error_count": 0,
        }
        event, updates, warnings = self.run_group_event_case(
            preview,
            execute_result={
                "action_id": "action-mixed",
                "log_store": "ads_ai",
                "requested_count": 20,
                "remaining_count": 2,
                "retryable_error_count": 0,
                "blocked_count": 0,
                "permanent_error_count": 0,
            },
        )

        self.assertEqual("partial", event["status"])
        self.assertEqual("live_execute_partial", event["reason"])
        self.assertEqual([
            (
                "action-mixed", "tick-1", "partial",
                "live_execute_partial", 2,
            )
        ], updates)
        self.assertEqual([], warnings)
        continuation = load_pure_runner_functions()["group_event_continuation_key"]({
            "last_event": {
                "status": event["status"], "action": "pause",
                "event_key": "tick-1", "result": event["result"],
            }
        }, "pause")
        self.assertEqual("tick-1", continuation)


if __name__ == "__main__":
    unittest.main()
