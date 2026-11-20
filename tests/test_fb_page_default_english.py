import unittest
from types import SimpleNamespace

from features.fb_auto_posts.repositories import PageTarget, CandidateSnapshot
from features.fb_auto_posts.languages import page_language, candidate_snapshots_for_pages
from features.fb_video_publish.sources import SourceRepository


class DefaultEnglishTests(unittest.TestCase):
    def test_blank_page_selects_only_english_materials(self):
        requested = []
        def select(config):
            requested.append(config['language'])
            return CandidateSnapshot(tuple(SimpleNamespace(language=lang) for lang in ('en', 'es', '')), (), ())
        pages = [PageTarget('1', ('1',), '10', 'owner', 'UTC', '  ', 1)]
        result = candidate_snapshots_for_pages({'language': 'es'}, pages, SimpleNamespace(candidate_snapshot=select))
        self.assertEqual(requested, ['en'])
        self.assertEqual([x.language for x in result['en'].candidates], ['en'])
        self.assertEqual(page_language(''), '')  # Missing source/drama language is not English.

    def test_explicit_languages_and_authorization_are_preserved(self):
        for raw, expected in [(None, 'en'), ('', 'en'), ('es', 'es'), ('bad language', 'bad language')]:
            page = PageTarget('1', ('1',), '10', 'owner', 'UTC', raw, 0)
            self.assertEqual(page.language, expected)
            self.assertEqual(page.eligible_token_count, 0)
        self.assertEqual(page_language('bad language'), '')

    def test_manual_page_catalog_uses_same_blank_default(self):
        source = SourceRepository.__new__(SourceRepository)
        source._page_rows = lambda *_args, **_kwargs: [
            {'page_id': '10', 'language': ' ', 'eligible_token_count': 1},
            {'page_id': '11', 'language': 'es', 'eligible_token_count': 1},
            {'page_id': '12', 'language': '', 'eligible_token_count': 0},
        ]
        result = {p['page_id']: p for p in source.list_pages({'is_admin': True})}
        self.assertEqual([result[p]['language'] for p in ('10', '11', '12')], ['en', 'es', 'en'])
        self.assertEqual(result['12']['eligible_token_count'], 0)


if __name__ == '__main__':
    unittest.main()
