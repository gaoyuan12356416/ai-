import copy
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from features.ad_control_copy_engine import service


class RecordingIntentStore:
    def __init__(self, reserve_result=None, events=None):
        self.reserve_result = reserve_result or {"ok": True}
        self.events = events if events is not None else []

    def reserve(self, intent, limits, cooldown_days):
        self.events.append(("reserve", intent, limits, cooldown_days))
        result = dict(self.reserve_result)
        result.setdefault("intent_id", intent["intent_id"])
        return result

    def update(self, intent_id, status, result=None, reason=""):
        self.events.append(("intent", status, intent_id, result, reason))

    def add_lineage(self, intent_id, source, copied, ledger_result):
        self.events.append(("lineage", intent_id))


class RecordingMeta:
    def __init__(self, events, mapping_complete=True):
        self.events = events
        self.mapping_complete = mapping_complete

    def deep_copy_campaign(self, **kwargs):
        self.events.append(("meta_copy", kwargs))
        return {
            "campaign_id": "new-campaign",
            "status": "PAUSED",
            "mapping_complete": self.mapping_complete,
            "adsets": [{"source_adset_id": "old-set", "adset_id": "new-set"}],
            "ads": [{"source_ad_id": "old-ad", "ad_id": "new-ad", "creative_id": "new-creative"}],
        }

    def activate_campaign(self, campaign_id):
        self.events.append(("activate", campaign_id))
        return {"id": campaign_id, "status": "ACTIVE"}


class RecordingLedger:
    def __init__(self, events, ok=True):
        self.events = events
        self.ok = ok

    def write_facebook_copy(self, **kwargs):
        self.events.append(("ledger", kwargs))
        return {"ok": self.ok, "created_data_ids": [101]} if self.ok else {"ok": False}


def group(**updates):
    value = {
        "group_id": "g1",
        "owner_user_id": "u1",
        "object_level": "campaign",
        "run_mode": "live",
        "strategy": {"copy": {
            "top_n_per_account": 1,
            "daily_rule_limit": 2,
            "daily_user_limit": 3,
            "source_cooldown_days": 1,
            "budget": {"mode": "source_ratio", "ratio": "0.5", "budget_type": "daily_budget"},
        }},
    }
    value.update(updates)
    return value


def candidate(**updates):
    value = {
        "object_key": "1:campaign:old-campaign",
        "object_id": "old-campaign",
        "campaign_id": "old-campaign",
        "account_id": "act_1",
        "account_time_zone": "UTC+8",
        "budget_type": "daily_budget",
        "budget_level": "CBO",
        "source_budget": 10000,
        "source_created_rows": [{"id": 7, "campaign_id": "old-campaign", "adset_id": "old-set", "ad_id": "old-ad", "budget": 10000}],
        "roas_pct": 120,
        "spend": 50,
        "target_rule_id": "r-copy",
        "target_action": "copy",
    }
    value.update(updates)
    return value


class RuleGroupValidationTests(unittest.TestCase):
    def test_legacy_groups_are_migrated_to_live_before_new_default_applies(self):
        app_source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        migration = "if \"run_mode\" not in columns:"
        self.assertIn(migration, app_source)
        block = app_source[app_source.index(migration):app_source.index(migration) + 500]
        self.assertIn("UPDATE ad_control_rule_group SET run_mode='live'", block)
        self.assertIn("run_mode TEXT NOT NULL DEFAULT 'observe'", app_source)

    def test_new_group_is_forced_disabled_and_observe(self):
        result = service.normalize_rule_group({
            "name": "copy winners",
            "accounts": ["act_1"],
            "object_level": "campaign",
            "run_mode": "live",
            "enabled": True,
            "rules": [{"action": "copy"}],
        }, "owner-1")
        self.assertFalse(result["enabled"])
        self.assertEqual("observe", result["run_mode"])
        self.assertEqual(["1"], result["account_ids"])
        self.assertEqual("copy", result["rules"][0]["action"])

    def test_foreign_owner_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "owner_forbidden"):
            service.normalize_rule_group({
                "name": "x", "accounts": ["1"], "rules": [{"action": "pause"}],
                "owner_user_id": "someone-else",
            }, "owner-1")

    def test_live_transition_needs_exact_confirmation(self):
        existing = {"group_id": "g", "run_mode": "observe", "enabled": False}
        payload = {"group_id": "g", "name": "x", "accounts": ["1"], "rules": [{"action": "copy"}], "run_mode": "live"}
        with self.assertRaisesRegex(ValueError, "live_mode_confirm_required"):
            service.normalize_rule_group(payload, "owner-1", existing)
        payload["confirm"] = service.LIVE_CONFIRMATION
        self.assertEqual("live", service.normalize_rule_group(payload, "owner-1", existing)["run_mode"])

    def test_top_level_group_fields_round_trip_into_strategy(self):
        result = service.normalize_rule_group({
            "name": "api group",
            "accounts": ["act_1"],
            "rules": [{"action": "copy"}],
            "schedule": {"timezone": "account", "time": "10:00"},
            "limits": {"daily_rule_limit": 2},
            "candidate_selection": {"mode": "top_n_per_account", "top_n": 3},
            "strategy": {"limits": {"daily_rule_limit": 4}},
        }, "owner-1")
        self.assertEqual(4, result["strategy"]["limits"]["daily_rule_limit"])
        self.assertEqual("10:00", result["strategy"]["schedule"]["time"])
        self.assertEqual("top_n_per_account", result["strategy"]["candidate_selection"]["mode"])

    def test_invalid_copy_configuration_matrix_fails_fast(self):
        base = {
            "name": "invalid matrix", "accounts": ["1"],
            "rules": [{"rule_id": "copy", "action": "copy"}],
        }
        cases = [
            ({"schedule": {"type": "interval", "interval_minutes": 4}}, "invalid_schedule"),
            ({"limits": {"per_rule_daily": 0}}, "invalid_limits"),
            ({"limits": {"per_user_daily": 99999}}, "invalid_limits"),
            ({"candidate_selection": {"mode": "random"}}, "invalid_candidate_selection"),
            ({"candidate_selection": {"mode": "top_n_per_account", "top_n": 0}}, "invalid_candidate_selection"),
            ({"rules": [{"action": "copy", "drama_scope": {"type": "recent_days", "days": 0}}]}, "invalid_drama_scope"),
            ({"rules": [{"action": "copy", "drama_scope": {"type": "specified", "drama_ids": []}}]}, "invalid_drama_scope"),
            ({"rules": [{"action": "copy", "copy": {"budget": {"mode": "bogus", "multiplier": 1}}}]}, "invalid_copy_budget"),
            ({"rules": [{"action": "copy", "copy": {"budget": {"mode": "actual_cpi_multiplier", "multiplier": 0}}}]}, "invalid_copy_budget"),
            ({"rules": [{"action": "copy", "copy": {"budget": {"mode": "source_budget_ratio", "ratio": 11}}}]}, "invalid_copy_budget"),
            ({"rules": [{"action": "copy", "copy": {"roas_bid": {"direction": "sideways", "percent": 10}}}]}, "invalid_roas_adjustment"),
            ({"rules": [{"action": "copy", "copy": {"roas_bid": {"direction": "decrease", "percent": 101}}}]}, "invalid_roas_adjustment"),
        ]
        for updates, error_code in cases:
            payload = copy.deepcopy(base)
            payload.update(copy.deepcopy(updates))
            with self.subTest(error_code=error_code, updates=updates):
                with self.assertRaisesRegex(ValueError, error_code):
                    service.normalize_rule_group(payload, "owner-1")

    def test_canonical_drama_ids_match_specified_scope(self):
        matched, reason = service.match_drama_scope(
            {"series_code": "series-1", "content_id": "content-1"},
            {"type": "specified", "drama_ids": ["series-1"]},
        )
        self.assertTrue(matched, reason)


class RuleResolutionTests(unittest.TestCase):
    def test_pause_wins_and_copy_is_shadowed(self):
        rules = [
            {"rule_id": "copy-first", "action": "copy", "priority": 1, "conditions": []},
            {"rule_id": "pause-later", "action": "pause", "priority": 99, "conditions": []},
        ]
        result = service.evaluate_rule_actions({}, rules, lambda item, condition: True)
        self.assertEqual("pause", result["target_action"])
        self.assertEqual("pause-later", result["target_rule_id"])
        copy_match = next(item for item in result["matched_rules"] if item["rule_id"] == "copy-first")
        self.assertEqual("pause-later", copy_match["shadowed_by_rule"])


class EngineSafetyTests(unittest.TestCase):
    def make_engine(self, config, events=None, ledger_ok=True, mapping_complete=True, store=None):
        events = events if events is not None else []
        store = store or RecordingIntentStore(events=events)
        return service.CopyEngine(
            RecordingMeta(events, mapping_complete=mapping_complete),
            RecordingLedger(events, ok=ledger_ok),
            store,
            config=config,
            clock=lambda: datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc),
        ), events

    def test_observe_mode_has_zero_external_or_intent_writes(self):
        engine, events = self.make_engine(service.CopyEngineConfig(copy_enabled=True))
        result = engine.execute(group(run_mode="observe"), [candidate()])
        self.assertEqual("observed", result[0]["status"])
        self.assertEqual("would_copy", result[0]["reason"])
        self.assertEqual([], events)

    def test_default_kill_switch_has_zero_external_or_intent_writes(self):
        engine, events = self.make_engine(service.CopyEngineConfig())
        result = engine.execute(group(), [candidate()])
        self.assertEqual("copy_disabled", result[0]["reason"])
        self.assertEqual([], events)

    def test_ad_live_is_explicitly_phase_not_enabled(self):
        engine, events = self.make_engine(service.CopyEngineConfig(copy_enabled=True, ad_copy_enabled=True))
        result = engine.execute(group(object_level="ad"), [candidate()])
        self.assertEqual("phase_not_enabled", result[0]["reason"])
        self.assertEqual([], events)

    def test_campaign_is_paused_and_ledger_precedes_activation(self):
        engine, events = self.make_engine(service.CopyEngineConfig(copy_enabled=True))
        result = engine.execute(group(), [candidate()])
        self.assertEqual("success", result[0]["status"])
        copy_event = next(item for item in events if item[0] == "meta_copy")
        self.assertTrue(copy_event[1]["deep_copy"])
        self.assertEqual("PAUSED", copy_event[1]["status_option"])
        names = [item[0] for item in events]
        self.assertLess(names.index("ledger"), names.index("activate"))
        ledger_state_index = next(
            index for index, item in enumerate(events)
            if item[0] == "intent" and item[1] == "ledger_written"
        )
        self.assertLess(ledger_state_index, names.index("activate"))

    def test_incomplete_mapping_is_quarantined_without_ledger_or_activation(self):
        engine, events = self.make_engine(service.CopyEngineConfig(copy_enabled=True), mapping_complete=False)
        result = engine.execute(group(), [candidate()])
        self.assertEqual("error", result[0]["status"])
        self.assertEqual("copy_mapping_incomplete", result[0]["reason"])
        self.assertNotIn("ledger", [item[0] for item in events])
        self.assertNotIn("activate", [item[0] for item in events])
        self.assertIn("quarantined", [item[1] for item in events if item[0] == "intent"])

    def test_failed_created_data_write_never_activates(self):
        engine, events = self.make_engine(service.CopyEngineConfig(copy_enabled=True), ledger_ok=False)
        result = engine.execute(group(), [candidate()])
        self.assertEqual("created_data_write_failed", result[0]["reason"])
        self.assertNotIn("activate", [item[0] for item in events])

    def test_meta_created_intent_resumes_without_second_meta_copy(self):
        events = []
        existing_copy = {
            "campaign_id": "new-campaign", "status": "PAUSED", "mapping_complete": True,
            "adsets": [{"source_adset_id": "old-set", "adset_id": "new-set"}],
            "ads": [{"source_ad_id": "old-ad", "ad_id": "new-ad", "creative_id": "new-creative"}],
        }
        store = RecordingIntentStore(reserve_result={
            "ok": False,
            "reason": "duplicate_intent",
            "intent_id": "old-intent",
            "existing": {
                "intent_id": "old-intent", "status": "meta_created", "owner_user_id": "u1",
                "rule_group_id": "g1", "rule_id": "r-copy", "account_id": "1",
                "source_object_id": "old-campaign",
                "result": {"copied": existing_copy, "adjustments": {"campaign_budget": 5000}},
            },
        }, events=events)
        engine, events = self.make_engine(service.CopyEngineConfig(copy_enabled=True), events=events, store=store)
        result = engine.execute(group(), [candidate()])
        self.assertEqual("success", result[0]["status"])
        self.assertNotIn("meta_copy", [item[0] for item in events])
        self.assertIn("ledger", [item[0] for item in events])
        self.assertIn("activate", [item[0] for item in events])

    def test_top_n_is_deterministic_per_account(self):
        engine, events = self.make_engine(service.CopyEngineConfig(copy_enabled=True))
        results = engine.execute(group(), [
            candidate(campaign_id="low", object_id="low", object_key="low", roas_pct=20),
            candidate(campaign_id="high", object_id="high", object_key="high", roas_pct=200),
        ])
        by_campaign = {item["campaign_id"]: item for item in results}
        self.assertEqual("outside_top_n", by_campaign["low"]["reason"])
        self.assertEqual("success", by_campaign["high"]["status"])

    def test_x_cpi_requires_currency_offset_and_known_budget_type(self):
        config = service.CopyEngineConfig(copy_enabled=True)
        engine, events = self.make_engine(config)
        g = group(strategy={"copy": {"top_n_per_account": 1, "budget": {"mode": "x_cpi", "x": 100, "budget_type": "daily_budget"}}})
        result = engine.execute(g, [candidate(cpi=2, source_budget=None, currency_offset=None)])
        self.assertEqual("unknown_currency_offset", result[0]["reason"])
        self.assertEqual([], events)

    def test_each_matched_rule_has_independent_top_n_and_budget(self):
        engine, events = self.make_engine(service.CopyEngineConfig(copy_enabled=True))
        configured = group(
            rules=[
                {"rule_id": "r-all", "action": "copy", "copy": {
                    "candidate_selection": {"mode": "all"},
                    "budget": {"mode": "source_ratio", "ratio": "0.5", "budget_type": "daily_budget"},
                }},
                {"rule_id": "r-top", "action": "copy", "copy": {
                    "candidate_selection": {"mode": "top_n_per_account", "top_n": 1},
                    "budget": {"mode": "source_ratio", "ratio": "0.2", "budget_type": "daily_budget"},
                }},
            ],
            strategy={"copy": {"budget": {"mode": "source_ratio", "ratio": "1", "budget_type": "daily_budget"}}},
        )
        results = engine.execute(configured, [
            candidate(campaign_id="all", object_id="all", object_key="all", target_rule_id="r-all", source_budget=10000),
            candidate(campaign_id="top-low", object_id="top-low", object_key="top-low", target_rule_id="r-top", source_budget=20000, roas_pct=10),
            candidate(campaign_id="top-high", object_id="top-high", object_key="top-high", target_rule_id="r-top", source_budget=20000, roas_pct=200),
        ])
        by_campaign = {item["campaign_id"]: item for item in results}
        self.assertEqual("outside_top_n", by_campaign["top-low"]["reason"])
        budgets = {
            item[1]["campaign_id"]: item[1]["adjustments"]["campaign_budget"]
            for item in events if item[0] == "meta_copy"
        }
        self.assertEqual({"all": 5000, "top-high": 4000}, budgets)


class ScheduleAndSelectionTests(unittest.TestCase):
    def test_fixed_and_interval_use_real_account_timezone(self):
        fixed = {"schedule": {"timezone": "account", "type": "fixed_time", "time": "10:00"}}
        self.assertEqual((True, ""), service.schedule_due(
            fixed, "UTC+8", datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
        ))
        self.assertEqual("outside_fixed_time", service.schedule_due(
            fixed, "UTC+8", datetime(2026, 7, 15, 1, 59, tzinfo=timezone.utc)
        )[1])
        interval = {"schedule": {"timezone": "account", "type": "interval", "interval_minutes": 60}}
        self.assertTrue(service.schedule_due(
            interval, "UTC+8", datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
        )[0])
        self.assertEqual("outside_interval", service.schedule_due(
            interval, "UTC+8", datetime(2026, 7, 15, 2, 5, tzinfo=timezone.utc)
        )[1])
        self.assertEqual("unknown_account_timezone", service.schedule_due(fixed, "", datetime.now(timezone.utc))[1])

    def test_cross_midnight_execution_window_boundaries(self):
        config = {"allowed_after": "22:00", "allowed_before": "02:00"}
        for hour, minute, expected in ((21, 59, False), (22, 0, True), (1, 59, True), (2, 1, False)):
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(expected, service.inside_execution_window(
                    datetime(2026, 7, 15, hour, minute), config
                ))
        self.assertTrue(service.inside_execution_window(
            datetime(2026, 7, 15, 12, 0), {"allowed_after": "08:00", "allowed_before": "08:00"}
        ))

    def test_frontend_top_n_per_account_mode_and_stable_ties(self):
        configured = group(
            rules=[{"rule_id": "r-copy", "action": "copy", "candidate_selection": {
                "mode": "top_n_per_account", "top_n": 1,
            }}]
        )
        selected = service.apply_copy_candidate_selection(configured, [
            candidate(campaign_id="b", object_id="b", object_key="b", roas_pct=100, spend=50),
            candidate(campaign_id="a", object_id="a", object_key="a", roas_pct=100, spend=50),
        ])
        by_id = {item["campaign_id"]: item for item in selected}
        self.assertEqual("copy", by_id["a"]["target_action"])
        self.assertEqual("outside_top_n", by_id["b"]["candidate_selection_reason"])

    def test_ambiguous_campaign_product_is_removed_before_scan(self):
        clean, errors = service.deduplicate_account_product_campaigns({
            "1": {
                "p1": {"same": {}, "only-p1": {}},
                "p2": {"same": {}, "only-p2": {}},
            }
        })
        self.assertEqual("ambiguous_source_product", errors[0]["reason"])
        self.assertNotIn("same", clean["1"]["p1"])
        self.assertNotIn("same", clean["1"]["p2"])
        self.assertIn("only-p1", clean["1"]["p1"])


class FacebookAdapterPollingTests(unittest.TestCase):
    def test_copy_waits_for_completion_before_readback(self):
        calls = []
        clock = {"value": 0.0, "polls": 0}

        def transport(method, version, path, params):
            calls.append((method, path, params))
            if method == "POST" and path == "/old/copies":
                return {"copied_campaign_id": "new"}
            if method == "GET" and path == "/old/copies":
                clock["polls"] += 1
                return {"data": [{"copied_campaign_id": "new", "is_completed": clock["polls"] >= 2}]}
            if method == "GET" and path == "/new":
                return {"id": "new", "account_id": "1", "status": "PAUSED", "name": "copy", "daily_budget": 100}
            if method == "GET" and path == "/new/adsets":
                return {"data": [{"id": "new-set", "source_adset_id": "old-set", "status": "PAUSED"}]}
            if method == "GET" and path == "/new/ads":
                return {"data": [{"id": "new-ad", "source_ad_id": "old-ad", "adset_id": "new-set", "creative": {"id": "cr"}}]}
            if method == "POST" and path == "/new-set":
                return {"success": True}
            if method == "GET" and path == "/new-set":
                return {"id": "new-set", "status": "PAUSED"}
            raise AssertionError((method, path, params))

        adapter = service.FacebookCampaignCopyAdapter(
            transport, poll_interval_seconds=1, poll_timeout_seconds=5,
            monotonic=lambda: clock["value"],
            sleeper=lambda seconds: clock.__setitem__("value", clock["value"] + seconds),
        )
        result = adapter.deep_copy_campaign("act_1", "old", True, "PAUSED", {
            "budget_level": "CBO", "budget_type": "daily_budget", "campaign_budget": 100,
        })
        self.assertEqual("new", result["campaign_id"])
        self.assertEqual(2, clock["polls"])
        self.assertLess(
            [path for _, path, _ in calls].index("/old/copies", 1),
            [path for _, path, _ in calls].index("/new"),
        )

    def test_copy_poll_timeout_never_reads_or_activates_new_campaign(self):
        calls = []
        clock = {"value": 0.0}

        def transport(method, version, path, params):
            calls.append((method, path, params))
            if method == "POST":
                self.assertEqual("PAUSED", params["status_option"])
                return {"copied_campaign_id": "new"}
            if path == "/old/copies":
                return {"data": [{"copied_campaign_id": "new", "is_completed": False}]}
            raise AssertionError(path)

        adapter = service.FacebookCampaignCopyAdapter(
            transport, poll_interval_seconds=1, poll_timeout_seconds=2,
            monotonic=lambda: clock["value"],
            sleeper=lambda seconds: clock.__setitem__("value", clock["value"] + seconds),
        )
        with self.assertRaisesRegex(RuntimeError, "copy_poll_timeout"):
            adapter.deep_copy_campaign("1", "old", True, "PAUSED", {})
        self.assertNotIn("/new", [path for _, path, _ in calls])


class SQLiteIntentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "copy.sqlite3"

        def connection():
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            return conn

        self.connection = connection
        conn = connection()
        service.ensure_copy_tables(conn)
        conn.commit()
        conn.close()
        self.store = service.SQLiteCopyIntentStore(connection)

    def tearDown(self):
        self.temp_dir.cleanup()

    def intent(self, suffix="1"):
        now = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
        return {
            "intent_id": "intent-" + suffix,
            "idempotency_key": "key-" + suffix,
            "owner_user_id": "u1",
            "rule_group_id": "g1",
            "rule_id": "r1",
            "account_id": "1",
            "object_level": "campaign",
            "source_object_id": "campaign-" + suffix,
            "source_created_data_id": "source-" + suffix,
            "account_date": "2026-07-15",
            "now_utc": now,
        }

    def test_idempotency_and_rule_quota_are_atomic(self):
        limits = {"rule": 1, "user": 10, "hard": 50}
        self.assertTrue(self.store.reserve(self.intent(), limits, 0)["ok"])
        duplicate = self.store.reserve(self.intent(), limits, 0)
        self.assertEqual("duplicate_intent", duplicate["reason"])
        limited = self.store.reserve(self.intent("2"), limits, 0)
        self.assertEqual("rule_daily_limit", limited["reason"])

        third = self.intent("3")
        third["rule_id"] = "r2"
        self.assertTrue(self.store.reserve(third, limits, 0)["ok"])

    def test_source_cooldown_blocks_new_time_bucket(self):
        limits = {"rule": 10, "user": 10, "hard": 50}
        first = self.intent("1")
        self.assertTrue(self.store.reserve(first, limits, 1)["ok"])
        second = self.intent("2")
        second["source_object_id"] = first["source_object_id"]
        blocked = self.store.reserve(second, limits, 1)
        self.assertEqual("source_cooldown", blocked["reason"])


if __name__ == "__main__":
    unittest.main()
