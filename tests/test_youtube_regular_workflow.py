import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from features.drama_synthesis.core import DramaSynthesisStore
from features.youtube_auto_publish.regular import RegularPublication
from features.youtube_auto_publish.regular_media import dimensions, editor_object_sha
from features.youtube_auto_publish.regular_routes import edit_title, stream_preview
from features.youtube_auto_publish.regular_titles import title_prompt, validated_titles
from features.youtube_auto_publish.service import YouTubeWorkflow
from features.youtube_auto_publish.templates import WorkflowError


def photo(color):
    buffer = io.BytesIO()
    Image.new('RGB', (640, 360), color).save(buffer, format='JPEG')
    return buffer.getvalue()


class Catalog:
    allowed_hosts = ('example.org',)

    def get(self, material_id, **kwargs):
        return dict(id=str(material_id), name='Cut ' + str(material_id), url='https://example.org/' + str(material_id),
                    content_id='original', app_id='1479', language='en', macro_name='Drama', macro_desc='Final wedding',
                    drama_cover_url='https://static-v1.mydramawave.com/cover.png', drama_cover_status='available')

    def configuration(self):
        return '', {'configured': True}


class Ordinary(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.engine = DramaSynthesisStore(self.root / 'ledger.sqlite')
        self.engine.ensure_storage()
        self.actor = {'tenant_key':'team','user_id':'operator','name':'operator'}
        self.catalog = Catalog()
        self.options = [dict(id=str(i), name='Channel '+str(i), eligible=True, comment_eligible=True,
                             channel_id='UC' + str(i) * 22, youtube_account_id='7', scopes=['youtube']) for i in (1, 2)]
        self.workflow = YouTubeWorkflow(self.engine.db_path, self.root/'assets', self.catalog,
            lambda actor: self.options, lambda material:'https://example.org/link', self.engine,
            generate=lambda body, version:photo('white'), fetch_reference_cover=lambda url:photo('red'))
        self.sha = hashlib.sha256(b'actual production video').hexdigest()
        self.editor = dict(sha256=self.sha, task_id='at_'+'7'*32, version='v2', product='dramawave',
                           content_id='original', language='en', size_bytes=23, width=1920, height=1080,
                           duration_ms=120000, evidence=[{'description':'They meet at the station','transcript':'Hello'}])
        self.reg = RegularPublication(self.workflow, evidence=lambda mid,cookie:dict(self.editor, material_id=mid,
                                      url=self.catalog.get(mid)['url']), generate=lambda prompt, root: ['One', 'Two', 'Three'])
        self.addCleanup(self.reg.pool.shutdown)
        self.cover = self.workflow.upload_cover(self.actor, {'data':base64.b64encode(photo('navy')).decode()})['asset']['id']
        self.base = dict(material_id='140', sha256=self.sha, channel_id='1', title_template='At the station',
                         description_template='Watch', comment_template='', cover_source='local', cover_asset_id=self.cover,
                         requirements='Use the protagonists',operation_id='prepare-ordinary-0001')

    def prepare(self, **changes):
        payload = dict(self.base, **changes)
        self.reg.describe(self.actor,payload['material_id'],'session')
        return self.reg.prepare(self.actor,payload)['task']

    def ledger_count(self):
        with self.workflow.db() as db:
            return db.execute('SELECT COUNT(*) FROM drama_youtube_publish').fetchone()[0]

    def test_local_preview_title_revision_and_idempotent_final(self):
        task=self.prepare()
        self.assertEqual(task['status'], 'review')
        self.assertTrue(task['regular_media']['preview_url'].startswith('/api/ai-video-editing/tasks/at_'))
        self.assertFalse(task['versions'][0]['approved'])
        self.assertEqual(self.reg.prepare(self.actor,self.base)['task']['id'],task['id'])
        self.assertEqual(self.workflow.run_once()['claimed'],False)
        old={'action':'approve','version':1,'review_token':task['review_token']}
        edited=edit_title(self.workflow,self.actor,task['id'],{'title':'Second title','review_token':old['review_token']})['task']
        self.assertEqual(edited['title'],'Second title')
        with self.assertRaises(WorkflowError):
            self.workflow.review(self.actor,task['id'],old)
        manual=self.workflow.review(self.actor,task['id'],{'action':'manual','version':1,'cover_asset_id':self.cover})['task']
        self.assertEqual(manual['status'],'review'); self.assertFalse(manual['versions'][-1]['approved'])
        self.assertEqual(self.ledger_count(),0)
        approval={'action':'approve','version':2,'review_token':manual['review_token']}
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(lambda _:self.workflow.review(self.actor,task['id'],approval)['task']['status'], [1,2]))
        self.assertEqual(result,['enqueue_pending','enqueue_pending'])
        self.assertEqual(self.ledger_count(),0)
        self.workflow.run_once()
        with self.workflow.db() as db:
            row=db.execute('SELECT source_material_id,source_sha256,source_size,title,channel_id FROM drama_youtube_publish').fetchone()
        self.assertEqual(tuple(row),('140',self.sha,23,'Second title',self.options[0]['channel_id']))
        self.workflow.review(self.actor,task['id'],approval)
        self.assertEqual(self.ledger_count(),1)

    def test_ai_cover_is_reviewed_and_schedule_invalidates_old_confirmation(self):
        task=self.prepare(cover_source='ai',cover_asset_id='')
        self.assertEqual(task['status'],'queued_generation')
        self.workflow.run_once()
        task=self.workflow.get_task(self.actor,task['id'])['task']
        self.assertEqual(task['status'],'review',task.get('error'))
        old=task['review_token']
        at=(datetime.now(timezone.utc)+timedelta(hours=5)).isoformat(timespec='seconds')
        scheduled=self.workflow.schedule(self.actor,task['id'],{'action':'reschedule','publish_at':at,
                                  'schedule_version':0,'operation_id':'reschedule-regular-001'})['task']
        self.assertNotEqual(scheduled['review_token'],old)
        with self.assertRaises(WorkflowError):
            self.workflow.review(self.actor,task['id'],{'action':'approve','version':1,'review_token':old})
        self.assertEqual(self.ledger_count(),0)
        self.workflow.review(self.actor,task['id'],{'action':'approve','version':1,'review_token':scheduled['review_token']})
        for _ in range(3): self.workflow.run_once()
        row=self.engine.get_reviewed_youtube_by_preparation(task['id'])
        self.assertTrue(row['publish_at'])

    def test_legacy_local_behavior_preserved(self):
        legacy=self.workflow.create_task(self.actor,self.base)['task']
        self.assertEqual(legacy['status'],'enqueue_pending')
        self.assertNotIn('regular_media',legacy)

    def test_reschedule_after_approval_before_first_enqueue_requires_new_review(self):
        task = self.prepare()
        admin = dict(self.actor,user_id='another-user',role='admin')
        self.assertFalse(self.workflow.get_task(admin,task['id'])['task']['can_review'])
        original = dict(action='approve',version=1,review_token=task['review_token'])
        self.workflow.review(self.actor,task['id'],original)
        at = (datetime.now(timezone.utc)+timedelta(days=1)).isoformat(timespec='seconds')
        updated = self.workflow.schedule(self.actor,task['id'],dict(action='reschedule',publish_at=at,
             schedule_version=0,operation_id='reschedule-after-approval'))['task']
        self.assertEqual(updated['status'],'review')
        with self.assertRaises(WorkflowError):
            self.workflow.review(self.actor,task['id'],original)
        self.workflow.run_once()
        self.assertEqual(self.ledger_count(),0)
        self.workflow.review(self.actor,task['id'],dict(original,review_token=updated['review_token']))
        self.workflow.run_once()
        self.assertEqual(self.ledger_count(),1)

    def test_single_short_isolated_and_horizontal_and_long_vertical_admitted(self):
        for width,height,length,blocked in [(720,1280,180000,True),(720,720,180000,True),
                                           (720,1280,180001,False),(1920,1080,4000,False)]:
            self.editor.update(width=width,height=height,duration_ms=length)
            resp=self.reg.describe(self.actor,'140','a')
            self.assertEqual(resp['shorts'],blocked)
            if blocked:
                with self.assertRaisesRegex(WorkflowError,'不超过 3 分钟'):
                    self.reg.prepare(self.actor,self.base)
            else:
                task=self.reg.prepare(self.actor,dict(self.base,operation_id='regular-test-length-'+str(length)))['task']
                self.assertEqual(task['status'],'review')

    def test_defaults_and_private_evidence(self):
        first=self.reg.describe(self.actor,'140','session')
        self.assertEqual(first['default_channel'],'')
        self.reg.set_default(self.actor,'140','2')
        self.assertEqual(self.reg.describe(self.actor,'141','session')['default_channel'],'2')
        stranger=dict(self.actor,user_id='other')
        self.assertEqual(self.reg.default(stranger,self.catalog.get('140')),'')
        with self.assertRaises(WorkflowError):
            self.reg.stored(stranger,'140',self.sha)
        self.assertEqual(self.reg.titles(self.actor,dict(self.base,mode='clip'))['titles'],['One','Two','Three'])
        clip=title_prompt('clip',first['material'],self.editor['evidence'],'secret future wedding','')
        self.assertIn('station',clip); self.assertNotIn('wedding',clip)
        with self.assertRaises(WorkflowError):
            title_prompt('clip',first['material'],[],'Full synopsis','')
        self.assertIn('Final wedding',title_prompt('story',first['material'],[],'',''))
        self.assertIn('Owner synopsis',title_prompt('story',first['material'],[],'Owner synopsis',''))
        with self.assertRaises(WorkflowError):
            validated_titles(['One','one','Two'])

    def test_legacy_snapshot_bytes_range_and_terminal_cleanup(self):
        evidence_calls = []
        self.reg.evidence=lambda mid,cookie:evidence_calls.append(mid)
        event=threading.Event()
        def stage(material,root,hosts):
            root.mkdir(parents=True,exist_ok=True)
            (root/(self.sha+'.video')).write_bytes(b'actual production video')
            event.set()
            return dict(self.editor, snapshot=True)
        self.reg.stage=stage
        self.assertEqual(self.reg.describe(self.actor,'140','c')['status'],'checking')
        self.assertTrue(event.wait(3))
        for i in range(50):
            record=self.reg.describe(self.actor,'140','c',poll=True)
            if record['status']=='ready': break
            time.sleep(.01)
        self.assertTrue(record['preview_url'].endswith(self.sha))
        self.assertEqual(evidence_calls,['140'])
        from unittest.mock import Mock
        handler=Mock(headers={'Range':'bytes=7-16'},wfile=io.BytesIO())
        stream_preview(handler,self.reg,self.actor,'140',self.sha)
        handler.send_response.assert_called_once_with(206)
        self.assertEqual(handler.wfile.getvalue(),b'production')
        task=self.reg.prepare(self.actor,self.base)['task']
        self.workflow.review(self.actor,task['id'],dict(action='approve',version=1,review_token=task['review_token']))
        self.workflow.run_once()
        with self.workflow.db(True) as db:
            db.execute('UPDATE youtube_regular_material SET seen=0')
        self.reg.last_sweep=0; self.reg._sweep()
        self.assertTrue((self.reg.root/(self.sha+'.video')).exists())
        with self.workflow.db(True) as db:
            db.execute("UPDATE drama_youtube_publish SET video_state='published', status='published' WHERE preparation_id=?",(task['id'],))
            self.assertEqual(db.execute("SELECT state FROM youtube_auto_preparation WHERE id=?",(task['id'],)).fetchone()[0],'uploading')
        self.reg.last_sweep=0; self.reg._sweep()
        self.assertFalse((self.reg.root/(self.sha+'.video')).exists())

    def test_formal_url_digest_guard(self):
        material = self.catalog.get('140')
        material['url'] = ('https://example.org/ai-video-editing/youtube-final/v1/'
                           'at_' + '2' * 32 + '-' + self.sha + '.mp4')
        self.assertEqual(editor_object_sha(material['url']),self.sha)
        self.assertEqual(editor_object_sha(material['url'] + '/download'),'')
        with self.assertRaisesRegex(WorkflowError,'定稿版本不符'):
            self.reg._save(self.actor,material,dict(self.editor,sha256='a'*64))
        self.reg.evidence = lambda mid,cookie:None
        with patch.object(self.reg,'_catalog',return_value=material), patch.object(self.reg,'stage') as stage:
            with self.assertRaisesRegex(WorkflowError,'定稿尚未读到版本回执'):
                self.reg.describe(self.actor,'140','different-operator')
            stage.assert_not_called()

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'ffmpeg programs required')
    def test_real_video_dimensions(self):
        target=self.root/'sample.mp4'
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=navy:s=160x90:r=25',
                        '-t','1','-pix_fmt','yuv420p','-c:v','libx264',str(target)],check=True)
        self.assertEqual(dimensions(target),(160,90,1000))
