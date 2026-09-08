import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from features.post_daily_report.common import Window
from features.post_daily_report.x import collect


PRIMARY_SCHEMA = """
CREATE TABLE x_post_schedule_random_plan(source_type TEXT,run_date TEXT,account_ids_json TEXT,publish_times_json TEXT);
CREATE TABLE x_post_schedule_run(id INTEGER PRIMARY KEY,source_type TEXT,run_date TEXT,publish_time TEXT,account_ids_json TEXT,status TEXT,error_code TEXT,schedule_mode TEXT);
CREATE TABLE x_post_manual_run(id INTEGER PRIMARY KEY,trigger_source TEXT,run_date TEXT,created_at TEXT,publish_mode TEXT,scheduled_at TEXT,account_ids_json TEXT,expected_count INTEGER,status TEXT,error_code TEXT);
CREATE TABLE x_post_queue(id INTEGER PRIMARY KEY,account_id INTEGER,run_date TEXT,source_type TEXT,status TEXT,delivery_mode TEXT,schedule_run_id INTEGER,manual_run_id INTEGER,run_id INTEGER,catchup_run_id INTEGER);
CREATE TABLE x_post_publish_log(id INTEGER PRIMARY KEY,queue_id INTEGER,status TEXT,x_post_id TEXT,published_at TEXT,unknown_outcome INTEGER DEFAULT 0,error_code TEXT);
CREATE TABLE x_post_repost_ledger(queue_id INTEGER,status TEXT,source_post_id TEXT,reposted_at TEXT,unknown_outcome INTEGER DEFAULT 0,error_code TEXT);
CREATE TABLE x_post_schedule_config_audit(source_type TEXT,config_version INTEGER,snapshot_json TEXT,created_at TEXT);
"""
AUTO_SCHEMA = """
CREATE TABLE x_auto_run(id INTEGER PRIMARY KEY,template_id INTEGER,template_version INTEGER,trigger_type TEXT,shanghai_date TEXT,publish_time TEXT,status TEXT,error_code TEXT);
CREATE TABLE x_auto_template_version(template_id INTEGER,version INTEGER,config_json TEXT);
CREATE TABLE x_auto_template(id INTEGER PRIMARY KEY,enabled INTEGER,created_at TEXT);
CREATE TABLE x_auto_event(event_type TEXT,details_json TEXT,created_at TEXT);
CREATE TABLE x_auto_task(id INTEGER PRIMARY KEY,run_id INTEGER,account_id TEXT,status TEXT,execution_queue_id INTEGER,publish_id TEXT,published_at_utc TEXT,unknown_outcome INTEGER DEFAULT 0,error_code TEXT);
CREATE TABLE x_auto_random_plan(template_id INTEGER,template_version INTEGER,shanghai_date TEXT,publish_times_json TEXT);
"""


class XCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = {key: str(Path(self.tmp.name) / (key + ".db")) for key in ("x", "x_auto")}
        self.db = sqlite3.connect(self.paths["x"])
        self.auto = sqlite3.connect(self.paths["x_auto"])
        self.addCleanup(self.db.close)
        self.addCleanup(self.auto.close)
        self.db.executescript(PRIMARY_SCHEMA)
        self.auto.executescript(AUTO_SCHEMA)
        self.window = Window.for_date("2026-09-07")
        self.plan("material", [], [])
        self.plan("drama", [], [])

    def insert(self, db, table, **values):
        columns = list(values)
        db.execute("INSERT INTO " + table + " (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", [values[k] for k in columns])

    def plan(self, source, accounts, times):
        self.db.execute("DELETE FROM x_post_schedule_random_plan WHERE source_type=?", (source,))
        self.insert(self.db, "x_post_schedule_random_plan", source_type=source, run_date=self.window.date, account_ids_json=json.dumps(accounts), publish_times_json=json.dumps(times))

    def schedule_run(self, run_id=1, source="material", accounts=(1, 2), time="09:00", **changes):
        values = dict(id=run_id, source_type=source, run_date=self.window.date, publish_time=time, account_ids_json=json.dumps(accounts), status="completed", error_code="", schedule_mode="random")
        values.update(changes)
        self.insert(self.db, "x_post_schedule_run", **values)

    def queue(self, queue_id, account, run_id=1, status="published", stamp="2026-09-07T01:01:00Z", mode="direct", **changes):
        values = dict(id=queue_id, account_id=account, run_date=self.window.date, source_type="material", status=status, delivery_mode=mode, schedule_run_id=run_id)
        log_changes = changes.pop("log", {})
        values.update(changes)
        self.insert(self.db, "x_post_queue", **values)
        log = dict(id=queue_id, queue_id=queue_id, status=status, x_post_id=str(1000 + queue_id), published_at=stamp, unknown_outcome=0, error_code="")
        log.update(log_changes)
        self.insert(self.db, "x_post_publish_log", **log)

    def template(self, accounts=(1, 2), times=("09:00",), version=1):
        self.insert(self.auto, "x_auto_template", id=1, enabled=1, created_at="2026-09-01T00:00:00Z")
        self.insert(self.auto, "x_auto_template_version", template_id=1, version=version, config_json=json.dumps({"account_ids": accounts, "schedule": {"mode": "fixed", "times": times}}))
        self.insert(self.auto, "x_auto_event", event_type="template_enabled", details_json=json.dumps({"template_id": 1, "version": version}), created_at="2026-09-01T00:00:00Z")

    def result(self):
        self.db.commit()
        self.auto.commit()
        result = collect(self.paths, self.window)
        self.assertFalse(result["warnings"], result["warnings"])
        return result, {r["source"]: r for r in result["rows"]}

    def test_partial_capacity_uses_full_plan_accounts(self):
        self.plan("material", [1, 2, 3], ["09:00"])
        self.schedule_run(accounts=[1, 2, 3])
        self.queue(1, 1)
        _, rows = self.result()
        row = rows["material_pool"]
        self.assertEqual((row["expected"], row["published"], row["failed"]), (3, 1, 2))
        self.assertEqual(row["reasons"][0]["code"], "x_post_schedule_partial_capacity")

    def test_zero_queue_preflight_and_entirely_missing_run(self):
        self.plan("material", [1, 2], ["09:00", "11:00"])
        self.schedule_run(status="failed_preflight", error_code="x_post_pool_fifo_conflict")
        _, rows = self.result()
        row = rows["material_pool"]
        self.assertEqual((row["expected"], row["published"], row["failed"], row["pending"]), (4, 0, 2, 2))
        self.assertEqual({r["code"]: r["count"] for r in row["reasons"]}, {"x_post_pool_fifo_conflict": 2, "schedule_not_created": 2})

    def test_relay_source_is_not_target_success_and_empty_repost_id_is_allowed(self):
        self.plan("drama", [1, 2, 3], ["09:00"])
        self.schedule_run(source="drama", accounts=[1, 2, 3])
        self.queue(1, 1, status="source_published", mode="premium_relay_repost")
        self.insert(self.db, "x_post_repost_ledger", queue_id=1, status="source_published", source_post_id="1001", reposted_at="")
        self.queue(2, 2, mode="premium_relay_repost")
        self.insert(self.db, "x_post_repost_ledger", queue_id=2, status="reposted", source_post_id="1002", reposted_at="2026-09-07T01:02:00Z")
        self.queue(3, 3, log={"unknown_outcome": 1})
        _, rows = self.result()
        row = rows["drama_pool"]
        self.assertEqual((row["published"], row["pending"], row["unknown"]), (1, 1, 1))
        self.assertEqual(row["form_counts"], {"目标转发": 1})

    def test_cross_day_prior_and_next_morning_are_separate(self):
        self.plan("material", [1, 2], ["09:00"])
        self.schedule_run()
        self.queue(1, 1, stamp="2026-09-07T16:30:00Z")
        self.queue(2, 2, stamp="2026-09-08T02:30:00Z")
        self.schedule_run(run_id=2, accounts=[3], run_date="2026-09-06")
        self.queue(3, 3, run_id=2, run_date="2026-09-06")
        result, rows = self.result()
        self.assertEqual((rows["material_pool"]["published"], rows["material_pool"]["late"], rows["material_pool"]["pending"]), (0, 1, 1))
        self.assertEqual(result["prior_completed"], 1)

    def test_manual_scheduled_uses_due_date_and_auto_bridge_is_counted_once(self):
        self.insert(self.db, "x_post_manual_run", id=1, trigger_source="manual", run_date="2026-09-06", created_at="2026-09-06T00:00:00Z", publish_mode="scheduled", scheduled_at="2026-09-07T01:00:00Z", account_ids_json="[1]", expected_count=1, status="completed")
        self.queue(1, 1, run_id=None, manual_run_id=1)
        self.insert(self.db, "x_post_manual_run", id=2, trigger_source="auto_template", run_date=self.window.date, created_at="2026-09-07T01:00:00Z", publish_mode="immediate", account_ids_json="[2]", expected_count=1, status="completed")
        self.queue(2, 2, run_id=None, manual_run_id=2)
        self.template(accounts=[2])
        self.insert(self.auto, "x_auto_run", id=1, template_id=1, template_version=1, trigger_type="auto", shanghai_date=self.window.date, publish_time="09:00", status="completed")
        self.insert(self.auto, "x_auto_task", id=1, run_id=1, account_id="2", status="published", execution_queue_id=2, publish_id="1002", published_at_utc="2026-09-07T01:01:01Z")
        _, rows = self.result()
        self.assertEqual((rows["manual_scheduled"]["expected"], rows["manual_scheduled"]["published"]), (1, 1))
        self.assertEqual((rows["auto_template"]["expected"], rows["auto_template"]["published"]), (1, 1))
        self.assertEqual(sum(r["published"] for r in rows.values()), 2)
        self.assertEqual(rows["manual_immediate"]["expected"], 0)

    def test_auto_no_candidate_and_missing_run_use_template_plan(self):
        self.template(accounts=[1, 2], times=["09:00", "11:00"])
        self.insert(self.auto, "x_auto_run", id=1, template_id=1, template_version=1, trigger_type="auto", shanghai_date=self.window.date, publish_time="09:00", status="completed")
        for account in [1, 2]:
            self.insert(self.auto, "x_auto_task", id=account, run_id=1, account_id=str(account), status="no_candidate", error_code="x_auto_no_eligible_material")
        _, rows = self.result()
        row = rows["auto_template"]
        self.assertEqual((row["expected"], row["published"], row["failed"], row["pending"]), (4, 0, 2, 2))

    def test_fixed_schedule_requires_audit_and_respects_enable_interval(self):
        self.db.execute("DELETE FROM x_post_schedule_random_plan WHERE source_type='material'")
        self.schedule_run(accounts=[1])
        self.queue(1, 1)
        _, rows = self.result()
        self.assertIsNone(rows["material_pool"]["expected"])
        self.assertEqual(rows["material_pool"]["published"], 1)
        self.insert(self.db, "x_post_schedule_config_audit", source_type="material", config_version=1, snapshot_json=json.dumps({"enabled": 1, "schedule_mode": "fixed", "account_ids_json": "[1]", "publish_times_json": '["09:00","11:00"]'}), created_at="2026-09-01T00:00:00Z")
        _, rows = self.result()
        self.assertEqual((rows["material_pool"]["expected"], rows["material_pool"]["pending"]), (2, 1))

    def test_random_plan_gap_not_hidden_by_current_audit(self):
        self.db.execute("DELETE FROM x_post_schedule_random_plan WHERE source_type='material'")
        self.insert(self.db, "x_post_schedule_config_audit", source_type="material", config_version=1, snapshot_json=json.dumps({"enabled": 1, "schedule_mode": "random", "account_ids_json": "[1]", "random_daily_count": 3}), created_at="2026-09-01T00:00:00Z")
        _, rows = self.result()
        self.assertIsNone(rows["material_pool"]["expected"])

    def test_bootstrap_audit_does_not_invent_pre_observation_history(self):
        self.db.execute("DELETE FROM x_post_schedule_random_plan WHERE source_type='material'")
        self.insert(self.db, "x_post_schedule_config_audit", source_type="material", config_version=1, snapshot_json=json.dumps({"enabled": 1, "schedule_mode": "fixed", "account_ids_json": "[1]", "publish_times_json": '["19:00"]'}), created_at="2026-09-07T02:00:00Z")
        _, rows = self.result()
        self.assertIsNone(rows["material_pool"]["expected"])

    def test_compensation_child_does_not_expand_expected(self):
        self.db.execute("CREATE TABLE x_post_schedule_codefix_compensation_audit(original_schedule_run_id INTEGER,compensation_schedule_run_id INTEGER)")
        self.db.execute("INSERT INTO x_post_schedule_codefix_compensation_audit VALUES(1,2)")
        self.plan("material", [1], ["09:00"])
        self.schedule_run(accounts=[1], status="failed_preflight")
        self.schedule_run(run_id=2, accounts=[1], time="10:00")
        self.queue(1, 1, run_id=2)
        _, rows = self.result()
        self.assertEqual((rows["material_pool"]["expected"], rows["material_pool"]["published"]), (1, 1))


class XAuditMigrationTests(unittest.TestCase):
    def test_trigger_migration_captures_existing_writer_updates_and_is_idempotent(self):
        from features.x_posts.service import SCHEDULE_CONFIG_AUDIT_DDL
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE x_post_schedule_config(source_type TEXT PRIMARY KEY,enabled INTEGER,timezone TEXT,account_ids_json TEXT,publish_times_json TEXT,schedule_mode TEXT,random_daily_count INTEGER,random_effective_date TEXT,version INTEGER,updated_at TEXT)")
        db.execute("INSERT INTO x_post_schedule_config VALUES('material',1,'Asia/Shanghai','[1,2]','[\"09:00\"]','fixed',0,'',5,'2020-01-01T00:00:00Z')")
        before = datetime.now(timezone.utc).replace(microsecond=0)
        for statement in SCHEDULE_CONFIG_AUDIT_DDL:
            db.execute(statement)
        snapshot = db.execute("SELECT config_version,snapshot_json,created_at FROM x_post_schedule_config_audit").fetchone()
        self.assertEqual(snapshot[0], 5)
        self.assertEqual(json.loads(snapshot[1])["account_ids_json"], "[1,2]")
        self.assertGreaterEqual(datetime.fromisoformat(snapshot[2].replace("Z", "+00:00")), before)
        # Simulate an old running writer: it knows nothing about the audit hook.
        db.execute("UPDATE x_post_schedule_config SET version=6,account_ids_json='[1]' WHERE source_type='material'")
        for statement in SCHEDULE_CONFIG_AUDIT_DDL:
            db.execute(statement)
        snapshots = db.execute("SELECT config_version,snapshot_json FROM x_post_schedule_config_audit ORDER BY config_version").fetchall()
        self.assertEqual([r[0] for r in snapshots], [5, 6])
        self.assertEqual(json.loads(snapshots[1][1])["account_ids_json"], "[1]")
        self.assertEqual(json.loads(snapshots[0][1])["account_ids_json"], "[1,2]")

    def test_full_storage_bootstrap_creates_audit_once(self):
        from features.x_posts.service import XPostStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "post.db"
            XPostStore(path)
            XPostStore(path)
            with closing(sqlite3.connect(path)) as db:
                rows = db.execute("SELECT source_type,count(*) FROM x_post_schedule_config_audit GROUP BY source_type").fetchall()
                self.assertEqual(rows, [("drama", 1), ("material", 1)])


if __name__ == "__main__":
    unittest.main()
