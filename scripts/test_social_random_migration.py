import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from features import social_random_schedule as daily
from scripts.migrate_social_random_schedule import build_plan,apply_plan,reverse_before_effective


class ClosingConnection(sqlite3.Connection):
    def __exit__(self,*args):
        try:return super().__exit__(*args)
        finally:self.close()


def connect(path):
    return sqlite3.connect(path,factory=ClosingConnection)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.fb=Path(self.tmp.name)/'fb.sqlite3';self.dispatch=Path(self.tmp.name)/'dispatch.sqlite3'
        self.now=dt.datetime(2026,11,10,3,tzinfo=dt.timezone.utc)
        config={'schedule':{'mode':'random','start':'01:00','end':'23:59','daily_count':1}}
        with connect(self.fb) as c:
            c.executescript("""
                CREATE TABLE fb_auto_run(id INTEGER PRIMARY KEY,config_json TEXT,trigger_type TEXT);
                CREATE TABLE fb_auto_task(id INTEGER PRIMARY KEY,run_id INTEGER,template_id INTEGER,template_version INTEGER,
                    page_id TEXT,status TEXT,skip_reason TEXT,planned_publish_at_utc TEXT,schedule_policy TEXT DEFAULT '',
                    graph_post_id TEXT DEFAULT '',unknown_outcome INTEGER DEFAULT 0,lease_owner TEXT DEFAULT '',lease_expires_at_utc TEXT DEFAULT '',
                    attempt_count INTEGER DEFAULT 2,started_at_utc TEXT DEFAULT '2026-11-09T00:00:00+00:00',prepared_sha256 TEXT DEFAULT 'original');
                CREATE TABLE fb_auto_due_slot(template_id INTEGER,slot_key TEXT,status TEXT,error_code TEXT DEFAULT '',schedule_policy TEXT DEFAULT '');
                CREATE TABLE fb_auto_publish_attempt(task_id INTEGER);
            """)
            c.execute("INSERT INTO fb_auto_run VALUES(1,?,'auto')",(json.dumps(config),))
            for identity in (1,2):
                c.execute("INSERT INTO fb_auto_task(id,run_id,template_id,template_version,page_id,status,skip_reason,planned_publish_at_utc) VALUES(?,1,3,2,?,'ready','','2026-11-11T12:00:00+00:00')",(identity,str(identity)))
            c.execute("INSERT INTO fb_auto_due_slot(template_id,slot_key,status) VALUES(3,'auto:v2:2026-11-11:20:00','prepared')")
        with connect(self.dispatch) as c:
            c.executescript("""CREATE TABLE pipeline_job(id INTEGER PRIMARY KEY,source_key TEXT,stage TEXT,status TEXT,
               scheduled_at TEXT,source_meta_json TEXT,payload_json TEXT,generation INTEGER DEFAULT 0,
               claim_hash TEXT DEFAULT '',external_intent INTEGER DEFAULT 0,canonical_receipt TEXT DEFAULT '',
               lease_expires_at TEXT DEFAULT '',hold_reason TEXT DEFAULT '');""")
            for identity in (1,2):
                c.execute("INSERT INTO pipeline_job(id,source_key,stage,status,scheduled_at,source_meta_json,payload_json) VALUES(?,?,'publish','ready','2026-11-11T12:00:00Z','{}',?)",(identity,'fb:auto:'+str(identity),json.dumps({'planned_publish_at_utc':'2026-11-11T12:00:00+00:00','artifact':'keep'})))

    def plan(self):return build_plan(self.fb,self.dispatch,'2026-11-11',now=self.now)

    def test_retime_preserves_artifact_and_preparation_attempts(self):
        plan=self.plan();self.assertEqual(len(plan['groups'][0]['entries']),2)
        result=apply_plan(self.fb,self.dispatch,plan,now=self.now)
        self.assertEqual(result['retimed'],2)
        self.assertEqual(apply_plan(self.fb,self.dispatch,plan,now=self.now)['bound'],2)
        with connect(self.fb) as c:
            self.assertEqual(c.execute("SELECT prepared_sha256,attempt_count,schedule_policy FROM fb_auto_task WHERE id=1").fetchone(),('original',2,daily.POLICY))
            self.assertEqual(c.execute("SELECT count(*) FROM fb_auto_task").fetchone()[0],2)
            expected=c.execute("SELECT planned_publish_at_utc FROM fb_auto_task WHERE id=1").fetchone()[0]
        with connect(self.dispatch) as c:
            row=c.execute("SELECT scheduled_at,source_meta_json,payload_json FROM pipeline_job WHERE id=1").fetchone()
            self.assertEqual(dt.datetime.fromisoformat(row[0].replace('Z','+00:00')),dt.datetime.fromisoformat(expected))
            self.assertEqual(json.loads(row[1])['cutover_due_at'],row[0])
            self.assertEqual(json.loads(row[2])['artifact'],'keep')

    def test_real_publish_evidence_is_preserved_only_on_its_page(self):
        with connect(self.fb) as c:c.execute('INSERT INTO fb_auto_publish_attempt VALUES(1)')
        plan=self.plan()
        result=apply_plan(self.fb,self.dispatch,plan,now=self.now)
        self.assertEqual(result['retimed'],1)
        with connect(self.fb) as c:
            self.assertEqual(c.execute('SELECT planned_publish_at_utc,schedule_policy FROM fb_auto_task WHERE id=1').fetchone(),('2026-11-11T12:00:00+00:00',''))

    def test_new_execution_after_preview_does_not_block_other_page(self):
        plan=self.plan()
        with connect(self.dispatch) as c:c.execute("UPDATE pipeline_job SET generation=1,claim_hash='occupied' WHERE id=1")
        result=apply_plan(self.fb,self.dispatch,plan,now=self.now)
        self.assertEqual(result['retimed'],1)
        self.assertEqual(result['skipped'][0]['task_id'],1)

    def test_failed_is_bound_without_reactivation(self):
        with connect(self.fb) as c:c.execute("UPDATE fb_auto_task SET status='failed' WHERE id=1")
        result=apply_plan(self.fb,self.dispatch,self.plan(),now=self.now)
        self.assertEqual(result['retimed'],1)
        with connect(self.fb) as c:
            self.assertEqual(c.execute('SELECT status FROM fb_auto_task WHERE id=1').fetchone()[0],'failed')
            self.assertEqual(c.execute("SELECT native_id FROM social_random_slot WHERE target_id='1'").fetchone()[0],'1')

    def test_partial_preserved_page_uses_complete_original_plan(self):
        with connect(self.fb) as c:
            config=json.loads(c.execute('SELECT config_json FROM fb_auto_run').fetchone()[0])
            config['schedule']['daily_count']=2
            c.execute('UPDATE fb_auto_run SET config_json=?',(json.dumps(config),))
            c.execute("UPDATE fb_auto_task SET status='failed' WHERE id=1")
            c.execute('CREATE TABLE fb_auto_schedule_plan(template_id INTEGER,template_version INTEGER,local_date TEXT,times_json TEXT)')
            c.execute("INSERT INTO fb_auto_schedule_plan VALUES(3,2,'2026-11-11',?)",(json.dumps(['12:00','20:00']),))
        plan=self.plan()
        target=next(t for t in plan['groups'][0]['plan']['targets'] if t['target_id']=='1')
        self.assertEqual(target['times'],['12:00','20:00'])
        self.assertEqual(next(e for e in plan['groups'][0]['entries'] if e['task_id']==1)['ordinal'],1)
        apply_plan(self.fb,self.dispatch,plan,now=self.now)
        with connect(self.fb) as c:
            self.assertEqual(c.execute("SELECT native_id FROM social_random_slot WHERE target_id='1' AND ordinal=1").fetchone()[0],'1')

    def test_pre_activation_rollback_preserves_frozen_plan_and_material(self):
        plan=self.plan();apply_plan(self.fb,self.dispatch,plan,now=self.now)
        with self.assertRaisesRegex(ValueError,'compatibility_rollback'):
            reverse_before_effective(self.fb,self.dispatch,plan,now=self.now)
        planned=next(e['after'] for g in plan['groups'] for e in g['entries'] if e['task_id']==1)
        with connect(self.fb) as c:
            self.assertEqual(c.execute('SELECT planned_publish_at_utc,prepared_sha256,attempt_count FROM fb_auto_task WHERE id=1').fetchone(),(planned,'original',2))
        with self.assertRaisesRegex(ValueError,'compatibility_rollback'):
            reverse_before_effective(self.fb,self.dispatch,plan,now=self.now+dt.timedelta(days=1))


if __name__=='__main__':unittest.main()
