from pathlib import Path
from unittest.mock import patch

from tests.test_fb_page_local_list import LocalPageListTest
from tests.test_fb_shared_manual_pages import actor
from features.fb_video_publish.core import DomainError


class PagePaginationTest(LocalPageListTest):
    def test_server_pages_are_disjoint_and_tenant_local(self):
        with patch.object(Path, 'read_text', return_value='{"names":{}}'):
            first = self.get(page_index='1', page_size='20')
            second = self.get(page_index='2', page_size='20')
            self.assertEqual(first['total'], 3910)
            self.assertEqual(len(first['items']), 20)
            self.assertFalse({r['page_id'] for r in first['items']} & {r['page_id'] for r in second['items']})
            outside = self.service.configured_pages(actor('outsider', 'outside'), pagination={'page_index': '1'})
            self.assertEqual(outside['items'], [])
            self.assertEqual(outside['creators'], [])
        self.sources.list_pages.assert_not_called()

    def test_search_creator_filter_and_last_page_use_full_catalog(self):
        self.configs['4909'].update(created_by='last', created_by_name='Last creator')
        self.configs['1000'].update(created_by='first', created_by_name='First creator')
        with patch.object(Path, 'read_text', return_value='{"names":{"4909":"Far away page"}}'):
            result = self.get(page_index='1', q='FAR AWAY', created_by='last')
            self.assertEqual([r['page_id'] for r in result['items']], ['4909'])
            self.assertEqual(result['total'], 1)
            self.assertEqual({c['value'] for c in result['creators']}, {'first', 'last'})
            self.assertEqual(self.get(page_index='999')['page_index'], 196)
            self.assertEqual(self.get(page_index='1', q='no match')['total'], 0)

    def test_bad_pagination_fails_without_remote_query(self):
        for query in ({'page_index': '0'}, {'page_size': '101'}, {'page_index': 'NaN'}, {'page_index': '1', 'q': 'x'*201}):
            with self.assertRaises(DomainError):
                self.get(**query)
        self.sources.list_pages.assert_not_called()

    def test_create_picker_marks_configured_pages_outside_current_page(self):
        self.sources.list_pages.side_effect = None
        self.sources.list_pages.return_value = [{'page_id': '4909'}, {'page_id': '9999'}]
        from features.fb_video_publish.service import PREFIX
        result = self.service.handle('GET', PREFIX + '/pages/available', self.actor)
        self.assertEqual([r['configured'] for r in result['items']], [True, False])
