"""Read-only cross-date accounting regressions; no publisher/network writers."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from contextlib import contextmanager, closing

from features.post_daily_report.accounting import effective_state, daily_override, backlog, load_dispatch
from features.post_daily_report.common import Window
from features.post_daily_report.fb_video import collect_video
from features.post_daily_report.plans import collect_plan_gaps, _CACHE
from features.post_daily_report.report import total, build_card, build_compact_card
from features.pipeline_dispatch.core import DispatchStore
from features.pipeline_dispatch import views


@contextmanager
def connection(path):
    with closing(sqlite3.connect(path)) as db:
        with db:
            yield db


class SharedStateTests(unittest.TestCase):
    def setUp(self):
        self.now=Window.for_date('2026-11-04').cutoff
        self.context={'available':True,'jobs':{},'publications':{},'blocked_resources':set(),'warnings':[]}

    def add(self,key,status='failed',stage='prepare',**values):
        row=dict(id=1,stage=stage,status=status,source_status='planned',hold_reason='',error_code='',scheduled_at='2026-11-03T00:00:00+00:00',resource_keys_json='[]')
        row.update(values);self.context['jobs'].setdefault(key,[]).append(row)

    def test_claim_rejection_is_blocked_and_receipt_wins(self):
        self.add('fb:auto:1',error_code='canonical_task_not_claimable')
        self.assertEqual(daily_override(self.context,'fb:auto:1',self.now)[0:2],('blocked','canonical_task_not_claimable'))
        self.context['publications']['fb:auto:1']={'publish_id':'confirmed'}
        self.assertEqual(effective_state(self.context,'fb:auto:1',self.now)['state'],'published')

    def test_complete_without_receipt_unknown_and_explicit_close_excluded(self):
        self.add('fb:video:1','completed','publish')
        self.add('fb:video:2','completed','publish',source_status='cancelled')
        self.assertEqual(effective_state(self.context,'fb:video:1',self.now)['state'],'unknown')
        self.assertEqual(effective_state(self.context,'fb:video:2',self.now)['state'],'closed')

    def test_unique_target_and_future_appointment_not_due_backlog(self):
        self.add('fb:auto:1','completed')
        self.add('fb:auto:1','queued','publish',id=2,hold_reason='canonical_link_pending')
        self.add('fb:auto:2','queued',scheduled_at='2026-11-06T00:00:00+00:00')
        self.add('fb:auto:3','completed','publish',source_status='skipped')
        plans={'available':True,'items':[{'channel':'fb','code':'plan_not_enqueued'}], 'warnings':[]}
        with patch('features.post_daily_report.plans.collect_plan_gaps',return_value=plans):
            row=backlog({},context=self.context,cutoff=self.now,channels=['fb'])['channels'][0]
        self.assertEqual((row['instantiated_unfinished'],row['unqueued'],row['unfinished'],row['scheduled'],row['closed']),(1,1,2,1,1))

    def test_private_scope_never_leaks_other_owner_or_plan_ids(self):
        self.add('fb:auto:mine');self.add('fb:auto:secret')
        result=backlog({},context=self.context,cutoff=self.now,visible_keys={'fb:auto:mine'},include_plans=False,channels=['fb'])
        self.assertNotIn('secret',json.dumps(result))
        self.assertEqual(result['instantiated_unfinished'],1)
        self.assertIsNone(result['unqueued'])

    def test_closed_plan_not_reported_as_unfinished(self):
        self.assertEqual(total([dict(expected=5,published=1,late=1,policy_skipped=2)])['missing'],1)

    def test_archive_and_bridged_ancestor_do_not_inflate_backlog(self):
        self.add('tt:auto:1','archived',hold_reason='backlog_archived')
        self.assertEqual(effective_state(self.context,'tt:auto:1',self.now)['state'],'closed')
        self.add('tt:intake:2','completed',source_status='ready')
        self.context['aliases']={'tt:intake:2':'tt:queue:3'}
        self.assertIsNone(effective_state(self.context,'tt:intake:2',self.now))
        self.add('youtube:creation:4','completed',channel='youtube')
        self.assertIsNone(effective_state(self.context,'youtube:creation:4',self.now))


class ManualVideoTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row;self.addCleanup(self.db.close)
        self.db.executescript('CREATE TABLE fb_video_task(task_id TEXT,page_id TEXT,source_kind TEXT,status TEXT,graph_post_id TEXT,unknown_outcome INTEGER,planned_at_utc TEXT,link_state TEXT,prepare_state TEXT); CREATE TABLE fb_video_attempt(task_id TEXT,result_kind TEXT,graph_post_id TEXT,created_at_utc TEXT);')
        self.window=Window.for_date('2026-11-04')
        self.context={'jobs':{},'publications':{}}

    def task(self,key,due,status='queued',post='',unknown=0):
        self.db.execute('INSERT INTO fb_video_task VALUES(?,?,?,?,?,?,?,?,?)',(key,'page','manual',status,post,unknown,due,'pending','ready'))

    def receipt(self,key,at,post='post'):
        self.db.execute('INSERT INTO fb_video_attempt VALUES(?,?,?,?)',(key,'success',post,at))

    def test_seven_manual_plans_and_eight_prior_confirmations(self):
        for n in range(7):self.task('q'+str(n),'2026-11-04T01:00:00Z')
        for n in range(8):
            key='p'+str(n);post='post'+str(n)
            self.task(key,'2026-11-03T01:00:00Z','published',post)
            self.receipt(key,'2026-11-04T02:00:00Z',post)
            self.receipt(key,'2026-11-04T02:01:00Z',post)
        rows,prior=collect_video(self.db,self.window,self.context,set())
        self.assertEqual((sum(r['expected'] for r in rows),sum(r['blocked'] for r in rows),prior),(7,7,8))

    def test_accepted_unknown_duplicate_and_cutoff_never_inflate_success(self):
        for key,status,unknown in [('submitted','submitted',0),('unknown','published',1),('ok','published',0),('duplicate','published',0),('late','published',0)]:
            self.task(key,'2026-11-04T01:00:00Z',status,'late-post' if key=='late' else 'post',unknown)
            self.receipt(key,'2026-11-05T02:01:00Z' if key=='late' else '2026-11-04T02:00:00Z','late-post' if key=='late' else 'post')
        rows,prior=collect_video(self.db,self.window,self.context,set());r=rows[0]
        self.assertEqual((r['expected'],r['published'],r['late'],r['unknown']),(5,1,0,3))


class MonitorIntegrationTests(unittest.TestCase):
    def test_old_failed_task_visible_scoped_and_readonly(self):
        with tempfile.TemporaryDirectory() as folder:
            now=dt.datetime(2026,11,5,3,tzinfo=dt.timezone.utc)
            store=DispatchStore(Path(folder)/'dispatch.db',clock=lambda:now-dt.timedelta(days=1))
            for owner in ('alice','bob'):
                job=store.offer(dict(source_key='fb:auto:'+owner,channel='fb',family='auto',stage='prepare',status='queued',owner_user_id=owner,owner_tenant_key='tenant',required_module='fb_page_posts',resource_keys=['page:'+owner]))
                with store.transaction() as db:
                    db.execute("UPDATE pipeline_job SET status='failed',source_status='planned',error_code='canonical_task_not_claimable',finished_at=created_at WHERE id=?",(job['id'],))
            store.clock=lambda:now
            actor=dict(is_admin=False,user_id='alice',tenant_key='tenant',allowed_channels=['fb'],allowed_modules=['fb_page_posts'])
            with closing(store.connect()) as db:before=[tuple(r) for r in db.execute('SELECT * FROM pipeline_job')]
            data=views.query(store,'summary',actor,{'window':'today'})
            tasks=views.query(store,'tasks',actor,{'window':'today'})
            self.assertEqual(tasks['total'],1)
            self.assertEqual(tasks['items'][0]['effective_state'],'blocked')
            self.assertEqual(data['backlog']['channels'][0]['states'],{'blocked':1})
            self.assertNotIn('bob',json.dumps(data)+json.dumps(tasks))
            with closing(store.connect()) as db:after=[tuple(r) for r in db.execute('SELECT * FROM pipeline_job')]
            self.assertEqual(before,after)

    def test_historical_reconstruction_does_not_backdate_later_closure(self):
        with tempfile.TemporaryDirectory() as folder:
            now=dt.datetime(2026,11,5,3,tzinfo=dt.timezone.utc)
            store=DispatchStore(Path(folder)/'dispatch.db',clock=lambda:now-dt.timedelta(days=1))
            job=store.offer(dict(source_key='fb:auto:1',channel='fb',family='auto',stage='prepare',status='queued'))
            with store.transaction() as db:
                db.execute("UPDATE pipeline_job SET source_status='cancelled',status='completed',updated_at=? WHERE id=?",(now.isoformat(),job['id']))
            context=load_dispatch({'dispatch':store.path},now-dt.timedelta(hours=1))
            self.assertNotEqual(effective_state(context,'fb:auto:1',now)['state'],'closed')


class PlanTests(unittest.TestCase):
    def test_cancellation_and_compensation_do_not_create_duplicate_gaps(self):
        from tests.test_post_daily_report_x import PRIMARY_SCHEMA, AUTO_SCHEMA
        with tempfile.TemporaryDirectory() as folder:
            paths={k:Path(folder)/(k+'.db') for k in ('x','x_auto')}
            with connection(paths['x']) as db:
                db.executescript(PRIMARY_SCHEMA+'CREATE TABLE x_post_unqueued_cancellation(run_id INTEGER,account_id TEXT,created_at TEXT);')
                db.execute("INSERT INTO x_post_schedule_run VALUES(1,'material','2026-11-04','09:00','[1,2,3]','completed','','random')")
                db.execute("INSERT INTO x_post_schedule_random_plan VALUES('material','2026-11-04','[1,2,3]','[\"09:00\"]')")
                db.execute("INSERT INTO x_post_queue(id,account_id,schedule_run_id,status) VALUES(1,1,1,'failed')")
                db.execute("INSERT INTO x_post_unqueued_cancellation VALUES(1,'2','2026-11-04T10:00:00Z')")
            with connection(paths['x_auto']) as db:db.executescript(AUTO_SCHEMA)
            _CACHE.clear();result=collect_plan_gaps(paths,Window.for_date('2026-11-04').cutoff,['x'])
            self.assertTrue(result['available'])
            self.assertEqual({(r['account_id'],r['closed']) for r in result['items']},{('2',True),('3',False)})

    def test_frozen_fixed_slot_without_run_included_until_disable(self):
        from tests.test_post_daily_report_x import PRIMARY_SCHEMA, AUTO_SCHEMA
        with tempfile.TemporaryDirectory() as folder:
            paths={k:Path(folder)/(k+'.db') for k in ('x','x_auto')}
            with connection(paths['x']) as db:db.executescript(PRIMARY_SCHEMA)
            with connection(paths['x_auto']) as db:
                db.executescript(AUTO_SCHEMA)
                db.execute("INSERT INTO x_auto_template VALUES(1,0,'2026-11-04T00:00:00Z')")
                db.execute('INSERT INTO x_auto_template_version VALUES(1,1,?)',(json.dumps({'account_ids':[1,2],'schedule':{'mode':'fixed','times':['09:00','11:00']}}),))
                db.execute("INSERT INTO x_auto_event VALUES('template_enabled','{\"template_id\":1,\"version\":1}','2026-11-04T00:00:00Z')")
                db.execute("INSERT INTO x_auto_event VALUES('template_disabled','{\"template_id\":1,\"version\":1}','2026-11-04T02:00:00Z')")
            _CACHE.clear();result=collect_plan_gaps(paths,Window.for_date('2026-11-04').cutoff,['x'])
            self.assertEqual(len(result['items']),2)
            self.assertTrue(all('09:00' in r['plan_key'] for r in result['items']))


if __name__=='__main__':unittest.main()
