import copy
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch

from features.fb_auto_posts.core import FBAutoPostStore,ActorScope
from features.fb_auto_posts.effects import choose_material,write_effect,load_scores
from features.fb_auto_posts.effect_collector import GatedReader,parse_metrics,graph_created_at,FeedbackError
from features.fb_auto_posts.strategy import daily_capacity,slot_indices,planned_time,cooldown_content_ids
from features.fb_auto_posts.validation import normalize_template_payload,ValidationError
from scripts.test_fb_auto_validation import payload
from scripts.test_fb_page_languages import page,material,Pages,Materials

UTC=timezone.utc
NOW=datetime(2026,9,22,tzinfo=UTC)


def config():
    raw=payload()
    raw.update(drama_cooldown_hours=24,default_daily_count=0,
        page_daily_limits=[{'page_id':'101','daily_count':5,'tier':'A','name':'First'}],
        stagger_minutes=40,feedback_selection={'enabled':True,'rollout_percent':50,'exploration_percent':20})
    raw['schedule']={'mode':'fixed','times':['09:15','12:15','15:15','18:15','21:15']}
    return raw


class StrategyRulesTests(unittest.TestCase):
    def test_frequency_preserves_526_ceiling_and_holds_unlisted_pages(self):
        raw=config();raw['page_daily_limits']=[];pages=[]
        for i,count in enumerate([5]*6+[4]*110+[2]*28+[0],101):
            raw['page_daily_limits'].append({'page_id':str(i),'daily_count':count})
            pages.append(page(i,'en'))
        normalized=normalize_template_payload(raw)
        self.assertEqual(daily_capacity(normalized,pages),526)
        self.assertEqual(len(slot_indices(normalized,'101')),5)
        self.assertEqual(slot_indices(normalized,'99999'),set())
        for row in normalized['page_daily_limits']:
            self.assertEqual(len(slot_indices(normalized,row['page_id'])),row['daily_count'])

    def test_invalid_frequency_and_overlapping_windows_fail(self):
        for field,value in [('drama_cooldown_hours',24.5),('default_daily_count',6),('stagger_minutes',True)]:
            raw=config();raw[field]=value
            with self.assertRaises(ValidationError):normalize_template_payload(raw)
        raw=config();raw['page_daily_limits']*=2
        with self.assertRaises(ValidationError):normalize_template_payload(raw)
        raw=config();raw['schedule']['times']=['23:30'];raw['page_daily_limits'][0]['daily_count']=1
        with self.assertRaises(ValidationError):normalize_template_payload(raw)

    def test_stagger_is_reproducible_within_slot_and_manual_is_unshifted(self):
        raw=config();stamp='2026-09-23T01:15:00+00:00'
        values={planned_time(raw,str(i),'auto:2026-09-23:09:15',stamp) for i in range(101,200)}
        self.assertGreater(len(values),80)
        self.assertTrue(all(stamp<=s<='2026-09-23T01:55:00+00:00' for s in values))
        self.assertEqual(planned_time(raw,'101','manual:1',stamp,manual=True),stamp)

    def test_feedback_does_not_turn_missing_fields_into_zero(self):
        with self.assertRaises(FeedbackError):parse_metrics({'insights':{'data':[]}})
        self.assertEqual(parse_metrics({'insights':{'data':[{'name':'post_media_view','values':[{'value':12}]},{'name':'post_clicks_by_type','values':[{'value':{}}]}]}})['link_clicks'],0)

    def test_gated_reader_disallows_write_endpoint_and_unsafe_sql(self):
        with self.assertRaises(FeedbackError):GatedReader({'FB_AUTO_MYSQL_HOST':'101.32.56.53','FB_AUTO_MYSQL_PORT':'63353'})
        reader=GatedReader({'FB_AUTO_MYSQL_HOST':'101.32.56.53','FB_AUTO_MYSQL_PORT':'63350'})
        with self.assertRaises(FeedbackError):reader.query('SELECT 1; DELETE FROM anything')

    def test_graph_created_time_accepts_meta_offsets_and_rejects_missing_timezone(self):
        for value in ('2026-09-01T06:04:25+0000','2026-09-01T06:04:25Z','2026-09-01T14:04:25+08:00'):
            self.assertEqual(graph_created_at(value),'2026-09-01T06:04:25+00:00')
        for value in (None,'','2026-09-01T06:04:25','not-a-date'):
            with self.assertRaises(FeedbackError):graph_created_at(value)


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.now=NOW
        self.store=FBAutoPostStore(Path(self.tmp.name)/'s.db',now_fn=lambda:self.now)
        self.actor=ActorScope('test','test',True,'248')
        self.raw=config();self.raw['stagger_minutes']=0
        self.template=self.store.create_template(self.raw,self.actor)
        self.store.set_template_status(self.template['id'],True,self.actor,1)
        self.pages=Pages([page('101','en')])
        self.materials=Materials({'en':tuple(replace(material(str(i),'en'),content_id='drama'+str(i//10)) for i in range(100,300))})

    def tearDown(self):self.tmp.cleanup()

    def plan(self,at):
        dt=datetime.fromisoformat(at)
        minute=dt.astimezone(timezone(timedelta(hours=8))).strftime('%H:%M')
        key='auto:v1:'+dt.astimezone(timezone(timedelta(hours=8))).date().isoformat()+':'+minute
        with self.store.connect() as conn:
            conn.execute("INSERT INTO fb_auto_due_slot(template_id,template_version,slot_key,planned_publish_at_utc,status,created_at_utc,updated_at_utc) VALUES(?,1,?,?,'pending',?,?)",(self.template['id'],key,at,self.now.isoformat(),self.now.isoformat()))
        due=self.store.claim_due_slot('p')
        result=self.store.create_run(self.template['id'],key,'auto',self.actor,self.pages,self.materials,
            planned_publish_at_utc=at,expected_template_version=1,expected_due_id=due['id'],
            expected_due_lease_owner=due['lease_owner'],expected_due_lease_expires_at_utc=due['lease_expires_at_utc'])
        self.store.complete_due_slot(due['id'],run_id=result['run_id'])
        with self.store.connect() as conn:return dict(conn.execute('SELECT * FROM fb_auto_task WHERE run_id=?',(result['run_id'],)).fetchone())

    def test_five_slots_choose_five_dramas_and_never_reuse_material(self):
        rows=[self.plan('2026-09-23T'+hour+':15:00+00:00') for hour in ['01','04','07','10','13']]
        self.assertEqual({r['status'] for r in rows},{'planned'})
        self.assertEqual(len({r['content_id'] for r in rows}),5)
        self.assertEqual(len({r['material_id'] for r in rows}),5)

    def test_out_of_order_future_reservation_blocks_same_drama(self):
        later=self.plan('2026-09-24T01:15:00+00:00')
        earlier=self.plan('2026-09-23T13:15:00+00:00')
        self.assertNotEqual(later['content_id'],earlier['content_id'])

    def test_exhausted_drama_is_skipped_without_cross_language(self):
        self.materials=Materials({'en':tuple(replace(material(str(i),'en'),content_id='only') for i in range(100,105)),'es':(material('999','es'),)})
        first=self.plan('2026-09-23T01:15:00+00:00')
        second=self.plan('2026-09-23T04:15:00+00:00')
        self.assertEqual(first['status'],'planned')
        self.assertEqual(second['status'],'skipped')
        self.assertEqual(second['skip_reason'],'fb_auto_no_video_after_drama_cooldown')

    def test_publish_guard_uses_actual_completion_time(self):
        prior=self.plan('2026-09-23T01:15:00+00:00')
        nextrow=self.plan('2026-09-24T04:15:00+00:00')
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_auto_task SET status='published',completed_at_utc='2026-09-23T05:15:00+00:00' WHERE id=?",(prior['id'],))
            conn.execute("UPDATE fb_auto_task SET status='ready',content_id=?,prepared_at_utc='2026-09-23T06:00:00+00:00',media_url='https://cdn.example/prepared.mp4',prepared_media_url='https://cdn.example/prepared.mp4' WHERE id=?",(prior['content_id'],nextrow['id']))
        self.now=datetime(2026,9,24,4,15,tzinfo=UTC)
        self.assertIsNone(self.store.claim_next('worker'))
        with self.store.connect() as conn:
            row=conn.execute('SELECT * FROM fb_auto_task WHERE id=?',(nextrow['id'],)).fetchone()
            self.assertEqual(row['skip_reason'],'fb_auto_drama_cooldown_at_publish')
            self.assertEqual(row['attempt_count'],0)

    def test_unknown_is_never_replayed_or_selected_around(self):
        first=self.plan('2026-09-23T01:15:00+00:00')
        with self.store.connect() as conn:conn.execute("UPDATE fb_auto_task SET status='unknown',unknown_outcome=1 WHERE id=?",(first['id'],))
        second=self.plan('2026-09-23T04:15:00+00:00')
        self.assertEqual(second['skip_reason'],'fb_auto_page_unknown_block')

    def test_feedback_snapshot_requires_real_post_identity(self):
        first=self.plan('2026-09-23T01:15:00+00:00')
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_auto_task SET status='published',graph_post_id='999',completed_at_utc=? WHERE id=?",(self.now.isoformat(),first['id']))
            conn.execute("INSERT INTO fb_auto_publish_ledger(task_id,page_id,material_id,status,graph_post_id,created_at_utc,updated_at_utc) VALUES(?,'101',?,'published','999',?,?)",(first['id'],first['material_id'],self.now.isoformat(),self.now.isoformat()))
            item={'page_id':'101','video_id':'999','post_id':'101_999','language':'en','created_at_utc':'2026-09-20T00:00:00+00:00','collected_at_utc':self.now.isoformat(),'media_views':100,'link_clicks':1}
            with self.assertRaises(ValueError):write_effect(conn,first['id'],item)
            item['post_id']='101_888'
            result=write_effect(conn,first['id'],item,stage='baseline')
            self.assertIsNone(result['dnu_revenue_d3'])
            self.assertEqual(load_scores(conn,normalize_template_payload(self.raw),self.now),{})


class SelectionTests(unittest.TestCase):
    def test_single_page_viral_sample_is_insufficient_and_arms_are_explicit(self):
        candidates=(material('1','en'),material('2','en'))
        materials=Materials({'en':candidates})
        raw=config();raw['feedback_selection']={'enabled':True,'rollout_percent':100,'exploration_percent':0}
        chosen,decision=choose_material(candidates,[],materials=materials,config=raw,page_id='101',slot_key='s',scores={})
        self.assertEqual(decision['arm'],'fallback')
        raw['feedback_selection']['exploration_percent']=100
        chosen,decision=choose_material(candidates,['1'],materials=materials,config=raw,page_id='101',slot_key='s',scores={})
        self.assertEqual(chosen.material_id,'2')
        self.assertEqual(decision['arm'],'explore')


if __name__=='__main__':unittest.main()
