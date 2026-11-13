import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from features.youtube_auto_publish.service import YouTubeWorkflow
from features.youtube_auto_publish.source import MaterialSource
from features.youtube_auto_publish.templates import render, WorkflowError, long_url, has_link_source


class YouTubeMoboreelsTest(unittest.TestCase):
    def test_exact_double_brace_only_and_preview_does_not_resolve(self):
        self.assertEqual(render('Watch {{m_url}}',{},'description',allow_unresolved=True),'Watch {{m_url}}')
        self.assertEqual(render('Watch {{m_url}}',{'macro_m_url':'https://example.test/m/1'},'description'),'Watch https://example.test/m/1')
        for invalid in ['{m_url}','{{{m_url}}}','{{url}}']:
            with self.assertRaises(WorkflowError): render(invalid,{},'description',allow_unresolved=True)
        self.assertEqual(render('{name}',{'macro_name':'旧剧名'},'title'),'旧剧名')

    def test_material_story_id_bypasses_old_drama_resolver(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'source.sql';path.write_text('SELECT * FROM approved_material_pool')
            row={'id':'501','content_id':'194369322','app_id':'1479','name':'素材名','source_job_id':'legacy-job',
                 'url':'https://ai.yingliangads.com/material.mp4'}
            legacy=Mock(side_effect=AssertionError('legacy resolver must not run'))
            source=MaterialSource(path,lambda sql:[(json.dumps(row).encode().hex(),)],drama_resolver=legacy)
            with patch('features.moboreels_library.macro_client.lookup_story',return_value={
                    'story_id':'194369322','story_name':'Moboreels 剧名','introduce':'简介',
                    'img_src':'https://video-cdn3.cdreader.com/cover.webp'}) as lookup:
                material=source.get('501',moboreels=True)
            lookup.assert_called_once_with('194369322')
            legacy.assert_not_called()
            self.assertEqual(material['macro_name'],'Moboreels 剧名')
            self.assertEqual(material['drama_cover_status'],'available')
            self.assertEqual(material['promotion_provider'],'moboreels')
            self.assertEqual(material['long_url'], '')
            self.assertFalse(material['link_ready'])

    def test_explicit_moboreels_source_cannot_reuse_legacy_job_or_attribution(self):
        material = {'promotion_provider':'moboreels','content_id':'194369322',
                    'source_job_id':'legacy-job','link_job_id':'legacy-link','attribution':{'old':True},
                    'long_url':'https://old.example.test/dramawave', 'macro_url':'https://old.example.test/link'}
        self.assertEqual(long_url(material), '')
        self.assertFalse(has_link_source(material))
        with self.assertRaises(WorkflowError):
            render('{url}', material, 'description')
        material.update(source_job_id='', drama_status='matched')
        self.assertFalse(has_link_source(material))

    def test_task_freezes_creator_link_and_duplicate_request_reuses(self):
        with tempfile.TemporaryDirectory() as root:
            source=Mock();source.get.return_value={'id':'501','content_id':'194369322','name':'素材',
                'macro_name':'Mobo 剧名','macro_desc':'简介','promotion_provider':'moboreels'}
            channel={'id':'1','channel_id':'UC'+'a'*22,'eligible':True,'comment_eligible':True}
            flow=YouTubeWorkflow(Path(root)/'db.sqlite',Path(root)/'assets',source,
                lambda actor:[channel],Mock(side_effect=AssertionError('old short link must not run')),object())
            flow._asset=Mock(return_value={'id':'asset-1'})
            flow._dto=lambda body, actor:body
            actor={'tenant_key':'tenant','user_id':'creator','name':'创建者'}
            payload={'operation_id':'stable-operation-1234','material_id':'501','channel_id':'1',
                'title_template':'{name}','description_template':'Watch {{m_url}}',
                'comment_template':'{{m_url}}','cover_source':'local','requirements':'','cover_asset_id':'asset-1'}
            link={'status':'succeeded','short_url':'https://ai.yingliangads.com/m/testcode12345678',
                  'short_link_id':'short-id','promotion_key':'promotion-id','koc_code':'CODE'}
            with patch('features.youtube_auto_publish.service.resolve_m_url',return_value=link) as resolver:
                first=flow.create_task(actor,payload)['task']
                second=flow.create_task(actor,payload)['task']
            self.assertEqual(first,second)
            resolver.assert_called_once()
            self.assertEqual(resolver.call_args.args[0],actor)
            self.assertEqual(resolver.call_args.kwargs['story_id'],'194369322')
            self.assertEqual(resolver.call_args.kwargs['task_id'],first['id'])
            self.assertEqual(first['comment'],link['short_url'])
            self.assertEqual(first['creator'],actor)
            self.assertEqual(first['material']['moboreels_promotion'],link)
            source.get.assert_called_once_with('501',moboreels=True)


if __name__=='__main__': unittest.main()
