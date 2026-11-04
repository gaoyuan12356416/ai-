import copy
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import reconcile_fb_once_receipts as op


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name)/'native.db'
        self.now = datetime.now(timezone.utc)
        self.at = (self.now-timedelta(days=1)).isoformat(timespec='seconds')
        with closing(sqlite3.connect(self.db)) as c, c:
            c.executescript('''
                CREATE TABLE fb_auto_task(id INTEGER PRIMARY KEY,run_id INTEGER,page_id TEXT,material_id TEXT,status TEXT,graph_post_id TEXT,unknown_outcome INTEGER,error_code TEXT,error_message TEXT,lease_owner TEXT,lease_expires_at_utc TEXT,next_reconcile_at_utc TEXT,completed_at_utc TEXT,message_text TEXT,short_url TEXT,prepared_duration_seconds REAL);
                CREATE TABLE fb_auto_publish_attempt(id INTEGER,task_id INTEGER,result_kind TEXT,created_at_utc TEXT);
                CREATE TABLE fb_auto_publish_ledger(task_id INTEGER PRIMARY KEY,page_id TEXT,material_id TEXT,status TEXT,graph_post_id TEXT,definite_attempts INTEGER,unknown_outcome INTEGER,error_code TEXT,created_at_utc TEXT,updated_at_utc TEXT);
                CREATE TABLE fb_auto_run(id INTEGER PRIMARY KEY,status TEXT,completed_at_utc TEXT);
                CREATE TABLE fb_video_task(task_id TEXT PRIMARY KEY,page_id TEXT,status TEXT,graph_post_id TEXT,unknown_outcome INTEGER,error_code TEXT,error_message TEXT,execution_lease TEXT,execution_until TEXT,reconcile_at TEXT,updated_at_utc TEXT,caption TEXT,short_url TEXT,processing_mode TEXT,prepared_json TEXT,snapshot_json TEXT);
                CREATE TABLE fb_video_attempt(id INTEGER,task_id TEXT,result_kind TEXT,created_at_utc TEXT);
            ''')
            c.execute("INSERT INTO fb_auto_task VALUES(1,2,'123','m','unknown','',1,'unknown','unknown','','','',?,'caption https://s/1','https://s/1',30)", (self.at,))
            c.execute("INSERT INTO fb_auto_publish_attempt VALUES(1,1,'unknown',?)", (self.at,))
            c.execute("INSERT INTO fb_auto_publish_ledger VALUES(1,'123','m','unknown','',0,1,'unknown',?,?)", (self.at,self.at))
            c.execute("INSERT INTO fb_auto_run VALUES(2,'attention_required',?)", (self.at,))
            c.execute("INSERT INTO fb_video_task VALUES('v1','124','unknown','987',1,'unknown','unknown','','','',?,'caption','s','original','{}','{}')", (self.at,))
        with closing(op.connect(self.db)) as c:
            self.state = op.snapshot(c, 'fb:auto:1')
        self.row = {'id':'999', 'owner_page_id':'123', 'created_time':self.at.replace('+00:00','+0000'), 'length':30.0,
                    'exact_description':True, 'exact_frozen_short_url':True, 'description_sha256':op.text_sha('caption https://s/1'),
                    'status':{'video_status':'ready','publishing_phase':{'status':'complete','publish_status':'published','publish_time':self.at.replace('+00:00','+0000')}}}
        self.r = {'family':'auto','page_id':'123','direct':self.row,'queries':[{'http_status':200,'kind':'matched_video_confirmation'}],
                  'checked_at':self.now.isoformat(),'original_graph_id':'','scan_complete':True,'matches':[copy.deepcopy(self.row)]}
        self.m = {'page_id':'123','description_sha256':op.text_sha('caption https://s/1'),'short_url_sha256':op.text_sha('https://s/1'),'start':op.parsed(self.at).timestamp(),'end':op.parsed(self.at).timestamp()+120}

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, source='fb:auto:1'):
        with closing(op.connect(self.db)) as c:
            state = op.snapshot(c, source)
        proof = op.proof_for(state,self.r,self.m,self.now) if source == 'fb:auto:1' else {'video_id':'987','page_id':'124','published_at':self.at}
        p = {'kind':op.KIND,'db_path':str(self.db),'created_at':self.now.isoformat(),'items':[{'source_key':source,'before':state,'before_sha256':op.sha(state),'proof':proof}],'unresolved':[]}
        p['plan_sha256']=op.sha(p)
        return p

    def test_timezone_and_exact_identity(self):
        self.assertEqual(op.proof_for(self.state,self.r,self.m,self.now)['video_id'],'999')
        for key,value in [('owner_page_id','456'),('description_sha256','wrong'),('length',90)]:
            r=copy.deepcopy(self.r);r['direct'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):op.proof_for(self.state,r,self.m,self.now)

    def test_ready_without_published_or_incomplete_scan_rejected(self):
        r=copy.deepcopy(self.r);r['direct']['status']['publishing_phase']['publish_status']='unpublished'
        with self.assertRaises(ValueError):op.proof_for(self.state,r,self.m,self.now)
        r=copy.deepcopy(self.r);r['scan_complete']=False
        with self.assertRaises(ValueError):op.proof_for(self.state,r,self.m,self.now)

    def test_active_lease_and_duplicate_receipt_rejected(self):
        state=copy.deepcopy(self.state);state['task']['lease_expires_at_utc']=(self.now+timedelta(minutes=1)).isoformat()
        proof=self.plan()['items'][0]['proof']
        with closing(op.connect(self.db)) as c,self.assertRaisesRegex(ValueError,'active_native_lease'):op.eligible(c,'fb:auto:1',state,proof,self.now)
        with closing(sqlite3.connect(self.db)) as c, c:c.execute("UPDATE fb_video_task SET graph_post_id='999'")
        with closing(op.connect(self.db)) as c,self.assertRaisesRegex(ValueError,'video_bound_elsewhere'):op.eligible(c,'fb:auto:1',self.state,proof,self.now)

    @patch.object(op,'validate_backup_location')
    def test_atomic_apply_idempotent_and_attempts_preserved(self,_):
        p=self.plan();r=op.apply_plan(p,p['plan_sha256'],Path(self.temp.name)/'before.db')
        self.assertEqual(r['platform_writes'],0)
        with closing(op.connect(self.db)) as c:
            after=op.snapshot(c,'fb:auto:1')
            self.assertEqual(after['task']['status'],'published')
            self.assertEqual(after['ledger']['graph_post_id'],'999')
            self.assertEqual(after['attempts'],self.state['attempts'])
            self.assertEqual(after['task']['completed_at_utc'],self.at)
        r=op.apply_plan(p,p['plan_sha256'],Path(self.temp.name)/'repeat.db')
        self.assertEqual(r['items'][0]['status'],'already_committed')

    @patch.object(op,'validate_backup_location')
    def test_cas_drift_keeps_unknown(self,_):
        p=self.plan()
        with closing(sqlite3.connect(self.db)) as c, c:c.execute("UPDATE fb_auto_task SET error_message='changed'")
        with self.assertRaisesRegex(ValueError,'native_compare_and_swap_failed'):op.apply_plan(p,p['plan_sha256'],Path(self.temp.name)/'drift.db')
        with closing(op.connect(self.db)) as c:self.assertEqual(op.snapshot(c,'fb:auto:1')['task']['status'],'unknown')

    @patch.object(op,'validate_backup_location')
    def test_video_retains_original_publish_date(self,_):
        p=self.plan('fb:video:v1');op.apply_plan(p,p['plan_sha256'],Path(self.temp.name)/'video.db')
        with closing(op.connect(self.db)) as c:
            t=op.snapshot(c,'fb:video:v1')['task']
            self.assertEqual((t['status'],t['graph_post_id'],t['updated_at_utc']),('published','987',self.at))


if __name__=='__main__':unittest.main()
