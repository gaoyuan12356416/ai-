"""Waiting Pages keep their original FB slot without exposing a task to a publisher."""
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from features import social_random_schedule as daily
from features.fb_auto_posts import target_schedule, upcoming
from features.fb_auto_posts.core import ActorScope, FBAutoPostStore
from features.fb_auto_posts.repositories import CandidateSnapshot, MaterialCandidate, PageTarget, RepositoryError
from features.fb_auto_posts.service import Runtime
from features.moboreels_upcoming import Availability
from scripts.test_fb_auto_validation import payload


class Pages:
    def __init__(self):
        self.rows = [PageTarget('6', ('6',), pid, '248','UTC',lang,1)
                     for pid,lang in [('11','en'),('12','es')]]

    def list_pages(self, group_ids, **scope):
        return list(self.rows)

    def legacy_conflicts(self, groups):
        return []


class Materials:
    def __init__(self):
        self.mysql = SimpleNamespace(select=object(),schema='source')
        self.rows = {}

    def candidate_snapshot(self, config):
        ready = config['_mobo_reviews'][config['language']].ready
        rows = tuple(MaterialCandidate(str(mid),cid,'https://example.com/m.mp4','video','drama',lang,
                          Decimal(400),None,None,None,None,'moboreels',deploy_time=ready[cid]['at'])
                     for mid,(cid,lang) in self.rows.items() if cid in ready and lang == config['language'])
        return CandidateSnapshot(rows,(),())

    def choose_from(self, candidates, excluded):
        return next((item for item in candidates if item.material_id not in excluded),None)


class UpcomingPageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.now = datetime(2026,11,20,4,tzinfo=timezone.utc)
        self.store = FBAutoPostStore(Path(tmp.name)/'fb.sqlite3',now_fn=lambda:self.now)
        self.pages,self.materials = Pages(),Materials()
        raw = dict(payload(), message_template='{{m_url}}', material_products=['Moboreels'],
                   drama_launch_window_days='-1~0', cooldown_days=0, drama_cooldown_hours=0)
        self.actor = ActorScope('u','owner',False,'248')
        self.template=self.store.create_template(raw,self.actor,{'product':'Dramawave','app_id':'1479'})['id']
        self.store.set_template_status(self.template,True,self.actor,1)
        self.runtime=Runtime(self.store,self.pages,self.materials,SimpleNamespace(live_enabled=True),
                             SimpleNamespace(), 'TEST',prebuild_enabled=True)
        self.epoch=lambda:int(self.now.timestamp())
        self.states={'en':'ready','es':'pending'}
        self.story={'en':'100','es':'200'}
        self.materials.rows={501:('100','en')}
        def discover(_select,_schema,_window,now,**kwargs):
            return {'policy':'mobo_upcoming_v1','scanned_at':now,
                    'stories':{cid:{'language':language,'at':now} for language,cid in self.story.items()}}
        def assess(_select,_schema,plan,language,now):
            state=self.states[language]
            cid=self.story[language]
            self.assertIn(cid,plan['stories'])
            return Availability({cid:{'name':language,'at':now}} if state=='ready' else {},
                              {cid:now+3600} if state=='pending' else {},
                              now+3600 if state in ('ready','pending') else None)
        self.discover=self.enter(patch.object(upcoming.catalog,'discover',side_effect=discover))
        self.enter(patch.object(upcoming.catalog,'assess',side_effect=assess))

    def enter(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def manual(self,key='manual:one'):
        return self.store.create_run(self.template,key,'manual',self.actor,self.pages,self.materials)

    def auto(self,key,planned=None):
        when = (planned or self.now).isoformat(timespec='seconds')
        with self.store.connect() as conn:
            conn.execute("""INSERT INTO fb_auto_due_slot(template_id,template_version,slot_key,planned_publish_at_utc,
              status,created_at_utc,updated_at_utc,trigger_type) VALUES(?,1,?,?,'pending',?,?,'auto')""",
                (self.template,key,when,when,when))
        return self.runtime.plan_next('planner')

    def detail(self,rid):
        return self.store.get_run(rid,self.actor)['run']

    def test_partial_release_wait_no_material_then_fill_original_slot(self):
        self.runtime.max_publishable_pages = 1
        first=self.manual()
        rid=first['run_id']
        self.assertEqual(first['summary']['waiting_mobo'],1)
        self.assertEqual([r['page_id'] for r in self.detail(rid)['tasks']],['11'])
        self.assertEqual([r['page_id'] for r in self.detail(rid)['waiting_pages']],['12'])
        self.assertTrue(self.manual()['idempotent'])
        self.assertEqual(self.runtime.plan_next('planner')['status'],'no_due_slot')
        self.now+=timedelta(hours=1)
        self.states['es']='ready'
        result=self.runtime.plan_next('planner')
        self.assertEqual(result['run_id'],rid)
        self.assertEqual(result['summary']['waiting_mobo'],1)
        self.assertEqual(self.runtime.plan_next('planner')['status'],'no_due_slot')
        self.now+=timedelta(days=1)
        self.materials.rows[502]=('200','es')
        self.runtime.plan_next('planner')
        detail=self.detail(rid)
        self.assertEqual([(r['page_id'],r['material_id']) for r in detail['tasks']],[('11','501'),('12','502')])
        self.assertEqual(detail['tasks'][1]['planned_publish_at_utc'],self.now.isoformat(timespec='seconds'))
        self.assertNotIn('waiting_pages',detail)
        self.assertEqual(detail['queued_tasks'],2)
        self.assertEqual(detail['skipped_tasks'],0)
        self.assertEqual(self.discover.call_count,1)
        with self.store.connect() as c:
            frozen=json.loads(c.execute("SELECT moboreels_json FROM fb_auto_task WHERE page_id='12'").fetchone()[0])
            self.assertEqual(frozen['material']['content_id'],'200')
        self.assertEqual(self.runtime.plan_next('planner')['status'],'no_due_slot')

    def test_lease_race_and_changed_scope_only_affects_waiting_page(self):
        rid=self.manual()['run_id']
        self.now+=timedelta(hours=1)
        old=upcoming.claim(self.store,30)
        self.assertIsNone(upcoming.claim(self.store,30))
        self.now+=timedelta(seconds=31)
        fresh=upcoming.claim(self.store,30)
        with self.store.connect() as c:
            self.assertFalse(upcoming.owned(c,old,self.now))
            self.assertEqual(upcoming.owned(c,fresh,self.now),{'12'})
        self.pages.rows[1]=replace(self.pages.rows[1],language='ja')
        self.now+=timedelta(seconds=31)
        self.runtime.plan_next('planner')
        detail=self.detail(rid)
        self.assertEqual(len(detail['tasks']),1)
        self.assertNotIn('waiting_pages',detail)
        self.assertEqual(detail['skipped_pages'],[{'page_id':'12','reason':'fb_auto_page_scope_changed'}])

    def test_disable_cancels_manual_wait_and_does_not_touch_unknown(self):
        rid=self.manual()['run_id']
        with self.store.connect() as c:
            c.execute("UPDATE fb_auto_task SET status='unknown',unknown_outcome=1 WHERE page_id='11'")
        self.store.set_template_status(self.template,False,self.actor,1)
        self.store.set_template_status(self.template,True,self.actor,1)
        self.now+=timedelta(hours=2)
        self.assertEqual(self.runtime.plan_next('planner')['status'],'no_due_slot')
        detail=self.detail(rid)
        self.assertEqual(detail['tasks'][0]['status'],'unknown')
        self.assertNotIn('waiting_pages',detail)

    def test_cross_day_wait_and_new_automatic_slot_share_page_daily_limit(self):
        self.pages.rows=self.pages.rows[1:]
        a=self.auto('auto:day1')['run_id']
        self.now+=timedelta(days=1)
        self.states['es']='ready'
        self.materials.rows.update({502:('200','es'),503:('200','es')})
        b=self.auto('auto:day2')['run_id']
        self.assertEqual(self.detail(b)['tasks'][0]['status'],'planned')
        self.assertEqual(self.runtime.plan_next('planner')['run_id'],a)
        detail=self.detail(a)
        self.assertEqual(detail['tasks'][0]['status'],'skipped')
        self.assertEqual(detail['tasks'][0]['skip_reason'],'fb_auto_page_frequency_limit')

    def test_cancelled_schedule_skips_only_its_original_page(self):
        rid=self.manual()['run_id']
        self.states['es']='cancelled'
        self.now+=timedelta(hours=1)
        self.runtime.plan_next('planner')
        detail=self.detail(rid)
        self.assertEqual([r['status'] for r in detail['tasks']],['planned','skipped'])
        self.assertNotIn('waiting_pages',detail)
        self.assertEqual(detail['queued_tasks'],1)
        self.assertEqual(detail['skipped_tasks'],1)

    def test_language_source_timeout_is_visible_and_other_page_progresses(self):
        self.materials.now_fn = lambda:self.now
        select = self.materials.candidate_snapshot
        def unreliable(config):
            if config['language'] == 'es':
                raise RepositoryError('fb_auto_source_query_failed','查询超时')
            return select(config)
        with patch.object(self.materials,'candidate_snapshot',side_effect=unreliable):
            detail = self.detail(self.manual()['run_id'])
        self.assertEqual([t['page_id'] for t in detail['tasks']],['11'])
        self.assertIn('fb_auto_source_query_failed',detail['waiting_pages'][0]['reason'])

    def test_target_midnight_binds_waiting_page_only_after_material_is_ready(self):
        self.store.set_template_status(self.template,False,self.actor,1)
        raw=dict(payload(),message_template='{{m_url}}',material_products=['Moboreels'],drama_launch_window_days='-1~0',
                 schedule={'mode':'random','daily_count':1,'start':'08:00','end':'22:00'})
        self.store.update_template(self.template,raw,self.actor,1)
        self.store.set_template_status(self.template,True,self.actor,2)
        self.now=datetime(2026,11,20,16,tzinfo=timezone.utc)
        self.store.enqueue_due_slots(live_enabled=True,prebuild_days_ahead=1)
        rid=self.runtime.plan_next('planner')['run_id']
        source=target_schedule.source_key(self.template)
        with self.store.connect() as conn:
            day=conn.execute("SELECT slot_key FROM fb_auto_run WHERE id=?",(rid,)).fetchone()[0].split(':')[2]
            initial=daily.slot_binding(conn,source,day,'12',0)
        self.assertFalse(initial['native_id'])
        self.assertEqual([r['page_id'] for r in self.detail(rid)['waiting_pages']],['12'])
        self.now+=timedelta(hours=2)
        self.states['es']='ready'
        self.materials.rows[502]=('200','es')
        result=self.runtime.plan_next('planner')
        self.assertEqual(result['run_id'],rid)
        with self.store.connect() as conn:
            binding=daily.slot_binding(conn,source,day,'12',0)
            task=conn.execute("SELECT id,planned_publish_at_utc FROM fb_auto_task WHERE run_id=? AND page_id='12'",(rid,)).fetchone()
        self.assertEqual(binding['native_id'],str(task['id']))
        self.assertEqual(binding['planned_at'],initial['planned_at'])
        self.assertEqual(task['planned_publish_at_utc'],datetime.fromisoformat(initial['planned_at'].replace('Z','+00:00')).isoformat(timespec='seconds'))


class ReadyMaterialQueryTests(unittest.TestCase):
    def test_ready_ids_use_selected_mobo_family_and_confirm_both_languages(self):
        from tests.test_fb_mobo_launch_window import MoboLaunchWindowTests
        fixture = MoboLaunchWindowTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for mid in range(1, 6):
            fixture.story(mid, [fixture.now])
        fixture.db.execute('ALTER TABLE ads_ai.cdreader_koc_drama ADD COLUMN lang_id INTEGER DEFAULT 15')
        fixture.db.execute("UPDATE ads_custom_source SET language='fil'")
        fixture.db.execute("UPDATE ads_custom_source SET product='DramawaveSource' WHERE id=2")
        fixture.db.execute("UPDATE ads_custom_source SET language='de' WHERE id=3")
        fixture.db.execute("UPDATE ads_ai.cdreader_koc_drama SET lang_id=3 WHERE story_id='104'")
        ready = {str(i): {'at': fixture.now - 60, 'name': 'story'} for i in range(101,105)}
        config = dict(fixture.config, language='tl', material_products=['Moboreels','DramawaveSource'],
            drama_launch_window_days='-1~0', _mobo_reviews={'tl': Availability(ready, {}, fixture.now + 300)})
        with patch('features.fb_video_publish.moboreels.institution_id',return_value='scope'):
            rows = fixture.repo.candidate_snapshot(config).candidates
        self.assertEqual([(r.material_id,r.content_id,r.language,r.deploy_time) for r in rows],
                         [('1','101','tl',fixture.now - 60)])


if __name__=='__main__':
    unittest.main()
