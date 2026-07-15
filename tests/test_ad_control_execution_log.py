import json
import inspect
import unittest
import concurrent.futures
import logging
import os
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path

from deploy import apply_ad_control_execution_log_fix as deploy_fix
from deploy.apply_ad_control_execution_log_fix import replace_once
from features.ad_control_copy_engine import service as copy_service
from features.ad_control_execution_log import service


class DeployPatchCompatibilityTests(unittest.TestCase):
    def test_optional_legacy_functions_are_replaced_or_safely_absent(self):
        absent = "def another_function():\n    return True\n"
        for name, replacement in (
            ("ad_control_action_status", deploy_fix.ACTION_STATUS_FUNCTION),
            ("fetch_ad_control_action", deploy_fix.FETCH_ACTION_FUNCTION),
        ):
            with self.subTest(name=name):
                unchanged, changed = deploy_fix.replace_optional_function(
                    absent, name, replacement
                )
                self.assertFalse(changed)
                self.assertEqual(absent, unchanged)

                legacy = (
                    "def %s(item):\n" % name
                    + "    return 'legacy'\n\n\n"
                    + "def another_function():\n"
                    + "    return True\n"
                )
                updated, changed = deploy_fix.replace_optional_function(
                    legacy, name, replacement
                )
                self.assertTrue(changed)
                self.assertIn(replacement, updated)
                reapplied, changed = deploy_fix.replace_optional_function(
                    updated, name, replacement
                )
                self.assertFalse(changed)
                self.assertEqual(updated, reapplied)

    def test_optional_legacy_source_block_is_idempotent(self):
        absent = "current merged target\n"
        unchanged, changed = deploy_fix.replace_optional_once(
            absent, "legacy block", "replacement block", "legacy compatibility"
        )
        self.assertFalse(changed)
        self.assertEqual(absent, unchanged)

        updated, changed = deploy_fix.replace_optional_once(
            "before\nlegacy block\nafter\n",
            "legacy block",
            "replacement block",
            "legacy compatibility",
        )
        self.assertTrue(changed)
        reapplied, changed = deploy_fix.replace_optional_once(
            updated,
            "legacy block",
            "replacement block",
            "legacy compatibility",
        )
        self.assertFalse(changed)
        self.assertEqual(updated, reapplied)

    def test_current_merged_app_patch_is_idempotent(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8").replace("\r\n", "\n")
        updated, _ = deploy_fix.patch_app_text(source)
        self.assertEqual([], deploy_fix.action_log_safety_violations(updated))
        reapplied, changed = deploy_fix.patch_app_text(updated)
        self.assertFalse(changed)
        self.assertEqual(updated, reapplied)

    def test_old_feature_baseline_is_upgraded_to_live_safe_constants(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8").replace("\r\n", "\n")
        safe, _ = deploy_fix.patch_app_text(source)
        old_feature_constants = '''AD_CONTROL_MAX_LIVE_EXECUTE_PER_ACCOUNT = int(os.environ.get("AD_CONTROL_MAX_LIVE_EXECUTE_PER_ACCOUNT", "20"))
AD_CONTROL_LIVE_EXECUTE_MAX_WORKERS = int(os.environ.get("AD_CONTROL_LIVE_EXECUTE_MAX_WORKERS", "4"))
AD_CONTROL_ACTION_LOG_DB_NAME = os.environ.get("AD_CONTROL_ACTION_LOG_DB_NAME", "ads_ai").strip() or "ads_ai"
AD_CONTROL_ACTION_LOG_TABLE = os.environ.get("AD_CONTROL_ACTION_LOG_TABLE", "ad_control_action_log").strip() or "ad_control_action_log"
AD_CONTROL_ACTION_LOG_MYSQL_HOST = os.environ.get("AD_CONTROL_ACTION_LOG_MYSQL_HOST", MYSQL_HOST).strip()
AD_CONTROL_ACTION_LOG_MYSQL_PORT = os.environ.get("AD_CONTROL_ACTION_LOG_MYSQL_PORT", MYSQL_PORT).strip()
AD_CONTROL_ACTION_LOG_MYSQL_USER = os.environ.get("AD_CONTROL_ACTION_LOG_MYSQL_USER", MYSQL_USER).strip()
AD_CONTROL_ACTION_LOG_MYSQL_PASSWORD = os.environ.get("AD_CONTROL_ACTION_LOG_MYSQL_PASSWORD", MYSQL_PASSWORD)
AD_CONTROL_ACTION_LOG_CONNECT_TIMEOUT = int(os.environ.get("AD_CONTROL_ACTION_LOG_CONNECT_TIMEOUT", "5"))
AD_CONTROL_ACTION_LOG_IO_TIMEOUT = int(os.environ.get("AD_CONTROL_ACTION_LOG_IO_TIMEOUT", "8"))
AD_CONTROL_ACTION_LOG_LOCAL_OFFSET_HOURS = int(os.environ.get("AD_CONTROL_ACTION_LOG_LOCAL_OFFSET_HOURS", "8"))
AD_CONTROL_LIVE_MAX_WORKERS = int(os.environ.get("AD_CONTROL_LIVE_MAX_WORKERS", "12"))'''
        old_feature_source = safe.replace(
            deploy_fix.ACTION_LOG_CONSTANT_BLOCK, old_feature_constants, 1
        )
        self.assertNotEqual(safe, old_feature_source)
        updated, changed = deploy_fix.patch_app_text(old_feature_source)
        self.assertTrue(changed)
        self.assertEqual([], deploy_fix.action_log_safety_violations(updated))
        self.assertEqual(1, updated.count('or "63353").strip()'))
        self.assertEqual(1, updated.count('or "63350").strip()'))
        self.assertNotIn("def ad_control_action_log_config():", updated)

    def test_prior_generic_patch_output_is_safely_upgraded_and_idempotent(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8").replace("\r\n", "\n")
        safe, _ = deploy_fix.patch_app_text(source)
        legacy_constants = '''AD_CONTROL_MAX_LIVE_EXECUTE_PER_ACCOUNT = int(os.environ.get("AD_CONTROL_MAX_LIVE_EXECUTE_PER_ACCOUNT", "20"))
AD_CONTROL_LIVE_EXECUTE_MAX_WORKERS = int(os.environ.get("AD_CONTROL_LIVE_EXECUTE_MAX_WORKERS", "4"))
AD_CONTROL_ACTION_LOG_DB_NAME = os.environ.get("AD_CONTROL_ACTION_LOG_DB_NAME", "ads_ai").strip() or "ads_ai"
AD_CONTROL_ACTION_LOG_TABLE = os.environ.get("AD_CONTROL_ACTION_LOG_TABLE", "ad_control_action_log").strip() or "ad_control_action_log"
AD_CONTROL_ACTION_LOG_MYSQL_HOST = os.environ.get("AD_CONTROL_ACTION_LOG_MYSQL_HOST", MYSQL_HOST).strip()
AD_CONTROL_ACTION_LOG_MYSQL_PORT = os.environ.get("AD_CONTROL_ACTION_LOG_MYSQL_PORT", MYSQL_PORT).strip()
AD_CONTROL_ACTION_LOG_MYSQL_USER = os.environ.get("AD_CONTROL_ACTION_LOG_MYSQL_USER", MYSQL_USER).strip()
AD_CONTROL_ACTION_LOG_MYSQL_PASSWORD = os.environ.get("AD_CONTROL_ACTION_LOG_MYSQL_PASSWORD", MYSQL_PASSWORD)
AD_CONTROL_ACTION_LOG_CONNECT_TIMEOUT = int(os.environ.get("AD_CONTROL_ACTION_LOG_CONNECT_TIMEOUT", "5"))
AD_CONTROL_ACTION_LOG_IO_TIMEOUT = int(os.environ.get("AD_CONTROL_ACTION_LOG_IO_TIMEOUT", "8"))
AD_CONTROL_ACTION_LOG_LOCAL_OFFSET_HOURS = int(os.environ.get("AD_CONTROL_ACTION_LOG_LOCAL_OFFSET_HOURS", "8"))
AD_CONTROL_LIVE_MAX_WORKERS = int(os.environ.get("AD_CONTROL_LIVE_MAX_WORKERS", "12"))'''
        generic = deploy_fix.INTEGRATION_BLOCK
        reader = deploy_fix.function_matches(generic, "ad_control_action_log_reader_config")[0]
        generic = generic[:reader.start()] + generic[reader.end():]
        generic = generic.replace(
            "ad_control_action_log_writer_config", "ad_control_action_log_config"
        ).replace("ad_control_action_log_reader_config", "ad_control_action_log_config")
        unsafe = safe.replace(deploy_fix.ACTION_LOG_CONSTANT_BLOCK, legacy_constants, 1)
        unsafe = unsafe.replace(deploy_fix.INTEGRATION_BLOCK, generic, 1)
        self.assertIn("def ad_control_action_log_config():", unsafe)
        updated, changed = deploy_fix.patch_app_text(unsafe)
        self.assertTrue(changed)
        self.assertEqual([], deploy_fix.action_log_safety_violations(updated))
        reapplied, changed = deploy_fix.patch_app_text(updated)
        self.assertFalse(changed)
        self.assertEqual(updated, reapplied)

    def test_safety_validator_rejects_timeout_or_worker_regression(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8").replace("\r\n", "\n")
        safe, _ = deploy_fix.patch_app_text(source)
        regressed = safe.replace(
            'AD_CONTROL_ACTION_LOG_CONNECT_TIMEOUT = int(os.environ.get("AD_CONTROL_ACTION_LOG_CONNECT_TIMEOUT", "3"))',
            'AD_CONTROL_ACTION_LOG_CONNECT_TIMEOUT = int(os.environ.get("AD_CONTROL_ACTION_LOG_CONNECT_TIMEOUT", "5"))',
            1,
        ).replace(
            'AD_CONTROL_LIVE_MAX_WORKERS = int(os.environ.get("AD_CONTROL_LIVE_MAX_WORKERS", "4"))',
            'AD_CONTROL_LIVE_MAX_WORKERS = int(os.environ.get("AD_CONTROL_LIVE_MAX_WORKERS", "12"))',
            1,
        )
        violations = deploy_fix.action_log_safety_violations(regressed)
        self.assertTrue(any("CONNECT_TIMEOUT" in item for item in violations))
        self.assertTrue(any("LIVE_MAX_WORKERS" in item for item in violations))


class ExecutionBatchTests(unittest.TestCase):
    def test_batch_is_balanced_deterministic_and_capped_per_account(self):
        items = []
        for account_id, count in (("3", 30), ("1", 30), ("2", 30)):
            for index in range(count):
                items.append({
                    "account_id": "act_%s" % account_id,
                    "campaign_id": "%s-%02d" % (account_id, index),
                })
        selected = service.balanced_execution_items(items, max_total=200, max_per_account=20)
        self.assertEqual(60, len(selected))
        counts = {}
        for item in selected:
            account_id = service.normalize_account(item["account_id"])
            counts[account_id] = counts.get(account_id, 0) + 1
        self.assertEqual({"1": 20, "2": 20, "3": 20}, counts)
        self.assertEqual(["1", "2", "3"], [service.normalize_account(item["account_id"]) for item in selected[:3]])
        self.assertEqual(selected, service.balanced_execution_items(reversed(items), 200, 20))

    def test_global_cap_remains_200(self):
        items = [
            {"account_id": str(index), "campaign_id": str(index)}
            for index in range(250)
        ]
        self.assertEqual(200, len(service.balanced_execution_items(items, 200, 20)))


class GraphErrorTests(unittest.TestCase):
    def test_code_4_subcode_is_retryable_even_when_not_transient(self):
        reason = json.dumps({
            "message": "Application request limit reached",
            "type": "OAuthException",
            "is_transient": False,
            "code": 4,
            "error_subcode": 5044001,
        })
        details = service.graph_error_details(reason)
        self.assertTrue(details["retryable"])
        self.assertTrue(details["rate_limited"])
        self.assertEqual(4, details["error_code"])
        self.assertEqual(5044001, details["error_subcode"])

    def test_python_dict_shaped_error_is_parsed(self):
        details = service.graph_error_details(
            "{'error': {'message': 'limit', 'code': 4, 'error_subcode': 5044001}}"
        )
        self.assertEqual(4, details["error_code"])
        self.assertEqual(5044001, details["error_subcode"])


class SummaryTests(unittest.TestCase):
    def test_out_of_batch_deferred_and_retryable_are_all_remaining(self):
        results = [
            {"status": "success"},
            {"status": "error", "retryable": True},
            {"status": "deferred", "reason": "deferred_after_account_rate_limit"},
            {"status": "deferred", "reason": "deferred_after_account_rate_limit"},
        ]
        summary = service.execution_summary(results, matched_count=218, requested_count=200)
        self.assertEqual("partial", summary["run_status"])
        self.assertEqual(20, summary["deferred_count"])
        self.assertEqual(1, summary["retryable_error_count"])
        self.assertEqual(21, summary["remaining_count"])

    def test_permanent_error_blocks_run(self):
        summary = service.execution_summary(
            [{"status": "error", "retryable": False}], matched_count=1, requested_count=1
        )
        self.assertEqual("blocked", summary["run_status"])
        self.assertEqual(1, summary["permanent_error_count"])

    def test_nonterminal_skip_blocks_run(self):
        summary = service.execution_summary(
            [{"status": "skipped", "reason": "missing_meta_token"}], 1, 1
        )
        self.assertEqual("blocked", summary["run_status"])
        self.assertEqual(1, summary["blocked_count"])

    def test_not_active_is_terminal_skip(self):
        summary = service.execution_summary(
            [{"status": "skipped", "reason": "not_active"}], 1, 1
        )
        self.assertEqual("executed", summary["run_status"])
        self.assertEqual(1, summary["terminal_skip_count"])


class DailyGroupingTests(unittest.TestCase):
    @staticmethod
    def item(
        action_id,
        created_at,
        run_status="partial",
        remaining=0,
        requested=200,
        success=200,
        skipped=0,
        error=0,
        retryable=0,
        binding_id="binding-1",
        event_key="",
        runner_reason="",
        actor="ad_control_rule_runner",
        source_type="scheduled",
        log_version=2,
        verification=False,
    ):
        return {
            "action_id": action_id,
            "preview_id": "preview-" + action_id,
            "binding_id": binding_id,
            "rule_id": "",
            "event_key": event_key,
            "source_type": source_type,
            "actor_user_id": actor,
            "product": "dramawave",
            "action": "pause",
            "level": "campaign",
            "run_status": run_status,
            "runner_reason": runner_reason,
            "dry_run": False,
            "scanned_count": 1000,
            "candidate_count": 500,
            "matched_count": 386,
            "batch_planned_count": requested,
            "deferred_count": remaining,
            "requested_count": requested,
            "success_count": success,
            "skipped_count": skipped,
            "error_count": error,
            "retryable_error_count": retryable,
            "blocked_count": 0,
            "remaining_count": remaining,
            "criteria": {
                "rule_group_id": binding_id,
                "verification_only": verification,
            },
            "reason_summary": [],
            "results": [],
            "log_version": log_version,
            "created_at": created_at,
            "updated_at": created_at,
            "log_store": "ads_ai",
        }

    def test_same_business_day_batches_reduce_to_final_executed_group(self):
        items = [
            self.item("a1", "2026-07-15 04:13:18", remaining=195, success=169, skipped=31),
            self.item(
                "a2",
                "2026-07-15 04:22:58",
                run_status="executed",
                remaining=0,
                requested=186,
                success=186,
            ),
        ]
        grouped = service.group_actions_daily(
            items,
            limit=50,
            now=datetime(2026, 7, 15, 6, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(1, len(grouped["items"]))
        item = grouped["items"][0]
        self.assertEqual("2026-07-15", item["business_date"])
        self.assertEqual(2, item["batch_count"])
        self.assertEqual(386, item["attempt_count"])
        self.assertEqual(355, item["success_count"])
        self.assertEqual(31, item["skipped_count"])
        self.assertEqual(0, item["remaining_count"])
        self.assertEqual("success", item["display_status"]["key"])
        self.assertEqual("当日执行完成", item["display_status"]["label"])

    def test_real_runner_chain_only_completes_after_zero_target_verification(self):
        items = [
            self.item("a1", "2026-07-15 04:13:18", remaining=186, success=200),
            self.item(
                "a2",
                "2026-07-15 04:22:58",
                remaining=0,
                requested=186,
                success=186,
                runner_reason="live_execute_verify_remaining",
            ),
            self.item(
                "a3",
                "2026-07-15 04:32:58",
                run_status="executed",
                remaining=0,
                requested=0,
                success=0,
                verification=True,
            ),
        ]
        grouped = service.group_actions_daily(
            items,
            now=datetime(2026, 7, 15, 5, 0, tzinfo=timezone.utc),
        )["items"][0]
        self.assertEqual(3, grouped["batch_count"])
        self.assertEqual(2, grouped["execution_batch_count"])
        self.assertEqual(1, grouped["verification_batch_count"])
        self.assertEqual("success", grouped["display_status"]["key"])

    def test_same_day_different_event_keys_still_merge(self):
        items = [
            self.item("a1", "2026-07-15 04:00:00", event_key="b:pause:2026-07-15:12:00"),
            self.item("a2", "2026-07-15 05:00:00", event_key="b:pause:2026-07-15:13:00"),
        ]
        self.assertEqual(1, len(service.group_actions_daily(items)["items"]))

    def test_later_success_event_does_not_hide_earlier_blocked_event(self):
        items = [
            self.item(
                "blocked",
                "2026-07-15 04:00:00",
                run_status="blocked",
                remaining=0,
                success=0,
                error=1,
                event_key="b:pause:2026-07-15:12:00",
            ),
            self.item(
                "success",
                "2026-07-15 05:00:00",
                run_status="executed",
                remaining=0,
                event_key="b:pause:2026-07-15:13:00",
            ),
        ]
        grouped = service.group_actions_daily(items)["items"][0]
        self.assertEqual(2, grouped["event_count"])
        self.assertEqual("blocked", grouped["display_status"]["key"])

    def test_later_success_event_does_not_hide_earlier_open_event(self):
        items = [
            self.item(
                "open",
                "2026-07-15 04:00:00",
                remaining=10,
                event_key="b:pause:2026-07-15:12:00",
            ),
            self.item(
                "success",
                "2026-07-15 05:00:00",
                run_status="executed",
                remaining=0,
                event_key="b:pause:2026-07-15:13:00",
            ),
        ]
        grouped = service.group_actions_daily(
            items,
            now=datetime(2026, 7, 15, 5, 1, tzinfo=timezone.utc),
        )["items"][0]
        self.assertEqual(10, grouped["remaining_count"])
        self.assertEqual("partial", grouped["display_status"]["key"])

    def test_stale_legacy_partial_with_retryable_error_is_incomplete(self):
        items = [
            self.item("a1", "2026-07-14 04:09:17", remaining=727),
            self.item("a2", "2026-07-14 04:22:23", remaining=460),
            self.item("a3", "2026-07-14 04:33:09", remaining=220),
            self.item(
                "a4",
                "2026-07-14 04:43:04",
                remaining=19,
                success=199,
                error=1,
                retryable=1,
                log_version=1,
            ),
        ]
        grouped = service.group_actions_daily(
            items,
            now=datetime(2026, 7, 15, 6, 0, tzinfo=timezone.utc),
        )
        item = grouped["items"][0]
        self.assertEqual(4, item["batch_count"])
        self.assertEqual(800, item["attempt_count"])
        self.assertEqual(19, item["remaining_count"])
        self.assertTrue(item["status_inferred"])
        self.assertEqual("incomplete", item["display_status"]["key"])
        self.assertIn("限流", item["display_status"]["label"])

    def test_event_key_keeps_cross_midnight_continuation_on_original_day(self):
        event_key = "binding-1:pause:2026-07-14:12:00"
        items = [
            self.item("a1", "2026-07-14 15:55:00", event_key=event_key, remaining=10),
            self.item(
                "a2",
                "2026-07-14 16:05:00",
                event_key=event_key,
                run_status="executed",
                remaining=0,
            ),
        ]
        grouped = service.group_actions_daily(items)
        self.assertEqual(1, len(grouped["items"]))
        self.assertEqual("2026-07-14", grouped["items"][0]["business_date"])

    def test_manual_actions_without_event_key_remain_separate(self):
        items = [
            self.item("a1", "2026-07-15 04:00:00", actor="admin", source_type="manual"),
            self.item("a2", "2026-07-15 05:00:00", actor="admin", source_type="manual"),
        ]
        grouped = service.group_actions_daily(items)["items"]
        self.assertEqual(2, len(grouped))
        self.assertEqual(2, len({item["group_id"] for item in grouped}))
        self.assertTrue(all(item["group_type"] == "action" for item in grouped))

    def test_manual_actions_with_event_key_still_remain_separate(self):
        items = [
            self.item(
                "a1",
                "2026-07-15 04:00:00",
                actor="admin",
                source_type="manual",
                event_key="manual:2026-07-15:1",
            ),
            self.item(
                "a2",
                "2026-07-15 05:00:00",
                actor="admin",
                source_type="manual",
                event_key="manual:2026-07-15:2",
            ),
        ]
        self.assertEqual(2, len(service.group_actions_daily(items)["items"]))

    def test_different_rules_never_merge(self):
        items = [
            self.item("a1", "2026-07-15 04:00:00", binding_id="binding-1"),
            self.item("a2", "2026-07-15 04:01:00", binding_id="binding-2"),
        ]
        self.assertEqual(2, len(service.group_actions_daily(items)["items"]))

    def test_product_action_level_and_mode_are_isolated(self):
        base = self.item("base", "2026-07-15 04:00:00")
        variants = []
        for action_id, field, value in (
            ("product", "product", "hotdrama"),
            ("action", "action", "reopen"),
            ("level", "level", "adset"),
            ("mode", "dry_run", True),
        ):
            variant = dict(base)
            variant["action_id"] = action_id
            variant[field] = value
            variants.append(variant)
        self.assertEqual(5, len(service.group_actions_daily([base] + variants)["items"]))

    def test_scheduled_rows_without_rule_identity_remain_separate(self):
        first = self.item("a1", "2026-07-15 04:00:00", binding_id="")
        second = self.item("a2", "2026-07-15 04:01:00", binding_id="")
        first["criteria"] = {}
        second["criteria"] = {}
        self.assertEqual(2, len(service.group_actions_daily([first, second])["items"]))

    def test_same_second_verification_wins_deterministically(self):
        items = [
            self.item("z-partial", "2026-07-15 04:00:00", remaining=0),
            self.item(
                "a-verify",
                "2026-07-15 04:00:00",
                run_status="executed",
                requested=0,
                success=0,
                verification=True,
            ),
        ]
        grouped = service.group_actions_daily(items)
        self.assertEqual("a-verify", grouped["items"][0]["latest_action_id"])
        self.assertEqual("success", grouped["items"][0]["display_status"]["key"])

    def test_same_second_blocked_wins_over_non_verification_executed(self):
        items = [
            self.item("z-success", "2026-07-15 04:00:00", run_status="executed", remaining=0),
            self.item("a-blocked", "2026-07-15 04:00:00", run_status="blocked", error=1),
        ]
        grouped = service.group_actions_daily(items)["items"][0]
        self.assertEqual("blocked", grouped["display_status"]["key"])

    def test_executed_with_remaining_is_never_complete(self):
        item = self.item("a1", "2026-07-15 04:00:00", run_status="executed", remaining=1)
        grouped = service.group_actions_daily([item])["items"][0]
        self.assertEqual("inconsistent", grouped["display_status"]["key"])

    def test_non_retryable_partial_error_is_blocked(self):
        item = self.item("a1", "2026-07-15 04:00:00", error=1, retryable=0)
        grouped = service.group_actions_daily(
            [item],
            now=datetime(2026, 7, 15, 4, 1, tzinfo=timezone.utc),
        )["items"][0]
        self.assertEqual("blocked", grouped["display_status"]["key"])

    def test_legacy_empty_status_with_remaining_is_not_complete(self):
        item = self.item(
            "a1",
            "2026-07-15 04:00:00",
            run_status="",
            remaining=5,
            success=1,
            log_version=1,
        )
        grouped = service.group_actions_daily(
            [item],
            now=datetime(2026, 7, 15, 4, 1, tzinfo=timezone.utc),
        )["items"][0]
        self.assertEqual("partial", grouped["display_status"]["key"])

    def test_partial_zero_remaining_waits_for_verification(self):
        item = self.item(
            "a1",
            "2026-07-15 04:00:00",
            remaining=0,
            runner_reason="live_execute_verify_remaining",
        )
        grouped = service.group_actions_daily(
            [item],
            now=datetime(2026, 7, 15, 4, 1, tzinfo=timezone.utc),
        )["items"][0]
        self.assertEqual("verifying", grouped["display_status"]["key"])

    def test_partial_stale_window_is_three_hours(self):
        item = self.item("a1", "2026-07-15 04:00:00", remaining=1)
        at_boundary = service.group_actions_daily(
            [item],
            now=datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc),
        )["items"][0]
        after_boundary = service.group_actions_daily(
            [item],
            now=datetime(2026, 7, 15, 7, 0, 1, tzinfo=timezone.utc),
        )["items"][0]
        self.assertEqual("partial", at_boundary["display_status"]["key"])
        self.assertEqual("incomplete", after_boundary["display_status"]["key"])

    def test_group_id_does_not_change_when_a_new_batch_arrives(self):
        first = self.item("a1", "2026-07-15 04:00:00")
        group_id = service.group_actions_daily([first])["items"][0]["group_id"]
        second = self.item("a2", "2026-07-15 04:05:00", run_status="executed", remaining=0)
        self.assertEqual(
            group_id,
            service.group_actions_daily([first, second])["items"][0]["group_id"],
        )

    def test_ads_ai_duplicate_wins_even_when_sqlite_is_first(self):
        fallback = self.item("a1", "2026-07-15 04:00:00", remaining=10)
        fallback["log_store"] = "sqlite_fallback"
        mysql = self.item("a1", "2026-07-15 04:00:00", run_status="executed", remaining=0)
        mysql["log_store"] = "ads_ai"
        grouped = service.group_actions_daily([fallback, mysql])["items"][0]
        self.assertEqual("success", grouped["display_status"]["key"])
        self.assertEqual("ads_ai", grouped["log_store"])

    def test_truncated_source_drops_possibly_incomplete_oldest_group(self):
        items = [
            self.item("old", "2026-07-14 04:00:00", binding_id="old-binding"),
            self.item("new", "2026-07-15 04:00:00", binding_id="new-binding"),
        ]
        grouped = service.group_actions_daily(items, source_truncated=True)
        self.assertTrue(grouped["truncated"])
        self.assertEqual(1, grouped["discarded_group_count"])
        self.assertEqual("new-binding", grouped["items"][0]["binding_id"])

    def test_truncated_source_drops_all_groups_on_boundary_business_date(self):
        items = [
            self.item("old-a", "2026-07-14 04:00:00", binding_id="old-a"),
            self.item("old-b", "2026-07-14 04:01:00", binding_id="old-b"),
            self.item("new", "2026-07-15 04:00:00", binding_id="new"),
        ]
        grouped = service.group_actions_daily(items, source_truncated=True)
        self.assertEqual(2, grouped["discarded_group_count"])
        self.assertEqual(["new"], [item["binding_id"] for item in grouped["items"]])

    def test_limit_applies_after_grouping(self):
        items = [
            self.item("a1", "2026-07-15 04:00:00", binding_id="a"),
            self.item("a2", "2026-07-15 04:05:00", binding_id="a"),
            self.item("b1", "2026-07-15 04:10:00", binding_id="b"),
            self.item("c1", "2026-07-15 04:15:00", binding_id="c"),
        ]
        grouped = service.group_actions_daily(items, limit=2)
        self.assertEqual(2, len(grouped["items"]))
        self.assertEqual(3, grouped["group_count"])
        self.assertTrue(grouped["truncated"])


class DailyListFunctionTests(unittest.TestCase):
    class EmptyConnection:
        def execute(self, *args, **kwargs):
            return self

        def fetchall(self):
            return []

        def close(self):
            return None

    def namespace(self, mysql_items, has_more=False):
        captured = {}

        class ServiceProxy:
            action_business_date = staticmethod(service.action_business_date)
            group_actions_daily = staticmethod(service.group_actions_daily)

            @staticmethod
            def list_actions_page(config, filters, limit, table):
                captured.update({"filters": filters, "limit": limit, "table": table})
                values = list(mysql_items)
                if filters.get("date_from"):
                    values = [item for item in values if str(item.get("created_at") or "") >= filters["date_from"]]
                if filters.get("date_to"):
                    values = [item for item in values if str(item.get("created_at") or "") <= filters["date_to"]]
                return {"items": values, "has_more": has_more, "limit": limit}

        def utc_bound(value, end=False):
            value = str(value or "").strip()
            if not value:
                return ""
            local_dt = datetime.strptime(value[:10], "%Y-%m-%d")
            if end:
                local_dt += timedelta(days=1, seconds=-1)
            return (local_dt - timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")

        namespace = {
            "ensure_ad_control_tables": lambda: None,
            "ad_control_int": lambda value, default, minimum, maximum: max(minimum, min(maximum, int(value or default))),
            "ad_control_action_log_utc_bound": utc_bound,
            "ad_control_execution_log_service": ServiceProxy,
            "ad_control_action_log_reader_config": lambda: {},
            "AD_CONTROL_ACTION_LOG_TABLE": "ad_control_action_log",
            "AD_CONTROL_ACTION_LOG_LOCAL_OFFSET_HOURS": 8,
            "datetime": datetime,
            "timedelta": timedelta,
            "logging": logging,
            "JOB_DB_LOCK": threading.Lock(),
            "get_job_db_connection": self.EmptyConnection,
            "ad_control_safe_json_dict": lambda value: json.loads(value or "{}"),
            "ad_control_action_rule_map": lambda items: {},
        }
        exec(deploy_fix.ACTION_STATUS_FUNCTION, namespace)

        def audit(item, rule_map, include_samples=False):
            return {
                "status": namespace["ad_control_action_status"](item),
                "counts": {
                    "requested": int(item.get("requested_count") or 0),
                    "success": int(item.get("success_count") or 0),
                    "skipped": int(item.get("skipped_count") or 0),
                    "error": int(item.get("error_count") or 0),
                },
                "reason_summary": [],
                "samples": [],
                "log_store": item.get("log_store") or "ads_ai",
            }

        namespace["ad_control_action_audit"] = audit
        exec(deploy_fix.LIST_ACTIONS_FUNCTION, namespace)
        return namespace, captured

    def test_daily_date_filter_widens_query_and_filters_by_business_date(self):
        item = DailyGroupingTests.item(
            "a1",
            "2026-07-14 16:05:00",
            run_status="executed",
            remaining=0,
            event_key="binding-1:pause:2026-07-14:23:55",
        )
        namespace, captured = self.namespace([item])
        response = namespace["list_ad_control_actions"](
            product="dramawave",
            date_from="2026-07-14",
            date_to="2026-07-14",
            view="daily",
            internal=True,
        )
        self.assertEqual("daily", response["view"])
        self.assertEqual(1, len(response["items"]))
        self.assertEqual("2026-07-14", response["items"][0]["business_date"])
        self.assertEqual("2026-07-12 16:00:00", captured["filters"]["date_from"])
        self.assertEqual("2026-07-15 15:59:59", captured["filters"]["date_to"])
        self.assertEqual(1000, captured["limit"])

    def test_raw_view_keeps_action_rows_and_caps_reader_at_200(self):
        item = DailyGroupingTests.item("a1", "2026-07-15 04:00:00")
        namespace, captured = self.namespace([item])
        response = namespace["list_ad_control_actions"](
            limit=999, view="raw", internal=True
        )
        self.assertEqual("raw", response["view"])
        self.assertFalse(response["items"][0].get("is_daily_group"))
        self.assertEqual(200, captured["limit"])

    def test_raw_legacy_status_does_not_hide_remaining(self):
        namespace, _ = self.namespace([])
        status = namespace["ad_control_action_status"]({
            "run_status": "",
            "success_count": 1,
            "remaining_count": 5,
        })
        self.assertEqual("partial", status["key"])

    def test_daily_observe_audit_is_not_overwritten_by_group_status(self):
        item = DailyGroupingTests.item(
            "observe-a1",
            "2026-07-15 04:00:00",
            run_status="executed",
            remaining=0,
        )
        item["criteria"]["run_mode"] = "observe"
        namespace, _ = self.namespace([item])
        response = namespace["list_ad_control_actions"](
            view="daily", internal=True
        )
        audit = response["items"][0]["audit"]
        self.assertEqual("observe", audit["mode"])
        self.assertEqual("只观察", audit["mode_label"])
        self.assertEqual("observed", audit["status"]["key"])


class PersistenceShapeTests(unittest.TestCase):
    @staticmethod
    def writer_config(**overrides):
        config = {
            "host": service.WRITER_HOST,
            "port": service.WRITER_PORT,
            "user": "ads_aius",
            "password": "secret",
            "database": service.WRITER_DATABASE,
            "connect_timeout": 30,
            "read_timeout": 30,
            "write_timeout": 30,
        }
        config.update(overrides)
        return config

    @staticmethod
    def reader_config(**overrides):
        config = PersistenceShapeTests.writer_config(
            host=service.READER_HOST,
            port=service.READER_PORT,
        )
        config.update(overrides)
        return config

    def test_sensitive_fields_are_redacted(self):
        value = service.sanitize_json({
            "access_token": "secret",
            "nested": {"password": "p", "safe": "ok"},
        })
        self.assertEqual("[REDACTED]", value["access_token"])
        self.assertEqual("[REDACTED]", value["nested"]["password"])
        self.assertEqual("ok", value["nested"]["safe"])

    def test_normalized_record_preserves_chinese_and_defaults(self):
        record = service.normalize_record({
            "action_id": "a1",
            "criteria": {"产品": "短剧"},
            "results": [{"status": "skipped", "reason": "已关停"}],
        })
        self.assertIn("短剧", record["criteria_json"])
        self.assertIn("已关停", record["results_json"])
        self.assertEqual(1, record["log_version"])

    def test_mysql_ddl_is_deployment_only_and_has_required_indexes(self):
        self.assertFalse(hasattr(service, "ensure_table"))
        self.assertFalse(hasattr(service, "table_ddl"))
        ddl_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "doc",
            "006.ai-auto-rule-control-execution-log",
            "001_create_ad_control_action_log.sql",
        )
        with open(ddl_path, "r", encoding="utf-8") as handle:
            ddl = handle.read()
        self.assertIn("`ads_ai`.`ad_control_action_log`", ddl)
        self.assertIn("PRIMARY KEY (`action_id`)", ddl)
        self.assertIn("`idx_acl_event_created`", ddl)

    def test_writer_and_reader_endpoints_are_fail_closed(self):
        valid_writer = service.validate_target(self.writer_config(), "writer")
        self.assertEqual(service.WRITER_PORT, valid_writer["port"])
        self.assertEqual(3, valid_writer["connect_timeout"])
        self.assertEqual(5, valid_writer["read_timeout"])
        service.validate_target(self.reader_config(), "reader")
        for overrides in (
            {"host": "127.0.0.1"},
            {"port": service.READER_PORT},
            {"database": "kunlunads_dev"},
        ):
            with self.assertRaises(service.ActionLogSafetyError):
                service.validate_target(self.writer_config(**overrides), "writer")
        with self.assertRaises(service.ActionLogSafetyError):
            service.validate_target(
                self.writer_config(), "writer", table="other_table"
            )
        with self.assertRaises(service.ActionLogSafetyError):
            service.validate_target(self.writer_config(), "reader")

    def test_writer_sql_is_single_row_fixed_table_and_no_ddl(self):
        calls = []

        class Cursor:
            rowcount = 1

            def execute(self, sql, params):
                calls.append((sql, params))
                return 1

        with mock.patch.object(
            service,
            "_serialized_write",
            side_effect=lambda config, table, callback: callback(Cursor()),
        ):
            service.upsert_action(
                self.writer_config(),
                {"action_id": "a1", "criteria": {}, "results": []},
            )
            service.update_runner_status(
                self.writer_config(), "a1", "event", "partial", "reason", 1
            )
        self.assertEqual(2, len(calls))
        self.assertTrue(calls[0][0].startswith(
            "INSERT INTO `ads_ai`.`ad_control_action_log`"
        ))
        self.assertIn(
            "UPDATE `ads_ai`.`ad_control_action_log`", calls[1][0]
        )
        self.assertIn("WHERE action_id=%s LIMIT 1", calls[1][0])
        self.assertFalse(any("CREATE " in sql or "DELETE " in sql for sql, _ in calls))

    def test_payload_over_512_kib_fails_before_database_write(self):
        with mock.patch.object(service, "_serialized_write") as writer:
            with self.assertRaises(service.ActionLogSafetyError):
                service.upsert_action(
                    self.writer_config(),
                    {
                        "action_id": "a1",
                        "criteria": {"oversized": "x" * service.MAX_PAYLOAD_BYTES},
                        "results": [],
                    },
                )
            writer.assert_not_called()

    def test_host_wide_rate_limit_allows_burst_two_then_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = os.path.join(directory, "writer.lock")
            with mock.patch.object(service, "WRITER_LOCK_FILE", lock_path), mock.patch.object(
                service.time, "time", return_value=1000.0
            ):
                first = service._acquire_interprocess_write_slot()
                service._release_interprocess_write_slot(*first)
                second = service._acquire_interprocess_write_slot()
                service._release_interprocess_write_slot(*second)
                with self.assertRaises(service.ActionLogSafetyError):
                    service._acquire_interprocess_write_slot()

    def test_deploy_patch_splits_writer_and_reader_configs(self):
        self.assertIn(
            "def ad_control_action_log_writer_config():",
            deploy_fix.INTEGRATION_BLOCK,
        )
        self.assertIn(
            "def ad_control_action_log_reader_config():",
            deploy_fix.INTEGRATION_BLOCK,
        )
        self.assertIn("63353", deploy_fix.INTEGRATION_BLOCK)
        self.assertIn("63350", deploy_fix.INTEGRATION_BLOCK)
        self.assertIn(
            'observe_mode = str(criteria.get("run_mode") or "").strip().lower() == "observe"',
            deploy_fix.LIST_ACTIONS_FUNCTION,
        )
        self.assertIn('{"key": "observed", "label": "观察完成", "class": "ok"}', deploy_fix.LIST_ACTIONS_FUNCTION)
        self.assertIn('"mode": mode', deploy_fix.LIST_ACTIONS_FUNCTION)
        self.assertIn('"mode_label": mode_label', deploy_fix.LIST_ACTIONS_FUNCTION)
        self.assertIn('view = "daily"', deploy_fix.LIST_ACTIONS_FUNCTION)
        self.assertIn(
            "ad_control_execution_log_service.group_actions_daily(",
            deploy_fix.LIST_ACTIONS_FUNCTION,
        )
        self.assertIn('"owned_binding_ids": sorted(owned_group_ids)', deploy_fix.LIST_ACTIONS_FUNCTION)
        self.assertIn('"view": view', deploy_fix.LIST_ACTIONS_FUNCTION)
        patch_source = inspect.getsource(deploy_fix.patch_app_text)
        self.assertIn("audit_observe_status_new", patch_source)
        self.assertIn("audit_observe_mode_new", patch_source)
        self.assertIn('"mode": "observe" if observe_mode', patch_source)

    def test_all_live_preview_paths_default_to_four_workers(self):
        root = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(root, "app.py"), "r", encoding="utf-8") as handle:
            app_source = handle.read()
        with open(os.path.join(root, ".env.example"), "r", encoding="utf-8") as handle:
            env_source = handle.read()
        deploy_source = os.path.join(root, "deploy", "apply_ad_control_execution_log_fix.py")
        with open(deploy_source, "r", encoding="utf-8") as handle:
            patch_source = handle.read()
        expected = 'AD_CONTROL_LIVE_MAX_WORKERS = int(os.environ.get("AD_CONTROL_LIVE_MAX_WORKERS", "4"))'
        self.assertIn(expected, app_source)
        self.assertIn('AD_CONTROL_LIVE_MAX_WORKERS=4', env_source)
        self.assertIn(expected, deploy_fix.ACTION_LOG_CONSTANT_BLOCK)
        self.assertIn("ensure_action_log_safety_constants(text)", patch_source)
        self.assertNotIn(
            'AD_CONTROL_LIVE_MAX_WORKERS = int(os.environ.get("AD_CONTROL_LIVE_MAX_WORKERS", "12"))',
            app_source,
        )

    def test_migration_is_capped_and_has_no_force_overwrite(self):
        migration_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "scripts",
            "migrate_ad_control_action_logs.py",
        )
        with open(migration_path, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("MAX_MIGRATION_ROWS = 20", source)
        self.assertIn("MIN_WRITE_INTERVAL_SECONDS = 1.0", source)
        self.assertNotIn('"--force"', source)

    def test_replace_once_is_idempotent_when_new_contains_old(self):
        old = "MAX = 200\n"
        new = old + "PER_ACCOUNT = 20\n"
        patched, changed = replace_once(old, old, new, "test")
        self.assertTrue(changed)
        again, changed_again = replace_once(patched, old, new, "test")
        self.assertFalse(changed_again)
        self.assertEqual(patched, again)

    def test_replace_function_supports_multiline_signature(self):
        source = "def target(\n    first,\n    second=None,\n):\n    return first\n\n\ndef after():\n    return 2\n"
        replacement = "def target(value):\n    return value + 1"
        patched, changed = deploy_fix.replace_function(source, "target", replacement)
        self.assertTrue(changed)
        self.assertIn("def target(value):\n    return value + 1", patched)
        self.assertIn("def after():\n    return 2", patched)

    def test_repo_list_function_matches_deployment_template(self):
        root = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(root, "app.py"), "r", encoding="utf-8") as handle:
            app_source = handle.read().replace("\r\n", "\n")
        matches = deploy_fix.function_matches(app_source, "list_ad_control_actions")
        self.assertEqual(1, len(matches))
        self.assertEqual(
            deploy_fix.LIST_ACTIONS_FUNCTION.rstrip(),
            matches[0].group(0).rstrip(),
        )

    def test_mysql_datetimes_are_json_serializable_strings(self):
        row = {key: "" for key in service.LOG_COLUMNS}
        row.update({
            "action_id": "a1",
            "criteria_json": "{}",
            "results_json": "[]",
            "reason_summary_json": "[]",
            "created_at": datetime(2026, 7, 15, 1, 2, 3),
            "updated_at": datetime(2026, 7, 15, 4, 5, 6),
        })
        decoded = service._decode_row(row)
        self.assertEqual("2026-07-15 01:02:03", decoded["created_at"])
        self.assertEqual("2026-07-15 04:05:06", decoded["updated_at"])
        json.dumps(decoded, ensure_ascii=False)

    def test_foreign_mysql_action_error_is_not_swallowed_by_sqlite_fallback(self):
        self.assertIn(
            "except StructuredApiError:\n        raise",
            deploy_fix.FETCH_ACTION_FUNCTION,
        )

    def test_list_page_is_lightweight_bounded_and_reports_more(self):
        calls = []

        class Cursor:
            def execute(self, sql, params):
                calls.append((sql, params))

            def fetchall(self):
                rows = []
                for index in range(4):
                    row = {key: "" for key in service.LOG_COLUMNS if key != "results_json"}
                    row.update({
                        "action_id": "a%s" % index,
                        "criteria_json": "{}",
                        "reason_summary_json": "[]",
                        "created_at": "2026-07-15 00:00:0%s" % index,
                        "updated_at": "2026-07-15 00:00:0%s" % index,
                    })
                    rows.append(row)
                return rows

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Connection:
            def cursor(self):
                return Cursor()

            def close(self):
                return None

        with mock.patch.object(service, "_connect", return_value=Connection()):
            page = service.list_actions_page(self.reader_config(), limit=3)
        self.assertEqual(3, len(page["items"]))
        self.assertTrue(page["has_more"])
        self.assertNotIn("results_json", calls[0][0])
        self.assertEqual(4, calls[0][1][-1])


class LiveExecuteContractTests(unittest.TestCase):
    class _Connection:
        def execute(self, *args, **kwargs):
            return self

        def commit(self):
            return None

        def close(self):
            return None

    def namespace(
        self, items, graph_get, graph_set, workers=1,
        run_mode="live", object_level="campaign", token_for_user=None,
        product="dramawave", product_whitelist=None, account_whitelists=None,
    ):
        criteria = {
            "mode": "live",
            "product": product,
            "accounts": ["1", "2"],
            "preview_hash": "hash",
            "execution_target_count": len(items),
            "execution_batch_count": len(items),
            "scan_count": len(items),
            "candidate_count": len(items),
            "run_mode": run_mode,
            "object_level": object_level,
        }
        campaign_by_account = {}
        for item in items:
            account_id = service.normalize_account(item["account_id"])
            campaign_by_account.setdefault(account_id, {})[item["campaign_id"]] = {}

        def guarded_pause(preview, criteria, token, item, account_id):
            meta = graph_get(token, item["campaign_id"], "account_id,status,effective_status,name")
            meta_account = service.normalize_account(meta.get("account_id"))
            if not meta_account or meta_account != account_id:
                return {"meta": meta, "skip_reason": "account_owner_mismatch"}
            if str(meta.get("effective_status") or "").upper() != "ACTIVE":
                return {"meta": meta, "skip_reason": "not_active"}
            return {
                "meta": meta,
                "payload_result": graph_set(token, item["campaign_id"], "PAUSED"),
            }

        namespace = {
            "ensure_ad_control_tables": lambda: None,
            "fetch_ad_control_preview": lambda preview_id: {
                "preview_id": preview_id,
                "actor_user_id": "runner",
                "criteria_json": json.dumps(criteria),
                "sample_json": json.dumps(items),
            },
            "ad_control_safe_json_dict": lambda value: json.loads(value or "{}"),
            "ad_control_safe_json_list": lambda value: json.loads(value or "[]"),
            "StructuredApiError": RuntimeError,
            "AD_CONTROL_MAX_LIVE_EXECUTE": 200,
            "AD_CONTROL_LIVE_EXECUTE_MAX_WORKERS": workers,
            "ad_control_token_config_for_accounts": lambda product, accounts: {
                account_id: {"user_id": "user-1"} for account_id in accounts
            },
            "ad_control_token_for_user_id": token_for_user or (lambda user_id: "token"),
            "ad_control_product_campaign_whitelist": product_whitelist or (
                lambda product, accounts: campaign_by_account
            ),
            "ad_control_account_campaign_whitelists": account_whitelists or (
                lambda accounts: {}
            ),
            "ad_control_copy_service": copy_service,
            "ad_control_normalize_account": service.normalize_account,
            "ad_control_graph_get": graph_get,
            "ad_control_graph_set_status": graph_set,
            "ad_control_validate_live_preview_group": lambda *args, **kwargs: {},
            "ad_control_guarded_campaign_pause": guarded_pause,
            "ad_control_save_object_state": lambda *args, **kwargs: None,
            "ad_control_execution_log_service": service,
            "JOB_DB_LOCK": threading.Lock(),
            "get_job_db_connection": lambda: self._Connection(),
            "ad_control_actor": lambda session: session.get("user_id", ""),
            "ad_control_persist_action_log": lambda *args, **kwargs: {},
            "concurrent": concurrent,
            "threading": threading,
            "logging": logging,
            "uuid": uuid,
            "json": json,
        }
        exec(deploy_fix.EXECUTE_LIVE_FUNCTION, namespace)
        return namespace

    @staticmethod
    def item(account_id, campaign_id):
        return {
            "account_id": account_id,
            "campaign_id": campaign_id,
            "object_id": campaign_id,
            "object_key": "%s:%s" % (account_id, campaign_id),
            "target_action": "pause",
        }

    def test_application_limit_opens_global_circuit_and_defers_unissued_work(self):
        items = [
            self.item("1", "c1"), self.item("1", "c2"),
            self.item("2", "c3"), self.item("2", "c4"),
        ]
        calls = {"get": 0, "set": 0}

        def graph_get(token, campaign_id, fields):
            calls["get"] += 1
            account_id = "1" if campaign_id in {"c1", "c2"} else "2"
            return {"account_id": account_id, "effective_status": "ACTIVE"}

        def graph_set(token, campaign_id, status):
            calls["set"] += 1
            raise RuntimeError(json.dumps({
                "message": "Application request limit reached",
                "code": 4,
                "error_subcode": 5044001,
            }))

        namespace = self.namespace(items, graph_get, graph_set, workers=1)
        with self.assertLogs(level="ERROR"):
            result = namespace["execute_ad_control_live"]({
                "preview_id": "p1",
                "preview_hash": "hash",
                "dry_run": False,
                "confirm": "EXECUTE_LIVE_PAUSE",
            }, {"user_id": "runner"})
        self.assertEqual(1, calls["get"])
        self.assertEqual(1, calls["set"])
        self.assertEqual(1, result["error_count"])
        self.assertEqual(3, result["deferred_count"])
        self.assertEqual(4, result["remaining_count"])
        self.assertEqual(3, len([item for item in result["results"] if item["status"] == "deferred"]))

    def test_missing_meta_owner_is_fail_closed(self):
        items = [self.item("1", "c1")]
        calls = {"set": 0}

        def graph_set(token, campaign_id, status):
            calls["set"] += 1
            return {"success": True}

        namespace = self.namespace(
            items,
            lambda token, campaign_id, fields: {"effective_status": "ACTIVE"},
            graph_set,
        )
        result = namespace["execute_ad_control_live"]({
            "preview_id": "p1",
            "preview_hash": "hash",
            "dry_run": False,
            "confirm": "EXECUTE_LIVE_PAUSE",
        }, {"user_id": "runner"})
        self.assertEqual(0, calls["set"])
        self.assertEqual("account_owner_mismatch", result["results"][0]["reason"])
        self.assertEqual("blocked", result["run_status"])

    def test_observe_mode_records_objects_without_token_or_graph_access(self):
        items = [self.item("1", "c1")]
        namespace = self.namespace(
            items,
            lambda *args: (_ for _ in ()).throw(AssertionError("Graph GET")),
            lambda *args: (_ for _ in ()).throw(AssertionError("Graph POST")),
            run_mode="observe",
            token_for_user=lambda user_id: (_ for _ in ()).throw(AssertionError("token read")),
        )
        result = namespace["execute_ad_control_live"]({
            "preview_id": "p1", "preview_hash": "hash", "dry_run": False,
        }, {"user_id": "runner"})
        self.assertEqual(0, result["success_count"])
        self.assertEqual(1, result["skipped_count"])
        self.assertEqual("observed", result["results"][0]["status"])
        self.assertEqual("would_pause", result["results"][0]["reason"])
        self.assertEqual("c1", result["results"][0]["object_id"])

    def test_campaign_copy_is_blocked_before_token_or_graph_access(self):
        item = self.item("1", "c1")
        item["target_action"] = "copy"
        namespace = self.namespace(
            [item],
            lambda *args: (_ for _ in ()).throw(AssertionError("Graph GET")),
            lambda *args: (_ for _ in ()).throw(AssertionError("Graph POST")),
            token_for_user=lambda user_id: (_ for _ in ()).throw(AssertionError("token read")),
        )
        result = namespace["execute_ad_control_live"]({
            "preview_id": "p1", "preview_hash": "hash", "dry_run": False,
            "confirm": "EXECUTE_LIVE_RULE_GROUP",
        }, {"user_id": "runner"})
        self.assertEqual("copy_persistence_not_configured", result["results"][0]["reason"])
        self.assertEqual(1, result["blocked_count"])
        self.assertEqual("blocked", result["run_status"])

    def test_account_only_mixed_group_never_requeries_product_whitelists(self):
        pause_item = self.item("1", "pause-1")
        pause_item["token_user_id"] = "user-1"
        copy_item = self.item("1", "copy-1")
        copy_item["target_action"] = "copy"
        copy_item["token_user_id"] = "user-1"
        calls = {"set": 0}

        def graph_set(token, campaign_id, status):
            calls["set"] += 1
            return {"success": True}

        forbidden = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("account-only execute must not query product whitelists")
        )
        namespace = self.namespace(
            [pause_item, copy_item],
            lambda token, campaign_id, fields: {
                "account_id": "1", "effective_status": "ACTIVE",
            },
            graph_set,
            product="",
            product_whitelist=forbidden,
            account_whitelists=forbidden,
        )
        result = namespace["execute_ad_control_live"]({
            "preview_id": "p1",
            "preview_hash": "hash",
            "dry_run": False,
            "confirm": "EXECUTE_LIVE_RULE_GROUP",
        }, {"user_id": "runner"})
        by_key = {item["object_key"]: item for item in result["results"]}
        self.assertEqual("success", by_key[pause_item["object_key"]]["status"])
        self.assertEqual(
            "copy_persistence_not_configured",
            by_key[copy_item["object_key"]]["reason"],
        )
        self.assertEqual(1, calls["set"])


if __name__ == "__main__":
    unittest.main()
