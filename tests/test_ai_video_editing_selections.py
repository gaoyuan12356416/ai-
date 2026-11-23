import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from features.ai_video_editing import studio, selections, catalog_imports, source_jobs, smart_api, previews
from features.ai_video_editing.runtime import Runtime, owner, packed, Fault
from features.ai_video_editing.catalog import source_key


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.rt=Runtime(Path(self.temp.name)/'private');studio.setup(self.rt)
        self.actor={'tenant_key':'t','user_id':'u'};self.other={'tenant_key':'other','user_id':'u'}
        self.key=source_key(['42','DramaWave','TW','zh-TW'])
        self.refs=[{'kind':'catalog','product':'dramawave','key':self.key,'episode':i,'name':'Episode '+str(i)} for i in range(1,6)]

    def asset(self,n,content=None):
        aid='aa_'+('%032x'%n);raw=content or ('video'+str(n)).encode()
        path=self.rt.root/'uploads'/(aid+'.mp4');path.write_bytes(raw)
        with self.rt.db(True) as db:
            db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?,?,?,?,?,?)',(aid,owner(self.actor),'Episode '+str(n),'Drama',n,hashlib.sha256(raw).hexdigest(),packed({'duration_ms':5000,'has_audio':True,'width':720,'height':1280}),time.time()))
        return aid

    def ready(self,n):
        aid=self.asset(n)
        with self.rt.db(True) as db:
            catalog_imports.submit_rows(db,self.actor,{'product':'dramawave','key':self.key,'episodes':[n]})
            db.execute("UPDATE catalog_imports SET status='ready',asset_id=? WHERE episode=? AND owner=?",(aid,n,owner(self.actor)))
        return aid

    def create(self,refs=None,op='selection1'):
        return studio.dispatch(self.rt,self.actor,'POST','/sessions',{'items':refs or self.refs,'operation_id':op}, {})['item']

    def analyze(self,fail=None):
        def process(rt,actor,snap,folder):
            if snap['source_id']==fail:raise Fault('fixture_failure','fixture failed',422)
            return {'engine':'multimodal','candidates':[],'warnings':[]}
        with patch.object(smart_api,'process_source',side_effect=process) as spy:
            while source_jobs.execute_one(self.rt):pass
            return spy.call_count

    def test_five_intents_survive_ready_subset_and_out_of_order_completion(self):
        self.ready(1);s=self.create();sid=s['id']
        self.assertEqual(len(s['selection']['items']),5)
        self.assertEqual(s['source_ids'],['aa_'+('%032x'%1)])
        restored=studio.session(Runtime(self.rt.root),self.actor,sid)
        self.assertEqual(restored['selection']['summary']['total'],5)
        for n in [5,3,2,4]:self.ready(n)
        selections.tick(self.rt);self.assertEqual(self.analyze(),5);selections.tick(self.rt)
        done=studio.session(self.rt,self.actor,sid)
        self.assertEqual(done['selection']['status'],'succeeded')
        self.assertEqual(done['source_ids'],['aa_'+('%032x'%i) for i in range(1,6)])
        self.assertEqual(len(smart_api.receipt(self.rt,self.actor,done['analysis_id'])['sources']),5)

    def test_receipt_replay_after_download_does_not_duplicate_or_change_digest(self):
        s=self.create();self.ready(1)
        again=self.create();self.assertEqual(s['id'],again['id'])
        with self.rt.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM catalog_imports').fetchone()[0],5)
            self.assertEqual(db.execute('SELECT count(*) FROM studio_source_batches').fetchone()[0],1)
        with self.assertRaises(Fault):self.create(self.refs[:4])

    def test_failed_source_does_not_stop_other_sources_and_retry_only_failed(self):
        for i in range(1,6):self.ready(i)
        s=self.create();selections.tick(self.rt)
        failed='aa_'+('%032x'%2)
        self.assertEqual(self.analyze(fail=failed),5);selections.tick(self.rt)
        state=selections.progress(self.rt,self.actor,s['id'])
        self.assertEqual(state['summary'],{'total':5,'imported':5,'analyzed':4,'failed':1})
        self.assertEqual(state['status'],'needs_attention')
        item=next(i for i in state['items'] if i['asset_id']==failed)
        selections.retry(self.rt,self.actor,s['id'],item['id'])
        self.assertEqual(self.analyze(),1);selections.tick(self.rt)
        self.assertEqual(selections.progress(self.rt,self.actor,s['id'])['status'],'succeeded')

    def test_reorder_and_add_one_reuse_completed_content(self):
        for i in range(1,6):self.ready(i)
        s=self.create();selections.tick(self.rt);self.analyze();selections.tick(self.rt)
        current=studio.session(self.rt,self.actor,s['id'])
        body={'items':self.refs[::-1],'version':current['version'],'operation_id':'reverse'}
        selections.submit(self.rt,self.actor,s['id'],body);selections.tick(self.rt)
        self.assertEqual(self.analyze(),0)
        current=studio.session(self.rt,self.actor,s['id']);six=self.asset(6)
        selections.submit(self.rt,self.actor,s['id'],{'items':self.refs+[{'kind':'asset','asset_id':six}],'version':current['version'],'operation_id':'add'})
        selections.tick(self.rt);self.assertEqual(self.analyze(),1)

    def test_owner_version_and_legacy_subset_protection(self):
        self.ready(1);s=self.create()
        with self.assertRaises(Fault):selections.progress(self.rt,self.other,s['id'])
        with self.assertRaises(Fault):selections.submit(self.rt,self.actor,s['id'],{'items':self.refs,'version':0,'operation_id':'stale'})
        with self.assertRaises(Fault):studio.dispatch(self.rt,self.actor,'PATCH','/sessions/'+s['id'],{'source_ids':s['source_ids'],'version':s['version']},{})

    def test_waiting_creation_requires_complete_selection_and_explicit_removal(self):
        self.ready(1);s=self.create()
        body={'operation_id':'make','config':{'clip':{'method':'hook_first'}}}
        receipt=studio.submit(self.rt,self.actor,s['id'],body)
        studio.tick(self.rt)
        with self.rt.db() as db:self.assertEqual(db.execute('SELECT status FROM studio_runs WHERE id=?',(receipt['run_id'],)).fetchone()[0],'waiting_sources')
        with self.rt.db(True) as db:db.execute("UPDATE catalog_imports SET status='failed',error='fixture' WHERE episode=2")
        with self.rt.db(True) as db:db.execute('UPDATE studio_runs SET expires=0')
        studio.tick(self.rt)
        with self.rt.db() as db:self.assertEqual(db.execute('SELECT status FROM studio_runs WHERE id=?',(receipt['run_id'],)).fetchone()[0],'needs_attention')
        current=studio.session(self.rt,self.actor,s['id'])
        selections.submit(self.rt,self.actor,s['id'],{'items':[self.refs[0]],'version':current['version'],'operation_id':'remove'})
        with self.rt.db() as db:
            self.assertEqual(db.execute('SELECT status FROM studio_runs WHERE id=?',(receipt['run_id'],)).fetchone()[0],'superseded')
            self.assertEqual(db.execute("SELECT count(*) FROM studio_runs WHERE status='waiting_sources'").fetchone()[0],1)

    def test_cached_asset_and_catalog_reference_deduplicate(self):
        aid=self.ready(1)
        s=self.create([{'kind':'asset','asset_id':aid},self.refs[0]])
        self.assertEqual(s['selection']['summary']['total'],1)

    def test_preview_failure_is_auxiliary(self):
        self.ready(1);s=self.create(self.refs[:1])
        with patch.object(previews,'request',side_effect=RuntimeError('preview failed')):
            selections.tick(self.rt)
        self.analyze();selections.tick(self.rt)
        self.assertEqual(selections.progress(self.rt,self.actor,s['id'])['status'],'succeeded')

    def test_same_content_uses_one_child_across_selections(self):
        a=self.asset(1,b'same');b=self.asset(2,b'same')
        first=self.create([{'kind':'asset','asset_id':a}]);self.create([{'kind':'asset','asset_id':b}],op='other-session')
        selections.tick(self.rt);self.assertEqual(self.analyze(),1);selections.tick(self.rt)
        self.assertEqual(selections.progress(self.rt,self.actor,first['id'])['status'],'succeeded')

    def test_creation_receipt_survives_readiness_and_rejects_stale_new_intent(self):
        self.ready(1);s=self.create()
        body={'operation_id':'create-once','version':s['version'],'config':{'clip':{'method':'hook_first','seconds':30}}}
        receipt=studio.submit(self.rt,self.actor,s['id'],body)
        current=studio.session(self.rt,self.actor,s['id'])
        self.assertEqual(current['version'],s['version']+1)
        for i in range(2,6):self.ready(i)
        selections.tick(self.rt)
        self.assertEqual(studio.submit(self.rt,self.actor,s['id'],body),receipt)
        with self.assertRaises(Fault) as error:
            studio.submit(self.rt,self.actor,s['id'],dict(body,operation_id='stale-new'))
        self.assertEqual(error.exception.code,'version_conflict')

    def test_limit_uses_unique_materials(self):
        refs=[{'kind':'asset','asset_id':self.asset(i)} for i in range(1,22)]
        s=self.create(refs[:20]+refs[:4]);self.assertEqual(s['selection']['summary']['total'],20)
        with self.assertRaises(Fault):self.create(refs,op='too-many')


if __name__=='__main__':unittest.main()
