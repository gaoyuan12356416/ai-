import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from features.tt_auto_posts.core import TTPostAutoStore
from features.tt_promotion import pub_copy
from features.tt_promotion.publishing import Publisher,PublicationError,day_slots,now,iso,initialize
from features.tt_promotion.pub_store import connection,TASK_BASE


class NativeFixture:
    def __init__(self,path):
        self.store=TTPostAutoStore(path)
        self.blocked=set()
        self.input_count=0
    def settings(self,acct):
        return {'privacy_level':'PUBLIC_TO_EVERYONE','allow_comment':False},1
    def conflicts(self,account,at):
        return ['旧模板占用'] if account in self.blocked else []
    def preview(self,t,ch,opts,seed,blocked):
        self.input_count+=1
        ids=opts.get('material_ids') or ['51','52','53']
        mid=opts.get('material_id') or next(i for i in ids if i not in blocked)
        return dict(content_id=opts.get('content_id') or 'cid',material_id=mid,drama_name='Moon',
                    description='Episode opening',reason='preferred_match',profile_version='one')
    def step(self):return {'claimed':False}


class PublisherTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'new.sqlite3')
        self.runtime=NativeFixture(self.path)
        self.p=Publisher(self.path,self.runtime)
        self.actor=dict(user_id='author',name='Operator',tenant_key='one',role='user')
        self.channels=[dict(account_id=i,enabled=True,eligible=True,drama_language='en') for i in ('12','13')]
        self.data=dict(name='test',account_ids=['12','13'],caption_template='{desc} {code}',
                       schedule=dict(mode='fixed',times=['01:00','22:10']),model='income',ai_copy=False)
        self.t=self.call('POST','/templates',self.data)
        self.base='/templates/'+str(self.t['id'])
    def call(self,method,path,payload=None,channels=None,actor=None):
        return self.p.handle(method,path,payload or {},actor or self.actor,self.channels if channels is None else channels)
    def rows(self,sql):
        with connection(self.path) as db:return [dict(r) for r in db.execute(sql)]
    def test_preview_has_no_reservation_copy_or_shortlink(self):
        self.runtime.blocked.add('13')
        rows=self.call('POST',self.base+'/preview',dict(expected_version=1))['items']
        self.assertEqual([r['status'] for r in rows],['eligible','skipped'])
        self.assertTrue(rows[1]['conflicts']);self.assertIn('〔正式四位码〕',rows[1]['caption'])
        self.assertFalse(self.rows('select * from ttp_slot'))
        self.assertFalse(self.rows('select * from ttp_copy'))
    def test_draft_and_scope_and_version(self):
        data=dict(expected_version=1,operation_id='draft-key',account_id='12',material_id='51')
        draft=self.call('POST',self.base+'/draft',data)
        self.assertEqual(draft['state'],'waiting')
        self.assertTrue(pub_copy.process_one(self.path,lambda _: 'New text {code}',table='ttp_draft'))
        got=self.call('GET',self.base+'/draft/'+draft['id'])
        self.assertEqual((got['source'],got['caption']),('ai','New text {code}'))
        self.assertFalse(self.rows('select * from ttp_slot'))
        with self.assertRaises(PublicationError):self.call('GET',self.base,channels=self.channels[:1])
        with self.assertRaises(PublicationError):self.call('GET',self.base,actor=dict(self.actor,tenant_key='else'))
        self.assertEqual(self.call('POST',self.base,dict(self.data,expected_version=1))['version'],2)
        with self.assertRaises(PublicationError):self.call('POST',self.base+'/run',dict(expected_version=1,operation_id='operation-id'))
    def test_one_receipt_and_independent_report_contract(self):
        args=dict(expected_version=1,account_ids=['12'],manual_caption='My text {code}',operation_id='operation-11')
        job=self.call('POST',self.base+'/run',args)
        self.assertEqual(job['id'],self.call('POST',self.base+'/run',args)['id'])
        for _ in range(3):self.p.work(lambda actor:self.channels)
        tasks=self.rows('SELECT * FROM tt_promotion_report_task')
        self.assertEqual(len(tasks),1)
        self.assertGreater(tasks[0]['id'],TASK_BASE)
        self.assertEqual(tasks[0]['template_creator_user_id'],'author')
        self.assertEqual(tasks[0]['template_tenant'],'one')
        self.assertEqual(tasks[0]['route_queue_id'],7_000_000_000_000_000_000+tasks[0]['id'])
        self.assertEqual(self.rows('SELECT * FROM tt_promotion_report_plan')[0]['task_id'],tasks[0]['id'])
        self.assertEqual(self.rows('SELECT source FROM ttp_copy')[0]['source'],'manual')
        with self.assertRaises(PublicationError):self.call('POST',self.base+'/run',dict(args,manual_caption='Different'))
    def test_conflict_is_local_and_no_double_reservation(self):
        self.runtime.blocked.add('12')
        self.call('POST',self.base+'/run',dict(expected_version=1,operation_id='with-conflict'))
        self.p.work(lambda actor:self.channels)
        states=self.rows('select account_id,state,reason from ttp_slot order by account_id')
        self.assertEqual([r['state'] for r in states],['skipped','reserved'])
        self.assertFalse(self.rows('SELECT * FROM tt_auto_task'))
    def test_ai_failure_is_single_retry_and_fallback(self):
        self.call('POST',self.base,dict(self.data,expected_version=1,ai_copy=True))
        self.call('POST',self.base+'/run',dict(expected_version=2,operation_id='ai-automatic',account_ids=['12']))
        self.p.work(lambda actor:self.channels)
        def fail(facts):raise ValueError('bad')
        pub_copy.process_one(self.path,fail)
        with connection(self.path,write=True) as d:d.execute("update ttp_copy set next_at=''")
        pub_copy.process_one(self.path,fail)
        self.assertFalse(pub_copy.process_one(self.path,fail))
        self.assertEqual(self.rows('select state,attempts,source from ttp_copy'),[dict(state='final',attempts=2,source='template')])
    def test_scheduler_reentry_and_draft_default(self):
        self.p.schedule();self.assertFalse(self.rows('select * from ttp_job'))
        self.call('POST',self.base+'/enable',dict(expected_version=1))
        self.p.schedule()
        count=len(self.rows('select * from tt_promotion_report_plan'))
        self.p.schedule();self.assertEqual(len(self.rows('select * from tt_promotion_report_plan')),count)
        self.call('POST',self.base+'/disable',dict(expected_version=1))
        for r in self.rows('select slot_state from tt_promotion_report_plan'):self.assertEqual(r['slot_state'],'skipped')
        self.assertEqual(self.rows('select seq from sqlite_sequence where name=\'tt_auto_task\'')[0]['seq'],TASK_BASE)
        initialize(self.path)
    def test_day_preview_is_read_only_and_pages_account_slots(self):
        data=dict(expected_version=1,plan_date='2026-12-10',offset=1,limit=1)
        first=self.call('POST',self.base+'/preview',data)
        self.assertEqual(first['total'],4)
        self.assertEqual(len(first['items']),1)
        self.assertEqual(first,self.call('POST',self.base+'/preview',data))
        self.assertFalse(self.rows('SELECT * FROM ttp_day'))
        self.assertFalse(self.rows('SELECT * FROM ttp_job'))

    def test_other_template_receipt_never_advances_series(self):
        self.call('POST',self.base+'/run',dict(expected_version=1,account_ids=['13'],mode='material',
                        material_id='51',operation_id='old-operation'))
        self.p.work(lambda actor:self.channels)
        with connection(self.path,write=True) as db:
            db.execute("INSERT INTO ttp_series VALUES('one','cid',?,'12','[\"51\",\"52\"]')",(self.t['id'],))
            series=db.execute('SELECT * FROM ttp_series').fetchone()
            with self.assertRaisesRegex(PublicationError,'其他任务占用'):
                self.p._series_state(db,series,['51','52'])
    def test_invalid_targets_and_caption(self):
        with self.assertRaises(PublicationError):self.call('POST','/templates',dict(self.data,account_ids=['99']))
        with self.assertRaises(PublicationError):self.call('POST','/templates',dict(self.data,caption_template='{wrong}'))
        with self.assertRaises(PublicationError):self.call('POST',self.base+'/preview',dict(expected_version=1,mode='serial',content_id='x',episodes=['2','2']))
        with self.assertRaises(PublicationError):self.call('GET','/runs/not-a-number')

if __name__=='__main__':unittest.main()
