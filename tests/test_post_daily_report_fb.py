import ast
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone

from features.post_daily_report.common import Window
from features.post_daily_report.fb import collect


class FBReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "fb.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.executescript("""
          CREATE TABLE fb_auto_run(id INTEGER PRIMARY KEY,template_id INTEGER,slot_key TEXT,
            trigger_type TEXT,total_pages INTEGER,planned_publish_at_utc TEXT,created_at_utc TEXT);
          CREATE TABLE fb_auto_due_slot(id INTEGER PRIMARY KEY,template_id INTEGER,slot_key TEXT,
            trigger_type TEXT,planned_publish_at_utc TEXT,run_id INTEGER,status TEXT,error_code TEXT);
          CREATE TABLE fb_auto_task(id INTEGER PRIMARY KEY,run_id INTEGER,page_id TEXT,status TEXT,
            graph_post_id TEXT,unknown_outcome INTEGER,completed_at_utc TEXT,
            planned_publish_at_utc TEXT,skip_reason TEXT,error_code TEXT);
          CREATE TABLE fb_auto_publish_ledger(task_id INTEGER PRIMARY KEY,page_id TEXT,status TEXT,
            graph_post_id TEXT,unknown_outcome INTEGER);
        """)
        self.window = Window.for_date("2026-09-07")

    def run_row(self, run_id=1, pages=1, trigger="auto", planned="2026-09-07T01:00:00Z"):
        self.db.execute("INSERT INTO fb_auto_run VALUES(?,?,?,?,?,?,?)",
                        (run_id, 1, f"slot{run_id}", trigger, pages, planned, "2026-09-05T16:00:00Z"))

    def task(self, task_id=1, run_id=1, status="published", completed="2026-09-07T02:00:00Z",
             ledger_status="published", unknown=0, ledger_unknown=0, graph_id=None):
        post_id = str(graph_id or f"post{task_id}")
        self.db.execute("INSERT INTO fb_auto_task VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (task_id, run_id, "page1", status, post_id, unknown, completed, "", "", ""))
        if ledger_status is not None:
            self.db.execute("INSERT INTO fb_auto_publish_ledger VALUES(?,?,?,?,?)",
                            (task_id, "page1", ledger_status, post_id, ledger_unknown))

    def result(self):
        self.db.commit()
        return collect({"fb": self.path}, self.window)

    def row(self, source="auto_template"):
        return next(r for r in self.result()["rows"] if r["source"] == source)

    def test_planned_date_not_advance_creation_date_and_late_separate(self):
        self.run_row(pages=3)
        self.task()
        self.task(task_id=2, completed="2026-09-07T17:00:00Z")
        self.task(task_id=3, completed="2026-09-08T03:00:00Z")
        row = self.row()
        self.assertEqual((row["expected"], row["published"], row["late"], row["unknown"]), (3, 1, 1, 1))

    def test_submitted_is_pending_unknown_is_not_success(self):
        self.run_row(pages=3)
        self.task(status="submitted", ledger_status="submitted")
        self.task(task_id=2, status="unknown", ledger_status="unknown", unknown=1)
        self.task(task_id=3, ledger_unknown=1)
        row = self.row()
        self.assertEqual((row["published"], row["pending"], row["unknown"]), (0, 1, 2))
        self.assertIn("submitted", [r["code"] for r in row["reasons"]])

    def test_success_requires_matching_ledger_identity(self):
        self.run_row(pages=2)
        self.task(ledger_status=None)
        self.task(task_id=2)
        self.db.execute("UPDATE fb_auto_publish_ledger SET graph_post_id='different' WHERE task_id=2")
        row = self.row()
        self.assertEqual((row["published"], row["unknown"]), (0, 2))

    def test_manual_and_earlier_plan_completed_yesterday(self):
        self.run_row(trigger="manual")
        self.task()
        self.run_row(run_id=2, planned="2026-09-06T01:00:00Z")
        self.task(task_id=2, run_id=2)
        result = self.result()
        manual = next(r for r in result["rows"] if r["source"] == "manual_template")
        auto = next(r for r in result["rows"] if r["source"] == "auto_template")
        self.assertEqual((manual["expected"], manual["published"], auto["expected"], result["prior_completed"]), (1, 1, 0, 1))

    def test_no_run_without_snapshot_is_unknown_expectation(self):
        self.db.execute("INSERT INTO fb_auto_due_slot VALUES(1,1,'slot','auto','2026-09-07T01:00:00Z',NULL,'missed','fb_auto_due_slot_too_late')")
        row = self.row()
        self.assertIsNone(row["expected"])
        self.assertTrue(any("预期条数未知" in warning for warning in row["warnings"]))
        self.assertEqual(row["failed"], 0)  # Never invent a Page count from a slot count.

    def test_frozen_schedule_without_due_slot_is_not_zero_expectation(self):
        self.db.execute("CREATE TABLE fb_auto_schedule_plan(template_id INTEGER,local_date TEXT,times_json TEXT)")
        self.db.execute("INSERT INTO fb_auto_schedule_plan VALUES(1,'2026-09-07','[\"09:00\"]')")
        row = self.row()
        self.assertIsNone(row["expected"])
        self.assertTrue(any("缺少时隙及运行记录" in warning for warning in row["warnings"]))

    def test_no_run_snapshot_and_known_empty_are_distinct(self):
        self.db.execute("CREATE TABLE fb_auto_due_target_snapshot(due_slot_id INTEGER PRIMARY KEY,expected_pages INTEGER)")
        self.db.execute("INSERT INTO fb_auto_due_slot VALUES(1,1,'slot','auto','2026-09-07T01:00:00Z',NULL,'missed','fb_auto_page_pool_unpublishable')")
        self.db.execute("INSERT INTO fb_auto_due_target_snapshot VALUES(1,4)")
        row = self.row()
        self.assertEqual((row["expected"], row["failed"]), (4, 4))
        self.db.execute("UPDATE fb_auto_due_target_snapshot SET expected_pages=0")
        self.assertEqual(self.row()["expected"], 0)

    def test_due_slot_linked_by_slot_does_not_double_expected(self):
        self.run_row()
        self.task()
        self.db.execute("INSERT INTO fb_auto_due_slot VALUES(1,1,'slot1','auto','2026-09-07T01:00:00Z',NULL,'pending','')")
        self.assertEqual(self.row()["expected"], 1)

    def test_duplicate_graph_id_is_counted_once_and_flagged(self):
        self.run_row(pages=2)
        self.task(graph_id="same")
        self.task(task_id=2, graph_id="same")
        row = self.row()
        self.assertEqual((row["published"], row["unknown"]), (1, 1))

    def test_missing_task_is_visible(self):
        self.run_row(pages=2)
        self.task()
        row = self.row()
        self.assertEqual((row["expected"], row["published"], row["unknown"]), (2, 1, 1))

    def test_reader_does_not_mutate_database(self):
        self.run_row()
        self.task()
        self.db.commit()
        original = self.path.read_bytes()
        self.result()
        self.assertEqual(self.path.read_bytes(), original)


class FBTargetAuditTests(unittest.TestCase):
    def test_hook_freezes_first_target_set_and_known_empty(self):
        # Load only the new method, avoiding imports of the production publisher.
        source = (Path(__file__).resolve().parents[1] / "features/fb_auto_posts/core.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FBAutoPostStore")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "snapshot_due_targets")
        method.decorator_list = []
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method], type_ignores=[])
        scope = {"json": json, "utc_iso": lambda dt: dt.isoformat()}
        exec(compile(ast.fix_missing_locations(module), "audit-hook", "exec"), scope)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "audit.sqlite3"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE fb_auto_due_target_snapshot(due_slot_id INTEGER PRIMARY KEY,template_id INTEGER,template_version INTEGER,slot_key TEXT,trigger_type TEXT,planned_publish_at_utc TEXT,expected_pages INTEGER,page_ids_json TEXT,captured_at_utc TEXT)")
            db.close()
            class Store:
                _lock = threading.RLock()
                now_fn = staticmethod(lambda: datetime.now(timezone.utc))
                @contextmanager
                def connect(self):
                    connection = sqlite3.connect(path)
                    try:
                        with connection:
                            yield connection
                    finally:
                        connection.close()
            call = scope["snapshot_due_targets"]
            args = dict(template_id=1, template_version=2, slot_key="slot", trigger_type="auto", planned_publish_at_utc="2026-09-07T01:00:00Z")
            call(Store(), due_slot_id=1, page_ids=["p2", "p1", "p1"], **args)
            call(Store(), due_slot_id=1, page_ids=["p3"], **args)
            call(Store(), due_slot_id=2, page_ids=[], **args)
            db = sqlite3.connect(path)
            rows = db.execute("SELECT expected_pages,page_ids_json FROM fb_auto_due_target_snapshot ORDER BY due_slot_id").fetchall()
            db.close()
            self.assertEqual(rows, [(2, '["p1", "p2"]'), (0, '[]')])
        create_run = ast.get_source_segment(source, next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "create_run"))
        self.assertLess(create_run.index("self.snapshot_due_targets("), create_run.index('"fb_auto_previous_run_backlog"'))
        self.assertLess(create_run.index("self.snapshot_due_targets("), create_run.index('"fb_auto_page_pool_empty"'))


if __name__ == "__main__":
    unittest.main()
