"""Behavioural tests for manual modes, sparse evidence and safe growth ranking."""
import copy
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.x_growth_profile import store
from features.x_growth_profile.api import editable
from features.x_growth_profile.metrics import extract
from features.x_growth_profile.model import build_profiles,follower_summary
from features.x_growth_profile.selection import GrowthDecision

NOW=datetime(2026,11,19,tzinfo=timezone.utc)

def sample(i,theme='revenge',views=1000,account=1,drama=None,age=72,clicks=10):
    return dict(post_id=str(i),account_id=account,content_id=drama or str(i//2),catalog='dramawave',language='en',
                published_at=(NOW-timedelta(hours=96)).isoformat(),observed_at=(NOW-timedelta(hours=96-age)).isoformat(),
                duration=600,stage='72h',metadata={'audience':'general','themes':[theme]},
                metrics=dict(organic_impressions=views,impressions=views,profile_clicks=clicks,organic_reposts=5,bookmarks=2))

class GrowthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'growth.sqlite3';store.initialize(self.db)
        self.env=patch.dict(os.environ,{'X_GROWTH_DB_PATH':str(self.db),'X_AUTO_POST_DB_PATH':'','X_POST_DB_PATH':''});self.env.start()

    def tearDown(self):
        self.env.stop();self.tmp.cleanup()

    def test_default_is_revenue_and_reads_do_not_create_choices(self):
        self.assertEqual(store.selection_context({'id':1})['mode'],'revenue')
        with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM channel_mode').fetchone()[0],0)

    def test_manual_change_version_and_audit(self):
        r=store.set_mode(1,'growth',0,'tenant:user');self.assertEqual(r['mode_version'],1)
        with self.assertRaisesRegex(ValueError,'conflict'):store.set_mode(1,'revenue',0,'tenant:user')
        r=store.set_mode(1,'revenue',1,'tenant:user');self.assertEqual(r['mode_version'],2)
        with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM mode_audit').fetchone()[0],2)

    def test_invalid_modes_rejected(self):
        for mode,version in [('auto',0),('growth',True),('growth',-1)]:
            with self.assertRaises(ValueError):store.set_mode(1,mode,version,'actor')

    def test_legacy_channel_setting_cannot_select_a_new_model(self):
        from features.x_growth_profile.selection import choose_context
        store.set_mode(1, 'growth', 0, 'old-operator')
        self.assertEqual(choose_context({'id': 1})['mode'], 'revenue')
        self.assertEqual(store.publish_profile_context(1, 'growth')['mode'], 'growth')

    def test_existing_snapshot_not_rewritten(self):
        from features.x_growth_profile.selection import choose_context
        snapshot = {'mode': 'growth', 'mode_version': 9, 'profile': {'version': 'old'}}
        self.assertEqual(choose_context({'id': 1, 'selection_profile': snapshot}), snapshot)

    def test_owner_and_admin_authorization(self):
        accounts=Path(self.tmp.name)/'accounts.db'
        with sqlite3.connect(accounts) as db:
            db.execute('CREATE TABLE x_authorized_account(id,owner_tenant_key,owner_user_id,deleted_at)')
            db.execute('INSERT INTO x_authorized_account VALUES(1,\'a\',\'u\',NULL)')
        db.close()
        self.assertTrue(editable(1,{'tenant_key':'a','user_id':'u'},accounts))
        self.assertFalse(editable(1,{'tenant_key':'b','user_id':'u'},accounts))
        self.assertTrue(editable(1,{'role':'admin'},accounts))
        self.assertFalse(editable(2,{'role':'admin'},accounts))

    def test_missing_metrics_stay_null_zero_is_valid(self):
        value=extract({'public_metrics':{'impression_count':0}},[])
        self.assertEqual(value['impressions'],0)
        self.assertIsNone(value['profile_clicks'])
        self.assertNotIn('video_views',value)

    def test_video_expansion_attached_only_and_scoped(self):
        result=extract({'attachments':{'media_keys':['a']}},[
            {'media_key':'b','type':'video','public_metrics':{'view_count':999}},
            {'media_key':'a','type':'video','organic_metrics':{'view_count':5,'watch_time_ms':42}}])
        self.assertEqual(result['video_views'],5)
        self.assertEqual(result['watch_time_ms'],42)
        self.assertEqual(result['video_metrics_scope'],'media_aggregate_not_post_attribution')

    def test_media_views_cannot_inflate_post_score(self):
        rows=[sample(i,views=200+i*100) for i in range(8)]
        before=build_profiles(rows,{},now=NOW)
        rows[0]['metrics']['video_views']=10**12
        self.assertEqual(before,build_profiles(rows,{},now=NOW))

    def test_late_backfill_not_a_72_hour_measurement(self):
        result=build_profiles([sample(1,age=170)],{},now=NOW)[1]
        self.assertEqual(result['mature_posts'],0)

    def test_low_exposure_does_not_become_negative_evidence(self):
        result=build_profiles([sample(1,views=20,clicks=0)],{},now=NOW)[1]
        self.assertEqual(result['themes'],[])
        self.assertEqual(result['policy']['exploration'],1)

    def test_one_hit_drama_does_not_prove_theme(self):
        result=build_profiles([sample(i,drama='same') for i in range(20)],{},now=NOW)[1]
        self.assertFalse(result['themes'][0]['mature'])
        self.assertEqual(result['policy']['weights'],{})

    def test_multitag_evidence_not_duplicated(self):
        rows=[sample(i) for i in range(8)]
        for row in rows:row['metadata']['themes']=['revenge','marital']
        p=build_profiles(rows,{},now=NOW)[1]
        self.assertEqual(sum(n['effective_posts'] for n in p['themes']),8)
        self.assertTrue(all(not n['mature'] for n in p['themes']))

    def test_equal_performance_does_not_create_winner(self):
        rows=[sample(i,'revenge' if i<8 else 'marital') for i in range(16)]
        p=build_profiles(rows,{},now=NOW)[1]
        self.assertEqual(p['policy']['exploration'],1)

    def test_consistent_winner_receives_majority(self):
        rows=[sample(i,'revenge' if i<8 else 'marital',views=10000 if i<8 else 200,clicks=200 if i<8 else 0) for i in range(16)]
        p=build_profiles(rows,{},now=NOW)[1]
        self.assertAlmostEqual(p['policy']['weights']['revenge'],.6)
        self.assertAlmostEqual(p['policy']['exploration'],.2)

    def test_weekly_policy_frozen_between_reviews(self):
        old={1:{'policy_updated_at':(NOW-timedelta(days=2)).isoformat(),'policy':{'weights':{'marital':.6},'exploration':.4}}}
        p=build_profiles([sample(i) for i in range(8)],{},previous=old,now=NOW)[1]
        self.assertEqual(p['policy'],old[1]['policy'])

    def test_weekly_transition_from_exploration_is_bounded(self):
        rows=[sample(i,'revenge' if i<8 else 'marital',views=10000 if i<8 else 200,clicks=200 if i<8 else 0) for i in range(16)]
        old={1:{'policy_updated_at':(NOW-timedelta(days=7)).isoformat(),'policy':{'weights':{},'exploration':1}}}
        p=build_profiles(rows,{},previous=old,now=NOW)[1]
        self.assertAlmostEqual(p['policy']['exploration'],.8)
        self.assertLessEqual(max(p['policy']['weights'].values()),.2)

    def test_weekly_return_to_exploration_is_bounded(self):
        old={1:{'policy_updated_at':(NOW-timedelta(days=7)).isoformat(),'policy':{'weights':{'revenge':.6,'marital':.2},'exploration':.2}}}
        p=build_profiles([sample(i) for i in range(8)],{},previous=old,now=NOW)[1]
        self.assertAlmostEqual(p['policy']['exploration'],.4)
        self.assertAlmostEqual(sum(p['policy']['weights'].values())+p['policy']['exploration'],1)

    def test_follower_net_not_post_attribution(self):
        rows=[dict(observed_at=(NOW-timedelta(days=days)).isoformat(),followers=n) for days,n in [(14,100),(7,90),(0,80)]]
        f=follower_summary(rows,NOW)
        self.assertEqual(f['net_7d'],-10);self.assertTrue(f['two_weeks_nonpositive'])
        self.assertEqual(f['basis'],'account_net_change_not_post_attribution')

    def test_missing_follower_baseline_is_not_zero(self):
        f=follower_summary([dict(observed_at=NOW.isoformat(),followers=100)],NOW)
        self.assertIsNone(f['net_7d'])

    def test_growth_decision_deterministic_and_preview_does_not_write(self):
        account={'id':1,'drama_language':'en'};context={'mode':'growth','profile':{}}
        meta=[{'themes':[t]} for t in ('revenge','marital','fantasy','family')]
        first=GrowthDecision(account,['preview',1],context);first.prepare(meta)
        second=GrowthDecision(account,['preview',1],context);second.prepare(meta)
        self.assertEqual(first.order,second.order)
        with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM mode_audit').fetchone()[0],0)

    def test_exploration_corrects_recent_imbalance(self):
        g=GrowthDecision({'id':1,'drama_language':'en'},['task',4],{'mode':'growth','profile':{}})
        meta=[{'themes':[t]} for t in ('revenge','marital','fantasy')]
        with patch('features.x_growth_profile.selection.recent_choices',return_value=[{'language':'en','selected_tag_key':'revenge'}]*8):g.prepare(meta)
        self.assertNotEqual(g.order[0],'revenge')

    def test_missing_taxonomy_preserves_candidates(self):
        g=GrowthDecision({'id':1},['task'],{'mode':'growth','profile':{}});g.prepare([{}])
        self.assertEqual(g.rank({}),(0,0));self.assertTrue(g.audit({})['fallback'])

    def test_collector_saves_real_age_once_and_never_changes_mode(self):
        from features.x_growth_profile import collector
        now=datetime.now(timezone.utc)
        published=(now-timedelta(hours=73)).isoformat()
        accounts=Path(self.tmp.name)/'accounts.sqlite3';posts=Path(self.tmp.name)/'posts.sqlite3';auto=Path(self.tmp.name)/'auto.sqlite3'
        db=sqlite3.connect(accounts)
        db.execute('CREATE TABLE x_authorized_account(id,x_user_id,status,publish_approved,deleted_at,drama_language,access_expires_at,owner_user_id,owner_tenant_key)')
        db.execute("INSERT INTO x_authorized_account VALUES(1,'123','active',1,NULL,'pt-br',?,'u','t')",((now+timedelta(hours=1)).isoformat(),));db.commit();db.close()
        db=sqlite3.connect(posts)
        db.execute('CREATE TABLE x_post_queue(id,account_id,content_id,material_language,preflight_duration,tag)')
        db.execute("INSERT INTO x_post_queue VALUES(1,1,'drama','pt',600,'')")
        db.execute('CREATE TABLE x_post_publish_log(x_post_id,published_at,x_media_id,queue_id,status,unknown_outcome)')
        db.execute("INSERT INTO x_post_publish_log VALUES('999',?,'media',1,'published',0)",(published,));db.commit();db.close()
        db=sqlite3.connect(auto)
        db.execute('CREATE TABLE x_auto_task(execution_queue_id,selection_json,published_at_utc)')
        selection={'drama':{'catalog':'dramawave'},'tag_profile':{'classification':{'themes':['revenge'],'audience':'general'}}}
        db.execute('INSERT INTO x_auto_task VALUES(1,?,?)',(json.dumps(selection),published));db.commit();db.close()
        # Simulate an existing collector launched three hours ago. This post
        # predates its former 24h discovery window but is a live 73h sample now.
        with store.connect(write=True) as db:db.execute('INSERT INTO collector_state VALUES(?,?)',('started_at',json.dumps((now-timedelta(hours=3)).isoformat())))
        store.set_mode(1,'growth',0,'actor')
        def fake(url,token,payload=None):
            if '/growth-origins' in url:
                from features.x_growth_profile.origins import query
                return {'items':query(auto,payload['since'])}
            self.assertIsNone(payload)
            if '/users/me' in url:return {'data':{'id':'123','public_metrics':{'followers_count':100}}}
            return {'data':[{'id':'999','author_id':'123','public_metrics':{'impression_count':1000,'bookmark_count':3},'organic_metrics':{'impression_count':1000,'user_profile_clicks':10,'retweet_count':2}}]}
        with patch.object(collector,'ACCOUNT_DB',str(accounts)),patch.object(collector,'access_token',return_value='fake-test-token'),patch.object(collector,'request',side_effect=fake) as req,patch.dict(os.environ,{'X_INTERNAL_TOKEN':'test-only','X_POST_DB_PATH':str(posts),'X_AUTO_POST_DB_PATH':str(auto)}):
            result=collector.collect()
            self.assertEqual(result['post_samples'],1);self.assertEqual(result['follower_samples'],1)
            self.assertEqual(req.call_count,3)
            collector.collect();self.assertEqual(req.call_count,4)
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT stage FROM post_snapshot').fetchone()[0],'72h')
            profile=json.loads(db.execute('SELECT data_json FROM growth_profile WHERE account_id=1').fetchone()[0])
            self.assertEqual(profile['themes'][0]['language'],'pt-br')
            self.assertEqual(profile['mature_posts'],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM mode_audit').fetchone()[0],1)
        self.assertEqual(store.selection_context({'id':1})['mode'],'growth')

    def test_first_follower_baseline_for_active_account_without_posts(self):
        from features.x_growth_profile import collector
        accounts = Path(self.tmp.name) / 'account-without-posts.sqlite3'
        with sqlite3.connect(accounts) as db:
            db.execute('CREATE TABLE x_authorized_account(id, x_user_id, status, publish_approved, deleted_at)')
            db.executemany('INSERT INTO x_authorized_account VALUES(?,?,?,?,?)', [
                (10, '120', 'active', 0, ''), (11, '121', 'error', 1, ''), (12, '122', 'active', 1, 'deleted')])
        db.close()
        with patch.object(collector, 'ACCOUNT_DB', str(accounts)), patch.object(collector, 'discover'), \
             patch.object(collector, 'access_token', return_value='test'), \
             patch.object(collector, 'request', return_value={'data': {'id':'120','public_metrics':{'followers_count':0}}}) as get:
            result = collector.collect()
            self.assertEqual(result['follower_samples'], 1)
            self.assertEqual(result['post_samples'], 0)
            self.assertEqual(result['profiles'], 1)
            self.assertEqual(get.call_count, 1)
        with store.connect() as db:
            profile = json.loads(db.execute('SELECT data_json FROM growth_profile WHERE account_id=10').fetchone()[0])
            self.assertEqual(profile['followers']['followers'], 0)
            self.assertIsNone(profile['followers']['net_7d'])
            self.assertEqual(profile['policy']['exploration'], 1)

if __name__=='__main__':unittest.main()
