"""Page pool scope, with strict catalog rules retained for other publishers."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import unittest
from types import SimpleNamespace
from features.fb_video_publish.sources import SourceRepository


class Catalog:
    schema = 'kunlunads_dev'
    blacklist_schema = 'ads_setting'

    def __init__(self, product='HotDrama', duration='120', released=True, metadata=True):
        self.product, self.duration = product, duration
        self.released, self.metadata = released, metadata

    def select(self, sql, params):
        if 's.data_source,s.product' in sql:
            return [dict(material_id='101', product=self.product, data_source='6', type='2',
                         is_delete='0', content_id='501', language='en', video_duration=self.duration,
                         drama_released='1' if self.released else '0', content_blocked='0', series_blocked='0')]
        if 'ads_custom_source s' in sql:
            # The real catalog query remains responsible for eligibility filtering.
            assert 's.type=2' in sql
            if ('s.product=%s' in sql and self.product != 'Dramawave') or (self.duration == '0' and 's.video_duration' in sql.split('WHERE', 1)[-1]):
                return []
            if not self.released and 'd.release_status=1' in sql:
                return []
            return [dict(material_id='101', content_id='501', language='en',
                         video_duration=self.duration, material_name='Video',
                         media_url='https://socialkit-cdn.yingliang.tech/video.mp4')]
        if 'ads_drama_info d' in sql:
            if not self.metadata or (not self.released and 'd.release_status=1' in sql):
                return []
            return [dict(content_id='501', language='en', drama_name='Drama')]
        if 'ads_drama_resource r' in sql:
            return []
        raise AssertionError('unexpected query')


class ProductScopeTests(unittest.TestCase):
    def repo(self, **kwargs):
        return SourceRepository(None, SimpleNamespace(mysql=Catalog(**kwargs)))

    def test_other_products_pass_admission_and_execution_read(self):
        for product in ['HotDrama', 'OtherProduct', '']:
            repo = self.repo(product=product)
            result = repo.resolve_materials(['101'], enforce_blacklist=False, page_pool=True)
            self.assertEqual(list(result['items']), ['101'])
            self.assertEqual(result['errors'], {})
            fresh = repo.get_materials(['101'], enforce_blacklist=False, page_pool=True)
            self.assertEqual(fresh['101'], result['items']['101'])

    def test_catalog_duration_no_longer_blocks_page_pool(self):
        result = self.repo(duration='0').resolve_materials(['101'], enforce_blacklist=False, page_pool=True)
        self.assertEqual(list(result['items']), ['101'])
        self.assertEqual(result['errors'], {})

    def test_page_pool_allows_unreleased_or_missing_dramawave_mapping(self):
        for metadata in [True, False]:
            repo = self.repo(released=False, metadata=metadata)
            result = repo.resolve_materials(['101'], enforce_blacklist=False, page_pool=True)
            self.assertEqual(result['errors'], {})
            fresh = repo.get_materials(['101'], enforce_blacklist=False, page_pool=True)
            self.assertEqual(fresh, result['items'])
            self.assertEqual(fresh['101']['drama_name'], 'Drama' if metadata else '')

    def test_other_callers_still_require_released_drama(self):
        result = self.repo(product='Dramawave', released=False).resolve_materials(['101'], enforce_blacklist=False)
        self.assertEqual(result['errors']['101']['code'], 'material_not_released')

    def test_default_policy_and_dramawave_remain_compatible(self):
        result = self.repo().resolve_materials(['101'], enforce_blacklist=False)
        self.assertEqual(result['errors']['101']['code'], 'material_product_mismatch')
        self.assertEqual(list(self.repo(product='Dramawave').get_materials(['101'])), ['101'])


if __name__ == '__main__':
    unittest.main()
