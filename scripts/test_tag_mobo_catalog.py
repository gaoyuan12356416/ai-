"""Exercise the catalog boundary, strict hydration and frozen native URL macro."""
import os
from pathlib import Path
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.test_x_auto_post_selector import FakeSource, FakeMetricStore, FakeValidator, drama, material, selector, request, rules
from features.x_tag_publish.material_catalog import CatalogSource, for_request
from features.x_auto_posts.selector import NoEligibleMaterial


class MoboTests(unittest.TestCase):
    def selector(self, source, metrics):
        class Validator(FakeValidator):
            def validate(self, material_id):
                return dict(super().validate(material_id),content_id='147905322' if material_id=='20' else 'D1')
        return selector(source,metrics,validator=Validator())

    def source(self):
        class Source(FakeSource):
            schema='fixture'
            material_data_source=6
            def _stream_select(self, sql, params):
                self.query=(sql,params)
                return [dict(story_id='147905322',story_name='Mobo story')]
            def list_material_rows(self, *, content_id, language, products=('Dramawave',)):
                return self.materials.get(content_id,[])
        return Source([drama('D1')],{'147905322':[material('20','147905322',product='MoboShort')],
                                     'D1':[material('10','D1')]})

    @patch.dict(os.environ,{'MOBOREELS_INSTITUTION_USER_ID':'test-institution'})
    def test_mobo_only_does_not_read_dramawave_catalog_or_metrics(self):
        source=self.source();metrics=FakeMetricStore(ready_dates=[])
        original=self.selector(source,metrics);req=request(rules(material_products=['Moboreels','Moboshort'],drama={'resource_types':[]}))
        with patch('features.moboreels_library.settings.read_env_file',return_value={}):
            adapted=for_request(original,req);result=adapted.preview(req)
        self.assertIs(original.source,source)
        self.assertIsInstance(adapted.source,CatalogSource)
        self.assertIsNone(source.drama_call)
        self.assertEqual(metrics.loads,[])
        self.assertEqual(result.drama.catalog,'moboreels')
        self.assertEqual(result.drama.deploy_time,0)
        self.assertEqual(result.material.source.material_id,'20')
        sql,params=source.query
        self.assertIn('s.product IN (%s,%s)',sql)
        self.assertEqual(sql.count('%s'),len(params))

    @patch.dict(os.environ,{'MOBOREELS_INSTITUTION_USER_ID':'test-institution'})
    def test_mixed_products_preserve_dramawave_and_explicit_filters(self):
        source=self.source();original=self.selector(source,FakeMetricStore())
        req=request(rules(material_products=['Dramawave','Moboreels'],drama={'resource_types':[]}))
        with patch('features.moboreels_library.settings.read_env_file',return_value={}):
            result=for_request(original,req).preview(req)
        self.assertIsNotNone(source.drama_call)
        self.assertEqual(result.material.source.material_id,'10')
        mobo_request=request(rules(material_products=['Moboshort'],drama={'launch_window_days':7}))
        with self.assertRaises(NoEligibleMaterial):
            for_request(original,mobo_request).preview(mobo_request)

    def test_strict_mobo_hydration_never_falls_back_to_1479(self):
        from features.x_posts.selector import DramawaveCandidateSelector
        item=dict(material_id='8394460',product='moboreels',material_type=2,material_is_deleted=0,
                  material_language='en',content_id='147905322',material_url='https://example.test/video.mp4',
                  material_name='fixture',video_duration=282)
        strict=DramawaveCandidateSelector.__new__(DramawaveCandidateSelector)
        strict._pool_material_rows=lambda *a,**kw:[item]
        strict._pool_drama_rows=lambda *a: self.fail('Mobo ID reached DramaWave catalog')
        metadata=dict(drama_name='Mobo story',description='Mobo synopsis',language='en',labels=['Moboreels'])
        with patch('features.x_posts.moboreels.story_metadata',return_value=metadata):
            result=strict._pool_candidate('8394460','2026-11-17')
        self.assertEqual(result['source_provider'],'moboreels')
        self.assertEqual(result['content_id'],'147905322')
        self.assertEqual(result['drama_deploy_time'],'')
        from features.moboreels_library.macro_client import MoboreelsLinkError
        with patch('features.x_posts.moboreels.story_metadata',side_effect=MoboreelsLinkError('offline')):
            from features.x_posts.selector import PoolCandidateRejection
            with self.assertRaises(PoolCandidateRejection):strict._pool_candidate('8394460','2026-11-17')

    def test_native_mobo_url_render_and_mixed_macro_rejection(self):
        from features.x_posts.service import build_post_text, XPostError
        result=build_post_text('https://ai.yingliangads.com/x/1.html','Story','Synopsis',
                              '{{drama_name}} {{desc}} {{m_url}}',m_url='https://ai.yingliangads.com/m/test')
        self.assertIn('/m/test',result)
        self.assertNotIn('/x/1.html',result)
        self.assertNotIn('{{',result)
        with self.assertRaises(XPostError):
            build_post_text('https://example.test/1.html','Story','Synopsis','{{drama_name}} {{desc}} {{url}} {{m_url}}')

if __name__=='__main__':unittest.main()
