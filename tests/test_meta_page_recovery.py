import copy
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.meta_page_recovery.domain import (account_dates,purchases,active_ad,select_page,profile_match,
    build_creative,creative_page,video_ids,replace_video,CHAT_ID)
from features.meta_page_recovery.state import Store
from features.meta_page_recovery.engine import scan,Worker,verify_creative
from features.meta_page_recovery.meta import MetaError
from features.meta_page_recovery.notify import card,Sender
from features.meta_page_recovery.sources import Reader,SourceError


def batch_messages(store):
    with store.db() as db:
        return [dict(r) for r in db.execute("SELECT * FROM notifications WHERE run_id NOT LIKE 'daily-%' ORDER BY rowid")]


def page(pid='10',group=24,status=0,lang='en',**kw):
    return dict(page_id=pid,group_id=group,status=status,forbidden_status=0,is_permission=1,
        language=lang,active_ad_count=3,fan_ccount=100,status_at=0,**kw)


def ad():
    return dict(id='100',account_id='42',configured_status='ACTIVE',effective_status='WITH_ISSUES',
        creative={'id':'old'},campaign={'id':'c','configured_status':'ACTIVE'},adset={'id':'s','configured_status':'ACTIVE'})


def creative():
    return dict(id='old',object_story_spec={'page_id':'10','instagram_actor_id':'old_ig',
        'video_data':{'video_id':'v1','image_hash':'cover','image_url':'https://derived.invalid/cover',
            'message':'unchanged text','title':'title','call_to_action':{'type':'INSTALL_MOBILE_APP',
                'value':{'link':'https://landing.invalid/a?x=1','app_link':'app://drama/id?campaign=keep'}}}},
        url_tags='utm_content={{ad.id}}',asset_feed_spec={'videos':[{'video_id':'v1','adlabels':[{'name':'video'}]}]})


def profile(audience='female',theme='ceo',share=7000):
    return {'status':'ready','audiences':[{'key':audience,'share_bp':10000,
        'children':[{'key':theme,'share_bp':share,'channel_share_bp':share,'matchable':True}]}]}


class Sources:
    def __init__(self):self.rows=[page(),page('20')];self.calls=0
    def pages(self):return copy.deepcopy(self.rows)
    def ads(self,pid,cursor=''):
        return ([dict(ad_id='100',ad_account_id='act_42',creative_id='old',user_id=1,source_id='111')], '100',True) if not cursor else ([],cursor,True)
    def tokens(self,data):return ['secret-not-logged']
    def target(self,*_):return {'language':'en','audience':'female','themes':['ceo']}


class Graph:
    def __init__(self):
        self.current=ad();self.creatives={'old':creative()};self.creates=0;self.updates=0;self.validations=0
        self.pur='1';self.create_error=None;self.update_error=None;self.pause_on_validate=False
    def ad(self,aid):return copy.deepcopy(self.current)
    def creative(self,cid):return copy.deepcopy(self.creatives[cid])
    def get(self,*_,**__):return {'id':'42','timezone_name':'UTC'}
    def insights(self,*_):return [] if self.pur is None else [{'ad_id':'100','actions':[{'action_type':'purchase','value':self.pur},{'action_type':'omni_purchase','value':'900'}]}]
    def new_instagram(self,pid):return 'new_ig'
    def post(self,path,**payload):
        if path.endswith('/adcreatives'):
            self.creates+=1
            self.creatives['new']=copy.deepcopy(payload)|{'id':'new'}
            if self.create_error:raise self.create_error
            return {'id':'new'}
        if 'execution_options' in payload:
            self.validations+=1
            if self.pause_on_validate:self.current['configured_status']='PAUSED'
            return {'success':True}
        self.updates+=1
        self.current['creative']={'id':payload['creative']['creative_id']}
        self.current['effective_status']='PENDING_REVIEW'
        if self.update_error:raise self.update_error
        return {'success':True}
    def pages(self,*_,**__):return [{'id':k,'name':v.get('name')} for k,v in self.creatives.items()]


class DomainTests(unittest.TestCase):
    def test_initial_handshake_retry_stays_on_sql_gate(self):
        env={'ADMIN_MAPPING_MYSQL_HOST':'101.32.56.53','ADMIN_MAPPING_MYSQL_PORT':'63350','ADMIN_MAPPING_MYSQL_USER':'test','ADMIN_MAPPING_MYSQL_PASSWORD':'fixture'}
        responses=[SimpleNamespace(returncode=1,stdout='',stderr='ERROR 2013 greeting'),SimpleNamespace(returncode=0,stdout='1\n7b7d\n',stderr='')]
        with patch('features.meta_page_recovery.sources.subprocess.run',side_effect=responses) as run:
            self.assertEqual(Reader(env).query(['SELECT 1']),[{}])
            self.assertEqual(run.call_count,2)
            self.assertEqual(run.call_args.args[0][0],'/usr/bin/mysql')
            self.assertNotIn('SQL_GATE_BYPASS',run.call_args.kwargs['env'])
    def test_mysql_2002_connection_refusal_has_one_gated_retry(self):
        env={'ADMIN_MAPPING_MYSQL_HOST':'101.32.56.53','ADMIN_MAPPING_MYSQL_PORT':'63350','ADMIN_MAPPING_MYSQL_USER':'test','ADMIN_MAPPING_MYSQL_PASSWORD':'fixture'}
        responses=[SimpleNamespace(returncode=1,stdout='',stderr='ERROR 2002 connect'),SimpleNamespace(returncode=0,stdout='1\n7b7d\n',stderr='')]
        with patch('features.meta_page_recovery.sources.subprocess.run',side_effect=responses) as run:
            self.assertEqual(Reader(env).query(['SELECT 1']),[{}]);self.assertEqual(run.call_count,2)
    def test_partial_read_failure_is_not_retried_as_empty(self):
        env={'ADMIN_MAPPING_MYSQL_HOST':'101.32.56.53','ADMIN_MAPPING_MYSQL_PORT':'63350','ADMIN_MAPPING_MYSQL_USER':'test','ADMIN_MAPPING_MYSQL_PASSWORD':'fixture'}
        with patch('features.meta_page_recovery.sources.subprocess.run',return_value=SimpleNamespace(returncode=1,stdout='1\n',stderr='ERROR 2013 during query')) as run:
            with self.assertRaises(SourceError):Reader(env).query(['SELECT 1'])
            self.assertEqual(run.call_count,1)
    def test_timezone_today_yesterday(self):
        now=datetime(2026,11,17,1,0,tzinfo=timezone.utc)
        self.assertEqual(account_dates({'timezone_name':'America/Los_Angeles'},now),{'since':'2026-11-15','until':'2026-11-16'})
        self.assertEqual(account_dates({'timezone_name':'Asia/Shanghai'},now),{'since':'2026-11-16','until':'2026-11-17'})
    def test_purchase_aliases_do_not_double_count(self):
        self.assertEqual(purchases([{'actions':[{'action_type':'purchase','value':'2'},{'action_type':'omni_purchase','value':'99'}]}]),'2')
    def test_no_data_not_zero(self):
        self.assertIsNone(purchases([]));self.assertEqual(purchases([{}]),'0')
    def test_duplicate_purchase_is_invalid(self):
        with self.assertRaises(ValueError):purchases([{'actions':[{'action_type':'purchase','value':1}]*2}])
    def test_bad_effective_status_can_be_active(self):self.assertTrue(active_ad(ad()))
    def test_parent_pause_excludes(self):
        value=ad();value['adset']['configured_status']='PAUSED';self.assertFalse(active_ad(value))
    def test_preserve_content_and_tracking(self):
        original=creative();payload=build_creative(original,'20','job','new_ig')
        self.assertEqual(payload['object_story_spec']['page_id'],'20')
        self.assertEqual(payload['object_story_spec']['instagram_user_id'],'new_ig')
        self.assertNotIn('instagram_actor_id',payload['object_story_spec'])
        self.assertEqual(payload['url_tags'],original['url_tags'])
        self.assertEqual(payload['object_story_spec']['video_data']['call_to_action'],original['object_story_spec']['video_data']['call_to_action'])
        self.assertNotIn('image_url',payload['object_story_spec']['video_data'])
        self.assertEqual(original['object_story_spec']['page_id'],'10')
    def test_cannot_guess_missing_post_content(self):
        with self.assertRaises(ValueError):build_creative({'object_story_id':'10_123'},'20','job')
    def test_asset_feed_only_supported(self):
        value={'object_story_spec':{'page_id':'10'},'asset_feed_spec':{'videos':[{'video_id':'x'}]}}
        self.assertEqual(build_creative(value,'20','j')['asset_feed_spec'],value['asset_feed_spec'])
    def test_video_replacement_all_placements(self):
        value=creative();changed=replace_video(value,'v1','v2')
        self.assertEqual(video_ids(changed),['v2']);self.assertEqual(video_ids(value),['v1'])
    def test_content_readback_rejects_url_change(self):
        payload=build_creative(creative(),'20','j');changed=copy.deepcopy(payload)
        changed['url_tags']='changed'
        with self.assertRaises(ValueError):verify_creative(changed,payload)
    def test_same_pool_and_language(self):
        result=select_page([page('1',25),page('2',24,lang='ja'),page('3',24)],{},dict(language='en'),24)
        self.assertEqual(result['page_id'],'3')
    def test_unknown_direction_matches_themes(self):
        result=profile_match(profile('__unknown__'),{'audience':'__unknown__','themes':['ceo']})
        self.assertEqual(result,('profiled',7000,'themes_only'))
    def test_known_mismatch_not_unprofiled(self):
        result=profile_match(profile('male'),{'audience':'female','themes':['ceo']})
        self.assertEqual(result[:2],('profiled',0))
    def test_same_language_unprofiled_precedes_blank_profile(self):
        result=select_page([page('1',lang=''),page('2')],{'1':profile()},dict(language='en',audience='female',themes=['ceo']),24)
        self.assertEqual(result['page_id'],'2')
    def test_load_includes_allocations(self):
        result=select_page([page('1'),page('2')],{},dict(language='en'),24,{'1':2,'2':0})
        self.assertEqual(result['page_id'],'2')
    def test_no_available_page(self):
        self.assertIsNone(select_page([page(status=1)],{},dict(language='en'),24))


class EngineTests(unittest.TestCase):
    def setUp(self):
        quiet=patch('features.meta_page_recovery.engine.log');quiet.start();self.addCleanup(quiet.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'state.db');self.sources=Sources();self.graph=Graph()
        self.worker=Worker(self.store,self.sources,{},lambda ids:({}, {'version':'v'}, []),lambda token:self.graph)
    def queue(self):
        scan(self.store,self.sources)
        self.sources.rows[0]['status']=1
        scan(self.store,self.sources)
    def age(self):
        with self.store.db() as db:db.execute("UPDATE jobs SET updated='2020-01-01T00:00:00+00:00'")
    def test_initial_bad_is_only_baseline(self):
        self.sources.rows[0]['status']=1;scan(self.store,self.sources)
        self.assertEqual(self.store.jobs({'queued'}),[])
    def test_new_bad_once(self):
        self.queue();scan(self.store,self.sources)
        self.assertEqual(len(self.store.jobs({'queued'})),1)
    def test_unknown_at_baseline_does_not_prove_a_new_ban(self):
        self.sources.rows[0]['status']=None;scan(self.store,self.sources)
        self.sources.rows[0]['status']=1;scan(self.store,self.sources)
        self.assertEqual(self.store.jobs({'queued'}),[])
    def test_temporary_unknown_does_not_create_false_recovery_generation(self):
        self.queue();self.sources.rows[0]['status']=None;scan(self.store,self.sources)
        self.worker.tick()
        self.assertEqual((self.graph.creates,self.graph.updates),(0,0))
        self.assertEqual(self.store.jobs({'retry_wait'})[0]['reason'],'source_page_status_unknown')
        self.sources.rows[0]['status']=1;scan(self.store,self.sources)
        with self.store.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM incidents').fetchone()[0],1)
    def test_resolved_pool_conflict_resumes_only_that_incident(self):
        scan(self.store,self.sources)
        self.sources.rows[0]['status']=1;self.sources.rows.append(page('10',group=25,status=1))
        scan(self.store,self.sources);self.assertEqual(self.store.jobs({'queued'}),[])
        self.sources.rows=self.sources.rows[1:];scan(self.store,self.sources)
        jobs=self.store.jobs({'queued'});self.assertEqual(len(jobs),1);self.assertEqual(jobs[0]['payload']['group_id'],25)
    def test_forbidden_flag_transition(self):
        scan(self.store,self.sources);self.sources.rows[0]['forbidden_status']=1;scan(self.store,self.sources)
        self.assertEqual(len(self.store.jobs({'queued'})),1)
    def test_recovered_then_bad_new_incident(self):
        self.queue();self.sources.rows[0]['status']=0;scan(self.store,self.sources)
        self.sources.rows[0]['status']=1;scan(self.store,self.sources)
        with self.store.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM incidents').fetchone()[0],2)
    def test_happy_path_keeps_ad_id(self):
        self.queue();self.worker.tick()
        self.assertEqual((self.graph.creates,self.graph.updates),(1,1))
        self.assertEqual(self.graph.current['id'],'100');self.assertEqual(creative_page(self.graph.creative('new')),'20')
        self.assertEqual(len(self.store.jobs({'pending_review'})),1)
    def test_billing_and_processing_are_not_reported_as_review_or_delivery(self):
        self.queue();job=self.store.claim('act_42')
        payload=build_creative(creative(),'20',job['id'],'new_ig')
        self.graph.creatives['new']=dict(payload,id='new')
        current=ad();current['creative']={'id':'new'}
        proof={'new_creative_id':'new','new_page_id':'20','creative_payload':payload}
        for effective in ('PENDING_BILLING_INFO','PREAPPROVED','IN_PROCESS'):
            current['effective_status']=effective
            self.worker.confirm(job,self.graph,current,proof)
            saved=self.store.jobs({'changed'})[0]
            self.assertEqual(saved['reason'],'meta_effective_status_'+effective)
        current['effective_status']='ACTIVE';self.worker.confirm(job,self.graph,current,proof)
        self.assertEqual(len(self.store.jobs({'deliverable'})),1)
    def test_zero_pur_no_write(self):
        self.queue();self.graph.pur='0';self.worker.tick()
        self.assertEqual(self.graph.creates,0);self.assertEqual(len(self.store.jobs({'skipped'})),1)
    def test_no_data_is_retry_not_zero(self):
        self.queue();self.graph.pur=None;self.worker.tick()
        self.assertEqual(len(self.store.jobs({'retry_wait'})),1);self.assertEqual(self.graph.creates,0)
    def test_user_pause_during_prepare_prevents_swap(self):
        self.queue();self.graph.pause_on_validate=True;self.worker.tick()
        self.assertEqual(self.graph.creates,1);self.assertEqual(self.graph.updates,0)
    def test_unknown_ad_reconciles_without_repeat(self):
        self.queue();self.graph.update_error=MetaError('timeout',uncertain=True);self.worker.tick()
        self.assertEqual(len(self.store.jobs({'unknown_ad'})),1)
        self.age();self.worker.reconcile()
        self.assertEqual(len(self.store.jobs({'pending_review'})),1);self.assertEqual(self.graph.updates,1)
    def test_unknown_create_reconciles_and_reuses(self):
        self.queue();self.graph.create_error=MetaError('timeout',uncertain=True);self.worker.tick()
        self.assertEqual(len(self.store.jobs({'unknown_creative'})),1)
        self.age();self.worker.reconcile();self.graph.create_error=None
        scan(self.store,self.sources);self.worker.tick()
        self.assertEqual((self.graph.creates,self.graph.updates),(1,1))
    def test_unknown_create_absence_never_replayed(self):
        self.queue();self.graph.create_error=MetaError('timeout',uncertain=True);self.worker.tick()
        self.graph.creatives.pop('new');self.age();self.worker.reconcile();scan(self.store,self.sources);self.worker.tick()
        self.assertEqual(self.graph.creates,1);self.assertEqual(len(self.store.jobs({'unknown_creative'})),1)
    def test_restart_with_write_intent_stays_unknown(self):
        self.queue();job=self.store.claim('act_42');self.store.intent(job['id'],'ad_update',{'creative':{'creative_id':'new'}})
        self.store.recover_process();self.assertEqual(len(self.store.jobs({'unknown_ad'})),1)
    def test_known_create_receipt_survives_crash_before_proof(self):
        self.queue();job=self.store.claim('act_42')
        proof={'old_creative_id':'old','new_page_id':'20'};self.store.save_proof(job['id'],proof)
        payload=build_creative(creative(),'20',job['id'],'new_ig');self.graph.creatives['new']=copy.deepcopy(payload)|{'id':'new'}
        intent=self.store.intent(job['id'],'creative_create',payload);self.store.receipt(intent,'accepted',{'id':'new'})
        self.store.recover_process();scan(self.store,self.sources);self.worker.tick()
        self.assertEqual(self.graph.creates,0);self.assertEqual(self.graph.updates,1)
    def test_source_failure_reports_failure_not_zero(self):
        self.sources.pages=lambda:(_ for _ in ()).throw(RuntimeError('database'))
        run=scan(self.store,self.sources)
        row=batch_messages(self.store)[0];summary=json.loads(row['body'])
        self.assertTrue(summary['error']);self.assertEqual(summary['run_id'],run)
    def test_finished_batch_waits_for_jobs(self):
        self.queue()
        self.assertEqual(len(batch_messages(self.store)),1) # only initial baseline
        self.worker.tick();self.assertEqual(len(batch_messages(self.store)),2)
    def test_no_candidate_is_visible_and_retryable(self):
        self.queue();self.sources.rows.pop();self.worker.tick()
        self.assertEqual(len(self.store.jobs({'waiting_no_candidate'})),1)
    def test_first_seen_already_bad_old_timestamp_not_new(self):
        scan(self.store,self.sources);self.sources.rows.append(page('30',status=1));scan(self.store,self.sources)
        self.assertEqual(self.store.jobs({'queued'}),[])

    def test_ready_media_recovers_without_upload_replay(self):
        self.queue();job=self.store.claim('act_42')
        payload=build_creative(creative(),'20',job['id'],'new_ig')
        proof={'old_creative_id':'old','new_page_id':'20','media_reupload_attempted':True,'media_original_payload':payload}
        start=self.store.intent(job['id'],'video_start',{'original_video_id':'v1','file_size':5,'sha256':'hash'})
        self.store.receipt(start,'accepted',{'video_id':'v2','upload_session_id':'session'})
        finish=self.store.intent(job['id'],'video_finish',{'video_id':'v2','file_size':5,'sha256':'hash'})
        self.store.receipt(finish,'unknown',error='timeout')
        self.store.result(job['id'],'unknown_media','timeout',proof);self.age()
        original_get=self.graph.get
        self.graph.get=lambda path,**kw:({'id':'v2','status':{'video_status':'ready'}} if path=='v2' else original_get(path,**kw))
        self.worker.reconcile();self.assertEqual(len(self.store.jobs({'retry_wait'})),1)
        scan(self.store,self.sources);self.worker.tick()
        self.assertEqual((self.graph.creates,self.graph.updates),(1,1))
        self.assertEqual(video_ids(self.graph.creative('new')),['v2'])


class NotificationTests(unittest.TestCase):
    setUp=EngineTests.setUp
    def test_empty_run_card_and_fixed_recipient(self):
        scan(self.store,self.sources);row=batch_messages(self.store)[0]
        body=card(json.loads(row['body']));self.assertIn('无新增异常',body['elements'][0]['content'])
        self.assertEqual(CHAT_ID,'oc_b633b6fba2c78f43ef1ebacd314d518c')
    def test_missing_page_status_is_visible_instead_of_a_zero_success(self):
        self.sources.rows[0]['status']=None;scan(self.store,self.sources)
        summary=json.loads(batch_messages(self.store)[0]['body'])
        self.assertEqual(summary['pools']['24']['unknown_pages'],1)
        content=card(summary)['elements'][0]['content']
        self.assertIn('Page状态待核实 1',content);self.assertNotIn('本轮无新增异常 Page。',content)
    def test_delivery_receipt_deduplicates(self):
        scan(self.store,self.sources)
        self.store.queue_daily(datetime.now(timezone.utc)+timedelta(days=1))
        with patch.object(Sender,'send',return_value='message-1') as send:
            sender=Sender({'FEISHU_APP_ID':'a','FEISHU_APP_SECRET':'s'})
            sender.deliver(self.store);sender.deliver(self.store)
            self.assertEqual(send.call_count,1)
    def test_uncertain_send_is_not_repeated(self):
        from features.fb_page_assignment.notify import DeliveryError
        scan(self.store,self.sources)
        self.store.queue_daily(datetime.now(timezone.utc)+timedelta(days=1))
        with patch.object(Sender,'send',side_effect=DeliveryError('timeout',uncertain=True)) as send:
            sender=Sender({});sender.deliver(self.store);sender.deliver(self.store)
            self.assertEqual(send.call_count,1)
            self.assertEqual(self.store.snapshot()['notifications'],{'recorded':1,'unknown':1})


class DailyReportTests(unittest.TestCase):
    setUp=EngineTests.setUp
    queue=EngineTests.queue

    def instant(self):
        return datetime.now(timezone.utc)+timedelta(days=1)

    def test_beijing_ten_oclock_window(self):
        before=self.store.daily_summary(datetime(2026,11,17,1,59,tzinfo=timezone.utc))
        exact=self.store.daily_summary(datetime(2026,11,17,2,tzinfo=timezone.utc))
        self.assertEqual(before['run_id'],'daily-2026-11-16')
        self.assertEqual(exact['run_id'],'daily-2026-11-17')
        self.assertEqual(exact['started'],'2026-11-16T02:00:00+00:00')
        self.assertEqual(exact['finished'],'2026-11-17T02:00:00+00:00')

    def test_batch_is_recorded_without_delivery(self):
        scan(self.store,self.sources)
        with patch.object(Sender,'send') as send:
            Sender({}).deliver(self.store)
        send.assert_not_called()
        self.assertEqual(batch_messages(self.store)[0]['status'],'recorded')

    def test_repeated_retry_is_one_ad_and_no_data_is_not_zero(self):
        self.graph.pur=None;self.queue();self.worker.tick()
        for _ in range(2):scan(self.store,self.sources);self.worker.tick()
        summary=self.store.daily_summary(self.instant());pool=summary['pools']['24']
        self.assertEqual(summary['scan_runs'],3)
        self.assertEqual((pool['scanned_ads'],pool['waiting'],pool['pur_no_data'],pool['pur_zero']),(1,1,1,0))
        self.assertEqual(pool['new_abnormal_pages'],1)
        text=card(summary)['elements'][0]['content']
        self.assertIn('PUR无数据待查 1',text)
        self.assertIn('无返回数据不等于零购买',text)

    def test_inflight_batch_and_scan_failure_do_not_block_report(self):
        self.queue()
        self.sources.pages=lambda:(_ for _ in ()).throw(RuntimeError('database'))
        scan(self.store,self.sources)
        report=self.store.daily_summary(self.instant())
        self.assertEqual(report['unfinished_runs'],1)
        self.assertEqual(report['failed_runs'],1)
        self.assertEqual(report['pools']['24']['waiting'],1)
        self.assertTrue(report['error'])
        self.store.queue_daily(self.instant())
        self.assertEqual(len(self.store.notifications()),1)

    def test_accepted_updates_are_deduplicated_and_report_body_is_frozen(self):
        self.queue();self.worker.tick();job=self.store.jobs({'pending_review'})[0]
        attempt=self.store.intent(job['id'],'ad_update',{})
        self.store.receipt(attempt,'accepted',{'success':True})
        report_id=self.store.queue_daily(self.instant());original=self.store.notifications()[0]
        self.assertEqual(json.loads(original['body'])['pools']['24']['swapped'],1)
        self.store.result(job['id'],'deliverable','review_accepted')
        self.assertEqual(self.store.queue_daily(self.instant()),report_id)
        self.assertEqual(self.store.notifications()[0]['body'],original['body'])
        self.assertEqual(self.store.notifications()[0]['uuid'],original['uuid'])

    def test_daily_timer_and_worker_cannot_send_the_same_report_twice(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self.store.queue_daily(self.instant());pending=self.store.notifications()
        barrier=threading.Barrier(2)
        def same_snapshot():barrier.wait(timeout=5);return pending
        with patch.object(self.store,'notifications',side_effect=same_snapshot), patch.object(Sender,'send',return_value='one-message') as send:
            with ThreadPoolExecutor(max_workers=2) as pool:
                tasks=[pool.submit(Sender({}).deliver,self.store) for _ in range(2)]
                for task in tasks:task.result(timeout=10)
        self.assertEqual(send.call_count,1)
        self.assertEqual(self.store.snapshot()['notifications'],{'sent':1})

    def test_legacy_archive_preserves_sent_unknown_and_daily_messages(self):
        scan(self.store,self.sources)
        with self.store.db() as db:
            body=batch_messages(self.store)[0]['body']
            for rid,status in [('legacy-pending','pending'),('legacy-failed','failed'),('legacy-sent','sent'),('legacy-unknown','unknown'),('daily-2026-11-17','pending')]:
                db.execute('INSERT INTO notifications(run_id,status,uuid,body) VALUES(?,?,?,?)',(rid,status,rid,body))
        self.assertEqual(self.store.archive_batch_notifications(),2)
        with self.store.db() as db:states=dict(db.execute('SELECT run_id,status FROM notifications'))
        self.assertEqual(states['legacy-sent'],'sent');self.assertEqual(states['legacy-unknown'],'unknown')
        self.assertEqual(states['daily-2026-11-17'],'pending')
        self.assertEqual(states['legacy-pending'],'recorded')
        with self.store.db() as db:
            db.execute("UPDATE notifications SET status='pending' WHERE run_id='legacy-pending'")
        self.assertEqual([x['run_id'] for x in self.store.notifications()],['daily-2026-11-17'])

    def test_previous_window_is_excluded(self):
        self.queue();self.worker.tick()
        summary=self.store.daily_summary(self.instant())
        with self.store.db() as db:
            db.execute("UPDATE runs SET started='2020-01-01T00:00:00+00:00'")
            db.execute("UPDATE jobs SET updated='2020-01-01T00:00:00+00:00'")
            db.execute("UPDATE attempts SET finished='2020-01-01T00:00:00+00:00'")
        empty=self.store.daily_summary(self.instant())
        self.assertEqual(empty['scan_runs'],0)
        self.assertEqual(empty['pools']['24']['scanned_ads'],0)
        self.assertEqual(empty['pools']['24']['swapped'],0)
        self.assertEqual(summary['pools']['24']['scanned_ads'],1)


if __name__=='__main__':unittest.main()
