import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.moboreels_library import synthesis
from features.moboreels_library.content_tools import ContentTools
from features.moboreels_library.promotions import PromotionError


class SynthesisTests(unittest.TestCase):
    def setUp(self):
        self.drama = {"story_id": "123", "story_name": "Example", "lang_id": 3,
                      "img_src": "https://example.com/cover.jpg"}
        self.rows = [{"epis_no": 2, "epis_url": "https://example.com/2.mp4?sign=a"},
                     {"epis_no": 1, "epis_url": "https://example.com/1.mp4?sign=a"}]

    def fetch(self, rows=None, drama=True):
        catalog = SimpleNamespace(detail=lambda story: self.drama if drama else None)
        provider = SimpleNamespace(_post=lambda path, body: SimpleNamespace(
            status_code=200, json=lambda: {"code": 200, "data": self.rows if rows is None else rows}))
        with patch('features.moboreels_library.settings.Settings.from_env', return_value=SimpleNamespace()), \
             patch('features.moboreels_library.db.Database'), \
             patch('features.moboreels_library.catalog.Catalog', return_value=catalog), \
             patch('features.moboreels_library.provider.ProviderClient', return_value=provider):
            return synthesis.fetch_episodes('123')

    def test_product_preserves_existing_and_deduplicates(self):
        original = [{"app_id": "1479"}, {"app_id": "979"}]
        items = synthesis.include_product(original)
        self.assertEqual([x['app_id'] for x in items], ['1479', '979', '3360'])
        self.assertEqual(len(original), 2)
        self.assertEqual(synthesis.include_product(items), items)

    def test_library_metadata_episode_order_and_signed_urls(self):
        rows = self.fetch()
        self.assertEqual([r['episode_number'] for r in rows], [1, 2])
        self.assertEqual(rows[0]['episode_url'], self.rows[1]['epis_url'])
        self.assertEqual((rows[0]['app_id'], rows[0]['language'], rows[0]['drama_name']),
                         ('3360', 'en', 'Example'))
        self.assertEqual(rows[0]['all_episodes_count'], 2)
        self.assertEqual(rows[0]['cover_url'], self.drama['img_src'])

    def test_execution_reads_fresh_urls(self):
        first = self.fetch()
        self.rows[1]['epis_url'] = 'https://example.com/1.mp4?sign=new'
        second = self.fetch()
        self.assertNotEqual(first[0]['episode_url'], second[0]['episode_url'])

    def test_empty_episodes(self):
        with self.assertRaisesRegex(ValueError, '暂无可用'):
            self.fetch(rows=[])

    def test_missing_story_does_not_fall_back_to_other_catalog(self):
        with self.assertRaisesRegex(ValueError, '未找到'):
            self.fetch(drama=False)

    def test_invalid_provider_episode_is_rejected(self):
        for row in ({'epis_no': 1, 'epis_url': 'http://example.com/a.mp4'},
                    {'epis_no': 0, 'epis_url': 'https://example.com/a.mp4'}):
            with self.assertRaises(ValueError):
                self.fetch(rows=[row])

    def test_missing_language(self):
        self.drama['lang_id'] = None
        with self.assertRaisesRegex(ValueError, '语言'):
            self.fetch()

    def test_app_routes_only_moboreels_to_library(self):
        source = (Path(__file__).parents[1] / 'app.py').read_text(encoding='utf8')
        tree = ast.parse(source)
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == 'fetch_drama_episodes')
        calls = []
        namespace = {'pick_drama_variant': lambda *args, **kwargs: calls.append(args)}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'app.py', 'exec'), namespace)
        with patch.object(synthesis, 'fetch_episodes', return_value=['mobo']) as fetch:
            self.assertEqual(namespace['fetch_drama_episodes']('123', '3360'), ['mobo'])
            fetch.assert_called_once_with('123')
            for product in ('1479', '979'):
                with self.assertRaises(ValueError):
                    namespace['fetch_drama_episodes']('old', product)
            self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
