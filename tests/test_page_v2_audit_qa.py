"""Single-use named-table safety and production batch writer routing."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from features.fb_page_assignment_v2.audit import TABLE, Workers, _check
from features.fb_page_assignment_v2.audit_qa_table import QATable, qa_name
from features.fb_page_assignment_v2.audit_sandbox import isolated_mysql_sink
from features.fb_page_assignment_v2.ledger import Store


class Server:
    def __init__(self):
        self.exists=False
        self.rows={}
        self.sql=[]
        self.server="server-qa"
        self.ddl="(id int)"
    @contextmanager
    def connection(self,role,deadline):
        yield self
    def cursor(self):
        return Cursor(self)
    def commit(self):
        self.sql.append("COMMIT")


class Cursor:
    def __init__(self,server):
        self.s = server
        self.result=[]
    def __enter__(self):
        return self
    def __exit__(self,*args):
        pass
    def fetchone(self):
        return self.result[0] if self.result else None
    def fetchall(self):
        return self.result
    def execute(self,sql,params=()):
        s=self.s
        s.sql.append(sql)
        self.result=[]
        if "information_schema.tables" in sql:
            self.result=[("BASE TABLE","InnoDB")] if s.exists else []
        elif sql.startswith("CREATE TABLE"):
            self.assert_target(sql.split(" LIKE ")[0])
            if s.exists: raise RuntimeError("already-exists")
            s.exists=True
        elif sql.startswith("SHOW CREATE TABLE"):
            self.result=[("t","CREATE TABLE t "+(s.ddl if "qa_" in sql else "(id int)"))]
        elif sql=="SELECT @@server_uuid":
            self.result=[(s.server,)]
        elif sql.startswith("SELECT diagnostics_json"):
            row=s.rows.get((params[0],0))
            self.result=[(row[10],row[12])] if row else []
        elif "information_schema.triggers" in sql:
            self.result=[(0,)]
        elif sql.startswith("SELECT 1 FROM"):
            self.result=[(1,)] if s.rows else []
        elif sql.startswith("INSERT INTO"):
            self.assert_target(sql)
            s.rows[params[0],params[1]]=params
        elif sql.startswith("SELECT task_id,item_index"):
            self.assert_target(sql)
            self.result=[(key[0],key[1],*_check(row)) for key,row in s.rows.items() if key[0] in params]
        elif sql.startswith("SELECT selection_stage"):
            self.result=[(row[7],row[8],row[1]==0,row[5] is None,1) for row in s.rows.values()
                         if row[0]!=params[0]]
        elif sql.startswith("START TRANSACTION"):
            pass
        elif sql.startswith("DROP TABLE"):
            self.assert_target(sql)
            s.rows.clear()
            s.exists=False
        else:
            raise AssertionError("unrecognized SQL")
    def executemany(self,sql,rows):
        for row in rows:
            self.execute(sql,row)
    def assert_target(self,sql):
        if TABLE in sql or "fb_page_assignment_qa_20261126" not in sql:
            raise AssertionError("unexpected target "+sql)


class NamedQA(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.instance=uuid.uuid4().hex
        (self.root/"qa-instance").write_text(self.instance)
        self.s=Server()
        self.name="fb_page_assignment_qa_20261126"
    def table(self):
        return QATable(self.root,self.name,self.s.connection)
    def test_writes_use_qa_target_and_counts_match_compacted_receipts(self):
        qa=self.table().prepare()
        qa.budget_status=lambda:{"roles":{"read":4,"write":1},"in_use":5}
        qa.foreground_busy=lambda:True
        state=Store(self.root/"state")
        state.record("native:91::qa.test","abc",62,["d"],{"snapshot":{"candidates":[["1"]]},
                   "rows":[{"drama_id":"d","page_id":"1","selection_stage":"language_profile"}]})
        Workers(state,sink=qa).flush(state.claim())
        self.assertEqual(state.status()["pending"],0)
        self.assertEqual(qa.counts(1,0), (1,1,{"candidate_snapshot":1,"v2_request":1,"language_profile":1},{}))
        self.assertEqual(qa.overlap()["peak_reads"],4)
        self.assertGreater(qa.overlap()["read_and_write_during_foreground"],0)
        self.assertEqual(len(self.s.rows),4)
        self.assertTrue(all(TABLE not in sql for sql in self.s.sql if sql.startswith(("INSERT","DROP"))))
        self.assertEqual(self.table().prepare().identity,qa.identity)
        with self.assertRaises(ValueError):
            qa.drop(uuid.uuid4().hex)
        self.assertTrue(self.s.exists)
        self.assertEqual(qa.drop(self.instance)["state"],"dropped")
        self.assertFalse(self.s.exists)
    def test_refuses_other_owner_unsafe_names_and_schema_change(self):
        for name in ("ads_ai.fb_drama_page_assignment_log","other.fb_page_assignment_qa_20261126",
                     "fb_page_assignment_qa_20261126;drop", "fb_page_assignment_qa_%"):
            with self.assertRaises(ValueError):
                qa_name(name)
        self.s.exists=True
        with self.assertRaisesRegex(ValueError,"not owned"):
            self.table().prepare()
        table=self.table().prepare(claim_empty=True)
        row=list(self.s.rows[table.marker,0])
        row[10]="foreign"
        self.s.rows[table.marker,0]=tuple(row)
        with self.assertRaisesRegex(ValueError,"owner"):
            table.drop(self.instance)
        self.assertTrue(self.s.exists)
        self.s.ddl="(different int)"
        with self.assertRaisesRegex(ValueError,"schema differs"):
            table.drop(self.instance)

    @unittest.skipUnless(os.environ.get("FB_PAGE_V2_QA_CONTAINER"),"network=none MySQL container required")
    def test_true_mysql_clone_real_commit_and_dispose(self):
        base=isolated_mysql_sink(os.environ["FB_PAGE_V2_QA_CONTAINER"])
        name=self.name+"_"+uuid.uuid4().hex[:6]
        qa=QATable(self.root,name,base.connection).prepare()
        try:
            state=Store(self.root/"state")
            state.record("native:11::qa.real","valid",24,["d"],{"snapshot":{},
                       "rows":[{"drama_id":"d","page_id":None,"error_code":"drama_profile_missing"}]})
            Workers(state,sink=qa).flush(state.claim())
            self.assertEqual(state.status()["pending"],0)
            self.assertEqual(qa.counts(1,0)[:2],(1,1))
        finally:
            qa.drop(self.instance)
