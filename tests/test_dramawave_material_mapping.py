"""Exercise the generated catalog expression with distinct per-product records."""
import sqlite3
import unittest
from unittest.mock import patch

from features.publishing_products import source_app_sql
from features.x_video_publish.sources import SourceRepository


class DramaWaveMaterialMappingTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.create_function('LOCATE', 2, lambda needle, value: value.find(needle) + 1)
        self.db.create_collation('utf8mb4_unicode_ci',
                                 lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold()))
        self.db.execute("ATTACH DATABASE ':memory:' AS catalog")
        self.db.execute('CREATE TABLE catalog.ads_apps_setting (id INTEGER, name TEXT)')
        self.db.execute('CREATE TABLE catalog.ads_custom_source (id INTEGER, product TEXT)')

    def resolve_app(self, product, catalog_id):
        self.db.execute('DELETE FROM catalog.ads_apps_setting')
        self.db.execute('DELETE FROM catalog.ads_custom_source')
        self.db.execute('INSERT INTO catalog.ads_apps_setting VALUES (?,?)', (catalog_id, product))
        self.db.execute('INSERT INTO catalog.ads_custom_source VALUES (1,?)', (product,))
        sql = 'SELECT ' + source_app_sql('s', 'catalog') + ' FROM catalog.ads_custom_source s'
        return self.db.execute(sql).fetchone()[0]

    def test_dramawave_variants_ignore_material_product_id(self):
        for product in ('Dramawave', ' Drama-comics ', 'Drama-B', 'Drama-C',
                        'Drama-novel', 'Drama-AI素材', 'Drama-社媒专用素材',
                        'DramawaveSource', 'DRAMAWAVE H5', 'dramawaveminis',
                        '[w2a]DramaWave iOS', 'Dramawave-copy-test'):
            with self.subTest(product=product):
                self.assertEqual(self.resolve_app(product, 9999), 1479)

    def test_mobo_and_other_products_keep_their_catalog(self):
        for product, app_id in (('moboreels', 3360), ('MoboShort', 2373),
                                ('hotdrama', 3302), ('DramaBite', 2428),
                                ('Free-comics', 3210)):
            with self.subTest(product=product):
                self.assertEqual(self.resolve_app(product, app_id), app_id)

    def test_comics_hydrates_1479_and_mobo_uses_ai_library(self):
        from types import SimpleNamespace
        repo = SourceRepository(SimpleNamespace(schema='catalog'))
        rows = []
        for mid, product, app in ((1, 'Drama-comics', 3093), (2, 'moboreels', 3360)):
            rows.append(dict(material_id=str(mid), product=product,
                source_app_id=self.resolve_app(product, app), material_type=2,
                material_is_deleted=0, material_language='en', content_id='shared-id',
                material_url='https://cdn.test/video.mp4', material_name='Video',
                video_duration=60, source_tag_name='Drama'))
        dramas = [dict(app_id=app, content_id='shared-id', series_code='series',
                       language='en', drama_name='Title-'+str(app),
                       drama_description='Description', drama_labels='Romance')
                  for app in (1479, 3093, 3360)]
        deploy = [dict(app_id=app, content_id='shared-id', app='app', language='en', deploy_time=0)
                  for app in (1479, 3093, 3360)]
        with patch('features.moboreels_library.macro_client.lookup_story', return_value={
                'story_id':'shared-id', 'is_show':1, 'lang_id':3,
                'story_name':'AI Library Title', 'introduce':'AI Library Description'}) as lookup:
            result = repo._resolve_rows(['1', '2'], rows, mappings=(dramas, deploy))
            lookup.assert_called_once_with('shared-id')
        self.assertEqual(result['errors'], {})
        self.assertEqual(result['items']['1']['drama_name'], 'Title-1479')
        self.assertEqual(result['items']['2']['drama_name'], 'AI Library Title')

    def test_mobo_does_not_fall_back_when_library_is_unavailable(self):
        from types import SimpleNamespace
        from features.moboreels_library.macro_client import MoboreelsEligibilityError
        from tests.test_post_product_scope import material, drama, deploy
        repo = SourceRepository(SimpleNamespace(schema='catalog'))
        for app in (3360, 2373):
            with self.subTest(app=app), patch('features.moboreels_library.macro_client.lookup_story',
                    side_effect=MoboreelsEligibilityError('not listed')):
                result = repo._resolve_rows(['1'], [material(1, app)],
                    mappings=([drama(app), drama(1479)], [deploy(app), deploy(1479)]))
                self.assertEqual(result['items'], {})
                self.assertEqual(result['errors']['1']['code'], 'moboreels_story_invalid')


if __name__ == '__main__':
    unittest.main()
