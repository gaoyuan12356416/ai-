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
        self.db.execute("INSERT INTO fb_auto_run(id,template_id,slot_key,trigger_type,total_pages,planned_publish_at_utc,created_at_utc) VALUES(?,?,?,?,?,?,?)",
                        (run_id, 1, f"slot{run_id}", trigger, pages, planned, "2026-09-05T16:00:00Z"))

    def task(self, task_id=1, run_id=1, status="published", completed="2026-09-07T02:00:00Z",
             ledger_status="published", unknown=0, ledger_unknown=0, graph_id=None, page_id="page1"):
        post_id = str(graph_id or f"post{task_id}")
        self.db.execute("INSERT INTO fb_auto_task(id,run_id,page_id,status,graph_post_id,unknown_outcome,completed_at_utc,planned_publish_at_utc,skip_reason,error_code) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (task_id, run_id, page_id, status, post_id, unknown, completed, "", "", ""))
        if ledger_status is not None:
            self.db.execute("INSERT INTO fb_auto_publish_ledger VALUES(?,?,?,?,?)",
                            (task_id, page_id, ledger_status, post_id, ledger_unknown))

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
        self.assertEqual((row["expected"], row["blocked"], row["failed"]), (4, 4, 0))
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

    def frequency_schema(self):
        self.db.executescript("""
          ALTER TABLE fb_auto_run ADD COLUMN config_json TEXT;
          ALTER TABLE fb_auto_run ADD COLUMN template_version INTEGER;
          CREATE TABLE fb_auto_run_page(run_id INTEGER,page_id TEXT);
          CREATE TABLE fb_auto_template_version(template_id INTEGER,version INTEGER,config_json TEXT);
        """)

    def test_frozen_page_frequency_reconciles_and_excludes_shadowed_unknown_skips(self):
        from features.post_daily_report.report import validate_channel, build_card, build_compact_card
        self.frequency_schema()
        limits = {"1000": 0, "1002": 2, "1004": 4, "1005": 5}
        times = ["09:00", "12:00", "15:00", "18:00", "21:00"]
        config = {"schedule": {"mode": "fixed", "times": times}, "default_daily_count": 0,
                  "page_daily_limits": [{"page_id": p, "daily_count": n} for p, n in limits.items()]}
        # Current/template-table settings deliberately disagree with frozen runs.
        self.db.execute("INSERT INTO fb_auto_template_version VALUES(1,4,'{}')")
        for index, minute in enumerate(times, 1):
            self.run_row(run_id=index, pages=4, planned=f"2026-09-07T{int(minute[:2])-8:02d}:00:00Z")
            slot = f"auto:v4:2026-09-07:{minute}"
            self.db.execute("UPDATE fb_auto_run SET slot_key=?,config_json=?,template_version=4 WHERE id=?",
                            (slot, json.dumps(config), index))
            for offset, page in enumerate(limits):
                task_id = (index-1)*4 + offset + 1
                self.db.execute("INSERT INTO fb_auto_run_page VALUES(?,?)", (index,page))
                # Independent fixed fixture: publisher's frozen two-slot plan for
                # Page 1002 is 09:00/18:00; Page 1005 uses all five slots.
                allowed = page == "1005" or (page == "1002" and minute in {"09:00", "18:00"})
                if page == "1004":
                    status, code = "skipped", "fb_auto_page_unknown_block"
                elif not allowed:
                    status, code = "skipped", "fb_auto_page_frequency_limit"
                else:
                    status, code = "published", ""
                self.task(task_id=task_id, run_id=index, page_id=page, status=status, ledger_status=status)
                self.db.execute("UPDATE fb_auto_task SET skip_reason=? WHERE id=?", (code,task_id))
        result = self.result()
        row = result["rows"][0]
        self.assertEqual((row["raw_targets"],row["expected"],row["published"],row["blocked"],row["frequency_excluded"]), (20,11,7,4,9))
        self.assertEqual((row["failed"],row["unknown"],row["policy_skipped"]),(0,0,0))
        validate_channel(result)
        report = {"date":self.window.date,"channels":[result]}
        for card in (build_card(report),build_compact_card(report)):
            text = json.dumps(card,ensure_ascii=False)
            self.assertIn("确认成功 7",text)
            self.assertIn("明确失败 0",text)
            self.assertNotIn("未完成",text)

    def test_missing_frozen_frequency_scope_stays_unknown(self):
        self.frequency_schema()
        self.run_row(pages=2)
        config={"schedule":{"mode":"fixed","times":["09:00"]},"default_daily_count":1}
        self.db.execute("UPDATE fb_auto_run SET config_json=?,slot_key='auto:v4:2026-09-07:09:00'",(json.dumps(config),))
        self.task()
        row=self.row()
        self.assertIsNone(row["expected"])
        self.assertEqual(row["published"],1)

    def test_random_frequency_requires_matching_frozen_version_plan(self):
        self.frequency_schema()
        self.db.execute("CREATE TABLE fb_auto_schedule_plan(template_id INTEGER,template_version INTEGER,local_date TEXT,times_json TEXT)")
        config={'schedule':{'mode':'random','daily_count':3},'default_daily_count':1}
        times=['09:00','15:00','21:00']
        for run_id,minute in enumerate(times,1):
            self.run_row(run_id=run_id,planned=f"2026-09-07T{int(minute[:2])-8:02d}:00:00Z")
            self.db.execute("UPDATE fb_auto_run SET config_json=?,template_version=4,slot_key=? WHERE id=?",(json.dumps(config),'auto:v4:2026-09-07:'+minute,run_id))
            self.db.execute("INSERT INTO fb_auto_run_page VALUES(?,'1002')",(run_id,))
            self.task(task_id=run_id,run_id=run_id,status='skipped',ledger_status=None,page_id='1002')
            self.db.execute("UPDATE fb_auto_task SET skip_reason='fb_auto_page_unknown_block' WHERE id=?",(run_id,))
        self.assertIsNone(self.row()['expected'])
        self.db.execute("INSERT INTO fb_auto_schedule_plan VALUES(1,3,'2026-09-07',?)",(json.dumps(times),))
        self.assertIsNone(self.row()['expected'])
        self.db.execute("UPDATE fb_auto_schedule_plan SET template_version=4")
        row=self.row()
        self.assertEqual((row['expected'],row['blocked'],row['frequency_excluded']),(1,1,2))

    def test_no_run_frequency_uses_frozen_config_and_target_ids(self):
        self.frequency_schema()
        self.db.execute("ALTER TABLE fb_auto_due_slot ADD COLUMN template_version INTEGER")
        self.db.execute("CREATE TABLE fb_auto_due_target_snapshot(due_slot_id INTEGER PRIMARY KEY,expected_pages INTEGER,page_ids_json TEXT)")
        config={'schedule':{'mode':'fixed','times':['09:00','12:00','15:00','18:00','21:00']},'default_daily_count':0,
                'page_daily_limits':[{'page_id':'1002','daily_count':2}]}
        self.db.execute("INSERT INTO fb_auto_template_version VALUES(1,4,?)",(json.dumps(config),))
        self.db.execute("INSERT INTO fb_auto_due_slot VALUES(1,1,'auto:v4:2026-09-07:09:00','auto','2026-09-07T01:00:00Z',NULL,'missed','fb_auto_due_slot_too_late',4)")
        self.db.execute("INSERT INTO fb_auto_due_target_snapshot VALUES(1,2,'[\"1000\",\"1002\"]')")
        row=self.row()
        self.assertEqual((row['expected'],row['blocked'],row['failed'],row['frequency_excluded']),(1,1,0,1))
        self.db.execute("UPDATE fb_auto_due_target_snapshot SET page_ids_json='[\"1002\"]'")
        self.assertIsNone(self.row()['expected'])

    def test_failed_last_token_does_not_hide_video_fetch_failure_and_skips_are_separate(self):
        self.db.execute("CREATE TABLE fb_auto_publish_attempt(task_id INTEGER,error_code TEXT)")
        self.run_row(pages=4)
        self.task(status="failed",ledger_status="failed")
        self.db.execute("UPDATE fb_auto_task SET error_code='fb_graph_190' WHERE id=1")
        self.db.execute("INSERT INTO fb_auto_publish_attempt VALUES(1,'fb_graph_389')")
        for task_id,code in [(2,'fb_auto_drama_cooldown_at_publish'),(3,'fb_auto_task_too_late')]:
            self.task(task_id=task_id,status='skipped',ledger_status=None)
            self.db.execute("UPDATE fb_auto_task SET skip_reason=? WHERE id=?",(code,task_id))
        self.task(task_id=4,status='unknown',ledger_status='unknown',unknown=1)
        row=self.row()
        self.assertEqual((row['failed'],row['blocked'],row['policy_skipped'],row['unknown']),(1,1,1,1))
        self.assertIn('fb_graph_389',[r['code'] for r in row['reasons']])
        self.assertNotIn('fb_graph_190',[r['code'] for r in row['reasons']])
        self.assertEqual(row['policy_reasons'][0]['code'],'fb_auto_drama_cooldown_at_publish')

    def test_late_prepared_block_and_repeated_preparation_have_distinct_causes(self):
        self.db.executescript("ALTER TABLE fb_auto_task ADD COLUMN prepared_at_utc TEXT; ALTER TABLE fb_auto_task ADD COLUMN attempt_count INTEGER;")
        self.run_row(pages=2)
        for task_id in (1,2):
            self.task(task_id=task_id,status='skipped',ledger_status=None)
            self.db.execute("UPDATE fb_auto_task SET error_code='fb_auto_task_too_late' WHERE id=?",(task_id,))
        self.db.execute("UPDATE fb_auto_task SET prepared_at_utc='2026-09-06T01:00:00Z' WHERE id=1")
        self.db.execute("UPDATE fb_auto_task SET attempt_count=500 WHERE id=2")
        self.run_row(run_id=2,planned='2026-09-05T01:00:00Z')
        self.task(task_id=3,run_id=2,status='unknown',ledger_status='unknown',unknown=1,completed='2026-09-06T01:00:00Z')
        row=self.row()
        self.assertEqual(row['blocked'],2)
        self.assertEqual({r['code'] for r in row['reasons']},{'fb_report_late_unknown_block','fb_report_prepare_retries_expired'})


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
