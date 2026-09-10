"""Isolated preparation-workflow tests. All platforms and SQL runners are mocked.

Run: python scripts/test_youtube_auto_service.py
"""
from __future__ import annotations

import base64
import copy
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
from features.drama_synthesis.core import build_long_url
from features.youtube_auto_publish.service import YouTubeWorkflow
from features.youtube_auto_publish.source import MaterialSource, validate_sql
from features.youtube_auto_publish.templates import WorkflowError, long_url, render
from features.youtube_auto_publish.runtime import generate_cover_factory, notify_factory, readonly_runner


def png(width=1280, height=720):
    out = io.BytesIO()
    Image.new('RGB', (width, height), '#183aa1').save(out, 'PNG')
    return out.getvalue()


class EngineStore:
    def __init__(self):
        self.tasks = {}
        self.enqueues = []
        self.retries = []

    def youtube_task(self, task_id):
        return copy.deepcopy(self.tasks[task_id])

    def enqueue_reviewed_youtube(self, **payload):
        for existing in self.tasks.values():
            if existing['operation_id'] == payload['operation_id']:
                return copy.deepcopy(existing)
        self.enqueues.append(copy.deepcopy(payload))
        task_id = len(self.tasks) + 1
        value = dict(id=task_id, operation_id=payload['operation_id'], status='pending',
                     reviewed_phase='upload', video_id='', video_state='pending',
                     thumbnail_status='pending', processing_status='pending',
                     comment_status='pending' if payload['comment_text'] else 'skipped',
                     unknown_outcome=False)
        self.tasks[task_id] = value
        return copy.deepcopy(value)

    def retry_reviewed_youtube(self, task_id):
        self.retries.append(task_id)


class WorkflowCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='youtube-workflow-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.actor = {'tenant_key':'tenant-one', 'user_id':'owner-one', 'role':'user',
                      'name':'Test owner', 'open_id':'test-open-id'}
        self.admin = dict(self.actor, user_id='tenant-admin', role='admin')
        self.other = dict(self.actor, user_id='other-user')
        self.foreign = dict(self.admin, tenant_key='tenant-two')
        self.material = dict(id='1', name='Test material',
                             url='https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/test.mp4',
                             thumbnail_url='', source_job_id='a'*32, content_id='drama/one',
                             source_kind='concat_video', macro_name='Test Drama', macro_desc='A short synopsis.',
                             language='en', duration='12:30', size='80 MB', app_id='1479')
        self.sql_file = self.root/'material.sql'
        self.sql_file.write_text('SELECT * FROM source_for_unit_test', encoding='utf-8')
        self.sql_calls = []
        def query(sql):
            self.sql_calls.append(sql)
            match = re.search(r"CAST\(pool.id AS CHAR\)='(\d+)'", sql)
            return [] if match and match[1] != '1' else [(json.dumps(self.material).encode().hex(),)]
        self.source = MaterialSource(str(self.sql_file), query)
        self.channel = dict(id='channel-1', name='Test channel', channel_id='UCtest',
                            youtube_account_id='account-secret-id', language='en', eligible=True,
                            comment_eligible=True, scopes=['youtube.force-ssl'])
        self.channels = Mock(side_effect=lambda actor:[copy.deepcopy(self.channel)])
        self.short_link = Mock(return_value='https://example.invalid/short-for-tests')
        self.generate = Mock(return_value=png())
        self.notify = Mock(return_value='mock-message-id')
        self.store = EngineStore()
        self.service = YouTubeWorkflow(self.root/'jobs.sqlite3', self.root/'assets', self.source,
                                       self.channels, self.short_link, self.store,
                                       generate=self.generate, notify=self.notify,
                                       public_base='https://example.invalid')
        self.sequence = 0

    def payload(self, **changes):
        self.sequence += 1
        value = dict(operation_id=f'operation_for_test_{self.sequence:04d}', material_id='1',
                     channel_id='channel-1', title_template='{name}',
                     description_template='{desc}\nWatch: {url}', comment_template='',
                     cover_source='ai', requirements='Cinematic 16:9 thumbnail.', cover_asset_id='')
        value.update(changes)
        return value

    def create(self, actor=None, **changes):
        return self.service.create_task(actor or self.actor, self.payload(**changes))['task']

    def assert_error(self, code, callback, status=None):
        with self.assertRaises(WorkflowError) as ctx:
            callback()
        self.assertEqual(ctx.exception.code, code)
        if status is not None:
            self.assertEqual(ctx.exception.status, status)
        return ctx.exception

    def cover(self, actor=None, data=None):
        return self.service.upload_cover(actor or self.actor, {
            'file_name':'unit-test.png', 'data':base64.b64encode(png() if data is None else data).decode()
        })['asset']

    def ready(self, **changes):
        value = self.create(**changes)
        self.service.run_once()
        result = self.service.get_task(self.actor,value['id'])['task']
        self.assertEqual(result['status'],'review')
        return result

    def enqueue(self, **changes):
        asset = self.cover()
        task = self.create(cover_source='local',cover_asset_id=asset['id'],**changes)
        self.service.run_once()
        return self.service.get_task(self.actor,task['id'])['task']

    def test_unconfigured_sql_never_queries_or_lists_channels(self):
        self.source.sql_file = ''
        result = self.service.bootstrap(self.actor)
        self.assertFalse(result['source']['configured'])
        self.assertEqual(result['channels'],[])
        self.assertEqual(self.service.list_materials(self.actor)['items'],[])
        self.channels.assert_not_called()
        self.assertEqual(self.sql_calls,[])
        self.assert_error('material_source_unconfigured',lambda:self.create(),409)
        self.short_link.assert_not_called()

    def test_reserved_sql_file_can_be_replaced_without_restart(self):
        self.sql_file.write_text('-- SQL to be provided\n',encoding='utf-8')
        self.assertFalse(self.service.list_materials(self.actor)['configured'])
        self.sql_file.write_text('SELECT * FROM configured_source',encoding='utf-8')
        self.assertEqual(len(self.service.list_materials(self.actor)['items']),1)
        self.assertIn('configured_source',self.sql_calls[-1])

    def test_source_fixed_product_and_url_filter(self):
        items = self.service.list_materials(self.actor)['items']
        self.assertEqual(items[0]['macro_url'],'')
        self.assertEqual(items[0]['long_url'],build_long_url('a'*32,'drama/one'))
        self.assertIn("CAST(pool.app_id AS CHAR)='1479'",self.sql_calls[-1])
        self.assertIn('LIMIT 100',self.sql_calls[-1])
        self.material['url']='https://evil.example/private'
        self.source.cache=None
        self.assertEqual(self.service.list_materials(self.actor)['items'],[])

    def test_source_revalidates_removed_material_at_submit(self):
        self.service.list_materials(self.actor)
        self.assert_error('material_unavailable',lambda:self.create(material_id='2'),409)
        self.assertGreaterEqual(len(self.sql_calls),2)

    def test_search_filters_database_before_limit_and_hex_encodes_injection_text(self):
        all_rows=[dict(self.material,id=str(i),name=f'Material {i:03d}') for i in range(1,102)]
        all_rows[-1]['name']='专属素材 TargetBeyond100'
        queries=[]
        def filtered_runner(sql):
            queries.append(sql)
            rows=all_rows
            match=re.search(r'LOCATE\(LOWER\(CONVERT\(0x([0-9a-f]+) USING utf8mb4\)\)',sql)
            if match:
                needle=bytes.fromhex(match[1]).decode('utf-8').casefold()
                rows=[row for row in rows if needle in (row['id']+' '+row['name']).casefold()]
            return [(json.dumps(row).encode().hex(),) for row in rows[:100]]
        self.source.query_runner=filtered_runner
        initial=self.service.list_materials(self.actor)
        self.assertEqual(len(initial['items']),100)
        self.assertNotIn('101',[row['id'] for row in initial['items']])
        searched=self.service.list_materials(self.actor,search='专属素材 TargetBeyond100')
        self.assertEqual([row['id'] for row in searched['items']],['101'])
        self.assertLess(queries[-1].index('LOCATE('),queries[-1].rindex('LIMIT 100'))
        self.assertNotIn('专属素材 TargetBeyond100',queries[-1])
        previous_count=len(queries)
        self.service.list_materials(self.actor,search='专属素材 TargetBeyond100')
        self.assertEqual(len(queries),previous_count,'identical normalized search should reuse its cache')
        by_id=self.service.list_materials(self.actor,search='101')
        self.assertEqual([row['id'] for row in by_id['items']],['101'])
        self.assertEqual(len(queries),previous_count+1,'a changed search must issue its own query')
        injection="x' OR 1=1 --"
        result=self.service.list_materials(self.actor,search=injection)
        self.assertEqual(result['items'],[])
        self.assertNotIn(injection,queries[-1])
        self.assertNotIn(injection.casefold(),queries[-1])
        self.assertIn('0x'+injection.casefold().encode('utf-8').hex(),queries[-1])
        self.assertLess(queries[-1].index('LOCATE('),queries[-1].rindex('LIMIT 100'))

    def test_sql_mutations_and_multiple_statements_rejected(self):
        for sql in ['DELETE FROM x','SELECT 1; SELECT 2','SELECT SLEEP(5)','SELECT 1 INTO OUTFILE "/tmp/x"']:
            with self.subTest(sql=sql):
                self.assert_error('material_sql_invalid',lambda:validate_sql(sql),503)
        self.assertEqual(validate_sql(' SELECT id FROM x; '),'SELECT id FROM x')

    def test_single_pass_macros_all_fields_and_frozen_snapshot(self):
        self.material['macro_name']='Literal {url}'
        result=self.create(title_template='{name}',description_template='{desc}\n{url}',comment_template='{name}')
        self.assertEqual(result['title'],'Literal {url}')
        self.assertEqual(result['comment'],'Literal {url}')
        self.assertEqual(result['description'],'A short synopsis.\nhttps://example.invalid/short-for-tests')
        self.short_link.assert_called_once()
        self.material['macro_name']='Changed later'
        saved=self.service.get_task(self.actor,result['id'])['task']
        self.assertEqual(saved['title'],'Literal {url}')
        self.assertEqual(saved['title_template'],'{name}')

    def test_unknown_and_double_brace_macros_rejected_before_short_link(self):
        for template in ('{unknown}','{{name}}'):
            with self.subTest(template=template):
                self.assert_error('unknown_macro',lambda:self.create(title_template=template))
        self.short_link.assert_not_called()

    def test_invalid_cover_preflight_has_no_short_link_side_effect(self):
        foreign_asset=self.cover(self.other)
        cases=[('cover_source_required',{'cover_source':'unsupported'}),
               ('cover_requirements_required',{'requirements':''}),
               ('cover_requirements_required',{'requirements':'x'*2001}),
               ('not_found',{'cover_source':'local','cover_asset_id':'missing'}),
               ('not_found',{'cover_source':'local','cover_asset_id':foreign_asset['id']})]
        for code,changes in cases:
            with self.subTest(code=code,source=changes.get('cover_source','ai')):
                self.assert_error(code,lambda:self.create(**changes))
                self.short_link.assert_not_called()
                self.assertEqual(self.service.list_tasks(self.actor)['total'],0)

    def test_missing_or_oversized_non_url_macros_have_no_short_link_side_effect(self):
        cases=[('macro_desc','','macro_source_missing'),
               ('macro_name','x'*101,'text_too_long'),
               ('macro_desc','中'*1667,'text_too_long')]
        original=dict(self.material)
        for field,value,code in cases:
            with self.subTest(field=field,length=len(value)):
                self.material.clear()
                self.material.update(original)
                self.material[field]=value
                self.assert_error(code,lambda:self.create())
                self.short_link.assert_not_called()
                self.assertEqual(self.service.list_tasks(self.actor)['total'],0)

    def test_invalid_url_source_association_rejected(self):
        self.material['source_job_id']='invalid'
        self.assert_error('source_association_missing',lambda:self.create(),409)
        self.short_link.assert_not_called()

    def test_macros_unicode_character_and_utf8_byte_limits(self):
        m=dict(macro_name='😀'*100,macro_desc='中'*1666,macro_url='https://example.invalid',source_job_id='a'*32,content_id='drama')
        self.assertEqual(len(render('{name}',m,'title')),100)
        self.assertEqual(len(render('{desc}',m,'description').encode()),4998)
        self.assert_error('text_too_long',lambda:render('{name}x',m,'title'))
        self.assert_error('text_too_long',lambda:render('{desc}中',m,'description'))
        self.assertEqual(render('',m,'comment'),'')
        self.assertEqual(long_url(m),build_long_url('a'*32,'drama'))

    def test_idempotent_replay_returns_same_task_and_conflicting_payload_rejected(self):
        payload=self.payload()
        first=self.service.create_task(self.actor,payload)['task']
        second=self.service.create_task(self.actor,payload)['task']
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(self.service.list_tasks(self.actor)['total'],1)
        self.short_link.assert_called_once()
        changed=dict(payload,title_template='changed')
        self.assert_error('idempotency_conflict',lambda:self.service.create_task(self.actor,changed),409)

    def test_concurrent_same_operation_has_one_preparation(self):
        payload=self.payload(description_template='Static description')
        barrier=threading.Barrier(2)
        def create():
            barrier.wait(timeout=5)
            return self.service.create_task(self.actor,payload)['task']['id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids=list(pool.map(lambda _:create(),range(2)))
        self.assertEqual(len(set(ids)),1)
        self.assertEqual(self.service.list_tasks(self.actor)['total'],1)

    def test_owner_and_tenant_isolation_with_tenant_admin(self):
        value=self.create()
        self.assert_error('not_found',lambda:self.service.get_task(self.other,value['id']),404)
        self.assert_error('not_found',lambda:self.service.get_task(self.foreign,value['id']),404)
        self.assertEqual(self.service.list_tasks(self.other)['items'],[])
        self.assertEqual(self.service.get_task(self.admin,value['id'])['task']['id'],value['id'])
        self.assertEqual(self.service.list_tasks(self.admin)['total'],1)

    def test_anonymous_actor_rejected(self):
        self.assert_error('cookie_auth_required',lambda:self.service.bootstrap({}),401)
        self.assert_error('cookie_auth_required',lambda:self.service.list_tasks({}),401)

    def test_settings_only_tenant_admin_and_no_existing_task_changes(self):
        value=self.create()
        self.assert_error('forbidden',lambda:self.service.save_settings(self.actor,{'default_description':'new'}),403)
        self.service.save_settings(self.admin,{'default_description':'{name}\n{url}'})
        self.assertEqual(self.service.settings(self.actor)['default_description'],'{name}\n{url}')
        self.assertNotEqual(self.service.settings(self.foreign)['default_description'],'{name}\n{url}')
        self.assertEqual(self.service.get_task(self.actor,value['id'])['task']['description'],value['description'])

    def test_dtos_do_not_expose_credential_or_creator_details(self):
        value=self.create()
        for key in ('creator','request','lease_token'):
            self.assertNotIn(key,value)
        for data in [self.service.bootstrap(self.actor)['channels'][0],value['channel']]:
            self.assertNotIn('scopes',data)
            self.assertNotIn('youtube_account_id',data)

    def test_local_cover_is_immutable_owner_scoped_before_enqueue(self):
        asset=self.cover()
        self.assert_error('not_found',lambda:self.service.asset(self.other,asset['id']),404)
        result=self.create(cover_source='local',cover_asset_id=asset['id'])
        self.assertEqual(result['status'],'enqueue_pending')
        self.assertEqual(len(self.store.enqueues),0)
        self.service.run_once()
        self.assertEqual(len(self.store.enqueues),1)
        self.assertEqual(self.store.enqueues[0]['source_material_id'],'1')
        self.assertEqual(self.store.enqueues[0]['app_id'],'1479')
        self.generate.assert_not_called()

    def test_invalid_oversize_small_and_non_raster_covers_rejected(self):
        for data in (b'<svg/>',b'x'*(2*1024*1024+1),png(50,50)):
            with self.subTest(size=len(data)):
                with self.assertRaises(WorkflowError):
                    self.cover(data=data)
        self.assert_error('cover_invalid',lambda:self.service.upload_cover(self.actor,{'data':'not base64'}))

    def test_tampered_cover_blocks_enqueue(self):
        asset=self.cover()
        value=self.create(cover_source='local',cover_asset_id=asset['id'])
        path=Path(self.service.asset(self.actor,asset['id'])['path'])
        path.write_bytes(b'tampered')
        self.service.run_once()
        result=self.service.get_task(self.actor,value['id'])['task']
        self.assertEqual(result['status'],'enqueue_failed')
        self.assertEqual(result['error']['code'],'cover_changed')
        self.assertEqual(self.store.enqueues,[])

    def test_ai_requires_review_before_enqueue_and_notification_is_server_owned(self):
        result=self.ready()
        self.assertEqual(self.store.enqueues,[])
        self.assertEqual(result['notification']['status'],'pending')
        self.service.run_once()
        refreshed=self.service.get_task(self.actor,result['id'])['task']
        self.assertEqual(refreshed['notification']['status'],'sent')
        self.assertEqual(self.store.enqueues,[])
        self.notify.assert_called_once()
        args=self.notify.call_args.args
        self.assertEqual(args[1],1)
        self.assertEqual(args[2],f"https://example.invalid/youtube-publish.html?task_id={result['id']}&version=1")

    def test_approve_current_cover_enqueues_only_once(self):
        value=self.ready()
        result=self.service.review(self.actor,value['id'],{'action':'approve','version':1})['task']
        self.assertEqual(result['status'],'enqueue_pending')
        self.assertFalse(result['can_review'])
        self.assert_error('review_conflict',lambda:self.service.review(self.actor,value['id'],{'action':'approve','version':1}),409)
        for _ in range(3):
            self.service.run_once()
        self.assertEqual(len(self.store.enqueues),1)
        self.assertTrue(self.store.enqueues[0]['approved_cover_sha256'])

    def test_reject_requires_feedback_creates_new_version_and_preserves_history(self):
        value=self.ready()
        self.assert_error('feedback_required',lambda:self.service.review(self.actor,value['id'],{'action':'reject','version':1,'feedback':'  '}))
        self.service.review(self.actor,value['id'],{'action':'reject','version':1,'feedback':'Increase contrast'})
        for _ in range(3):
            self.service.run_once()
        result=self.service.get_task(self.actor,value['id'])['task']
        self.assertEqual(result['status'],'review')
        self.assertEqual(result['current_version'],2)
        self.assertEqual(len(result['versions']),2)
        self.assertEqual(result['versions'][1]['feedback'],'Increase contrast')
        self.assertEqual(self.generate.call_count,2)
        self.assertEqual(self.store.enqueues,[])
        self.assert_error('review_conflict',lambda:self.service.review(self.actor,value['id'],{'action':'approve','version':1}),409)

    def test_manual_replacement_preserves_ai_history_and_approves_new_version(self):
        value=self.ready()
        asset=self.cover(data=png(1600,900))
        result=self.service.review(self.actor,value['id'],{'action':'manual','version':1,'cover_asset_id':asset['id']})['task']
        self.assertEqual(result['status'],'enqueue_pending')
        self.assertEqual(result['current_version'],2)
        self.assertEqual(len(result['versions']),2)
        self.assertEqual(result['versions'][-1]['source'],'manual')
        self.assertTrue(result['versions'][-1]['approved'])

    def test_foreign_manual_asset_and_other_user_review_rejected(self):
        value=self.ready()
        asset=self.cover(self.other)
        self.assert_error('not_found',lambda:self.service.review(self.actor,value['id'],{'action':'manual','version':1,'cover_asset_id':asset['id']}),404)
        self.assert_error('not_found',lambda:self.service.review(self.other,value['id'],{'action':'approve','version':1}),404)

    def test_tenant_admin_manual_replacement_enqueues_creator_task(self):
        value=self.ready()
        asset=self.cover(self.admin)
        result=self.service.review(self.admin,value['id'],{'action':'manual','version':1,'cover_asset_id':asset['id']})['task']
        self.assertEqual(result['status'],'enqueue_pending')
        for _ in range(3):
            self.service.run_once()
        result=self.service.get_task(self.actor,value['id'])['task']
        self.assertEqual(result['status'],'uploading')
        self.assertEqual(len(self.store.enqueues),1)

    def test_concurrent_approval_cas_one_wins(self):
        value=self.ready()
        barrier=threading.Barrier(2)
        def approve():
            barrier.wait(timeout=5)
            try:
                self.service.review(self.actor,value['id'],{'action':'approve','version':1})
                return 'approved'
            except WorkflowError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(lambda _:approve(),range(2)))
        self.assertCountEqual(outcomes,['approved','review_conflict'])

    def test_generator_failure_is_visible_and_requires_explicit_retry(self):
        self.generate.side_effect=RuntimeError('sensitive provider trace must not be displayed')
        value=self.create()
        self.service.run_once()
        failed=self.service.get_task(self.actor,value['id'])['task']
        self.assertEqual(failed['status'],'generation_failed')
        self.assertTrue(failed['can_retry'])
        self.assertNotIn('sensitive',json.dumps(failed))
        self.service.run_once()
        self.assertEqual(self.generate.call_count,1)
        self.generate.side_effect=None
        self.service.retry(self.actor,value['id'])
        self.service.run_once()
        self.assertEqual(self.service.get_task(self.actor,value['id'])['task']['status'],'review')

    def test_wrong_generated_aspect_ratio_never_reaches_review(self):
        self.generate.return_value=png(720,1280)
        value=self.create()
        self.service.run_once()
        result=self.service.get_task(self.actor,value['id'])['task']
        self.assertEqual(result['status'],'generation_failed')
        self.assertEqual(result['versions'],[])
        self.notify.assert_not_called()

    def test_notification_explicit_failure_keeps_review_available(self):
        self.notify.side_effect=WorkflowError('notify_down','Notification unavailable',503)
        value=self.ready()
        self.service.run_once()
        result=self.service.get_task(self.actor,value['id'])['task']
        self.assertEqual(result['notification']['status'],'failed')
        self.assertTrue(result['can_review'])
        self.service.run_once()
        self.assertEqual(self.notify.call_count,1)

    def test_notification_unknown_outcome_not_automatically_resent(self):
        self.notify.side_effect=TimeoutError('internal token trace')
        value=self.ready()
        self.service.run_once()
        result=self.service.get_task(self.actor,value['id'])['task']
        self.assertEqual(result['notification']['status'],'unknown')
        self.assertNotIn('internal token',str(result))
        self.service.run_once()
        self.assertEqual(self.notify.call_count,1)

    def test_disabled_worker_no_generation_or_new_tasks(self):
        self.create()
        self.service.enabled=False
        self.assertEqual(self.service.run_once(),{'claimed':False})
        self.generate.assert_not_called()
        self.assert_error('feature_disabled',lambda:self.create(),503)

    def test_public_comment_failure_dto_and_counts(self):
        value=self.enqueue(comment_template='First comment')
        ledger=self.store.tasks[value['publish_id']]
        ledger.update(status='partial_failed',reviewed_phase='comment',video_id='video-123',video_state='published',
                      thumbnail_status='succeeded',processing_status='complete',comment_status='failed',comment_error_message='Comment failed')
        result=self.service.get_task(self.actor,value['id'])['task']
        self.assertEqual(result['status'],'comment_failed')
        self.assertTrue(result['can_retry'])
        steps={s['key']:s for s in result['steps']}
        self.assertEqual(steps['public']['status'],'complete')
        self.assertEqual(steps['comment']['status'],'error')
        listing=self.service.list_tasks(self.actor,status='published')
        self.assertEqual(listing['total'],1)
        self.assertEqual(listing['counts']['published'],1)
        self.assertEqual(listing['counts']['failed'],1)
        self.service.retry(self.actor,value['id'])
        self.assertEqual(self.store.retries,[value['publish_id']])

    def test_published_video_pending_comment_not_whole_task_complete(self):
        value=self.enqueue(comment_template='First comment')
        self.store.tasks[value['publish_id']].update(status='published',reviewed_phase='public',video_id='video-123',
            video_state='published',thumbnail_status='succeeded',processing_status='complete',comment_status='pending')
        result=self.service.get_task(self.actor,value['id'])['task']
        self.assertEqual(result['status'],'uploading')
        self.assertEqual(result['phase'],'comment')
        self.assertEqual(result['steps'][-1]['status'],'active')

    def test_empty_comment_skipped_and_thumbnail_failure_private(self):
        value=self.enqueue()
        ledger=self.store.tasks[value['publish_id']]
        ledger.update(status='failed',reviewed_phase='thumbnail',video_id='video-123',video_state='private',thumbnail_status='failed')
        result=self.service.get_task(self.actor,value['id'])['task']
        self.assertEqual(result['status'],'thumbnail_failed')
        self.assertEqual(result['steps'][1]['status'],'error')
        self.assertEqual(result['steps'][3]['status'],'pending')
        self.assertEqual(result['steps'][4]['status'],'skipped')
        ledger.update(status='published',reviewed_phase='complete',video_state='published',thumbnail_status='succeeded',processing_status='complete')
        result=self.service.get_task(self.actor,value['id'])['task']
        self.assertEqual(result['status'],'published')
        self.assertEqual(result['steps'][4]['message'],'未填写，已跳过')

    def test_unknown_upload_outcome_disables_retry(self):
        value=self.enqueue()
        self.store.tasks[value['publish_id']].update(status='unknown',reviewed_phase='upload',unknown_outcome=True)
        result=self.service.get_task(self.actor,value['id'])['task']
        self.assertTrue(result['unknown_outcome'])
        self.assertFalse(result['can_retry'])


class RuntimeAdapterTests(unittest.TestCase):
    def test_readonly_runner_uses_replica_gate_transaction_and_environment_password(self):
        app=SimpleNamespace(MYSQL_USER='test-reader',MYSQL_PASSWORD='unit-test-secret')
        process=SimpleNamespace(stdout='1\nABCD\n',returncode=0)
        with patch('features.youtube_auto_publish.runtime.subprocess.run',return_value=process) as run:
            self.assertEqual(readonly_runner(app)('SELECT 1'),[['ABCD']])
        call=run.call_args
        self.assertEqual(call.args[0][0],'mysql')
        self.assertIn('101.32.56.53',call.args[0])
        self.assertIn('START TRANSACTION READ ONLY',call.args[0][-1])
        self.assertNotIn('unit-test-secret',str(call.args[0]))
        self.assertEqual(call.kwargs['env']['MYSQL_PWD'],'unit-test-secret')

    def test_readonly_runner_rejects_writer_readback(self):
        app=SimpleNamespace(MYSQL_USER='reader',MYSQL_PASSWORD='')
        with patch('features.youtube_auto_publish.runtime.subprocess.run',return_value=SimpleNamespace(stdout='0\nABCD\n')):
            with self.assertRaises(WorkflowError) as ctx:
                readonly_runner(app)('SELECT 1')
        self.assertEqual(ctx.exception.code,'replica_required')

    def test_generator_adapter_sanitizes_env_and_requires_actual_raster_file(self):
        task={'id':'test-task','material':{'macro_name':'Test drama','macro_desc':'Synopsis'},'requirements':'Cinema','versions':[{'feedback':'Improve contrast'}]}
        with tempfile.TemporaryDirectory() as root:
            def run(command,**kwargs):
                work=Path(command[command.index('-C')+1])
                (work/'cover.png').write_bytes(png(1536,864))
                return SimpleNamespace(returncode=0)
            with patch.dict(os.environ,{'MYSQL_PASSWORD':'not-for-generator','FEISHU_SECRET':'not-for-generator'}), patch('features.youtube_auto_publish.runtime.subprocess.run',side_effect=run) as process:
                output=generate_cover_factory(root)(task,{'number':1})
            self.assertTrue(output.startswith(b'\x89PNG'))
            self.assertNotIn('MYSQL_PASSWORD',process.call_args.kwargs['env'])
            self.assertNotIn('FEISHU_SECRET',process.call_args.kwargs['env'])
            self.assertIn('16:9',process.call_args.kwargs['input'])
            self.assertIn('Improve contrast',process.call_args.kwargs['input'])

    def test_notification_adapter_has_stable_uuid_and_deep_link(self):
        app=SimpleNamespace(get_feishu_tenant_access_token=lambda:'unit-test-token',FEISHU_MESSAGE_URL='https://example.invalid/messages')
        response=Mock(status_code=200)
        response.json.return_value={'code':0,'data':{'message_id':'mock-message'}}
        session=Mock()
        session.__enter__=Mock(return_value=session)
        session.__exit__=Mock(return_value=False)
        session.post.return_value=response
        task={'id':'task-one','title':'Test task','creator':{'open_id':'open-one','user_id':'user-one'}}
        with patch('features.youtube_auto_publish.runtime.requests.Session',return_value=session):
            callback=notify_factory(app)
            callback(task,1,'https://example.invalid/youtube-publish.html?task_id=task-one&version=1')
            first=copy.deepcopy(session.post.call_args.kwargs['json'])
            callback(task,1,'https://example.invalid/youtube-publish.html?task_id=task-one&version=1')
            second=session.post.call_args.kwargs['json']
        self.assertEqual(first['uuid'],second['uuid'])
        self.assertEqual(first['receive_id'],'open-one')
        self.assertIn('task_id=task-one&version=1',first['content'])
        self.assertNotIn('unit-test-token',first['content'])
        self.assertFalse(session.trust_env)
        self.assertFalse(session.post.call_args.kwargs['allow_redirects'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
