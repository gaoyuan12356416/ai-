import contextlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('recovery', Path(__file__).resolve().parents[1]/'scripts/recover_fb_unclaimed_bridge.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.central = self.root/'central.sqlite3'
        self.native = self.root/'native.sqlite3'
        with contextlib.closing(sqlite3.connect(self.central)) as conn, conn:
            conn.executescript("""
              CREATE TABLE pipeline_job(id INTEGER PRIMARY KEY,source_key TEXT,stage TEXT,channel TEXT,family TEXT,source_id TEXT,
                status TEXT,error_code TEXT,external_intent INTEGER,canonical_receipt TEXT,hold_reason TEXT,lease_expires_at TEXT,
                generation INTEGER,source_status TEXT,node_id TEXT,worker_id TEXT,claim_hash TEXT,started_at TEXT,finished_at TEXT,error_message TEXT,updated_at TEXT);
              CREATE TABLE pipeline_event(id INTEGER PRIMARY KEY,job_id INTEGER,event_type TEXT,details_json TEXT,created_at TEXT);
              CREATE TABLE pipeline_publication(source_key TEXT);
              INSERT INTO pipeline_job VALUES(100,'fb:auto:11','publish','fb','auto','11','failed','canonical_bridge_unavailable',0,'','','2020-01-01T00:00:00Z',1,'ready','node','worker','oldhash','oldstart','oldfinish','','oldtime');
              INSERT INTO pipeline_job SELECT 200,'fb:auto:22',stage,channel,family,'22',status,error_code,external_intent,canonical_receipt,hold_reason,lease_expires_at,generation,source_status,node_id,worker_id,claim_hash,started_at,finished_at,error_message,updated_at FROM pipeline_job WHERE id=100;
            """)
            for table in ('pipeline_operation','pipeline_receipt','pipeline_checkpoint','pipeline_resource','pipeline_legacy_resource'):
                conn.execute('CREATE TABLE '+table+'(job_id INTEGER)')
        with contextlib.closing(sqlite3.connect(self.native)) as conn, conn:
            conn.executescript("""
              CREATE TABLE fb_auto_task(id INTEGER PRIMARY KEY,status TEXT,unknown_outcome INTEGER,graph_post_id TEXT,lease_owner TEXT,
                lease_expires_at_utc TEXT,prepared_at_utc TEXT,prepared_media_url TEXT,prepared_sha256 TEXT,media_url TEXT,source_media_url TEXT,planned_publish_at_utc TEXT);
              INSERT INTO fb_auto_task VALUES(11,'ready',0,'','','','2020-01-01','https://prepared','sha','https://prepared','https://original','2020-01-01T00:00:00Z');
              INSERT INTO fb_auto_task SELECT 22,status,unknown_outcome,graph_post_id,lease_owner,lease_expires_at_utc,prepared_at_utc,prepared_media_url,prepared_sha256,media_url,source_media_url,planned_publish_at_utc FROM fb_auto_task WHERE id=11;
              CREATE TABLE fb_auto_publish_attempt(task_id INTEGER);
              CREATE TABLE fb_auto_publish_ledger(task_id INTEGER);
              CREATE TABLE pipeline_fb_claim(source_key TEXT,stage TEXT);
              CREATE TABLE pipeline_fb_operation(source_key TEXT);
              CREATE TABLE pipeline_canonical_hold(source_key TEXT);
              INSERT INTO pipeline_fb_claim VALUES('fb:auto:11','prepare');
            """)

    def tearDown(self):
        self.folder.cleanup()

    @contextlib.contextmanager
    def connect(self, write=False):
        conn = sqlite3.connect(self.central.as_uri()+'?mode='+('rw' if write else 'ro'),uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute('ATTACH DATABASE ? AS native',(self.native.as_uri()+'?mode=ro',))
        conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        try:
            yield conn
        finally:
            conn.close()

    def test_exact_requeue_preserves_native_and_other_jobs_and_is_idempotent(self):
        before_native = self.native.read_bytes()
        with self.connect() as conn:
            prepared = recovery.plan(conn,[11])
        self.assertEqual(prepared['excluded'],[])
        with self.connect(True) as conn:
            verified = recovery.plan(conn,[11])
            self.assertEqual(verified['plan_sha256'],prepared['plan_sha256'])
            self.assertEqual(recovery.apply(conn,verified,self.root/'backup.json'),[11])
            conn.commit()
        self.assertEqual(self.native.read_bytes(),before_native)
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT status FROM pipeline_job WHERE id=100').fetchone()[0],'queued')
            self.assertEqual(conn.execute('SELECT status FROM pipeline_job WHERE id=200').fetchone()[0],'failed')
            self.assertTrue(recovery.plan(conn,[11])['items'][0]['already_applied'])
            self.assertEqual(conn.execute('SELECT generation FROM pipeline_job WHERE id=100').fetchone()[0],1)
        self.assertEqual(json.loads((self.root/'backup.json').read_text())['items'][0]['job']['status'],'failed')

    def test_write_intent_or_receipt_never_recovers(self):
        cases = [
            ('central',"UPDATE pipeline_job SET external_intent=1 WHERE id=100"),
            ('central',"INSERT INTO pipeline_operation VALUES(100)"),
            ('central',"INSERT INTO pipeline_receipt VALUES(100)"),
            ('central',"INSERT INTO pipeline_publication VALUES('fb:auto:11')"),
            ('central',"INSERT INTO pipeline_resource VALUES(100)"),
            ('native',"INSERT INTO fb_auto_publish_attempt VALUES(11)"),
            ('native',"INSERT INTO fb_auto_publish_ledger VALUES(11)"),
            ('native',"INSERT INTO pipeline_fb_claim VALUES('fb:auto:11','publish')"),
            ('native',"INSERT INTO pipeline_fb_operation VALUES('fb:auto:11')"),
            ('native',"UPDATE fb_auto_task SET unknown_outcome=1 WHERE id=11"),
            ('native',"UPDATE fb_auto_task SET graph_post_id='receipt' WHERE id=11"),
            ('native',"UPDATE fb_auto_task SET lease_owner='active' WHERE id=11"),
        ]
        for database,statement in cases:
            with self.subTest(statement=statement):
                path=getattr(self,database)
                original=path.read_bytes()
                with contextlib.closing(sqlite3.connect(path)) as conn, conn:
                    conn.execute(statement)
                with self.connect() as conn:
                    result=recovery.plan(conn,[11,22])
                self.assertEqual([x['task_id'] for x in result['excluded']],[11])
                self.assertEqual([x['task_id'] for x in result['items']],[22])
                path.write_bytes(original)

    def test_changed_proof_changes_plan_hash(self):
        with self.connect() as conn:
            original=recovery.plan(conn,[11])
        with contextlib.closing(sqlite3.connect(self.central)) as conn, conn:
            conn.execute("UPDATE pipeline_job SET generation=2 WHERE id=100")
        with self.connect() as conn:
            self.assertNotEqual(original['plan_sha256'],recovery.plan(conn,[11])['plan_sha256'])


if __name__ == '__main__':
    unittest.main()
