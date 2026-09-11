"""Authorization, cache staleness and submit fences, without platform writes."""
import copy
from contextlib import closing
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.drama_synthesis.youtube import YouTubeCredential
from features.youtube_auto_publish.channels import ChannelProbe, ChannelDirectory, ChannelFailures, verdict
from features.youtube_auto_publish.cache import ReadCache
from features.youtube_auto_publish.templates import WorkflowError
import test_youtube_auto_service as workflow_fixture


CRED = YouTubeCredential(account_id='250',channel_local_id='258',channel_id='UC'+'a'*22,
    channel_name='Example',channel_status=1,scopes=frozenset({'youtube.force-ssl'}),
    refresh_token='SECRET_REFRESH',client_id='SECRET_ID',client_secret='SECRET_CLIENT')


def wait_until(fn):
    for _ in range(200):
        if fn():return
        time.sleep(.01)
    raise AssertionError('background work did not finish')


class ProbeTests(unittest.TestCase):
    def probe(self, *, token_status=200, token=None, channel_status=200, channel=None, credential=CRED):
        session=Mock()
        session.post.return_value=Mock(status_code=token_status,json=lambda:token or {'access_token':'SECRET_ACCESS'})
        session.get.return_value=Mock(status_code=channel_status,json=lambda:channel or {'items':[{'id':CRED.channel_id,'status':{'longUploadsStatus':'allowed'}}]})
        result=ChannelProbe(lambda:session)(credential)
        self.assertNotIn('SECRET',json.dumps(result))
        return result,session

    def test_allowed_is_required_eligible_means_phone_verification_not_enabled(self):
        for status,eligible in [('allowed',True),('eligible',False),('disallowed',False),('unknown',False)]:
            with self.subTest(status=status):
                result,_=self.probe(channel={'items':[{'id':CRED.channel_id,'status':{'longUploadsStatus':status}}]})
                self.assertEqual(result['eligible'],eligible)
                if status=='eligible':self.assertIn('手机验证',result['reason'])

    def test_valid_probe_does_not_claim_thumbnail_permission(self):
        result,session=self.probe()
        self.assertTrue(result['eligible'])
        self.assertEqual(result['thumbnail_permission'],'unverified')
        self.assertTrue(result['comment_eligible'])
        self.assertEqual(session.post.call_count,1)
        self.assertEqual(session.get.call_count,1)
        session.close.assert_called_once()

    def test_disabled_missing_refresh_and_scopes_do_not_call_network(self):
        for credential in [replace(CRED,channel_status=0),replace(CRED,refresh_token=''),replace(CRED,scopes=frozenset({'youtube.readonly'}))]:
            result,session=self.probe(credential=credential)
            self.assertFalse(result['eligible'])
            session.post.assert_not_called()

    def test_refresh_revoked_vs_transient(self):
        for status,code,expected in [(400,'invalid_grant','blocked'),(500,'backendError','unknown')]:
            result,session=self.probe(token_status=status,token={'error':code})
            self.assertEqual(result['auth_status'],expected)
            session.get.assert_not_called()

    def test_live_scope_reduction_and_channel_mismatch(self):
        result,_=self.probe(token={'access_token':'SECRET','scope':'https://www.googleapis.com/auth/youtube.readonly'})
        self.assertFalse(result['eligible'])
        result,_=self.probe(channel={'items':[{'id':'wrong','status':{'longUploadsStatus':'allowed'}}]})
        self.assertFalse(result['eligible'])

    def test_network_exception_is_unknown_and_secret_free(self):
        import requests
        session=Mock();session.post.side_effect=requests.Timeout('SECRET')
        result=ChannelProbe(lambda:session)(CRED)
        self.assertEqual(result['auth_status'],'unknown');self.assertNotIn('SECRET',str(result))


class DirectoryTests(unittest.TestCase):
    def test_nonblocking_singleflight_all_channels_and_partial_success(self):
        gate=threading.Event();repo=Mock();repo._query.return_value=[CRED,replace(CRED,channel_local_id='259',channel_status=0)]
        def probe(cred):
            gate.wait(1)
            return verdict('verified' if cred.channel_status else 'blocked','checked')
        directory=ChannelDirectory(repo,probe=probe)
        start=time.monotonic();first=directory.options()
        self.assertLess(time.monotonic()-start,.1);self.assertTrue(first['checking'])
        for _ in range(10):directory.options(refresh=True)
        gate.set();wait_until(lambda:not directory.running)
        rows=directory.options()['channels']
        self.assertEqual(len(rows),2);self.assertEqual(sum(r['eligible'] for r in rows),1)
        self.assertEqual(repo._query.call_count,1)
        self.assertNotIn('SECRET',json.dumps(rows));self.assertNotIn('youtube_account_id',rows[0])

    def test_submit_ignores_valid_cache_and_reads_new_credentials(self):
        repo=Mock();repo._query.return_value=[CRED]
        probe=Mock(return_value=verdict('verified','ok',comment_eligible=True))
        directory=ChannelDirectory(repo,probe=probe)
        directory.options();wait_until(lambda:not directory.running)
        probe.return_value=verdict('blocked','revoked')
        with self.assertRaises(WorkflowError):directory.validate({},'258')
        self.assertEqual(probe.call_count,2)
        self.assertIn('ch.id AS UNSIGNED)=258',repo._query.call_args.args[0])

    def test_known_forbidden_still_blocks_good_oauth_and_comment_requires_scope(self):
        repo=Mock();repo._query.return_value=[CRED]
        directory=ChannelDirectory(repo,probe=lambda _:verdict('verified','ok'),failures=lambda:{('258',CRED.channel_id):'2026-09-11T02:13:02Z'})
        with self.assertRaisesRegex(WorkflowError,'403 forbidden'):directory.validate({},'258')
        directory.failures=lambda:{}
        with self.assertRaisesRegex(WorkflowError,'首评'):directory.validate({},'258',comment=True)

    def test_enqueue_keeps_frozen_account_identity(self):
        repo=Mock();repo._query.return_value=[replace(CRED,account_id='251')]
        directory=ChannelDirectory(repo,probe=Mock())
        with self.assertRaisesRegex(WorkflowError,'身份已变化'):
            directory.validate({},'258',expected={'youtube_account_id':'250','channel_id':CRED.channel_id})
        directory.probe.assert_not_called()

    def test_new_failure_blocks_existing_snapshot_immediately(self):
        repo=Mock();repo._query.return_value=[CRED]
        directory=ChannelDirectory(repo,probe=lambda _:verdict('verified','ok'))
        directory.options();wait_until(lambda:not directory.running)
        self.assertTrue(directory.options()['channels'][0]['eligible'])
        directory.failures=lambda:{('258',CRED.channel_id):'2026-09-11T02:13:02Z'}
        self.assertFalse(directory.options()['channels'][0]['eligible'])


class CacheTests(unittest.TestCase):
    def test_nonblocking_duplicate_search_and_independent_keys(self):
        gate=threading.Event();loader=Mock(side_effect=lambda:gate.wait(1) and [{'id':'1'}])
        cache=ReadCache()
        start=time.monotonic()
        for _ in range(10):self.assertTrue(cache.read(('SQL','search'),loader)[1]['refreshing'])
        self.assertLess(time.monotonic()-start,.1)
        gate.set();wait_until(lambda:not cache.running)
        rows,meta=cache.read(('SQL','search'),loader);rows[0]['id']='changed'
        self.assertEqual(cache.read(('SQL','search'),loader)[0][0]['id'],'1')
        self.assertEqual(loader.call_count,1)
        cache.read(('SQL','other'),lambda:[{'id':'2'}]);wait_until(lambda:not cache.running)
        self.assertEqual(cache.read(('SQL','search'),loader)[0][0]['id'],'1')

    def test_stale_while_refresh_and_hard_expiry(self):
        now=[1.];cache=ReadCache(clock=lambda:now[0],ttl=5,max_age=10)
        cache.read('a',lambda:[1]);wait_until(lambda:not cache.running)
        gate=threading.Event();now[0]=7
        rows,meta=cache.read('a',lambda:gate.wait(1) and [2])
        self.assertEqual(rows,[1]);self.assertTrue(meta['stale']);self.assertTrue(meta['refreshing'])
        now[0]=12
        self.assertEqual(cache.read('a',lambda:[3])[0],[])
        gate.set();wait_until(lambda:not cache.running)
        self.assertEqual(cache.read('a',lambda:[3])[0],[2])

    def test_sql_change_never_serves_old_scope_and_cache_is_bounded(self):
        cache=ReadCache(capacity=2)
        for key in ['sql1','sql2','sql3']:
            self.assertEqual(cache.read(key,lambda:[key])[0],[])
            wait_until(lambda:not cache.running)
        self.assertEqual(len(cache.entries),2)

    def test_failed_refresh_keeps_warm_rows_and_cold_failure_is_explicit(self):
        cache=ReadCache();cache.read('a',lambda:[1]);wait_until(lambda:not cache.running)
        def failure():raise RuntimeError('SECRET')
        cache.read('a',failure,refresh=True);wait_until(lambda:not cache.running)
        rows,meta=cache.read('a',failure)
        self.assertEqual(rows,[1]);self.assertNotIn('SECRET',str(meta))
        cache.read('b',failure);wait_until(lambda:not cache.running)
        with self.assertRaises(WorkflowError):cache.read('b',failure)


class FailureEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.main=root/'main.sqlite3';self.outbox=root/'notice.sqlite3'
        with closing(sqlite3.connect(self.main)) as db, db:
            db.execute('CREATE TABLE drama_youtube_publish(id INTEGER PRIMARY KEY,preparation_id TEXT,channel_local_id TEXT,channel_id TEXT,thumbnail_status TEXT,error_code TEXT,error_message TEXT,updated_at_utc TEXT,app_id TEXT,workflow TEXT)')
            db.execute('CREATE TABLE drama_youtube_publish_event(task_id INTEGER,phase TEXT,outcome TEXT,created_at_utc TEXT)')
            db.execute('INSERT INTO drama_youtube_publish VALUES(1,?,?,?,?,?,?,?, ?,?)',('prep','258',CRED.channel_id,'unknown','youtube_video_reconcile_unknown','unknown','2026-09-11T10:00:00Z','1479','reviewed_thumbnail'))
        self.stamp='2026-09-11T02:13:02Z'
        self.key=('258',CRED.channel_id)
        with closing(sqlite3.connect(self.outbox)) as db, db:
            db.execute('CREATE TABLE failure_notice(payload TEXT,created_at REAL)')
            db.execute('INSERT INTO failure_notice VALUES(?,1)',(json.dumps(dict(task_id='prep',code='youtube_thumbnail_set_failed',message='HTTP 403 (forbidden)',time=self.stamp)),))
            db.executemany('INSERT INTO failure_notice VALUES(?,?)',[(json.dumps({'code':'irrelevant'}),x) for x in range(2,2105)])
        self.failures=ChannelFailures(self.main,self.outbox)

    def test_old_error_survives_new_records_and_old_success_later_ledger_update(self):
        with closing(sqlite3.connect(self.main)) as db, db:
            db.execute("UPDATE drama_youtube_publish SET thumbnail_status='succeeded'")
            db.execute("INSERT INTO drama_youtube_publish_event VALUES(1,'thumbnail','succeeded','2026-09-11T01:00:00Z')")
        self.assertEqual(self.failures(),{self.key:self.stamp})
        with closing(sqlite3.connect(self.main)) as db, db:
            db.execute("INSERT INTO drama_youtube_publish_event VALUES(1,'thumbnail','succeeded','2026-09-11T03:00:00Z')")
        self.assertEqual(self.failures(),{})

    def test_admin_explicit_verification_audit_and_new_error_fence(self):
        repo=Mock();repo._query.return_value=[CRED]
        directory=ChannelDirectory(repo,failures=self.failures,probe=lambda _:verdict('verified','ok'))
        actor={'role':'admin','tenant_key':'tenant','user_id':'operator'}
        payload={'channel_id':'258','failure_at':self.stamp,'confirmation':'studio_thumbnail_verified'}
        with self.assertRaises(WorkflowError):directory.verify_thumbnail(dict(actor,role='user'),payload)
        with self.assertRaises(WorkflowError):directory.verify_thumbnail(actor,dict(payload,confirmation=''))
        with self.assertRaises(WorkflowError):directory.verify_thumbnail(actor,dict(payload,failure_at='2099-01-01T00:00:00Z'))
        self.assertFalse(self.failures.audit_path.exists())
        self.assertTrue(directory.verify_thumbnail(actor,payload)['verified'])
        self.assertEqual(self.failures(),{})
        directory.verify_thumbnail(actor,payload)
        with closing(sqlite3.connect(self.failures.audit_path)) as db, db:self.assertEqual(db.execute('SELECT count(*) FROM thumbnail_verification').fetchone()[0],1)
        with closing(sqlite3.connect(self.outbox)) as db, db:
            db.execute('INSERT INTO failure_notice VALUES(?,9999)',(json.dumps(dict(task_id='prep',code='youtube_thumbnail_set_failed',message='HTTP 403 (forbidden)',time='2026-09-11T03:00:00Z')),))
        self.assertTrue(self.failures())
        with self.assertRaises(WorkflowError):directory.verify_thumbnail(actor,payload)

    def test_human_confirmation_does_not_bypass_current_failed_oauth(self):
        repo=Mock();repo._query.return_value=[CRED]
        directory=ChannelDirectory(repo,failures=self.failures,probe=lambda _:verdict('blocked','revoked'))
        with self.assertRaisesRegex(WorkflowError,'revoked'):
            directory.verify_thumbnail({'role':'admin'},dict(channel_id='258',failure_at=self.stamp,confirmation='studio_thumbnail_verified'))
        self.assertFalse(self.failures.audit_path.exists())


class SubmissionTests(unittest.TestCase):
    def test_new_submit_and_enqueue_validate_but_idempotent_read_does_not(self):
        fixture=workflow_fixture.WorkflowCase()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        directory=Mock();directory.validate.return_value=copy.deepcopy(fixture.channels(fixture.actor)[0])
        fixture.service.channel_directory=directory
        value=fixture.ready()
        fixture.assertEqual(directory.validate.call_count,1)
        fixture.service.review(fixture.actor,value['id'],{'action':'approve','version':1})
        directory.validate.side_effect=WorkflowError('channel_unavailable','revoked',409)
        fixture.service.run_once();fixture.service.run_once()
        fixture.assertEqual(fixture.store.enqueues,[])
        fixture.assertEqual(fixture.service.get_task(fixture.actor,value['id'])['task']['status'],'enqueue_failed')


if __name__=='__main__':unittest.main()
