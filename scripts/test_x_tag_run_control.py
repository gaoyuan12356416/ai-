"""Termination races, receipt preservation and HTTP authorization contracts."""
import ast
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.x_auto_posts.core import XAutoPostStore, AuditActor
from features.x_tag_publish.run_control import terminate_run, RunControlError


class RunControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.auto = Path(self.temp.name) / 'auto.sqlite3'
        self.posts = Path(self.temp.name) / 'posts.sqlite3'
        self.store = XAutoPostStore(self.auto)
        self.actor = {'user_id':'operator','name':'Operator','tenant_key':'tenant'}
        template = self.store.create_template(name='test', config={'account_ids':['1','2'],'language':'en','body_template':'{{drama_name}}'},
                                             actor=AuditActor('operator','Operator'),confirmation={'accepted':True,'version':'x-auto-v1'})
        self.template = template
        self.run = self.make_run('first')
        self.task = self.store.create_task(run_id=self.run.id,account_id='1',language='en')
        self.other = self.store.create_task(run_id=self.make_run('unrelated').id,account_id='2',language='en')
        with closing(sqlite3.connect(self.posts)) as c, c:
            c.executescript('''
                CREATE TABLE x_post_manual_run(id INTEGER PRIMARY KEY,trigger_source TEXT,external_task_key TEXT,
                  status TEXT,queued_count INTEGER DEFAULT 1,failed_count INTEGER DEFAULT 0,error_code TEXT DEFAULT '',
                  error_message TEXT DEFAULT '',finished_at TEXT DEFAULT '',updated_at TEXT DEFAULT '');
                CREATE TABLE x_post_queue(id INTEGER PRIMARY KEY,manual_run_id INTEGER,account_id INTEGER,status TEXT,updated_at TEXT);
                CREATE TABLE x_post_publish_log(id INTEGER PRIMARY KEY,queue_id INTEGER UNIQUE,account_id INTEGER,status TEXT,
                  attempt_count INTEGER DEFAULT 0,x_post_id TEXT DEFAULT '',unknown_outcome INTEGER DEFAULT 0,
                  error_code TEXT DEFAULT '',error_message TEXT DEFAULT '',created_at TEXT,updated_at TEXT);
                CREATE TABLE pipeline_canonical_hold(source_key TEXT PRIMARY KEY,cutoff TEXT,reason TEXT);
            ''')

    def make_run(self,key):
        return self.store.create_run(run_key=key,template_id=self.template.id,template_version=self.template.version,
                                     trigger_type='manual',scheduled_at_utc='2026-11-17T00:00:00+00:00',
                                     shanghai_date='2026-11-17',publish_time='08:00')

    def stop(self):
        return terminate_run(self.auto,self.posts,self.run.id,self.actor)

    def queue(self,status='ready',log=None):
        with closing(sqlite3.connect(self.auto)) as c, c:
            c.execute('UPDATE x_auto_task SET status=?,execution_run_id=8,execution_queue_id=9 WHERE id=?',(status,self.task.id))
        with closing(sqlite3.connect(self.posts)) as c, c:
            c.execute("INSERT INTO x_post_manual_run(id,trigger_source,external_task_key,status) VALUES(8,'auto_template',?,'running')",
                      ('x-auto-task-'+str(self.task.id),))
            c.execute("INSERT INTO x_post_queue VALUES(9,8,1,'queued','before')")
            if log:
                c.execute('INSERT INTO x_post_publish_log(id,queue_id,account_id,status,attempt_count,x_post_id,unknown_outcome) '
                          'VALUES(10,9,1,?,?,?,?)',log)

    def test_pending_selection_canceled_idempotently_and_scoped(self):
        self.store.claim_next_pending_task(worker_id='old-selector',lease_seconds=120)
        result=self.stop()
        self.assertEqual(result['canceled_count'],1)
        self.assertEqual(self.store.get_task(self.task.id).status,'canceled')
        self.assertEqual(self.store.get_task(self.other.id).status,'pending')
        with closing(sqlite3.connect(self.auto)) as c, c:
            events=c.execute('SELECT COUNT(*) FROM x_auto_event').fetchone()[0]
        self.assertTrue(self.stop()['already_terminated'])
        with closing(sqlite3.connect(self.auto)) as c, c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM x_auto_event').fetchone()[0],events)
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE x_auto_task SET status='retry_wait' WHERE id=?",(self.task.id,))

    def test_ready_queue_and_reserved_log_stopped(self):
        self.queue(log=('reserved',0,'',0))
        self.assertEqual(self.stop()['canceled_count'],1)
        with closing(sqlite3.connect(self.posts)) as c, c:
            self.assertEqual(c.execute('SELECT status,error_code,attempt_count FROM x_post_publish_log').fetchone(),
                             ('failed','operator_terminated',0))
            self.assertEqual(c.execute('SELECT status FROM x_post_manual_run').fetchone()[0],'stopped')
            self.assertEqual(c.execute('SELECT source_key FROM pipeline_canonical_hold').fetchone()[0],'x:queue:9')

    def test_queue_without_log_keeps_dedup_and_creates_audit_receipt(self):
        self.queue()
        self.stop()
        with closing(sqlite3.connect(self.posts)) as c, c:
            self.assertEqual(c.execute('SELECT id,status FROM x_post_queue').fetchone(),(9,'failed'))
            self.assertEqual(c.execute('SELECT attempt_count FROM x_post_publish_log').fetchone()[0],0)

    def test_inflight_upload_and_unknown_are_not_rewritten(self):
        for status,log in [('ready',('media_uploading',1,'',0)),('unknown',('post_creating',1,'',1))]:
            with self.subTest(status=status):
                self.queue(status,log)
                with closing(sqlite3.connect(self.posts)) as c, c: before=c.execute('SELECT * FROM x_post_publish_log').fetchall()
                result=self.stop()
                self.assertEqual(result['waiting_count'],1)
                self.assertEqual(self.store.get_task(self.task.id).status,status)
                with closing(sqlite3.connect(self.posts)) as c, c:
                    self.assertEqual(c.execute('SELECT * FROM x_post_publish_log').fetchall(),before)
                    c.execute('DELETE FROM x_post_queue');c.execute('DELETE FROM x_post_manual_run');c.execute('DELETE FROM x_post_publish_log')

    def test_confirmed_publication_and_completed_siblings_preserved(self):
        with closing(sqlite3.connect(self.auto)) as c, c:
            c.execute("UPDATE x_auto_task SET status='published',publish_id='123',publish_url='https://x.com/a/status/123' WHERE id=?",(self.task.id,))
        self.assertEqual(self.stop()['canceled_count'],0)
        self.assertEqual(self.store.get_task(self.task.id).publish_id,'123')

    def test_invalid_auth_id_and_finished_run(self):
        for rid,actor,code in [(self.run.id,{},'permission_denied'),(-1,self.actor,'invalid_request'),(999,self.actor,'x_auto_run_not_found')]:
            with self.assertRaises(RunControlError) as error: terminate_run(self.auto,self.posts,rid,actor)
            self.assertEqual(error.exception.code,code)
        self.store.set_run_status(self.run.id,'completed')
        with self.assertRaises(RunControlError) as error:self.stop()
        self.assertEqual(error.exception.status,409)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        source=ast.parse((ROOT/'features/x_tag_publish/gateway.py').read_text(encoding='utf-8'))
        handler=next(n for n in source.body if isinstance(n,ast.ClassDef) and n.name=='Handler')
        method=next(n for n in handler.body if isinstance(n,ast.FunctionDef) and n.name=='dispatch')
        self.env=dict(__package__='features.x_tag_publish',re=re,urlsplit=urlsplit,parse_qs=parse_qs,
                      AUTO='/api/admin/x-tag-auto-publish',POST='/api/admin/x-tag-posts',Error=RunControlError,
                      PROFILE_TEMPLATE_NAV='templates',PROFILE_POOL_NAV='pool',authorize=Mock(return_value={'user_id':'trusted'}),
                      lane_paths=lambda:{'X_AUTO_POST_DB_PATH':'auto','X_POST_DB_PATH':'posts'})
        exec(compile(ast.Module(body=[method],type_ignores=[]),'<gateway>','exec'),self.env)
        self.handler=types.SimpleNamespace(path='/api/admin/x-tag-auto-publish/runs/123/terminate',command='POST',
                                          headers={'Cookie':'opaque','Origin':'https://ai.yingliangads.com'},body=lambda:{})

    def test_authorized_post_uses_trusted_actor(self):
        with patch('features.x_tag_publish.run_control.terminate_run',return_value={'ok':True}) as stop:
            self.assertEqual(self.env['dispatch'](self.handler),(200,{'ok':True}))
            stop.assert_called_once_with('auto','posts',123,{'user_id':'trusted'})

    def test_auth_origin_method_and_payload_fail_before_mutation(self):
        with patch('features.x_tag_publish.run_control.terminate_run') as stop:
            self.env['authorize'].side_effect=RunControlError('denied','denied',403)
            with self.assertRaises(RunControlError):self.env['dispatch'](self.handler)
            self.env['authorize'].side_effect=None
            self.handler.headers['Origin']='https://evil.example'
            with self.assertRaises(RunControlError):self.env['dispatch'](self.handler)
            self.handler.headers['Origin']='https://ai.yingliangads.com'
            self.handler.command='GET'
            with self.assertRaises(RunControlError):self.env['dispatch'](self.handler)
            self.handler.command='POST';self.handler.body=lambda:{'_actor':{'user_id':'forged'}}
            with self.assertRaises(RunControlError):self.env['dispatch'](self.handler)
            stop.assert_not_called()

    def test_origin_broker_blocks_terminated_run_before_next_native_write(self):
        source=ast.parse((ROOT/'features/x_tag_publish/gateway.py').read_text(encoding='utf-8'))
        fn=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='read_hit_origin')
        self.env.update(json=json,lane_paths=lambda:{'X_AUTO_POST_DB_PATH':Path(tempfile.gettempdir()).resolve()/'auto'})
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<gateway>','exec'),self.env)
        with patch('subprocess.run',return_value=types.SimpleNamespace(stdout=json.dumps({'run_status':'canceled'}))):
            with self.assertRaises(RunControlError) as error:self.env['read_hit_origin'](123)
            self.assertEqual(error.exception.code,'x_auto_run_terminated')
        with patch('subprocess.run',return_value=types.SimpleNamespace(stdout=json.dumps({'run_status':'running','account_id':'7'}))):
            self.assertEqual(self.env['read_hit_origin'](123),{'account_id':'7'})


if __name__=='__main__':unittest.main()
