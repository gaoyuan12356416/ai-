"""Local catalog availability and bounded tenant-scoped authorization checks."""
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from features.fb_video_publish.core import DomainError
from features.fb_video_publish.service import Service, PREFIX
from features.fb_video_publish.client import query_params
from tests.test_fb_shared_manual_pages import actor


class LocalPageListTest(TestCase):
    def setUp(self):
        self.actor = actor('ordinary')
        self.configs = {str(n): dict(page_id=str(n), language='en', caption_template='edited', version=7)
                        for n in range(1000, 4910)}
        self.store = SimpleNamespace(configs=lambda a: self.configs if a['tenant_key'] == 'tenant' else {})
        self.sources = SimpleNamespace(list_pages=Mock(side_effect=DomainError('source_busy', 'busy', 503)))
        self.service = Service(self.store, self.sources, 'fixture-signing-key' * 3)

    def get(self, **query):
        return self.service.handle('GET', PREFIX + '/pages', self.actor, query=query)

    def test_large_list_is_local_even_when_source_busy_and_names_missing(self):
        with patch.object(Path, 'read_text', side_effect=PermissionError):
            result = self.get()
        self.assertEqual(len(result['items']), 3910)
        self.sources.list_pages.assert_not_called()
        first = result['items'][0]
        self.assertEqual(first['page_name'], first['page_id'])
        self.assertTrue(first['authorization_unknown'])
        self.assertEqual(first['eligible_token_count'], 0)
        with patch.object(Path, 'read_text', return_value='broken json'):
            self.assertEqual(len(self.get()['items']), 3910)
        with patch.object(Path, 'read_text', return_value='{"names":{"1000":"Cached name"}}'):
            self.assertEqual(self.get()['items'][0]['page_name'], 'Cached name')
        self.assertEqual(self.service.configured_pages(actor('outsider', 'outside'))['items'], [])

    def test_verification_is_bounded_and_checks_tenant_before_source(self):
        for ids in ['', '1000,1000', 'abc', ','.join(str(n) for n in range(1000, 1009))]:
            with self.assertRaises(DomainError):
                self.get(verify_ids=ids)
        with self.assertRaises(DomainError) as err:
            self.get(verify_ids='9999')
        self.assertEqual(err.exception.status, 404)
        self.sources.list_pages.assert_not_called()
        self.sources.list_pages.side_effect = None
        self.sources.list_pages.return_value = []
        ids = list(self.configs)[:8]
        result = self.get(verify_ids=','.join(ids))
        self.sources.list_pages.assert_called_once_with(self.actor, page_ids=ids)
        self.assertEqual(len(result['items']), 8)
        self.assertEqual(len(query_params('verify_ids='+','.join(['9'*31]*8))['verify_ids']), 255)

    def test_missing_or_invalid_authorization_stays_visible_and_blocked(self):
        self.sources.list_pages.side_effect = None
        self.sources.list_pages.return_value = [dict(page_id='1000', page_name='Live name',
                    eligible_token_count=0, blocked_reason='expired')]
        items = self.get(verify_ids='1000,1001')['items']
        self.assertEqual([p['page_id'] for p in items], ['1000', '1001'])
        for p in items:
            self.assertTrue(p['blocked_reason'])
            self.assertEqual(p['eligible_token_count'], 0)
            self.assertFalse(p['authorization_unknown'])
            self.assertNotIn('version', p)
            self.assertNotIn('caption_template', p)
        self.sources.list_pages.return_value[0].update(eligible_token_count=1, blocked_reason='')
        self.assertEqual(self.get(verify_ids='1000')['items'][0]['eligible_token_count'], 1)

    def test_busy_is_local_verification_failure_and_resolve_still_checks_live(self):
        with self.assertRaises(DomainError) as err:
            self.get(verify_ids='1000')
        self.assertEqual(err.exception.code, 'source_busy')
        self.sources.get_pages = Mock(side_effect=DomainError('page_unavailable', 'expired', 409))
        with self.assertRaises(DomainError) as err:
            self.service.resolve(self.actor, {'page_ids': ['1000']})
        self.assertEqual(err.exception.code, 'page_unavailable')
        self.sources.get_pages.assert_called_once_with(self.actor, ['1000'], manual=True)
