import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from unittest.mock import patch
from meta_violation_cleanup.cover_source import cover_url, select_cover, scan
from meta_violation_cleanup.cover_store import CoverStore


class CoverSourceTests(unittest.TestCase):
    def setUp(self):
        self.materials = [{'data_source_id': 'Drama01', 'language': 'EN', 'data_source':6,'product':'DramaWave'}]

    def row(self, **extra):
        return dict({'app_id': '1479', 'content_id': 'Drama01', 'language': 'en',
                     'cover': 'https://example.com/cover.jpg?a=1'}, **extra)

    def test_exact_product_content_language(self):
        rows = [self.row(app_id='3302'), self.row(content_id='drama01'), self.row(language='fr')]
        self.assertEqual(select_cover(self.materials, '1479', rows)['error'], 'cover_missing')
        self.assertEqual(select_cover(self.materials, '1479', rows + [self.row()])['cover_url'],
                         'https://example.com/cover.jpg?a=1')

    def test_repeated_episode_rows_and_empty_covers(self):
        self.assertIn('cover_url', select_cover(self.materials, '1479', [self.row(), self.row(), self.row(cover='')]))

    def test_conflicting_and_invalid_covers(self):
        self.assertEqual(select_cover(self.materials, '1479', [self.row(), self.row(cover='https://example.com/other.jpg')])['error'], 'cover_ambiguous')
        self.assertEqual(select_cover(self.materials, '1479', [self.row(cover='file:///tmp/a')])['error'], 'cover_url_invalid')

    def test_multiple_drama_creative_is_not_silently_flattened(self):
        self.assertEqual(select_cover(self.materials + [dict(self.materials[0],data_source_id='Other')], '1479', [self.row()])['error'], 'cover_drama_identity_ambiguous')

    def test_cross_product_and_non_drama_materials_rejected(self):
        for extra in ({'product':'HotDrama'},{'data_source':1}):
            self.assertEqual(select_cover([dict(self.materials[0],**extra)], '1479', [self.row()])['error'],
                             'cover_material_product_or_type_mismatch')

    def test_url_keeps_attribution_and_rejects_credentials(self):
        self.assertEqual(cover_url('https://static.mydramawave.com/a.png?q=2'), 'https://static-v1.mydramawave.com/a.png?q=2')
        self.assertEqual(cover_url('https://user:secret@example.com/a.png'), '')


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = CoverStore(Path(self.folder.name)/'cover.sqlite3')
        self.rows = {str(a):[{'id':str(a), 'ad_id':str(a), 'creative_id':str(a+1000),
                            'ad_account_id':'77','product':'1479','source_id':'0','original_source_id':'501'}]
                     for a in range(100,104)}
        self.violations = [{'ad_id':str(a),'issues_info':{'global':{'Spam':'x'}},
                            'created_at':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()}
                           for a in range(100,103)]
        self.deleted = set()
        outer = self
        class Source:
            def products(self): return []
            def confirmed_deleted_ads(self): return outer.deleted
            def violations(self, start, end): return outer.violations
            def snapshot(self): return nullcontext()
            def lineages(self, ids): return {a:outer.rows[a] for a in ids if a in outer.rows}
            def sources(self, ids): return {}
            def selected_credentials(self, rows): return {r['id']:{'user_id':'803'} for r in rows}
        self.source = Source()

    def scan(self, bid):
        def lookup(source, mids, *args):
            return ([r for rows in self.rows.values() for r in rows if r['original_source_id'] in mids], {})
        with patch('meta_violation_cleanup.cover_source.material_rows', side_effect=lookup):
            return scan(self.source,self.store,bid)

    def test_three_distinct_threshold_and_initial_backlog(self):
        self.violations = self.violations[:2] * 3
        self.assertEqual(self.scan('below')['new_materials'],0)
        self.violations.append(dict(self.violations[0],ad_id='102'))
        summary = self.scan('first')
        self.assertEqual(summary['new_materials'],1)
        self.assertEqual(summary['new_ads'],4)
        self.assertEqual(len(self.store.ready_tasks()),4)

    def test_new_hour_associations_survive_expired_window(self):
        self.scan('first')
        self.violations = []
        self.rows['104'] = [dict(self.rows['103'][0],id='104',ad_id='104',creative_id='1104')]
        summary = self.scan('next')
        self.assertEqual(summary['new_materials'],0)
        self.assertEqual(summary['new_ads'],1)
        self.assertEqual(len(self.store.ready_tasks()),5)

    def test_out_of_scope_linkage_is_not_modified(self):
        self.rows['103'][0]['product']='826'
        self.assertEqual(self.scan('first')['new_ads'],3)

    def test_historical_deleted_receipt_excluded_before_queue(self):
        self.deleted={'103'}
        summary=self.scan('first')
        self.assertEqual(summary['historical_deleted_ads_skipped'],1)
        self.assertEqual(summary['new_ads'],3)


if __name__ == '__main__':
    unittest.main()
