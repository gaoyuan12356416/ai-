import json
from contextlib import closing
import sqlite3
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from features.moboreels_library.macro_client import MoboreelsLinkError
from features.x_posts import moboreels
from features.x_posts.selector import DramawaveCandidateSelector
from features.x_posts.service import build_post_text, XPostError
from features.x_video_publish.templates import render_caption, normalize_caption
from features.tt_posts.core import render_caption_template, TTPostError
from features.tt_posts.moboreels import freeze_queue_actor, prepare_caption
from features.tt_auto_posts.links import render_auto_caption, build_auto_short_url
from features.x_accounts.youtube_share_text import preview


class MacroRenderingTests(unittest.TestCase):
    def test_previews_never_create_links_and_actual_renderer_uses_supplied_link(self):
        with patch.object(moboreels.macro_client, 'resolve_m_url', side_effect=AssertionError('preview write')):
            self.assertIn('/m/preview', build_post_text('https://x.test/legacy', 'Name', 'Desc', '{{drama_name}} {{desc}} {{m_url}}'))
            self.assertIn('/m/real', render_caption('{name} {{m_url}}', 'Name', 'Desc', 'https://x.test/legacy', m_url='https://x.test/m/real'))
            self.assertEqual(render_caption_template('{{m_url}}', '123'), '{{m_url}}')
            self.assertEqual(render_auto_caption('{{m_url}}', '123', short_url=build_auto_short_url(1), description='D',
                m_url='https://x.test/m/real'), 'https://x.test/m/real')

    def test_exact_macro_and_provider_conflicts(self):
        with self.assertRaises(XPostError): normalize_caption('{m_url}')
        with self.assertRaises(XPostError): normalize_caption('{url} {{m_url}}')
        with self.assertRaises(TTPostError): render_caption_template('{url} {{m_url}}', '123', defer_url=True)
        with self.assertRaises(TTPostError): render_caption_template('{code} {{m_url}}', '123', defer_code=True)
        values={'title':'Video','youtube_url':'https://www.youtube.com/watch?v=abcdefghijk',
                'm_url':'https://x.test/m/preview'}
        self.assertTrue(preview('{youtube_url} {{m_url}}', values)['valid'])
        self.assertFalse(preview('{youtube_url} {m_url}', values)['valid'])
        self.assertFalse(preview('{youtube_url} {{m_url}}', dict(values, m_url=''))['valid'])

    def test_explicit_source_never_reads_internal_drama_mapping(self):
        class Selector(DramawaveCandidateSelector):
            def _pool_material_rows(self, material_id, **kwargs):
                return [dict(material_id='12',product='Moboreels',material_type=2,material_is_deleted=0,
                    material_language='en',content_id='123',material_url='https://cdn.test/video.mp4',
                    material_name='Video',video_duration=60)]
            def _pool_drama_rows(self, *args):
                raise AssertionError('internal drama ID lookup')
            def _validate_drama_deploy_time(self, *args, **kwargs):
                raise AssertionError('internal deployment lookup')
        selector=Selector(None)
        with patch.object(moboreels.macro_client,'lookup_story',return_value={
                'story_id':'123','story_name':'Moboreels title','introduce':'Moboreels intro','lang_id':3,'is_show':1}):
            with moboreels.source_policy(True):
                row=selector._pool_candidate('12','2026-11-12')
                self.assertEqual((row['content_id'],row['drama_name'],row['description']),('123','Moboreels title','Moboreels intro'))
        self.assertFalse(moboreels.source_enabled())


class CaptionFreezeTests(unittest.TestCase):
    def test_x_schedule_version_creator_is_frozen_and_never_uses_pool_uploader(self):
        from features.x_posts.service import XPostStore
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'x.sqlite'
            store = XPostStore(str(db))
            creator = dict(tenant_key='tenant', user_id='creator', name='Creator')
            payload = dict(enabled=False,account_ids=[],publish_times=[],
                body_template='{{drama_name}} {{desc}} {{m_url}}',version=1)
            first = store.save_schedule_config('material', payload, actor=creator)
            store.save_schedule_config('material', dict(payload,version=first['version']),
                actor=dict(creator,user_id='later-editor'))
            with closing(sqlite3.connect(db)) as conn:
                conn.execute('INSERT INTO x_post_schedule_run (id,slot_key,source_type,run_date,publish_time,'
                    'config_version,account_ids_json,expected_count,created_at,updated_at) '
                    'VALUES (1,?,?,?,?,?,?,?,?,?)',
                    ('slot','material','2026-11-13','08:00',first['version'],'[9]',1,'now','now'))
                conn.commit()
            queue = dict(id=7,schedule_run_id=1,manual_run_id=None,content_id='123',account_id=9,material_id='12')
            with patch.object(moboreels.macro_client,'resolve_actor',return_value=creator) as actor, \
                    patch.object(moboreels,'freeze_url',return_value='https://x.test/m/frozen') as freeze, \
                    patch('features.x_posts.attribution.resolve_via_loopback',side_effect=AssertionError('legacy pool owner')):
                self.assertEqual(moboreels.resolve_x_url(str(db),queue,dict(id=8)),'https://x.test/m/frozen')
                actor.assert_called_once_with('creator','tenant')
                self.assertEqual(freeze.call_args.kwargs['task_id'],'x-queue:7')
                with self.assertRaises(MoboreelsLinkError):
                    moboreels.resolve_x_url(str(db),dict(queue,schedule_run_id=99),dict(id=8))
                with self.assertRaises(MoboreelsLinkError):
                    moboreels.resolve_x_url(str(db),dict(queue,schedule_run_id=None),dict(id=8))
                self.assertEqual(freeze.call_count,1)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        gate = patch.object(moboreels.macro_client, '_request', return_value={'drama': {'story_id': '123', 'is_show': 1}})
        gate.start(); self.addCleanup(gate.stop)
        self.db=Path(self.tmp.name)/'ledger.sqlite'
        self.store=SimpleNamespace(db_path=str(self.db))
        self.row=dict(id=1,caption_template='Watch {{m_url}}',caption='Watch {{m_url}}',content_id='123',
            material_id='12',account_id='9',claim_token='claim',status='publishing',publish_id='',unknown_outcome=0)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('CREATE TABLE tt_post_queue(id INTEGER PRIMARY KEY,caption_template TEXT,caption TEXT,content_id TEXT,material_id TEXT,account_id TEXT,claim_token TEXT,status TEXT,publish_id TEXT,unknown_outcome INTEGER)')
            conn.execute('INSERT INTO tt_post_queue VALUES (?,?,?,?,?,?,?,?,?,?)',tuple(self.row.values()))
            freeze_queue_actor(conn,1,{'tenant_key':'tenant','user_id':'creator','name':'Creator'})
            conn.commit()

    def tearDown(self): self.tmp.cleanup()

    def test_creator_and_caption_reused_on_second_prepare(self):
        actor={'tenant_key':'tenant','user_id':'creator','name':'Creator'}
        with patch('features.tt_posts.moboreels.macro_client.resolve_actor',return_value=actor) as owner, \
             patch('features.tt_posts.moboreels.freeze_url',return_value='https://x.test/m/frozen') as link:
            first=prepare_caption(self.store,self.row,'claim')
            second=prepare_caption(self.store,self.row,'claim')
        self.assertEqual(first['caption'],second['caption'])
        self.assertEqual(link.call_count,1)
        owner.assert_called_once_with('creator','tenant')
        self.assertEqual(link.call_args.kwargs['task_id'],'tt-queue:1')
        self.assertEqual(link.call_args.kwargs['story_id'],'123')

    def test_unknown_link_result_leaves_unresolved_caption(self):
        with patch('features.tt_posts.moboreels.macro_client.resolve_actor',return_value={'user_id':'creator'}), \
             patch('features.tt_posts.moboreels.freeze_url',side_effect=MoboreelsLinkError('unknown')):
            with self.assertRaises(MoboreelsLinkError):prepare_caption(self.store,self.row,'claim')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT caption FROM tt_post_queue').fetchone()[0],'Watch {{m_url}}')
            self.assertEqual(conn.execute('SELECT short_url FROM tt_moboreels_context').fetchone()[0],'')

    def test_lost_claim_cannot_create_link(self):
        with patch('features.tt_posts.moboreels.freeze_url',side_effect=AssertionError('not permitted')):
            with self.assertRaises(MoboreelsLinkError):prepare_caption(self.store,self.row,'other-claim')


class AutoSourceTests(unittest.TestCase):
    def test_tt_selection_retains_dedup_and_never_reads_legacy_metrics(self):
        from scripts.test_tt_auto_post_selector import (FakeStore,FakeLegacyReader,FakeValidator,material,NOW)
        from features.tt_auto_posts.selector import TwoStageSelector,SelectionRules,SelectionRequest
        store=FakeStore()
        repo=SimpleNamespace(schema='kunlunads_dev',material_data_source=6,product='Dramawave',app_id=1479,
            _stream_select=lambda sql,params:[{'story_id':'123','story_name':'Moboreels','lang_id':3}],
            list_material_rows=lambda **kwargs:[material('12301','123'),material('12302','123')])
        metrics=SimpleNamespace(load=lambda **kwargs: (_ for _ in ()).throw(AssertionError('old metric lookup')))
        selector=TwoStageSelector(repo,metrics,FakeLegacyReader(seen=['12301']),store,
            material_validator=FakeValidator(overrides={'content_id':'123','drama_name':'Moboreels'}))
        request=SelectionRequest(run_id=1,task_id=2,template_id=3,template_version=1,account_id='9',
            language='en',rules=SelectionRules(),now=NOW)
        with patch.dict('os.environ',{'MOBOREELS_INSTITUTION_USER_ID':'institution'}),moboreels.source_policy(True):
            result=selector.select_and_reserve(request)
        self.assertEqual(result.reservation['material_id'],'12302')
        self.assertEqual(store.reserve_calls[0]['selection_snapshot']['source_provider'],'moboreels')
        self.assertEqual(store.reserve_calls[0]['selection_snapshot']['metric_dates'],[])

    def test_default_rules_and_authorized_material_scope(self):
        from features.x_auto_posts import repositories, selector
        from features.x_posts.moboreels_auto import Source, validate_rules
        validate_rules(selector.SelectionRules.from_mapping({'drama':{'spend':{'min':0},'d0_roas':{'min':0}}}))
        statements=[]
        repo=SimpleNamespace(schema='ads_ai',material_data_source=1,product='Dramawave',app_id=1479,
            _stream_select=lambda sql,params: statements.append((sql,params)) or [{'story_id':'123','story_name':'Name','lang_id':3}])
        source=Source(repo,repositories)
        with patch.dict('os.environ',{'MOBOREELS_INSTITUTION_USER_ID':'institution'}):
            rows=source.list_drama_rows(language='en',now_epoch=1)
        self.assertEqual(rows[0].content_id,'123')
        self.assertIn('s.data_source=%s AND s.product=%s',statements[0][0])
        self.assertNotIn('ads_drama_info',statements[0][0])
        self.assertEqual(statements[0][1],('institution',3,1,'Dramawave','en'))
        self.assertFalse(source.blacklist_snapshot().drama_series_codes)
        with self.assertRaises(MoboreelsLinkError):
            validate_rules(selector.SelectionRules.from_mapping({'drama':{'spend':{'min':1}}}))


class LiveLaneSmokeTests(unittest.TestCase):
    def test_native_x_descriptor_and_share_items_freeze_actual_urls(self):
        root=Path(__file__).resolve().parents[1]
        code='''import sys,sqlite3,tempfile,json
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager,closing
from unittest.mock import patch
r=Path(sys.argv[1]);sys.path.insert(0,str(r/'ops/moboreels-attribution-20261113/runtime/x'))
import features
features.__path__.append(str(r/'features'))
from features.pipeline_dispatch.x_native import XNativeBridge,posts
from features.x_posts import moboreels
b=object.__new__(XNativeBridge)
b.r=SimpleNamespace(POST_DB_PATH='unused',POST_SHORT_BASE_URL='https://x.test/s',POST_PUBLIC_ROOT='unused',
 POST_STORAGE_MOUNT_ROOT='unused',POST_STORAGE_ROOT='unused',preflight_post_storage_request=lambda:None)
b.store=SimpleNamespace(prepare_log=lambda i,l,s,text:dict(id=i,long_url=l,short_url=s,post_text=text))
queue=dict(id=7,account_id=9,content_id='123',material_id='12',preflight_duration=30,source_type='material',
 drama_name='Name',description='Description',body_template='{name} {{m_url}}',copy_contract_version=2,
 material_language='en',tag='Moboreels',page_name='Page',page_id='9',material_name='Material',account_username='acct')
with patch.dict('os.environ',{'X_POST_ATTRIBUTION_V2_ENABLED':'0'}),patch.object(moboreels,'resolve_x_url',return_value='https://x.test/m/actual') as resolve,patch.object(posts,'_build_short_url',return_value='https://gy.g2flow.com/s2l/8.html'),patch.object(posts,'build_w2a_url',return_value='https://old.test/path'),patch.object(posts,'write_short_redirect'):
 log=b._links(queue,dict(id=8))
 assert log['post_text']=='Name https://x.test/m/actual'
 assert b._links(queue,log)==log
 assert resolve.call_count==1
from features.x_accounts.youtube_shares import YouTubeShares
with tempfile.TemporaryDirectory() as directory:
 db=str(Path(directory)/'share.sqlite')
 @contextmanager
 def connect(*,write=False):
  with closing(sqlite3.connect(db)) as c:
   c.row_factory=sqlite3.Row
   yield c
   c.commit()
 with connect(write=True) as c:
  c.execute('CREATE TABLE youtube_share_item(id INTEGER PRIMARY KEY,status TEXT,claim_token TEXT,attempted_at REAL,claimed_at REAL,duplicate_of INTEGER)')
  c.executemany('INSERT INTO youtube_share_item(id,status,claim_token,attempted_at) VALUES (?,?,?,NULL)',[(1,'queued',''),(2,'queued','')])
  c.execute('CREATE TABLE youtube_share_moboreels(run_id TEXT PRIMARY KEY,story_id TEXT,material_id TEXT)')
  c.execute('INSERT INTO youtube_share_moboreels VALUES (?,?,?)',('r','123','12'))
  c.execute('CREATE TABLE youtube_share_moboreels_item(item_id INTEGER PRIMARY KEY,short_url TEXT,text TEXT)')
 svc=object.__new__(YouTubeShares);svc._connect=connect
 run=dict(id='r',description_template='{youtube_url} {{m_url}}',text='https://www.youtube.com/watch?v=abcdefghijk {{m_url}}',actor_user='creator',actor_tenant='tenant')
 actor=dict(user_id='creator',tenant_key='tenant',name='Creator')
 with patch('features.moboreels_library.macro_client.resolve_actor',return_value=actor),patch.object(moboreels,'freeze_url',side_effect=lambda actor,**kw:'https://x.test/m/'+str(kw['account_id'])) as freeze:
  a=svc._moboreels_text(dict(id=1,account_id=91,run=run),allow_queued=True)
  again=svc._moboreels_text(dict(id=1,account_id=91,run=run),allow_queued=True)
  b=svc._moboreels_text(dict(id=2,account_id=92,run=run),allow_queued=True)
  assert a==again and a!=b and freeze.call_count==2
  assert freeze.call_args.kwargs['story_id']=='123'
  assert '{{m_url}}' not in a and '/m/preview' not in a
  from features.pipeline_dispatch.x_share_native import XShareBridge
  with connect(write=True) as c:
   c.execute('CREATE TABLE pipeline_canonical_hold(source_key TEXT PRIMARY KEY)')
   c.execute('CREATE TABLE pipeline_share_claim(source_key TEXT,generation INTEGER,claim_hash TEXT,native_claim TEXT,descriptor_json TEXT,PRIMARY KEY(source_key,generation))')
   c.execute('INSERT INTO youtube_share_item(id,status,claim_token,attempted_at) VALUES (?,?,?,NULL)',(3,'queued',''))
  fresh=dict(id=3,account_id=93,run=run,claim_token='')
  native=object.__new__(XShareBridge);native.service=svc;native.item=lambda identity:dict(fresh);native.verify=lambda item:actor
  svc.clock=lambda:12345
  job=dict(channel='x',family='youtube_share',stage='publish',source_key='x:youtube_share:3',source_id=3,generation=1)
  claim=dict(claim_token='a'*32)
  descriptor=native.reserve(job,claim)
  assert descriptor['request']['text'].endswith('/m/93')
  assert native.reserve(job,claim)==descriptor
  assert freeze.call_count==3
  with connect() as c:
   assert c.execute('SELECT status FROM youtube_share_item WHERE id=3').fetchone()[0]=='publishing'
   assert json.loads(c.execute('SELECT descriptor_json FROM pipeline_share_claim').fetchone()[0])==descriptor
'''
        result=subprocess.run([sys.executable,'-c',code,str(root)],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_each_distinct_live_lane_renders_without_creating_links(self):
        root=Path(__file__).resolve().parents[1]
        code='''import sys
from pathlib import Path
r=Path(sys.argv[1]);lane=sys.argv[2]
sys.path.insert(0,str(r/'ops/moboreels-attribution-20261113/runtime'/lane))
import features
features.__path__.append(str(r/'features'))
from features.x_posts.moboreels import source_policy
if lane=='x':
 from features.x_posts.service import build_post_text
 from features.x_video_publish.templates import render_caption
 from features.x_auto_posts.selector import SelectionRules
 assert build_post_text('https://x.test/old','Name','Desc','{{drama_name}} {{desc}} {{m_url}}',m_url='https://x.test/m/real').endswith('/m/real')
 assert render_caption('{name} {{m_url}}','Name','Desc','https://x.test/old',m_url='https://x.test/m/real').endswith('/m/real')
else:
 from features.tt_posts.core import render_caption_template
 from features.tt_auto_posts.links import render_auto_caption,build_auto_short_url
 from features.tt_auto_posts.selector import SelectionRules
 assert render_caption_template('{{m_url}}','123',m_url='https://x.test/m/real')=='https://x.test/m/real'
 assert render_auto_caption('{{m_url}}','123',short_url=build_auto_short_url(1),description='Description',m_url='https://x.test/m/real')=='https://x.test/m/real'
from features.x_posts.moboreels_auto import validate_rules
validate_rules(SelectionRules())
'''
        for lane in ('x','tt','tt_auto'):
            with self.subTest(lane=lane):
                result=subprocess.run([sys.executable,'-c',code,str(root),lane],capture_output=True,text=True,encoding='utf-8')
                self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__': unittest.main()
