"""Real cross-UID private WAL fixture plus unchanged frozen timing fences."""
import ast
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import pwd
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
module=ast.parse((ROOT/'features/x_tag_publish/gateway.py').read_text())
func=next(x for x in module.body if isinstance(x,ast.FunctionDef) and x.name=='read_hit_origin')
class Error(Exception):
    def __init__(self,code,*a):self.code=code
spec=importlib.util.spec_from_file_location('hit_timing',ROOT/'features/x_tag_publish/hit_timing.py')
timing=importlib.util.module_from_spec(spec);spec.loader.exec_module(timing)


class Tests(unittest.TestCase):
    @unittest.skipUnless(os.geteuid()==0,'cross-UID Linux check requires root')
    def test_private_wal_read_as_its_owner_without_cross_user_acl(self):
        owner=pwd.getpwnam('x-post-daily')
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);os.chown(folder,owner.pw_uid,owner.pw_gid)
            db=folder/'auto.sqlite3'
            with sqlite3.connect(db) as c:
                c.execute('PRAGMA journal_mode=WAL')
                c.execute('CREATE TABLE x_auto_task(id,account_id,material_id,selection_json,scheduled_at_utc)')
                c.execute("INSERT INTO x_auto_task VALUES(1,2,'3','{}','2026-11-13T00:00:00+00:00')")
            c.close()
            os.chown(db,owner.pw_uid,owner.pw_gid);db.chmod(0o600)
            ns={'Error':Error,'json':json,'lane_paths':lambda:{'X_AUTO_POST_DB_PATH':db}}
            exec(compile(ast.Module(body=[func],type_ignores=[]),'<origin>','exec'),ns)
            for _ in range(3):self.assertEqual(ns['read_hit_origin'](1)['account_id'],2)
            self.assertIsNone(ns['read_hit_origin'](99))
            self.assertEqual(folder.stat().st_mode&0o777,0o700)
            self.assertEqual(db.stat().st_mode&0o777,0o600)
            for invalid in (True,'1',0,-1,2**63):
                with self.assertRaises(Error):ns['read_hit_origin'](invalid)
    def test_remote_origin_retains_due_and_identity_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Path(temp)/'post.sqlite3'
            with sqlite3.connect(db) as c:
                c.execute('CREATE TABLE x_post_manual_run(id,trigger_source,external_task_key)')
                c.execute("INSERT INTO x_post_manual_run VALUES(1,'auto_template','x-auto-task-17')")
            store=types.SimpleNamespace(db_path=db,get_queue=lambda q:dict(manual_run_id=1,account_id=2,material_id='3'))
            when='2026-11-14T00:00:00+00:00'
            row=dict(account_id=2,material_id='3',selection_json=json.dumps({'hit_publish_timing':{'publish_at_utc':when}}),scheduled_at_utc=when)
            with patch.object(timing,'_origin_reader',lambda tid:dict(row)):
                with self.assertRaises(Exception) as e:timing.assert_due('unreadable-auto',store,1,dt.datetime(2026,11,13,tzinfo=dt.timezone.utc))
                self.assertEqual(e.exception.code,'x_auto_hit_not_due')
                timing.assert_due('unreadable-auto',store,1,dt.datetime(2026,11,15,tzinfo=dt.timezone.utc))
                row['account_id']=9
                with self.assertRaises(Exception) as e:timing.assert_due('',store,1)
                self.assertEqual(e.exception.code,'x_auto_hit_origin_invalid')
    def test_operator_audited_failure_ack_is_exact_and_has_no_platform_intent(self):
        import contextlib,hashlib
        spec=importlib.util.spec_from_file_location('dispatch_native',ROOT/'features/x_tag_publish/dispatch_native.py')
        native=importlib.util.module_from_spec(spec);spec.loader.exec_module(native)
        c=sqlite3.connect(':memory:');self.addCleanup(c.close);c.row_factory=sqlite3.Row
        c.executescript("""CREATE TABLE pipeline_x_unentered_failure_ack(source_key,generation,claim_hash,error_code);
            CREATE TABLE pipeline_x_claim(source_key,generation);
            CREATE TABLE pipeline_x_operation(source_key,generation);
            CREATE TABLE pipeline_x_checkpoint(source_key,generation);
            CREATE TABLE x_post_queue(id); CREATE TABLE x_post_publish_log(queue_id);""")
        token='unit-test-claim-secret-at-least-32-characters';hashed=hashlib.sha256(token.encode()).hexdigest()
        c.execute("INSERT INTO pipeline_x_unentered_failure_ack VALUES('x:queue:1',2,?,'x_tag_dispatch_failed')",(hashed,))
        c.execute('INSERT INTO x_post_queue VALUES(1)');c.execute('INSERT INTO x_post_publish_log VALUES(1)')
        @contextlib.contextmanager
        def connect():yield c
        bridge=types.SimpleNamespace(connect=connect,claim_hash=lambda claim:hashlib.sha256(claim['claim_token'].encode()).hexdigest())
        job=dict(source_key='x:queue:1',source_id='1',generation=2,stage='publish');claim={'claim_token':token};receipt=dict(error_code='x_tag_dispatch_failed',unknown_outcome=False)
        with patch('features.pipeline_dispatch.x_prepost_recovery.authorization',return_value={'state':'armed'}):
            self.assertTrue(native.acknowledge_unentered_failure(bridge,job,claim,receipt))
            self.assertFalse(native.acknowledge_unentered_failure(bridge,job,{'claim_token':'wrong'},receipt))
            self.assertFalse(native.acknowledge_unentered_failure(bridge,job,claim,dict(receipt,unknown_outcome=True)))
            c.execute("INSERT INTO pipeline_x_operation VALUES('x:queue:1',2)")
            self.assertFalse(native.acknowledge_unentered_failure(bridge,job,claim,receipt))
            c.execute('DELETE FROM pipeline_x_operation')
        with patch('features.pipeline_dispatch.x_prepost_recovery.authorization',return_value=None):
            self.assertFalse(native.acknowledge_unentered_failure(bridge,job,claim,receipt))

    def test_type_simple_readiness_waits_for_listener(self):
        from scripts import deploy_x_tag_origin_broker as deploy
        import urllib.error
        response=unittest.mock.MagicMock();response.__enter__.return_value.status=200
        opener=types.SimpleNamespace(open=unittest.mock.Mock(side_effect=[urllib.error.URLError('not-bound'),response]))
        with patch('urllib.request.build_opener',return_value=opener),patch.object(deploy.time,'sleep') as sleep:
            deploy.ready('http://127.0.0.1:8898/healthz')
        self.assertEqual(opener.open.call_count,2);sleep.assert_called_once_with(.5)

    def test_endpoint_authentication_precedes_read(self):
        handler=next(x for x in module.body if isinstance(x,ast.ClassDef) and x.name=='Handler')
        dispatch=next(x for x in handler.body if isinstance(x,ast.FunctionDef) and x.name=='dispatch')
        import hmac
        from urllib.parse import urlsplit
        calls=[]
        ns=dict(Error=Error,os=os,hmac=hmac,urlsplit=urlsplit,read_hit_origin=lambda tid:calls.append(tid) or {'account_id':2})
        exec(compile(ast.Module(body=[dispatch],type_ignores=[]),'<gateway>','exec'),ns)
        fake=types.SimpleNamespace(path='/internal/x-tag/hit-origin',command='POST',headers={},body=lambda:{'task_id':17})
        with patch.dict(os.environ,X_INTERNAL_TOKEN='test-private-token-which-is-at-least-32-characters'):
            with self.assertRaises(Error):ns['dispatch'](fake)
            self.assertEqual(calls,[])
            fake.headers['Authorization']='Bearer test-private-token-which-is-at-least-32-characters'
            self.assertEqual(ns['dispatch'](fake),(200,{'origin':{'account_id':2}}))
            self.assertEqual(calls,[17])

if __name__=='__main__':unittest.main()
