import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import app


class AdControlV2ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "jobs.sqlite3")
        self.path_patch = mock.patch.object(app, "JOB_DB_PATH", self.db_path)
        self.path_patch.start()
        app.AD_CONTROL_DRAMA_SCHEMA_CACHE.clear()
        app.ensure_ad_control_tables()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def session(owner):
        return {"user_id": owner}

    def save_group(self, owner, group_id, **updates):
        payload = {
            "group_id": group_id,
            "name": group_id,
            "account_ids": ["act_1"],
            "object_level": "campaign",
            "run_mode": "observe",
            "rules": [{"rule_id": "copy-rule", "action": "copy"}],
            "strategy": {},
        }
        payload.update(updates)
        return app.save_ad_control_rule_group(payload, self.session(owner))

    def update_group_row(self, group_id, **values):
        with app.JOB_DB_LOCK:
            conn = app.get_job_db_connection()
            try:
                assignments = ",".join("%s=?" % key for key in values)
                conn.execute(
                    "UPDATE ad_control_rule_group SET %s WHERE group_id=?" % assignments,
                    tuple(values.values()) + (group_id,),
                )
                conn.commit()
            finally:
                conn.close()

    def group_row(self, group_id):
        with app.JOB_DB_LOCK:
            conn = app.get_job_db_connection()
            try:
                return dict(conn.execute(
                    "SELECT * FROM ad_control_rule_group WHERE group_id=?", (group_id,)
                ).fetchone())
            finally:
                conn.close()

    def mark_preview_ready(self, group_id, owner="u1"):
        group = app.fetch_ad_control_rule_group(group_id, owner_user_id=owner)
        preview_id = "preview-%s" % group_id
        preview_hash = group["current_preview_hash"]
        with app.JOB_DB_LOCK:
            conn = app.get_job_db_connection()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO ad_control_preview("
                    "preview_id,actor_user_id,criteria_json,sample_json,expires_at"
                    ") VALUES(?,?,?,?,?)",
                    (preview_id, owner, "{}", "[]", "2099-01-01 00:00:00"),
                )
                conn.execute(
                    "UPDATE ad_control_rule_group SET last_preview_id=?,last_preview_hash=? "
                    "WHERE group_id=?",
                    (preview_id, preview_hash, group_id),
                )
                conn.commit()
            finally:
                conn.close()

    def test_legacy_migration_sets_existing_rows_live(self):
        legacy_path = str(Path(self.temp_dir.name) / "legacy.sqlite3")
        conn = sqlite3.connect(legacy_path)
        conn.execute("""
            CREATE TABLE ad_control_rule_group (
              group_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', product TEXT NOT NULL DEFAULT '',
              rule_set_id TEXT NOT NULL DEFAULT '', account_group_id TEXT NOT NULL DEFAULT '',
              account_ids_json TEXT NOT NULL DEFAULT '[]', rules_json TEXT NOT NULL DEFAULT '[]',
              strategy_json TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 0,
              emergency_stopped INTEGER NOT NULL DEFAULT 0, last_preview_id TEXT NOT NULL DEFAULT '',
              last_preview_hash TEXT NOT NULL DEFAULT '', last_run_at TEXT NOT NULL DEFAULT '',
              last_result_json TEXT NOT NULL DEFAULT '{}', created_by TEXT NOT NULL DEFAULT '',
              owner_user_id TEXT NOT NULL DEFAULT '', object_level TEXT NOT NULL DEFAULT 'campaign',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              deleted INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute(
            "INSERT INTO ad_control_rule_group(group_id,name,created_by,owner_user_id) VALUES('legacy','legacy','u1','u1')"
        )
        conn.execute(
            "INSERT INTO ad_control_rule_group("
            "group_id,name,created_by,owner_user_id,enabled"
            ") VALUES('orphan','orphan','','',1)"
        )
        conn.commit()
        conn.close()
        with mock.patch.object(app, "JOB_DB_PATH", legacy_path):
            app.ensure_ad_control_tables()
            conn = app.get_job_db_connection()
            try:
                self.assertEqual("live", conn.execute(
                    "SELECT run_mode FROM ad_control_rule_group WHERE group_id='legacy'"
                ).fetchone()[0])
                orphan = conn.execute(
                    "SELECT enabled,emergency_stopped,run_mode FROM ad_control_rule_group "
                    "WHERE group_id='orphan'"
                ).fetchone()
                self.assertEqual((0, 1, "live"), tuple(orphan))
            finally:
                conn.close()

    def test_account_only_v2_group_is_never_reclassified_as_legacy(self):
        created = self.save_group("u1", "v2-account-only")
        self.assertEqual("", created["product"])
        self.assertEqual("", created["rule_set_id"])
        for _ in range(3):
            app.ensure_ad_control_tables()
            fetched = app.fetch_ad_control_rule_group(
                "v2-account-only", owner_user_id="u1"
            )
            self.assertEqual("", fetched["rule_set_id"])
            self.assertEqual("", fetched["product"])
        with app.JOB_DB_LOCK:
            conn = app.get_job_db_connection()
            try:
                legacy_count = conn.execute(
                    "SELECT COUNT(*) FROM ad_control_rule_set WHERE rule_set_id='legacy_v2-account-only'"
                ).fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(0, legacy_count)

    def test_malformed_preview_expiry_fails_closed(self):
        with app.JOB_DB_LOCK:
            conn = app.get_job_db_connection()
            try:
                conn.execute(
                    "INSERT INTO ad_control_preview("
                    "preview_id,actor_user_id,criteria_json,sample_json,expires_at"
                    ") VALUES(?,?,?,?,?)",
                    ("malformed-expiry", "u1", "{}", "[]", "not-a-timestamp"),
                )
                conn.commit()
            finally:
                conn.close()
        with self.assertRaises(app.StructuredApiError) as raised:
            app.fetch_ad_control_preview("malformed-expiry")
        self.assertEqual("preview_invalid", raised.exception.code)

    def test_owner_crud_enable_and_emergency_stop_are_isolated(self):
        own = self.save_group("u1", "own")
        other = self.save_group("u2", "other")
        self.assertFalse(own["enabled"])
        self.assertEqual("observe", own["run_mode"])
        self.assertEqual(["own"], [item["group_id"] for item in app.list_ad_control_rule_groups(owner_user_id="u1")["items"]])
        for operation in (
            lambda: app.fetch_ad_control_rule_group("other", owner_user_id="u1"),
            lambda: self.save_group("u1", "other", name="hijack"),
            lambda: app.delete_ad_control_rule_group("other", owner_user_id="u1"),
            lambda: app.set_ad_control_rule_group_enabled("other", True, owner_user_id="u1"),
            lambda: app.ad_control_emergency_stop({"scope": "rule_group", "group_id": "other"}, owner_user_id="u1"),
        ):
            with self.assertRaises(app.StructuredApiError):
                operation()

        self.update_group_row("own", enabled=1, last_preview_id="p", last_preview_hash="h")
        self.update_group_row("other", enabled=1, last_preview_id="p", last_preview_hash="h")
        stopped = app.ad_control_emergency_stop({"scope": "global"}, owner_user_id="u1")
        self.assertEqual(1, stopped["affected_count"])
        self.assertEqual(0, self.group_row("own")["enabled"])
        self.assertEqual(1, self.group_row("other")["enabled"])
        with self.assertRaises(app.StructuredApiError):
            app.ad_control_emergency_stop({}, owner_user_id="u1")

        self.update_group_row("own", emergency_stopped=0)
        self.mark_preview_ready("own")
        with mock.patch.object(app, "ad_control_validate_scope_token_access", return_value={"ok": True}):
            enabled = app.set_ad_control_rule_group_enabled("own", True, owner_user_id="u1")
        self.assertTrue(enabled["enabled"])
        app.set_ad_control_rule_group_enabled("own", False, owner_user_id="u1")
        self.update_group_row(
            "own",
            run_mode="live",
            rules_json=json.dumps([{"rule_id": "pause-rule", "action": "pause"}]),
        )
        with self.assertRaisesRegex(app.StructuredApiError, "live mode confirmation"):
            app.set_ad_control_rule_group_enabled("own", True, owner_user_id="u1")
        self.mark_preview_ready("own")
        with mock.patch.object(app, "ad_control_validate_scope_token_access", return_value={"ok": True}):
            enabled = app.set_ad_control_rule_group_enabled(
                "own", True, owner_user_id="u1", live_mode_confirm="ENABLE_LIVE_MODE"
            )
        self.assertTrue(enabled["enabled"])

        ad_group = self.save_group("u1", "ad-group", object_level="ad")
        self.assertFalse(ad_group["enabled"])
        ad_group = self.save_group(
            "u1", "ad-group", object_level="ad", enabled=True,
        )
        self.assertFalse(ad_group["enabled"])
        self.update_group_row(ad_group["group_id"], last_preview_id="p", last_preview_hash="h")
        with self.assertRaisesRegex(app.StructuredApiError, "Ad copy phase"):
            app.set_ad_control_rule_group_enabled("ad-group", True, owner_user_id="u1")

    def test_emergency_stop_wins_enable_race_for_group_and_global_scope(self):
        for scope in ("rule_group", "global"):
            with self.subTest(scope=scope):
                group_id = "race-%s" % scope
                self.save_group("u1", group_id)
                self.mark_preview_ready(group_id)

                def stop_during_token_validation(_scope):
                    payload = {"scope": scope}
                    if scope == "rule_group":
                        payload["group_id"] = group_id
                    app.ad_control_emergency_stop(payload, owner_user_id="u1")
                    return {"ok": True}

                with mock.patch.object(
                    app,
                    "ad_control_validate_scope_token_access",
                    side_effect=stop_during_token_validation,
                ):
                    with self.assertRaises(app.StructuredApiError) as raised:
                        app.set_ad_control_rule_group_enabled(
                            group_id, True, owner_user_id="u1"
                        )
                self.assertEqual("emergency_stop_changed", raised.exception.code)
                row = self.group_row(group_id)
                self.assertEqual(0, row["enabled"])
                self.assertEqual(1, row["emergency_stopped"])

    def test_existing_emergency_stop_can_only_be_cleared_without_a_new_stop(self):
        self.save_group("u1", "resume-stopped")
        self.mark_preview_ready("resume-stopped")
        app.ad_control_emergency_stop(
            {"scope": "rule_group", "group_id": "resume-stopped"},
            owner_user_id="u1",
        )
        with mock.patch.object(
            app, "ad_control_validate_scope_token_access", return_value={"ok": True}
        ):
            resumed = app.set_ad_control_rule_group_enabled(
                "resume-stopped", True, owner_user_id="u1"
            )
        self.assertTrue(resumed["enabled"])
        self.assertFalse(resumed["emergency_stopped"])

    def test_legacy_config_resources_are_owner_isolated(self):
        rule_payload = {
            "rule_id": "legacy-rule",
            "name": "legacy-rule",
            "enabled": False,
            "criteria": {"product": "p", "action": "pause", "level": "campaign"},
        }
        app.save_ad_control_rule(rule_payload, self.session("u1"))
        self.assertEqual(
            ["legacy-rule"],
            [item["rule_id"] for item in app.list_ad_control_rules(owner_user_id="u1")["items"]],
        )
        self.assertEqual([], app.list_ad_control_rules(owner_user_id="u2")["items"])
        with self.assertRaises(app.StructuredApiError):
            app.save_ad_control_rule(rule_payload, self.session("u2"))
        with self.assertRaises(app.StructuredApiError):
            app.set_ad_control_rule_enabled("legacy-rule", True, owner_user_id="u2")

        account_group_payload = {
            "group_id": "legacy-account-group", "name": "legacy-account-group",
            "product": "p", "account_ids": ["1"],
        }
        app.save_ad_control_account_group(account_group_payload, self.session("u1"))
        self.assertEqual(
            ["legacy-account-group"],
            [item["group_id"] for item in app.list_ad_control_account_groups(owner_user_id="u1")["items"]],
        )
        self.assertEqual([], app.list_ad_control_account_groups(owner_user_id="u2")["items"])
        with self.assertRaises(app.StructuredApiError):
            app.save_ad_control_account_group(account_group_payload, self.session("u2"))
        with self.assertRaises(app.StructuredApiError):
            app.delete_ad_control_account_group("legacy-account-group", owner_user_id="u2")

        rule_set_payload = {
            "rule_set_id": "legacy-rule-set", "name": "legacy-rule-set", "product": "p",
            "rules": [{"rule_id": "pause-rule", "action": "pause"}],
        }
        app.save_ad_control_rule_set(rule_set_payload, self.session("u1"))
        self.assertEqual(
            ["legacy-rule-set"],
            [item["rule_set_id"] for item in app.list_ad_control_rule_sets(owner_user_id="u1")["items"]],
        )
        self.assertEqual([], app.list_ad_control_rule_sets(owner_user_id="u2")["items"])
        with self.assertRaises(app.StructuredApiError):
            app.fetch_ad_control_rule_set("legacy-rule-set", owner_user_id="u2")
        with self.assertRaises(app.StructuredApiError):
            app.save_ad_control_rule_set(rule_set_payload, self.session("u2"))
        with self.assertRaises(app.StructuredApiError):
            app.delete_ad_control_rule_set("legacy-rule-set", owner_user_id="u2")

    def test_behavior_change_disables_group_and_invalidates_previous_preview(self):
        direct = self.save_group("u1", "direct-enable-bypass", enabled=True)
        self.assertFalse(direct["enabled"])
        self.update_group_row("direct-enable-bypass", run_mode="live")
        direct = self.save_group(
            "u1", "direct-enable-bypass", run_mode="live", enabled=True,
        )
        self.assertFalse(direct["enabled"])
        self.assertEqual("missing", direct["preview_status"])

        self.save_group("u1", "change-safe")
        self.mark_preview_ready("change-safe")
        with mock.patch.object(app, "ad_control_validate_scope_token_access", return_value={"ok": True}):
            enabled = app.set_ad_control_rule_group_enabled(
                "change-safe", True, owner_user_id="u1"
            )
        self.assertTrue(enabled["enabled"])
        changed = self.save_group(
            "u1", "change-safe",
            rules=[{
                "rule_id": "copy-rule", "action": "copy",
                "conditions": [{"field": "spend", "op": "gte", "value": 10}],
            }],
        )
        self.assertFalse(changed["enabled"])
        self.assertEqual("", changed["last_preview_id"])
        self.assertEqual("missing", changed["preview_status"])

        round_trip = self.save_group(
            "u1", "round-trip",
            schedule={"type": "fixed_time", "time": "10:00", "timezone": "account"},
            limits={"per_rule_daily": 1, "per_user_daily": 2},
            candidate_selection={"mode": "top_n_per_account", "top_n": 1},
        )
        self.assertEqual("10:00", round_trip["strategy"]["schedule"]["time"])
        self.assertEqual(2, round_trip["strategy"]["limits"]["per_user_daily"])
        self.save_group("u2", "foreign-tombstone")
        app.delete_ad_control_rule_group("foreign-tombstone", owner_user_id="u2")
        with self.assertRaises(app.StructuredApiError):
            self.save_group("u1", "foreign-tombstone")
        self.assertEqual("u2", self.group_row("foreign-tombstone")["owner_user_id"])

    def test_legacy_fanout_migration_is_atomic_and_owner_bounded(self):
        for index, group_id in enumerate(("old-a", "old-b"), start=1):
            self.save_group(
                "u1", group_id, account_ids=[str(index)],
                strategy={
                    "frontend_rule_group_id": "new-group",
                    "selected_account_ids": [str(index)],
                    "account_count": 1,
                },
            )
            self.update_group_row(group_id, enabled=1)
        migrated = self.save_group(
            "u1", "new-group",
            migrate_from_group_ids=["old-a", "old-b"],
            strategy={"frontend_rule_group_id": "new-group"},
        )
        self.assertFalse(migrated["enabled"])
        self.assertEqual("observe", migrated["run_mode"])
        self.assertEqual(1, self.group_row("old-a")["deleted"])
        self.assertEqual(0, self.group_row("old-a")["enabled"])

        self.save_group("u2", "foreign", strategy={"frontend_rule_group_id": "blocked-new"})
        with self.assertRaises(app.StructuredApiError):
            self.save_group(
                "u1", "blocked-new", migrate_from_group_ids=["foreign"],
                strategy={"frontend_rule_group_id": "blocked-new"},
            )
        with self.assertRaises(app.StructuredApiError):
            app.fetch_ad_control_rule_group("blocked-new", owner_user_id="u1")

        self.save_group("u1", "rollback-old", strategy={"frontend_rule_group_id": "rollback-new"})
        self.update_group_row("rollback-old", enabled=1)
        with app.JOB_DB_LOCK:
            conn = app.get_job_db_connection()
            try:
                conn.execute("""
                    CREATE TRIGGER abort_group_migration BEFORE UPDATE OF deleted ON ad_control_rule_group
                    WHEN OLD.group_id='rollback-old' AND NEW.deleted=1
                    BEGIN SELECT RAISE(ABORT, 'forced migration rollback'); END
                """)
                conn.commit()
            finally:
                conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.save_group(
                "u1", "rollback-new", migrate_from_group_ids=["rollback-old"],
                strategy={"frontend_rule_group_id": "rollback-new"},
            )
        self.assertEqual(0, self.group_row("rollback-old")["deleted"])
        self.assertEqual(1, self.group_row("rollback-old")["enabled"])
        with self.assertRaises(app.StructuredApiError):
            app.fetch_ad_control_rule_group("rollback-new", owner_user_id="u1")

    def test_divergent_fanout_configuration_fails_before_transaction(self):
        for group_id in ("split-a", "split-b"):
            self.save_group(
                "u1", group_id,
                strategy={"frontend_rule_group_id": "split-new"},
            )
            self.update_group_row(group_id, enabled=1)
        self.update_group_row(
            "split-b",
            rules_json=json.dumps([{
                "rule_id": "copy-rule", "action": "copy",
                "copy": {"budget": {"mode": "source_budget_ratio", "ratio": 0.5}},
            }]),
        )
        with self.assertRaises(app.StructuredApiError) as raised:
            self.save_group(
                "u1", "split-new",
                migrate_from_group_ids=["split-a", "split-b"],
                strategy={"frontend_rule_group_id": "split-new"},
            )
        self.assertEqual("fanout_source_config_mismatch", raised.exception.code)
        for group_id in ("split-a", "split-b"):
            row = self.group_row(group_id)
            self.assertEqual(0, row["deleted"])
            self.assertEqual(1, row["enabled"])
        with self.assertRaises(app.StructuredApiError):
            app.fetch_ad_control_rule_group("split-new", owner_user_id="u1")

    def test_fanout_revalidates_concurrent_source_change_in_final_transaction(self):
        for group_id in ("race-a", "race-b"):
            self.save_group(
                "u1", group_id,
                strategy={"frontend_rule_group_id": "race-new"},
            )
            self.update_group_row(group_id, enabled=1)

        original_normalize = app.ad_control_copy_service.normalize_rule_group
        writer_errors = []

        def normalize_after_concurrent_change(payload, actor, existing=None):
            normalized = original_normalize(payload, actor, existing=existing)

            def change_source():
                try:
                    self.update_group_row(
                        "race-b",
                        rules_json=json.dumps([{
                            "rule_id": "pause-rule", "action": "pause",
                            "conditions": [{"field": "spend", "op": ">=", "value": 50}],
                        }]),
                    )
                except Exception as exc:
                    writer_errors.append(exc)

            writer = threading.Thread(target=change_source)
            writer.start()
            writer.join(timeout=5)
            if writer.is_alive():
                self.fail("concurrent source writer did not finish")
            if writer_errors:
                raise writer_errors[0]
            return normalized

        with mock.patch.object(
            app.ad_control_copy_service,
            "normalize_rule_group",
            side_effect=normalize_after_concurrent_change,
        ):
            with self.assertRaises(app.StructuredApiError) as raised:
                self.save_group(
                    "u1", "race-new",
                    migrate_from_group_ids=["race-a", "race-b"],
                    strategy={"frontend_rule_group_id": "race-new"},
                )
        self.assertEqual("fanout_source_config_mismatch", raised.exception.code)
        self.assertEqual("pause", json.loads(self.group_row("race-b")["rules_json"])[0]["action"])
        for group_id in ("race-a", "race-b"):
            row = self.group_row(group_id)
            self.assertEqual(0, row["deleted"])
            self.assertEqual(1, row["enabled"])
        with self.assertRaises(app.StructuredApiError):
            app.fetch_ad_control_rule_group("race-new", owner_user_id="u1")

    def insert_action(
        self, action_id, actor, group_id="", created_at="2026-07-15 00:00:00",
        criteria_extra=None,
    ):
        criteria = {"rule_group_id": group_id, "binding_id": group_id} if group_id else {}
        criteria.update(criteria_extra or {})
        with app.JOB_DB_LOCK:
            conn = app.get_job_db_connection()
            try:
                conn.execute("""
                    INSERT INTO ad_control_action(
                      action_id,actor_user_id,criteria_json,results_json,created_at
                    ) VALUES(?,?,?,?,?)
                """, (action_id, actor, json.dumps(criteria), "[]", created_at))
                conn.commit()
            finally:
                conn.close()

    def test_action_log_owner_filter_paginates_and_keeps_deleted_history(self):
        self.save_group("u1", "own")
        self.save_group("u2", "other")
        for index in range(250):
            self.insert_action("foreign-%03d" % index, "ad_control_rule_runner", "other", "2026-07-15 12:%02d:00" % (index % 60))
        for index in range(3):
            self.insert_action("own-%d" % index, "ad_control_rule_runner", "own", "2026-07-14 01:0%d:00" % index)
        self.insert_action("legacy-own", "u1")
        self.insert_action("legacy-other", "u2")
        items = app.list_ad_control_actions(limit=10, owner_user_id="u1")["items"]
        ids = {item["action_id"] for item in items}
        self.assertTrue({"own-0", "own-1", "own-2", "legacy-own"}.issubset(ids))
        self.assertNotIn("legacy-other", ids)
        self.assertFalse(any(action_id.startswith("foreign-") for action_id in ids))
        app.delete_ad_control_rule_group("own", owner_user_id="u1")
        history = app.list_ad_control_actions(limit=10, binding_id="own", owner_user_id="u1")["items"]
        self.assertEqual(3, len(history))
        with self.assertRaises(app.StructuredApiError):
            app.list_ad_control_actions(binding_id="other", owner_user_id="u1")

    def test_action_target_details_require_and_honor_owner(self):
        self.save_group("u1", "own")
        self.save_group("u2", "other")
        self.insert_action("own-targets", "ad_control_rule_runner", "own")
        self.insert_action("other-targets", "ad_control_rule_runner", "other")

        payload = app.get_ad_control_action_targets(
            "own-targets", owner_user_id="u1"
        )
        self.assertEqual("own-targets", payload["action_id"])

        with self.assertRaises(app.StructuredApiError) as foreign:
            app.get_ad_control_action_targets(
                "other-targets", owner_user_id="u1"
            )
        self.assertEqual("action_not_found", foreign.exception.code)

        with self.assertRaises(app.StructuredApiError) as missing:
            app.get_ad_control_action_targets("own-targets")
        self.assertEqual("missing_owner", missing.exception.code)

    def test_observe_log_is_not_labeled_as_dry_run(self):
        self.save_group("u1", "observe-log")
        self.insert_action(
            "observe-action",
            "ad_control_rule_runner",
            "observe-log",
            criteria_extra={"run_mode": "observe"},
        )
        item = app.list_ad_control_actions(
            limit=10, owner_user_id="u1"
        )["items"][0]
        self.assertEqual("observe", item["audit"]["mode"])
        self.assertEqual("只观察", item["audit"]["mode_label"])
        self.assertEqual("观察完成", item["audit"]["status"]["label"])
        target_payload = app.get_ad_control_action_targets(
            "observe-action", owner_user_id="u1"
        )
        self.assertEqual("observe", target_payload["audit"]["mode"])
        self.assertEqual("只观察", target_payload["audit"]["mode_label"])
        self.assertEqual("观察完成", target_payload["audit"]["status"]["label"])

    def test_save_cannot_enable_legacy_rule_group(self):
        payload = {
            "group_id": "legacy-save-gate",
            "name": "legacy-save-gate",
            "product": "dramawave",
            "account_ids": ["act_1"],
            "rules": [{"rule_id": "pause-rule", "action": "pause"}],
            "enabled": True,
        }
        created = app.save_ad_control_rule_group(payload, self.session("u1"))
        self.assertFalse(created["enabled"])

        self.update_group_row("legacy-save-gate", enabled=1)
        unchanged = app.save_ad_control_rule_group(payload, self.session("u1"))
        self.assertTrue(unchanged["enabled"])

        changed_payload = dict(payload, account_ids=["act_1", "act_2"])
        changed = app.save_ad_control_rule_group(
            changed_payload, self.session("u1")
        )
        self.assertFalse(changed["enabled"])

    def test_legacy_account_pool_owner_is_enforced_without_breaking_service_links(self):
        app.save_ad_control_account_group(
            {
                "group_id": "pool-u2",
                "name": "pool-u2",
                "product": "dramawave",
                "account_ids": ["act_200"],
            },
            self.session("u2"),
        )
        foreign_payload = {
            "group_id": "foreign-pool-link",
            "name": "foreign-pool-link",
            "product": "dramawave",
            "account_group_id": "pool-u2",
            "rules": [{"rule_id": "pause-rule", "action": "pause"}],
        }
        with self.assertRaises(app.StructuredApiError) as foreign:
            app.save_ad_control_rule_group(foreign_payload, self.session("u1"))
        self.assertEqual("account_group_not_found", foreign.exception.code)

        with app.JOB_DB_LOCK:
            conn = app.get_job_db_connection()
            try:
                conn.execute(
                    "INSERT INTO ad_control_account_group("
                    "group_id,name,product,account_ids_json,created_by"
                    ") VALUES(?,?,?,?,?)",
                    ("pool-service", "pool-service", "dramawave", '["300"]', "codex"),
                )
                conn.execute(
                    "INSERT INTO ad_control_rule_group("
                    "group_id,name,product,account_group_id,rules_json,created_by,owner_user_id"
                    ") VALUES(?,?,?,?,?,?,?)",
                    (
                        "service-link", "service-link", "dramawave", "pool-service",
                        "[]", "codex", "u1",
                    ),
                )
                conn.execute(
                    "INSERT INTO ad_control_rule_group("
                    "group_id,name,product,account_group_id,rules_json,created_by,owner_user_id"
                    ") VALUES(?,?,?,?,?,?,?)",
                    (
                        "malicious-link", "malicious-link", "dramawave", "pool-u2",
                        "[]", "u1", "u1",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

        service_link = app.fetch_ad_control_rule_group(
            "service-link", owner_user_id="u1"
        )
        self.assertEqual(["300"], service_link["account_ids"])
        malicious_link = app.fetch_ad_control_rule_group(
            "malicious-link", owner_user_id="u1"
        )
        self.assertEqual([], malicious_link["account_ids"])

    def preview_with_items(self, group_id, owner, items, scheduled=False):
        whitelists = {"1": {"p": {
            str(item.get("campaign_id") or item.get("object_id")): {"account_time_zone": "UTC+8"}
            for item in items
        }}}
        with mock.patch.object(app, "ad_control_resource_snapshot", return_value={"over_limit": False}), \
             mock.patch.object(app, "ad_control_account_campaign_whitelists", return_value=whitelists), \
             mock.patch.object(app, "ad_control_token_config_for_accounts", return_value={"1": {"user_id": "token-user"}}), \
             mock.patch.object(app, "ad_control_collect_live_account", return_value={
                 "account_id": "1", "items": items, "errors": [], "active_count": len(items), "candidate_count": len(items)
             }):
            return app.create_ad_control_live_preview(
                {"rule_group_id": group_id, "scheduled": scheduled}, self.session(owner)
            )

    def test_account_without_campaign_whitelist_is_a_benign_zero_candidate(self):
        scope = {
            "product": "dramawave",
            "scheduled": True,
            "strategy": {
                "schedule": {
                    "type": "fixed_time",
                    "time": "12:00",
                    "timezone": "account",
                }
            },
        }
        with mock.patch.object(
            app,
            "ad_control_token_for_user_id",
            side_effect=AssertionError("empty whitelist must not read Token"),
        ), mock.patch.object(
            app,
            "ad_control_meta_active_campaigns",
            side_effect=AssertionError("empty whitelist must not call Graph"),
        ):
            result = app.ad_control_collect_live_account(
                scope, "act_1", {}, {}, "dramawave"
            )

        self.assertEqual("1", result["account_id"])
        self.assertEqual([], result["items"])
        self.assertEqual([], result["errors"])
        self.assertEqual(0, result["active_count"])
        self.assertEqual(0, result["candidate_count"])
        self.assertEqual(0, result["missing_start_count"])

    def test_observe_and_formal_copy_gates_make_zero_graph_writes(self):
        self.save_group("u1", "copy")
        item = {
            "product": "p", "account_id": "1", "campaign_id": "c1", "object_id": "c1",
            "object_key": "p:campaign:1:c1", "target_action": "copy", "target_rule_id": "copy-rule",
            "metrics": {"roas_pct": 120, "spend": 10},
        }
        preview = self.preview_with_items("copy", "u1", [item])
        with mock.patch.object(app, "ad_control_token_for_user_id", side_effect=AssertionError("token read")), \
             mock.patch.object(app, "ad_control_graph_get", side_effect=AssertionError("Graph read")), \
             mock.patch.object(app, "ad_control_graph_set_status", side_effect=AssertionError("Graph write")):
            observed = app.execute_ad_control_live({
                "preview_id": preview["preview_id"], "preview_hash": preview["preview_hash"], "dry_run": False,
            }, self.session("u1"))
        self.assertEqual("would_copy", observed["results"][0]["reason"])
        self.assertEqual("observed", observed["results"][0]["status"])
        self.assertEqual("c1", observed["results"][0]["object_id"])
        self.assertEqual("copy", observed["results"][0]["target_action"])

        pause_item = dict(item)
        pause_item.update({
            "campaign_id": "pause-observe", "object_id": "pause-observe",
            "object_key": "p:campaign:1:pause-observe", "target_action": "pause",
            "target_rule_id": "pause-rule",
        })
        self.save_group(
            "u1", "pause-observe",
            rules=[{"rule_id": "pause-rule", "action": "pause"}],
        )
        pause_preview = self.preview_with_items("pause-observe", "u1", [pause_item])
        with mock.patch.object(app, "ad_control_token_config_for_accounts", side_effect=AssertionError("token config")), \
             mock.patch.object(app, "ad_control_token_for_user_id", side_effect=AssertionError("token read")), \
             mock.patch.object(app, "ad_control_graph_get", side_effect=AssertionError("Graph read")), \
             mock.patch.object(app, "ad_control_graph_set_status", side_effect=AssertionError("Graph write")):
            pause_observed = app.execute_ad_control_live({
                "preview_id": pause_preview["preview_id"],
                "preview_hash": pause_preview["preview_hash"],
                "dry_run": False,
            }, self.session("u1"))
        self.assertEqual("would_pause", pause_observed["results"][0]["reason"])

        self.update_group_row("copy", run_mode="live", enabled=1)
        preview = self.preview_with_items("copy", "u1", [item])
        with mock.patch.object(app, "ad_control_token_for_user_id", side_effect=AssertionError("token read")), \
             mock.patch.object(app, "ad_control_graph_get", side_effect=AssertionError("Graph read")), \
             mock.patch.object(app, "ad_control_graph_set_status", side_effect=AssertionError("Graph write")):
            blocked = app.execute_ad_control_live({
                "preview_id": preview["preview_id"], "preview_hash": preview["preview_hash"],
                "dry_run": False, "confirm": "EXECUTE_LIVE_RULE_GROUP",
            }, self.session("u1"))
        self.assertEqual("copy_persistence_not_configured", blocked["results"][0]["reason"])

    def test_stale_preview_blocks_writes_and_mixed_copy_does_not_block_pause(self):
        pause_rule = [{"rule_id": "pause-rule", "action": "pause"}]
        pause_item = {
            "product": "p", "account_id": "1", "campaign_id": "pause-campaign",
            "object_id": "pause-campaign", "object_key": "p:campaign:1:pause-campaign",
            "target_action": "pause", "target_rule_id": "pause-rule", "token_user_id": "token-user",
            "metrics": {"roas_pct": 120, "spend": 10},
        }
        self.save_group("u1", "stale-live", run_mode="live", rules=pause_rule)
        self.update_group_row("stale-live", run_mode="live")
        stale_preview = self.preview_with_items("stale-live", "u1", [pause_item])
        self.update_group_row("stale-live", enabled=1)
        changed = self.save_group(
            "u1", "stale-live", run_mode="observe",
            rules=[{"rule_id": "copy-rule", "action": "copy"}],
        )
        self.assertEqual("missing", changed["preview_status"])
        with mock.patch.object(app, "ad_control_token_config_for_accounts", side_effect=AssertionError("token config")), \
             mock.patch.object(app, "ad_control_token_for_user_id", side_effect=AssertionError("token read")), \
             mock.patch.object(app, "ad_control_graph_get", side_effect=AssertionError("Graph read")), \
             mock.patch.object(app, "ad_control_graph_set_status", side_effect=AssertionError("Graph write")):
            with self.assertRaisesRegex(app.StructuredApiError, "changed after preview"):
                app.execute_ad_control_live({
                    "preview_id": stale_preview["preview_id"],
                    "preview_hash": stale_preview["preview_hash"],
                    "dry_run": False,
                    "confirm": "EXECUTE_LIVE_PAUSE",
                }, self.session("u1"))

        mixed_rules = [
            {"rule_id": "pause-rule", "action": "pause"},
            {"rule_id": "copy-rule", "action": "copy"},
        ]
        copy_item = dict(pause_item)
        copy_item.update({
            "campaign_id": "copy-campaign", "object_id": "copy-campaign",
            "object_key": "p:campaign:1:copy-campaign", "target_action": "copy",
            "target_rule_id": "copy-rule",
        })
        self.save_group("u1", "mixed-live", run_mode="live", rules=mixed_rules)
        self.update_group_row("mixed-live", run_mode="live")
        mixed_preview = self.preview_with_items("mixed-live", "u1", [pause_item, copy_item])
        self.update_group_row("mixed-live", enabled=1)
        with mock.patch.object(app, "ad_control_token_for_user_id", return_value="token"), \
             mock.patch.object(app, "ad_control_graph_get", return_value={
                 "account_id": "1", "effective_status": "ACTIVE", "status": "ACTIVE"
             }), \
             mock.patch.object(app, "ad_control_graph_set_status", return_value={"success": True}), \
             mock.patch.object(app, "ad_control_update_business_status"), \
             mock.patch.object(app, "ad_control_save_object_state"):
            mixed_result = app.execute_ad_control_live({
                "preview_id": mixed_preview["preview_id"],
                "preview_hash": mixed_preview["preview_hash"],
                "dry_run": False,
                "confirm": "EXECUTE_LIVE_RULE_GROUP",
            }, self.session("u1"))
        result_by_key = {item["object_key"]: item for item in mixed_result["results"]}
        self.assertEqual("success", result_by_key[pause_item["object_key"]]["status"])
        self.assertNotIn(copy_item["object_key"], result_by_key)
        self.assertEqual(0, mixed_result["blocked_count"])
        self.assertEqual(1, mixed_result["remaining_count"])

    def test_live_mixed_preview_reserves_the_pause_batch_before_copy(self):
        mixed_rules = [
            {"rule_id": "pause-rule", "action": "pause"},
            {"rule_id": "copy-rule", "action": "copy"},
        ]
        pause_items = [
            {
                "product": "p", "account_id": "1", "campaign_id": "z%02d" % index,
                "object_id": "z%02d" % index,
                "object_key": "p:campaign:1:z%02d" % index,
                "target_action": "pause", "target_rule_id": "pause-rule",
                "token_user_id": "token-user",
            }
            for index in range(1, 22)
        ]
        copy_item = {
            "product": "p", "account_id": "1", "campaign_id": "a00",
            "object_id": "a00", "object_key": "p:campaign:1:a00",
            "target_action": "copy", "target_rule_id": "copy-rule",
        }
        self.save_group("u1", "mixed-batch", run_mode="live", rules=mixed_rules)
        self.update_group_row("mixed-batch", run_mode="live", enabled=1)

        preview = self.preview_with_items(
            "mixed-batch", "u1", pause_items + [copy_item]
        )

        self.assertEqual(21, preview["pause_count"])
        self.assertEqual(1, preview["copy_count"])
        self.assertEqual(20, preview["execution_count"])
        self.assertEqual(2, preview["execution_remaining_count"])
        self.assertTrue(preview["items"])
        self.assertTrue(all(
            item.get("target_action") == "pause" for item in preview["items"]
        ))
        self.assertNotIn(copy_item["object_key"], {
            item.get("object_key") for item in preview["items"]
        })
        graph_posts = []
        with mock.patch.object(app, "ad_control_token_for_user_id", return_value="token"), \
             mock.patch.object(app, "ad_control_graph_get", return_value={
                 "account_id": "1", "effective_status": "ACTIVE", "status": "ACTIVE"
             }), \
             mock.patch.object(app, "ad_control_graph_set_status", side_effect=lambda token, campaign_id, status: graph_posts.append(campaign_id) or {"success": True}), \
             mock.patch.object(app, "ad_control_save_object_state"), \
             mock.patch.object(app, "ad_control_persist_action_log", return_value={"ok": True}):
            result = app.execute_ad_control_live({
                "preview_id": preview["preview_id"],
                "preview_hash": preview["preview_hash"],
                "dry_run": False,
                "confirm": "EXECUTE_LIVE_RULE_GROUP",
            }, self.session("u1"))

        self.assertEqual(20, len(graph_posts))
        self.assertEqual(20, result["success_count"])
        self.assertEqual(0, result["blocked_count"])
        self.assertEqual(2, result["remaining_count"])
        self.assertTrue(all(
            item.get("target_action") != "copy" for item in result["results"]
        ))

    def test_ad_preview_short_circuits_and_top_n_is_applied(self):
        self.save_group("u1", "ad", object_level="ad")
        with mock.patch.object(app, "ad_control_resource_snapshot", side_effect=AssertionError("resource scan")), \
             mock.patch.object(app, "ad_control_account_campaign_whitelists", side_effect=AssertionError("business scan")):
            preview = app.create_ad_control_live_preview({"rule_group_id": "ad"}, self.session("u1"))
        self.assertTrue(preview["phase_not_enabled"])
        with self.assertRaisesRegex(app.StructuredApiError, "Ad copy phase"):
            app.execute_ad_control_live({
                "preview_id": preview["preview_id"],
                "preview_hash": preview["preview_hash"],
                "dry_run": True,
            }, self.session("u1"))

        self.save_group("u1", "top", rules=[{
            "rule_id": "copy-rule", "action": "copy",
            "candidate_selection": {"mode": "top_n_per_account", "top_n": 1},
        }])
        items = [
            {"product": "p", "account_id": "1", "campaign_id": key, "object_id": key,
             "object_key": key, "target_action": "copy", "target_rule_id": "copy-rule",
             "metrics": {"roas_pct": roas, "spend": 10}}
            for key, roas in (("low", 10), ("high", 100))
        ]
        preview = self.preview_with_items("top", "u1", items)
        self.assertEqual(1, preview["copy_count"])
        self.assertEqual(1, preview["outside_top_n_count"])
        self.assertEqual("outside_top_n", preview["observations"][0]["candidate_selection_reason"])

    def test_duplicate_product_campaign_and_drama_schema_probe_fail_closed(self):
        self.save_group("u1", "dup")
        duplicate = {"1": {"p1": {"same": {}}, "p2": {"same": {}}}}
        with mock.patch.object(app, "ad_control_resource_snapshot", return_value={"over_limit": False}), \
             mock.patch.object(app, "ad_control_account_campaign_whitelists", return_value=duplicate), \
             mock.patch.object(app, "ad_control_collect_live_account", side_effect=AssertionError("ambiguous campaign scanned")):
            preview = app.create_ad_control_live_preview({"rule_group_id": "dup"}, self.session("u1"))
        self.assertEqual("ambiguous_source_product", preview["errors"][0]["reason"])

        app.AD_CONTROL_DRAMA_SCHEMA_CACHE.clear()
        probes = []
        with mock.patch.object(app, "mysql_table_columns", side_effect=lambda table, database: probes.append(table) or {
            "created_data_id", "series_code", "content_id", "published_at"
        }), mock.patch.object(app, "run_mysql", return_value=[]):
            app.ad_control_campaign_drama_context(["kunlunads_dev:1"])
            app.ad_control_campaign_drama_context(["kunlunads_dev:2"])
        self.assertEqual(2, len(probes))

    def test_account_schedule_wrapper_uses_given_timezone_without_server_fallback(self):
        strategy = {"schedule": {"timezone": "account", "type": "fixed_time", "time": "10:00"}}
        self.assertEqual((True, ""), app.ad_control_account_schedule_due(
            strategy, "UTC+8", datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
        ))
        self.assertEqual("unknown_account_timezone", app.ad_control_account_schedule_due(
            strategy, "", datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
        )[1])
        for operator, expected in ((">=", True), ("<=", True), (">", True), ("<", False), ("==", True), ("!=", False)):
            with self.subTest(operator=operator):
                self.assertEqual(expected, app.ad_control_match_condition(
                    {"metrics": {"spend": 10}},
                    {"field": "spend", "operator": operator, "value": 10 if operator in (">=", "<=", "==", "!=") else 9},
                ))


if __name__ == "__main__":
    unittest.main()
