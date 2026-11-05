"""Offline Portuguese manual-publishing regression; no platform requests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from features.x_accounts.language import canonical_drama_language, same_drama_language
from features.x_posts.service import XPostStore, XPostError
from features.x_video_publish.core import PersonalStore
from features.x_video_publish.sources import SourceRepository
from scripts.test_x_account_language_routing import material_candidate


class PortugueseLanguageTests(unittest.TestCase):
    def test_alias_comparison_preserves_stored_tags_and_other_languages(self):
        for left in ('pt', 'PT', ' pt '):
            for right in ('pt-br', 'PT_BR', 'pt-BR'):
                self.assertTrue(same_drama_language(left, right))
                self.assertTrue(same_drama_language(right, left))
        self.assertEqual(canonical_drama_language('pt'), 'pt')
        self.assertEqual(canonical_drama_language('PT_BR'), 'pt-br')
        for right in ('es', 'en', 'pt-pt', 'pt-ao', 'invalid value'):
            self.assertFalse(same_drama_language('pt', right))
        self.assertTrue(same_drama_language('jp', 'ja'))

    def test_preview_commit_execution_and_frozen_language(self):
        actor = {'tenant_key': 'fixture', 'user_id': 'fixture'}
        for account_language, source_language in (('pt-br', 'pt'), ('pt', 'pt-br')):
            with self.subTest(account=account_language), tempfile.TemporaryDirectory() as tmp:
                store = PersonalStore(Path(tmp) / 'test.sqlite3')
                account = {'id': 1, 'owner_tenant_key': 'fixture', 'owner_user_id': 'fixture',
                    'status': 'active', 'publish_approved': True, 'publish_eligible': True,
                    'drama_language': account_language, 'username': 'fixture'}
                material = {'material_id': '101', 'language': source_language,
                    'drama_name': 'Fixture', 'drama_description': 'Fixture description'}
                request = {'source': 'manual', 'rows': [{'account_id': '1', 'material_id': '101', 'caption': '{name}'}]}
                preview = store.preview(actor, request, {'1': account}, {'101': material})
                self.assertEqual(preview['valid_count'], 1)
                batch = store.commit(actor, preview['preview_token'], 'pt-regression', {'1': account}, {'101': material})
                task = batch['tasks'][0]
                self.assertEqual(task['drama_language'], account_language)
                self.assertEqual(task['source_snapshot']['language'], source_language)
                self.assertEqual(store.assert_run_publishable(task['manual_run_id'], account)['material_id'], '101')
                changed = {**account, 'drama_language': 'es'}
                with self.assertRaises(XPostError):
                    store.assert_run_publishable(task['manual_run_id'], changed)
                candidate = material_candidate('102', source_language)
                candidate.update(account_id=1, account_username='fixture', account_drama_language=account_language,
                    page_name='Fixture', page_id='fixture')
                payload = XPostStore(Path(tmp) / 'test.sqlite3')._queue_payload(candidate)
                self.assertEqual(payload['account_drama_language'], account_language)

    def test_catalog_filter_covers_both_codes_and_retains_source_language(self):
        for language in ('pt', 'pt-br', 'PT_BR'):
            with self.subTest(language=language):
                mysql = Mock(schema='fixture')
                source = SourceRepository(mysql)
                source._read_exact_bundle = Mock(return_value=([{'material_id': '101'}], {}))
                source._resolve_rows = Mock(return_value={'items': {'101': {'material_id': '101', 'language': 'pt'}}, 'errors': {}})
                result = source.listing({'q': '101', 'language': language})
                self.assertEqual(result['items'][0]['language'], 'pt')
                where, params = source._read_exact_bundle.call_args.args
                self.assertIn('pt', params)
                self.assertIn('pt-br', params)


if __name__ == '__main__':
    unittest.main()
