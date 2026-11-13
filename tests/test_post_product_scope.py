import importlib.util
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
import features.fb_auto_posts
features.fb_auto_posts.__path__.append(str(ROOT/'ops/moboreels-attribution-20261113/runtime/fb/features/fb_auto_posts'))
import features.x_posts
features.x_posts.__path__.append(str(ROOT/'ops/moboreels-attribution-20261113/runtime/x/features/x_posts'))

from features.publishing_products import current_app_id, source_app_scope, source_app_sql
from features.x_video_publish.sources import SourceRepository
from features.fb_video_publish.sources import SourceRepository as FBSource
from features.fb_video_publish.group_sources import GroupPoolSource
from features.fb_video_publish.query_broker import _validate_sources
from features.youtube_auto_publish.source import MaterialSource, COLUMNS

ROOT = Path(__file__).resolve().parents[1]


def material(mid, app):
    return dict(material_id=str(mid), product={1479:'Dramawave',3360:'moboreels',2373:'MoboShort'}[app],
        source_app_id=app, material_type=2, material_is_deleted=0, is_delete=0,
        material_language='en', content_id='shared-id', material_url='https://cdn.test/video.mp4',
        material_name='Video', video_duration=60, source_tag_name='Drama')


def drama(app):
    return dict(app_id=app,content_id='shared-id',series_code='series',language='en',
        drama_name='Title-'+str(app),drama_description='Description-'+str(app),drama_labels='Romance')


def deploy(app):
    return dict(app_id=app,content_id='shared-id',app='app',language='en',deploy_time=0,release_status=1)


class ProductScopeTests(unittest.TestCase):
    def test_same_content_id_resolves_with_its_actual_product(self):
        repo=SourceRepository(SimpleNamespace(schema='kunlunads_dev'))
        apps=[1479,3360,2373]
        rows=[material(i+1,app) for i,app in enumerate(apps)]
        result=repo._resolve_rows(['1','2','3'],rows,mappings=([drama(a) for a in apps],[deploy(a) for a in apps]))
        self.assertEqual(result['errors'],{})
        self.assertEqual([result['items'][str(i+1)]['drama_name'] for i in range(3)],['Title-'+str(a) for a in apps])
        self.assertEqual(current_app_id(),1479)

    def test_missing_own_mapping_does_not_borrow_another_product(self):
        repo=SourceRepository(SimpleNamespace(schema='kunlunads_dev'))
        result=repo._resolve_rows(['1'],[material(1,3360)],mappings=([drama(1479)],[deploy(1479)]))
        self.assertEqual(result['items'],{})
        self.assertEqual(result['errors']['1']['code'],'drama_mapping_missing')

    def test_context_resets_after_failure(self):
        with self.assertRaises(RuntimeError):
            with source_app_scope(3360):
                self.assertEqual(current_app_id(),3360)
                with source_app_scope(2373):self.assertEqual(current_app_id(),2373)
                self.assertEqual(current_app_id(),3360)
                raise RuntimeError()
        self.assertEqual(current_app_id(),1479)

    def test_fb_manual_and_pool_metadata_do_not_mix_products(self):
        class SQL:
            schema='kunlunads_dev'; blacklist_schema='ads_setting'
            def select(self,sql,params):
                _validate_sources(sql)
                self.assert_sql=sql
                return [drama(a) for a in (1479,3360,2373)]
        repo=FBSource(None,SimpleNamespace(mysql=SQL()))
        values=[dict(content_id='shared-id',language='en',source_app_id=a) for a in (1479,3360,2373)]
        result=repo._enrich(values,strict=True,need_desc=True,manual=True)
        self.assertEqual([r['drama_name'] for r in result],['Title-1479','Title-3360','Title-2373'])
        condition,params=repo._conditions(enforce_blacklist=False,manual=True)
        self.assertNotIn('s.product=%s',condition)

    def test_fb_group_accepts_external_release_without_dw_deploy_timestamp(self):
        source=SimpleNamespace(mysql=SimpleNamespace(schema='kunlunads_dev',blacklist_schema='ads_setting'))
        repo=GroupPoolSource(source)
        result=repo._enrich(dict(material_id='1',content_id='shared-id',source_app_id=3360),
            [drama(1479),drama(3360)],[deploy(1479),deploy(3360)],need_desc=True)
        self.assertEqual(result['drama_name'],'Title-3360')

    def test_source_identity_sql_passes_existing_fb_broker(self):
        sql='SELECT '+source_app_sql('s','kunlunads_dev')+' source_app_id FROM `kunlunads_dev`.ads_custom_source s WHERE s.id=%s'
        _validate_sources(sql)
        with self.assertRaises(ValueError):source_app_sql('s;DROP','kunlunads_dev')

    def test_youtube_accepts_external_app_metadata_and_retains_host_checks(self):
        statements=[]
        row={k:'' for k in COLUMNS}
        row.update(id='12',name='Video',url='https://ai.yingliangads.com/video.mp4',app_id='3360',content_id='shared-id',language='en')
        def query(sql):
            statements.append(sql)
            return [(json.dumps(row).encode().hex(),)]
        source=MaterialSource('',query)
        result=source._read('SELECT * FROM configured_pool',material_id='12')
        self.assertEqual(result[0]['app_id'],'3360')
        self.assertNotIn("pool.app_id AS CHAR)='1479'",statements[0])
        row['url']='https://untrusted.test/video.mp4'
        self.assertEqual(source._read('SELECT * FROM configured_pool',material_id='12'),[])

    def test_youtube_indexed_uploaders_and_legacy_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source.sql'
            path.write_text('SELECT * FROM configured_pool',encoding='utf-8')
            queries=[]
            def query(sql):
                queries.append(sql)
                return [(json.dumps({'id':'7','name':'Uploader'}).encode().hex(),)]
            source=MaterialSource(path,query)
            self.assertEqual(source._read_uploaders(path.read_text()),[{'id':'7','name':'Uploader'}])
            self.assertIn('GROUP BY pool.uploader_id',queries[-1])
            indexed='SELECT HEX(JSON_OBJECT(\'id\',\'7\',\'name\',\'Uploader\'))'
            path.with_suffix('.uploaders.sql').write_text(indexed,encoding='utf-8')
            self.assertEqual(source._read_uploaders(path.read_text()),[{'id':'7','name':'Uploader'}])
            self.assertEqual(queries[-1],indexed)

    def test_youtube_uploader_sql_cannot_execute_writes(self):
        from features.youtube_auto_publish.templates import WorkflowError
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source.sql'
            path.with_suffix('.uploaders.sql').write_text('DELETE FROM users',encoding='utf-8')
            queries=[]
            source=MaterialSource(path,lambda sql:queries.append(sql) or [])
            with self.assertRaises(WorkflowError):source._read_uploaders('SELECT * FROM configured_pool')
            self.assertEqual(queries,[])

    def test_auto_material_query_only_removes_product_for_explicit_moboreels(self):
        from features.x_posts.moboreels import source_policy
        from features.x_auto_posts.repositories import ReadOnlyMySQLRepository
        repo=ReadOnlyMySQLRepository(lambda: None)
        calls=[]
        repo._stream_select=lambda sql,params: calls.append((sql,params)) or []
        with source_policy(True):repo.list_material_rows(content_id='story',language='en')
        self.assertNotIn('s.product=%s',calls[-1][0])
        self.assertEqual(calls[-1][0].count('%s'),len(calls[-1][1]))
        repo.list_material_rows(content_id='story',language='en')
        self.assertIn('s.product=%s',calls[-1][0])
        self.assertEqual(calls[-1][0].count('%s'),len(calls[-1][1]))

    def test_every_deployed_selector_keeps_product_scope_and_resets_it(self):
        for lane in ('main','x','tt','tt_auto'):
            path=ROOT/'ops/post-product-scope-20261113/runtime'/lane/'features/x_posts/selector.py'
            spec=importlib.util.spec_from_file_location('features.x_posts.product_test_'+lane,path)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            class Selector(module.DramawaveCandidateSelector):
                def _pool_material_rows(self,*args,**kwargs):return [material(1,3360)]
                def _pool_drama_rows(self,*args,**kwargs):
                    if current_app_id()!=3360:raise AssertionError('wrong app')
                    return [drama(3360)]
                def _drama_deploy_rows(self,*args,**kwargs):return [deploy(3360)]
                def _violation_counts(self,*args):return dict(facebook_count=0,tiktok_count=0,twitter_count=0,resource_audit_count=0)
                def _material_tags(self,*args):return []
            with self.subTest(lane=lane):
                result=Selector(None)._pool_candidate('1','2026-11-12')
                self.assertEqual(result['drama_name'],'Title-3360')
                self.assertEqual(current_app_id(),1479)


if __name__=='__main__':unittest.main()
